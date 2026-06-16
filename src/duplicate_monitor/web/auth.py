"""Session-based authentication for the dashboard.

Every dashboard user logs in with his **own Maximo credentials**. The
login form posts the username + password to ``/login``; the server
probes Maximo with those credentials to verify them, and if Maximo
accepts the user, a server-side session is created and the browser
gets back a signed cookie with a random session token.

Decisions made by the reviewer (mark a duplicate group as confirmed,
mark it as a false positive, etc.) are tagged with the username from
the active session, so the audit trail records *who* classified each
group.

The Maximo passwords themselves are never persisted by this service.
They are used live to validate against Maximo and are dropped
immediately afterwards; only the username and an opaque session token
ever touch the SQLite store.
"""

from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Request

from duplicate_monitor.core.config import CFG
from duplicate_monitor.sources.maximo import validate_credentials
from duplicate_monitor.storage import db

SESSION_COOKIE = "kidana_session"
SESSION_TTL_HOURS = 12


def authenticate(username: str, password: str) -> bool:
    """Return ``True`` if Maximo accepts these credentials.

    Delegates to ``maximo.validate_credentials`` which runs through the
    six-strategy fallback that the rest of the integration uses, so a
    user that can sign in to Maximo Web can also sign in here.
    """
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return False
    return validate_credentials(CFG.maximo_base_url, username, password)


def create_session(username: str) -> str:
    """Create a server-side session and return the random token that
    the caller should set as a cookie on the response."""
    token = secrets.token_urlsafe(32)
    db.create_session(token, username, ttl_hours=SESSION_TTL_HOURS)
    return token


def get_current_user(request: Request) -> Optional[str]:
    """Return the Maximo username for the active session, or ``None``
    if the request is unauthenticated or the session has expired.

    Reading a request also refreshes the session's ``last_seen_at`` so
    an actively used cookie does not expire while the reviewer is in
    the middle of a triage round.
    """
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    row = db.lookup_session(token)
    if not row:
        return None
    return row.get("username")


def end_session(request: Request) -> None:
    """Invalidate the active session (logout)."""
    token = request.cookies.get(SESSION_COOKIE, "")
    db.delete_session(token)


__all__ = (
    "SESSION_COOKIE",
    "authenticate",
    "create_session",
    "get_current_user",
    "end_session",
)
