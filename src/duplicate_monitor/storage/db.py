"""SQLite storage for the live monitor.

Three tables:
  - sr_seen      : every SR we've ever ingested (dedup key for polling)
  - alerts       : duplicate-match events surfaced to the user
  - poll_history : audit log of poll attempts (success/failure, source, count)

Single-file DB at live_monitor/monitor.db — safe to delete to reset.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Optional

from duplicate_monitor.core.config import CFG

# Process-wide lock — SQLite is fine for our scale but Python-side
# serialization prevents the "database is locked" race when poller and
# dashboard write simultaneously.
_LOCK = threading.RLock()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sr_seen (
    sr            TEXT PRIMARY KEY,
    first_seen    TEXT NOT NULL,
    reported      TEXT,
    status        TEXT,
    location      TEXT,
    asset         TEXT,
    summary       TEXT,
    detail        TEXT,
    payload_json  TEXT,
    source        TEXT
);

CREATE INDEX IF NOT EXISTS idx_sr_first_seen ON sr_seen(first_seen);
CREATE INDEX IF NOT EXISTS idx_sr_status     ON sr_seen(status);

CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at   TEXT NOT NULL,
    new_sr        TEXT NOT NULL,
    match_sr      TEXT NOT NULL,
    score         INTEGER NOT NULL,
    reasons       TEXT,
    new_summary   TEXT,
    new_location  TEXT,
    new_detail    TEXT,
    match_summary TEXT,
    match_location TEXT,
    state         TEXT NOT NULL DEFAULT 'open',
    decision      TEXT,
    note          TEXT,
    decided_at    TEXT,
    decided_by    TEXT,
    UNIQUE(new_sr, match_sr)
);

CREATE INDEX IF NOT EXISTS idx_alerts_state ON alerts(state);
CREATE INDEX IF NOT EXISTS idx_alerts_time  ON alerts(detected_at);

CREATE TABLE IF NOT EXISTS poll_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    source         TEXT NOT NULL,
    success        INTEGER NOT NULL DEFAULT 0,
    sr_fetched     INTEGER DEFAULT 0,
    sr_new         INTEGER DEFAULT 0,
    alerts_created INTEGER DEFAULT 0,
    error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_poll_time ON poll_history(started_at);

-- Dashboard sessions — one row per active browser cookie. Each row
-- records the Maximo username used to authenticate. Decisions made
-- inside that session are tagged with the username so the audit trail
-- shows who marked which group as a duplicate.
CREATE TABLE IF NOT EXISTS sessions (
    token        TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
"""


def _migrate_existing_db(c: sqlite3.Connection) -> None:
    """Backfill schema changes onto databases created before this commit.

    ``CREATE TABLE IF NOT EXISTS`` does not add new columns to an existing
    table, so old monitor.db files would be missing the
    ``alerts.decided_by`` column even after the schema string above was
    updated. This routine adds the column when missing.
    """
    existing = {row["name"] for row in c.execute("PRAGMA table_info(alerts)").fetchall()}
    if existing and "decided_by" not in existing:
        c.execute("ALTER TABLE alerts ADD COLUMN decided_by TEXT")


def _now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def _conn():
    with _LOCK:
        CFG.db_path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(CFG.db_path), timeout=10.0, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        try:
            yield c
        finally:
            c.close()


def init_db() -> None:
    """Idempotent — call once at startup."""
    with _conn() as c:
        c.executescript(_SCHEMA)
        _migrate_existing_db(c)


# ─── sr_seen ──────────────────────────────────────────────────────────────────


def upsert_sr(
    sr: str,
    *,
    reported: str = "",
    status: str = "",
    location: str = "",
    asset: str = "",
    summary: str = "",
    detail: str = "",
    payload: Optional[dict] = None,
    source: str = "unknown",
) -> bool:
    """Insert if new. Returns True if this SR is freshly seen."""
    with _conn() as c:
        row = c.execute("SELECT 1 FROM sr_seen WHERE sr=?", (sr,)).fetchone()
        if row:
            return False
        c.execute(
            "INSERT INTO sr_seen "
            "(sr, first_seen, reported, status, location, asset, summary, detail, payload_json, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                sr,
                _now_utc(),
                reported,
                status,
                location,
                asset,
                summary,
                detail,
                json.dumps(payload or {}, ensure_ascii=False),
                source,
            ),
        )
        return True


def count_seen() -> int:
    with _conn() as c:
        r = c.execute("SELECT COUNT(*) AS n FROM sr_seen").fetchone()
        return int(r["n"] or 0)


def recent_seen(limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM sr_seen ORDER BY first_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def open_srs() -> list[dict]:
    """SRs not in a closed/completed status — used as the comparison pool."""
    closed = ("CLOSE", "CLOSED", "COMP", "COMPLETED", "CAN", "CANCELLED")
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM sr_seen WHERE UPPER(IFNULL(status,'')) NOT IN "
            f"({','.join(['?'] * len(closed))}) "
            "ORDER BY reported DESC",
            closed,
        ).fetchall()
        return [dict(r) for r in rows]


# ─── alerts ───────────────────────────────────────────────────────────────────


