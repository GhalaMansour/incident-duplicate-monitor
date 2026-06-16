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


def score_pair(record_a: dict, record_b: dict) -> tuple[int, list[str], dict]:
    """Score the similarity between two service-request records.

    The caller is expected to have already blocked the records by
    (fault, location) so a positive base score is added for those two
    matches. Additional points come from matching asset, smart text
    comparison, requestor number, and the reporting time gap.

    Args:
        record_a, record_b: Dicts with the keys consumed by the scorer:
            ``loc``, ``fault``, ``asset``, ``detail``, ``requestor_no``,
            ``reported_dt``. Missing keys default to falsy values.

    Returns:
        Tuple ``(score, reasons, metadata)``. ``score`` is the integer
        total. ``reasons`` is a human-readable list of contributing
        signals (Arabic). ``metadata`` carries diagnostic fields like
        ``tpl_pct`` and ``txt_class``.
    """
    score = 0
    reasons: list[str] = []
    metadata: dict = {"tpl_pct": 0, "txt_class": "different"}

    if record_a.get("loc"):
        reasons.append(f"نفس الموقع ({record_a['loc']})")
        score += 4

    if record_a.get("fault"):
        reasons.append(f"نفس العطل ({record_a['fault']})")
        score += 3

    asset_a = record_a.get("asset")
    asset_b = record_b.get("asset")
    if asset_a and asset_b and asset_a == asset_b:
        loc_n = normalize_arabic(record_a.get("loc", ""))
        asset_n = normalize_arabic(asset_a)
        if asset_n != loc_n:
            score += 4
            reasons.append(f"نفس الأصل ({asset_a})")
        else:
            reasons.append(f"نفس الأصل/الموقع ({asset_a})")

    classification, points, tpl_pct = smart_text_compare(
        record_a.get("detail", ""),
        record_b.get("detail", ""),
    )
    metadata["tpl_pct"] = tpl_pct
    metadata["txt_class"] = classification
    if points > 0:
        score += points
        label = {
            "identical": f"تفاصيل متطابقة ({tpl_pct}%)",
            "similar": f"تفاصيل متشابهة ({tpl_pct}%)",
        }.get(classification, "")
        if label:
            reasons.append(label)
    elif classification == "template_only":
        reasons.append(f"تنبيه: قالب موحّد بأرقام مختلفة ({tpl_pct}%)")

    if (
        record_a.get("requestor_no")
        and record_b.get("requestor_no")
        and record_a["requestor_no"] == record_b["requestor_no"]
    ):
        score += 2
        reasons.append(f"نفس رقم المبلّغ ({record_a['requestor_no']})")

    dt_a, dt_b = record_a.get("reported_dt"), record_b.get("reported_dt")
    if dt_a and dt_b:
        gap_days = abs((dt_b - dt_a).total_seconds()) / 86400.0
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
