"""The loop's half of the contract: move the turn, or run it exactly as before.

The important test in this file is the first one. Everything else is about the
sandbox path being correct; that one is about the sandbox path being *absent*
when the switch is off, because the deployment in production today is the switch
being off.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.remote_turn import dispatcher as dispatcher_module
from nanobot.remote_turn.dispatcher import TurnOutcome


class _Ctx:
    def __init__(self) -> None:
        self.msg = SimpleNamespace(metadata={"message_id": "m1"})
        self.session_key = "telegram:3"
        self.turn_id = "t1"
        self.kind = "user"
        self.delivery = SimpleNamespace(route=SimpleNamespace(channel="telegram", chat_id="3"))
        self.initial_messages = [{"role": "user", "content": "hi"}]
        self.final_content: str | None = None
        self.all_messages: list[dict[str, Any]] = []
        self.stop_reason = ""
        self.had_injections = False
        self.streamed_content = False
        self.on_stream = None


def _self() -> Any:
    return SimpleNamespace(
        workspace="/tmp/ws",
        max_iterations=9,
        max_tool_result_chars=1234,
        _last_usage={},
    )


async def _call(ctx: _Ctx, self_obj: Any) -> bool:
    return await AgentLoop._run_turn_in_sandbox(self_obj, ctx)  # type: ignore[arg-type]


async def test_the_switch_off_means_the_host_runs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(dispatcher_module.TURN_HOST_ENV, raising=False)
    constructed: list[int] = []

    def _no_dispatch(*args: Any, **kwargs: Any) -> Any:
        constructed.append(1)
        raise AssertionError("no dispatcher may be built when the switch is off")

    monkeypatch.setattr(dispatcher_module, "SandboxTurnDispatcher", _no_dispatch)
    ctx = _Ctx()

    assert await _call(ctx, _self()) is False
    assert constructed == []
    assert ctx.final_content is None


async def test_an_unsendable_provider_leaves_the_turn_on_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(dispatcher_module.TURN_HOST_ENV, "sandbox")
    monkeypatch.setattr("nanobot.remote_turn.host.build_turn_request", lambda *a, **k: None)

    assert await _call(_Ctx(), _self()) is False


async def test_a_failed_dispatch_leaves_the_turn_on_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(dispatcher_module.TURN_HOST_ENV, "sandbox")

    class _Dispatcher:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def dispatch(self, request: Any, *, on_event: Any = None) -> TurnOutcome:
            return TurnOutcome(ok=False, error="worker died")

    monkeypatch.setattr("nanobot.remote_turn.SandboxTurnDispatcher", _Dispatcher)
    ctx = _Ctx()

    assert await _call(ctx, _self()) is False
    assert ctx.final_content is None


async def test_a_successful_dispatch_adopts_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(dispatcher_module.TURN_HOST_ENV, "sandbox")
    seen: dict[str, Any] = {}

    def _build(*args: Any, **kwargs: Any) -> Any:
        from nanobot.remote_turn.protocol import ProviderCredentials, RemoteTurnRequest

        seen["kwargs"] = kwargs
        return RemoteTurnRequest(
            session_key=str(kwargs.get("session_key")),
            messages=list(kwargs.get("messages") or []),
            credentials=ProviderCredentials(provider="deepseek", model="deepseek-chat"),
            channel=str(kwargs.get("channel") or ""),
            chat_id=str(kwargs.get("chat_id") or ""),
        )

    monkeypatch.setattr("nanobot.remote_turn.host.build_turn_request", _build)

    class _Dispatcher:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def dispatch(self, request: Any, *, on_event: Any = None) -> TurnOutcome:
            seen["request"] = request
            if on_event is not None:
                from nanobot.remote_turn.protocol import RemoteTurnEvent

                await on_event(RemoteTurnEvent(kind="delta", text="he"))
                await on_event(RemoteTurnEvent(kind="tool", text="exec"))
            return TurnOutcome(
                ok=True,
                final_content="done",
                stop_reason="completed",
                tools_used=["exec"],
                messages=[{"role": "assistant", "content": "done"}],
                usage={"input_tokens": 5},
                backend="novita",
            )

    monkeypatch.setattr("nanobot.remote_turn.SandboxTurnDispatcher", _Dispatcher)
    ctx = _Ctx()
    streamed: list[str] = []

    async def _on_stream(text: str) -> None:
        streamed.append(text)

    ctx.on_stream = _on_stream
    self_obj = _self()

    assert await _call(ctx, self_obj) is True
    assert ctx.final_content == "done"
    assert ctx.all_messages == [{"role": "assistant", "content": "done"}]
    assert ctx.stop_reason == "completed"
    assert self_obj._last_usage == {"input_tokens": 5}
    # Deltas reach the channel's stream; tool events do not (they are not text).
    assert streamed == ["he"]
    assert seen["request"].session_key == "telegram:3"
    assert seen["request"].channel == "telegram"


async def test_a_turn_with_no_transcript_is_not_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(dispatcher_module.TURN_HOST_ENV, "sandbox")
    ctx = _Ctx()
    ctx.initial_messages = []

    assert await _call(ctx, _self()) is False