def add_alert(
    new_sr: str, match_sr: str, score: int, reasons: list[str], new_meta: dict, match_meta: dict
) -> Optional[int]:
    """Returns new alert ID, or None if duplicate (already alerted)."""
    with _conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO alerts "
                "(detected_at, new_sr, match_sr, score, reasons, "
                " new_summary, new_location, new_detail, "
                " match_summary, match_location) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    _now_utc(),
                    new_sr,
                    match_sr,
                    score,
                    " · ".join(reasons or []),
                    new_meta.get("summary", ""),
                    new_meta.get("location", ""),
                    (new_meta.get("detail", "") or "")[:1000],
                    match_meta.get("summary", ""),
                    match_meta.get("location", ""),
                ),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # already alerted on this pair


def list_alerts(state: Optional[str] = None, limit: int = 200) -> list[dict]:
    q = "SELECT * FROM alerts"
    args: list[Any] = []
    if state:
        q += " WHERE state=?"
        args.append(state)
    q += " ORDER BY detected_at DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
        return [dict(r) for r in rows]


def update_alert(
    alert_id: int, *, decision: str = "", note: str = "", state: Optional[str] = None
) -> None:
    fields, args = [], []
    if decision:
        fields.append("decision=?")
        args.append(decision)
        fields.append("decided_at=?")
        args.append(_now_utc())
    if note:
        fields.append("note=?")
        args.append(note)
    if state:
        fields.append("state=?")
        args.append(state)
    if not fields:
        return
    args.append(alert_id)
    with _conn() as c:
        c.execute(f"UPDATE alerts SET {', '.join(fields)} WHERE id=?", args)


def alert_counts() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT state, COUNT(*) AS n FROM alerts GROUP BY state").fetchall()
        return {r["state"]: int(r["n"]) for r in rows}


# ─── poll_history ─────────────────────────────────────────────────────────────


def start_poll(source: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO poll_history (started_at, source) VALUES (?,?)",
            (_now_utc(), source),
        )
        return cur.lastrowid


def finish_poll(
    poll_id: int,
    *,
    success: bool,
    sr_fetched: int = 0,
    sr_new: int = 0,
    alerts_created: int = 0,
    error: str = "",
) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE poll_history SET finished_at=?, success=?, sr_fetched=?, "
            "sr_new=?, alerts_created=?, error=? WHERE id=?",
            (
                _now_utc(),
                1 if success else 0,
                sr_fetched,
                sr_new,
                alerts_created,
                error[:500],
                poll_id,
            ),
        )


def recent_polls(limit: int = 30) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM poll_history ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def health_summary() -> dict:
    """One-glance health for the dashboard banner."""
    with _conn() as c:
        last = c.execute("SELECT * FROM poll_history ORDER BY started_at DESC LIMIT 1").fetchone()
        ok24 = c.execute(
            "SELECT COUNT(*) AS n FROM poll_history WHERE success=1 "
            "AND started_at >= datetime('now','-1 day')"
        ).fetchone()
        fail24 = c.execute(
            "SELECT COUNT(*) AS n FROM poll_history WHERE success=0 "
            "AND started_at >= datetime('now','-1 day')"
        ).fetchone()
    return {
        "last_poll": dict(last) if last else None,
        "ok_24h": int(ok24["n"] or 0) if ok24 else 0,
        "fail_24h": int(fail24["n"] or 0) if fail24 else 0,
    }


# ─── sessions ────────────────────────────────────────────────────────────────


def create_session(token: str, username: str, ttl_hours: int = 12) -> None:
    """Insert a new login session. ``token`` is the random value sent
    back to the browser as a cookie; ``username`` is the Maximo login."""
    from datetime import timedelta

    now = datetime.now(UTC)
    expires = now + timedelta(hours=ttl_hours)
    with _conn() as c:
        c.execute(
            "INSERT INTO sessions (token, username, created_at, last_seen_at, expires_at) "
            "VALUES (?,?,?,?,?)",
            (
                token,
                username,
                now.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
                expires.isoformat(timespec="seconds"),
            ),
        )


def lookup_session(token: str) -> Optional[dict]:
    """Return the session row for ``token`` if it is still valid.
    Updates ``last_seen_at`` as a side effect so an active reviewer's
    session is kept alive."""
    if not token:
        return None
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM sessions WHERE token=? AND expires_at >= ?",
            (token, now),
        ).fetchone()
        if not row:
            return None
        c.execute("UPDATE sessions SET last_seen_at=? WHERE token=?", (now, token))
        return dict(row)


def delete_session(token: str) -> None:
    """Remove a session row (logout)."""
    if not token:
        return
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


def purge_expired_sessions() -> int:
    """Remove every session whose expiry has passed. Returns the number
    of rows removed. Cheap; safe to call at startup and periodically."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with _conn() as c:
        result = c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        return result.rowcount or 0


def set_decision(
    alert_id: int,
    decision: str,
    note: str = "",
    decided_by: str = "",
) -> bool:
    """Record a reviewer's decision on an alert. ``decided_by`` is the
    Maximo username of the logged-in reviewer; stored so the audit
    trail names who classified the duplicate."""
    with _conn() as c:
        result = c.execute(
            "UPDATE alerts SET state='decided', decision=?, note=?, "
            "decided_at=?, decided_by=? WHERE id=?",
            (decision, note[:500], _now_utc(), decided_by, alert_id),
        )
        return (result.rowcount or 0) > 0
