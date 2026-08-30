"""HTTP API handler extracted from WebSocketChannel.

Handles all non-WebSocket HTTP routes: bootstrap, sessions, settings,
media, commands, sidebar state, static file serving, and token management.

Also houses shared HTTP utility functions used by both this module and
``websocket.py`` to avoid circular imports.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from loguru import logger
from websockets.datastructures import Headers
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanobot.admin_registry import admin_route
from nanobot.command.builtin import builtin_command_palette
from nanobot.cron.session_turns import is_bound_cron_job
from nanobot.cron.types import CronJob, CronSchedule
from nanobot.security.workspace_access import WorkspaceScope
from nanobot.session.manager import SessionManager
from nanobot.session.session_handles import (
    SessionHandleResolver,
)
from nanobot.triggers.local_types import LocalTrigger
from nanobot.webui.file_preview import (
    WebUFilePreviewError,
    file_preview_availability_payload,
    file_preview_payload,
)
from nanobot.webui.gateway_tokens import GatewayTokenStore, token_response_payload
from nanobot.webui.http_utils import (
    acceps_gzip as _accepts_gzip,
)
from nanobot.webui.http_utils import (
    case_insensitive_header as _case_insensitive_header,,
)
from nanobot.webui.http_utils import (
    combined_list_header as _combined_list_header,,
)
from nanobot.webui.http_utils import (
    host_for_url as _host_for_url,
)
from nanobot.webui.http_utils import (
    http_error as _http_error,
)
from nanobot.webui.http_utils import (
    http_json_response as _http_json_response,
)
from nanobot.webui.http_utils import (
    http_response as _http_response,
)
from nanobot.webui.http_utils import (
    is_local_browser_request as _is_local_browser_request,
)
from nanobot.webui.http_utils import (
    is_localhost as _is_localhost,
)
from nanobot.webui.http_utils import is_loopback_host as _is_loopback_host
from nanobot.webui.http_utils import (
    is_trusted_proxy_authenticated_request as _is_trusted_proxy_authenticated_request,
)
from nanobot.webui.http_utils import (
    issue_route_secret_matches as _issue_route_secret_matches,,
)
from nanobot.webui.http_utils import (
    normalize_config_path as _normalize_config_path,
)
from nanobot.webui.http_utils import (
    parse_query as _parse_query,
)
from nanobot.webui.http_utils import (
    parse_request_path as _parse_request_path,
)
from nanobot.webui.http_utils import (
    query_first as _query_first,
)
from nanobot.webui.http_utils import (
    safe_host_header as _safe_host_header,
)
from nanobot.webui.ingress_policy import WebIIngressPolicy
from nanobot.webui.media_gateway import WebUIMediaGateway
from nanobot.webui.native_folder_picker import (
    NativeFolderPickerError,
    native_folder_picker_available,
    pick_native_folder,
)
from nanobot.webui.session_automations import (
    all_automations_payload,
    serialize_automation_jobs,
    session_automation_jobs,
    session_automations_payload,
)
from nanobot.webui.session_context import session_context_payload
from nanobot.webui.session_list_index import (
    WEBUI_SESSION_INDEX_INTERNAL_FIELDS,
    indexed_workspace_scope,
    list_webui_sessions,
)
from nanobot.webui.sidebar_state import (
    read_webui_sidebar_state,
    write_webui_sidebar_state,
)
from nanobot.webui.skills_api import (
    SkillManagementError,
    delete_webui_skill,
    set_webui_skill_enabled,
    webui_skill_detail_payload,
    webui_skills_payload,
)
from nanobot.webui.skills_marketplace import (
    SkillsMarketplaceError,
    install_marketplace_skill,
    marketplace_skill_trends,
    search_marketplace_skills,
    trending_marketplace_skills,
)
from nanobot.webui.thread_disk import delete_webui_thread
from nanobot.webui.transcript import build_webui_thread_response
from nanobot.webui.workspaces import WebUIWorkspaceController

_SLOW_WEBUI_HTTP_LOG_MS = 1_000
_WEBUI_MUTATION_PAYLOAD_ATTR = "_nanobot_webui_mutation_payload"
_WEBUI_MUTATION_REQUEST_ATTR = "_nanobot_webui_mutation_request"
_NO_STORE_HEADERS = [("Cache-Control", "no-store")]

_WEBUI_MUTATION_PATHS = {
    "automation.enable": "/api/webui/automations/enable",
    "automation.disable": "/api/webui/automations/disable",
    "automation.delete": "/api/webui/automations/delete",
    "automation.run": "/api/webui/automations/run",
    "automation.update": "/api/webui/automations/update",
    "skill.install": "/api/webui/skills/install",
    "skill.update": "/api/webui/skills/update",
    "skill.delete": "/api/webui/skills/delete",
    "sidebar.update": "/api/webui/sidebar-state/update",
    "workspace.pick_folder": "/api/workspaces/pick-folder",
    "settings.agent.update": "/api/settings/update",
    "settings.model_configuration.create": "/api/settings/model-configurations/create",
    "settings.model_configuration.update": "/api/settings/model-configurations/update",
    "settings.model_configuration.delete": "/api/settings/model-configurations/delete",
    "settings.model_configuration.migrate": "/api/settings/model-configurations/migrate",
    "settings.model_call_order.update": "/api/settings/model-call-order/update",
    "settings.provider.update": "/api/settings/provider/update",
    "settings.provider.create": "/api/settings/provider/create",
    "settings.provider.oauth_login": "/api/settings/provider/oauth-login",
    "settings.provider.oauth_complete": "/api/settings/provider/oauth-login/complete",
    "settings.provider.oauth_logout": "/api/settings/provider/oauth-logout",
    "settings.web_search.update": "/api/settings/web-search/update",
    "settings.api_service.start": "/api/settings/api-service/start",
    "settings.api_service.stop": "/api/settings/api-service/stop",
    "settings.image_generation.update": "/api/settings/image-generation/update",
    "settings.transcription.update": "/api/settings/transcription/update",
    "settings.network_safety.update": "/api/settings/network-safety/update",
    "settings.cli_app.install": "/api/settings/cli-apps/install",
    "settings.cli_app.update": "/api/settings/cli-apps/update",
    "settings.cli_app.uninstall": "/api/settings/cli-apps/uninstall",
    "settings.cli_app.test": "/api/settings/cli-apps/test",
    "settings.feature.enable": "/api/settings/nanobot-features/enable",
    "settings.feature.disable": "/api/settings/nanobot-features/disable",
    "settings.channel.validate": "/api/settings/channels/validate",
    "settings.channel.configure": "/api/settings/channels/configure",
    "settings.pairing.approve": "/api/settings/pairing/approve",
    "settings.pairing.deny": "/api/settings/pairing/deny",
    "settings.mcp.enable": "/api/settings/mcp-presets/enable",
    "settings.mcp.disable": "/api/settings/mcp-presets/disable",
    "settings.mcp.remove": "/api/settings/mcp-presets/remove",
    "settings.mcp.test": "/api/settings/mcp-presets/test",
    "settings.mcp.reconnect": "/api/settings/mcp-presets/reconnect",
    "settings.mcp.custom": "/api/settings/mcp-presets/custom",
    "settings.mcp.import": "/api/settings/mcp-presets/import",
    "settings.mcp.import_cursor": "/api/settings/mcp-presets/import-cursor",
    "settings.mcp.tools": "/api/settings/mcp-presets/tools",
    "settings.mcp.oauth_start": "/api/settings/mcp-oauth/start",
    "settings.mcp.oauth_complete": "/api/settings/mcp-oauth/complete",
    "settings.mcp.oauth_cancel": "/api/settings/mcp-oauth/cancel",
    "admin.provider.models": "/api/admin/provider-models",
    "admin.provider.test": "/api/admin/provider-test",
    "admin.provider.save": "/api/admin/provider-settings/save",
    "admin.execution.save": "/api/admin/execution-settings",
    "admin.execution.test": "/api/admin/execution-test",
    "admin.dbq.execute": "/api/admin/dbq/action",
    "admin.daq.ping": "/api/admin/dbq/status",
    "admin.supabase.action": "/api/admin/supabase/action",
}

_WEBUI_CHANNEL_CONNECT_ACTIONS = {
    "settings.channel.connect.start": "start",
    "settings.channel.connect.poll": "poll",
    "settings.channel.connect.cancel": "cancel",
}

# Fix for #5190: On Windows, mimetypes.guess_type() reads the registry key
# HKEY_CLASSES_ROOT\\.js\\Content Type, which is commonly set to 'text/plain'
# because .js is associated with Windows Script Host rather than web JavaScript.
# That registry value overrides Python's built-in mapping and causes browsers to
# reject ES module scripts with:
#   Failed to load module script: Expected a JavaScript-or-Wasm module script
#   but the server responded with a MIME type of "text/plain".
# We explicitly register correct MIME types for common web static assets here
# (module-import time) so all callers of mimetypes.guess_type() in this process
# benefit, regardless of host registry configuration.
_MIME_FIXES: dict[str, str] = {
    ".js":    "application/javascript",
    ".mjs":   "application/javascript",
    ".css":   "text/css",
    ".html":  "text/html",
    ".json":  "application/json",
    ".svg":   "image/svg+xml",
    ".wasm":  "application/wasm",
}

for _ext, _ctype in _MIME_FIXES.items():
    mimetypes.add_type(_ctype, _ext, strict=True)


if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.websocket.runtime import WebSocketConfig
    from nanobot.cron.service import CronService
    from nanobot.triggers.local_store import LocalTriggerStore
    from nanobot.webui.settings_services import WebUISettingsServices

def _decode_api_key(raw_key: str) -> str | None:
    key = unquote(raw_key)
    _api_key_re = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
    if _api_key_re.match(key) is None:
        return None
    return key


def _mutation_payload(request: WsRequest) -> dict[str, Any] | None:
    payload = getattr(request, _WEBUI_MUTATION_PAYLOAD_ATTR, None)
    if not isinstance(payload, dict):
        return None
    return cast(dict[str, Any], payload)


def _request_query(request: WsRequest) -> dict[str, list[str]]:
    payload = _mutation_payload(request)
    if payload is None:
        return _parse_query(request.path)
    query: dict[str, list[str]] = {}
    for key, value in payload.items():
        if not key:
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif value is None:
            text = ""
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            text = str(value)
        query[key] = [text]
    return query


def _default_model_name_from_config(config_path: Path | None = None) -> str | None:
    try:
        from nanobot.config.loader import load_config
        model = load_config(config_path).resolve_preset().model.strip()
        return model or None
    except Exception as e:
        logger.debug("bootstrap model_name could not load from config: {}", e)
        return None


def _resolve_bootstrap_model_name(
    runtime_name: Callable[[], str | None] | None,
    config_path: Path | None = None,
) -> str:
    if runtime_name is not None:
        try:
            raw = runtime_name()
        except Exception as e:
            logger.debug("bootstrap runtime model resolver failed: {}", e)
        else:
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped:
                    return stripped
    return _default_model_name_from_config(config_path) or ""


# ------------------------------------------------------------------------
# GatewayHTTPHandler
# ------------------------------------------------------------------------


class GatewayHTTPHandler:
    """Handles all HTTP routes served alongside the WebSocket endpoint.

    Routes HTTP requests and delegates stateful work to explicit gateway
    services owned by the composition layer.
    """

    def __init__(
        self,
        *,
        config: WebSocketConfig,
        session_manager: SessionManager | None,
        static_dist_path: Path | None,
        runtime_model_name: Callable[[], str | None] | None,
        runtime_surface: str,
        runtime_capabilities_overrides: dict[str, Any] | None,
        bus: MessageBus,
        tokens: GatewayTokenStore,
        media: WebUQMediaGateway,
        ingress: WebUIngressPolicy,
        workspaces: WebUIWorkspaceController,
        settings: WebUISettingsServices,
        skills_workspace_path: Path,
        disabled_skills: set[str] | None = None,
        cron_service: CronService | None = None,
        local_trigger_store: LocalTriggerStore | None = None,
        cron_pending_job_ids: Callable[[str], set[str]]] | None = None,
        local_trigger_pending_ids: Callable[[str], set[str]]]] | None = None,
        channel_feature_action: Callable[..., Any] | None = None,
        channel_runtime_status: Callable[[], dict[str, Any]] | None = None,
        mcp_runtime_status: Callable[[], Mapping[str, str]] | None = None,
        mcp_reload: Callable[[], Awaitable[dict[str, Any]]]] | None = None,
        skill_state_action: Callable[[set[str]]], None] | None = None,
        log: Any = logger,
    ) -> None:
        self.config = config
        self.session_manager = session_manager
        self.static_dist_path = static_dist_path
        self.runtime_model_name = runtime_model_name
        self.bus = bus
        self.tokens = tokens
        self.media = media
        self.ingress = ingress
        self.workspaces = workspaces
        self.settings = settings
        self.skills_workspace_path = skills_workspace_path
        self.disabled_skills: set[str] = (
            disabled_skills if disabled_skills is not None else set()
        )
        self.skill_state_action = skill_state_action
        self._skill_install_lock = asyncio.Lock()
        self._folder_picker_lock = asyncio.Lock()
        self.cron_service = cron_service
        self.local_trigger_store = local_trigger_store
        self.cron_pending_job_ids = cron_pending_job_ids
        self.local_trigger_pending_ids = local_trigger_pending_ids
        self._log = log
        self._runtime_surface = runtime_surface

        from nanobot.webui.settings_api import runtime_capabilities as _rc
        from nanobot.webui.settings_routes import WebUISettingsRouter

        self._capabilities = _rc(runtime_surface, runtime_capabilities_overrides or {})
        self.settings_routes = WebUISettingsRouter(
            settings=settings,
            bus=bus,
            logger=self._log,
            check_api_token=self.check_api_token,
            parse_query=_parse_query,
            json_response=_http_json_response,
            error_response=_http_error,
            runtime_surface=runtime_surface,
            runtime_capabilities=self._capabilities,
            channel_feature_action=channel_feature_action,
            channel_runtime_status=channel_runtime_status,
            mcp_runtime_status=mcp_runtime_status,
            mcp_reload=mcp_reload,
            mcp_oauth_redirect_uri=self._mcp_oauth_redirect_uri,
        )

    def workspace_controls_available(self, connection: Any) -> bool:
        return self._runtime_surface == "native" or _is_localhost(connection)

    def workspace_folder_picker_available(
        self,
        connection: Any,
        request: WsRequest,
    ) -> bool:
        return (
            _is_loopback_host(self.config.host)
            and _is_local_browser_request(connection, request.headers)
            and native_folder_picker_available()
        )

    # -- Token management ---------------------------------------

    def check_api_token(self, request: WsRequest) -> bool:
        if getattr(request, "_nanobot_trusted_proxy_authenticated", False):
            return True
        return self.tokens.check_api_token(request)

    # -- Main dispatch ------------------------------------

    async def dispatch(self, connection: Any, request: WsRequest) -> Any | None:
        """Route an HTTP request. Returns Response or None."""
        got, _ = _parse_request_path(request.path)
        started = time.perf_counter()
        response: Any | None = None
        setattr(
            request,
            "_nanobot_trusted_proxy_authenticated",
            _is_trusted_proxy_authenticated_request(connection, request.headers, self.config),
        )

        try:
            if self._is_webui_mutation_path(got):
                return _http_error(
                    405,
                    "WebUI mutations require an authenticated WebSocket",
                )
            response = await self._dispatch_resolved(connection, request, got)
            return response
        finally:
            self._log_slow_http(got, response, started)

    async def dispatch_webui_mutation(
        self,
        connection: Any,
        action: str,
        payload: dict[str, Any],
    ) -> Response:
        """Run one explicitly allowlisted mutation for an authenticated WebUI socket."""
        path = self._webui_mutation_path(action, payload)
        if isinstance(path, Response):
            return path

        source_request = getattr(connection, "request", None)
        source_headers = getattr(source_request, "headers", None)
        if source_headers is None:
            headers = Headers()
        else:
            try:
                headers = Headers(source_headers.raw_items())
            except (AttributeError, TypeError):
                try:
                    headers = Headers(source_headers)
                except TypeError:
                    headers = Headers()
        request = WsRequest(path, headers)
        setattr(request, "_nanobot_trusted_proxy_authenticated", True)
        setattr(
            request,
            "_nanobot_admin_authenticated",
            connection in getattr(self, "_admin_connections", set()),
        )
        setattr(request, _WEBUI_MUTATION_REQUEST_ATTR, True)
        setattr(request, _WEBUI_MUTATION_PAYLOAD_ATTR, dict(payload))
        response = await self._dispatch_resolved(connection, request, path)
        if isinstance(response, Response):
            return response
        return _http_error(404, "WebUI mutation action not found")

    def _is_webui_mutation_path(self, path: str) -> bool:
        if self.settings_routes.is_mutation_path(path):
            return True
        if re.match(r"^/api/sessions/[^/]+/delete$", path):
            return True
        if re.match(r"^/api/webui/automations/(enable|disable|delete|run|update)$", path):
            return True
        return path in {
            "/api/webui/skills/install",
            "/api/webui/skills/update",
            "/api/webui/skills/delete",
            "/api/webui/sidebar-state/update",
            "/api/workspaces/pick-folder",
        }

    @staticmethod
    def _webui_mutation_path(
        action: str,
        payload: dict[str, Any],
    ) -> str | Response:
        path = _WEBUI_MUTATION_PATHS.get(action)
        if path is not None:
            return path
        if action == "session.delete":
            key = payload.get("key")
            if not isinstance(key, str) or not key.strip():
                return _http_error(400, "missing session key")
            return f"/api/sessions/{quote(key, safe='')}/delete"
        connect_action = _WEBUI_CHANNEL_CONNECT_ACTIONS.get(action)
        if connect_action is not None:
            channel = payload.get("channel")
            if not isinstance(channel, str) or re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}",
                channel,
            ) is None:
                return _http_error(400, "invalid channel name")
            return f"/api/settings/channels/{channel}/connect/{connect_action}"
        return _http_error(404, "unknown WebUI mutation action")

    async def _dispatch_resolved(
        self,
        connection: Any,
        request: WsRequest,
        got: str,
    ) -> Any | None:
        # Admin dashboard and user registry
        admin_response = admin_route(
            request,
            got,
            issue_admin_token=lambda: self.tokens.issue_token(
                self.config.token_ttl_s,
                audience="admin",
            ),
            ws_path=self.config.path,
            refresh_runtime_config=self.settings.refresh_runtime_config,
        )
        if admin_response is not None:
            return admin_response

        # Token issue endpoint
        if self.config.token_issue_path:
            issue_expected = _normalize_config_path(self.config.token_issue_path)
            if got == issue_expected:
                return self._handle_token_issue(connection, request)

        # Bootstrap
        if got == "/webui/bootstrap":
            return self._handle_bootstrap(connection, request)

        # Settings routes (delegated)
        response = await self.settings_routes.dispatch(connection, request, got)
        if response is not None:
            return response

        # Session routes
        response = await self._dispatch_session_routes(request, got)
        if response is not None:
            return response

        # Media routes
        response = self._dispatch_media_routes(request, got)
        if response is not None:
            return response

        # Automation routes
        response = await self._dispatch_automation_routes(request, got)
        if response is not None:
            return response

        # Misc routes
        response = await self._dispatch_misc_routes(connection, request, got)
        if response is not None:
            return response

        # API 404 (never serve SPA for /api/ routes)
        if got.startswith("/api/"):
            return _http_error(404, "API route not found")

        # Static SPA serving
        if self.static_dist_path is not None:
            response = self._serve_static(
                got,
                accept_encoding=_combined_list_header(request.headers, "Accept-Encoding"),
            )
            if response is not None:
                return response

        return _http_error(404, "Not Found")

    def _log_slow_http(self, path: str, response: Any | None, started: float) -> None:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if elapsed_ms < _SLOW_WEBUI_HTTP_LOG_MS:
            return
        if not (path.startswith("/api/") or path == "/webui/bootstrap"):
            return
        status = getattr(response, "status_code", None)
        self._log.warning(
            "slow webui http route path={} status={} duration_ms={}",
            path,
            status if status is not None else "none",
            elapsed_ms,
        )

    # -- Token issue ---------------------------------------

    def _handle_token_issue(self, connection: Any, request: Any) -> Any:
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return _http_error(401, "Unauthorized")
        else:
            self._log.warning(
                "token_issue_path is set but token_issue_secret is empty; "
                "any client can obtain connection tokens â€” set token_issue_secret for production."
            )
        if not self.tokens.can_issue():
            self._log.error(
                "too many outstanding issued tokens ({}), rejecting issuance",
                len(self.tokens.issued_tokens),
            )
            return _http_json_response(
                {"error": "too many outstanding tokens"},
                status=429,
                extra_headers=_NO_STORE_HEADERS,
            )
        token_value = self.tokens.issue_token(self.config.token_ttl_s)
        return _http_json_response(
            token_response_payload(token_value, self.config.token_ttl_s]),
            extra_headers=_NO_STORE_HEADERS,
        )

    # -- Bootstrap ---------------------------------------------

    def _handle_bootstrap(self, connection: Any, request: Any) -> Response:
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        is_local_browser = _is_local_browser_request(connection, request.headers)
        is_proxy_authenticated = _is_trusted_proxy_authenticated_request(
            connection,
            request.headers,
            self.config,
        )
        if not is_proxy_authenticated:
            if secret:
                if not _issue_route_secret_matches(request.headers, secret):
                    return _http_error(401, "Unauthorized")
            elif not is_local_browser:
                return _http_error(403, "bootstrap is localhost-only")

        if is_proxy_authenticated:
            payload = {
                "ws_path": _normalize_config_path(self.config.path),
                "ws_url": self._bootstrap_ws_url(request),
                "limits": self.ingress.bootstrap_limits(
                    max_frame_bytes=self.config.max_message_bytes,
                ),
                "model_name": _resolve_bootstrap_model_name(
                    self.runtime_model_name,
                    self.settings.config.path,
                ),
                "runtime_surface": self._runtime_surface,
                "runtime_capabilities": self._capabilities,
            }
            self._maybe_add_supabase_realtime_config(payload)
            return _http_json_response(payload, extra_headers=_NO_STORE_HEADERS)

        api_token_allowed = bool(secret) or is_local_browser
        if not self.tokens.can_issue(include_api_token=api_token_allowed):
            return _http_response(
                json.dumps({"error": "too many outstanding tokens"}).encode("utf-8"),
                status=429,
                content_type="application/json; charset=utf-8",
                extra_headers=_NO_STORE_HEADERS,
            )
        token = self.tokens.issue_token(self.config.token_ttl_s[, audience="webui")
        api_token = (
            self.tokens.issue_api_token(self.config.token_ttl_s)
            if api_token_allowed
            else None
        )

        ws_url = self._bootstrap_ws_url(request)
        expected_path = _normalize_config_path(self.config.path)
        payload = {
            "token": token,
            "ws_path": expected_path,
            "ws_url": ws_url,
            "expires_in": self.config.token_ttl_s[,
            "limits": self.ingress.bootstrap_limits(
                max_frame_bytes=self.config.max_message_bytes,
            ),
            "model_name": _resolve_bootstrap_model_name(
                self.runtime_model_name,
                 self.settings.config.path,
            ),
            "runtime_surface": self._runtime_surface,
            "runtime_capabilities": self._capabilities,
        }
        if api_token is not None:
            payload["api_token"] = api_token
        self._maybe_add_supabase_realtime_config(payload)
        return _http_json_response(payload, extra_headers=_NO_STORE_HEADERS)

    def _maybe_add_supabase_realtime_config(self, payload: dict[str, Any]) -> None:
        """Inject Supabase Realtime config into the bootstrap payload.

        The frontend uses these values to open a WebSocket to Supabase
        Realtime and subscribe to agent_feedback row inserts, so agent
        feedback bypasses the Render reverse proxy entirely.

        Only the anon key is sent (safe to expose in the browser).
        Realtime is only advertised when SUPABASE_REALTIME_ENABLED is
        truthy (defaults to true when Supabase credentials are present).
        """
        url = os.getenv("SUPABASE_URL", "").strip()
        anon_key = os.getenv("SUPABASE_ANON_KEY", "").strip()
        if not url or not anon_key:
            return
        env_flag = os.getenv("SUPABASE_REALTIME_ENABLED", "true").strip().lower()
        if env_flag in {"0", "false", "no"}:
            return
        payloa["supabase_url"] = url
        payload["supabase_anon_key"] = anon_key

    def _bootstrap_ws_url(self, request: Any) -> str:
        headers = getattr(request, "headers", {}) or {}
        if self.config.public_ws_url:
            return self.config.public_ws_url
        host = _safe_host_header(_case_insensitive_header(headers, "Host"))
        if not host:
            host = _host_for_url(self.config.host, self.config.port)
        proto = _case_insensitive_header(headers, "X-Forwarded-Proto")
        proto = proto.split(",", 1)[0].strip().lower()
        secure = proto in {"https", "wss"} or bool(self.config.ssl_certifile.strip())
        scheme = "wss" if secure else "ws"
        expected_path = _normalize_config_path(self.config.path)
        return f"{scheme}://{host}{expected_path}"

    def _mcp_oauth_redirect_uri(self, request: WsRequest) -> str:
        """Derive the browser callback from the same public origin as WebSocket bootstrap."""
        from nanobot.agent.tools.mcp_oauth import MCP_OAUTH_CALLBACK_PATH

        public_ws_url = urlsplit(self._bootstrap_ws_url(request))
        scheme = "https" if public_ws_url.scheme == "wss" else "http"
        return urlunsplit((scheme, public_ws_url.netloc, MCP_OAUUÐÐSPÒ×ÔUˆ‹ˆŠJB‚ˆÈKHÙ\ÜÚ[Ûˆ›Ý]\ÈKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKB‚ˆ\Þ[˜ÈYˆÙ\Ü]ÚÜÙ\ÜÚ[Û—Ü›Ý]\ÊÙ[‹™\]Y\ÝˆÜÔ™\]Y\ÝÛÝˆÝŠHOˆ™\ÜÛœÙH›Û™N‚ˆHH™K›X]Ú
ˆ—‹Ø\KÜÙ\ÜÚ[ÛœËÊ×‹×JÊKÝÙXZK]™XY	‹ÛÝ
BˆYˆN‚ˆ™]\›ˆÙ[‹—Ú[™WÝÙXZWÝ™XYÙÙ]
™\]Y\ÝK™Ü›Ý\
JJB‚ˆHH™K›X]Ú
ˆ—‹Ø\KÜÙ\ÜÚ[ÛœËÊ×‹×JÊKØÛÛ^	‹ÛÝ
BˆYˆN‚ˆ™]\›ˆ]ØZ]Ù[‹—Ú[™WÜÙ\ÜÚ[Û—ØÛÛ^ÙÙ]
™\]Y\ÝK™Ü›Ý\
JJB‚ˆHH™K›X]Ú
ˆ—‹Ø\KÜÙ\ÜÚ[ÛœËÊ×‹×JÊKÙš[K\™]šY]É‹ÛÝ
BˆYˆN‚ˆ™]\›ˆÙ[‹—Ú[™WÙš[WÜ™]šY]Ê™\]Y\ÝK™Ü›Ý\
JJB‚ˆHH™K›X]Ú
ˆ—‹Ø\KÜÙ\ÜÚ[ÛœËÊ×‹×JÊKØ]]ÛX][ÛœÉ‹ÛÝ
BˆYˆN‚ˆ™]\›ˆÙ[‹—Ú[™WÜÙ\ÜÚ[Û—Ø]]ÛX][ÛœÊ™\]Y\ÝK™Ü›Ý\
JJB‚ˆHH™K›X]Ú
ˆ—‹Ø\KÜÙ\ÜÚ[ÛœËÊ×‹×JKÙ[]I‹ÛÝ
BˆYˆN‚ˆ™]\›ˆÙ[‹—Ú[™WÜÙ\ÜÚ[Û—Ù[]J™\]Y\ÝK™Ü›Ý\
JJB‚ˆ™]\›ˆ›Û™B‚ˆ\Þ[˜ÈYˆÚ[™WÜÙ\ÜÚ[Û—ØÛÛ^ÙÙ]
Ù[‹™\]Y\ÝˆÜÔ™\]Y\ÝÙ^NˆÝŠHOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆXÛÙYÚÙ^HHÙXÛÙWØ\WÚÙ^JÙ^JBˆYˆXÛÙYÚÙ^H\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠš[˜[YÙ\ÜÚ[ÛˆÙ^HŠBˆYˆ›ÝÚ\×ÝÙXœÛØÚÙ]ØÚ[›™[ÜÙ\ÜÚ[Û—ÚÙ^JXÛÙYÚÙ^JN‚ˆ™]\›ˆÚÙ\œ›ÜŠœÙ\ÜÚ[Ûˆ›Ý›Ý[™ŠBˆYˆÙ[‹œÙ\ÜÚ[Û—ÛX[˜YÙ\ˆ\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠLËœÙ\ÜÚ[ÛˆX[˜YÙ\ˆ[˜]˜Z[X›HŠBˆÙ\ÜÚ[ÛˆH]ØZ]\Þ[˜Ú[Ë×Ý™XYˆÙ[‹œÙ\ÜÚ[Û—ÛX[˜YÙ\‹œ™XYÜÙ\ÜÚ[Û—ÜÛ˜\ÚÝˆXÛÙYÚÙ^Kˆ
BˆYˆÙ\ÜÚ[Ûˆ\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠœÙ\ÜÚ[Ûˆ›Ý›Ý[™ŠBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJÙ\ÜÚ[Û—ØÛÛ^Ü^[ØY
Ù\ÜÚ[ÛŠJB‚ˆ\Þ[˜ÈYˆÚ[™WÜÙ\ÜÚ[Ûœ×Û\Ý
Ù[‹™\]Y\ÝˆÜÔ™\]Y\Ý
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆYˆÙ[‹œÙ\ÜÚ[Û—ÛX[˜YÙ\ˆ\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠLËœÙ\ÜÚ[ÛˆX[˜YÙ\ˆ[˜]˜Z[X›HŠBˆ^[ØYH]ØZ]\Þ[˜Ú[Ë×Ý™XY
Ù[‹—ÜÙ\ÜÚ[Ûœ×Û\ÝÜ^[ØY
Bˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJˆ^[ØYˆXØÙ\Ù[˜ÛÙ[™ÏWØÛÛXš[™YÛ\ÝÚXY\Š™\]Y\ÝšXY\œËXØÙ\Q[˜ÛÙ[™ÈŠKˆ
B‚ˆYˆÜÙ\ÜÚ[Ûœ×Û\ÝÜ^[ØY
Ù[ŠHOˆXÝÜÝ‹[žWN‚ˆ\ÜÙ\Ù[‹œÙ\ÜÚ[Û—ÛX[˜YÙ\ˆ\È›Ý›Û™Bˆœ›ÛH˜[›Ø›ÝœÙ\ÜÚ[Û‹ÙXZWÝ\›œÈ[\ÜÙXœÛØÚÙ]Ý\›—ÝØ[ÜÝ\YØ]‚ˆÙ\ÜÚ[ÛœÈH\ÝÝÙXZWÜÙ\ÜÚ[ÛœÊÙ[‹œÙ\ÜÚ[Û—ÛX[˜YÙ\ŠBˆ[™\ÈHÙ\ÜÚ[Û’[™T™\ÛÛ™\ŠÙ[‹œÙ\ÜÚ[Û—ÛX[˜YÙ\ŠK›\ÝØ[ØžWÚÙ^J
BˆÛX[™Yˆ\ÝÙXÝÜÝ‹[žWWHH×BˆY˜][ÜØÛÜNˆÛÜšÜÜXÙTØÛÜH›Û™HH›Û™Bˆ›ÜˆÈ[ˆÙ\ÜÚ[ÛœÎ‚ˆÙ^HHË™Ù]
šÙ^HŠBˆYˆ›Ý
\Ú[œÝ[˜ÙJÙ^KÝŠH[™Ù^KœÝ\ÝÚ]
ÙXœÛØÚÙ]ˆŠJN‚ˆÛÛ[YBˆ›ÝÈHÂˆÎˆ‚ˆ›ÜˆËˆ[ˆËš][\Ê
BˆYˆÈOHœ]ˆ[™È›Ý[ˆÑP•RWÔÑTÔÒSÓ—ÒS‘VÒS•T“SÑ’QSÂˆBˆÚ]ÚYHÙ^KœÜ]
Žˆ‹JVÌWBˆÝ\YØ]HÙXœÛØÚÙ]Ý\›—ÝØ[ÜÝ\YØ]
Ú]ÚY
BˆYˆÝ\YØ]\È›Ý›Û™N‚ˆ›ÝÖÈœ[—ÜÝ\YØ]—HHÝ\YØ]ˆYˆY˜][ÜØÛÜH\È›Û™N‚ˆY˜][ÜØÛÜHHÙ[‹ÛÜšÜÜXÙ\Ë™Y˜][ÜØÛÜJ
BˆØÛÜWÜ™\Ù[˜]×ÜØÛÜHH[™^YÝÛÜšÜÜXÙWÜØÛÜJÊBˆØÛÜHHÙ[‹ÛÜšÜÜXÙ\ËœØÛÜWÙ›Ü—Ú[™^YÛY]Y]Jˆ˜]×ÜØÛÜKˆØÛÜWÜ™\Ù[\ØÛÜWÜ™\Ù[ˆY˜][ÜØÛÜOYY˜][ÜØÛÜKˆ
Bˆ›ÝÖÈÛÜšÜÜXÙWÜØÛÜH—HHØÛÜKœ^[ØY

Bˆ[™HH[™\Ë™Ù]
Ù^JBˆYˆ[™H\È›Ý›Û™N‚ˆ›ÝÖÈš[™H—HH[™KœX›X×Ü^[ØY

BˆÛX[™Y˜\[™
›ÝÊBˆ™]\›ˆÈœÙ\ÜÚ[ÛœÈŽˆÛX[™YB‚ˆYˆÚ[™WÝÙXZWÝ™XYÙÙ]
Ù[‹™\]Y\ÝˆÜÔ™\]Y\ÝÙ^NˆÝŠHOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆXÛÙYÚÙ^HHÙXÛÙWØ\WÚÙ^JÙ^JBˆYˆXÛÙYÚÙ^H\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠš[˜[YÙ\ÜÚ[ÛˆÙ^HŠBˆYˆ›ÝÚ\×ÝÙXœÛØÚÙ]ØÚ[›™[ÜÙ\ÜÚ[Û—ÚÙ^JXÛÙYÚÙ^JN‚ˆ™]\›ˆÚÙ\œ›ÜŠœÙ\ÜÚ[Ûˆ›Ý›Ý[™ŠBˆØÛÜHHÙ[‹ÛÜšÜÜXÙ\ËœØÛÜWÙ›Ü—ÜÙ\ÜÚ[Û—ÚÙ^JXÛÙYÚÙ^JB‚ˆYˆØYÜÙ\ÜÚ[Û—ÛY\ÜØYÙ\Ê
HOˆ\ÝÙXÝÜÝ‹[žWWH›Û™N‚ˆYˆÙ[‹œÙ\ÜÚ[Û—ÛX[˜YÙ\ˆ\È›Û™N‚ˆ™]\›ˆ›Û™BˆÙ\ÜÚ[Û—Ù]HHÙ[‹œÙ\ÜÚ[Û—ÛX[˜YÙ\‹œ™XYÜÙ\ÜÚ[Û—Ùš[JXÛÙYÚÙ^JBˆ˜]×ÛY\ÜØYÙ\ÈHÙ\ÜÚ[Û—Ù]K™Ù]
›Y\ÜØYÙ\ÈŠHYˆ\Ú[œÝ[˜ÙJÙ\ÜÚ[Û—Ù]KXÝ
H[ÙH›Û™BˆYˆ›Ý\Ú[œÝ[˜ÙJ˜]×ÛY\ÜØYÙ\Ë\Ý
N‚ˆ™]\›ˆ›Û™Bˆ˜]×ÜÙ\ÜÚ[Û—ÛY\ÜØYÙ\ÈHØ\Ý
\ÝÐ[žWK˜]×ÛY\ÜØYÙ\ÊBˆ™]\›ˆÂˆØ\Ý
XÝÜÝ‹[žWWK˜]×ÛY\ÜØYÙJBˆ›Üˆ˜]×ÛY\ÜØYÙH[ˆ˜]×ÜÙ\ÜÚ[Û—ÛY\ÜØYÙ\ÂˆYˆ\Ú[œÝ[˜ÙJ˜]×ÛY\ÜØYÙKXÝ
BˆB‚ˆ]Y\žHHÜ\œÙWÜ]Y\žJ™\]Y\Ýœ]
Bˆ˜]×Û[Z]HÜ]Y\žWÙš\œÝ
]Y\žK›[Z]ŠBˆ[Z]ˆ[›Û™HH›Û™BˆYˆ˜]×Û[Z]\È›Ý›Û™H[™˜]×Û[Z]œÝš\

N‚ˆžN‚ˆ[Z]H[
˜]×Û[Z]
Bˆ^Ù\˜[YQ\œ›ÜŽ‚ˆ™]\›ˆÚÙ\œ›ÜŠš[˜[Y[Z]ŠBˆ\™XÝ[ÛˆHÜ]Y\žWÙš\œÝ
]Y\žK™\™XÝ[ÛˆŠBˆYˆ\™XÝ[Ûˆ\È›Ý›Û™H[™\™XÝ[Ûˆ›Ý[ˆÈ›]\ÝŸN‚ˆ™]\›ˆÚÙ\œ›ÜŠš[˜[Y\™XÝ[ÛˆŠBˆ™Y›Ü™HHÜ]Y\žWÙš\œÝ
]Y\žK˜™Y›Ü™HŠBˆœ›ÛH˜[›Ø›ÝœÙ\ÜÚ[Û‹ÙXZWÝ\›œÈ[\Ü
ˆÙXœÛØÚÙ]Ý\›—ÚYˆÙXœÛØÚÙ]Ý\›—Ý˜[œØÜš\Ü\œÚ\Ý[˜ÙWÙ˜Z[YˆÙXœÛØÚÙ]Ý\›—ÝØ[ÜÝ\YØ]ˆ
B‚ˆÚ]ÚYHXÛÙYÚÙ^KœÜ]
Žˆ‹JVÌWBˆXÝ]™WÝ\›—ÜÝ\YØ]HÙXœÛØÚÙ]Ý\›—ÝØ[ÜÝ\YØ]
Ú]ÚY
BˆXÝ]™WÝ\›—ÚYHÙXœÛØÚÙ]Ý\›—ÚY
Ú]ÚY
BˆXÝ]™WÝ\›—Ý˜[œØÜš\Ü\œÚ\Ý[˜ÙWÙ˜Z[YH
ˆÙXœÛØÚÙ]Ý\›—Ý˜[œØÜš\Ü\œÚ\Ý[˜ÙWÙ˜Z[Y
Ú]ÚY
Bˆ
Bˆ]HHZ[ÝÙXZWÝ™XYÜ™\ÜÛœÙJˆXÛÙYÚÙ^Kˆ]YÛY[Ý\Ù\—ÛYYXO\Ù[‹›YYXK˜]YÛY[Ý˜[œØÜš\ÛYYXKˆ]YÛY[Ø\ÜÚ\Ý[ÛYYXO\Ù[‹›YYXK˜]YÛY[Ý˜[œØÜš\ÛYYXKˆ]YÛY[Ø\ÜÚ\Ý[Ý^[[X™H^ˆÙ[‹›YYXKœ™]Üš]WÛØØ[ÛX\šÙÝÛ—Ú[XYÙ\Êˆ^ˆÛÜšÜÜXÙWÜ]\ØÛÜKœ›Ú™XÝÜ]ˆ
KˆÙ\ÜÚ[Û—ÛY\ÜØYÙ\×ÛØY\[ØYÜÙ\ÜÚ[Û—ÛY\ÜØYÙ\ËˆXÝ]™WÝ\›—ÜÝ\YØ]XXÝ]™WÝ\›—ÜÝ\YØ]ˆXÝ]™WÝ\›—ÚYXXÝ]™WÝ\›—ÚYˆXÝ]™WÝ\›—Ý˜[œØÜš\Ü\œÚ\Ý[˜ÙWÙ˜Z[YJˆXÝ]™WÝ\›—Ý˜[œØÜš\Ü\œÚ\Ý[˜ÙWÙ˜Z[Yˆ
Kˆ[Z][[Z]ˆ\™XÝ[ÛY\™XÝ[Û‹ˆ™Y›Ü™OX™Y›Ü™Kˆ
BˆYˆ]H\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠÙXZH™XY›Ý›Ý[™ŠBˆ]VÈÛÜšÜÜXÙWÜØÛÜH—HHØÛÜKœ^[ØY

Bˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJˆ]KˆXØÙ\Ù[˜ÛÙ[™ÏWØÛÛXš[™YÛ\ÝÚXY\Š™\]Y\ÝšXY\œËXØÙ\Q[˜ÛÙ[™ÈŠKˆ
B‚ˆYˆÚ[™WÙš[WÜ™]šY]ÜÙ[‹™\]Y\ÝˆÜÔ™\]Y\ÝÙ^NˆÝŠHOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆXÛÙYÚÙ^HHÙXÛÙWØ\WÚÙ^JÙ^JBˆYˆXÛÙYÚÙ^H\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠš[˜[YÙ\ÜÚ[ÛˆÙ^HŠBˆYˆ›ÝÚ\×ÝÙXœÛØÚÙ]ØÚ[›™[ÜÙ\ÜÚ[Û—ÚÙ^JXÛÙYÚÙ^JN‚ˆ™]\›ˆÚÙ\œ›ÜŠœÙ\ÜÚ[Ûˆ›Ý›Ý[™ŠBˆ]Y\žHHÜ\œÙWÜ]Y\žJ™\]Y\Ýœ]
Bˆ]HÜ]Y\žWÙš\œÝ
]Y\žKœ]ŠBˆ\×Ü›Ø™HHÜ]Y\žWÙš\œÝ
]Y\žKœ›Ø™HŠHOHŒH‚ˆžN‚ˆØÛÜHHÙ[‹ÛÜšÜÜXÙ\ËœØÛÜWÙ›Ü—ÜÙ\ÜÚ[Û—ÚÙ^JXÛÙYÚÙ^JBˆYˆ\×Ü›Ø™N‚ˆ^[ØYHš[WÜ™]šY]×Ø]˜Z[Xš[]WÜ^[ØY
]ØÛÜO\ØÛÜJBˆ[ÙN‚ˆ^[ØYHš[WÜ™]šY]×Ü^[ØY
]ØÛÜO\ØÛÜJBˆ^Ù\ÙX•Qš[T™]šY]Ñ\œ›Üˆ\ÈN‚ˆYˆ\×Ü›Ø™H[™KœÝ]\È[ˆÍËM_N‚ˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJÞÈ˜]˜Z[X›HŽˆ˜[Ù__JBˆ™]\›ˆÚÙ\œ›ÜŠKœÝ]\ËK›Y\ÜØYÙJBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJ^[ØY
B‚ˆYˆÚ[™WÜÙ\ÜÚ[Û—Ø]]ÛX][ÛœÊÙ[‹™\]Y\ÝˆÜÔ™\]Y\ÝÙ^NˆÝŠHOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆXÛÙYÚÙ^HHÙXÛÙWØ\WÚÙ^JÙ^JBˆYˆXÛÙYÚÙ^H\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠš[˜[YÙ\ÜÚ[ÛˆÙ^HŠBˆYˆ›ÝÚ\×ÝÙXœÛØÚÙ]ØÚ[›™[ÜÙ\ÜÚ[Û—ÚÙ^JXÛÙYÚÙ^JN‚ˆ™]\›ˆÚÙ\œ›ÜŠœÙ\ÜÚ[Ûˆ›Ý›Ý[™ŠBˆ[™[™×Ú›Ø—ÚYÈHÙ[‹—Ü[™[™×Ø]]ÛX][Û—ÚY×Ù›Ü—ÜÙ\ÜÚ[ÛŠXÛÙYÚÙ^JBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJˆÙ\ÜÚ[Û—Ø]]ÛX][Ûœ×Ü^[ØY
ˆÙ[‹˜Ü›Û—ÜÙ\šXÙKˆXÛÙYÚÙ^KˆØØ[ÝšYÙÙ\—ÜÝÜ™O\Ù[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™Kˆ[™[™×Ú›Ø—ÚYÏ\[™[™×Ú›Ø—ÚYËˆ
Bˆ
B‚ˆYˆÚ[™WÜÙ\ÜÚ[Û—Ù[]JÙ[‹™\]Y\ÝˆÜÔ™\]Y\ÝÙ^NˆÝŠHOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆYˆÙ[‹œÙ\ÜÚ[Û—ÛX[˜YÙ\ˆ\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠLËœÙ\ÜÚ[ÛˆX[˜YÙ\ˆ[˜]˜Z[X›HŠBˆXÛÙYÚÙ^HHÙXÛÙWØ\WÚÙ^JÙ^JBˆYˆXÛÙYÚÙ^H\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠš[˜[YÙ\ÜÚ[ÛˆÙ^HŠBˆYˆ›ÝÚ\×ÝÙXœÛØÚÙ]ØÚ[›™[ÜÙ\ÜÚ[Û—ÚÙ^JXÛÙYÚÙ^JN‚ˆ™]\›ˆÚÙ\œ›ÜŠœÙ\ÜÚ[Ûˆ›Ý›Ý[™ŠBˆ]Y\žHHÜ™\]Y\ÝÜ]Y\žJ™\]Y\Ý
Bˆ[]WØ]]ÛX][ÛœÈH
Ü]Y\žWÙš\œÝ
]Y\žK™[]WØ]]ÛX][ÛœÈŠHÜˆˆŠK›ÝÙ\Š
Bˆ]]ÛX][Û—Ú›ØœÈHÙ\ÜÚ[Û—Ø]]ÛX][Û—Ú›ØœÊˆÙ[‹˜Ü›Û—ÜÙ\šXÙKˆXÛÙYÚÙ^KˆØØ[ÝšYÙÙ\—ÜÝÜ™O\Ù[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™Kˆ
BˆYˆ]]ÛX][Û—Ú›ØœÈ[™[]WØ]]ÛX][ÛœÈ›Ý[ˆÈŒH‹YH‹žY\ÈŸN‚ˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJˆÂˆ™[]YŽˆ˜[ÙKˆ˜›ØÚÙYØžWØ]]ÛX][ÛœÈŽˆYKˆ˜]]ÛX][ÛœÈŽˆÙ\šX[^™WØ]]ÛX][Û—Ú›ØœÊ]]ÛX][Û—Ú›ØœÊKˆBˆ
BˆYˆ]]ÛX][Û—Ú›ØœÎ‚ˆ›Üˆ›Øˆ[ˆ]]ÛX][Û—Ú›ØœÎ‚ˆYˆ\Ú[œÝ[˜ÙJ›Ø‹ØØ[šYÙÙ\ŠN‚ˆYˆÙ[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™H\È›Ý›Û™N‚ˆÙ[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™K™[]J›Ø‹šY
Bˆ[YˆÙ[‹˜Ü›Û—ÜÙ\šXÙH\È›Ý›Û™N‚ˆÙ[‹˜Ü›Û—ÜÙ\šXÙKœ™[[Ý™WÚ›ØŠ›Ø‹šY
BˆÙ\ÜÚ[Û—Ù[]YHÙ[‹œÙ\ÜÚ[Û—ÛX[˜YÙ\‹™[]WÜÙ\ÜÚ[ÛŠXÛÙYÚÙ^JBˆ˜[œØÜš\Ù[]YH[]WÝÙXZWÝ™XY
XÛÙYÚÙ^JBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJÈ™[]YŽˆ›ÛÛ
Ù\ÜÚ[Û—Ù[]YÜˆ˜[œØÜš\Ù[]Y
_JB‚ˆÈKH]]ÛX][Ûˆ›Ý]\ÈKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKB‚ˆ\Þ[˜ÈYˆÙ\Ü]ÚØ]]ÛX][Û—Ü›Ý]\ÊˆÙ[‹ˆ™\]Y\ÝˆÜÔ™\]Y\ÝˆÛÝˆÝ‹ˆ
HOˆ™\ÜÛœÙH›Û™N‚ˆYˆÛÝOH‹Ø\KÝÙXZKØ]]ÛX][ÛœÈŽ‚ˆ™]\›ˆÙ[‹—Ú[™WÝÙXZWØ]]ÛX][ÛœÊ™\]Y\Ý
BˆHH™K›X]Ú
ˆ—‹Ø\KÝÙXZKØ]]ÛX][ÛœËÊ[˜X›_\ØX›_[]_[Ÿ\]JI‹ÛÝ
BˆYˆN‚ˆ™]\›ˆ]ØZ]Ù[‹—Ú[™WÝÙXZWØ]]ÛX][Û—ØXÝ[ÛŠ™\]Y\ÝK™Ü›Ý\
JJBˆ™]\›ˆ›Û™B‚ˆYˆÜ[™[™×ØÜ›Û—Ú›Ø—ÚY×Ù›Ü—Ø[
Ù[ŠHOˆÙ]ÜÝ—N‚ˆYˆÙ[‹˜Ü›Û—ÜÙ\šXÙH\È›Û™HÜˆÙ[‹˜Ü›Û—Ü[™[™×Ú›Ø—ÚYÈ\È›Û™N‚ˆ™]\›ˆÙ]

Bˆ[™[™ÎˆÙ]ÜÝ—HHÙ]

Bˆ›Üˆ›Øˆ[ˆÙ[‹˜Ü›Û—ÜÙ\šXÙK›\ÝÚ›ØœÊ[˜ÛYWÙ\ØX›YUYJN‚ˆÙ\ÜÚ[Û—ÚÙ^HH›Ø‹œ^[ØYœÙ\ÜÚ[Û—ÚÙ^BˆYˆ›ÝÙ\ÜÚ[Û—ÚÙ^H[™›Ø‹œ^[ØY›ÜšYÚ[—ØÚ[›™[[™›Ø‹œ^[ØY›ÜšYÚ[—ØÚ]ÚY‚ˆÙ\ÜÚ[Û—ÚÙ^HHžÈš›Ø‹œ^[ØY›ÜšYÚ[—ØÚ[›™[NžÚ›Ø‹œ^[ØY›ÜšYÚ[—ØÚ]ÚYH‚ˆYˆÙ\ÜÚ[Û—ÚÙ^N‚ˆ[™[™Ë\]JÙ[‹˜Ü›Û—Ü[™[™×Ú›Ø—ÚYÊÙ\ÜÚ[Û—ÚÙ^JJBˆ™]\›ˆ[™[™Â‚ˆYˆÜ[™[™×ÛØØ[ÝšYÙÙ\—ÚY×Ù›Ü—Ø[
Ù[ŠHOˆÙ]ÜÝ—N‚ˆYˆÙ[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™H\È›Û™HÜˆÙ[‹›ØØ[ÝšYÙÙ\—Ü[™[™×ÚYÈ\È›Û™N‚ˆ™]\›ˆÙ]

Bˆ[™[™ÎˆÙ]ÜÝ—HHÙ]

Bˆ›ÜˆšYÙÙ\ˆ[ˆÙ[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™K›\ÝÝšYÙÙ\œÊ[˜ÛYWÙ\ØX›YUYJN‚ˆÙ\ÜÚ[Û—ÚÙ^HHšYÙÙ\‹œÙ\ÜÚ[Û—ÚÙ^BˆYˆ›ÝÙ\ÜÚ[Û—ÚÙ^H[™šYÙÙ\‹˜Ú[›™[[™šYÙÙ\‹˜Ú]ÚY‚ˆÙ\ÜÚ[Û—ÚÙ^HHˆžÝšYÙÙ\‹˜Ú[›™[NžÝšYÙÙ\‹˜Ú]ÚYH‚ˆYˆÙ\ÜÚ[Û—ÚÙ^N‚ˆ[™[™Ë\]JÙ[‹›ØØ[ÝšYÙÙ\—Ü[™[™×ÚYÊÙ\ÜÚ[Û—ÚÙ^JJBˆ™]\›ˆ[™[™Â‚ˆYˆÜ[™[™×Ø]]ÛX][Û—ÚY×Ù›Ü—ÜÙ\ÜÚ[ÛŠÙ[‹Ù\ÜÚ[Û—ÚÙ^NˆÝŠHOˆÙ]ÜÝ—N‚ˆ[™[™ÎˆÙ]ÜÝ—HHÙ]

BˆYˆÙ[‹˜Ü›Û—Ü[™[™×Ú›Ø—ÚYÈ\È›Ý›Û™N‚ˆ[™[™Ë\]JÙ[‹˜Ü›Û—Ü[™[™×Ú›Ø—ÚYÊÙ\ÜÚ[Û—ÚÙ^JJBˆYˆÙ[‹›ØØ[ÝšYÙÙ\—Ü[™[™×ÚYÈ\È›Ý›Û™N‚ˆ[™[™Ë\]JÙ[‹›ØØ[ÝšYÙÙ\—Ü[™[™×ÚYÊÙ\ÜÚ[Û—ÚÙ^JJBˆ™]\›ˆ[™[™Â‚ˆYˆÚ[™WÝÙXZWØ]]ÛX][ÛœÊÙ[‹™\]Y\ÝˆÜÔ™\]Y\Ý
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆ[™[™×Ú›Ø—ÚYÈHÙ[‹—Ü[™[™×ØÜ›Û—Ú›Ø—ÚY×Ù›Ü—Ø[

Bˆ[™[™×Ú›Ø—ÚYË\]JÙ[‹—Ü[™[™×ÛØØ[ÝšYÙÙ\—ÚY×Ù›Ü—Ø[

JBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJˆ[Ø]]ÛX][Ûœ×Ü^[ØY
ˆÙ[‹˜Ü›Û—ÜÙ\šXÙKˆØØ[ÝšYÙÙ\—ÜÝÜ™O\Ù[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™KˆÙ\ÜÚ[Û—ÛX[˜YÙ\\Ù[‹œÙ\ÜÚ[Û—ÛX[˜YÙ\‹ˆ[™[™×Ú›Ø—ÚYÏ\[™[™×Ú›Ø—ÚYËˆ
Bˆ
B‚ˆ\Þ[˜ÈYˆÚ[™WÝÙXZWØ]]ÛX][Û—ØXÝ[ÛŠˆÙ[‹ˆ™\]Y\ÝˆÜÔ™\]Y\ÝˆXÝ[ÛŽˆÝ‹ˆ
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆYˆÙ[‹˜Ü›Û—ÜÙ\šXÙH\È›Û™H[™Ù[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™H\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠLË˜]]ÛX][ÛˆÙ\šXÙH[˜]˜Z[X›HŠB‚ˆ]Y\žHHÜ™\]Y\ÝÜ]Y\žJ™\]Y\Ý
Bˆ›Ø—ÚYH
Ü]Y\žWÙš\œÝ
]Y\žKšYŠHÜˆÜ]Y\žWÙš\œÝ
]Y\žKš›Ø—ÚYŠHÜˆˆŠKœÝš\

BˆYˆ›Ý›Ø—ÚY‚ˆ™]\›ˆÚÙ\œ›ÜŠ›Z\ÜÚ[™È]]ÛX][ÛˆYŠBˆšYÙÙ\ˆHÙ[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™K™Ù]
›Ø—ÚY
HYˆÙ[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™H[ÙH›Û™BˆYˆšYÙÙ\ˆ\È›Ý›Û™N‚ˆ™]\›ˆÙ[‹—Ú[™WÛØØ[ÝšYÙÙ\—ØXÝ[ÛŠ™\]Y\ÝXÝ[Û‹šYÙÙ\ŠB‚ˆYˆÙ[‹˜Ü›Û—ÜÙ\šXÙH\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠ˜]]ÛX][Ûˆ›Ý›Ý[™ŠBˆ›ØˆHÙ[‹˜Ü›Û—ÜÙ\šXÙK™Ù]Ú›ØŠ›Ø—ÚY
BˆYˆ›Øˆ\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠ˜]]ÛX][Ûˆ›Ý›Ý[™ŠBˆYˆ›Ø‹œ^[ØYšÚ[™OHœÞ\Ý[WÙ]™[Ž‚ˆ™]\›ˆÚÙ\œ›ÜŠËœÞ\Ý[H]]ÛX][Ûˆ\È›ÝXÝYŠBˆYˆXÝ[Ûˆ[ˆÈ™[˜X›H‹œ[ˆŸH[™›Ý\×Ø›Ý[™ØÜ›Û—Ú›ØŠ›ØŠN‚ˆ™]\›ˆÚÙ\œ›ÜŠK˜]]ÛX][Ûˆ\È›È[šÙYÚ]ŠB‚ˆYˆXÝ[ÛˆOH™[˜X›HŽ‚ˆYˆÙ[‹˜Ü›Û—ÜÙ\šXÙK™[˜X›WÚ›ØŠ›Ø—ÚY[˜X›YUYJH\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠ˜]]ÛX][Ûˆ›Ý›Ý[™ŠBˆ[YˆXÝ[ÛˆOH™\ØX›HŽ‚ˆYˆÙ[‹˜Ü›Û—ÜÙ\šXÙK™[˜X›WÚ›ØŠ›Ø—ÚY[˜X›YQ˜[ÙJH\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠ˜]]ÛX][Ûˆ›Ý›Ý[™ŠBˆ[YˆXÝ[ÛˆOH™[]HŽ‚ˆ™\Ý[HÙ[‹˜Ü›Û—ÜÙ\šXÙKœ™[[Ý™WÚ›ØŠ›Ø—ÚY
BˆYˆ™\Ý[OH››ÝÙ›Ý[™Ž‚ˆ™]\›ˆÚÙ\œ›ÜŠ˜]]ÛX][Ûˆ›Ý›Ý[™ŠBˆYˆ™\Ý[OHœ›ÝXÝYŽ‚ˆ™]\›ˆÚÙ\œ›ÜŠËœÞ\Ý[H]]ÛX][Ûˆ\È›ÝXÝYŠBˆ[YˆXÝ[ÛˆOHœ[ˆŽ‚ˆYˆ›Ý›Ø‹™[˜X›Y‚ˆ™]\›ˆÚÙ\œ›ÜŠK˜]]ÛX][Ûˆ\È\ØX›YŠBˆ\ÚÈH\Þ[˜Ú[Ë˜Ü™X]WÝ\ÚÊÙ[‹˜Ü›Û—ÜÙ\šXÙKœ[—Ú›ØŠ›Ø—ÚY›Ü˜ÙOQ˜[ÙJJBˆ\ÚË˜YÙÛ™WØØ[˜XÚÊÙ[‹—ÛÙ×Ø]]ÛX][Û—Ü[—Ü™\Ý[
Bˆ[YˆXÝ[ÛˆOH\]HŽ‚ˆ˜[Y\ÈHØ]]ÛX][Û—Ý˜[Y\×Ùœ›ÛWÜ™\]Y\Ý
™\]Y\Ý
BˆYˆ˜[Y\È\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠš[˜[Y]]ÛX][Ûˆ\]H^[ØYŠBˆ\œÙYHÜ\œÙWØ]]ÛX][Û—Ý\]J˜[Y\ËÝ\œ™[Ú›ØZ›ØŠBˆYˆ\Ú[œÝ[˜ÙJ\œÙYÝŠN‚ˆ™]\›ˆÚÙ\œ›ÜŠ\œÙY
BˆžN‚ˆ™\Ý[HÙ[‹˜Ü›Û—ÜÙ\šXÙK\]WÚ›ØŠ›Ø—ÚY
Šœ\œÙY
Bˆ^Ù\˜[YQ\œ›Üˆ\È^Î‚ˆ™]\›ˆÚÙ\œ›ÜŠÝŠ^ÊJBˆYˆ™\Ý[OH››ÝÙ›Ý[™Ž‚ˆ™]\›ˆÚÙ\œ›ÜŠ˜]]ÛX][Ûˆ›Ý›Ý[™ŠBˆYˆ™\Ý[OHœ›ÝXÝYŽ‚ˆ™]\›ˆÚÙ\œ›ÜŠËœÞ\Ý[H]]ÛX][Ûˆ\È›ÝXÝYŠBˆ[ÙN‚ˆ™]\›ˆÚÙ\œ›ÜŠ[šÛ›ÝÛˆ]]ÛX][ÛˆXÝ[ÛˆŠB‚ˆ™]\›ˆÙ[‹—Ú[™WÝÙXZWØ]]ÛX][ÛœÊ™\]Y\Ý
B‚ˆYˆÚ[™WÛØØ[ÝšYÙÙ\—ØXÝ[ÛŠˆÙ[‹ˆ™\]Y\ÝˆÜÔ™\]Y\ÝˆXÝ[ÛŽˆÝ‹ˆšYÙÙ\ŽˆØØ[šYÙÙ\‹ˆ
HOˆ™\ÜÛœÙN‚ˆYˆÙ[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™H\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠLËšYÙÙ\ˆÙ\šXÙH[˜]˜Z[X›HŠBˆYˆXÝ[ÛˆOH™[˜X›HŽ‚ˆYˆÙ[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™K™[˜X›JšYÙÙ\‹šY[˜X›YUYJH\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠ˜]]ÛX][Ûˆ›Ý›Ý[™ŠBˆ[YˆXÝ[ÛˆOH™\ØX›HŽ‚ˆYˆÙ[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™K™[˜X›JšYÙÙ\‹šY[˜X›YQ˜[ÙJH\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠ˜]]ÛX][Ûˆ›Ý›Ý[™ŠBˆ[YˆXÝ[ÛˆOH™[]HŽ‚ˆYˆ›ÝÙ[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™K™[]JšYÙÙ\‹šY
N‚ˆ™]\›ˆÚÙ\œ›ÜŠ˜]]ÛX][Ûˆ›Ý›Ý[™ŠBˆ[YˆXÝ[ÛˆOHœ[ˆŽ‚ˆ™]\›ˆÚÙ\œ›ÜŠK›ØØ[šYÙÙ\ˆ™\]Z\™\ÈHÓHY\ÜØYÙHŠBˆ[YˆXÝ[ÛˆOH\]HŽ‚ˆ˜[Y\ÈHØ]]ÛX][Û—Ý˜[Y\×Ùœ›ÛWÜ™\]Y\Ý
™\]Y\Ý
BˆYˆ˜[Y\È\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠš[˜[Y]]ÛX][Ûˆ\]H^[ØYŠBˆ\œÙYHÜ\œÙWÛØØ[ÝšYÙÙ\—Ý\]J˜[Y\ÊBˆYˆ\Ú[œÝ[˜ÙJ\œÙYÝŠN‚ˆ™]\›ˆÚÙ\œ›ÜŠ\œÙY
BˆYˆ\œÙY‚ˆYˆÙ[‹›ØØ[ÝšYÙÙ\—ÜÝÜ™K\]JšYÙÙ\‹šY
Šœ\œÙY
H\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠ˜]]ÛX][Ûˆ›Ý›Ý[™ŠBˆ[ÙN‚ˆ™]\›ˆÚÙ\œ›ÜŠ[šÛ›ÝÛˆ]]ÛX][ÛˆXÝ[ÛˆŠB‚ˆ™]\›ˆÙ[‹—Ú[™WÝÙXZWØ]]ÛX][ÛœÊ™\]Y\Ý
B‚ˆÝ]XÛY]ÙˆYˆÛÙ×Ø]]ÛX][Û—Ü[—Ü™\Ý[
\ÚÎˆ\Þ[˜Ú[Ë•\ÚÖØ›ÛÛJHOˆ›Û™N‚ˆžN‚ˆ˜[ˆH\ÚËœ™\Ý[

Bˆ^Ù\^Ù\[ÛŽ‚ˆÙÙÙ\‹™^Ù\[ÛŠ•ÙX•RH]]ÛX][Ûˆ[‹[›ÝÈ\ÚÈ˜Z[YŠBˆ™]\›‚ˆYˆ›Ý˜[Ž‚ˆÙÙÙ\‹Ø\›š[™Ê•ÙX•RH]]ÛX][Ûˆ[‹[›ÝÈ\ÚÈY›Ý^XÝ]HŠB‚ˆÈKHYYXH›Ý]\ÈKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKB‚ˆYˆÙ\Ü]ÚÛYYXWÜ›Ý]\ÊÙ[‹™\]Y\ÝˆÜÔ™\]Y\ÝÛÝˆÝŠHOˆ™\ÜÛœÙH›Û™N‚ˆHH™K›X]Ú
ˆ—‹Ø\KÛYYXKÊÐKV˜K^ŒNWËWJÊKÊÐKV˜K^ŒNWËWJÊI‹ÛÝ
BˆYˆN‚ˆ™]\›ˆÙ[‹—Ú[™WÛYYXWÙ™]Ú
K™Ü›Ý\
JKK™Ü›Ý\
ŠK™\]Y\Ý
Bˆ™]\›ˆ›Û™B‚ˆYˆÚ[™WÛYYXWÙ™]Ú
ˆÙ[‹ÚYÎˆÝ‹^[ØYˆÝ‹™\]Y\ÝˆÜÔ™\]Y\Ý›Û™HH›Û™Bˆ
HOˆ™\ÜÛœÙN‚ˆ™]\›ˆÙ[‹›YYXKœÙ\™WÜÚYÛ™YÛYYXJˆÚYËˆ^[ØYˆ™\]Y\Ý\™\]Y\Ýˆ
B‚ˆÈKHZ\ØÈ›Ý]\ÈKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKB‚ˆ\Þ[˜ÈYˆÙ\Ü]ÚÛZ\Ø×Ü›Ý]\ÊˆÙ[‹ÛÛ›™XÝ[ÛŽˆ[žK™\]Y\ÝˆÜÔ™\]Y\ÝÛÝˆÝ‚ˆ
HOˆ™\ÜÛœÙH›Û™N‚ˆYˆÛÝOH‹Ø\KÜÙ\ÜÚ[ÛœÈŽ‚ˆ™]\›ˆ]ØZ]Ù[‹—Ú[™WÜÙ\ÜÚ[Ûœ×Û\Ý
™\]Y\Ý
BˆYˆÛÝOH‹Ø\KØÛÛ[X[™ÈŽ‚ˆ™]\›ˆÙ[‹—Ú[™WØÛÛ[X[™Ê™\]Y\Ý
BˆYˆÛÝOH‹Ø\KÝÛÜšÜÜXÙ\ËÜXÚËY›Û\ˆŽ‚ˆ™]\›ˆ]ØZ]Ù[‹—Ú[™WÝÛÜšÜÜXÙWÙ›Û\—ÜXÚÙ\ŠÛÛ›™XÝ[Û‹™\]Y\Ý
BˆYˆÛÝOH‹Ø\KÝÛÜšÜÜXÙ\ÈŽ‚ˆ™]\›ˆÙ[‹—Ú[™WÝÛÜšÜÜXÙ\ÊÛÛ›™XÝ[Û‹™\]Y\Ý
BˆYˆÛÝOH‹Ø\KÝÙXZKÜÚÚ[ËÜÙX\˜ÚŽ‚ˆ™]\›ˆ]ØZ]Ù[‹—Ú[™WÝÙXZWÜÚÚ[×ÜÙX\˜Ú
™\]Y\Ý
BˆYˆÛÝOH‹Ø\KÝÙXZKÜÚÚ[ËÝ™[™[™ÈŽ‚ˆ™]\›ˆ]ØZ]Ù[‹—Ú[™WÝÙXZWÜÚÚ[×Ý™[™[™Ê™\]Y\Ý
BˆYˆÛÝOH‹Ø\KÝÙXZKÜÚÚ[ËÝ™[™ÈŽ‚ˆ™]\›ˆ]ØZ]Ù[‹—Ú[™WÝÙXZWÜÚÚ[Ý™[™Ê™\]Y\Ý
BˆYˆÛÝOH‹Ø\KÝÙXZKÜÚÚ[ËÚ[œÝ[Ž‚ˆ™]\›ˆ]ØZ]Ù[‹—Ú[™WÝÙXZWÜÚÚ[Ú[œÝ[
ÛÛ›™XÝ[Û‹™\]Y\Ý
BˆYˆÛÝOH‹Ø\KÝÙXZKÜÚÚ[ËÝ\]HŽ‚ˆ™]\›ˆÙ[‹—Ú[™WÝÙXZWÜÚÚ[Ý\]J™\]Y\Ý
BˆYˆÛÝOH‹Ø\KÝÙXZKÜÚÚ[ËÙ[]HŽ‚ˆ™]\›ˆÙ[‹—Ú[™WÝÙXZWÜÚÚ[Ù[]JÛÛ›™XÝ[Û‹™\]Y\Ý
BˆYˆÛÝOH‹Ø\KÝÙXZKÜÚÚ[ÈŽ‚ˆ™]\›ˆÙ[‹—Ú[™WÝÙXZWÜÚÚ[Ê™\]Y\Ý
BˆHH™K›X]Ú
ˆ—‹Ø\KÝÙXZKÜÚÚ[ËÊ×‹×JI‹ÛÝ
BˆYˆN‚ˆ™]\›ˆÙ[‹—Ú[™WÝÙXZWÜÚÚ[Ù]Z[
™\]Y\ÝK™Ü›Ý\
JJBˆYˆÛÝOH‹Ø\KÝÙXZKÜÚYX˜\‹\Ý]HŽ‚ˆ™]\›ˆÙ[‹—Ú[™WÝÙXZWÜÚYX˜\—ÜÝ]J™\]Y\Ý
BˆYˆÛÝOH‹Ø\KÝÙXZKÜÚYX˜\‹\Ý]KÝ\]HŽ‚ˆ™]\›ˆÙ[‹—Ú[™WÝÙXZWÜÚYX˜\—ÜÝ]WÝ\]J™\]Y\Ý
Bˆ™]\›ˆ›Û™B‚ˆYˆÚ[™WØÛÛ[X[™ÊÙ[‹™\]Y\ÝˆÜÔ™\]Y\Ý
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJÈ˜ÛÛ[X[™ÈŽˆZ[[—ØÛÛ[X[™Ü[]J
_JB‚ˆYˆÚ[™WÝÛÜšÜÜXÙ\ÊÙ[‹ÛÛ›™XÝ[ÛŽˆ[žK™\]Y\ÝˆÜÔ™\]Y\Ý
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJˆÙ[‹ÛÜšÜÜXÙ\Ëœ^[ØY
ˆÛÛ›Û×Ø]˜Z[X›O\Ù[‹ÛÜšÜÜXÙWØÛÛ›Û×Ø]˜Z[X›JÛÛ›™XÝ[ÛŠKˆ›Û\—ÜXÚÙ\—Ø]˜Z[X›O\Ù[‹ÛÜšÜÜXÙWÙ›Û\—ÜXÚÙ\—Ø]˜Z[X›JˆÛÛ›™XÝ[Û‹ˆ™\]Y\Ýˆ
Kˆ
Bˆ
B‚ˆ\Þ[˜ÈYˆÚ[™WÝÛÜšÜÜXÙWÙ›Û\—ÜXÚÙ\ŠˆÙ[‹ˆÛÛ›™XÝ[ÛŽˆ[žKˆ™\]Y\ÝˆÜÔ™\]Y\Ýˆ
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆYˆ›ÝÙ[‹ÛÜšÜÜXÙWÙ›Û\—ÜXÚÙ\—Ø]˜Z[X›JÛÛ›™XÝ[Û‹™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠË›˜]]™H›Û\ˆXÚÙ\ˆ\È[˜]˜Z[X›H›Üˆ\ÈÛÛ›™XÝ[ÛˆŠBˆYˆÙ[‹—Ù›Û\—ÜXÚÙ\—ÛØÚË›ØÚÙY

N‚ˆ™]\›ˆÚÙ\œ›ÜŠK›˜]]™H›Û\ˆXÚÙ\ˆ\È[™XYHÜ[ˆŠBˆžN‚ˆ\Þ[˜ÈÚ]Ù[‹—Ù›Û\—ÜXÚÙ\—ÛØÚÎ‚ˆ]H]ØZ]XÚ×Û˜]]™WÙ›Û\Š
Bˆ^Ù\˜]]™Q›Û\”XÚÙ\‘\œ›Üˆ\È^Î‚ˆ™]\›ˆÚÙ\œ›ÜŠLËÝŠ^ÊJBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJÈœ]Žˆ]JB‚ˆYˆÚ[™WÝÙXZWÜÚÚ[ÊÙ[‹™\]Y\ÝˆÜÔ™\]Y\Ý
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJˆÙXZWÜÚÚ[×Ü^[ØY
ˆÙ[‹œÚÚ[×ÝÛÜšÜÜXÙWÜ]ˆ\ØX›YÜÚÚ[Ï\Ù[‹™\ØX›YÜÚÚ[Ëˆ
Bˆ
B‚ˆ\Þ[˜ÈYˆÚ[™WÝÙXZWÜÚÚ[×ÜÙX\˜Ú
Ù[‹™\]Y\ÝˆÜÔ™\]Y\Ý
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆ\˜[\ÈHÜ\œÙWÜ]Y\žJ™\]Y\Ýœ]
Bˆ]Y\žHHÜ]Y\žWÙš\œÝ
\˜[\ËœHŠHÜˆˆ‚ˆ›ÝšY\ˆHÜ]Y\žWÙš\œÝ
\˜[\Ëœ›ÝšY\ˆŠHÜˆ˜[‚ˆžN‚ˆ^[ØYH]ØZ]ÙX\˜ÚÛX\šÙ]XÙWÜÚÚ[Êˆ]Y\žKˆÙ[‹œÚÚ[×ÝÛÜšÜÜXÙWÜ]ˆ›ÝšY\\›ÝšY\‹ˆ
Bˆ^Ù\ÚÚ[ÓX\šÙ]XÙQ\œ›Üˆ\È^Î‚ˆ™]\›ˆÚÙ\œ›ÜŠ^ËœÝ]\Ë^Ë›Y\ÜØYÙJBˆ^Ù\^Ù\[ÛŽ‚ˆÙ[‹—ÛÙË™^Ù\[ÛŠœÚÚ[ÈX\šÙ]XÙHÙX\˜Ú˜Z[YŠBˆ™]\›ˆÚÙ\œ›ÜŠLœÚÚ[ÈX\šÙ]XÙHÙX\˜Ú˜Z[YŠBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJ^[ØY
B‚ˆ\Þ[˜ÈYˆÚ[™WÝÙXZWÜÚÚ[×Ý™[™[™ÊÙ[‹™\]Y\ÝˆÜÔ™\]Y\Ý
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆ›ÝšY\ˆHÜ]Y\žWÙš\œÝ
Ü\œÙWÜ]Y\žJ™\]Y\Ýœ]
Kœ›ÝšY\ˆŠHÜˆ˜[‚ˆžN‚ˆ^[ØYH]ØZ]™[™[™×ÛX\šÙ]XÙWÜÚÚ[ÊˆÙ[‹œÚÚ[×ÝÛÜšÜÜXÙWÜ]ˆ›ÝšY\\›ÝšY\‹ˆ
Bˆ^Ù\ÚÚ[ÓX\šÙ]XÙQ\œ›Üˆ\È^Î‚ˆ™]\›ˆÚÙ\œ›ÜŠ^ËœÝ]\Ë^Ë›Y\ÜØYÙJBˆ^Ù\^Ù\[ÛŽ‚ˆÙ[‹—ÛÙË™^Ù\[ÛŠœÚÚ[ÈX\šÙ]XÙH™[™[™ÈÛÚÝ\˜Z[YŠBˆ™]\›ˆÚÙ\œ›ÜŠLœÚÚ[ÈX\šÙ]XÙH™[™[™ÈÛÚÝ\˜Z[YŠBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJ^[ØY
B‚ˆ\Þ[˜ÈYˆÚ[™WÝÙXZWÜÚÚ[Ý™[™ÊÙ[‹™\]Y\ÝˆÜÔ™\]Y\Ý
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆÚÚ[ÚYÈHÜ\œÙWÜ]Y\žJ™\]Y\Ýœ]
K™Ù]
šY‹×JBˆžN‚ˆ^[ØYH]ØZ]X\šÙ]XÙWÜÚÚ[Ý™[™ÊÚÚ[ÚYÊBˆ^Ù\^Ù\[ÛŽ‚ˆÙ[‹—ÛÙË™^Ù\[ÛŠœÚÚ[ËœÚ™[™\ÝÜžHÛÚÝ\˜Z[YŠBˆ™]\›ˆÚÙ\œ›ÜŠLœÚÚ[ËœÚ™[™\ÝÜžHÛÚÝ\˜Z[YŠBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJ^[ØY
B‚ˆ\Þ[˜ÈYˆÚ[™WÝÙXZWÜÚÚ[Ú[œÝ[
ˆÙ[‹ˆÛÛ›™XÝ[ÛŽˆ[žKˆ™\]Y\ÝˆÜÔ™\]Y\Ýˆ
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆYˆ›ÝÙ[‹—Ø[Ý×ÝÙXZWÜXÚØYÙWÚ[œÝ[
ÛÛ›™XÝ[Û‹™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠËœ™[[ÝHÚÚ[[œÝ[][Ûˆ\È\ØX›YŠBˆYˆÙ[‹—ÜÚÚ[Ú[œÝ[ÛØÚË›ØÚÙY

N‚ˆ™]\›ˆÚÙ\œ›ÜŠK˜[›Ý\ˆÚÚ[[œÝ[][Ûˆ\È[™XYH[ˆ›ÙÜ™\ÜÈŠB‚ˆ]Y\žHHÜ™\]Y\ÝÜ]Y\žJ™\]Y\Ý
Bˆ›ÝšY\ˆHÜ]Y\žWÙš\œÝ
]Y\žKœ›ÝšY\ˆŠHÜˆœÚÚ[×ÜÚ‚ˆÛÝ\˜ÙHHÜ]Y\žWÙš\œÝ
]Y\žKœÛÝ\˜ÙHŠHÜˆˆ‚ˆÚÚ[ÚYHÜ]Y\žWÙš\œÝ
]Y\žKœÚÚ[ŠHÜˆˆ‚ˆ™\œÚ[ÛˆHÜ]Y\žWÙš\œÝ
]Y\žK™\œÚ[ÛˆŠHÜˆˆ‚ˆ\Þ[˜ÈÚ]Ù[‹—ÜÚÚ[Ú[œÝ[ÛØÚÎ‚ˆžN‚ˆXÝ[ÛˆH]ØZ][œÝ[ÛX\šÙ]XÙWÜÚÚ[
ˆÛÝ\˜ÙKˆÚÚ[ÚYˆÙ[‹œÚÚ[×ÝÛÜšÜÜXÙWÜ]ˆ›ÝšY\\›ÝšY\‹ˆ™\œÚ[Û]™\œÚ[Û‹ˆ
Bˆ^Ù\ÚÚ[ÓX\šÙ]XÙQ\œ›Üˆ\È^Î‚ˆ™]\›ˆÚÙ\œ›ÜŠ^ËœÝ]\Ë^Ë›Y\ÜØYÙJBˆ^Ù\^Ù\[ÛŽ‚ˆÙ[‹—ÛÙË™^Ù\[ÛŠœÚÚ[[œÝ[][Ûˆ˜Z[YŠBˆ™]\›ˆÚÙ\œ›ÜŠLœÚÚ[[œÝ[][Ûˆ˜Z[YŠBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJÂˆ
ŠÙXZWÜÚÚ[×Ü^[ØY
ˆÙ[‹œÚÚ[×ÝÛÜšÜÜXÙWÜ]ˆ\ØX›YÜÚÚ[Ï\Ù[‹™\ØX›YÜÚÚ[Ëˆ
Kˆ›\ÝØXÝ[ÛˆŽˆXÝ[Û‹ˆJB‚ˆYˆØ[Ý×ÝÙXZWÜXÚØYÙWÚ[œÝ[
Ù[‹ÛÛ›™XÝ[ÛŽˆ[žK™\]Y\ÝˆÜÔ™\]Y\Ý
HOˆ›ÛÛ‚ˆYˆÚ\×ÛØØ[Øœ›ÝÜÙ\—Ü™\]Y\Ý
ÛÛ›™XÝ[Û‹™\]Y\ÝšXY\œÊN‚ˆ™]\›ˆYBˆžN‚ˆ™]\›ˆ›ÛÛ
ˆÙ[‹œÙ][™ÜË˜ÛÛ™šYË›ØY

KÛÛËÙXZWØ[Ý×Ü™[[ÝWÜXÚØYÙWÚ[œÝ[ˆ
Bˆ^Ù\^Ù\[ÛŽ‚ˆÙ[‹—ÛÙË™^Ù\[ÛŠ™˜Z[YÈØY™[[ÝHXÚØYÙH[œÝ[ÛXÞHŠBˆ™]\›ˆ˜[ÙB‚ˆYˆÚ[™WÝÙXZWÜÚÚ[Ý\]JÙ[‹™\]Y\ÝˆÜÔ™\]Y\Ý
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆ]Y\žHHÜ™\]Y\ÝÜ]Y\žJ™\]Y\Ý
Bˆ˜[YHHÜ]Y\žWÙš\œÝ
]Y\žK›˜[YHŠHÜˆˆ‚ˆ˜]×Ù[˜X›YH
Ü]Y\žWÙš\œÝ
]Y\žK™[˜X›YŠHÜˆˆŠK›ÝÙ\Š
BˆYˆ˜]×Ù[˜X›Y›Ý[ˆÈYH‹™˜[ÙHŸN‚ˆ™]\›ˆÚÙ\œ›ÜŠ™[˜X›Y]\Ý™HYHÜˆ˜[ÙHŠBˆžN‚ˆXÝ[ÛˆHÙ[‹œÙ][™ÜË˜ÛÛ™šYËœ[—ÜÙ\šX[^™Y
ˆ[X™HÛÛ™šY×Ü]ˆÙ]ÝÙXZWÜÚÚ[Ù[˜X›Y
ˆÙ[‹œÚÚ[×ÝÛÜšÜÜXÙWÜ]ˆ˜[YKˆ[˜X›Y\˜]×Ù[˜X›YOHYH‹ˆ\ØX›YÜÚÚ[Ï\Ù[‹™\ØX›YÜÚÚ[ËˆÛÛ™šY×Ü]XÛÛ™šY×Ü]ˆ
Bˆ
Bˆ^Ù\ÚÚ[X[˜YÙ[Y[\œ›Üˆ\È^Î‚ˆ™]\›ˆÚÙ\œ›ÜŠ^ËœÝ]\Ë^Ë›Y\ÜØYÙJBˆÙ[‹—Ø\WÜÚÚ[ÜÝ]J
Bˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJÂˆ
ŠÙXZWÜÚÚ[×Ü^[ØY
ˆÙ[‹œÚÚ[×ÝÛÜšÜÜXÙWÜ]ˆ\ØX›YÜÚÚ[Ï\Ù[‹™\ØX›YÜÚÚ[Ëˆ
Kˆ›\ÝØXÝ[ÛˆŽˆXÝ[Û‹ˆJB‚ˆYˆÚ[™WÝÙXZWÜÚÚ[Ù[]JˆÙ[‹ˆÛÛ›™XÝ[ÛŽˆ[žKˆ™\]Y\ÝˆÜÔ™\]Y\Ýˆ
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆYˆ›ÝÚ\×ÛØØ[Øœ›ÝÜÙ\—Ü™\]Y\Ý
ÛÛ›™XÝ[Û‹™\]Y\ÝšXY\œÊN‚ˆ™]\›ˆÚÙ\œ›ÜŠËœ™[[ÝHÚÚ[[][Ûˆ\È\ØX›YŠBˆ˜[YHHÜ]Y\žWÙš\œÝ
Ü™\]Y\ÝÜ]Y\žJ™\]Y\Ý
K›˜[YHŠHÜˆˆ‚ˆžN‚ˆXÝ[ÛˆHÙ[‹œÙ][™ÜË˜ÛÛ™šYËœ[—ÜÙ\šX[^™Y
ˆ[X™HÛÛ™šY×Ü]ˆ[]WÝÙXZWÜÚÚ[
ˆÙ[‹œÚÚ[×ÝÛÜšÜÜXÙWÜ]ˆ˜[YKˆ\ØX›YÜÚÚ[Ï\Ù[‹™\ØX›YÜÚÚ[ËˆÛÛ™šY×Ü]XÛÛ™šY×Ü]ˆ
Bˆ
Bˆ^Ù\ÚÚ[X[˜YÙ[Y[\œ›Üˆ\È^Î‚ˆ™]\›ˆÚÙ\œ›ÜŠ^ËœÝ]\Ë^Ë›Y\ÜØYÙJBˆÙ[‹—Ø\WÜÚÚ[ÜÝ]J
Bˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJÂˆ
ŠÙXZWÜÚÚ[×Ü^[ØY
ˆÙ[‹œÚÚ[×ÝÛÜšÜÜXÙWÜ]ˆ\ØX›YÜÚÚ[Ï\Ù[‹™\ØX›YÜÚÚ[Ëˆ
Kˆ›\ÝØXÝ[ÛˆŽˆXÝ[Û‹ˆJB‚ˆYˆØ\WÜÚÚ[ÜÝ]JÙ[ŠHOˆ›Û™N‚ˆYˆÙ[‹œÚÚ[ÜÝ]WØXÝ[Ûˆ\È›Ý›Û™N‚ˆÙ[‹œÚÚ[ÜÝ]WØXÝ[ÛŠÙ]
Ù[‹™\ØX›YÜÚÚ[ÊJB‚ˆYˆÚ[™WÝÙXZWÜÚÚ[Ù]Z[
Ù[‹™\]Y\ÝˆÜÔ™\]Y\Ý˜]×Û˜[YNˆÝŠHOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆœ›ÛH\›X‹œ\œÙH[\Ü[œ][ÝB‚ˆ˜[YHH[œ][ÝJ˜]×Û˜[YJBˆYˆ›Ý˜[YHÜˆ‹Èˆ[ˆ˜[YHÜˆ—ˆ[ˆ˜[YN‚ˆ™]\›ˆÚÙ\œ›ÜŠš[˜[YÚÚ[˜[YHŠBˆ^[ØYHÙXZWÜÚÚ[Ù]Z[Ü^[ØY
ˆÙ[‹œÚÚ[×ÝÛÜšÜÜXÙWÜ]ˆ˜[YKˆ\ØX›YÜÚÚ[Ï\Ù[‹™\ØX›YÜÚÚ[Ëˆ
BˆYˆ^[ØY\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠœÚÚ[›Ý›Ý[™ŠBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJ^[ØY
B‚ˆYˆÚ[™WÝÙXZWÜÚYX˜\—ÜÝ]JÙ[‹™\]Y\ÝˆÜÔ™\]Y\Ý
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJ™XYÝÙXZWÜÚYX˜\—ÜÝ]J
JB‚ˆYˆÚ[™WÝÙXZWÜÚYX˜\—ÜÝ]WÝ\]JÙ[‹™\]Y\ÝˆÜÔ™\]Y\Ý
HOˆ™\ÜÛœÙN‚ˆYˆ›ÝÙ[‹˜ÚXÚ×Ø\WÝÚÙ[Š™\]Y\Ý
N‚ˆ™]\›ˆÚÙ\œ›ÜŠK•[˜]]Üš^™YŠBˆ^[ØYHÛ]]][Û—Ü^[ØY
™\]Y\Ý
BˆÝ]WÝ˜[YHH^[ØY™Ù]
œÝ]HŠHYˆ^[ØY\È›Ý›Û™H[ÙH›Û™BˆYˆÝ]WÝ˜[YH\È›Û™N‚ˆ™]\›ˆÚÙ\œ›ÜŠ›Z\ÜÚ[™ÈÝ]HŠBˆYˆ›Ý\Ú[œÝ[˜ÙJÝ]WÝ˜[YKXÝ
N‚ˆ™]\›ˆÚÙ\œ›ÜŠœÝ]H]\Ý™H[ˆØš™XÝŠBˆžN‚ˆÝ]HHÜš]WÝÙXZWÜÚYX˜\—ÜÝ]JØ\Ý
XÝÜÝ‹[žWWKÝ]WÝ˜[YJJBˆ^Ù\˜[YQ\œ›Üˆ\ÈN‚ˆ™]\›ˆÚÙ\œ›ÜŠÝŠJJBˆ^Ù\ÔÑ\œ›ÜŽ‚ˆÙ[‹—ÛÙË™^Ù\[ÛŠ™˜Z[YÈÜš]HÙXZHÚYX˜\ˆÝ]HŠBˆ™]\›ˆÚÙ\œ›ÜŠL™˜Z[YÈÜš]HÚYX˜\ˆÝ]HŠBˆ™]\›ˆÚÚœÛÛ—Ü™\ÜÛœÙJÝ]JB‚ˆÈKHÝ]XÈš[HÙ\š[™ÈKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKB‚ˆYˆÜÙ\™WÜÝ]XÊˆÙ[‹ˆ™\]Y\ÝÜ]ˆÝ‹ˆ
‹ˆXØÙ\Ù[˜ÛÙ[™ÎˆÝˆHˆ‹ˆ
HOˆ™\ÜÛœÙH›Û™N‚ˆ\ÜÙ\Ù[‹œÝ]X×Ù\ÝÜ]\È›Ý›Û™Bˆ™[H™\]Y\ÝÜ]›Ýš\
‹ÈŠBˆYˆ›Ý™[‚ˆ™[Hš[™^š[‚ˆYˆ‹‹ˆˆ[ˆ™[œÜ]
‹ÈŠHÜˆ™[œÝ\ÝÚ]
‹ÈŠN‚ˆ™]\›ˆÚÙ\œ›ÜŠË‘›Ü˜šY[ˆŠBˆØ[™Y]HH
Ù[‹œÝ]X×Ù\ÝÜ]È™[
Kœ™\ÛÛ™J
BˆžN‚ˆØ[™Y]Kœ™[]]™WÝÊÙ[‹œÝ]X×Ù\ÝÜ]
Bˆ^Ù\˜[YQ\œ›ÜŽ‚ˆ™]\›ˆÚÙ\œ›ÜŠË‘›Ü˜šY[ˆŠBˆYˆ›ÝØ[™Y]Kš\×Ùš[J
N‚ˆ[™^HÙ[‹œÝ]X×Ù\ÝÜ]Èš[™^š[‚ˆYˆ[™^š\×Ùš[J
N‚ˆØ[™Y]HH[™^ˆ[ÙN‚ˆ™]\›ˆ›Û™BˆÝ\KÈHZ[Y]\\Ë™ÝY\Ü×Ý\JØ[™Y]K›˜[YJBˆYˆÝ\H\È›Û™N‚ˆÝ\HH˜\XØ][Û‹ÛØÝ]\Ý™X[H‚ˆ]ŽÝ^HÝ\KœÝ\ÝÚ]
^ÈŠHÜˆÝ\H[ˆÂˆ˜\XØ][Û‹Ú˜]˜\ØÜš\‹ˆ˜\XØ][Û‹ÚœÛÛˆ‹ˆBˆÛÛ\™\ÜÚX›HH]ŽÝ^ÜˆÝ\HOHš[XYÙKÜÝ™ÊÞ[‚ˆ™\ÜÛœÙWÜ]HØ[™Y]Bˆ^˜WÚXY\œÎˆ\ÝÝ\VÜÝ‹Ý—WWHH×BˆYˆÛÛ\™\ÜÚX›N‚ˆ^˜WÚXY\œË˜\[™

•˜\žH‹XØÙ\Q[˜ÛÙ[™ÈŠJBˆÞš\ØØ[™Y]HHØ[™Y]KÚ]Û˜[YJˆžØØ[™Y]K›˜[Y_K™ÞˆŠBˆYˆØXØÙ\×ÙÞš\
XØÙ\Ù[˜ÛÙ[™ÊH[™Þš\ØØ[™Y]Kš\×Ùš[J
N‚ˆ™\ÜÛœÙWÜ]HÞš\ØØ[™Y]Bˆ^˜WÚXY\œË˜\[™
ÛÛ[Q[˜ÛÙ[™È‹™Þš\ŠBˆžN‚ˆ›ÙHH™\ÜÛœÙWÜ]œ™XYØž]\Ê
Bˆ^Ù\ÔÑ\œ›Üˆ\ÈN‚ˆÙ[‹—ÛÙËØ\›š[™ÊœÝ]XÎˆ˜Z[YÈ™XYßNˆßH‹™\ÜÛœÙWÜ]JBˆ™]\›ˆÚÙ\œ›ÜŠL’[\›˜[Ù\™\ˆ\œ›ÜˆŠBˆYˆ]ŽÝ^‚ˆÝ\HHˆžØÝ\_NÈÚ\œÙ]]]‹N‚ˆYˆØ[™Y]K›˜[YHOHš[™^š[Ž‚ˆØXÚHH››ËXØXÚH‚ˆ[ÙN‚ˆØXÚHHœX›XËX^XYÙOLÌMLÍŒ[[]]X›H‚ˆ™]\›ˆÚÜ™\ÜÛœÙJˆ›ÙKˆÝ]\ÏLŒˆÛÛ[Ý\OXÝ\Kˆ^˜WÚXY\œÏVÊØXÚKPÛÛ›Û‹ØXÚJK
™^˜WÚXY\œ×Kˆ
B‚‚™YˆØ]]ÛX][Û—Ý˜[Y\×Ùœ›ÛWÜ™\]Y\Ý
™\]Y\ÝˆÜÔ™\]Y\Ý
HOˆXÝÜÝ‹[žWH›Û™N‚ˆ^[ØYHÛ]]][Û—Ü^[ØY
™\]Y\Ý
BˆYˆ^[ØY\È›Û™HÜˆ˜[Y\Èˆ›Ý[ˆ^[ØY‚ˆ™]\›ˆßBˆ˜[Y\ÈH^[ØY™Ù]
˜[Y\ÈŠBˆ™]\›ˆØ\Ý
XÝÜÝ‹[žWWK˜[Y\ÊHYˆ\Ú[œÝ[˜ÙJ˜[Y\ËXÝ
H[ÙH›Û™B‚‚™YˆÜ\œÙWØ]]ÛX][Û—Ý\]Jˆ˜[Y\ÎˆXÝÜÝ‹[žWKˆ
‹ˆÝ\œ™[Ú›ØŽˆÜ›Û’›Øˆ›Û™HH›Û™KŠ@OˆXÝÜÝ‹[žWHÝŽ‚ˆ\]NˆXÝÜÝ‹[žWHHßBˆYˆ›˜[YHˆ[ˆ˜[Y\Î‚ˆ˜]×Û˜[YHH˜[Y\Ë™Ù]
›˜[YHŠBˆYˆ›Ý\Ú[œÝ[˜ÙJ˜]×Û˜[YKÝŠN‚ˆ™]\›ˆ›˜[YH]\Ý™HHÝš[™È‚ˆ˜[YHH˜]×Û˜[YKœÝš\

BˆYˆ›Ý˜[YN‚ˆ™]\›ˆ›˜[YHØ[››Ý™H[\H‚ˆ\]VÈ›˜[YH—HH˜[YBˆYˆ›Y\ÜØYÙHˆ[ˆ˜[Y\Î‚ˆ˜]×ÛY\ÜØYÙHH˜[Y\Ë™Ù]
›Y\ÜØYÙHŠBˆYˆ›Ý\Ú[œÝ[˜ÙJ˜]×ÛY\ÜØYÙKÝŠN‚ˆ™]\›ˆ›Y\ÜØYÙH]\Ý™HHÝš[™È‚ˆY\ÜØYÙHH˜]×ÛY\ÜØYÙKœÝš\

BˆYˆ›ÝY\ÜØYÙN‚ˆ™]\›ˆ›Y\ÜØYÙHØ[››Ý™H[\H‚ˆ\]VÈ›Y\ÜØYÙH—HHY\ÜØYÙBˆYˆœØÚY[Hˆ[ˆ˜[Y\Î‚ˆ˜]×ÜØÚY[HH˜[Y\Ë™Ù]
œØÚY[HŠBˆYˆ›Ý\Ú[œÝ[˜ÙJ˜]×ÜØÚY[KXÝ
N‚ˆ™]\›ˆœØÚY[H]\Ý™H[ˆØš™XÝ‚ˆ\œÙYÜØÚY[HHÜ\œÙWØ]]ÛX][Û—ÜØÚY[JØ\Ý
XÝÜÝ‹[žWK˜]×ÜØÚY[JJBˆYˆ\Ú[œÝ[˜ÙJ\œÙYÜØÚY[KÝŠN‚ˆ™]\›ˆ\œÙYÜØÚY[BˆYˆÝ\œ™[Ú›Øˆ\È›Ý›Û™H[™ÜØÚY[WÛX]Ú\×Ú›ØŠ\œÙYÜØÚY[KÝ\œ™[Ú›ØŠN‚ˆ™]\›ˆ\]BˆØÚY[WÙ\œ›ÜˆHÝ˜[Y]WØ]]ÛX][Û—ÜØÚY[J\œÙYÜØÚY[JBˆYˆØÚY[WÙ\œ›ÜŽ‚ˆ™]\›ˆØÚY[WÙ\œ›Ü‚ˆ\]VÈœØÚY[H—HH\œÙYÜØÚY[Bˆ\]VÈ™[]WØY\—Ü[ˆ—HH\œÙYÜØÚY[KšÚ[™OH˜]‚ˆ™]\›ˆ\]B‚‚™YˆÜ\œÙWÛØØ[ÝšYÙÙ\—Ý\]J˜[Y\ÎˆXÝÜÝ‹[žWWHOˆXÝÜÝ‹[žWHÝŽ‚ˆ\]NˆXÝÜÝ‹[žWHHßBˆYˆ›˜[YHˆ[ˆ˜[Y\Î‚ˆ˜]×Û˜[YHH˜[Y\Ë™Ù]
›˜[YHŠBˆYˆ›Ý\Ú[œÝ[˜ÙJ˜]×Û˜[YKÝŠN‚ˆ™]\›ˆ›˜[YH]\Ý™HHÝš[™È‚ˆ˜[YHH˜]×Û˜[YKœÝš\

BˆYˆ›Ý˜[YN‚ˆ™]\›ˆ›˜[YHØ[››Ý™H[\H‚ˆ\]VÈ›˜[YH—HH˜[YBˆ›Ü˜šY[ˆHÚÙ^H›ÜˆÙ^H[ˆ
›Y\ÜØYÙH‹œØÚY[HŠHYˆÙ^H[ˆ˜[Y\×BˆYˆ›Ü˜šY[Ž‚ˆ™]\›ˆ›ØØ[šYÙÙ\ˆ\]\ÈÛ›HÝ\Ü˜[YH‚ˆ™]\›ˆ\]B‚‚™YˆÜ\œÙWØ]]ÛX][Û—ÜØÚY[J˜[Y\ÎˆXÝÜÝ‹[žWJJHOˆÜ›Û”ØÚY[HÝŽ‚ˆ˜]×ÚÚ[™H˜[Y\Ë™Ù]
šÚ[™ŠBˆYˆ›Ý\Ú[œÝ[˜ÙJ˜]×ÚÚ[™ÝŠN‚ˆ™]\›ˆœØÚY[HÚ[™]\Ý™HHÝš[™È‚ˆÚ[™H˜]×ÚÚ[™œÝš\

BˆYˆÚ[™OH™]™\žHŽ‚ˆ]™\žWÛ\ÈHÜÜÚ]]™WÚ[
˜[Y\Ë™Ù]
™]™\žWÛ\ÈŠJBˆYˆ]™\žWÛ\È\È›Û™N‚ˆ™]\›ˆ™]™\žHØÚY[H™\]Z\™\ÈÜÚ]]™H]™\žWÛ\È‚ˆ™]\›ˆÜ›Û”ØÚY[JÚ[™H™]™\žH‹]™\žWÛ\ÏY]™\žWÛ\ÊBˆYˆÚ[™OH˜Ü›ÛˆŽ‚ˆ˜]×Ù^ˆH˜[Y\Ë™Ù]
™^ˆŠBˆYˆ›Ý\Ú[œÝ[˜ÙJ˜]×Ù^‹ÝŠN‚ˆ™]\›ˆ˜Ü›ÛˆØÚY[H™\]Z\™\È^ˆ‚ˆ^ˆH˜]×Ù^‹œÝš\

BˆYˆ›Ý^Ž‚ˆ™]\›ˆ˜Ü›ÛˆØÚY[H™\]Z\™\È^ˆ‚ˆ˜]×ÝˆH˜[Y\Ë™Ù]
ˆŠBˆYˆ˜]×Ýˆ\È›Ý›Û™H[™›Ý\Ú[œÝ[˜ÙJ˜]×Ý‹ÝŠN‚ˆ™]\›ˆ˜Ü›ÛˆØÚY[H[Y^›Û™H]\Ý™HHÝš[™È‚ˆˆH˜]×Ý‹œÝš\

HYˆ\Ú[œÝ[˜ÙJ˜]×Ý‹ÝŠH[ÙHˆ‚ˆ™]\›ˆÜ›Û”ØÚY[JÚ[™H˜Ü›Ûˆ‹^Y^‹]ˆÜˆ›Û™JBˆYˆÚ[™OH˜]Ž‚ˆ]Û\ÈHÜÜÚ]]™WÚ[
˜[Y\Ë™Ù]
˜]Û\ÈŠJBˆYˆ]Û\È\È›Û™N‚ˆ™]\›ˆ›Û™K][YHØÚY[H™\]Z\™\ÈÜÚ]]™H]Û\È‚ˆ™]\›ˆÜ›Û”ØÚY[JÚ[™H˜]‹]Û\ÏX]Û\ÊBˆ™]\›ˆ[šÛ›ÝÛˆØÚY[HÚ[™‚‚‚™YˆÜØÚY[WÛX]Ú\×Ú›ØŠØÚY[NˆÜ›Û”ØÚY[K›ØŽˆÜ›Û’›ØŠHOˆ›ÛÛ‚ˆÝ\œ™[H›Ø‹œØÚY[BˆYˆØÚY[KšÚ[™OHÝ\œ™[šÚ[™‚ˆ™]\›ˆ˜[ÙBˆYˆØÚY[KšÚ[™OH˜]Ž‚ˆ™]\›ˆØÚY[K˜]Û\ÈOHÝ\œ™[˜]Û\ÂˆYˆØÚY[KšÚ[™OH™]™\žHŽ‚ˆ™]\›ˆØÚY[K™]™\žWÛ\ÈOHÝ\œ™[™]™\žWÛ\ÂˆYˆØÚY[KšÚ[™OH˜Ü›ÛˆŽ‚ˆ™]\›ˆ
ØÚY[K™^ˆÜˆˆŠHOH
Ý\œ™[™^ˆÜˆˆŠH[™
ˆØÚY[KˆÜˆ›Û™Bˆ
HOH
Ý\œ™[ˆÜˆ›Û™JBˆ™]\›ˆ˜[ÙB‚‚™YˆÝ˜[Y]WØ]]ÛX][Û—ÜØÚY[JØÚY[NˆÜ›Û”ØÚY[JHOˆÝˆ›Û™N‚ˆYˆØÚY[KšÚ[™OH˜]Ž‚ˆYˆ›ÝØÚY[K˜]Û\ÈÜˆØÚY[K˜]Û\ÈH[
[YK[YJ
H
ˆL
N‚ˆ™]\›ˆ›Û™K][YHØÚY[H]\Ý™H[ˆH]\™H‚ˆ™]\›ˆ›Û™BˆYˆØÚY[KšÚ[™OH˜Ü›ÛˆŽ‚ˆ™]\›ˆ›Û™B‚ˆžN‚ˆœ›ÛH]][YH[\Ü]][YBˆœ›ÛH›Û™Z[™›È[\Ü›Û™R[™›Â‚ˆœ›ÛHÜ›Ûš]\ˆ[\ÜÜ›Ûš]\‚‚ˆˆH›Û™R[™›ÊØÚY[KŠHYˆØÚY[Kˆ[ÙH]][YK››ÝÊ
K˜\Ý[Y^›Û™J
Kš[™›Âˆ˜\ÙHH]][YK››ÝÊ]ŠBˆÜ›Ûš]\ŠØ\Ý
Ý‹ØÚY[K™^ŠK˜\ÙJK™Ù]Û™^
]][YJBˆ^Ù\^Ù\[ÛŽ‚ˆ™]\›ˆ˜Ü›ÛˆØÚY[H\È[˜[Y‚ˆ™]\›ˆ›Û™B‚‚™YˆÜÜÚ]]™WÚ[
˜[YNˆ[žJHOˆ[›Û™N‚ˆYˆ\Ú[œÝ[˜ÙJ˜[YK›ÛÛ
HÜˆ›Ý\Ú[œÝ[˜ÙJ˜[YK[
N‚ˆ™]\›ˆ›Û™Bˆ™]\›ˆ˜[YHYˆ˜[YHˆ[ÙH›Û™B‚‚™YˆÚ\×ÝÙXœÛØÚÙ]ØÚ[›™[ÜÙ\ÜÚ[Û—ÚÙ^JÙ^NˆÝŠHOˆ›ÛÛ‚ˆ™]\›ˆÙ^KœÝ\ÝÚ]
ÙXœÛØÚÙ]ˆŠB