"""Background polling loop.

Runs forever (or until SIGINT) and on each tick:
  1. Picks a source (Maximo OSLC, file watcher, or auto-fallback)
  2. Fetches recent SRs
  3. Inserts each new SR into sr_seen
  4. For each NEW SR: scores it against all currently open SRs
  5. Emits alerts when score ≥ CFG.alert_score
  6. Logs the result to poll_history

Designed to be resilient — a Maximo outage does not crash the loop, and
the next tick will retry. After 3 consecutive Maximo failures we
automatically switch to file mode until the API recovers.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
import traceback
from typing import Optional

from duplicate_monitor.core.config import CFG
from duplicate_monitor.matching.engine import find_matches
from duplicate_monitor.scanner import full_scan as _scanner
from duplicate_monitor.sources.file import FileSource, FileSourceError
from duplicate_monitor.sources.maximo import MaximoSource, MaximoSourceError
from duplicate_monitor.storage import db

log = logging.getLogger("duplicate_monitor.poller")


# ─── Source selector ─────────────────────────────────────────────────────


class SourceManager:
    """Picks Maximo first, falls back to file after N failures."""

    FAIL_THRESHOLD = 3

    def __init__(self):
        self.maximo = MaximoSource()
        self.files = FileSource()
        self._maximo_failures = 0
        self._mode_override: Optional[str] = None  # forced by config

        if CFG.source_mode in ("maximo", "file"):
            self._mode_override = CFG.source_mode

    def current_mode(self) -> str:
        if self._mode_override:
            return self._mode_override
        if self.maximo.configured() and self._maximo_failures < self.FAIL_THRESHOLD:
            return "maximo"
        if self.files.configured():
            return "file"
        return "none"

    def fetch(self) -> tuple[str, list[dict]]:
        """Returns (source_label, rows). Raises if every source fails."""
        mode = self.current_mode()
        errors: list[str] = []

        if mode == "maximo" or (mode == "auto" and self.maximo.configured()):
            try:
                rows = self.maximo.fetch_recent()
                self._maximo_failures = 0
                return ("maximo", rows)
            except MaximoSourceError as e:
                self._maximo_failures += 1
                errors.append(f"maximo: {e}")
                log.warning(
                    "Maximo fetch failed (%d/%d): %s", self._maximo_failures, self.FAIL_THRESHOLD, e
                )
                if self._mode_override == "maximo":
                    raise

        # File fallback
        if self.files.configured():
            try:
                rows = self.files.fetch_recent()
                return ("file", rows)
            except FileSourceError as e:
                errors.append(f"file: {e}")
                log.warning("File fetch failed: %s", e)

        raise RuntimeError("All sources failed → " + " | ".join(errors))


# ─── One polling tick ────────────────────────────────────────────────────


def run_tick(sources: SourceManager) -> dict:
    """Execute one fetch + score cycle. Returns a summary dict."""
    poll_id = db.start_poll(source=sources.current_mode())
    summary = {"source": "", "fetched": 0, "new": 0, "alerts": 0, "error": ""}

    try:
        source, rows = sources.fetch()
        summary["source"] = source
        summary["fetched"] = len(rows)

        # Snapshot the open SR pool BEFORE we ingest new ones.
        # Primary pool: all SRs from the last full scan (all_rows from pkl).
        # Fallback: only the SQLite-cached SRs seen so far.
        # Using the full scan pool means a brand-new SR is compared against
        # ALL historical SRs — not just the ones the poller happened to see.
        pool = db.open_srs()
        try:
            _scan = _scanner.load_scan()
            if _scan and _scan.get("all_rows"):
                pool_srs = {r["sr"] for r in pool}
                for _row in _scan["all_rows"]:
                    _sr = _row.get("sr", "")
                    if _sr and _sr not in pool_srs:
                        pool.append(
                            {
                                "sr": _sr,
                                "reported": str(_row.get("reported", "")),
                                "location": _row.get("loc", ""),
                                "asset": _row.get("asset", ""),
                                "summary": _row.get("fault_orig") or _row.get("fault", ""),
                                "detail": _row.get("detail", ""),
                            }
                        )
                        pool_srs.add(_sr)
        except Exception:
            pass  # fall back to db pool silently

        new_count = 0
        alert_count = 0
        for r in rows:
            sr = (r.get("sr") or "").strip()
            if not sr:
                continue
            inserted = db.upsert_sr(
                sr,
                reported=r.get("reported", ""),
                status=r.get("status", ""),
                location=r.get("location", ""),
                asset=r.get("asset", ""),
                summary=r.get("summary", ""),
                detail=r.get("detail", ""),
                payload=r.get("_raw", {}),
                source=source,
            )
            if not inserted:
                continue  # already seen
            new_count += 1

            # Score against the pre-tick open pool
            matches = find_matches(
                r,
                pool,
                min_score=CFG.min_score,
                max_days=CFG.max_days,
            )
            for m in matches:
                if m["score"] < CFG.alert_score:
                    continue
                ex = m["match"]
                added = db.add_alert(
                    new_sr=sr,
                    match_sr=ex["sr"],
                    score=m["score"],
                    reasons=m["reasons"],
                    new_meta={
                        "summary": r.get("summary", ""),
                        "location": r.get("location", ""),
                        "detail": r.get("detail", ""),
                    },
                    match_meta={
                        "summary": ex.get("summary", ""),
                        "location": ex.get("location", ""),
                    },
                )
                if added:
                    alert_count += 1
                    log.info(
                        "ALERT created | new=%s ~ %s | score=%d (%s)",
                        sr,
                        ex["sr"],
                        m["score"],
                        " · ".join(m["reasons"]),
                    )

        summary["new"] = new_count
        summary["alerts"] = alert_count
        db.finish_poll(
            poll_id,
            success=True,
            sr_fetched=len(rows),
            sr_new=new_count,
            alerts_created=alert_count,
        )
        log.info(
            "Tick OK | source=%s | fetched=%d | new=%d | alerts=%d",
            source,
            len(rows),
            new_count,
            alert_count,
        )

    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        summary["error"] = err
        log.error("Tick FAILED: %s\n%s", err, traceback.format_exc())
        db.finish_poll(poll_id, success=False, error=err)

    return summary


# ─── Main loop ───────────────────────────────────────────────────────────

_STOP = False


def _on_signal(signum, _frame):
    global _STOP
    _STOP = True
    log.info("Signal %s received — stopping after current tick", signum)


def main_loop():
    """Block forever, polling at CFG.poll_interval_sec intervals."""
    db.init_db()

    # Overlay any values the operator saved through the dashboard
    # settings page so the live service uses them even when the
    # operator never touched .env.
    from duplicate_monitor.storage import settings as _settings

    _settings.apply_to_cfg()

    sources = SourceManager()

    log.info("=" * 60)
    log.info("Live monitor starting")
    log.info("=" * 60)
    log.info("\n%s", CFG.summary())

    # Graceful shutdown — signal handlers only work in the main thread.
    # When poller runs as a daemon thread (e.g. `run both`), the parent
    # process owns SIGINT and the thread just dies when the dashboard exits.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _on_signal)

    _consec_failures = 0
    _BACKOFF_STEPS = [1, 2, 4, 8, 16, 32]  # multipliers × poll_interval_sec
    _last_scan_time = 0.0  # monotonic timestamp of last full scan

    while not _STOP:
        t0 = time.monotonic()
        summary = run_tick(sources)
        elapsed = time.monotonic() - t0

        if summary.get("error"):
            _consec_failures += 1
            # After the first failure we log normally; subsequent identical
            # failures only get a short "still failing" note so the log stays
            # readable instead of accumulating hundreds of identical lines.
            multiplier = _BACKOFF_STEPS[min(_consec_failures - 1, len(_BACKOFF_STEPS) - 1)]
            sleep_for = min(
                CFG.poll_interval_sec * multiplier,
                3600,  # cap at 1 hour
            )
            if _consec_failures > 1:
                log.warning(
                    "Still failing (consecutive=%d). Backing off %ds before next attempt.",
                    _consec_failures,
                    sleep_for,
                )
        else:
            if _consec_failures > 0:
                log.info("Recovered after %d consecutive failures.", _consec_failures)
            _consec_failures = 0
            sleep_for = max(5, CFG.poll_interval_sec - elapsed)

            # ── Quick scan every tick: newest Maximo page only ────────
            if sources.maximo.configured():
                quick_summary = _scanner.run_quick_scan(sources.maximo)
                if quick_summary.get("error"):
                    log.warning("Quick scanner error: %s", quick_summary["error"])
                elif not quick_summary.get("unchanged"):
                    log.info(
                        "Quick scanner: +%d SRs · %d groups · %d pairs",
                        quick_summary.get("new_count", 0),
                        quick_summary.get("group_count", 0),
                        quick_summary.get("pair_count", 0),
                    )

            # ── Full scan every LM_SCAN_SEC (default 5 min) ──────────
            now = time.monotonic()
            if (now - _last_scan_time) >= CFG.scan_interval_sec:
                if sources.maximo.configured():
                    log.info("Scanner: starting full scan of all open SRs …")
                    scan_summary = _scanner.run_scan(sources.maximo)
                    log.info(
                        "Scanner: done — %d SRs · %d groups · %d pairs",
                        scan_summary["sr_count"],
                        scan_summary["group_count"],
                        scan_summary["pair_count"],
                    )
                    if scan_summary.get("error"):
                        log.warning("Scanner error: %s", scan_summary["error"])
                else:
                    log.debug("Scanner: Maximo not configured, skipping full scan")
                _last_scan_time = now

        # Sleep in 1-second slices so signals are picked up quickly
        for _ in range(int(sleep_for)):
            if _STOP:
                break
            time.sleep(1)

    log.info("Live monitor stopped.")


if __name__ == "__main__":
    main_loop()
