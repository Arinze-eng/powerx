"""Google OAuth (YouTube Data API v3) flow, run server-side by the gateway.

Mints authorization URLs, validates the ``state`` on the browser callback,
exchanges the code for tokens, refreshes expired access tokens, and stores the
result per user via :class:`YouTubeCredentialStore`.

The redirect URI registered on the Google client is exactly
``https://www.minis.publicvm.com`` (no path, no trailing slash), so the browser
callback arrives at the app root as ``/?code=...&state=...``. The gateway root
handler intercepts it when a pending flow matches the ``state``.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from nanobot.youtube.api import fetch_my_channel
from nanobot.youtube.credentials import YouTubeCredentialError, YouTubeCredentialStore

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

DEFAULT_CLIENT_ID = (
    "788178697454-942r7b5kon85rokjmjraupnjhe5qa04q.apps.googleusercontent.com"
)
DEFAULT_REDIRECT_URI = "https://www.minis.publicvm.com"

# Read the account, plus write actions (like, comment, subscribe, playlist edits).
YOUTUBE_SCOPES = (
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
)

_FLOW_TTL_S = 600


class YouTubeOAuthError(Exception):
    """Safe, user-facing error for a YouTube OAuth request."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class _YouTubeFlow:
    state: str
    user_id: str
    redirect_uri: str
    expires_at: float
    created_at: float


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


def oauth_client_id() -> str:
    return _env("YOUTUBE_OAUTH_CLIENT_ID", DEFAULT_CLIENT_ID)


def oauth_client_secret() -> str:
    return _env("YOUTUBE_OAUTH_CLIENT_SECRET")


def oauth_redirect_uri() -> str:
    """Exact redirect URI registered on the Google client."""
    return _env("YOUTUBE_OAUTH_REDIRECT_URI", DEFAULT_REDIRECT_URI).rstrip("/") or DEFAULT_REDIRECT_URI


