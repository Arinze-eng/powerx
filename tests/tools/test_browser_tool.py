from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.tools.browser import BrowserTool


async def _noop() -> None:
    return None


class FakeLocator:
    @property
    def first(self) -> "FakeLocator":
        return self

    def __init__(self) -> None:
        self.clicks = 0
        self.taps = 0
        self.moves = 0
        self.filled: list[str] = []
        self.presses: list[str] = []

    async def click(self) -> None:
        self.clicks += 1

    async def tap(self) -> None:
        self.taps += 1

    async def move(self) -> None:
        self.moves += 1

    async def fill(self, value: str) -> None:
        self.filled.append(value)

    async def press(self, value: str) -> None:
        self.presses.append(value)


class FakePage:
    def __init__(self, tmp_path: Path) -> None:
        self.url = "https://example.com/"
        self._title = "Example"
        self.locator_obj = FakeLocator()
        self.goto_urls: list[str] = []
        self.scrolls: list[tuple[int, int]] = []
        self.screenshot_paths: list[str] = []
        self.tmp_path = tmp_path

    async def title(self) -> str:
        return self._title

    def locator(self, _selector: str) -> SimpleNamespace:
        if _selector == "body":
            return SimpleNamespace(inner_text=self.inner_text)
        return self.locator_obj

    async def inner_text(self, timeout: int = 0) -> str:
        return "Example page content"

    def get_by_text(self, _text: str, exact: bool = False) -> FakeLocator:
        return self.locator_obj

    def get_by_role(self, _role: str, exact: bool = False) -> FakeLocator:
        return self.locator_obj

    async def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        self.goto_urls.append(url)
        self.url = url

    async def wait_for_load_state(self, *args, **kwargs) -> None:
        return None

    class Mouse:
        def __init__(self, outer: "FakePage") -> None:
            self.outer = outer

        async def wheel(self, x: int, y: int) -> None:
            self.outer.scrolls.append((x, y))

    @property
    def mouse(self) -> "FakePage.Mouse":
        return FakePage.Mouse(self)

    async def screenshot(self, *, path: str, full_page: bool = False) -> None:
        self.screenshot_paths.append(path)
        Path(path).write_bytes(b"fake-png")

    async def reload(self, wait_until: str = "domcontentloaded") -> None:
        return None

    async def go_back(self, wait_until: str = "domcontentloaded") -> None:
        return None

    async def go_forward(self, wait_until: str = "domcontentloaded") -> None:
        return None


@pytest.fixture
def browser(tmp_path: Path) -> BrowserTool:
    tool = BrowserTool(workspace=str(tmp_path))
    page = FakePage(tmp_path)
    session = SimpleNamespace(
        playwright=SimpleNamespace(stop=_noop),
        browser=SimpleNamespace(close=_noop),
        context=SimpleNamespace(close=_noop),
        page=page, last_used=0.0,
    )
    tool._sessions["default"] = session
    return tool


@pytest.mark.asyncio
async def test_browser_blocks_private_navigation(browser: BrowserTool) -> None:
    result = await browser.execute(action="navigate", url="http://127.0.0.1:8080/")
    assert result.is_error is True
    assert "blocked" in str(result).lower()


@pytest.mark.asyncio
async def test_browser_supports_navigation_interaction_scroll_and_screenshot(browser: BrowserTool, tmp_path: Path) -> None:
    page = browser._sessions["default"].page
    result = await browser.execute(action="navigate", url="https://example.com")
    assert "Example" in str(result)
    assert page.goto_urls == ["https://example.com"]

    for operation in (
        {"action": "click", "target": "#submit"},
        {"action": "tap", "target": "#touch"},
        {"action": "move", "target": "#touch"},
        {"action": "type", "target": "#name", "text": "Alice"},
        {"action": "press", "target": "#name", "key": "Enter"},
        {"action": "scroll", "direction": "down", "pixels": 400},
    ):
        result = await browser.execute(**operation)
        assert not getattr(result, "is_error", False)
    screenshot = await browser.execute(action="screenshot", full_page=True)
    assert "[Attachment:" in str(screenshot)
    assert page.screenshot_paths
    assert Path(page.screenshot_paths[-1]).is_relative_to(tmp_path)


@pytest.mark.asyncio
async def test_browser_close_removes_session(browser: BrowserTool) -> None:
    result = await browser.execute(action="close")
    assert result == "Browser session closed."
    assert browser._sessions == {}


def test_browser_config_accepts_novita_render_fields() -> None:
    from nanobot.config.schema import ToolsConfig
    config = ToolsConfig(
        browser={
            "enable": True,
            "provider": "novita",
            "novitaApiKeyEnv": "NOVITA_API_KEY",
            "novitaTemplate": "browser-chromium",
            "novitaTimeoutSeconds": 600,
            "novitaBrowserPort": 9223,
            "actionTimeoutMs": 9000,
        }
    )
    assert config.browser.enable is True
    assert config.browser.provider == "novita"
    assert config.browser.novita_api_key_env == "NOVITA_API_KEY"
    assert config.browser.novita_template == "browser-chromium"
    assert config.browser.novita_browser_port == 9223
    assert config.browser.action_timeout_ms == 9000


def test_browser_rewrites_cdp_websocket_to_remote_host() -> None:
    assert BrowserTool._rewrite_websocket_url(
        "ws://localhost:9223/devtools/page/abc", "9223-sandbox.example.com"
    ) == "wss://9223-sandbox.example.com/devtools/page/abc"
