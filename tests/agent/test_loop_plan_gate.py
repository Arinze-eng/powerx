"""The plan gate as wired into AgentLoop: who waits, who does not, and what releases it.

``test_plan_gate.py`` pins the policy and ``test_runner_plan_gate.py`` pins the
runner. Neither can see the decisions only the loop can make, because only the
loop knows the channel, the message metadata and whether a human is on the other
end:

* an interactive multi-step turn states its plan and then STOPS, holding it
  against the session so the user can approve it;
* an autonomous turn -- cron, a session trigger, a subagent result, an internal
  continuation slice -- gets the same plan gate but never waits, because waiting
  would block a session nobody is watching;
* the next message either releases the held plan (executed without re-planning)
  or drops it, and an unrecognised reply always drops it rather than running work
  the user did not approve.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from nanobot.agent.plan_gate import (
    PLAN_AWAITING_STOP_REASON,
    approval_prompt,
    pending_plans,
    reset_pending_plans_store,
)
from nanobot.cron.session_turns import CRON_TRIGGER_META
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session.turn_continuation import INTERNAL_CONTINUATION_META

#: A batch ask: the shape router reads this as ``multi_step``, which is the only
#: shape that waits for approval under the default ``POWERX_PLAN_APPROVAL``.
BATCH_ASK = "rename all the .txt files"

#: A one-step ask: ``single``, so the gate still arms but approval does not.
SINGLE_ASK = "edit this video"

PLAN_TEXT = "1. List every .txt file.\n2. Rename each one to .md."


@pytest.fixture(autouse=True)
def _isolate_plan_store(tmp_path, monkeypatch):
    """Give every test its own pending-plan store and its own gate switches.

    The store is a process-wide singleton bound to an env path on first use, so
    it has to be rebound AND reset. Without this a plan held by one test would
    be visible to the next.
    """
    monkeypatch.setenv("POWERX_PLAN_STORE", str(tmp_path / "pending_plans.json"))
    # The suite-wide conftest turns the gate off so unrelated tests are not
    # shifted by one reply; these tests are about the gate, so turn it back on.
    monkeypatch.setenv("POWERX_PLAN_GATE", "1")
    monkeypatch.setenv("POWERX_PLAN_APPROVAL", "multi_step")
    # Isolate from the zero-call caches, which would otherwise answer a repeated
    # ask without a provider call and hide the gate entirely.
    monkeypatch.setenv("POWERX_PLAN_CACHE", "0")
    reset_pending_plans_store()
    yield
    reset_pending_plans_store()


def _text(content: str) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=[], finish_reason="stop")


def _make_loop(tmp_path, script: list[LLMResponse]):
    """A real AgentLoop whose provider replays *script*, one reply per call."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = MagicMock(max_tokens=4096, temperature=0.1, reasoning_effort=None)
    provider.chat_with_retry = AsyncMock(side_effect=list(script))

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
    # Nothing is executed in these tests; the gate is about what is ADVERTISED.
    loop.tools.execute = AsyncMock(return_value="ok")
    return loop


def _ask(loop, text: str, **kwargs):
    """Run one interactive turn carrying *text* as the user's message."""
    return loop._run_agent_loop(
        [{"role": "user", "content": text}],
        runtime=loop.llm_runtime(),
        channel="webui",
        chat_id="chat-1",
        session_key="s1",
        **kwargs,
    )


def _advertised_tools(loop) -> list[object]:
    """The ``tools`` kwarg of every provider request, in order.

    ``None`` means the request carried no tool schemas at all -- the gate.
    """
    return [
        call.kwargs.get("tools", "MISSING")
        for call in loop.provider.chat_with_retry.await_args_list
    ]


