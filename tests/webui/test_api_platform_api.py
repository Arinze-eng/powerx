"""The OpenAI-compatible API platform as the WebUI settings section sees it.

The Telegram bot has had /apikey, /listapikeys, /revokeapikey and /apidoc for a
while. These tests cover the web route added on top of the same store, so a key
minted in either front end is the same key.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from types import SimpleNamespace
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.datastructures import Headers

from nanobot.config.loader import get_config_path
from nanobot.webui import api_platform_api
from nanobot.webui.api_platform_api import (
    PlatformError,
    _redact,
    platform_action,
    platform_payload,
)
from nanobot.webui.http_utils import http_json_response
from nanobot.webui.settings_routes import WebUISettingsRouter, _request_origin
from nanobot.webui.settings_services import WebUISettingsServices
from nanobot.webui.ws_http import GatewayHTTPHandler

USER = "user-1"
PLAIN_KEY = "px_" + "a" * 40

T = TypeVar("T")


def _run(coro: Awaitable[T]) -> T:
    """Drive one coroutine from a synchronous test body.

    pytest-asyncio runs in strict mode here, so a test without the marker gets
    no loop of its own and the route handlers still need one.
    """
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "k1",
        "name": "laptop",
        "key_prefix": "px_abcdefgh",
        "is_active": True,
        "total_requests": 3,
        "last_used_at": None,
        "created_at": "2026-09-01T00:00:00Z",
        # Present in the table, and must never reach a browser.
        "key_hash": "0" * 64,
        "agentx_user_id": USER,
        "telegram_user_id": 42,
    }
    row.update(overrides)
    return row


class FakeStore:
    """Stands in for ApiKeyStore so the tests never touch Supabase.

    One instance per test, because the real store is a thin client over shared
    rows: the create/revoke routes and the payload read have to see each other's
    writes, which a fresh object per call would hide.
    """

    def __init__(self, *, rows: list[dict[str, Any]] | None = None, enabled: bool = True) -> None:
        self.enabled = enabled
        self.rows: list[dict[str, Any]] = [_row()] if rows is None else rows
        self.created: list[str] = []

    async def list_keys(self, agentx_user_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows]

    async def create_key(
        self,
        *,
        agentx_user_id: str,
        telegram_user_id: int | None,
        name: str,
    ) -> tuple[str, dict[str, Any]]:
        row = _row(id=f"k{len(self.created) + 2}", name=name, key_prefix=PLAIN_KEY[:11])
        self.created.append(PLAIN_KEY)
        self.rows.insert(0, row)
        return PLAIN_KEY, row

    async def revoke_all(self, agentx_user_id: str) -> int:
        count = len([row for row in self.rows if row["is_active"]])
        for row in self.rows:
            row["is_active"] = False
        return count


def _router(*, user_id: str = USER) -> WebUISettingsRouter:
    return WebUISettingsRouter(
        settings=WebUISettingsServices.create(get_config_path()),
        bus=SimpleNamespace(),
        logger=SimpleNamespace(exception=lambda *_args: None, warning=lambda *_a: None),
        check_api_token=lambda _request: True,
        parse_query=lambda path: parse_qs(urlsplit(path).query),
        json_response=http_json_response,
        error_response=lambda status, message: http_json_response(
            {"error": message},
            status=status,
        ),
        runtime_surface="browser",
        runtime_capabilities={},
        youtube_user_id=lambda _request: user_id,
    )


def _mutation_request(payload: dict[str, Any]) -> SimpleNamespace:
    request = SimpleNamespace(path="/api/settings/api-platform/create", headers=Headers())
    request._nanobot_webui_mutation_request = True
    request._nanobot_webui_mutation_payload = payload
    request._nanobot_trusted_proxy_authenticated = True
    return request


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """A live fake store the module's handlers will find on every construction."""
    fake = FakeStore()
    monkeypatch.setattr(api_platform_api, "ApiKeyStore", lambda: fake)
    # Never read the operator's real address out of the environment or config.
    monkeypatch.delenv("NANOBOT_API_PUBLIC_URL", raising=False)
    monkeypatch.delenv("API_SERVER_URL", raising=False)
    return fake


def test_redact_keeps_nothing_replayable() -> None:
    redacted = _redact(_row())

    assert set(redacted) == {
        "id",
        "name",
        "prefix",
        "active",
        "requests",
        "last_used_at",
        "created_at",
    }
    assert redacted["prefix"] == "px_abcdefgh"
    assert redacted["requests"] == 3
    serialized = json.dumps(redacted)
    assert "0" * 64 not in serialized
    assert "key_hash" not in serialized


def test_payload_says_nothing_is_configured_when_the_store_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_platform_api, "ApiKeyStore", lambda: FakeStore(enabled=False))

    payload = _run(platform_payload(USER, origin="https://app.example.com"))

    assert payload["enabled"] is False
    assert payload["keys"] == []
    assert payload["notice"] == "The API platform is not configured on this server yet."


