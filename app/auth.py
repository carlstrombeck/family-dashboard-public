"""Who may use the dashboard: Google sign-in for listed family members, kept in a signed cookie."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time

from .config import Settings
from .store import Store, StoreError

log = logging.getLogger(__name__)

SESSION_COOKIE = "fd_session"
SESSION_DAYS = 400  # the longest browsers keep a cookie; the kitchen iPad shouldn't have to sign in again
REFRESH_AFTER = 7 * 24 * 3600
MEMBERS_TTL = 60  # removing someone takes effect within a minute
ROLES = ("admin", "member")


def session_key(settings: Settings) -> bytes:
    if settings.session_secret:
        return settings.session_secret.encode()
    # Without SESSION_SECRET, derive a key from the Google client secret (rotating it signs everyone out).
    return hmac.new(settings.google_client_secret.encode(), b"family-dashboard-session", hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_session(key: bytes, email: str, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    payload = _b64(json.dumps({"e": email, "i": issued}, separators=(",", ":")).encode())
    signature = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def read_session(key: bytes, cookie: str, now: float | None = None) -> tuple[str, int] | None:
    """Return (email, issued_at) for a valid, unexpired cookie."""
    try:
        payload, signature = cookie.split(".")
        expected = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(_unb64(payload))
        email, issued = str(data["e"]), int(data["i"])
    except (ValueError, KeyError, TypeError):
        return None
    if (now if now is not None else time.time()) - issued > SESSION_DAYS * 86400:
        return None
    return email, issued


class Members:
    """DASHBOARD_ADMINS (always admins, so nobody gets locked out) plus the dashboard_members table."""

    def __init__(self, settings: Settings, store: Store) -> None:
        self._admins = {e.lower() for e in settings.admins}
        self._store = store
        self._lock = threading.Lock()
        self._cached: dict[str, str] | None = None
        self._cached_at = 0.0

    def _table(self) -> dict[str, str]:
        with self._lock:
            if self._cached is None or time.monotonic() - self._cached_at > MEMBERS_TTL:
                try:
                    self._cached = self._store.members()
                    self._cached_at = time.monotonic()
                except StoreError:
                    log.warning("Could not load dashboard members", exc_info=True)
                    if self._cached is None:
                        return {}  # fail closed: only DASHBOARD_ADMINS get in
            return self._cached

    def role(self, email: str) -> str | None:
        email = email.strip().lower()
        if email in self._admins:
            return "admin"
        role = self._table().get(email)
        return role if role in ROLES else None

    def listing(self) -> list[tuple[str, str, str]]:
        """(email, role, source) for /setup."""
        rows = [(e, "admin", "DASHBOARD_ADMINS") for e in sorted(self._admins)]
        rows += [(e, r, "dashboard_members") for e, r in sorted(self._table().items()) if e not in self._admins]
        return rows

    @property
    def possible(self) -> bool:
        """Whether anyone could ever sign in (otherwise Google sign-in is pointless)."""
        return bool(self._admins) or getattr(self._store, "has_members_table", False)
