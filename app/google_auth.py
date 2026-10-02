"""Google OAuth: one-time consent in a browser, then a stored refresh token keeps the dashboard signed in."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import AuthorizedSession
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials

from .config import Settings
from .store import Store, StoreError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# calendar.events writes events but can't list calendars, so both calendar scopes are needed.
CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
SCOPES = (
    "https://www.googleapis.com/auth/calendar.readonly",
    CALENDAR_WRITE_SCOPE,
    "https://www.googleapis.com/auth/gmail.readonly",
)
LOGIN_SCOPES = ("openid", "email")
STATE_TTL = 15 * 60
TOKEN_KEY = "google_token"
PURPOSES = ("login", "connect")


class AuthError(Exception):
    """Something the person doing the setup needs to fix."""


class NotConnected(Exception):
    """No usable Google authorization; the fix is to (re)connect on /setup."""


class GoogleAuth:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store
        self._lock = threading.Lock()
        self._session: AuthorizedSession | None = None
        self._cached: dict | None = None

    @property
    def configured(self) -> bool:
        return self._settings.google_configured

    @property
    def connected(self) -> bool:
        try:
            return self.configured and bool(self._refresh_token())
        except StoreError:
            return False

    def _record(self) -> dict:
        """The stored connection: refresh token plus the scopes granted with it."""
        if self._settings.google_refresh_token:
            return {"refresh_token": self._settings.google_refresh_token, "scopes": list(SCOPES)}
        if self._cached is None:
            saved = self._store.get(TOKEN_KEY) or {}
            if saved.get("refresh_token"):
                self._cached = saved
            return saved
        return self._cached

    def _refresh_token(self) -> str | None:
        return self._record().get("refresh_token")

    @property
    def can_add_events(self) -> bool:
        """Connections made before adding events was possible only hold read access."""
        try:
            return CALENDAR_WRITE_SCOPE in self._record().get("scopes", [])
        except StoreError:
            return False

    # The OAuth state is signed rather than remembered, so the callback can land on a different
    # serverless instance than the one that started the sign-in. The nonce also goes in a cookie,
    # which ties the sign-in to the browser that started it. One redirect URI serves both
    # signing in to the dashboard ("login") and connecting Calendar + Gmail ("connect").

    def _sign(self, payload: str) -> str:
        key = self._settings.google_client_secret.encode()
        return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]

    def authorization_url(self, purpose: str = "connect") -> tuple[str, str]:
        """Return the Google consent URL and the nonce to store in the browser's cookie."""
        nonce = secrets.token_urlsafe(16)
        payload = f"{purpose}.{nonce}.{int(time.time())}"
        params = {
            "client_id": self._settings.google_client_id,
            "redirect_uri": self._settings.google_redirect_uri,
            "response_type": "code",
            "state": f"{payload}.{self._sign(payload)}",
        }
        if purpose == "login":
            params.update(scope=" ".join(LOGIN_SCOPES), prompt="select_account")
        else:
            params.update(
                scope=" ".join(SCOPES), access_type="offline", prompt="consent", include_granted_scopes="true"
            )
        return f"{AUTH_URL}?{urlencode(params)}", nonce

    def check_state(self, state: str, browser_nonce: str) -> str:
        """Validate the state Google sent back; return its purpose."""
        try:
            purpose, nonce, issued, signature = state.split(".")
            issued_at = int(issued)
        except ValueError as exc:
            raise AuthError("That sign-in link is damaged. Start again.") from exc
        if purpose not in PURPOSES or not hmac.compare_digest(signature, self._sign(f"{purpose}.{nonce}.{issued}")):
            raise AuthError("That sign-in link is damaged. Start again.")
        # Before the cookie check: the cookie expires with the link, and "expired" is the useful message.
        if time.time() - issued_at > STATE_TTL:
            raise AuthError("This sign-in link has expired. Start again.")
        if not browser_nonce or not hmac.compare_digest(nonce, browser_nonce):
            raise AuthError("Finish signing in in the same browser you started in. Start again.")
        return purpose

    def _exchange(self, code: str) -> dict:
        try:
            resp = requests.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self._settings.google_client_id,
                    "client_secret": self._settings.google_client_secret,
                    "redirect_uri": self._settings.google_redirect_uri,
                    "grant_type": "authorization_code",
                },
                timeout=15,
            )
        except requests.RequestException as exc:
            raise AuthError(f"Could not reach Google to finish connecting: {exc}") from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if not resp.ok:
            detail = payload.get("error_description") or payload.get("error") or resp.text[:200]
            raise AuthError(f"Google rejected the sign-in: {detail}")
        return payload

    def login_email(self, code: str) -> str:
        """Finish a dashboard sign-in and return the verified Google account email."""
        payload = self._exchange(code)
        try:
            claims = id_token.verify_oauth2_token(
                payload.get("id_token", ""), GoogleRequest(), audience=self._settings.google_client_id
            )
        except ValueError as exc:
            raise AuthError(f"Google's sign-in response didn't verify: {exc}") from exc
        if not claims.get("email") or not claims.get("email_verified"):
            raise AuthError("That Google account has no verified email address.")
        return str(claims["email"]).lower()

    def complete(self, code: str) -> None:
        """Finish connecting Calendar + Gmail and store the refresh token."""
        payload = self._exchange(code)
        granted = set(payload.get("scope", "").split())
        missing = set(SCOPES) - granted
        if missing:
            raise AuthError(
                "Some permissions were not granted. Start again and tick every box: "
                + ", ".join(sorted(s.rsplit("/", 1)[-1] for s in missing))
            )
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise AuthError(
                "Google did not return a refresh token. Remove the app at "
                "https://myaccount.google.com/permissions and connect again."
            )

        record = {"refresh_token": refresh_token, "scopes": sorted(granted), "saved_at": int(time.time())}
        try:
            self._store.set(TOKEN_KEY, record)
        except StoreError as exc:
            raise AuthError(f"Connected, but the token couldn't be saved: {exc}") from exc
        with self._lock:
            self._cached = record
            self._session = None

    def session(self) -> AuthorizedSession:
        with self._lock:
            if self._session is None:
                refresh_token = self._refresh_token() if self.configured else None
                if not refresh_token:
                    raise NotConnected("Google is not connected yet. Open /setup to connect it.")
                credentials = Credentials(
                    None,
                    refresh_token=refresh_token,
                    token_uri=TOKEN_URL,
                    client_id=self._settings.google_client_id,
                    client_secret=self._settings.google_client_secret,
                    # No scopes: refreshing then keeps whatever was granted, even for older connections.
                    scopes=None,
                )
                self._session = AuthorizedSession(credentials)
            return self._session

    def forget_session(self) -> None:
        """Drop the session and cached token; the next call re-reads the store (maybe reconnected elsewhere)."""
        with self._lock:
            self._session = None
            self._cached = None


def is_revoked(exc: BaseException) -> bool:
    return isinstance(exc, RefreshError)


def parse_redirect_url(url: str) -> tuple[str, str]:
    """Pull code and state out of the address Google redirected the browser to.

    Used when the redirect URI (e.g. http://localhost:8080/...) isn't reachable from the
    browser that did the sign-in, so the person copies the address bar into /setup instead.
    """
    query = parse_qs(urlsplit(url.strip()).query)
    if "error" in query:
        raise AuthError(f"Google returned an error: {query['error'][0]}")
    code = query.get("code", [""])[0]
    state = query.get("state", [""])[0]
    if not code or not state:
        raise AuthError("That address doesn't contain a sign-in code. Copy the whole address bar.")
    return code, state
