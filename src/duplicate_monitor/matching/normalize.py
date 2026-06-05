"""Arabic text normalization helpers used by the duplicate scorer.

The scoring algorithm in :mod:`duplicate_monitor.matching.scorer` is
sensitive to two cosmetic differences that should not count: Arabic
script variation (alef forms, ta marbuta vs. ha, tatweel) and HTML
markup carried over from Maximo's rich-text Details field. The
functions here normalize both away before scoring.

The implementations were lifted from the original
``scripts/find_duplicates.py`` and were renamed from internal
underscore-prefixed names to public ones since they are now the
documented public API of this module.
"""

from __future__ import annotations

import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_html(text: str) -> str:
    """Remove HTML tags, comments, and decode the most common entities.

    Maximo stores the Details field as rich text, which arrives as an
    HTML fragment over OSLC. Stripping is intentionally permissive — we
    are not trying to render the markup, only to extract a comparable
    plain-text form.

    Args:
        text: Raw HTML or plain text. ``None``-like values are returned
            unchanged.

    Returns:
        Text with tags, comments, and basic entities removed and
        whitespace collapsed.
    """
    if not text:
        return text
    text = _HTML_COMMENT_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    )
    return re.sub(r"\s+", " ", text).strip()


def normalize_arabic(text: str) -> str:
    """Apply Arabic script normalization and lowercase any Latin letters.

    The transformations:

    * Replace non-breaking space (``\\xa0``) with a regular space.
    * Collapse the alef variants ``إأآا`` to plain ``ا``.
    * Replace alef maqsura ``ى`` with ya ``ي``.
    * Replace ta marbuta ``ة`` with ha ``ه``.
    * Lowercase Latin letters and collapse whitespace.

    Args:
        text: The text to normalize.

    Returns:
        Normalized text. An empty input returns an empty string.
    """
    if not text:
        return ""
    text = text.replace("\xa0", " ").strip()
    text = re.sub(r"[إأآا]", "ا", text)
    text = text.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", text).lower()


__all__ = ("strip_html", "normalize_arabic")
