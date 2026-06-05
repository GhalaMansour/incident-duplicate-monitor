"""Tests for the duplicate scorer."""

from __future__ import annotations

from datetime import datetime

from duplicate_monitor.matching.scorer import (
    parse_date,
    score_pair,
    smart_text_compare,
)


def test_identical_text_scores_identical() -> None:
    cls, points, pct = smart_text_compare(
        "تسرب في الخزان رقم 5",
        "تسرب في الخزان رقم 5",
    )
    assert cls == "identical"
    assert points == 5
    assert pct >= 90


def test_template_only_with_different_numbers() -> None:
    cls, _points, _pct = smart_text_compare(
        "انقطاع كهرباء في المربع 12",
        "انقطاع كهرباء في المربع 47",
    )
    # Same boilerplate, different grid number, low token similarity.
    assert cls in ("template_only", "similar")


def test_completely_different_text_scores_different() -> None:
    cls, points, _pct = smart_text_compare(
        "تسرب مياه",
        "ضوء مكسور في المخيم",
    )
    assert cls == "different"
    assert points == 0


def test_score_pair_blocks_on_fault_and_location() -> None:
    record_a = {
        "loc": "MN03",
        "fault": "انقطاع كهرباء",
        "asset": "303076",
        "detail": "انقطاع كامل للإنارة في الشارع الرئيسي",
        "reported_dt": datetime(2026, 5, 21, 10, 0, 0),
    }
    record_b = {
        "loc": "MN03",
        "fault": "انقطاع كهرباء",
        "asset": "303076",
        "detail": "انقطاع كامل للإنارة في الشارع الرئيسي",
        "reported_dt": datetime(2026, 5, 21, 10, 30, 0),
    }
    score, reasons, metadata = score_pair(record_a, record_b)
    # 4 (loc) + 3 (fault) + 4 (asset) + 5 (identical text) + 3 (<1 day) = 19
    assert score >= 14
    assert any("الموقع" in r for r in reasons)
    assert any("العطل" in r for r in reasons)
    assert metadata["txt_class"] == "identical"


def test_parse_date_accepts_iso_and_us_formats() -> None:
    assert parse_date("2026-05-21 10:00:00") == datetime(2026, 5, 21, 10, 0, 0)
    assert parse_date("5/21/26 10:00 AM") == datetime(2026, 5, 21, 10, 0, 0)


def test_parse_date_returns_none_for_invalid() -> None:
    assert parse_date("") is None
    assert parse_date("nan") is None
    assert parse_date("not a date") is None
