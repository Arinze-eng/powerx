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
    # The reported host is the browser's own view and is not routable from the
    # agent, so the endpoint we actually reached wins and the path is kept.
    assert address == "ws://host:9222/devtools/browser/abc"


def test_novita_sandbox_uses_https_and_rewrites_the_reported_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real Novita sandbox is https-only and reports an internal ws host.

    Regression: the tool used to dial ``http://<host>``, which times out
    against the sandbox ingress, and returned the internal address verbatim.
    """
    seen: list[str] = []

    class _Response:
        is_success = True

        def json(self) -> dict[str, str]:
            # What a Novita sandbox actually reports: its own local view.
            return {
                "webSocketDebuggerUrl": "ws://localhost:9222/devtools/browser/internal-id"
            }

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
    host = "9223-abc123.us-phx-1.sandbox.novita.ai"
    tool = HumanBrowserTool(provider="novita")

    address = asyncio.run(tool._discover_ws_address(f"https://{host}"))

    assert seen == [f"https://{host}/json/version"]
    assert address == f"wss://{host}/devtools/browser/internal-id"


def test_bare_sandbox_host_defaults_to_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    class _Response:
        is_success = True

        def json(self) -> dict[str, str]:
            return {"webSocketDebuggerUrl": "ws://localhost:9222/devtools/browser/x"}

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
    tool = HumanBrowserTool(provider="novita")

    address = asyncio.run(tool._discover_ws_address("sandbox.example:9223"))

    assert seen == ["https://sandbox.example:9223/json/version"]
    assert address == "wss://sandbox.example:9223/devtools/browser/x"


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


# --------------------------------------------------------------------------
# read_page text extraction
#
# Regression: pydoll's execute_script returns the raw CDP envelope, not the
# script value. Treating the envelope as a string made _page_text fall through
# to page_source, so read_page returned raw HTML to the model.
# --------------------------------------------------------------------------


class _ScriptTab(_FakeTab):
    """A tab whose execute_script behaves like real pydoll (CDP envelope)."""

    def __init__(self, envelope: Any) -> None:
        super().__init__()
        self._envelope = envelope

    @property
    def execute_script(self):  # type: ignore[override]
        async def _script(_expression: str) -> Any:
            return self._envelope

        return _script


def test_page_text_unwraps_cdp_envelope() -> None:
    envelope = {
        "id": 4,
        "result": {"result": {"type": "string", "value": "Example Domain\n\nLearn more"}},
    }
    tool = _tool_with_session(_ScriptTab(envelope))
    text = asyncio.run(tool._page_text(tool._sessions["default"].tab))
    assert text == "Example Domain\n\nLearn more"


def test_page_text_accepts_plain_string() -> None:
    tool = _tool_with_session(_ScriptTab("just text"))
    text = asyncio.run(tool._page_text(tool._sessions["default"].tab))
    assert text == "just text"


def test_page_text_strips_html_when_script_is_unavailable() -> None:
    class _NoScriptTab(_FakeTab):
        def __init__(self) -> None:
            super().__init__()
            self.page_source = (
                "<html><head><style>body{color:red}</style>"
                "<script>var x=1;</script></head>"
                "<body><h1>Hello</h1><p>World</p></body></html>"
            )

    tool = _tool_with_session(_NoScriptTab())
    text = asyncio.run(tool._page_text(tool._sessions["default"].tab))
    assert "Hello" in text and "World" in text
    assert "<h1>" not in text and "var x=1" not in text and "color:red" not in text


def test_read_page_returns_visible_text_not_markup() -> None:
    envelope = {
        "id": 1,
        "result": {"result": {"type": "string", "value": "Readable page body"}},
    }
    tool = _tool_with_session(_ScriptTab(envelope))
    out = asyncio.run(tool.execute("read_page"))
    payload = json.loads(out)
    assert payload["text"] == "Readable page body"
    assert "<html" not in payload["text"]


# --------------------------------------------------------------------------
# Cloudflare Turnstile solving
#
# The widget lives in nested shadow roots (shadow host -> challenge iframe ->
# body -> inner shadow root -> checkbox), so the solver is exercised against a
# fake shadow DOM that mirrors that shape.
# --------------------------------------------------------------------------


class _FakeShadowRoot:
    """Stand-in for a pydoll ShadowRoot with a stubbed inner_html."""

    def __init__(self, html: str, iframe: Any = None) -> None:
        self._html = html
        self._iframe = iframe
        self.queries: list[str] = []

    @property
    async def inner_html(self) -> str:
        return self._html

    async def query(self, expression: str, **kwargs: Any) -> Any:
        self.queries.append(expression)
        return self._iframe


class _FakeBody:
    """The challenge iframe's body, which hosts the inner shadow root."""

    def __init__(self, inner_shadow: Any) -> None:
        self._inner_shadow = inner_shadow
        self.shadow_root_requests: list[dict[str, Any]] = []

    async def get_shadow_root(self, **kwargs: Any) -> Any:
        self.shadow_root_requests.append(kwargs)
        return self._inner_shadow


