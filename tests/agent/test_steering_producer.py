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
    """Exercises the real steer()/can_steer() methods without building a loop.

    Building a full AgentLoop needs a provider, bus, sessions and workspace,
    none of which steer() touches — it only reads the inbox registry. Binding
    the unimplemented methods keeps this honest: if steer() grows a dependency,
    this test stops being valid rather than silently passing.
    """

    steer = __import__("nanobot.agent.loop", fromlist=["AgentLoop"]).AgentLoop.steer
    can_steer = __import__(
        "nanobot.agent.loop", fromlist=["AgentLoop"]
    ).AgentLoop.can_steer

    def __init__(self) -> None:
        self._steering_inboxes: dict[str, SteeringInbox] = {}


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
