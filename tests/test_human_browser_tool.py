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


# --------------------------------------------------------------------------
# Page inventory, form filling and the wider action surface
#
# A real page needs a real Chromium, so the scripts are matched by a marker and
# answered from a table. What is pinned is the contract with the model: which
# targets come back, what each action does with them, and what it says when it
# cannot do it.
# --------------------------------------------------------------------------


class _RoutingTab(_FakeTab):
    """A tab that answers each script by matching a marker inside it."""

    def __init__(self, routes: dict[str, Any]) -> None:
        super().__init__()
        self.routes = routes
        self.scripts: list[str] = []

    @property
    def execute_script(self):  # type: ignore[override]
        async def _script(expression: str) -> Any:
            self.scripts.append(expression)
            for marker, value in self.routes.items():
                if marker in expression:
                    return value
            return ""

        return _script


_INVENTORY = json.dumps(
    [
        {
            "i": 0,
            "target": '[data-powerx-idx="0"]',
            "tag": "input",
            "name": "email",
            "text": "Email",
        },
        {
            "i": 1,
            "target": '[data-powerx-idx="1"]',
            "tag": "button",
            "text": "Sign in",
        },
    ]
)


def test_find_returns_clickable_targets_the_model_can_reuse() -> None:
    tab = _RoutingTab({"a,button,input,select": _INVENTORY})
    tool = _tool_with_session(tab)

    payload = json.loads(asyncio.run(tool.execute("find")))

    assert payload["count"] == 2
    assert payload["elements"][0]["target"] == '[data-powerx-idx="0"]'
    assert payload["elements"][1]["text"] == "Sign in"


def test_find_does_not_invent_targets_when_the_page_answers_nothing() -> None:
    tool = _tool_with_session(_FakeTab())
    payload = json.loads(asyncio.run(tool.execute("find")))
    assert payload["count"] == 0
    assert payload["elements"] == []


def test_fill_form_types_every_field_in_order() -> None:
    tab = _RoutingTab({})
    tool = _tool_with_session(tab)

    out = asyncio.run(
        tool.execute(
            "fill_form",
            fields=[
                {"target": "#email", "text": "a@b.test"},
                {"target": "#name", "text": "Ada"},
            ],
        )
    )

    assert json.loads(out)["count"] == 2
    assert tab.element.typed == [("a@b.test", True), ("Ada", True)]


def test_fill_form_rejects_an_empty_list() -> None:
    tool = _tool_with_session(_FakeTab())
    out = asyncio.run(tool.execute("fill_form", fields=[]))
    assert "Error" in out and "fields list" in out


def test_fill_form_rejects_an_entry_without_a_target() -> None:
    tool = _tool_with_session(_FakeTab())
    out = asyncio.run(tool.execute("fill_form", fields=[{"text": "orphan"}]))
    assert "Error" in out and "target" in out


def test_fill_form_caps_the_number_of_fields() -> None:
    tool = _tool_with_session(_FakeTab())
    too_many = [{"target": f"#f{i}", "text": "x"} for i in range(tool._MAX_FORM_FIELDS + 1)]
    out = asyncio.run(tool.execute("fill_form", fields=too_many))
    assert "Error" in out and "at most" in out


def test_press_rejects_a_key_that_is_not_on_the_list() -> None:
    tool = _tool_with_session(_FakeTab())
    out = asyncio.run(tool.execute("press", key="F13"))
    assert "Error" in out and "unsupported key" in out


def test_press_without_a_key_is_rejected() -> None:
    tool = _tool_with_session(_FakeTab())
    out = asyncio.run(tool.execute("press"))
    assert "Error" in out and "key is required" in out


def test_press_falls_back_to_a_real_form_submit_when_no_driver_keyboard() -> None:
    """A synthetic KeyboardEvent is untrusted; requestSubmit is not."""
    tab = _RoutingTab({"KeyboardEvent('keydown'": "submitted"})
    tool = _tool_with_session(tab)

    out = asyncio.run(tool.execute("press", key="Enter"))

    assert "Enter" in out and "submitted" in out
    script = next(script for script in tab.scripts if "requestSubmit" in script)
    assert "requestSubmit" in script


