"""Tests for the captcha-solving tool.

The tool talks to a CapSkip / 2captcha-compatible solver over HTTP. These tests
pin the wire protocol (submit, poll, not-ready, error), the tool's argument to
field mapping, and the two safety properties the module claims: the solver
endpoint comes only from configuration, and image input stays inside the
workspace.
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.tools import captcha as captcha_module
from nanobot.agent.tools.captcha import (
    CaptchaSolver,
    CaptchaSolverTool,
    CaptchaSolverToolConfig,
    SolverBusyError,
    SolverError,
)
from nanobot.agent.tools.loader import ToolLoader
from nanobot.config.schema import Config


class _FakeResponse:
    """Minimal stand-in for an httpx response."""

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class _FakeClient:
    """Records outgoing requests and replays scripted replies."""

    posts: list[tuple[str, dict[str, Any]]] = []
    gets: list[tuple[str, dict[str, Any]]] = []
    post_replies: list[str] = []
    get_replies: list[str] = []

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def post(self, url: str, data: Any = None) -> _FakeResponse:
        _FakeClient.posts.append((url, dict(data or {})))
        reply = _FakeClient.post_replies.pop(0) if _FakeClient.post_replies else ""
        return _FakeResponse(reply)

    async def get(self, url: str, params: Any = None) -> _FakeResponse:
        _FakeClient.gets.append((url, dict(params or {})))
        reply = _FakeClient.get_replies.pop(0) if _FakeClient.get_replies else ""
        return _FakeResponse(reply)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route all HTTP through the fake client and remove polling delays."""
    _FakeClient.posts.clear()
    _FakeClient.gets.clear()
    _FakeClient.post_replies.clear()
    _FakeClient.get_replies.clear()
    monkeypatch.setattr(captcha_module.httpx, "AsyncClient", _FakeClient)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(captcha_module.asyncio, "sleep", _no_sleep)


def _ctx(cfg: CaptchaSolverToolConfig) -> SimpleNamespace:
    return SimpleNamespace(config=SimpleNamespace(captcha_solver=cfg), workspace=None)


def _fields(tool: CaptchaSolverTool, action: str, **overrides: Any) -> dict[str, Any]:
    """Call the tool's field mapper with every argument defaulted."""
    values: dict[str, Any] = {
        "image_path": None,
        "sitekey": None,
        "url": None,
        "version": None,
        "action_name": None,
        "enterprise": None,
        "invisible": None,
        "gt": None,
        "challenge": None,
        "api_server": None,
        "challenge_url": None,
        "data": None,
        "pagedata": None,
    }
    values.update(overrides)
    return tool._fields_for(action, **values)


# --- registration and gating ------------------------------------------------


def test_tool_is_discovered() -> None:
    """The loader must pick the tool up from the package scan."""
    names = {cls.__name__ for cls in ToolLoader().discover()}
    assert "CaptchaSolverTool" in names


def test_config_is_disabled_by_default() -> None:
    cfg = Config().tools.captcha_solver
    assert cfg.enable is False
    assert cfg.base_url == "http://127.0.0.1:8080"


def test_enabled_requires_opt_in_and_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPSKIP_API_KEY", raising=False)
    assert CaptchaSolverTool.enabled(_ctx(CaptchaSolverToolConfig())) is False
    assert CaptchaSolverTool.enabled(_ctx(CaptchaSolverToolConfig(enable=True))) is False

    monkeypatch.setenv("CAPSKIP_API_KEY", "from-env")
    assert CaptchaSolverTool.enabled(_ctx(CaptchaSolverToolConfig(enable=True))) is True
    assert (
        CaptchaSolverTool.enabled(
            _ctx(CaptchaSolverToolConfig(enable=True, api_key="literal-key"))
        )
        is True
    )


# --- wire protocol ----------------------------------------------------------


def test_submit_targets_the_configured_endpoint() -> None:
    _FakeClient.post_replies.append(json.dumps({"status": 1, "request": "task-1"}))
    solver = CaptchaSolver("http://solver.test", "secret-key")

    task_id = asyncio.run(
        solver.submit({"method": "userrecaptcha", "googlekey": "k", "pageurl": "https://a.test"})
    )

    assert task_id == "task-1"
    url, data = _FakeClient.posts[-1]
    assert url == "http://solver.test/in.php"
    assert data["key"] == "secret-key"
    assert data["json"] == 1
    assert data["method"] == "userrecaptcha"


def test_submit_accepts_the_plain_ok_form() -> None:
    _FakeClient.post_replies.append("OK|task-2")
    solver = CaptchaSolver("http://solver.test", "k")
    assert asyncio.run(solver.submit({"method": "turnstile"})) == "task-2"


def test_submit_surfaces_a_rejection() -> None:
    _FakeClient.post_replies.append(json.dumps({"status": 0, "request": "ERROR_KEY_DENIED"}))
    solver = CaptchaSolver("http://solver.test", "k")
    with pytest.raises(SolverError, match="ERROR_KEY_DENIED"):
        asyncio.run(solver.submit({"method": "turnstile"}))


def test_poll_reports_busy_until_the_answer_exists() -> None:
    _FakeClient.get_replies.extend(
        [
            json.dumps({"status": 0, "request": "CAPCHA_NOT_READY"}),
            json.dumps({"status": 1, "request": "token-xyz"}),
        ]
    )
    solver = CaptchaSolver("http://solver.test", "k")

    with pytest.raises(SolverBusyError):
        asyncio.run(solver.poll("task-1"))

    assert asyncio.run(solver.poll("task-1")) == "token-xyz"
    url, params = _FakeClient.gets[-1]
    assert url == "http://solver.test/res.php"
    assert params["action"] == "get"
    assert params["id"] == "task-1"


