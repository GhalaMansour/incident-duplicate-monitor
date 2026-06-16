"""Duplicate similarity scorer for Arabic service requests.

The scoring algorithm combines four signals:

1. **Template similarity** — the fault description with numbers stripped
   is compared as a sequence-matched template.
2. **Number overlap** — the actual numbers (asset ids, signpost
   references, areas) that appear inside the description.
3. **Token similarity** — bag-of-tokens with fuzzy containment for
   short reports that say the same thing with extra context.
4. **Time gap** — service requests reported close in time are weighted
   higher.

A full write-up of thresholds, signals, and worked examples is in
``docs/scoring_algorithm.md``. The helpers below were lifted from the
original ``scripts/find_duplicates.py`` and were renamed from internal
underscore-prefixed names to public ones; they are the documented
public API of this module.
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta
from typing import Optional

from .normalize import normalize_arabic, strip_html

_NUM_PATTERN = re.compile(r"\d+(?:/\d+)?")

_DETAIL_STOPWORDS = frozenset(
    {
        "من",
        "في",
        "عن",
        "على",
        "الى",
        "الي",
        "رقم",
        "عدد",
        "داخل",
        "بداخل",
        "اقرب",
        "معلم",
        "الدوره",
        "الدورة",
        "دوره",
        "دورة",
        "المياه",
        "مياه",
        "القسم",
        "النسائي",
        "حمامات",
        "حسب",
        "افادة",
        "المبلغ",
    }
)


def smart_text_compare(a: str, b: str) -> tuple[str, int, int]:
    """Compare two free-text descriptions by template and by numbers.

    Returns a tuple ``(classification, points, template_pct)``:

    * ``classification``:
        - ``"identical"`` — template >= 90% and numbers overlap >= 50%
        - ``"similar"`` — template >= 90% regardless of numbers
        - ``"template_only"`` — template >= 90% but the numbers differ
          (numbers overlap < 30%): same boilerplate, different asset
          or grid number
        - ``"different"`` — template < 90%
    * ``points``: the score contribution (5 for identical, 3 for
      similar, 0 otherwise)
    * ``template_pct``: integer percentage similarity of the template

    Examples:
        >>> cls, pts, pct = smart_text_compare(
        ...     "تسرّب من الخزان رقم 5",
        ...     "تسرّب من الخزان رقم 5",
        ... )
        >>> cls
        'identical'
    """
    a_norm = normalize_arabic(strip_html(a or ""))
    b_norm = normalize_arabic(strip_html(b or ""))
    a_template = _NUM_PATTERN.sub("#", a_norm)
    b_template = _NUM_PATTERN.sub("#", b_norm)

    matcher = difflib.SequenceMatcher(None, a_template, b_template, autojunk=False)
    template_pct = int(matcher.ratio() * 100)
    # token_pct keeps the original numbers so that two SRs with the same
    # boilerplate but different asset/grid numbers diverge here and trip
    # the template_only guard.
    token_pct = _token_similarity_pct(a_norm, b_norm)
    final_pct = max(template_pct, token_pct)

    a_numbers = set(_NUM_PATTERN.findall(a or ""))
    b_numbers = set(_NUM_PATTERN.findall(b or ""))
    if a_numbers or b_numbers:
        numbers_overlap = len(a_numbers & b_numbers) / max(len(a_numbers | b_numbers), 1)
    else:
        numbers_overlap = 1.0

    if final_pct >= 90 and numbers_overlap >= 0.5:
        return ("identical", 5, final_pct)
    if template_pct >= 90 and numbers_overlap < 0.3:
        return ("template_only", 0, final_pct)
    if final_pct >= 90:
        return ("similar", 3, final_pct)
    return ("different", 0, final_pct)


def score_pair(record_a: dict, record_b: dict, *, max_days: int = 2) -> tuple[int, list[str], dict]:
    """Score the similarity between two service-request records.

    This is the single source of truth for pair scoring. Both the live
    detection path (``engine.py``) and the bulk detection path
    (``legacy.py``) delegate to this function so the four hard
    requirements and the score values are guaranteed identical.

    The four hard requirements (any failure returns score 0):
      1. Same location (``loc``).
      2. Same fault category (``fault``).
      3. Same asset id when both sides carry one. Lenient when either
         side has none.
      4. Description similarity >= 90 % (text class ``identical`` or
         ``similar``). ``template_only`` and ``different`` both fail.

    Plus a soft time gate: if both ``reported_dt`` values are present
    and the gap exceeds ``max_days``, the pair is dropped.

    Args:
        record_a, record_b: Dicts with the keys consumed by the scorer:
            ``loc``, ``fault``, ``asset``, ``detail``, ``requestor_no``,
            ``reported_dt``. Missing keys default to falsy values.
        max_days: Maximum allowed reporting gap in days. Pairs reported
            further apart are dropped.

    Returns:
        Tuple ``(score, reasons, metadata)``. ``score`` is the integer
        total — 0 if any hard gate failed. ``reasons`` lists the
        contributing signals (Arabic). ``metadata`` carries diagnostic
        fields including ``tpl_pct``, ``txt_class``, and ``gate``
        (set to the name of the failed gate when score is 0).
    """
    reasons: list[str] = []
    metadata: dict = {"tpl_pct": 0, "txt_class": "different"}

    # ── Gate 1: same location (required) ────────────────────────────
    loc_a = record_a.get("loc") or ""
    loc_b = record_b.get("loc") or ""
    if not loc_a or not loc_b or normalize_arabic(loc_a) != normalize_arabic(loc_b):
        metadata["gate"] = "location"
        return 0, ["موقعان مختلفان"], metadata

    # ── Gate 2: same fault (required) ───────────────────────────────
    fault_a = record_a.get("fault") or ""
    fault_b = record_b.get("fault") or ""
    if not fault_a or not fault_b or normalize_arabic(fault_a) != normalize_arabic(fault_b):
        metadata["gate"] = "fault"
        return 0, ["عطلان مختلفان"], metadata

    # ── Gate 3: same asset when both present (required) ────────────
    asset_a = (record_a.get("asset") or "").strip()
    asset_b = (record_b.get("asset") or "").strip()
    if asset_a and asset_b and asset_a != asset_b:
        metadata["gate"] = "asset"
        metadata["asset_mismatch"] = True
        return 0, ["أصول مختلفة — ليس تكراراً"], metadata

    # ── Gate 4: text similarity >= 90 % (required) ─────────────────
    classification, txt_points, tpl_pct = smart_text_compare(
        record_a.get("detail", ""),
        record_b.get("detail", ""),
    )
    metadata["tpl_pct"] = tpl_pct
    metadata["txt_class"] = classification
    if classification in ("different", "template_only"):
        metadata["gate"] = "text"
        if classification == "template_only":
            reasons.append(f"تنبيه: قالب موحّد بأرقام مختلفة ({tpl_pct}%)")
        return 0, reasons, metadata

    # ── Time gate (soft, configurable) ──────────────────────────────
    dt_a, dt_b = record_a.get("reported_dt"), record_b.get("reported_dt")
    gap_days: float | None = None
    if dt_a and dt_b:
        gap_days = abs((dt_b - dt_a).total_seconds()) / 86400.0
        if gap_days > max_days:
            metadata["gate"] = "time"
            return 0, [f"فارق زمني كبير ({gap_days:.1f} يوم)"], metadata

    # ── All gates passed — tally the points ─────────────────────────
    score = 0

    reasons.append(f"نفس الموقع ({loc_a})")
    score += 4

    reasons.append(f"نفس العطل ({fault_a})")
    score += 3

    if asset_a and asset_b:
        if normalize_arabic(asset_a) != normalize_arabic(loc_a):
            score += 4
            reasons.append(f"نفس الأصل ({asset_a})")
        else:
            reasons.append(f"نفس الأصل/الموقع ({asset_a})")

    score += txt_points
    label = {
        "identical": f"تفاصيل متطابقة ({tpl_pct}%)",
        "similar": f"تفاصيل متشابهة ({tpl_pct}%)",
    }.get(classification, "")
    if label:
        reasons.append(label)

    if (
        record_a.get("requestor_no")
        and record_b.get("requestor_no")
        and record_a["requestor_no"] == record_b["requestor_no"]
    ):
        score += 2
        reasons.append(f"نفس رقم المبلّغ ({record_a['requestor_no']})")

    if gap_days is not None:
        if gap_days < 1:
            score += 3
            reasons.append("نفس اليوم (+3)")
        elif gap_days < 2:
            score += 2
            reasons.append("فارق يوم واحد (+2)")
        elif gap_days <= 2:
            score += 1
            reasons.append("فارق يومان (+1)")

    return score, reasons, metadata


def parse_date(value: str) -> Optional[datetime]:
    """Parse a Maximo-emitted date string to a :class:`datetime`.

    Tries a small list of formats observed in Kidana Maximo exports and
    OSLC payloads, then falls back to :func:`pandas.to_datetime` when
    available.

    Returns ``None`` if parsing fails.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "null", "<na>"):
        return None
    formats = (
        "%m/%d/%y %I:%M %p",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%y %H:%M",
        "%m/%d/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M",
        "%d/%m/%y %I:%M %p",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        import pandas as pd

        result = pd.to_datetime(text, errors="coerce", dayfirst=False)
        if pd.isna(result):
            return None
        return result.to_pydatetime()
    except Exception:
        return None


def format_arabic_gap(gap: Optional[timedelta]) -> str:
    """Format a time gap as a short Arabic string ("+12 دقيقة", "+2 يوم", ...).

    Returns the placeholder ``"—"`` for ``None`` input.
    """
    if gap is None:
        return "—"
    total = abs(int(gap.total_seconds()))
    if total < 60:
        return f"+{total} ثانية"
    if total < 3600:
        minutes = total // 60
        return f"+{minutes} دقيقة"
    if total < 86400:
        hours = total // 3600
        minutes = (total % 3600) // 60
        return f"+{hours} ساعة {minutes} دقيقة" if minutes else f"+{hours} ساعة"
    days = total // 86400
    if days <= 14:
        return f"+{days} يوم"
    weeks = days // 7
    if weeks <= 8:
        return f"+{weeks} أسبوع"
    months = days // 30
    if months <= 12:
        return f"+{months} شهر"
    years = days // 365
    return f"+{years} سنة"


def _token_similarity_pct(a: str, b: str) -> int:
    tokens_a, tokens_b = _detail_tokens(a), _detail_tokens(b)
    if not tokens_a or not tokens_b:
        return 0
    intersection = len(tokens_a & tokens_b)
    dice = (2 * intersection) / max(len(tokens_a) + len(tokens_b), 1)
    containment = intersection / max(min(len(tokens_a), len(tokens_b)), 1)
    fuzzy = _fuzzy_token_containment(tokens_a, tokens_b)
    pct = max(dice, containment * 0.85, fuzzy)
    return int(round(pct * 100))


def _detail_tokens(text: str) -> set[str]:
    # Numbers are preserved as tokens so that two SRs with the same
    # boilerplate but different asset/grid references diverge here and
    # the template_only guard can fire.
    tokens = re.findall(r"[؀-ۿa-zA-Z0-9/]+", text or "")
    return {token for token in tokens if len(token) > 1 and token not in _DETAIL_STOPWORDS}


def _fuzzy_token_containment(a: set[str], b: set[str]) -> float:
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    if not small:
        return 0.0
    matched = 0
    for token in small:
        if token in large:
            matched += 1
            continue
        if any(
            difflib.SequenceMatcher(None, token, other, autojunk=False).ratio() >= 0.72
            for other in large
        ):
            matched += 1
    return matched / len(small)


__all__ = (
    "smart_text_compare",
    "score_pair",
    "parse_date",
    "format_arabic_gap",
)