class _FakeWidget:
    """The inner shadow root holding the Turnstile checkbox."""

    def __init__(self, checkbox: Any) -> None:
        self._checkbox = checkbox

    async def query(self, expression: str, **kwargs: Any) -> Any:
        return self._checkbox


class _FakeIframe:
    def __init__(self, body: Any) -> None:
        self._body = body

    async def find(self, **kwargs: Any) -> Any:
        self._body_find_kwargs = kwargs
        return self._body


class _CloudflareTab(_FakeTab):
    """A tab that exposes a Cloudflare Turnstile shadow DOM.

    ``roots`` is a list of per-scan snapshots: each call to
    ``find_shadow_roots`` pops the next one, which is how the async injection
    and the re-render of the challenge are modelled.
    """

    def __init__(self, roots: list[Any], clicks: list[bool] | None = None) -> None:
        super().__init__()
        self._roots = list(roots)
        self.scans = 0
        if clicks is not None:
            self.element.clicks = clicks

    async def find_shadow_roots(self, deep: bool = False) -> list[Any]:
        self.scans += 1
        if not self._roots:
            return []
        if len(self._roots) == 1:
            return self._roots
        return [self._roots.pop(0)]


def _cloudflare_widget(checkbox: Any) -> _FakeShadowRoot:
    """Build the full Turnstile traversal: iframe -> body -> shadow -> checkbox."""
    body = _FakeBody(_FakeWidget(checkbox))
    iframe = _FakeIframe(body)
    return _FakeShadowRoot(
        f'<iframe src="https://{hb._CLOUDFLARE_CHALLENGE_DOMAIN}/turnstile/v0/api.js"></iframe>',
        iframe=iframe,
    )


def test_solve_cloudflare_clicks_the_checkbox_humanized() -> None:
    checkbox = _FakeElement()
    root = _cloudflare_widget(checkbox)
    tab = _CloudflareTab([root])
    tool = _tool_with_session(tab, humanize=True, cloudflare_timeout_seconds=1.0)

    out = asyncio.run(tool.execute("solve_cloudflare"))

    report = json.loads(out)["cloudflare"]
    assert report["challenge_present"] is True
    assert report["solved"] is True
    assert checkbox.clicks == [True], "the checkbox click must be humanized"


def test_solve_cloudflare_respects_humanize_off() -> None:
    checkbox = _FakeElement()
    tab = _CloudflareTab([_cloudflare_widget(checkbox)])
    tool = _tool_with_session(tab, humanize=False, cloudflare_timeout_seconds=1.0)

    asyncio.run(tool.execute("solve_cloudflare"))

    assert checkbox.clicks == [False]


def test_solve_cloudflare_retries_until_the_widget_is_injected() -> None:
    checkbox = _FakeElement()
    # First scan: widget not yet injected. Second scan: it is. This is the
    # async-injection case Cloudflare actually produces, and the page already
    # references Cloudflare (which is what tells us to keep polling).
    tab = _CloudflareTab([_FakeShadowRoot("<div>not cloudflare</div>"), _cloudflare_widget(checkbox)])
    tab.page_source = '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>'
    tool = _tool_with_session(tab, cloudflare_timeout_seconds=5.0)

    out = asyncio.run(tool.execute("solve_cloudflare"))

    report = json.loads(out)["cloudflare"]
    assert report["solved"] is True
    assert report["attempts"] >= 2
    assert checkbox.clicks == [True]


