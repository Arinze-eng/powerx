"""Human-like browsing, driven by pydoll over the DevTools Protocol.

Pydoll (https://github.com/autoscrape-labs/pydoll) drives a Chromium-family
browser directly over CDP: no WebDriver binary, no ``navigator.webdriver``
flag, and mouse movement, typing and scrolling modelled on a person rather than
dispatched instantly. That makes it a useful second browser for pages the
existing ``browser`` tool's plain CDP calls handle poorly.

The agent image deliberately ships no browser, so this tool **attaches** to a
Chrome that is already running rather than launching one:

* ``provider="novita"`` (default) provisions the same ephemeral Novita
  ``browser-chromium`` sandbox the ``browser`` tool uses and attaches to it.
* ``provider="cdp"`` attaches to the operator's own Chromium via ``cdp_url``,
  which is either a ``ws://`` DevTools address or an ``http://host:port``
  debug endpoint (the browser-level address is read from ``/json/version``).

Navigation is gated the same way as the ``browser`` tool: every URL goes
through :func:`nanobot.security.network.resolve_url_target`, which rejects
loopback, private, link-local and cloud-metadata targets, and an optional
``allowed_domains`` list narrows it further.

The tool is disabled until an operator enables it; see
:class:`HumanBrowserToolConfig`.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import Field

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.config_base import Base
from nanobot.security.network import resolve_url_target

try:  # pragma: no cover - exercised only where the dependency is installed
    from pydoll.browser.chromium import Chrome
except ImportError:  # pragma: no cover
    Chrome = None  # type: ignore[assignment,misc]

try:  # pragma: no cover - exercised only where the dependency is installed
    from novita_sandbox import Novita
except ImportError:  # pragma: no cover
    Novita = None  # type: ignore[assignment,misc]


class HumanBrowserToolConfig(Base):
    """Configuration for the pydoll-backed human browser.

    ``enable`` defaults to False. ``provider="novita"`` reuses the sandbox
    infrastructure the ``browser`` tool already relies on, so it works with the
    same ``NOVITA_API_KEY``; switch to ``provider="cdp"`` to drive a Chromium
    you run yourself.
    """

    enable: bool = False
    provider: str = "novita"
    novita_api_key_env: str = "NOVITA_API_KEY"
    novita_template: str = "browser-chromium"
    novita_timeout_seconds: int = Field(default=600, ge=60, le=7_200)
    novita_browser_port: int = Field(default=9223, ge=1, le=65_535)
    cdp_url: str = ""
    navigation_timeout_ms: int = Field(default=30_000, ge=5_000, le=120_000)
    action_timeout_ms: int = Field(default=15_000, ge=2_000, le=60_000)
    session_idle_seconds: int = Field(default=900, ge=60, le=7_200)
    max_page_text_chars: int = Field(default=12_000, ge=1_000, le=50_000)
    humanize: bool = True
    allowed_domains: list[str] = Field(default_factory=list)


@dataclass
class _HumanBrowserSession:
    """One attached browser plus the sandbox we created for it, if any."""

    browser: Any
    tab: Any
    sandbox: Any
    last_used: float


class HumanBrowserTool(Tool):
    """Browse a public site with pydoll's humanized interactions."""

    config_key = "human_browser"
    _scopes = {"core", "subagent"}

    _MAX_TARGET = 500
    _MAX_TYPED_TEXT = 8_000
    _MAX_URL = 2_000
    _MAX_SCROLL = 5_000
    _ACTIONS = frozenset(
        {"navigate", "read_page", "click", "type", "scroll", "wait_for", "screenshot", "close"}
    )

    def __init__(
        self,
        *,
        workspace: Path | str | None = None,
        provider: str = "novita",
        novita_api_key_env: str = "NOVITA_API_KEY",
        novita_template: str = "browser-chromium",
        novita_timeout_seconds: int = 600,
        novita_browser_port: int = 9223,
        cdp_url: str = "",
        navigation_timeout_ms: int = 30_000,
        action_timeout_ms: int = 15_000,
        session_idle_seconds: int = 900,
        max_page_text_chars: int = 12_000,
        humanize: bool = True,
        allowed_domains: list[str] | None = None,
    ) -> None:
        self.workspace = workspace
        self.provider = str(provider or "novita").strip().lower()
        self.novita_api_key_env = novita_api_key_env
        self.novita_template = novita_template
        self.novita_timeout_seconds = novita_timeout_seconds
        self.novita_browser_port = novita_browser_port
        self.cdp_url = str(cdp_url or "").strip()
        self.navigation_timeout_ms = navigation_timeout_ms
        self.action_timeout_ms = action_timeout_ms
        self.session_idle_seconds = session_idle_seconds
        self.max_page_text_chars = max_page_text_chars
        self.humanize = bool(humanize)
        self.allowed_domains = [
            domain.strip().lower()
            for domain in (allowed_domains or [])
            if str(domain).strip()
        ]
        self._sessions: dict[str, _HumanBrowserSession] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def config_cls(cls):
        return HumanBrowserToolConfig

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        cfg = ctx.config.human_browser
        if not cfg.enable or Chrome is None:
            return False
        provider = str(cfg.provider or "novita").strip().lower()
        if provider == "cdp":
            return bool(str(cfg.cdp_url or "").strip())
        if provider == "novita":
            return bool(
                Novita is not None
                and os.getenv(str(cfg.novita_api_key_env or ""), "").strip()
            )
        return False

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        cfg = ctx.config.human_browser
        return cls(
            workspace=ctx.workspace,
            provider=cfg.provider,
            novita_api_key_env=cfg.novita_api_key_env,
            novita_template=cfg.novita_template,
            novita_timeout_seconds=cfg.novita_timeout_seconds,
            novita_browser_port=cfg.novita_browser_port,
            cdp_url=cfg.cdp_url,
            navigation_timeout_ms=cfg.navigation_timeout_ms,
            action_timeout_ms=cfg.action_timeout_ms,
            session_idle_seconds=cfg.session_idle_seconds,
            max_page_text_chars=cfg.max_page_text_chars,
            humanize=cfg.humanize,
            allowed_domains=cfg.allowed_domains,
        )

    @property
    def name(self) -> str:
        return "human_browser"

    @property
    def description(self) -> str:
        return (
            "Browse a public website with human-like mouse, typing and scrolling, attaching to an "
            "existing Chrome over the DevTools Protocol. Actions: navigate, read_page, click, type, "
            "scroll, wait_for, screenshot and close. Prefer this over the plain browser tool when a "
            "site is sensitive to obviously automated input. Private/internal URLs are blocked. "
            "Never submit purchases, publish content, send messages, or enter credentials unless the "
            "user explicitly authorized that exact action in the conversation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": sorted(self._ACTIONS)},
                "url": {"type": ["string", "null"], "maxLength": self._MAX_URL},
                "target": {"type": ["string", "null"], "maxLength": self._MAX_TARGET},
                "text": {"type": ["string", "null"], "maxLength": self._MAX_TYPED_TEXT},
                "direction": {
                    "type": ["string", "null"],
                    "enum": ["up", "down", "left", "right", None],
                },
                "pixels": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": self._MAX_SCROLL,
                },
                "full_page": {"type": ["boolean", "null"]},
                "timeout_ms": {
                    "type": ["integer", "null"],
                    "minimum": 500,
                    "maximum": 120_000,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    # --- session plumbing -------------------------------------------------

    def _session_key(self) -> str:
        context = current_request_context()
        if context is None:
            return "default"
        return context.session_key or f"{context.channel}:{context.chat_id}"

    async def _discover_ws_address(self, endpoint: str) -> str:
        """Ask a DevTools debug endpoint for its browser WebSocket URL."""
        base = endpoint.strip().rstrip("/")
        if base.startswith(("ws://", "wss://")):
            return base
        if not base.startswith(("http://", "https://")):
            base = f"http://{base}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(f"{base}/json/version")
        if not response.is_success:
            raise RuntimeError(
                f"the browser debug endpoint returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("the browser debug endpoint returned invalid JSON") from exc
        address = str((payload or {}).get("webSocketDebuggerUrl") or "").strip()
        if not address:
            raise RuntimeError("the browser debug endpoint exposed no WebSocket address")
        return address

    async def _new_session(self, key: str) -> _HumanBrowserSession:
        if Chrome is None:
            raise RuntimeError("the pydoll browser capability is unavailable in this deployment")

        sandbox = None
        browser = None
        try:
            if self.provider == "novita":
                if Novita is None:
                    raise RuntimeError("the Novita sandbox capability is unavailable")
                api_key = os.getenv(self.novita_api_key_env, "").strip()
                if not api_key:
                    raise RuntimeError(
                        f"the Novita browser provider requires {self.novita_api_key_env}"
                    )
                novita = Novita(api_key=api_key)
                sandbox = await asyncio.to_thread(
                    novita.sandbox.create,
                    self.novita_template,
                    timeout=self.novita_timeout_seconds,
                    allow_internet_access=True,
                )
                host = await asyncio.to_thread(sandbox.get_host, self.novita_browser_port)
                address = await self._discover_ws_address(f"http://{host}")
            elif self.provider == "cdp":
                if not self.cdp_url:
                    raise RuntimeError("provider 'cdp' requires cdp_url in the tool configuration")
                address = await self._discover_ws_address(self.cdp_url)
            else:
                raise RuntimeError(f"unsupported browser provider: {self.provider}")

            browser = Chrome()
            tab = await browser.connect(address)
            session = _HumanBrowserSession(browser, tab, sandbox, time.monotonic())
            self._sessions[key] = session
            return session
        except Exception:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001 - teardown must not mask the real error
                    pass
            if sandbox is not None:
                await asyncio.to_thread(sandbox.kill)
            raise

    async def _close_session(self, key: str) -> None:
        session = self._sessions.pop(key, None)
        if session is None:
            return
        if session.browser is not None:
            # close() only drops our WebSocket. stop() would kill a browser we
            # did not start, which is exactly what we must not do.
            try:
                await session.browser.close()
            except Exception:  # noqa: BLE001 - one dead socket must not block cleanup
                pass
        if session.sandbox is not None:
            await asyncio.to_thread(session.sandbox.kill)

    async def _get_session(self, key: str) -> _HumanBrowserSession:
        now = time.monotonic()
        for old_key, session in list(self._sessions.items()):
            if old_key != key and now - session.last_used > self.session_idle_seconds:
                await self._close_session(old_key)
        session = self._sessions.get(key)
        if session is None:
            session = await self._new_session(key)
        session.last_used = now
        return session

    # --- safety -----------------------------------------------------------

    async def _validate_url(self, url: str) -> None:
        ok, error, _ = await asyncio.to_thread(resolve_url_target, url)
        if not ok:
            raise ValueError(f"refusing to open that URL: {error}")
        if self.allowed_domains:
            host = (urlparse(url).hostname or "").lower()
            if not any(
                host == allowed or host.endswith(f".{allowed}")
                for allowed in self.allowed_domains
            ):
                raise ValueError(f"host {host!r} is not in the configured allowed_domains")

    # --- actions ----------------------------------------------------------

    async def _page_text(self, tab: Any) -> str:
        """Best-effort visible text for the current page."""
        for attribute in ("execute_script",):
            script = getattr(tab, attribute, None)
            if script is None:
                continue
            try:
                text = await script(
                    "return document.body ? document.body.innerText : '';"
                )
            except Exception:  # noqa: BLE001 - fall back to the raw source
                continue
            if isinstance(text, str) and text.strip():
                return text.strip()[: self.max_page_text_chars]
        source = getattr(tab, "page_source", None)
        if source is not None:
            try:
                html = await source if asyncio.iscoroutine(source) else source
            except Exception:  # noqa: BLE001
                html = None
            if isinstance(html, str):
                return html.strip()[: self.max_page_text_chars]
        return ""

    async def _summary(self, tab: Any) -> str:
        try:
            title = await tab.title
        except Exception:  # noqa: BLE001
            title = ""
        try:
            url = await tab.current_url
        except Exception:  # noqa: BLE001
            url = ""
        body = await self._page_text(tab)
        return json.dumps({"title": str(title or ""), "url": str(url or ""), "text": body})

    async def _resolve_element(self, tab: Any, target: str) -> Any:
        """Find an element by CSS selector, or by its visible text."""
        wanted = str(target or "").strip()
        if not wanted:
            raise ValueError("a CSS selector or visible text target is required")
        finder = getattr(tab, "find", None)
        if finder is not None:
            try:
                element = await finder(text=wanted, timeout=5, raise_exc=False)
            except Exception:  # noqa: BLE001 - fall through to the CSS query
                element = None
            if element is not None:
                return element
        query = getattr(tab, "query", None)
        if query is None:
            raise ValueError("this pydoll build exposes no element lookup")
        element = await query(wanted)
        if element is None:
            raise ValueError(f"no element matched {wanted!r}")
        return element

    async def execute(
        self,
        action: str,
        url: str | None = None,
        target: str | None = None,
        text: str | None = None,
        direction: str | None = None,
        pixels: int | None = None,
        full_page: bool | None = None,
        timeout_ms: int | None = None,
    ) -> Any:
        action = str(action or "").strip().lower()
        if action not in self._ACTIONS:
            return ToolResult.error("Error: unsupported human browser action")
        session_key = self._session_key()
        async with self._lock:
            try:
                if action == "close":
                    await self._close_session(session_key)
                    return "Human browser session closed."
                session = await self._get_session(session_key)
                tab = session.tab

                if action == "navigate":
                    requested = str(url or "").strip()
                    if not requested:
                        raise ValueError("a URL is required for navigate")
                    await self._validate_url(requested)
                    await asyncio.wait_for(
                        tab.go_to(requested),
                        timeout=self.navigation_timeout_ms / 1000,
                    )
                    return await self._summary(tab)

                if action == "read_page":
                    return await self._summary(tab)

                if action == "click":
                    element = await self._resolve_element(tab, str(target or ""))
                    await asyncio.wait_for(
                        element.click(humanize=self.humanize),
                        timeout=self.action_timeout_ms / 1000,
                    )
                    return await self._summary(tab)

                if action == "type":
                    if text is None:
                        raise ValueError("text is required for type")
                    element = await self._resolve_element(tab, str(target or ""))
                    await asyncio.wait_for(
                        element.type_text(
                            str(text)[: self._MAX_TYPED_TEXT], humanize=self.humanize
                        ),
                        timeout=self.action_timeout_ms / 1000,
                    )
                    return "Typed into the element."

                if action == "scroll":
                    step = int(pixels or 600)
                    axis = str(direction or "down").strip().lower()
                    delta = {
                        "down": (0, step),
                        "up": (0, -step),
                        "right": (step, 0),
                        "left": (-step, 0),
                    }.get(axis, (0, step))
                    await asyncio.wait_for(
                        tab.scroll.by(delta[0], delta[1]),
                        timeout=self.action_timeout_ms / 1000,
                    )
                    return await self._summary(tab)

                if action == "wait_for":
                    element = await self._resolve_element(tab, str(target or ""))
                    if element is None:
                        raise ValueError(f"no element matched {target!r}")
                    return f"Found the element after waiting: {str(target)[:200]}"

                if action == "screenshot":
                    name = f"human-browser-{int(time.time())}.png"
                    root = Path(self.workspace) if self.workspace else Path.cwd()
                    path = (root / name).resolve()
                    await asyncio.wait_for(
                        tab.take_screenshot(
                            path=str(path), beyond_viewport=bool(full_page)
                        ),
                        timeout=self.action_timeout_ms / 1000,
                    )
                    return json.dumps({"screenshot": str(path)})

                raise ValueError("unsupported human browser action")
            except ValueError as exc:
                return ToolResult.error(f"Error: {exc}")
            except TimeoutError:
                return ToolResult.error("Error: the browser action timed out")
            except httpx.HTTPError as exc:
                return ToolResult.error(f"Error: could not reach the browser endpoint: {exc}")
            except Exception as exc:  # noqa: BLE001 - surface driver errors as tool errors
                return ToolResult.error(
                    f"Error: the human browser failed: {type(exc).__name__}: {exc}"
                )
