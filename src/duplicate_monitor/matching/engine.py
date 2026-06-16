"""Live-mode duplicate detection engine.

Scores a single newly observed service request against the set of
currently open SRs. This is the hot path used by the poller; it is
intentionally cheap (O(open_srs) per new SR) so alerts surface within
seconds of a new SR arriving in Maximo.

The actual pair scoring lives in ``duplicate_monitor.matching.scorer``;
this module's ``score_pair`` is a thin adapter that converts the
poller-shaped raw record into the dict shape the shared scorer expects
and delegates the decision to ``scorer.score_pair``. The bulk path
(``legacy.detect``) delegates to the same function, so the two paths
are guaranteed to score identical pairs identically.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from duplicate_monitor.core.config import CFG
from duplicate_monitor.matching.scorer import score_pair as _scorer_score_pair

log = logging.getLogger("duplicate_monitor.engine")


# ─── Date parsing — accept the many shapes Maximo / Excel emit ─────────────

_DT_PATTERNS = [
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%m/%d/%y %I:%M %p",  # Excel: "5/16/26 7:53 PM"
    "%m/%d/%Y %I:%M %p",
    "%d/%m/%Y %H:%M",
    "%Y-%m-%d",
]


def _parse_dt(raw: str) -> Optional[datetime]:
    """Parse a datetime string and always return a timezone-naive object."""
    if not raw or raw in ("nan", "None"):
        return None
    raw = str(raw).strip().replace("Z", "+00:00")
    for fmt in _DT_PATTERNS:
        try:
            dt = datetime.strptime(raw, fmt)
            # Strip tzinfo so all datetimes are naive — avoids mixed-tz arithmetic
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            return dt
        except ValueError:
            continue
    # last resort: pandas-style fallback
    try:
        import pandas as pd

        v = pd.to_datetime(raw, errors="coerce")
        if v is not None and str(v) != "NaT":
            dt = v.to_pydatetime()  # type: ignore[union-attr]
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            return dt
    except Exception:
        pass
    return None


def _fault_blocking(summary: str) -> str:
    """Same logic as find_duplicates: last 2 comma-separated parts."""
    parts = [p.strip() for p in (summary or "").split(",") if p.strip()]
    return ",".join(parts[-2:]) if len(parts) >= 2 else (summary or "")


# ─── Scoring — delegates to scorer.score_pair ─────────────────────────────


def _to_scorer_record(raw: dict) -> dict:
    """Normalize a poller-shaped record into the dict shape the scorer
    consumes. Returns the keys ``loc``, ``fault``, ``asset``, ``detail``,
    ``requestor_no``, ``reported_dt``."""
    summary = raw.get("summary", "")
    return {
        "loc": raw.get("location", ""),
        "fault": _fault_blocking(summary),
        "asset": (raw.get("asset", "") or "").strip(),
        "detail": raw.get("detail", "") or summary,
        "requestor_no": raw.get("requestor_no", ""),
        "reported_dt": _parse_dt(raw.get("reported", "")),
    }


def score_pair(new: dict, existing: dict, *, max_days: int = 2) -> Optional[dict]:
    """
    Returns {score, reasons, classification} if the pair is a candidate,
    else None when any hard gate fails or the time gap exceeds
    ``max_days``.

    Delegates the full scoring decision to ``scorer.score_pair`` so the
    live path and the bulk path stay rule-identical.
    """
    score, reasons, metadata = _scorer_score_pair(
        _to_scorer_record(new),
        _to_scorer_record(existing),
        max_days=max_days,
    )
    if score == 0:
        return None
    return {
        "score": score,
        "reasons": reasons,
        "classification": metadata.get("txt_class", "different"),
    }


# ─── Top-level check for a new SR against a pool ──────────────────────────


def find_matches(
    new_sr: dict,
    pool: list[dict],
    *,
    min_score: Optional[int] = None,
    max_days: Optional[int] = None,
) -> list[dict]:
    """
    Score the new SR against every open SR in `pool` and return matches
    with score ≥ min_score, sorted descending. Each match dict adds the
    matched-pool row under "match".
    """
    min_s = min_score if min_score is not None else CFG.min_score
    max_d = max_days if max_days is not None else CFG.max_days

    matches: list[dict] = []
    new_sr_id = new_sr.get("sr", "")
    for ex in pool:
        if ex.get("sr") == new_sr_id:
            continue
        result = score_pair(new_sr, ex, max_days=max_d)
        if not result:
            continue
        if result["score"] < min_s:
            continue
        matches.append({**result, "match": ex})

    matches.sort(key=lambda m: m["score"], reverse=True)
    return matches
