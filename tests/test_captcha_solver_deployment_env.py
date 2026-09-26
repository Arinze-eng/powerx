"""The solver as a *deployment* sees it: env spelling, and the inbuilt fallback.

Two gaps motivated these tests, and both looked like "the captcha solver does
not work" from outside the code:

* The root settings model reads ``NANOBOT_TOOLS__CAPTCHA_SOLVER__ENABLE``
  because it prefixes ``NANOBOT_`` and nests with ``__``, while operators export
  ``CAPTCHA_ENABLE`` / ``captcha_solver.provider``. A deployment could therefore
  be configured correctly and still run with the solver off.
* SolveGate's ``gate`` enum is two wide. A deployment that also carries a
  2captcha-compatible key should get "SolveGate first, then the inbuilt solver"
  rather than a dead end -- and when there is no inbuilt key, an error that says
  so and tells the model to keep going, instead of stopping the task.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from nanobot.agent.tools import captcha as captcha_module
from nanobot.agent.tools.captcha import CaptchaSolverTool, CaptchaSolverToolConfig
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ToolsConfig

_ENV_SPELLINGS = (
    "CAPTCHA_ENABLE",
    "CAPTCHA_SOLVER_ENABLE",
    "NANOBOT_CAPTCHA_ENABLE",
    "CAPTCHA_SOLVER_PROVIDER",
    "CAPTCHA_PROVIDER",
    "CAPTCHA_SOLVER.PROVIDER",
    "CAPSKIP_API_KEY",
    "CAPSOLVE_API_KEY",
    "CLOUDFLARE_WAF_API_KEY",
    "CAPTCHA_INBUILT_FALLBACK",
)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200


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

    async def post(self, url: str, data: Any = None, json: Any = None, headers: Any = None):
        _FakeClient.posts.append((url, dict(data or {})))
        return _FakeResponse(
            _FakeClient.post_replies.pop(0) if _FakeClient.post_replies else ""
        )

    async def get(self, url: str, params: Any = None):
        _FakeClient.gets.append((url, dict(params or {})))
        return _FakeResponse(
            _FakeClient.get_replies.pop(0) if _FakeClient.get_replies else ""
        )


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.posts.clear()
    _FakeClient.gets.clear()
    _FakeClient.post_replies.clear()
    _FakeClient.get_replies.clear()
    monkeypatch.setattr(captcha_module.httpx, "AsyncClient", _FakeClient)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(captcha_module.asyncio, "sleep", _no_sleep)
    for name in _ENV_SPELLINGS:
        monkeypatch.delenv(name, raising=False)


def _ctx(tmp_path, cfg: CaptchaSolverToolConfig) -> ToolContext:
    return ToolContext(config=ToolsConfig(captcha_solver=cfg), workspace=str(tmp_path))


# --- the deployment's own spelling ------------------------------------------


def test_the_operators_env_spelling_turns_the_solver_on(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`CAPTCHA_ENABLE=true` + `captcha_solver.provider=solvegate` must bind."""
    monkeypatch.setenv("CAPTCHA_ENABLE", "true")
    monkeypatch.setenv("CAPTCHA_SOLVER_PROVIDER", "solvegate")
    monkeypatch.setenv("CLOUDFLARE_WAF_API_KEY", "sk_test_probe")

    cfg = CaptchaSolverToolConfig()

    assert cfg.enable is True
    assert cfg.provider == "solvegate"

    registry = ToolRegistry()
    ToolLoader().load(_ctx(tmp_path, cfg), registry)
    assert "captcha_solver" in registry.tool_names


def test_the_env_spelling_beats_a_config_file_that_says_false() -> None:
    """A dumped default config carries `enable: false`; the live env is newer."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("CAPTCHA_ENABLE", "true")
        monkeypatch.setenv("CAPTCHA_SOLVER_PROVIDER", "solvegate")

        cfg = CaptchaSolverToolConfig(enable=False, provider="capsolve")

        assert cfg.enable is True
        assert cfg.provider == "solvegate"
    finally:
        monkeypatch.undo()


def test_an_env_that_says_nothing_leaves_every_field_alone() -> None:
    """The defaults stay the defaults when no deployment spelling is set."""
    cfg = CaptchaSolverToolConfig()

    assert cfg.enable is False
    assert cfg.provider == "capskip"
    assert cfg.base_url == "http://127.0.0.1:8080"
    assert cfg.solvegate_api_key_env == "CLOUDFLARE_WAF_API_KEY"


def test_a_config_file_setting_still_wins_where_the_env_is_only_a_fallback() -> None:
    """For the non-overriding fields the file is consulted first."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("SOLVEGATE_BASE_URL", "https://from-env.test")

        assert CaptchaSolverToolConfig(solvegate_base_url="https://from-file.test").solvegate_base_url == (
            "https://from-file.test"
        )
        assert CaptchaSolverToolConfig().solvegate_base_url == "https://from-env.test"
    finally:
        monkeypatch.undo()


