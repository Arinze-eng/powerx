"""Tests for the liveness check behind the YouTube connector's connected flag.

``status()`` used to trust the stored row alone, so a revoked or expired grant
still read as ``connected: True`` with a channel title. The WebUI shows only
*Disconnect* in that state, leaving no way back to Connect while every tool call
failed with "Your YouTube connection has expired". These tests pin the corrected
behaviour: a definite rejection reads as disconnected, a transient failure does
not, and a fresh grant clears a stale verdict.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from nanobot.youtube import oauth as yt_oauth


class FakeStore:
    def __init__(self, creds: dict[str, Any] | None = None) -> None:
        self.creds = creds
        self.stored: dict[str, Any] | None = None
        self.deleted: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    async def get_credentials(self, user_id: str) -> dict[str, Any] | None:
        return self.creds

    async def store_credentials(self, **kwargs: Any) -> None:
        self.stored = kwargs

    async def update_tokens(self, **kwargs: Any) -> None:
        pass

    async def delete_credentials(self, user_id: str) -> bool:
        self.deleted.append(user_id)
        return True


def _expired_creds() -> dict[str, Any]:
    return {
        "access_token": "a",
        "refresh_token": "r",
        "token_expiry": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "scope": "s",
        "channel_id": "UC1",
        "channel_title": "Groupie Tech",
    }


def _manager(creds: dict[str, Any] | None) -> yt_oauth.YouTubeOAuthManager:
    manager = yt_oauth.YouTubeOAuthManager()
    manager._store = FakeStore(creds)  # type: ignore[assignment]
    return manager


@pytest.fixture(autouse=True)
def _client_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET", "test-secret")


def test_status_reports_disconnected_when_refresh_is_rejected() -> None:
    """A rejected grant must not read as connected, or Connect becomes unreachable."""
    manager = _manager(_expired_creds())

    async def rejected(user_id: str) -> str:
        raise yt_oauth.YouTubeOAuthError(
            "Your YouTube connection has expired. Reconnect YouTube in Settings.",
            status=401,
        )

    manager.access_token_for_user = rejected  # type: ignore[assignment]
    status = asyncio.run(manager.status("user-1"))

    assert status["connected"] is False
    assert status["needs_reconnect"] is True
    assert "channel_title" not in status


def test_status_reports_disconnected_when_no_token_can_be_minted() -> None:
    manager = _manager(_expired_creds())

    async def nothing(user_id: str) -> None:
        return None

    manager.access_token_for_user = nothing  # type: ignore[assignment]
    status = asyncio.run(manager.status("user-1"))

    assert status["connected"] is False
    assert status["needs_reconnect"] is True


def test_status_stays_connected_on_a_transient_failure() -> None:
    """A network blip must never be reported to the user as a disconnect."""
    manager = _manager(_expired_creds())

    async def flaky(user_id: str) -> str:
        raise RuntimeError("network down")

    manager.access_token_for_user = flaky  # type: ignore[assignment]
    status = asyncio.run(manager.status("user-1"))

    assert status["connected"] is True
    assert status["channel_title"] == "Groupie Tech"


def test_status_skips_the_liveness_probe_for_a_live_access_token() -> None:
    """A token that has not expired needs no call to Google."""
    creds = _expired_creds()
    creds["token_expiry"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    manager = _manager(creds)
    calls: list[str] = []

    async def counted(user_id: str) -> str:
        calls.append(user_id)
        return "a"

    manager.access_token_for_user = counted  # type: ignore[assignment]
    status = asyncio.run(manager.status("user-1"))

    assert status["connected"] is True
    assert calls == []


def test_dead_grant_verdict_is_cached() -> None:
    """Polling the WebUI must not hit Google's token endpoint on every request."""
    manager = _manager(_expired_creds())
    calls: list[str] = []

    async def rejected(user_id: str) -> str:
        calls.append(user_id)
        raise yt_oauth.YouTubeOAuthError("expired", status=401)

    manager.access_token_for_user = rejected  # type: ignore[assignment]

    first = asyncio.run(manager.status("user-1"))
    second = asyncio.run(manager.status("user-1"))

    assert first["connected"] is False
    assert second["connected"] is False
    assert calls == ["user-1"], "the rejected grant should be probed once"


def test_disconnect_clears_the_dead_grant_verdict() -> None:
    manager = _manager(_expired_creds())

    async def rejected(user_id: str) -> str:
        raise yt_oauth.YouTubeOAuthError("expired", status=401)

    manager.access_token_for_user = rejected  # type: ignore[assignment]
    asyncio.run(manager.status("user-1"))
    assert "user-1" in manager._dead_grants

    asyncio.run(manager.disconnect("user-1"))
    assert "user-1" not in manager._dead_grants


def test_fresh_connect_clears_the_dead_grant_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnecting must take effect immediately, not after the cache expires."""
    manager = _manager(_expired_creds())

    async def rejected(user_id: str) -> str:
        raise yt_oauth.YouTubeOAuthError("expired", status=401)

    manager.access_token_for_user = rejected  # type: ignore[assignment]
    asyncio.run(manager.status("user-1"))
    assert "user-1" in manager._dead_grants

    async def fake_exchange(code: str, *, redirect_uri: str) -> dict[str, Any]:
        return {"access_token": "new", "refresh_token": "new-r", "expires_in": 3600}

    async def fake_channel(access_token: str) -> dict[str, Any]:
        return {"id": "UC9", "snippet": {"title": "Fresh"}}

    monkeypatch.setattr(yt_oauth, "exchange_code_for_tokens", fake_exchange)
    monkeypatch.setattr(yt_oauth, "fetch_my_channel", fake_channel)

    flow = manager.start("user-1")
    result = asyncio.run(
        manager.submit_callback(state=flow["flow_state"], code="c", error=None)
    )

    assert result["status"] == "connected"
    assert "user-1" not in manager._dead_grants
