from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from websockets.datastructures import Headers
from websockets.http11 import Request as WsRequest

from nanobot.channels.websocket.runtime import WebSocketConfig
from nanobot.webui.ws_http import GatewayHTTPHandler


def _handler(config: WebSocketConfig) -> GatewayHTTPHandler:
    handler = object.__new__(GatewayHTTPHandler)
    handler.config = config
    return handler


def _request(**headers: str) -> WsRequest:
    return cast(WsRequest, SimpleNamespace(headers=Headers(headers)))


def test_mcp_oauth_callback_uses_configured_public_websocket_origin() -> None:
    handler = _handler(WebSocketConfig(path="/ws", public_ws_url="wss://agent.example/ws"))

    redirect_uri = handler._mcp_oauth_redirect_uri(_request(Host="ignored.example"))

    assert redirect_uri == "https://agent.example/auth/mcp/callback"


def test_mcp_oauth_callback_uses_safe_forwarded_request_origin() -> None:
    handler = _handler(WebSocketConfig(path="/ws", host="127.0.0.1", port=8765))

    redirect_uri = handler._mcp_oauth_redirect_uri(
        _request(Host="nanobot.example:9443", **{"X-Forwarded-Proto": "https"})
    )

    assert redirect_uri == "https://nanobot.example:9443/auth/mcp/callback"


# ---------------------------------------------------------------------------
# YouTube connector mutation allowlist
# ---------------------------------------------------------------------------

# The Settings card posts exactly these actions. They were missing from the
# allowlist while the settings router already served the target paths, so
# ``_webui_mutation_path`` fell through to 404 "unknown WebUI mutation action"
# and the Connect button could never reach the handler.
def test_youtube_connector_actions_resolve_to_settings_paths() -> None:
    assert (
        GatewayHTTPHandler._webui_mutation_path("settings.youtube.connect", {})
        == "/api/settings/youtube/start"
    )
    assert (
        GatewayHTTPHandler._webui_mutation_path("settings.youtube.disconnect", {})
        == "/api/settings/youtube/disconnect"
    )


def test_youtube_connector_paths_are_recognised_as_mutations() -> None:
    handler = object.__new__(GatewayHTTPHandler)
    from nanobot.webui.settings_routes import WebUISettingsRouter

    handler.settings_routes = WebUISettingsRouter.__new__(WebUISettingsRouter)
    for path in ("/api/settings/youtube/start", "/api/settings/youtube/disconnect"):
        # Both must be treated as mutations so the plain-HTTP path is rejected
        # with 405 instead of silently 404-ing.
        assert WebUISettingsRouter.is_mutation_path(path) is True
