"""User-editable settings stored alongside the SQLite database.

This module gives non-technical operators a way to configure the
Maximo connection from the dashboard instead of editing ``.env``.

How the layered configuration works:

  1. ``.env`` provides the initial values on first run (boot-strap).
  2. As soon as the operator saves from the dashboard, the JSON file
     here becomes the source of truth and overlays ``.env`` on every
     subsequent start. This matches the operator mental model: what
     you set in the dashboard is what runs.
  3. If neither source has a value, the field is empty.

To revert to ``.env``-driven config (typical IT-managed deployment),
delete the JSON file at ``user_settings.json`` and restart.

The JSON file is stored next to ``monitor.db`` so it lives with the
service's other runtime data and is excluded from git the same way.

Saved fields are Maximo connection details only — never anything
about the duplicate-detection algorithm itself, which stays in
``.env`` / ``core.config``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from duplicate_monitor.core.config import CFG, PACKAGE_DIR

log = logging.getLogger("duplicate_monitor.settings")

# JSON file location — beside monitor.db, gitignored by *.db / *.json
# patterns already in place.
_SETTINGS_PATH: Path = PACKAGE_DIR / "user_settings.json"

# Schema: which keys are user-editable from the dashboard.
_ALLOWED_KEYS = ("maximo_base_url", "maximo_user", "maximo_pass")

_LOCK = threading.RLock()


def _read_file() -> dict[str, Any]:
    """Load the JSON file if it exists. Missing or unreadable file
    returns an empty dict so the caller falls back to env-only mode."""
    if not _SETTINGS_PATH.exists():
        return {}
    try:
        with _SETTINGS_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Settings file unreadable, ignoring: %s", exc)
        return {}


def _write_file(data: dict[str, Any]) -> None:
    """Atomic write to the JSON file."""
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    tmp.replace(_SETTINGS_PATH)


def apply_to_cfg() -> None:
    """Overlay every value the dashboard saved on top of ``CFG`` and
    ``os.environ``. Called once at startup. Dashboard saves win over
    ``.env`` so the operator's intent is what runs."""
    with _LOCK:
        data = _read_file()
        if not data:
            return
        for key in _ALLOWED_KEYS:
            value = data.get(key)
            if not value:
                continue
            setattr(CFG, key, value)
            # Mirror to environment so the Maximo source's probes
            # and any downstream subprocess see the same values.
            env_name = key.upper()
            if env_name in ("MAXIMO_BASE_URL", "MAXIMO_USER", "MAXIMO_PASS"):
                os.environ[env_name] = value


def get_for_display() -> dict[str, Any]:
    """Return the values the dashboard form should pre-fill. Never
    returns the actual password — only a boolean indicating whether
    one is currently set."""
    return {
        "maximo_base_url": CFG.maximo_base_url,
        "maximo_user": CFG.maximo_user,
        "maximo_pass_set": bool(CFG.maximo_pass),
    }


def save_from_form(
    *,
    maximo_base_url: str,
    maximo_user: str,
    maximo_pass: str,
) -> None:
    """Persist a settings update from the dashboard form.

    Writes the new values to the JSON file, updates ``os.environ``
    so the live process picks them up, and mutates ``CFG`` in place
    so subsequent calls see the new values without a restart.

    A blank ``maximo_pass`` means "keep the current password" — the
    dashboard form never echoes the existing password back, so a
    blank field on submit indicates the operator did not change it.
    """
    maximo_base_url = (maximo_base_url or "").strip()
    maximo_user = (maximo_user or "").strip()

    with _LOCK:
        current = _read_file()

        # Compose the new state. Password is special — keep the
        # existing one if the form sent an empty string.
        new_state = dict(current)
        new_state["maximo_base_url"] = maximo_base_url
        new_state["maximo_user"] = maximo_user
        if maximo_pass:
            new_state["maximo_pass"] = maximo_pass

        _write_file(new_state)

        # Live update — process env + CFG attributes.
        os.environ["MAXIMO_BASE_URL"] = maximo_base_url
        os.environ["MAXIMO_USER"] = maximo_user
        CFG.maximo_base_url = maximo_base_url
        CFG.maximo_user = maximo_user
        if maximo_pass:
            os.environ["MAXIMO_PASS"] = maximo_pass
            CFG.maximo_pass = maximo_pass

    log.info("Maximo settings updated from dashboard (user=%s)", maximo_user)


__all__ = (
    "apply_to_cfg",
    "get_for_display",
    "save_from_form",
)