class TestInteractiveTurnWaits:
    """A multi-step ask from a person stops on its plan."""

    @pytest.mark.asyncio
    async def test_multi_step_turn_stops_and_holds_the_plan(self, tmp_path) -> None:
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT)])

        final_content, _, _, stop_reason, _ = await _ask(loop, BATCH_ASK)

        assert stop_reason == PLAN_AWAITING_STOP_REASON
        # Exactly one request: the plan. No work happened.
        assert loop.provider.chat_with_retry.await_count == 1
        assert _advertised_tools(loop) == [None]

        held = pending_plans().get("s1")
        assert held is not None
        assert held["plan"] == PLAN_TEXT
        assert held["shape"] == "multi_step"

        # The user is told how to release it, and the plan itself is intact
        # above that line so a streaming channel sees no contradiction.
        assert final_content == f"{PLAN_TEXT}\n\n{approval_prompt()}"

    @pytest.mark.asyncio
    async def test_plan_is_the_turn_s_own_assistant_message(self, tmp_path) -> None:
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT)])

        _, _, messages, _, _ = await _ask(loop, BATCH_ASK)

        plans = [
            message
            for message in messages
            if message.get("role") == "assistant"
            and PLAN_TEXT in str(message.get("content") or "")
        ]
        assert len(plans) == 1

    @pytest.mark.asyncio
    async def test_approval_off_gates_but_never_waits(self, tmp_path, monkeypatch) -> None:
        """``POWERX_PLAN_APPROVAL=off`` keeps the plan, drops the pause."""
        monkeypatch.setenv("POWERX_PLAN_APPROVAL", "off")
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT), _text("Renamed all of them.")])

        final_content, _, _, stop_reason, _ = await _ask(loop, BATCH_ASK)

        assert stop_reason == "completed"
        assert final_content == "Renamed all of them."
        # The plan request still went out with no tools; the work then ran.
        assert _advertised_tools(loop)[0] is None
        assert _advertised_tools(loop)[1] is not None
        assert pending_plans().get("s1") is None


class TestSingleStepTurnDoesNotWait:
    """A one-step ask is not worth a round trip of approval friction."""

    @pytest.mark.asyncio
    async def test_single_shape_turn_runs_after_its_plan(self, tmp_path) -> None:
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT), _text("Trimmed the clip.")])

        final_content, _, _, stop_reason, _ = await _ask(loop, SINGLE_ASK)

        assert stop_reason == "completed"
        assert final_content == "Trimmed the clip."
        assert _advertised_tools(loop)[0] is None
        assert _advertised_tools(loop)[1] is not None
        assert pending_plans().get("s1") is None

    @pytest.mark.asyncio
    async def test_approval_all_waits_on_a_single_shape_too(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("POWERX_PLAN_APPROVAL", "all")
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT)])

        _, _, _, stop_reason, _ = await _ask(loop, SINGLE_ASK)

        assert stop_reason == PLAN_AWAITING_STOP_REASON
        assert pending_plans().get("s1") is not None


class TestAutonomousTurnsNeverWait:
    """Nobody is present to approve a cron job or a subagent result."""

    @pytest.mark.asyncio
    async def test_cron_turn_plans_then_proceeds(self, tmp_path) -> None:
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT), _text("Done, 12 files renamed.")])

        final_content, _, _, stop_reason, _ = await _ask(
            loop, BATCH_ASK, metadata={CRON_TRIGGER_META: {"id": "job-1"}}
        )

        assert stop_reason == "completed"
        assert final_content == "Done, 12 files renamed."
        # The gate still armed -- the plan was stated first.
        assert _advertised_tools(loop)[0] is None
        assert pending_plans().get("s1") is None

    @pytest.mark.asyncio
    async def test_subagent_result_turn_plans_then_proceeds(self, tmp_path) -> None:
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT), _text("Incorporated the result.")])

        _, _, _, stop_reason, _ = await _ask(
            loop, BATCH_ASK, metadata={"injected_event": "subagent_result"}
        )

        assert stop_reason == "completed"
        assert pending_plans().get("s1") is None

    @pytest.mark.asyncio
    async def test_internal_continuation_slice_plans_then_proceeds(self, tmp_path) -> None:
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT), _text("Continued the work.")])

        _, _, _, stop_reason, _ = await _ask(
            loop, BATCH_ASK, metadata={INTERNAL_CONTINUATION_META: True}
        )

        assert stop_reason == "completed"
        assert pending_plans().get("s1") is None

    @pytest.mark.asyncio
    async def test_ephemeral_turn_plans_then_proceeds(self, tmp_path) -> None:
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT), _text("Consolidated.")])

        _, _, _, stop_reason, _ = await _ask(loop, BATCH_ASK, ephemeral=True)

        assert stop_reason == "completed"
        assert pending_plans().get("s1") is None

    @pytest.mark.asyncio
    async def test_system_channel_turn_plans_then_proceeds(self, tmp_path) -> None:
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT), _text("Handled.")])

        _, _, _, stop_reason, _ = await loop._run_agent_loop(
            [{"role": "user", "content": BATCH_ASK}],
            runtime=loop.llm_runtime(),
            channel="system",
            chat_id="sys",
            session_key="s1",
        )

        assert stop_reason == "completed"
        assert pending_plans().get("s1") is None


