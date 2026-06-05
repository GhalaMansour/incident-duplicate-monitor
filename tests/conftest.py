"""Shared pytest fixtures for the duplicate monitor test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip Maximo and SMTP credentials so tests never accidentally fire.

    Tests that need real values override via ``monkeypatch.setenv``.
    """
    for var in (
        "MAXIMO_BASE_URL",
        "MAXIMO_USER",
        "MAXIMO_PASS",
        "MAXIMO_SITE_ID",
        "LM_NOTIFY_EMAIL",
        "LM_SMTP_HOST",
        "LM_WEBHOOK_URL",
    ):
        monkeypatch.delenv(var, raising=False)
