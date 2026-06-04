"""Live-mode duplicate detection engine.

Scores a single newly observed service request against the set of
currently open SRs. This is the hot path used by the poller; it is
intentionally cheap (O(open_srs) per new SR) so the dashboard reflects
new duplicates within seconds of their arrival in Maximo.

The bulk batch detector (legacy.detect) is used by the full-scan path;
the engine here uses the same scoring vocabulary expressed via the
public helpers in `matching.normalize` and `matching.scorer`.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from duplicate_monitor.core.config import CFG
from duplicate_monitor.matching.normalize import normalize_arabic as _normalize_ar
from duplicate_monitor.matching.scorer import smart_text_compare as _smart_text_compare

log = logging.getLogger("duplicate_monitor.engine")


# ─── Date parsing — accept the many shapes Maximo / Excel emit ─────────────

_DT_PATTERNS = [
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%m/%d/%y %I:%M %p",     # Excel: "5/16/26 7:53 PM"
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


# ─── Scoring — mirrors find_duplicates.detect() per-pair logic ────────────

def score_pair(new: dict, existing: dict, *, max_days: int = 2) -> Optional[dict]:
    """
    Returns {score, reasons, classification} if the pair is a candidate,
    else None when blocked (e.g. time gap too large).
    """
    score = 0
    reasons: list[str] = []

    # ── Time gate ─────────────────────────────────────────────────
    d1 = _parse_dt(new.get("reported", ""))
    d2 = _parse_dt(existing.get("reported", ""))
    if d1 and d2:
        delta = abs((d1 - d2).total_seconds())
        gap_days = delta / 86400.0
        if gap_days > max_days:
            return None
        if d1.date() == d2.date():
            score += 2; reasons.append("نفس اليوم")
        elif gap_days <= 7:
            score += 1; reasons.append("نفس الأسبوع")

    # ── Fault (last 2 parts of Summary) ───────────────────────────
    f1 = _normalize_ar(_fault_blocking(new.get("summary", "")))
    f2 = _normalize_ar(_fault_blocking(existing.get("summary", "")))
    if not f1 or not f2 or f1 != f2:
        return None
    score += 3
    reasons.append("نفس العطل")

    # ── Location ──────────────────────────────────────────────────
    l1 = _normalize_ar(new.get("location", ""))
    l2 = _normalize_ar(existing.get("location", ""))
    if not l1 or not l2 or l1 != l2:
        return None
    score += 4
    reasons.append("نفس الموقع")

    # ── Asset ─────────────────────────────────────────────────────
    a1 = (new.get("asset", "") or "").strip()
    a2 = (existing.get("asset", "") or "").strip()
    if a1 and a2 and a1 != a2:
        return None
    if a1 and a2:
        score += 4
        reasons.append("نفس الأصل")

    # ── Text similarity ───────────────────────────────────────────
    d_a = new.get("detail", "") or new.get("summary", "")
    d_b = existing.get("detail", "") or existing.get("summary", "")
    if d_a and d_b:
        cls, bonus, sim = _smart_text_compare(d_a, d_b)
        sim_pct = int(sim if sim > 1 else sim * 100)
        if cls in ("different", "template_only") or sim_pct <= 80:
            return None
        if bonus:
            score += bonus
            reasons.append(f"نص متشابه ({sim_pct}%)")
        classification = cls
    else:
        return None

    return {
        "score":          score,
        "reasons":        reasons,
        "classification": classification,
    }


# ─── Top-level check for a new SR against a pool ──────────────────────────

def find_matches(new_sr: dict, pool: list[dict], *,
                 min_score: Optional[int] = None,
                 max_days: Optional[int]  = None) -> list[dict]:
    """
    Score the new SR against every open SR in `pool` and return matches
    with score ≥ min_score, sorted descending. Each match dict adds the
    matched-pool row under "match".
    """
    min_s = min_score if min_score is not None else CFG.min_score
    max_d = max_days  if max_days  is not None else CFG.max_days

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