class TestReleasingAHeldPlan:
    """The message after the plan decides what happens to it."""

    @pytest.mark.asyncio
    async def test_go_executes_the_approved_plan_without_replanning(self, tmp_path) -> None:
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT)])
        await _ask(loop, BATCH_ASK)
        assert pending_plans().get("s1") is not None

        # A fresh turn on the same session, with the user's approval.
        loop.provider.chat_with_retry = AsyncMock(
            side_effect=[_text("Renamed all 12 files.")]
        )
        final_content, _, messages, stop_reason, _ = await _ask(loop, "go")

        assert stop_reason == "completed"
        assert final_content == "Renamed all 12 files."
        # The gate is disarmed for an approved plan: the very first request
        # advertises tools, so nothing is spent re-planning work already agreed.
        assert _advertised_tools(loop) == [_advertised_tools(loop)[0]]
        assert _advertised_tools(loop)[0] is not None
        # And the plan is put back in front of the model, which cannot execute a
        # plan it cannot see.
        assert any(
            message.get("role") == "assistant" and PLAN_TEXT in str(message.get("content"))
            for message in messages
        )
        assert pending_plans().get("s1") is None

    @pytest.mark.asyncio
    async def test_approved_turn_carries_the_approved_note_not_the_gate_note(
        self, tmp_path
    ) -> None:
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT)])
        await _ask(loop, BATCH_ASK)

        loop.provider.chat_with_retry = AsyncMock(side_effect=[_text("Done.")])
        await _ask(loop, "yes please")

        request = str(loop.provider.chat_with_retry.await_args_list[0].kwargs["messages"])
        assert "the plan above is approved" in request
        assert "tools are DISABLED" not in request

    @pytest.mark.asyncio
    async def test_rejection_drops_the_plan_and_rearms_the_gate(self, tmp_path) -> None:
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT)])
        await _ask(loop, BATCH_ASK)

        loop.provider.chat_with_retry = AsyncMock(
            side_effect=[_text("2. Something else."), _text("Nothing to do.")]
        )
        _, _, _, stop_reason, _ = await _ask(loop, "no")

        # The rejected plan is gone, so the turn plans afresh -- it does not
        # execute the work the user just said no to.
        assert pending_plans().get("s1") is None
        assert stop_reason == "completed"
        assert _advertised_tools(loop)[0] is None
        assert PLAN_TEXT not in str(
            loop.provider.chat_with_retry.await_args_list[0].kwargs["messages"]
        )

    @pytest.mark.asyncio
    async def test_unrecognised_reply_drops_the_plan_rather_than_running_it(
        self, tmp_path
    ) -> None:
        """A near-miss is never read as approval.

        "ok, also add a test" is an approval-shaped word plus new work the held
        plan does not contain. Running the old plan would silently drop the test.
        """
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT)])
        await _ask(loop, BATCH_ASK)

        loop.provider.chat_with_retry = AsyncMock(
            side_effect=[_text("1. New plan."), _text("Done the new thing.")]
        )
        await _ask(loop, "ok, also add a test")

        assert pending_plans().get("s1") is None
        # Re-armed: the first request carried no tools and did not replay the
        # held plan, so nothing from the unapproved plan was executed.
        assert _advertised_tools(loop)[0] is None
        assert PLAN_TEXT not in str(
            loop.provider.chat_with_retry.await_args_list[0].kwargs["messages"]
        )

    @pytest.mark.asyncio
    async def test_a_second_session_does_not_reuse_the_first_session_s_plan(
        self, tmp_path
    ) -> None:
        """Two sessions never see each other's held plans."""
        loop = _make_loop(tmp_path, [_text(PLAN_TEXT)])
        await _ask(loop, BATCH_ASK)
        assert pending_plans().get("s1") is not None

        loop.provider.chat_with_retry = AsyncMock(side_effect=[_text("A fresh plan.")])
        await loop._run_agent_loop(
            [{"role": "user", "content": BATCH_ASK}],
            runtime=loop.llm_runtime(),
            channel="webui",
            chat_id="chat-2",
            session_key="s2",
        )

        # s2 plans on its own; s1's held plan is untouched and unseen.
        assert pending_plans().get("s1")["plan"] == PLAN_TEXT
        assert pending_plans().get("s2")["plan"] == "A fresh plan."


class TestGateOff:
    """The switch turns the whole mechanism off, not just the pause."""

    @pytest.mark.asyncio
    async def test_gate_off_advertises_tools_on_the_first_request(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("POWERX_PLAN_GATE", "0")
        loop = _make_loop(tmp_path, [_text("Renamed them.")])

        _, _, _, stop_reason, _ = await _ask(loop, BATCH_ASK)

        assert stop_reason == "completed"
        assert _advertised_tools(loop) == [_advertised_tools(loop)[0]]
        assert _advertised_tools(loop)[0] is not None
        assert pending_plans().get("s1") is None