def test_select_reports_an_option_the_control_does_not_have() -> None:
    tab = _RoutingTab({"root.options": "no-option"})
    tool = _tool_with_session(tab)
    out = asyncio.run(tool.execute("select", target="#country", option="Atlantis"))
    assert "Error" in out and "not an option" in out


def test_select_refuses_to_report_success_on_a_page_it_could_not_drive() -> None:
    """An empty script reply is not a selected option."""
    tool = _tool_with_session(_FakeTab())
    out = asyncio.run(tool.execute("select", target="#country", option="France"))
    assert "Error" in out and "no result" in out


def test_select_reports_a_selector_that_matched_nothing() -> None:
    tab = _RoutingTab({"root.options": "no-element"})
    tool = _tool_with_session(tab)
    out = asyncio.run(tool.execute("select", target="#country", option="France"))
    assert "Error" in out and "no element matched" in out


def test_hover_requires_a_selector() -> None:
    tool = _tool_with_session(_FakeTab())
    out = asyncio.run(tool.execute("hover"))
    assert "Error" in out and "selector is required" in out


def test_hover_refuses_to_report_success_on_a_page_it_could_not_drive() -> None:
    tool = _tool_with_session(_FakeTab())
    out = asyncio.run(tool.execute("hover", target="#menu"))
    assert "Error" in out and "no result" in out


def test_hover_dispatches_the_mouse_events_a_menu_listens_for() -> None:
    tab = _RoutingTab({"pointerover": "hovered"})
    tool = _tool_with_session(tab)

    assert asyncio.run(tool.execute("hover", target="#menu")) == "Hovered."
    script = next(s for s in tab.scripts if "pointerover" in s)
    for event in ("pointerover", "mouseover", "mouseenter", "mousemove"):
        assert event in script


def test_wait_for_text_returns_the_page_once_the_text_appears() -> None:
    tab = _RoutingTab({"document.body.innerText": "Your order is confirmed"})
    tool = _tool_with_session(tab)

    payload = json.loads(asyncio.run(tool.execute("wait_for_text", text="confirmed")))

    assert "confirmed" in payload["text"]


def test_wait_for_text_reports_a_timeout_instead_of_hanging() -> None:
    tab = _RoutingTab({"document.body.innerText": "nothing to see"})
    tool = _tool_with_session(tab)

    out = asyncio.run(tool.execute("wait_for_text", text="never", timeout_ms=500))

    assert "Error" in out and "did not appear" in out


def test_back_returns_the_page_summary() -> None:
    tab = _RoutingTab({"history.back()": "back", "document.body.innerText": "previous page"})
    tool = _tool_with_session(tab)

    payload = json.loads(asyncio.run(tool.execute("back")))

    assert "previous page" in payload["text"]


# --------------------------------------------------------------------------
# Captcha integration
# --------------------------------------------------------------------------


class _FakeSolver:
    """Stands in for the captcha solver tool, recording what it was asked."""

    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


_TURNSTILE_PAGE = json.dumps(
    {"kind": "turnstile", "sitekey": "0x4AAA", "target": '[data-powerx-idx="captcha"]'}
)


def test_auto_captcha_detects_solves_and_injects_the_token() -> None:
    tab = _RoutingTab(
        {
            "const pick = (sels)": _TURNSTILE_PAGE,
            "kinds.forEach": json.dumps({"filled": 1, "callbacks": 1, "ok": True}),
            "document.body.innerText": "Welcome back",
        }
    )
    solver = _FakeSolver({"captcha_type": "turnstile", "token": "solved-token"})
    tool = _tool_with_session(tab, captcha_solver=solver)

    payload = json.loads(asyncio.run(tool.execute("auto_captcha")))

    assert payload["solved"] is True
    assert payload["kind"] == "turnstile"
    assert payload["fields_filled"] == 1
    assert solver.calls[0]["action"] == "turnstile"
    assert solver.calls[0]["sitekey"] == "0x4AAA"
    token_script = next(s for s in tab.scripts if "kinds.forEach" in s)
    assert "solved-token" in token_script


