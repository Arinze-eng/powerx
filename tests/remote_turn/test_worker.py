"""The worker, driven in-process with a fake loop.

What matters here is the contract, not the model: the events come out in the
order a streaming host can consume, a turn that cannot even be built is reported
rather than raised, and a turn that produced nothing is still a finished turn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent import runner as runner_module
from nanobot.agent.runner import AgentRunResult
from nanobot.remote_turn.protocol import ProviderCredentials, RemoteTurnEvent, RemoteTurnRequest
from nanobot.remote_turn.worker import (
    ADMIN_LANE_ENV_VAR,
    LocalLoop,
    _config_for,
    apply_pool,
    build_loop,
    run_turn,
)


@pytest.fixture(autouse=True)
def _clean_pool_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this file inherits a pool from the one before it."""
    from nanobot.provider_pool import POOL_ENV_VAR

    for name in (POOL_ENV_VAR, "PROVIDER_POOL_PATH", ADMIN_LANE_ENV_VAR):
        monkeypatch.delenv(name, raising=False)


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


def test_the_sandbox_workspace_comes_from_the_host(tmp_path: Any) -> None:
    # It used to be an unconditional copy, which is how a host path -- meaningful
    # only to the host -- reached the sandbox and blew up on permission denied. The
    # dispatcher now sends the sandbox's own root; this asserts the worker applies
    # a workspace it is actually given, on a directory this box can use.
    wanted = tmp_path / "telegram-42"

    config = _config_for(_request(workspace=str(wanted)))

    assert config.agents.defaults.workspace == str(wanted)


def test_an_unusable_workspace_does_not_override_the_sandbox_default() -> None:
    # A path the worker cannot open must not be written into the config: leaving
    # the sandbox's own value in place is what keeps the turn running in the
    # sandbox instead of failing back to the host.
    config = _config_for(_request(workspace="/proc/definitely-not-writable/x"))

    assert config.agents.defaults.workspace != "/proc/definitely-not-writable/x"


_LANES = [
    {
        "id": "lane-a",
        "baseUrl": "https://one.test/v1",
        "apiKey": "key-one",
        "model": "nvidia/nemotron-3-super-120b-a12b",
        "label": "one",
        "enabled": True,
    },
    {
        "id": "lane-b",
        "baseUrl": "https://two.test/v1",
        "apiKey": "key-two",
        "model": "nvidia/nemotron-3-super-120b-a12b",
        "label": "two",
        "enabled": True,
    },
]


def test_the_pool_is_installed_where_the_project_expects_to_find_it() -> None:
    from nanobot.provider_pool import POOL_ENV_VAR, load_pool

    installed = apply_pool(_request(pool=list(_LANES)))

    assert installed == 2
    assert json.loads(os.environ[POOL_ENV_VAR])["entries"][0]["id"] == "lane-a"
    # The file is pointed at explicitly because load_pool prefers it over the
    # env var, and a stale file from an earlier turn would otherwise win.
    assert Path(os.environ["PROVIDER_POOL_PATH"]).exists()
    assert [e["id"] for e in load_pool()] == ["lane-a", "lane-b"]


def test_no_pool_in_the_request_changes_nothing() -> None:
    from nanobot.provider_pool import POOL_ENV_VAR

    os.environ.pop(POOL_ENV_VAR, None)

    assert apply_pool(_request()) == 0
    assert POOL_ENV_VAR not in os.environ


def test_a_pooled_turn_gets_the_real_rotating_provider() -> None:
    # The point of carrying the pool: the sandbox must rotate over the *same*
    # lanes the host does. Not one lane of it, and not one lane more -- the
    # turn's credentials name a provider this sandbox can build, so a lane it
    # added by itself would be an endpoint and a key nobody chose for this turn.
    loop = build_loop(
        _request(
            workspace="/tmp/powerx-remote-turn-pool-test",
            pool=list(_LANES),
        )
    )

    assert type(loop.provider).__name__ == "PoolProvider"
    assert [entry["id"] for entry, _ in loop.provider.lanes] == ["lane-a", "lane-b"]


