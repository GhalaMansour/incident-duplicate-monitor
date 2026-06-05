"""Tests for the Arabic normalization helpers."""

from __future__ import annotations

import pytest

from duplicate_monitor.matching.normalize import normalize_arabic, strip_html


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("إنذار", "انذار"),
        ("أمنية", "امنيه"),
        ("آمل", "امل"),
        ("ياسمين", "ياسمين"),
        ("مكتبة", "مكتبه"),
        ("Mina فرع 12", "mina فرع 12"),
        ("نص\xa0به\xa0nbsp", "نص به nbsp"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_arabic_collapses_variants(raw: str, expected: str) -> None:
    assert normalize_arabic(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<p>تسرب مياه</p>", "تسرب مياه"),
        ("<!-- comment --><b>ok</b>", "ok"),
        ("a&nbsp;&amp;b", "a &b"),
        ("plain text", "plain text"),
        ("", ""),
        (None, None),
    ],
)
def test_strip_html_removes_tags_and_entities(raw, expected) -> None:
    assert strip_html(raw) == expected