def build_authorization_url(*, state: str, redirect_uri: str | None = None) -> str:
    """Construct the Google authorization URL for the fixed redirect URI."""
    params = {
        "client_id": oauth_client_id(),
        "redirect_uri": redirect_uri or oauth_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(YOUTUBE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"


def _expiry_iso(expires_in: Any) -> str | None:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


async def exchange_code_for_tokens(code: str, *, redirect_uri: str) -> dict[str, Any]:
    """Exchange an authorization code for tokens at Google's token endpoint."""
    secret = oauth_client_secret()
    if not secret:
        raise YouTubeOAuthError(
            "YouTube OAuth is not configured (missing client secret).", status=500
        )
    body = {
        "code": code,
        "client_id": oauth_client_id(),
        "client_secret": secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(GOOGLE_TOKEN_URL, data=body)
    if response.status_code >= 400:
        raise YouTubeOAuthError(
            "Google rejected the authorization code. Try connecting again.", status=400
        )
    try:
        data = response.json()
    except Exception as exc:  # pragma: no cover - defensive
        raise YouTubeOAuthError("Google returned an unreadable token response.") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise YouTubeOAuthError("Google did not return an access token.", status=400)
    return data


async def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Refresh an expired access token."""
    secret = oauth_client_secret()
    if not secret:
        raise YouTubeOAuthError(
            "YouTube OAuth is not configured (missing client secret).", status=500
        )
    body = {
        "client_id": oauth_client_id(),
        "client_secret": secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(GOOGLE_TOKEN_URL, data=body)
    if response.status_code >= 400:
        raise YouTubeOAuthError(
            "Your YouTube connection has expired. Reconnect YouTube in Settings.",
            status=401,
        )
    try:
        data = response.json()
    except Exception as exc:  # pragma: no cover - defensive
        raise YouTubeOAuthError("Google returned an unreadable refresh response.") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise YouTubeOAuthError(
            "Your YouTube connection has expired. Reconnect YouTube in Settings.",
            status=401,
        )
    return data


class YouTubeOAuthManager:
    """Own short-lived browser flows while the gateway process is running."""

    def __init__(self) -> None:
        self._flows: dict[str, _YouTubeFlow] = {}
        self._store = YouTubeCredentialStore()

    # -- flow lifecycle -----------------------------------------------------

    def start(self, user_id: str, *, redirect_uri: str | None = None) -> dict[str, Any]:
        if not user_id:
            raise YouTubeOAuthError("Sign in to connect YouTube.", status=401)
        self._prune()
        target = (redirect_uri or oauth_redirect_uri()).rstrip("/") or DEFAULT_REDIRECT_URI
        state = secrets.token_urlsafe(32)
        now = time.monotonic()
        self._flows[state] = _YouTubeFlow(
            state=state,
            user_id=user_id,
            redirect_uri=target,
            expires_at=now + _FLOW_TTL_S,
            created_at=now,
        )
        return {
            "flow_state": state,
            "authorization_url": build_authorization_url(state=state, redirect_uri=target),
            "expires_in": _FLOW_TTL_S,
        }

    def has_pending_state(self, state: str) -> bool:
        self._prune()
        return bool(state) and state in self._flows

    async def submit_callback(
        self, *, state: str, code: str | None, error: str | None
    ) -> dict[str, Any]:
        """Complete a flow from the browser callback. Validates and consumes state."""
        self._prune()
        flow = self._flows.pop(state, None) if state else None
        if flow is None:
            raise YouTubeOAuthError(
                "This YouTube authorization request is unknown or has expired. Start again.",
                status=410,
            )
        if error:
            safe = error if error.isalnum() or error.replace("_", "").isalnum() else "failed"
            raise YouTubeOAuthError(f"YouTube authorization was not completed ({safe}).")
        if not code or len(code) > 8192:
            raise YouTubeOAuthError("Google did not return an authorization code.")

        tokens = await exchange_code_for_tokens(code, redirect_uri=flow.redirect_uri)
        access_token = tokens["access_token"]
        refresh_token = tokens.get("refresh_token") or ""
        scope = tokens.get("scope", " ".join(YOUTUBE_SCOPES))
        expiry = _expiry_iso(tokens.get("expires_in"))

        channel_id = None
        channel_title = None
        try:
            channel = await fetch_my_channel(access_token)
            if channel:
                channel_id = channel.get("id")
                channel_title = (channel.get("snippet") or {}).get("title")
        except Exception:
            # A brand-new Google account may have no channel; not fatal.
            channel_id = None
            channel_title = None

        try:
            await self._store.store_credentials(
                user_id=flow.user_id,
                access_token=access_token,
                refresh_token=refresh_token,
                token_expiry=expiry,
                scope=scope,
                channel_id=channel_id,
                channel_title=channel_title,
            )
        except YouTubeCredentialError as exc:
            raise YouTubeOAuthError(str(exc), status=500) from exc

        return {
            "status": "connected",
            "channel_id": channel_id,
            "channel_title": channel_title,
        }

    async def status(self, user_id: str) -> dict[str, Any]:
        if not user_id:
            return {"connected": False, "configured": bool(oauth_client_secret())}
        configured = bool(oauth_client_secret())
        if not self._store.enabled:
            return {"connected": False, "configured": configured}
        try:
            creds = await self._store.get_credentials(user_id)
        except YouTubeCredentialError:
            return {"connected": False, "configured": configured}
        if not creds:
            return {"connected": False, "configured": configured}
        return {
            "connected": True,
            "configured": configured,
            "channel_id": creds.get("channel_id"),
            "channel_title": creds.get("channel_title"),
            "scope": creds.get("scope", ""),
        }

    async def disconnect(self, user_id: str) -> dict[str, Any]:
        if not user_id:
            raise YouTubeOAuthError("Sign in to disconnect YouTube.", status=401)
        if self._store.enabled:
            try:
                await self._store.delete_credentials(user_id)
            except YouTubeCredentialError as exc:
                raise YouTubeOAuthError(str(exc), status=500) from exc
        return {"connected": False}

    # -- token access for the agent tool ------------------------------------

    async def access_token_for_user(self, user_id: str) -> str | None:
        """Return a live access token for one user, refreshing if needed.

        Returns None when the user is not connected or storage is unavailable.
        Never falls back to another user's credentials.
        """
        if not user_id or not self._store.enabled:
            return None
        try:
            creds = await self._store.get_credentials(user_id)
        except YouTubeCredentialError:
            return None
        if not creds:
            return None
        expiry = _parse_expiry(creds.get("token_expiry"))
        if expiry is not None and expiry <= datetime.now(timezone.utc) + timedelta(seconds=60):
            refresh_token = creds.get("refresh_token")
            if not refresh_token:
                return None
            tokens = await refresh_access_token(refresh_token)
            new_access = tokens["access_token"]
            await self._store.update_tokens(
                user_id=user_id,
                access_token=new_access,
                token_expiry=_expiry_iso(tokens.get("expires_in")),
                refresh_token=tokens.get("refresh_token"),
                scope=tokens.get("scope"),
            )
            return new_access
        return creds.get("access_token")

    # -- internals ----------------------------------------------------------

    def _prune(self) -> None:
        now = time.monotonic()
        for state, flow in list(self._flows.items()):
            if flow.expires_at <= now:
                self._flows.pop(state, None)


def _parse_expiry(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


_manager: YouTubeOAuthManager | None = None


def get_youtube_oauth_manager() -> YouTubeOAuthManager:
    """Process-wide manager shared by settings routes and the root callback."""
    global _manager
    if _manager is None:
        _manager = YouTubeOAuthManager()
    return _manager


def reset_youtube_oauth_manager() -> None:
    """Drop the shared manager (used by tests)."""
    global _manager
    _manager = None
