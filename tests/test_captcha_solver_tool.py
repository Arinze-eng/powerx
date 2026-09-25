"""Tests for the captcha-solving tool.

The tool talks to a CapSkip / 2captcha-compatible solver over HTTP. These tests
pin the wire protocol (submit, poll, not-ready, error), the tool's argument to
field mapping, and the two safety properties the module claims: the solver
endpoint comes only from configuration, and image input stays inside the
workspace.
"""

from __future__ import annotations

import asyncio
import os
import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.tools import captcha as captcha_module
from nanobot.agent.tools.captcha import (
    CaptchaSolver,
    SolveGateSolver,
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
    json_posts: list[tuple[str, Any, dict[str, Any]]] = []
    gets: list[tuple[str, dict[str, Any]]] = []
    post_replies: list[str] = []
    get_replies: list[str] = []

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def post(
        self, url: str, data: Any = None, json: Any = None, headers: Any = None
    ) -> _FakeResponse:
        _FakeClient.posts.append((url, dict(data or {})))
        if json is not None:
            _FakeClient.json_posts.append((url, json, dict(headers or {})))
        reply = _FakeClient.post_replies.pop(0) if _FakeClient.post_replies else ""
        if isinstance(reply, tuple):
            return _FakeResponse(reply[0], reply[1])
        return _FakeResponse(reply)

    async def get(self, url: str, params: Any = None) -> _FakeResponse:
        _FakeClient.gets.append((url, dict(params or {})))
        reply = _FakeClient.get_replies.pop(0) if _FakeClient.get_replies else ""
        return _FakeResponse(reply)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route all HTTP through the fake client and remove polling delays."""
    _FakeClient.posts.clear()
    _FakeClient.json_posts.clear()
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
        "publickey": None,
        "surl": None,
        "text": None,
        "min_score": None,
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


# --- the wider solver surface -----------------------------------------------


def test_hcaptcha_sends_the_sitekey_and_page() -> None:
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k")
    fields = _fields(tool, "hcaptcha", sitekey="hc-key", url="https://example.com")
    assert fields == {
        "method": "hcaptcha",
        "sitekey": "hc-key",
        "pageurl": "https://example.com",
    }


def test_hcaptcha_marks_an_invisible_widget() -> None:
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k")
    fields = _fields(
        tool, "hcaptcha", sitekey="hc-key", url="https://example.com", invisible=True
    )
    assert fields["invisible"] == 1


def test_funcaptcha_requires_a_publickey() -> None:
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k")
    with pytest.raises(ValueError, match="publickey"):
        _fields(tool, "funcaptcha", url="https://example.com")


def test_funcaptcha_carries_an_optional_service_url() -> None:
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k")
    fields = _fields(
        tool,
        "funcaptcha",
        publickey="pk-1",
        url="https://example.com",
        surl="https://client-api.arkoselabs.com",
    )
    assert fields["method"] == "funcaptcha"
    assert fields["publickey"] == "pk-1"
    assert fields["surl"] == "https://client-api.arkoselabs.com"


def test_coordinates_needs_both_an_image_and_an_instruction(tmp_path) -> None:
    """A grid challenge cannot be answered without being told the task."""
    image = tmp_path / "grid.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    tool = CaptchaSolverTool(
        base_url="http://solver.test", api_key="k", workspace=tmp_path
    )

    with pytest.raises(ValueError, match="text"):
        _fields(tool, "coordinates", image_path=str(image))

    fields = _fields(
        tool, "coordinates", image_path=str(image), text="click the traffic lights"
    )
    assert fields["method"] == "base64"
    assert fields["coordinates"] == 1
    assert fields["textinstructions"] == "click the traffic lights"
    assert fields["body"]


def test_solve_image_passes_an_optional_instruction(tmp_path) -> None:
    image = tmp_path / "text.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    tool = CaptchaSolverTool(
        base_url="http://solver.test", api_key="k", workspace=tmp_path
    )
    fields = _fields(tool, "solve_image", image_path=str(image), text="type the letters")
    assert fields["textinstructions"] == "type the letters"


def test_recaptcha_v3_clamps_the_score_threshold() -> None:
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k")
    high = _fields(
        tool,
        "recaptcha",
        sitekey="sk",
        url="https://example.com",
        version="v3",
        min_score=1.0,
    )
    assert high["min_score"] == 0.9

    low = _fields(
        tool,
        "recaptcha",
        sitekey="sk",
        url="https://example.com",
        version="v3",
        min_score=0.0,
    )
    assert low["min_score"] == 0.1


def test_recaptcha_v2_ignores_the_score_threshold() -> None:
    tool = CaptchaSolverTool(base_url="http://solver.test", api_key="k")
    fields = _fields(
        tool, "recaptcha", sitekey="sk", url="https://example.com", min_score=0.7
    )
    assert "min_score" not in fields


def test_submit_retries_a_server_error_then_succeeds() -> None:
    """A solve is paid for on submit, so a 5xx must not lose the task."""
    _FakeClient.post_replies.extend([("upstream exploded", 502), "OK|task-retry"])
    solver = CaptchaSolver("http://solver.test", "k")

    assert asyncio.run(solver.submit({"method": "turnstile"})) == "task-retry"
    assert len(_FakeClient.posts) == 2


def test_submit_gives_up_after_the_retry_budget() -> None:
    _FakeClient.post_replies.extend(
        [("down", 503), ("down", 503), ("down", 503), ("down", 503)]
    )
    solver = CaptchaSolver("http://solver.test", "k")

    with pytest.raises(SolverError, match="503"):
        asyncio.run(solver.submit({"method": "turnstile"}))
    assert len(_FakeClient.posts) == 3


def test_submit_does_not_retry_a_rejected_task() -> None:
    """A bad key or bad fields fails identically on retry, and costs a call."""
    _FakeClient.post_replies.append(json.dumps({"status": 0, "request": "ERROR_KEY_DENIED"}))
    solver = CaptchaSolver("http://solver.test", "k")

    with pytest.raises(SolverError, match="ERROR_KEY_DENIED"):
        asyncio.run(solver.submit({"method": "turnstile"}))
    assert len(_FakeClient.posts) == 1


# --------------------------------------------------------------------------
# SolveGate provider
#
# The protocol below was read off the live API: it takes a JSON body, wants a
# bearer token rather than a key field, accepts only the gates "turnstile" and
# "waf", answers synchronously with a token, and reports a test key as
# mode="sandbox". Each of those is pinned here so a change on either side is
# visible rather than silent.
# --------------------------------------------------------------------------


def _solvegate_tool(**overrides: Any) -> CaptchaSolverTool:
    options: dict[str, Any] = {
        "base_url": "http://unused.test",
        "api_key": "",
        "provider": "solvegate",
        "solvegate_base_url": "https://api.solvegate.io",
        "solvegate_api_key": "sg-test-key",
    }
    options.update(overrides)
    return CaptchaSolverTool(**options)


def _solvegate_reply(**overrides: Any) -> str:
    payload = {
        "id": "slv_wi4jrI6i7OTc",
        "status": "solved",
        "gate": "turnstile",
        "token": "tok_real",
        "solve_ms": 5,
        "expires_at": 1790336814,
        "mode": "live",
        "meter": "live",
        "billed": True,
        "error_code": None,
        "error_message": None,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_solvegate_posts_json_with_a_bearer_token() -> None:
    _FakeClient.post_replies.append(_solvegate_reply())
    solver = SolveGateSolver("https://api.solvegate.io", "sg-test-key")

    asyncio.run(solver.solve({"gate": "turnstile", "sitekey": "sk", "url": "https://a.test"}))

    url, body, headers = _FakeClient.json_posts[-1]
    assert url == "https://api.solvegate.io/v1/solve"
    assert headers["Authorization"] == "Bearer sg-test-key"
    assert body == {"gate": "turnstile", "sitekey": "sk", "url": "https://a.test"}
    # The documented curl -d form body answers 415, so it must not be used.
    assert _FakeClient.posts[-1][1] == {}


def test_solvegate_reads_the_token_and_timing() -> None:
    _FakeClient.post_replies.append(_solvegate_reply())
    solver = SolveGateSolver("https://api.solvegate.io", "k")

    result = asyncio.run(solver.solve({"gate": "turnstile"}))

    assert result["token"] == "tok_real"
    assert result["gate"] == "turnstile"
    assert result["id"] == "slv_wi4jrI6i7OTc"
    assert result["solve_ms"] == 5
    assert result["sandbox"] is False
    assert result["billed"] is True


def test_solvegate_flags_a_test_key_token_as_sandbox() -> None:
    """A sandbox token passes nothing; calling it solved would be a lie."""
    _FakeClient.post_replies.append(
        _solvegate_reply(
            token="SANDBOX.MBK0lokTZzqW811xWZi0pH0x_sandbox",
            mode="sandbox",
            meter="sandbox",
            billed=False,
        )
    )
    solver = SolveGateSolver("https://api.solvegate.io", "k")

    result = asyncio.run(solver.solve({"gate": "turnstile"}))

    assert result["sandbox"] is True
    assert result["billed"] is False


def test_solvegate_flags_a_sandbox_prefix_even_without_the_mode_field() -> None:
    _FakeClient.post_replies.append(
        _solvegate_reply(token="SANDBOX.abc", mode=None)
    )
    solver = SolveGateSolver("https://api.solvegate.io", "k")
    assert asyncio.run(solver.solve({"gate": "turnstile"}))["sandbox"] is True


def test_solvegate_reports_a_rejected_key() -> None:
    _FakeClient.post_replies.append(
        (json.dumps({"error": {"code": "invalid_key", "message": "Missing or revoked API key.",
                              "billed": False}}), 401)
    )
    solver = SolveGateSolver("https://api.solvegate.io", "k")

    with pytest.raises(SolverError, match="invalid_key"):
        asyncio.run(solver.solve({"gate": "turnstile"}))
    # A revoked key fails identically on retry, so it must not be retried.
    assert len(_FakeClient.json_posts) == 1


def test_solvegate_reports_a_refused_task() -> None:
    _FakeClient.post_replies.append(
        (json.dumps({"error": {"code": "bad_request", "message": "Required", "billed": False}}), 400)
    )
    solver = SolveGateSolver("https://api.solvegate.io", "k")

    with pytest.raises(SolverError, match="bad_request: Required"):
        asyncio.run(solver.solve({"gate": "turnstile"}))
    assert len(_FakeClient.json_posts) == 1


def test_solvegate_names_the_encoding_problem_on_415() -> None:
    _FakeClient.post_replies.append(("", 415))
    solver = SolveGateSolver("https://api.solvegate.io", "k")

    with pytest.raises(SolverError, match="JSON body"):
        asyncio.run(solver.solve({"gate": "turnstile"}))


def test_solvegate_retries_a_server_error() -> None:
    _FakeClient.post_replies.extend([("upstream exploded", 502), _solvegate_reply()])
    solver = SolveGateSolver("https://api.solvegate.io", "k")

    assert asyncio.run(solver.solve({"gate": "turnstile"}))["token"] == "tok_real"
    assert len(_FakeClient.json_posts) == 2


def test_solvegate_reports_a_failed_solve_with_its_reason() -> None:
    _FakeClient.post_replies.append(
        _solvegate_reply(status="failed", token=None, error_code="challenge_unavailable",
                         error_message="the widget was not reachable")
    )
    solver = SolveGateSolver("https://api.solvegate.io", "k")

    with pytest.raises(SolverError, match="the widget was not reachable"):
        asyncio.run(solver.solve({"gate": "turnstile"}))


def test_solvegate_rejects_an_unreadable_body() -> None:
    _FakeClient.post_replies.append("<html>gateway timeout</html>")
    solver = SolveGateSolver("https://api.solvegate.io", "k")

    with pytest.raises(SolverError, match="unreadable JSON"):
        asyncio.run(solver.solve({"gate": "turnstile"}))


# --- through the tool -------------------------------------------------------


def test_tool_sends_gate_turnstile_for_a_turnstile_action() -> None:
    _FakeClient.post_replies.append(_solvegate_reply())
    tool = _solvegate_tool()

    payload = json.loads(
        asyncio.run(
            tool.execute("turnstile", sitekey="0x4AAA", url="https://example.com")
        )
    )

    assert payload["provider"] == "solvegate"
    assert payload["token"] == "tok_real"
    assert payload["sandbox"] is False
    body = _FakeClient.json_posts[-1][1]
    assert body["gate"] == "turnstile"
    assert body["sitekey"] == "0x4AAA"
    assert body["url"] == "https://example.com"


def test_tool_sends_gate_waf_for_the_waf_action() -> None:
    _FakeClient.post_replies.append(_solvegate_reply(gate="waf"))
    tool = _solvegate_tool()

    payload = json.loads(
        asyncio.run(tool.execute("waf", sitekey="0x4AAA", url="https://example.com"))
    )

    assert payload["captcha_type"] == "waf"
    assert _FakeClient.json_posts[-1][1]["gate"] == "waf"


def test_tool_marks_a_sandbox_solve_as_a_test_token() -> None:
    _FakeClient.post_replies.append(
        _solvegate_reply(token="SANDBOX.x", mode="sandbox", billed=False)
    )
    tool = _solvegate_tool()

    payload = json.loads(
        asyncio.run(tool.execute("turnstile", sitekey="sk", url="https://example.com"))
    )

    assert payload["sandbox"] is True
    assert "will not accept it" in payload["note"]


def test_tool_requires_a_url_because_solvegate_does() -> None:
    tool = _solvegate_tool()
    out = asyncio.run(tool.execute("turnstile", sitekey="sk"))
    assert "Error" in out and "url" in out
    assert _FakeClient.json_posts == []


def test_tool_requires_a_sitekey_because_solvegate_does() -> None:
    tool = _solvegate_tool()
    out = asyncio.run(tool.execute("turnstile", url="https://example.com"))
    assert "Error" in out and "sitekey" in out
    assert _FakeClient.json_posts == []


def test_tool_refuses_a_gate_solvegate_does_not_have() -> None:
    """recaptcha is a real action, but not one this provider can serve."""
    tool = _solvegate_tool()
    out = asyncio.run(
        tool.execute("recaptcha", sitekey="sk", url="https://example.com")
    )
    assert "Error" in out and "turnstile, waf" in out
    assert _FakeClient.json_posts == []


def test_tool_reports_a_missing_solvegate_key_without_calling_out() -> None:
    tool = _solvegate_tool(solvegate_api_key="")
    out = asyncio.run(
        tool.execute("turnstile", sitekey="sk", url="https://example.com")
    )
    assert "Error" in out and "no API key" in out
    assert _FakeClient.json_posts == []


def test_tool_surfaces_a_revoked_key_as_a_tool_error() -> None:
    _FakeClient.post_replies.append(
        (json.dumps({"error": {"code": "invalid_key", "message": "revoked", "billed": False}}), 401)
    )
    tool = _solvegate_tool()
    out = asyncio.run(tool.execute("waf", sitekey="sk", url="https://example.com"))
    assert "Error" in out and "invalid_key" in out


def test_no_argument_can_redirect_the_solvegate_endpoint() -> None:
    """The endpoint stays pinned to configuration, as it is for CapSkip."""
    _FakeClient.post_replies.append(_solvegate_reply())
    tool = _solvegate_tool()

    asyncio.run(
        tool.execute(
            "turnstile",
            sitekey="sk",
            url="https://example.com",
            api_server="https://evil.test",
            challenge_url="https://evil.test",
        )
    )

    assert _FakeClient.json_posts[-1][0] == "https://api.solvegate.io/v1/solve"


def test_the_advertised_actions_include_waf() -> None:
    """The model can only call what the schema lists, so waf must be listed."""
    tool = _solvegate_tool()
    assert "waf" in CaptchaSolverTool._ACTIONS
    assert "waf" in tool.parameters["properties"]["action"]["enum"]


