"""Tests for mid-run user steering (nanobot.agent.steering + runner seams).

Covers the CowAgent-derived semantics:
* steering takes precedence over the model's proposed continuation
* every abandoned tool_call is still answered, so the transcript stays valid
* the inbox drains at most N steers per turn and honours a cycle ceiling
* no inbox == zero behaviour change for existing runs
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.runner import _STEERING_ABANDONED, AgentRunner
from nanobot.agent.steering import (
    MAX_STEER_CHARS,
    MAX_STEER_CYCLES,
    MAX_STEERS_PER_TURN,
    SteeringInbox,
    build_steering_messages,
    close_pending_tool_calls,
    should_honour_steer,
    synthetic_tool_result_message,
)
from nanobot.providers.base import LLMResponse, ToolCallRequest, LLMProvider


# --------------------------------------------------------------------------
# SteeringInbox
# --------------------------------------------------------------------------


class TestSteeringInbox:
    def test_push_and_drain_roundtrip(self):
        inbox = SteeringInbox()
        assert inbox.push("use postgres instead") is True
        assert inbox.has_pending() is True
        updates = inbox.drain()
        assert len(updates) == 1
        assert updates[0].text == "use postgres instead"
        assert inbox.has_pending() is False

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t", None])
    def test_blank_steers_are_rejected(self, blank):
        inbox = SteeringInbox()
        assert inbox.push(blank) is False
        assert inbox.pending() == 0

    def test_drain_respects_per_turn_cap_and_preserves_order(self):
        inbox = SteeringInbox()
        for i in range(MAX_STEERS_PER_TURN + 2):
            inbox.push(f"steer {i}")
        first = inbox.drain()
        assert [u.text for u in first] == [
            f"steer {i}" for i in range(MAX_STEERS_PER_TURN)
        ]
        # The remainder is left queued rather than silently dropped.
        assert inbox.pending() == 2
        rest = inbox.drain()
        assert [u.text for u in rest] == [
            f"steer {i}" for i in range(MAX_STEERS_PER_TURN, MAX_STEERS_PER_TURN + 2)
        ]

    def test_drain_on_empty_inbox_is_noop(self):
        inbox = SteeringInbox()
        assert inbox.drain() == []

    def test_long_steer_is_truncated_not_dropped(self):
        inbox = SteeringInbox()
        inbox.push("x" * (MAX_STEER_CHARS * 3))
        text = inbox.drain()[0].text
        assert len(text) < MAX_STEER_CHARS * 3
        assert "truncated" in text.lower()

    def test_close_if_empty_only_closes_when_empty(self):
        inbox = SteeringInbox()
        inbox.push("pending")
        assert inbox.close_if_empty() is False
        assert inbox.closed is False
        inbox.drain()
        assert inbox.close_if_empty() is True

    def test_closed_inbox_refuses_late_steers(self):
        inbox = SteeringInbox()
        inbox.close()
        assert inbox.push("too late") is False
        assert inbox.pending() == 0

    def test_thread_safety_under_concurrent_pushes(self):
        """Steers arrive from channel threads while the loop drains."""
        inbox = SteeringInbox()
        total = 200

        def pusher(start: int, count: int) -> None:
            for i in range(start, start + count):
                inbox.push(f"steer {i}")

        threads = [
            threading.Thread(target=pusher, args=(base, total // 4))
            for base in range(0, total, total // 4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert inbox.pending() == total

    def test_concurrent_drain_does_not_lose_or_duplicate(self):
        inbox = SteeringInbox()
        for i in range(100):
            inbox.push(f"steer {i}")
        seen: list[str] = []
        lock = threading.Lock()

        def drainer() -> None:
            while True:
                batch = inbox.drain(limit=7)
                if not batch:
                    return
                with lock:
                    seen.extend(u.text for u in batch)

        threads = [threading.Thread(target=drainer) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(seen) == sorted(f"steer {i}" for i in range(100))


# --------------------------------------------------------------------------
# Message rendering
# --------------------------------------------------------------------------


class TestMessageRendering:
    def test_steer_message_is_labelled_user_role(self):
        inbox = SteeringInbox()
        inbox.push("switch to sqlite")
        [msg] = build_steering_messages(inbox.drain())
        assert msg["role"] == "user"
        assert "switch to sqlite" in msg["content"]
        # Labelled so the model treats it as a correction, not chat.
        assert "Steering update" in msg["content"]

    def test_synthetic_result_closes_a_call(self):
        call = ToolCallRequest(id="call_1", name="shell", arguments={"cmd": "ls"})
        msg = synthetic_tool_result_message(call)
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "call_1"
        assert msg["name"] == "shell"
        assert msg["content"]

    def test_close_pending_answers_every_call(self):
        """The invariant: no tool_call is ever left unanswered."""
        calls = [
            ToolCallRequest(id=f"c{i}", name="shell", arguments={}) for i in range(4)
        ]
        messages: list[dict] = []
        assert close_pending_tool_calls(messages, calls) == 4
        assert [m["tool_call_id"] for m in messages] == ["c0", "c1", "c2", "c3"]
        assert all(m["role"] == "tool" for m in messages)

    def test_synthetic_result_accepts_dict_shaped_call(self):
        msg = synthetic_tool_result_message({"id": "d1", "name": "read_file"})
        assert msg["tool_call_id"] == "d1"
        assert msg["name"] == "read_file"

    def test_should_honour_steer_gates_on_all_three_conditions(self):
        inbox = SteeringInbox()
        assert should_honour_steer(None, 0) is False
        assert should_honour_steer(inbox, 0) is False
        inbox.push("x")
        assert should_honour_steer(inbox, 0) is True
        assert should_honour_steer(inbox, MAX_STEER_CYCLES) is False


# --------------------------------------------------------------------------
# Runner seams
# --------------------------------------------------------------------------


def _provider(responses: list[LLMResponse]) -> MagicMock:
    provider = MagicMock()
    provider.get_default_generation_settings.return_value = MagicMock(
        temperature=0.0, max_tokens=1024
    )
    provider.chat_with_retry = AsyncMockSequence(responses)
    return provider


class AsyncMockSequence:
    """Awaitable callable returning queued responses in order."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, *args, **kwargs):
        return self

    def __await__(self):
        self.calls += 1
        response = self._responses.pop(0) if self._responses else LLMResponse(
            content="done", tool_calls=[], finish_reason="stop"
        )
        future = asyncio.Future()
        future.set_result(response)
        return future.__await__()


