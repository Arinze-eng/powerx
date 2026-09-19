"""Tests for the pydoll-backed human browser tool.

Pydoll drives a real Chromium, so these tests never launch one: a fake tab
records what the tool asks of it. What is pinned here is the tool's own
behaviour -- registration and gating, URL safety, argument dispatch onto the
pydoll API, and the rule that closing a session disconnects without killing a
browser we merely attached to.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.tools import human_browser as hb
from nanobot.agent.tools.human_browser import HumanBrowserTool, HumanBrowserToolConfig
from nanobot.agent.tools.loader import ToolLoader
from nanobot.config.schema import Config


class _Awaitable:
    """Stands in for pydoll's async properties such as ``tab.title``."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def __await__(self):
        async def _inner() -> Any:
            return self._value

        return _inner().__await__()


class _FakeElement:
    def __init__(self) -> None:
        self.clicks: list[bool] = []
        self.typed: list[tuple[str, bool]] = []

    async def click(self, *, humanize: bool = False, **_kwargs: Any) -> None:
        self.clicks.append(humanize)

    async def type_text(self, text: str, *, humanize: bool = False, **_kwargs: Any) -> None:
        self.typed.append((text, humanize))


class _FakeScroll:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def by(self, x: int, y: int) -> None:
        self.calls.append((x, y))


class _FakeTab:
    """Minimal stand-in for a pydoll Tab."""

    def __init__(self) -> None:
        self.title = _Awaitable("Example page")
        self.current_url = _Awaitable("https://example.com/")
        self.page_source = "<html><body>hello</body></html>"
        self.scroll = _FakeScroll()
        self.element = _FakeElement()
        self.navigated: list[str] = []
        self.screenshots: list[str] = []

    async def go_to(self, url: str, timeout: int = 300) -> None:
        self.navigated.append(url)

    async def find(self, **_kwargs: Any) -> Any:
        return self.element

    async def query(self, _selector: str) -> Any:
        return self.element

    async def take_screenshot(self, path: str | None = None, **_kwargs: Any) -> None:
        self.screenshots.append(str(path))


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def stop(self) -> None:  # pragma: no cover - must never be called
        raise AssertionError("the tool must not stop a browser it merely attached to")


def _ctx(cfg: HumanBrowserToolConfig) -> SimpleNamespace:
    return SimpleNamespace(config=SimpleNamespace(human_browser=cfg), workspace=None)


def _tool_with_session(tab: _FakeTab, **overrides: Any) -> HumanBrowserTool:
    """A tool that already holds an attached session, so no browser is spawned."""
    options: dict[str, Any] = {
        "provider": "cdp",
        "cdp_url": "ws://127.0.0.1:9222/devtools/browser/fake",
    }
    options.update(overrides)
    tool = HumanBrowserTool(**options)
    tool._sessions["default"] = hb._HumanBrowserSession(
        browser=_FakeBrowser(), tab=tab, sandbox=None, last_used=time.monotonic()
    )
    return tool


# --- registration and gating ------------------------------------------------


def test_tool_is_discovered() -> None:
    names = {cls.__name__ for cls in ToolLoader().discover()}
    assert "HumanBrowserTool" in names


def test_config_is_disabled_by_default() -> None:
    cfg = Config().tools.human_browser
    assert cfg.enable is False
    assert cfg.provider == "novita"
    assert cfg.novita_browser_port == 9223
    assert cfg.humanize is True


def test_enabled_requires_opt_in_and_a_usable_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hb, "Chrome", object)
    monkeypatch.setattr(hb, "Novita", object)
    monkeypatch.delenv("NOVITA_API_KEY", raising=False)

    assert HumanBrowserTool.enabled(_ctx(HumanBrowserToolConfig())) is False
    assert (
        HumanBrowserTool.enabled(_ctx(HumanBrowserToolConfig(enable=True))) is False
    ), "the novita provider needs NOVITA_API_KEY"

    monkeypatch.setenv("NOVITA_API_KEY", "key")
    assert HumanBrowserTool.enabled(_ctx(HumanBrowserToolConfig(enable=True))) is True

    # The cdp provider needs a url instead of the novita key.
    assert (
        HumanBrowserTool.enabled(
            _ctx(HumanBrowserToolConfig(enable=True, provider="cdp"))
        )
        is False
    )
    assert (
        HumanBrowserTool.enabled(
            _ctx(
                HumanBrowserToolConfig(
                    enable=True, provider="cdp", cdp_url="ws://127.0.0.1:9222/devtools/browser/x"
                )
            )
        )
        is True
    )


def test_disabled_without_pydoll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hb, "Chrome", None)
    assert HumanBrowserTool.enabled(_ctx(HumanBrowserToolConfig(enable=True))) is False


# --- debug endpoint discovery -----------------------------------------------


def test_ws_address_passes_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = HumanBrowserTool(provider="cdp", cdp_url="ws://host/devtools/browser/abc")
    address = asyncio.run(tool._discover_ws_address("ws://host/devtools/browser/abc"))
    assert address == "ws://host/devtools/browser/abc"


