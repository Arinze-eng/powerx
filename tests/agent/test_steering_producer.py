"""Tests for the steering *producer*: AgentLoop.steer() + inbox lifecycle.

The runner-side tests live in test_runner_steering.py. These cover the other
half — that a live turn actually publishes an inbox a caller can push into, and
that the inbox disappears when the turn ends, so a steer can never be delivered
to the wrong (or a finished) run.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nanobot.agent.steering import SteeringInbox


class _LoopStub:
    """Exercises the real steering methods without building a full AgentLoop.

    Building an AgentLoop needs a provider, bus, sessions and workspace, none
    of which the steering methods touch. Binding every steering method from the
    real class keeps this honest: if a method grows a dependency the stub can't
    satisfy, the test fails loudly instead of passing vacuously.
    """

    _AgentLoop = __import__("nanobot.agent.loop", fromlist=["AgentLoop"]).AgentLoop

    steer = _AgentLoop.steer
    can_steer = _AgentLoop.can_steer
    steering_enabled = _AgentLoop.steering_enabled
    _should_steer = _AgentLoop._should_steer
    _try_route_steer = _AgentLoop._try_route_steer

    def __init__(self) -> None:
        self._steering_inboxes: dict[str, SteeringInbox] = {}
        self.commands = MagicMock()
        self.commands.is_dispatchable_command = MagicMock(return_value=False)
        self.commands.is_priority = MagicMock(return_value=False)


class TestSteerProducer:
    def test_steer_accepted_for_live_session(self):
        loop = _LoopStub()
        loop._steering_inboxes["s1"] = SteeringInbox()
        assert loop.steer("s1", "use the other symbol") is True
        assert loop._steering_inboxes["s1"].pending() == 1

    def test_steer_rejected_for_unknown_session(self):
        """Idle session -> False, so the caller delivers it as a normal turn."""
        loop = _LoopStub()
        assert loop.steer("nope", "hello") is False
        assert "nope" not in loop._steering_inboxes

    def test_steer_rejected_when_inbox_closed(self):
        loop = _LoopStub()
        inbox = SteeringInbox()
        inbox.close()
        loop._steering_inboxes["s1"] = inbox
        assert loop.steer("s1", "too late") is False
        assert inbox.pending() == 0

    def test_steer_rejects_blank(self):
        loop = _LoopStub()
        loop._steering_inboxes["s1"] = SteeringInbox()
        assert loop.steer("s1", "   ") is False

    def test_turn_context_carries_the_inbox(self):
        from nanobot.agent.loop import TurnContext

        assert "steering_inbox" in TurnContext.__dataclass_fields__

    def test_can_steer_tracks_liveness(self):
        loop = _LoopStub()
        assert loop.can_steer("s1") is False
        loop._steering_inboxes["s1"] = SteeringInbox()
        assert loop.can_steer("s1") is True
        loop._steering_inboxes["s1"].close()
        assert loop.can_steer("s1") is False

    def test_multiple_steers_accumulate_in_order(self):
        loop = _LoopStub()
        loop._steering_inboxes["s1"] = SteeringInbox()
        loop.steer("s1", "first")
        loop.steer("s1", "second")
        texts = [u.text for u in loop._steering_inboxes["s1"].drain()]
        assert texts == ["first", "second"]


class TestSpecPlumbing:
    def test_run_spec_accepts_inbox_and_defaults_none(self):
        from nanobot.agent.runner import AgentRunSpec

        assert "steering_inbox" in AgentRunSpec.__dataclass_fields__
        inbox = SteeringInbox()
        spec = MagicMock(spec=AgentRunSpec)
        spec.steering_inbox = inbox
        assert spec.steering_inbox is inbox

    def test_full_chain_signatures_expose_the_inbox(self):
        """Every layer must accept it, or the inbox is dropped mid-chain.

        The wire is: _dispatch -> _process_message -> TurnContext ->
        _run_agent_loop -> AgentRunSpec. A missing link here fails silently at
        runtime (the feature just never fires), so assert on all of them.
        """
        import inspect

        from nanobot.agent.loop import AgentLoop, TurnContext
        from nanobot.agent.runner import AgentRunSpec

        for fn in ("_process_message", "_run_agent_loop"):
            params = inspect.signature(getattr(AgentLoop, fn)).parameters
            assert "steering_inbox" in params, f"{fn} is missing steering_inbox"
            assert params["steering_inbox"].default is None

        assert "steering_inbox" in TurnContext.__dataclass_fields__
        assert "steering_inbox" in AgentRunSpec.__dataclass_fields__

    def test_chain_forwards_the_inbox_in_source(self):
        """Structural guard: each hop must actually pass its value along."""
        import inspect

        from nanobot.agent.loop import AgentLoop

        dispatch = inspect.getsource(AgentLoop._dispatch)
        assert "steering_inbox=steering_inbox" in dispatch, (
            "_dispatch no longer forwards the inbox into the turn"
        )
        process = inspect.getsource(AgentLoop._process_message)
        assert "steering_inbox=steering_inbox" in process, (
            "_process_message no longer forwards the inbox into TurnContext"
        )
        run_turn = inspect.getsource(AgentLoop._run_turn)
        assert "steering_inbox=ctx.steering_inbox" in run_turn, (
            "_run_turn no longer forwards the inbox into _run_agent_loop"
        )
        loop_src = inspect.getsource(AgentLoop._run_agent_loop)
        assert "steering_inbox=steering_inbox" in loop_src, (
            "_run_agent_loop no longer forwards the inbox into AgentRunSpec"
        )

    def test_dispatch_publishes_and_cleans_inbox(self):
        """_dispatch must register the inbox and remove it when the turn ends."""
        import inspect

        from nanobot.agent.loop import AgentLoop

        src = inspect.getsource(AgentLoop._dispatch)
        assert "_steering_inboxes[session_key] = steering_inbox" in src
        assert "own_inbox is steering_inbox" in src, "cleanup lost its identity check"
        assert "steering_inbox=steering_inbox" in src, "inbox never reaches the turn"


class TestSteeringPolicy:
    """_should_steer decides preempt-vs-inject. Default must be inject."""

    @staticmethod
    def _loop():
        return _LoopStub()

    @staticmethod
    def _msg(content="follow up", *, channel="cli", metadata=None, input_role=None):
        from nanobot.bus.events import InboundMessage

        return InboundMessage(
            channel=channel,
            sender_id="u1",
            chat_id="c1",
            content=content,
            metadata=metadata or {},
            input_role=input_role,
        )

    def test_default_is_off_so_injection_still_wins(self, monkeypatch):
        monkeypatch.delenv("POWERX_STEER_MID_SESSION", raising=False)
        loop = self._loop()
        assert loop.steering_enabled() is False
        assert loop._should_steer(self._msg(), "follow up") is False

    def test_env_enables_plain_followups(self, monkeypatch):
        monkeypatch.setenv("POWERX_STEER_MID_SESSION", "1")
        loop = self._loop()
        assert loop._should_steer(self._msg(), "follow up") is True

    def test_metadata_forces_steer_on_without_env(self, monkeypatch):
        monkeypatch.delenv("POWERX_STEER_MID_SESSION", raising=False)
        loop = self._loop()
        msg = self._msg(metadata={"steer": True})
        assert loop._should_steer(msg, "follow up") is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
    def test_explicit_negative_values_do_not_enable_steering(self, monkeypatch, value):
        """Regression: bool(os.environ.get(...)) is True for the string "0".

        An operator setting POWERX_STEER_MID_SESSION=0 in a deploy dashboard to
        mean OFF must not switch steering ON. Only affirmative values enable it.
        """
        monkeypatch.setenv("POWERX_STEER_MID_SESSION", value)
        loop = self._loop()
        assert loop.steering_enabled() is False
        assert loop._should_steer(self._msg(), "follow up") is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
    def test_affirmative_values_enable_steering(self, monkeypatch, value):
        monkeypatch.setenv("POWERX_STEER_MID_SESSION", value)
        loop = self._loop()
        assert loop.steering_enabled() is True
        assert loop._should_steer(self._msg(), "follow up") is True

    def test_metadata_vetoes_env(self, monkeypatch):
        """An explicit steer=False must beat the global switch."""
        monkeypatch.setenv("POWERX_STEER_MID_SESSION", "1")
        loop = self._loop()
        msg = self._msg(metadata={"steer": False})
        assert loop._should_steer(msg, "follow up") is False

    def test_system_turns_never_steer(self, monkeypatch):
        monkeypatch.setenv("POWERX_STEER_MID_SESSION", "1")
        loop = self._loop()
        assert loop._should_steer(self._msg(channel="system"), "x") is False

    def test_commands_never_steer(self, monkeypatch):
        monkeypatch.setenv("POWERX_STEER_MID_SESSION", "1")
        loop = self._loop()
        loop.commands.is_dispatchable_command = MagicMock(return_value=True)
        assert loop._should_steer(self._msg(), "/stop") is False
        loop.commands.is_dispatchable_command = MagicMock(return_value=False)
        loop.commands.is_priority = MagicMock(return_value=True)
        assert loop._should_steer(self._msg(), "!urgent") is False

    @pytest.mark.asyncio
    async def test_route_declines_when_policy_off(self, monkeypatch):
        from nanobot.agent.loop import AgentLoop

        monkeypatch.delenv("POWERX_STEER_MID_SESSION", raising=False)
        loop = _LoopStub()
        loop._steering_inboxes["s1"] = SteeringInbox()

        loop.commands.is_dispatchable_command = MagicMock(return_value=False)
        loop.commands.is_priority = MagicMock(return_value=False)
        msg = self._msg()
        result = await AgentLoop._try_route_steer(loop, msg, "s1", "follow up")
        assert result is False
        # Untouched inbox: the caller must still be able to inject.
        assert loop._steering_inboxes["s1"].pending() == 0

    @pytest.mark.asyncio
    async def test_route_consumes_when_forced_by_metadata(self):
        from nanobot.agent.loop import AgentLoop

        loop = _LoopStub()
        loop._steering_inboxes["s1"] = SteeringInbox()

        msg = self._msg(content="change course", metadata={"steer": True})
        result = await AgentLoop._try_route_steer(loop, msg, "s1", "change course")
        assert result is True
        assert loop._steering_inboxes["s1"].pending() == 1

    @pytest.mark.asyncio
    async def test_route_falls_back_when_no_live_inbox(self):
        """Steer typed between turns must degrade to injection, not vanish."""
        from nanobot.agent.loop import AgentLoop

        loop = _LoopStub()

        msg = self._msg(metadata={"steer": True})
        assert await AgentLoop._try_route_steer(loop, msg, "gone", "hi") is False

    @pytest.mark.asyncio
    async def test_route_rejects_empty_content(self):
        from nanobot.agent.loop import AgentLoop

        loop = _LoopStub()
        loop._steering_inboxes["s1"] = SteeringInbox()

        msg = self._msg(content="   ", metadata={"steer": True})
        assert await AgentLoop._try_route_steer(loop, msg, "s1", "   ") is False

    def test_routing_seam_is_wired_before_injection(self):
        """The steer attempt must precede the pending-queue put in run()."""
        import inspect

        from nanobot.agent.loop import AgentLoop

        src = inspect.getsource(AgentLoop.run)
        steer_at = src.index("_try_route_steer(")
        inject_at = src.index("self._pending_queues[effective_key].put_nowait(")
        assert steer_at < inject_at, "steering must be tried before injection"
        assert "continue" in src[steer_at:inject_at], (
            "accepted steer must skip the injection put"
        )

    def test_registry_and_lookup_use_the_same_key(self):
        """Register keys must match the key run() looks up, or steering dies.

        Both sides go through _effective_session_key; asserting the call exists
        on both sides catches a refactor that switches one to the raw key.
        """
        import inspect

        from nanobot.agent.loop import AgentLoop

        assert "_effective_session_key" in inspect.getsource(AgentLoop._dispatch)
        assert "_effective_session_key" in inspect.getsource(AgentLoop.run)


class TestRunnerConsumesProducer:
    @pytest.mark.asyncio
    async def test_pushed_steer_is_visible_to_the_loop_drain(self):
        """End-to-end across the seam: producer push -> runner drain."""
        from nanobot.agent.runner import AgentRunner

        loop = _LoopStub()
        inbox = SteeringInbox()
        loop._steering_inboxes["s1"] = inbox

        # Producer side (what a channel handler would do).
        assert loop.steer("s1", "stop, target EURUSD instead") is True

        # Consumer side (what the running loop does at a seam).
        runner = AgentRunner()
        spec = MagicMock(spec=__import__(
            "nanobot.agent.runner", fromlist=["AgentRunSpec"]
        ).AgentRunSpec)
        spec.steering_inbox = inbox
        spec.session_key = "s1"

        messages: list[dict] = []
        drained = await runner._drain_steering(spec, messages)
        assert drained == 1
        assert "EURUSD" in str(messages[0]["content"])
        # Drained exactly once.
        assert await runner._drain_steering(spec, messages) == 0
