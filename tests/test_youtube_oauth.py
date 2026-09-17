"""Tests for the YouTube OAuth manager and token storage.

Covers the exact redirect URI and scopes sent to Google, callback state
validation, the token exchange + storage path (Google endpoint mocked), the
refresh path, status and disconnect.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from nanobot.youtube import oauth as yt_oauth


def _parse_auth_url(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    query = parse_qs(parsed.query)
    return {key: values[0] for key, values in query.items()}


def test_authorization_url_uses_exact_redirect_uri_and_scopes() -> None:
    url = yt_oauth.build_authorization_url(state="state-abc")
    params = _parse_auth_url(url)
    assert params["redirect_uri"] == "https://www.minis.publicvm.com"
    assert params["client_id"] == yt_oauth.DEFAULT_CLIENT_ID
    assert params["response_type"] == "code"
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    assert params["state"] == "state-abc"
    scopes = set(params["scope"].split(" "))
    assert scopes == {
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/youtube.force-ssl",
    }


def test_redirect_uri_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_OAUTH_REDIRECT_URI", "https://example.test/")
    url = yt_oauth.build_authorization_url(state="s")
    assert _parse_auth_url(url)["redirect_uri"] == "https://example.test"


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = b"x" if payload is not None else b""
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload or {}


class FakeTokenClient:
    """Stands in for httpx.AsyncClient at Google's token endpoint."""

    calls: list[dict[str, Any]] = []
    response: FakeResponse = FakeResponse(200, {"access_token": "access-1", "expires_in": 3600})

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "FakeTokenClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, data: dict[str, Any] | None = None) -> FakeResponse:
        FakeTokenClient.calls.append({"url": url, "data": data or {}})
        return FakeTokenClient.response


class FakeStore:
    def __init__(self) -> None:
        self.stored: dict[str, Any] | None = None
        self.deleted: list[str] = []
        self.updated: dict[str, Any] | None = None
        self.creds: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return True

    async def store_credentials(self, **kwargs: Any) -> None:
        self.stored = kwargs

    async def get_credentials(self, user_id: str) -> dict[str, Any] | None:
        return self.creds

    async def update_tokens(self, **kwargs: Any) -> None:
        self.updated = kwargs

    async def delete_credentials(self, user_id: str) -> bool:
        self.deleted.append(user_id)
        return True


def _manager_with_fakes() -> tuple[yt_oauth.YouTubeOAuthManager, FakeStore]:
    manager = yt_oauth.YouTubeOAuthManager()
    store = FakeStore()
    manager._store = store  # type: ignore[assignment]
    return manager, store


def test_submit_callback_rejects_unknown_state() -> None:
    manager, _ = _manager_with_fakes()
    with pytest.raises(yt_oauth.YouTubeOAuthError) as excinfo:
        import asyncio

        asyncio.run(manager.submit_callback(state="nope", code="c", error=None))
    assert excinfo.value.status == 410


def test_start_creates_pending_state() -> None:
    manager, _ = _manager_with_fakes()
    payload = manager.start("user-1")
    assert payload["authorization_url"].startswith(yt_oauth.GOOGLE_AUTHORIZE_URL)
    assert manager.has_pending_state(payload["flow_state"]) is True


def test_submit_callback_exchanges_and_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET", "test-secret")
    FakeTokenClient.calls = []
    FakeTokenClient.response = FakeResponse(
        200,
        {
            "access_token": "access-xyz",
            "refresh_token": "refresh-xyz",
            "expires_in": 3600,
            "scope": " ".join(yt_oauth.YOUTUBE_SCOPES),
        },
    )
    monkeypatch.setattr(yt_oauth.httpx, "AsyncClient", FakeTokenClient)

    async def fake_my_channel(access_token: str) -> dict[str, Any]:
        assert access_token == "access-xyz"
        return {"id": "UCabc", "snippet": {"title": "My Channel"}}

    monkeypatch.setattr(yt_oauth, "fetch_my_channel", fake_my_channel)

    manager, store = _manager_with_fakes()
    state = manager.start("user-1")["flow_state"]
    result = asyncio.run(manager.submit_callback(state=state, code="CODE", error=None))

    assert result["status"] == "connected"
    assert result["channel_id"] == "UCabc"
    assert store.stored is not None
    assert store.stored["user_id"] == "user-1"
    assert store.stored["access_token"] == "access-xyz"
    assert store.stored["refresh_token"] == "refresh-xyz"
    # The exchange used the exact registered redirect URI.
    assert FakeTokenClient.calls[0]["url"] == yt_oauth.GOOGLE_TOKEN_URL
    assert FakeTokenClient.calls[0]["data"]["redirect_uri"] == "https://www.minis.publicvm.com"
    # State is consumed (single use).
    assert manager.has_pending_state(state) is False


def test_submit_callback_rejects_replayed_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET", "test-secret")
    FakeTokenClient.calls = []
    FakeTokenClient.response = FakeResponse(
        200, {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
    )
    monkeypatch.setattr(yt_oauth.httpx, "AsyncClient", FakeTokenClient)

    async def fake_my_channel(access_token: str) -> dict[str, Any] | None:
        return None

    monkeypatch.setattr(yt_oauth, "fetch_my_channel", fake_my_channel)

    manager, _ = _manager_with_fakes()
    state = manager.start("user-1")["flow_state"]
    asyncio.run(manager.submit_callback(state=state, code="CODE", error=None))
    with pytest.raises(yt_oauth.YouTubeOAuthError):
        asyncio.run(manager.submit_callback(state=state, code="CODE", error=None))


def test_access_token_refreshes_when_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    manager, store = _manager_with_fakes()
    store.creds = {
        "access_token": "old",
        "refresh_token": "refresh-1",
        "token_expiry": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        "scope": "",
        "channel_id": None,
        "channel_title": None,
    }

    async def fake_refresh(refresh_token: str) -> dict[str, Any]:
        assert refresh_token == "refresh-1"
        return {"access_token": "new-access", "expires_in": 3600}

    monkeypatch.setattr(yt_oauth, "refresh_access_token", fake_refresh)

    token = asyncio.run(manager.access_token_for_user("user-1"))
    assert token == "new-access"
    assert store.updated is not None
    assert store.updated["access_token"] == "new-access"


def test_access_token_none_when_not_connected() -> None:
    import asyncio

    manager, store = _manager_with_fakes()
    store.creds = None
    assert asyncio.run(manager.access_token_for_user("user-1")) is None


def test_status_and_disconnect() -> None:
    import asyncio

    manager, store = _manager_with_fakes()
    store.creds = {
        "access_token": "a",
        "refresh_token": "r",
        "token_expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "scope": "s",
        "channel_id": "UC1",
        "channel_title": "Chan",
    }
    status = asyncio.run(manager.status("user-1"))
    assert status["connected"] is True
    assert status["channel_id"] == "UC1"

    result = asyncio.run(manager.disconnect("user-1"))
    assert result["connected"] is False
    assert store.deleted == ["user-1"]