def _spec(provider, **overrides):
    kwargs = dict(
        initial_messages=[{"role": "user", "content": "start"}],
        tools=MagicMock(),
        max_iterations=5,
        max_tool_result_chars=8_000,
    )
    kwargs.update(overrides)
    kwargs.setdefault("tools", MagicMock(get_definitions=MagicMock(return_value=[])))
    return make_run_spec(provider, model="test-model", **kwargs)


class TestRunnerSteeringSeams:
    @pytest.mark.asyncio
    async def test_drain_steering_appends_and_reports_count(self):
        runner = AgentRunner()
        inbox = SteeringInbox()
        inbox.push("first")
        inbox.push("second")
        spec = _spec(MagicMock(), steering_inbox=inbox)
        messages: list[dict] = []
        assert await runner._drain_steering(spec, messages) == 2
        assert inbox.has_pending() is False
        # Both steers are reported, and both texts survive...
        joined = "\n".join(str(m.get("content")) for m in messages)
        assert "first" in joined and "second" in joined
        # ...but consecutive user messages merge into one turn, because
        # _append_injected_messages preserves strict role alternation.
        assert all(m["role"] == "user" for m in messages)
        assert len(messages) == 1

    @pytest.mark.asyncio
    async def test_drain_steering_without_inbox_is_inert(self):
        """The zero-change guarantee for every existing run."""
        runner = AgentRunner()
        spec = _spec(MagicMock())
        messages: list[dict] = []
        assert await runner._drain_steering(spec, messages) == 0
        assert messages == []

    @pytest.mark.asyncio
    async def test_steer_preempts_proposed_continuation_and_closes_calls(self):
        runner = AgentRunner()
        inbox = SteeringInbox()
        inbox.push("stop that, do the other thing")
        spec = _spec(MagicMock(), steering_inbox=inbox)

        calls = [
            ToolCallRequest(id="a", name="shell", arguments={}),
            ToolCallRequest(id="b", name="read_file", arguments={}),
        ]
        messages: list[dict] = []
        assistant = {"role": "assistant", "content": "", "tool_calls": []}

        applied = await runner._steer_before_continuation(
            spec, messages, assistant, calls, steer_cycles=0
        )
        assert applied is True
        # Assistant turn persisted, then BOTH abandoned calls answered.
        assert messages[0] is assistant
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["a", "b"]
        # ...followed by the steer itself.
        assert any("Steering update" in str(m.get("content")) for m in messages)

    @pytest.mark.asyncio
    async def test_no_steer_leaves_transcript_untouched(self):
        runner = AgentRunner()
        inbox = SteeringInbox()
        spec = _spec(MagicMock(), steering_inbox=inbox)
        messages: list[dict] = []
        calls = [ToolCallRequest(id="a", name="shell", arguments={})]
        applied = await runner._steer_before_continuation(
            spec, messages, {"role": "assistant"}, calls, steer_cycles=0
        )
        assert applied is False
        assert messages == []

    @pytest.mark.asyncio
    async def test_cycle_ceiling_disables_steering(self):
        """At the ceiling the model's continuation proceeds unsteered."""
        runner = AgentRunner()
        inbox = SteeringInbox()
        inbox.push("late steer")
        spec = _spec(MagicMock(), steering_inbox=inbox)
        messages: list[dict] = []
        applied = await runner._steer_before_continuation(
            spec,
            messages,
            {"role": "assistant"},
            [ToolCallRequest(id="a", name="shell", arguments={})],
            steer_cycles=MAX_STEER_CYCLES,
        )
        assert applied is False
        # The steer stays queued rather than being consumed and ignored.
        assert inbox.has_pending() is True

    @pytest.mark.asyncio
    async def test_execute_tools_abandons_remaining_serial_calls(self):
        """Seam 3: a steer arriving mid-batch stops the unstarted tail."""
        runner = AgentRunner()
        inbox = SteeringInbox()
        spec = _spec(MagicMock(), steering_inbox=inbox, concurrent_tools=False)

        calls = [ToolCallRequest(id=f"c{i}", name="shell", arguments={}) for i in range(3)]
        executed: list[str] = []

        async def fake_run_tool(_spec, tool_call, *a, **kw):
            executed.append(tool_call.id)
            if tool_call.id == "c0":
                inbox.push("changed my mind")
            return ("ok", {"tool_call_id": tool_call.id, "status": "ok"}, None)

        runner._run_tool = fake_run_tool  # type: ignore[method-assign]
        results, events, fatal = await runner._execute_tools(
            spec, calls, {}, {}
        )

        assert executed == ["c0"]
        assert fatal is None
        # Alignment invariant: one result per emitted call.
        assert len(results) == len(calls) == len(events)
        assert results[0] == "ok"
        assert results[1] is _STEERING_ABANDONED
        assert results[2] is _STEERING_ABANDONED
        assert [e["status"] for e in events] == ["ok", "skipped", "skipped"]

    @pytest.mark.asyncio
    async def test_execute_tools_without_inbox_runs_everything(self):
        runner = AgentRunner()
        spec = _spec(MagicMock(), steering_inbox=None, concurrent_tools=False)
        calls = [ToolCallRequest(id=f"c{i}", name="shell", arguments={}) for i in range(3)]
        executed: list[str] = []

        async def fake_run_tool(_spec, tool_call, *a, **kw):
            executed.append(tool_call.id)
            return ("ok", {"tool_call_id": tool_call.id, "status": "ok"}, None)

        runner._run_tool = fake_run_tool  # type: ignore[method-assign]
        results, events, fatal = await runner._execute_tools(spec, calls, {}, {})
        assert executed == ["c0", "c1", "c2"]
        assert len(results) == 3
        assert all(r == "ok" for r in results)
        assert fatal is None

    @pytest.mark.asyncio
    async def test_concurrent_batch_is_not_split_midflight(self):
        """A running concurrent batch finishes; we do not abandon half-calls."""
        runner = AgentRunner()
        inbox = SteeringInbox()
        spec = _spec(MagicMock(), steering_inbox=inbox, concurrent_tools=True)
        calls = [ToolCallRequest(id=f"c{i}", name="shell", arguments={}) for i in range(2)]
        executed: list[str] = []

        async def fake_run_tool(_spec, tool_call, *a, **kw):
            executed.append(tool_call.id)
            inbox.push("steer during the batch")
            return ("ok", {"tool_call_id": tool_call.id, "status": "ok"}, None)

        runner._run_tool = fake_run_tool  # type: ignore[method-assign]
        results, events, _ = await runner._execute_tools(spec, calls, {}, {})
        assert sorted(executed) == ["c0", "c1"]
        assert all(r == "ok" for r in results)
        assert len(results) == 2

    def test_abandoned_placeholder_is_distinct_string(self):
        assert isinstance(_STEERING_ABANDONED, str)
        assert _STEERING_ABANDONED not in ("", "ok")