def test_the_switch_that_keeps_the_sandboxs_own_lane_out_is_what_does_it() -> None:
    # Guards the test above against passing for the wrong reason: the ambient
    # lane is real, and it is the switch -- not an unbuildable config -- that
    # keeps it out of the rotation.
    from nanobot.providers.factory import make_provider

    request = _request(pool=list(_LANES))
    apply_pool(request)
    config = _config_for(request)

    assert [e["id"] for e, _ in make_provider(config).lanes] == ["lane-a", "lane-b"]

    os.environ.pop(ADMIN_LANE_ENV_VAR, None)

    assert [e["id"] for e, _ in make_provider(config).lanes] == [
        "lane-a",
        "lane-b",
        "admin-main",
    ]


def test_the_worker_survives_a_workspace_it_cannot_use(tmp_path: Any) -> None:
    # The dispatcher rewrites the path, but a backend that exposes no root, an
    # operator-pinned request, or a read-only mount can still deliver a directory
    # the worker cannot open. That used to raise straight out of build_loop and
    # fail the turn; now it degrades to the sandbox's own default.
    from nanobot.remote_turn.worker import _usable_workspace

    good = tmp_path / "workspace"
    assert _usable_workspace(str(good)) == str(good)
    assert good.is_dir(), "the workspace is created if the sandbox lacks it"

    # A path *through* a regular file cannot be mkdir'd for anyone, root included,
    # so this exercises the failure without depending on the uid the suite runs as.
    obstacle = tmp_path / "afile"
    obstacle.write_text("not a directory", encoding="utf-8")
    assert _usable_workspace(str(obstacle / "nested")) is None

    assert _usable_workspace(None) is None
    assert _usable_workspace("") is None


def test_build_loop_composes_the_real_agent() -> None:
    # The one test that touches the real composition root: a sandbox turn must
    # get the host's provider and the host's tool set, or a turn would behave
    # differently depending on where it ran.
    loop = build_loop(_request(workspace="/tmp/powerx-remote-turn-test"))

    assert loop.provider is not None
    definitions = loop.tools.get_definitions()
    assert definitions
    assert any(d.get("function", {}).get("name") == "exec" for d in definitions)


def test_the_event_hook_satisfies_the_runners_whole_contract() -> None:
    """Guards the bug that made every failing turn look like a broken worker.

    ``_EventHook`` implemented only the two methods this file calls by hand.
    ``AgentRunner`` calls more than that, and its ``on_error`` fires on every
    failed provider call -- so a bad API key, a rate limit or a 5xx raised
    ``AttributeError`` inside the worker instead of reporting the failure, and
    the host saw a crashed worker where it should have seen a fallback.

    Asserting the methods exist is the point: this is a duck-typing contract with
    a caller in another module, so the only thing that keeps it from regressing is
    a test that enumerates the surface.
    """
    from nanobot.remote_turn.worker import _EventHook

    hook = _EventHook(emit=None)
    for name in (
        "before_run",
        "after_run",
        "on_error",
        "on_finally",
        "before_iteration",
        "after_iteration",
        "on_stream",
        "on_stream_end",
        "on_provider_tool_event",
        "before_execute_tools",
        "before_execute_tool",
        "after_execute_tool",
        "wants_streaming",
        "finalize_content",
        "emit_reasoning",
    ):
        assert hasattr(hook, name), f"_EventHook is missing {name}"


async def test_a_provider_failure_comes_back_as_an_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The behavioural half of the test above: a failed turn is reported, not
    # raised, so the host falls back instead of losing the turn.
    async def _boom(self: Any, spec: Any) -> AgentRunResult:
        await spec.hook.on_error(
            SimpleNamespace(error="Error: bad api key", exception=None)
        )
        raise RuntimeError("Error: bad api key")

    monkeypatch.setattr(runner_module.AgentRunner, "run", _boom)
    events: list[RemoteTurnEvent] = []

    async def _emit(event: RemoteTurnEvent) -> None:
        events.append(event)

    final = await run_turn(_request(), emit=_emit, loop_factory=lambda _r: _loop())

    assert final.kind == "error"
    assert any(e.kind == "error" and "bad api key" in e.text for e in events)
    # The hook must not swallow the failure into an apparent success.
    assert all(e.kind != "final" for e in events)