# --- solvegate first, then the inbuilt solver -------------------------------


def _hybrid_tool() -> CaptchaSolverTool:
    return CaptchaSolverTool(
        base_url="http://inbuilt.test:8080",
        api_key="inbuilt-key",
        provider="solvegate",
        solvegate_base_url="https://api.solvegate.io",
        solvegate_api_key="gate-key",
    )


def test_a_deployment_running_both_advertises_the_union() -> None:
    """SolveGate's two gates plus everything the inbuilt client can answer."""
    actions = _hybrid_tool().answerable_actions

    assert {"turnstile", "waf", "recaptcha", "hcaptcha", "solve_image"} <= set(actions)


def test_a_recaptcha_on_a_hybrid_deployment_goes_to_the_inbuilt_solver() -> None:
    """SolveGate is tried first for its gates; the rest reaches the inbuilt one."""
    _FakeClient.post_replies.append("OK|t")
    _FakeClient.get_replies.append(json.dumps({"status": 1, "request": "tok"}))

    result = asyncio.run(
        _hybrid_tool().execute("recaptcha", sitekey="s", url="https://site.test/login")
    )

    assert "tok" in result
    # The inbuilt client's endpoint, never SolveGate's, and never its key.
    assert all(url.startswith("http://inbuilt.test:8080/") for url, _ in _FakeClient.posts)
    assert _FakeClient.posts[-1][1]["googlekey"] == "s"


def test_a_turnstile_on_a_hybrid_deployment_still_goes_to_solvegate() -> None:
    """SolveGate's own gate is answered by SolveGate, not by the inbuilt client."""
    tool = _hybrid_tool()
    _FakeClient.posts.clear()
    _FakeClient.post_replies.append(
        json.dumps({"status": "solved", "gate": "turnstile", "token": "0.gate-token"})
    )

    result = asyncio.run(tool.execute("turnstile", sitekey="0x4AAAA", url="https://site.test"))

    # The request went to SolveGate, and never to the inbuilt client's endpoint.
    assert [url for url, _ in _FakeClient.posts] == ["https://api.solvegate.io/v1/solve"]
    assert json.loads(str(result))["provider"] == "solvegate"
    assert json.loads(str(result))["token"] == "0.gate-token"


def test_the_solvegate_key_is_never_handed_to_the_inbuilt_client() -> None:
    """Two providers, two keys: pointing one at the other's endpoint is a leak."""
    tool = CaptchaSolverTool(
        base_url="http://inbuilt.test:8080",
        api_key="",
        provider="solvegate",
        solvegate_api_key="gate-key",
    )

    assert tool.api_key == ""
    assert tool.solvegate_api_key == "gate-key"
    # And with no inbuilt key, the enum is honestly just the two gates.
    assert tool.answerable_actions == ["turnstile", "waf"]


def test_solvegate_without_an_inbuilt_key_says_what_to_do_next() -> None:
    """The refusal is a provider limit plus the next move, not a dead end."""
    tool = CaptchaSolverTool(
        base_url="", api_key="", provider="solvegate", solvegate_api_key="gate-key"
    )

    result = str(asyncio.run(tool.execute("recaptcha", sitekey="s", url="https://site.test")))

    assert "turnstile, waf" in result
    assert "inbuilt fallback has no key" in result
    assert "continue with the rest of the task" in result


def test_the_inbuilt_fallback_can_be_switched_off() -> None:
    """An operator who wants SolveGate to be a hard boundary gets one."""
    tool = CaptchaSolverTool(
        base_url="http://inbuilt.test:8080",
        api_key="inbuilt-key",
        provider="solvegate",
        solvegate_api_key="gate-key",
        inbuilt_fallback=False,
    )

    assert tool.answerable_actions == ["turnstile", "waf"]
    result = str(asyncio.run(tool.execute("recaptcha", sitekey="s", url="https://site.test")))
    assert "inbuilt fallback is switched off" in result