class TestSteeringInvariants:
    @pytest.mark.asyncio
    async def test_every_tool_call_gets_exactly_one_reply(self):
        """Core transcript-validity invariant across the seam-3 path."""
        runner = AgentRunner()
        inbox = SteeringInbox()
        spec = _spec(MagicMock(), steering_inbox=inbox, concurrent_tools=False)
        calls = [ToolCallRequest(id=f"c{i}", name="shell", arguments={}) for i in range(5)]

        async def fake_run_tool(_spec, tool_call, *a, **kw):
            if tool_call.id == "c1":
                inbox.push("redirect")
            return ("ok", {"tool_call_id": tool_call.id, "status": "ok"}, None)

        runner._run_tool = fake_run_tool  # type: ignore[method-assign]
        results, events, _ = await runner._execute_tools(spec, calls, {}, {})

        assert len(results) == len(calls) == len(events)
        ids = [e["tool_call_id"] for e in events]
        assert len(ids) == len(set(ids)) == 5
        assert ids == [c.id for c in calls]

    @pytest.mark.asyncio
    async def test_repeated_drains_are_idempotent_when_empty(self):
        runner = AgentRunner()
        inbox = SteeringInbox()
        spec = _spec(MagicMock(), steering_inbox=inbox)
        messages: list[dict] = []
        for _ in range(5):
            assert await runner._drain_steering(spec, messages) == 0
        assert messages == []