def test_payload_asks_an_anonymous_visitor_to_sign_in(store: FakeStore) -> None:
    payload = _run(platform_payload("", origin="https://app.example.com"))

    assert payload["signed_in"] is False
    assert payload["keys"] == []
    assert payload["notice"] == "Sign in to generate and manage API keys."


def test_payload_reports_base_url_endpoint_models_and_redacted_keys(store: FakeStore) -> None:
    payload = _run(platform_payload(USER, origin="https://app.example.com"))

    assert payload["enabled"] is True
    assert payload["configured"] is True
    assert payload["base_url"] == "https://app.example.com"
    assert payload["endpoint"] == "https://app.example.com/v1"
    assert payload["docs"]["chat_completions"] == (
        "https://app.example.com/v1/chat/completions"
    )
    assert [model["id"] for model in payload["models"]] == ["powerx-agent"]
    assert payload["models"][0]["owned_by"] == "powerx"
    assert payload["max_keys"] == 10
    assert [key["name"] for key in payload["keys"]] == ["laptop"]
    assert "key_hash" not in json.dumps(payload)


def test_base_url_prefers_an_explicit_address_then_the_request_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NANOBOT_API_PUBLIC_URL", "https://api.powerx.example")
    assert api_platform_api.platform_base_url("https://app.example.com") == (
        "https://api.powerx.example"
    )

    monkeypatch.delenv("NANOBOT_API_PUBLIC_URL")
    assert api_platform_api.platform_base_url("https://app.example.com/") == (
        "https://app.example.com"
    )


def test_request_origin_uses_the_forwarded_host_and_ignores_localhost() -> None:
    proxied = SimpleNamespace(
        headers=Headers(
            {
                "X-Forwarded-Host": "app.powerx.example",
                "X-Forwarded-Proto": "https",
            }
        )
    )
    assert _request_origin(proxied) == "https://app.powerx.example"

    local = SimpleNamespace(
        headers=Headers({"Host": "localhost:18790", "X-Forwarded-Proto": "http"})
    )
    assert _request_origin(local) == ""

    assert _request_origin(SimpleNamespace(headers=Headers())) == ""


def test_settings_route_serves_the_payload_to_the_signed_in_user(store: FakeStore) -> None:
    response = _run(
        _router().dispatch(
            None,
            SimpleNamespace(
                path="/api/settings/api-platform",
                headers=Headers({"X-Forwarded-Host": "app.powerx.example"}),
            ),
            "/api/settings/api-platform",
        )
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["base_url"] == "https://app.powerx.example"
    assert payload["signed_in"] is True
    assert payload["keys"][0]["prefix"] == "px_abcdefgh"


def test_creating_a_key_returns_the_plaintext_once(store: FakeStore) -> None:
    response = _run(
        _router().dispatch(
            None,
            _mutation_request({"name": "laptop"}),
            "/api/settings/api-platform/create",
        )
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["created"]["key"] == PLAIN_KEY
    assert payload["created"]["prefix"] == "px_aaaaaaaa"
    assert payload["created"]["name"] == "laptop"
    # The refreshed list still carries only the prefix, never the key itself.
    assert all("key" not in key for key in payload["keys"])
    assert PLAIN_KEY not in json.dumps(payload["keys"])


def test_creating_a_key_stops_at_the_per_account_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full = FakeStore(rows=[_row(id=f"k{index}") for index in range(10)])
    monkeypatch.setattr(api_platform_api, "ApiKeyStore", lambda: full)

    response = _run(
        _router().dispatch(
            None,
            _mutation_request({}),
            "/api/settings/api-platform/create",
        )
    )

    assert response.status_code == 400
    assert "limit 10" in json.loads(response.body)["error"]


def test_revoking_from_the_web_revokes_every_key(store: FakeStore) -> None:
    response = _run(
        _router().dispatch(
            None,
            _mutation_request({}),
            "/api/settings/api-platform/revoke",
        )
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["revoked"] == 1
    assert [key["active"] for key in payload["keys"]] == [False]


def test_actions_refuse_an_anonymous_session(store: FakeStore) -> None:
    with pytest.raises(PlatformError):
        _run(platform_action("create", agentx_user_id=""))
    with pytest.raises(PlatformError):
        _run(platform_action("revoke", agentx_user_id=""))

    response = _run(
        _router(user_id="").dispatch(
            None,
            _mutation_request({}),
            "/api/settings/api-platform/create",
        )
    )
    assert response.status_code == 400
    assert json.loads(response.body)["error"] == "Sign in to manage API keys."


def test_the_socket_mutation_allowlist_routes_both_actions() -> None:
    assert GatewayHTTPHandler._webui_mutation_path(
        "settings.api_platform.create", {}
    ) == "/api/settings/api-platform/create"
    assert GatewayHTTPHandler._webui_mutation_path(
        "settings.api_platform.revoke", {}
    ) == "/api/settings/api-platform/revoke"