def test_solve_keeps_polling_across_an_empty_body() -> None:
    """CapSkip answers with an empty body before it starts saying NOT_READY."""
    _FakeClient.post_replies.append("OK|task-9")
    _FakeClient.get_replies.extend(
        ["", "CAPCHA_NOT_READY", json.dumps({"status": 1, "request": "final-token"})]
    )
    solver = CaptchaSolver("http://solver.test", "k")

    token = asyncio.run(solver.solve({"method": "turnstile"}, timeout=5, poll_interval=0.01))

    assert token == "final-token"
    assert len(_FakeClient.gets) == 3


def test_solve_surfaces_a_solver_failure() -> None:
    _FakeClient.post_replies.append("OK|task-1")
    _FakeClient.get_replies.append(json.dumps({"status": 0, "request": "ERROR_ZERO_BALANCE"}))
    solver = CaptchaSolver("http://solver.test", "k")

    with pytest.raises(SolverError, match="ERROR_ZERO_BALANCE"):
        asyncio.run(solver.solve({"method": "turnstile"}, timeout=5, poll_interval=0.01))


# --- field mapping ----------------------------------------------------------


def test_recaptcha_v3_sends_version_and_action() -> None:
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k")
    fields = _fields(
        tool,
        "recaptcha",
        sitekey="site-key",
        url="https://example.com",
        version="v3",
        action_name="submit",
    )
    assert fields["method"] == "userrecaptcha"
    assert fields["googlekey"] == "site-key"
    assert fields["pageurl"] == "https://example.com"
    assert fields["version"] == "v3"
    assert fields["action"] == "submit"


def test_recaptcha_v2_omits_v3_only_fields() -> None:
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k")
    fields = _fields(tool, "recaptcha", sitekey="site-key", url="https://example.com")
    assert fields["method"] == "userrecaptcha"
    assert "version" not in fields
    assert "action" not in fields


def test_turnstile_sends_sitekey_and_pageurl() -> None:
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k")
    fields = _fields(tool, "turnstile", sitekey="ts-key", url="https://example.com")
    assert fields == {
        "method": "turnstile",
        "sitekey": "ts-key",
        "pageurl": "https://example.com",
    }


def test_geetest_requires_gt_and_challenge() -> None:
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k")
    with pytest.raises(ValueError, match="gt"):
        _fields(tool, "geetest", url="https://example.com")
    fields = _fields(
        tool, "geetest", gt="gt-value", challenge="challenge-value", url="https://example.com"
    )
    assert fields["method"] == "geetest"
    assert fields["gt"] == "gt-value"


def test_recaptcha_requires_sitekey_and_url() -> None:
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k")
    with pytest.raises(ValueError, match="sitekey"):
        _fields(tool, "recaptcha", url="https://example.com")
    with pytest.raises(ValueError, match="url"):
        _fields(tool, "recaptcha", sitekey="site-key")


def test_unknown_action_returns_a_tool_error() -> None:
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k")
    result = asyncio.run(tool.execute("teleport"))
    assert result.is_error


# --- safety -----------------------------------------------------------------


def test_no_argument_can_redirect_the_solver_endpoint() -> None:
    """The solver host is fixed by config; a hostile page URL cannot move it.

    'url' is the page the captcha sits on -- solver metadata, never a request
    target. This is the property that stops the model aiming the solver at an
    internal service.
    """
    _FakeClient.post_replies.append("OK|t")
    _FakeClient.get_replies.append(json.dumps({"status": 1, "request": "tok"}))
    tool = CaptchaSolverTool(
        base_url="http://solver.internal:8080",
        api_key="k",
        timeout_seconds=10,
        poll_interval_seconds=0.01,
    )

    result = asyncio.run(
        tool.execute(
            "recaptcha",
            sitekey="s",
            url="http://169.254.169.254/latest/meta-data/",
        )
    )

    assert "tok" in result
    for url, _ in _FakeClient.posts + _FakeClient.gets:
        assert url.startswith("http://solver.internal:8080/")
    assert _FakeClient.posts[-1][1]["pageurl"] == "http://169.254.169.254/latest/meta-data/"


def test_image_within_the_workspace_is_base64_encoded(tmp_path) -> None:
    image = tmp_path / "captcha.png"
    image.write_bytes(b"PNGDATA")
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k", workspace=tmp_path)

    fields = _fields(tool, "solve_image", image_path="captcha.png")

    assert fields["method"] == "base64"
    assert base64.b64decode(fields["body"]) == b"PNGDATA"


def test_image_path_cannot_escape_the_workspace(tmp_path) -> None:
    outside = tmp_path.parent / "secret.png"
    outside.write_bytes(b"SECRET")
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k", workspace=tmp_path)

    with pytest.raises(ValueError, match="inside the agent workspace"):
        _fields(tool, "solve_image", image_path=str(outside))
    with pytest.raises(ValueError, match="inside the agent workspace"):
        _fields(tool, "solve_image", image_path="../secret.png")


def test_oversize_image_is_rejected(tmp_path) -> None:
    image = tmp_path / "big.png"
    image.write_bytes(b"a" * 4096)
    tool = CaptchaSolverTool(
        base_url="http://solver.test", api_key="k", workspace=tmp_path, max_image_bytes=1024
    )

    with pytest.raises(ValueError, match="limit"):
        _fields(tool, "solve_image", image_path="big.png")


def test_missing_image_is_rejected(tmp_path) -> None:
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k", workspace=tmp_path)
    with pytest.raises(ValueError, match="does not exist"):
        _fields(tool, "solve_image", image_path="absent.png")