def test_http_debug_endpoint_is_resolved_via_json_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    class _Response:
        is_success = True

        def json(self) -> dict[str, str]:
            return {"webSocketDebuggerUrl": "ws://host/devtools/browser/abc"}

    class _Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        async def get(self, url: str) -> _Response:
            seen.append(url)
            return _Response()

    monkeypatch.setattr(hb.httpx, "AsyncClient", _Client)
    tool = HumanBrowserTool(provider="cdp", cdp_url="http://host:9222")

    address = asyncio.run(tool._discover_ws_address("http://host:9222"))

    assert seen == ["http://host:9222/json/version"]
    assert address == "ws://host/devtools/browser/abc"


# --- URL safety -------------------------------------------------------------


def test_navigate_validates_then_drives_the_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hb, "resolve_url_target", lambda url, **_kw: (True, "", ()))
    tab = _FakeTab()
    tool = _tool_with_session(tab)

    result = asyncio.run(tool.execute("navigate", url="https://example.com"))

    assert tab.navigated == ["https://example.com"]
    assert json.loads(result)["title"] == "Example page"


def test_navigate_refuses_a_cloud_metadata_target() -> None:
    tab = _FakeTab()
    tool = _tool_with_session(tab)

    result = asyncio.run(
        tool.execute("navigate", url="http://169.254.169.254/latest/meta-data/")
    )

    assert result.is_error
    assert tab.navigated == [], "a blocked URL must never reach the browser"


def test_navigate_refuses_loopback_and_private_targets() -> None:
    tab = _FakeTab()
    tool = _tool_with_session(tab)

    for url in ("http://127.0.0.1/admin", "http://10.0.0.5/", "http://192.168.1.1/"):
        result = asyncio.run(tool.execute("navigate", url=url))
        assert result.is_error, url

    assert tab.navigated == []


def test_allowed_domains_narrows_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hb, "resolve_url_target", lambda url, **_kw: (True, "", ()))
    tab = _FakeTab()
    tool = _tool_with_session(tab, allowed_domains=["example.com"])

    refused = asyncio.run(tool.execute("navigate", url="https://other.test/"))
    assert refused.is_error
    assert tab.navigated == []

    allowed = asyncio.run(tool.execute("navigate", url="https://www.example.com/page"))
    assert tab.navigated == ["https://www.example.com/page"]
    assert json.loads(allowed)["url"] == "https://example.com/"


# --- action dispatch --------------------------------------------------------


def test_click_and_type_use_humanized_input() -> None:
    tab = _FakeTab()
    tool = _tool_with_session(tab, humanize=True)

    asyncio.run(tool.execute("click", target="#submit"))
    asyncio.run(tool.execute("type", target="#email", text="hello@example.com"))

    assert tab.element.clicks == [True]
    assert tab.element.typed == [("hello@example.com", True)]


def test_humanize_can_be_turned_off() -> None:
    tab = _FakeTab()
    tool = _tool_with_session(tab, humanize=False)

    asyncio.run(tool.execute("click", target="#submit"))

    assert tab.element.clicks == [False]


def test_scroll_moves_by_pixels_in_the_requested_direction() -> None:
    tab = _FakeTab()
    tool = _tool_with_session(tab)

    asyncio.run(tool.execute("scroll", direction="up", pixels=250))
    asyncio.run(tool.execute("scroll", direction="down", pixels=100))

    assert tab.scroll.calls == [(0, -250), (0, 100)]


def test_type_without_text_is_rejected() -> None:
    tab = _FakeTab()
    tool = _tool_with_session(tab)
    result = asyncio.run(tool.execute("type", target="#email"))
    assert result.is_error


def test_unknown_action_returns_a_tool_error() -> None:
    tool = _tool_with_session(_FakeTab())
    result = asyncio.run(tool.execute("teleport"))
    assert result.is_error


def test_screenshot_is_written_under_the_workspace(tmp_path: Path) -> None:
    tab = _FakeTab()
    tool = _tool_with_session(tab, workspace=tmp_path)

    result = asyncio.run(tool.execute("screenshot"))

    path = Path(json.loads(result)["screenshot"])
    assert path.parent == tmp_path.resolve()
    assert tab.screenshots == [str(path)]


# --- teardown ---------------------------------------------------------------


def test_close_disconnects_without_stopping_a_shared_browser() -> None:
    tab = _FakeTab()
    browser = _FakeBrowser()
    tool = HumanBrowserTool(provider="cdp", cdp_url="ws://x")
    tool._sessions["default"] = hb._HumanBrowserSession(
        browser=browser, tab=tab, sandbox=None, last_used=time.monotonic()
    )

    asyncio.run(tool.execute("close"))

    assert browser.closed is True
    assert tool._sessions == {}


def test_close_kills_a_sandbox_we_provisioned() -> None:
    class _Sandbox:
        def __init__(self) -> None:
            self.killed = False

        def kill(self) -> None:
            self.killed = True

    sandbox = _Sandbox()
    tool = HumanBrowserTool(provider="novita")
    tool._sessions["default"] = hb._HumanBrowserSession(
        browser=_FakeBrowser(), tab=_FakeTab(), sandbox=sandbox, last_used=time.monotonic()
    )

    asyncio.run(tool.execute("close"))

    assert sandbox.killed is True