def test_solve_cloudflare_reports_absence_without_raising() -> None:
    tab = _CloudflareTab([_FakeShadowRoot("<div>plain page</div>")])
    tool = _tool_with_session(tab, cloudflare_timeout_seconds=1.0)

    out = asyncio.run(tool.execute("solve_cloudflare"))

    report = json.loads(out)["cloudflare"]
    assert report["challenge_present"] is False
    assert report["solved"] is False
    assert "no Cloudflare Turnstile challenge was found" in report["reason"]


def test_plain_page_bails_out_fast_instead_of_waiting_the_full_timeout() -> None:
    """A page with no Cloudflare markers must not tax every navigate.

    Regression guard: an unconditional poll loop would add the whole
    cloudflare_timeout_seconds to every ordinary page load.
    """
    tab = _CloudflareTab([_FakeShadowRoot("<div>nothing to see</div>")])
    tab.page_source = "<html><body><h1>Plain page</h1></body></html>"
    tool = _tool_with_session(tab, cloudflare_timeout_seconds=30.0)

    started = time.monotonic()
    out = asyncio.run(tool.execute("solve_cloudflare"))
    elapsed = time.monotonic() - started

    report = json.loads(out)["cloudflare"]
    assert report["solved"] is False
    assert report["attempts"] == 1
    assert elapsed < 5.0, f"should bail out fast, took {elapsed:.1f}s"
    assert tab.scans == 1


def test_page_with_cloudflare_markers_keeps_polling() -> None:
    """An empty first scan is not conclusive on a challenge page."""
    checkbox = _FakeElement()
    bare = _FakeShadowRoot("<div>loading</div>")
    tab = _CloudflareTab([bare, _cloudflare_widget(checkbox)])
    tab.page_source = '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>'
    tool = _tool_with_session(tab, cloudflare_timeout_seconds=5.0)

    out = asyncio.run(tool.execute("solve_cloudflare"))

    report = json.loads(out)["cloudflare"]
    assert report["solved"] is True
    assert report["attempts"] >= 2, "must keep polling when Cloudflare is indicated"
    assert checkbox.clicks == [True]


def test_just_a_moment_title_keeps_polling() -> None:
    checkbox = _FakeElement()
    tab = _CloudflareTab([_FakeShadowRoot("<div>loading</div>"), _cloudflare_widget(checkbox)])
    tab.page_source = "<html><body>wait</body></html>"
    tab.title = _Awaitable("Just a moment...")
    tool = _tool_with_session(tab, cloudflare_timeout_seconds=5.0)

    out = asyncio.run(tool.execute("solve_cloudflare"))

    assert json.loads(out)["cloudflare"]["solved"] is True
    assert checkbox.clicks == [True]


def test_solve_cloudflare_degrades_when_shadow_roots_are_unsupported() -> None:
    tab = _FakeTab()  # no find_shadow_roots
    tool = _tool_with_session(tab, cloudflare_timeout_seconds=120.0)

    out = asyncio.run(tool.execute("solve_cloudflare"))

    report = json.loads(out)["cloudflare"]
    assert report["solved"] is False
    assert "no shadow root inspection" in report["reason"]


def test_navigate_solves_cloudflare_and_reports_it() -> None:
    checkbox = _FakeElement()
    tab = _CloudflareTab([_cloudflare_widget(checkbox)])
    tool = _tool_with_session(tab, cloudflare_timeout_seconds=1.0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hb, "resolve_url_target", lambda url, **_kw: (True, "", ()))
        out = asyncio.run(tool.execute("navigate", url="https://example.com"))

    payload = json.loads(out)
    assert payload["cloudflare"]["solved"] is True
    assert checkbox.clicks == [True]


def test_cloudflare_solving_can_be_disabled() -> None:
    checkbox = _FakeElement()
    tab = _CloudflareTab([_cloudflare_widget(checkbox)])
    tool = _tool_with_session(tab, solve_cloudflare=False)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hb, "resolve_url_target", lambda url, **_kw: (True, "", ()))
        out = asyncio.run(tool.execute("navigate", url="https://example.com"))

    assert "cloudflare" not in json.loads(out)
    assert checkbox.clicks == []


def test_config_defaults_enable_turnstile_solving() -> None:
    cfg = Config().tools.human_browser
    assert cfg.solve_cloudflare is True
    assert cfg.cloudflare_timeout_seconds == 15.0
