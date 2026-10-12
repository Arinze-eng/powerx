"""The worker, driven in-process with a fake loop.

What matters here is the contract, not the model: the events come out in the
order a streaming host can consume, a turn that cannot even be built is reported
rather than raised, and a turn that produced nothing is still a finished turn.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent import runner as runner_module
from nanobot.agent.runner import AgentRunResult
from nanobot.remote_turn.protocol import ProviderCredentials, RemoteTurnEvent, RemoteTurnRequest
from nanobot.remote_turn.worker import LocalLoop, _config_for, build_loop, run_turn


class _Provider:
    def __init__(self) -> None:
        self.generation = SimpleNamespace(
            temperature=0.1, max_tokens=1024, reasoning_effort=None
        )
        self.supports_progress_deltas = True


def _request(**overrides: Any) -> RemoteTurnRequest:
    payload: dict[str, Any] = {
        "session_key": "telegram:1",
        "messages": [{"role": "user", "content": "hi"}],
        "credentials": ProviderCredentials(
            provider="deepseek", model="deepseek-chat", api_key="sk-secret"
        ),
    }
    payload.update(overrides)
    return RemoteTurnRequest(**payload)


def _loop() -> LocalLoop:
    return LocalLoop(provider=_Provider(), tools=SimpleNamespace(get_definitions=lambda: []))


def _fake_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    deltas: tuple[str, ...] = (),
    tools: tuple[str, ...] = (),
    final: str = "done",
    messages: list[dict[str, Any]] | None = None,
    stop_reason: str = "completed",
) -> None:
    async def _run(self: Any, spec: Any) -> AgentRunResult:
        for delta in deltas:
            await spec.progress_callback(delta)
        for name in tools:
            await spec.hook.before_execute_tools(
                SimpleNamespace(tool_calls=[SimpleNamespace(name=name)])
            )
        return AgentRunResult(
            final_content=final,
            messages=messages if messages is not None else [{"role": "assistant", "content": final}],
            tools_used=list(tools),
            usage={"input_tokens": 1, "output_tokens": 2},
            stop_reason=stop_reason,
        )

    monkeypatch.setattr(runner_module.AgentRunner, "run", _run)


async def test_events_arrive_in_consumable_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, deltas=("he", "llo"), tools=("exec",))
    events: list[RemoteTurnEvent] = []

    async def _emit(event: RemoteTurnEvent) -> None:
        events.append(event)

    final = await run_turn(_request(), emit=_emit, loop_factory=lambda _r: _loop())

    assert [(e.kind, e.text) for e in events] == [
        ("delta", "he"),
        ("delta", "llo"),
        ("tool", "exec"),
        ("final", "done"),
    ]
    assert final.kind == "final"
    assert final.data["stop_reason"] == "completed"
    assert final.data["tools_used"] == ["exec"]
    assert final.data["usage"] == {"input_tokens": 1, "output_tokens": 2}


async def test_the_transcript_comes_home_with_the_result(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "done"}]
    _fake_run(monkeypatch, messages=messages)
    events: list[RemoteTurnEvent] = []

    async def _emit(event: RemoteTurnEvent) -> None:
        events.append(event)

    await run_turn(_request(), emit=_emit, loop_factory=lambda _r: _loop())

    assert events[-1].data["messages"] == messages


async def test_a_turn_that_cannot_be_built_is_reported_not_raised() -> None:
    events: list[RemoteTurnEvent] = []

    async def _emit(event: RemoteTurnEvent) -> None:
        events.append(event)

    def _boom(_request: RemoteTurnRequest) -> Any:
        raise RuntimeError("no api key in this sandbox")

    final = await run_turn(_request(), emit=_emit, loop_factory=_boom)

    assert final.kind == "error"
    assert "no api key in this sandbox" in final.text
    assert [e.kind for e in events] == ["error"]


async def test_a_turn_that_dies_midway_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run(self: Any, spec: Any) -> AgentRunResult:
        raise RuntimeError("provider returned 500")

    monkeypatch.setattr(runner_module.AgentRunner, "run", _run)
    events: list[RemoteTurnEvent] = []

    async def _emit(event: RemoteTurnEvent) -> None:
        events.append(event)

    final = await run_turn(_request(), emit=_emit, loop_factory=lambda _r: _loop())

    assert final.kind == "error"
    assert "provider returned 500" in final.text


async def test_an_empty_answer_is_a_finished_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    # Not an error: the host must not re-run a turn that legitimately said nothing.
    _fake_run(monkeypatch, final="", messages=[])
    events: list[RemoteTurnEvent] = []

    async def _emit(event: RemoteTurnEvent) -> None:
        events.append(event)

    final = await run_turn(_request(), emit=_emit, loop_factory=lambda _r: _loop())

    assert final.kind == "final"
    assert final.text == ""


def test_the_admin_selection_overrides_a_stale_sandbox_config() -> None:
    config = _config_for(
        _request(
            credentials=ProviderCredentials(
                provider="openrouter",
                model="anthropic/claude-sonnet-4",
                api_key="sk-or-1",
                api_base="https://example.test/v1",
            )
        )
    )

    assert config.agents.defaults.provider == "openrouter"
    assert config.agents.defaults.model == "anthropic/claude-sonnet-4"
    # A named preset would outrank the fields above, so it must be cleared.
    assert config.agents.defaults.model_preset is None
    assert config.providers.openrouter.api_key == "sk-or-1"
    assert config.providers.openrouter.api_base == "https://example.test/v1"


def test_the_sandbox_workspace_comes_from_the_host() -> None:
    config = _config_for(_request(workspace="/workspace/telegram-42"))

    assert config.agents.defaults.workspace == "/workspace/telegram-42"


def test_build_loop_composes_the_real_agent() -> None:
    # The one test that touches the real composition root: a sandbox turn must
    # get the host's provider and the host's tool set, or a turn would behave
    # differently depending on where it ran.
    loop = build_loop(_request(workspace="/tmp/powerx-remote-turn-test"))

    assert loop.provider is not None
    definitions = loop.tools.get_definitions()
    assert definitions
    assert any(d.get("function", {}).get("name") == "exec" for d in definitions)
