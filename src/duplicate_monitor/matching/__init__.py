"""Matching helpers used to detect duplicate service requests.

This subpackage carries the project's core matching IP: Arabic-aware
text normalization and a multi-signal scorer that combines template
similarity, number overlap, token similarity, and time-gap weighting.
See ``docs/scoring_algorithm.md`` for the full algorithm.
"""

from .normalize import normalize_arabic, strip_html
from .scorer import format_arabic_gap, parse_date, score_pair, smart_text_compare

__all__ = (
    "normalize_arabic",
    "strip_html",
    "smart_text_compare",
    "score_pair",
    "parse_date",
    "format_arabic_gap",
)