def test_auto_captcha_uses_the_page_url_when_none_is_given() -> None:
    tab = _RoutingTab(
        {
            "const pick = (sels)": _TURNSTILE_PAGE,
            "kinds.forEach": json.dumps({"filled": 1, "callbacks": 0, "ok": True}),
        }
    )
    solver = _FakeSolver({"token": "t"})
    tool = _tool_with_session(tab, captcha_solver=solver)

    asyncio.run(tool.execute("auto_captcha"))

    assert solver.calls[0]["url"] == "https://example.com/"


def test_auto_captcha_says_plainly_when_no_solver_is_configured() -> None:
    tool = _tool_with_session(_FakeTab())
    payload = json.loads(asyncio.run(tool.execute("auto_captcha")))
    assert payload["solved"] is False
    assert "no captcha solver" in payload["reason"]


def test_auto_captcha_reports_an_absent_captcha_rather_than_claiming_a_solve() -> None:
    tab = _RoutingTab({"const pick = (sels)": json.dumps({"kind": None})})
    tool = _tool_with_session(tab, captcha_solver=_FakeSolver({"token": "t"}))

    payload = json.loads(asyncio.run(tool.execute("auto_captcha")))

    assert payload["solved"] is False
    assert "no captcha was detected" in payload["reason"]


def test_auto_captcha_points_an_image_challenge_at_the_other_action() -> None:
    tab = _RoutingTab(
        {
            "const pick = (sels)": json.dumps(
                {"kind": "image", "target": '[data-powerx-idx="captcha"]'}
            )
        }
    )
    tool = _tool_with_session(tab, captcha_solver=_FakeSolver({"token": "t"}))

    payload = json.loads(asyncio.run(tool.execute("auto_captcha")))

    assert payload["solved"] is False
    assert "solve_image_captcha" in payload["reason"]


def test_auto_captcha_reports_a_token_the_solver_never_returned() -> None:
    tab = _RoutingTab({"const pick = (sels)": _TURNSTILE_PAGE})
    tool = _tool_with_session(
        tab, captcha_solver=_FakeSolver("Error: the captcha solver has no API key")
    )

    payload = json.loads(asyncio.run(tool.execute("auto_captcha")))

    assert payload["solved"] is False
    assert "no token" in payload["reason"]


def test_auto_captcha_marks_a_token_solved_but_not_injected() -> None:
    """A token the page never accepted is not a solved challenge."""
    tab = _RoutingTab(
        {
            "const pick = (sels)": _TURNSTILE_PAGE,
            "kinds.forEach": json.dumps({"filled": 0, "callbacks": 0, "ok": False}),
        }
    )
    tool = _tool_with_session(tab, captcha_solver=_FakeSolver({"token": "t"}))

    payload = json.loads(asyncio.run(tool.execute("auto_captcha")))

    assert payload["solved"] is False
    assert payload["fields_filled"] == 0


def test_solve_image_captcha_explains_itself_with_no_solver() -> None:
    tool = _tool_with_session(_FakeTab())
    payload = json.loads(asyncio.run(tool.execute("solve_image_captcha")))
    assert payload["solved"] is False
    assert "no captcha solver" in payload["reason"]


def test_solve_image_captcha_without_a_challenge_is_reported() -> None:
    tab = _RoutingTab({"const pick = (sels)": json.dumps({"kind": None})})
    tool = _tool_with_session(tab, captcha_solver=_FakeSolver({"token": "t"}))

    payload = json.loads(asyncio.run(tool.execute("solve_image_captcha")))

    assert payload["solved"] is False
    assert "no picture challenge" in payload["reason"]
