"""Tests for internal turn continuation policy."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.session.goal_state import (
    GOAL_STATE_KEY,
    explicit_goal_requested,
    sustained_goal_turn,
)
from nanobot.session.turn_continuation import (
    INTERNAL_CONTINUATION_KIND_META,
    INTERNAL_CONTINUATION_META,
    INTERNAL_CONTINUATION_PENDING_META,
    INTERNAL_CONTINUATION_RUN_STARTED_AT_META,
    _save_skip_for_turn,
    internal_continuation_pending,
    internal_continuation_run_started_at,
    maybe_continue_turn,
    should_finalize_on_max_iterations,
    should_stream_budget_response,
)


@pytest.mark.asyncio
async def test_maybe_continue_turn_queues_internal_message():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Finish the migration.",
            "ui_summary": "migration",
        },
    }
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "paused"},
    ]
    pending: asyncio.Queue[InboundMessage] = asyncio.Queue()
    ctx = SimpleNamespace(
        session=SimpleNamespace(metadata=meta),
        msg=InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="c1",
            content="start",
            metadata={
                "message_id": "msg-1",
                "origin_message_id": "msg-0",
                "_wants_stream": True,
                "webui": True,
                "original_command": "/goal",
                "goal_requested": True,
            },
        ),
        session_key="feishu:c1",
        pending_queue=pending,
        stop_reason="max_iterations",
        final_content="paused",
        all_messages=messages,
        suppress_response=False,
        visible_run_started_at=1234.5,
    )

    assert await maybe_continue_turn(ctx) is True

    queued = pending.get_nowait()
    assert queued.sender_id == "system:continuation"
    assert queued.metadata[INTERNAL_CONTINUATION_META] is True
    assert queued.metadata[INTERNAL_CONTINUATION_KIND_META] == "sustained_goal"
    assert queued.metadata[INTERNAL_CONTINUATION_RUN_STARTED_AT_META] == 1234.5
    assert internal_continuation_run_started_at(queued.metadata) == 1234.5
    assert internal_continuation_pending(ctx.msg.metadata)
    assert queued.metadata["webui"] is True
    assert queued.metadata["message_id"] == "msg-1"
    assert queued.metadata["origin_message_id"] == "msg-0"
    assert queued.metadata["_wants_stream"] is True
    assert not explicit_goal_requested(queued.metadata)
    assert sustained_goal_turn(meta, message_metadata=queued.metadata)
    assert "Finish the migration." in queued.content
    assert ctx.all_messages == messages[:-1]
    assert ctx.final_content == ""
    assert ctx.suppress_response is True
    assert ctx.msg.metadata[INTERNAL_CONTINUATION_PENDING_META] is True
    assert meta["_sustained_goal_continuation_rounds"] == 1


@pytest.mark.asyncio
async def test_internal_continuation_respects_round_limit():
    meta = {
        GOAL_STATE_KEY: {"status": "active", "objective": "x"},
        "_sustained_goal_continuation_rounds": 12,
    }
    ctx = SimpleNamespace(
        session=SimpleNamespace(metadata=meta),
        msg=InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="start"),
        session_key="feishu:c1",
        pending_queue=asyncio.Queue(),
        stop_reason="max_iterations",
        final_content="paused",
        all_messages=[],
    )

    assert should_stream_budget_response(
        stop_reason="max_iterations",
        pending_queue_available=True,
        session_metadata=meta,
    )
    assert await maybe_continue_turn(ctx) is False


def test_internal_continuation_requires_budget_boundary_and_queue():
    meta = {GOAL_STATE_KEY: {"status": "active", "objective": "x"}}

    assert should_stream_budget_response(
        stop_reason="completed",
        pending_queue_available=True,
        session_metadata=meta,
    )
    assert should_stream_budget_response(
        stop_reason="max_iterations",
        pending_queue_available=False,
        session_metadata=meta,
    )
    assert not should_finalize_on_max_iterations(
        pending_queue_available=True,
        session_metadata=meta,
    )
    assert should_finalize_on_max_iterations(
        pending_queue_available=False,
        session_metadata=meta,
    )
    assert should_finalize_on_max_iterations(
        pending_queue_available=True,
        session_metadata={},
    )


def test_save_skip_matches_prefix_when_current_message_merged():
    skip = _save_skip_for_turn(
        message_metadata=None,
        initial_message_count=2,  # [system, merged user]
        history_count=1,
        input_persisted_early=True,
    )
    assert skip == 2


def test_save_skip_unchanged_for_standalone_current_message():
    # [system, history user, current user] with the current user already saved.
    assert _save_skip_for_turn(
        message_metadata=None,
        initial_message_count=3,
        history_count=1,
        input_persisted_early=True,
    ) == 3
    assert _save_skip_for_turn(
        message_metadata=None,
        initial_message_count=3,
        history_count=1,
        input_persisted_early=False,
    ) == 2


# --------------------------------------------------------------------------- #
# Stall resume: a long coding task that stops without deciding it is finished   #
# (blank reply / hard tool failure) must continue instead of dying mid-task.    #
# --------------------------------------------------------------------------- #


def test_stall_stop_reasons_are_resumable():
    from nanobot.session.turn_continuation import stall_is_resumable

    # These are not decisions: the model produced nothing or a tool blew up.
    assert stall_is_resumable("empty_final_response") is True
    assert stall_is_resumable("tool_error") is True
    # A genuine completion or a user cancellation must never be resumed.
    assert stall_is_resumable("completed") is False
    assert stall_is_resumable("cancelled") is False
    assert stall_is_resumable("credit_exhausted") is False
    assert stall_is_resumable("") is False


def test_stall_suppresses_user_visible_finalization():
    """The user must not see a 'stopped' message when a resume is queued."""
    for reason in ("empty_final_response", "tool_error"):
        assert (
            should_stream_budget_response(
                stop_reason=reason,
                pending_queue_available=True,
                session_metadata={},
            )
            is False
        )
    # Without a queue there is nothing to resume with, so the turn reports.
    assert (
        should_stream_budget_response(
            stop_reason="empty_final_response",
            pending_queue_available=False,
            session_metadata={},
        )
        is True
    )
    # A completed turn is always reported.
    assert (
        should_stream_budget_response(
            stop_reason="completed",
            pending_queue_available=True,
            session_metadata={},
        )
        is True
    )


@pytest.mark.asyncio
async def test_stall_resume_queues_continuation_without_sustained_goal():
    """A stalled plain task resumes even with no sustained goal active."""
    meta: dict = {}
    ctx = SimpleNamespace(
        session=SimpleNamespace(metadata=meta),
        msg=InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="build it"),
        session_key="feishu:c1",
        pending_queue=asyncio.Queue(),
        stop_reason="empty_final_response",
        final_content="",
        all_messages=[{"role": "assistant", "content": ""}],
        visible_run_started_at=None,
        suppress_response=False,
    )

    assert await maybe_continue_turn(ctx) is True
    assert ctx.suppress_response is True
    assert ctx.final_content == ""
    queued = ctx.pending_queue.get_nowait()
    # Marked as an internal continuation so it is not persisted as user input.
    assert queued.metadata[INTERNAL_CONTINUATION_META] is True
    assert queued.metadata[INTERNAL_CONTINUATION_KIND_META] == "stall_resume"
    assert INTERNAL_CONTINUATION_PENDING_META in ctx.msg.metadata
    # The prompt tells the model the task is unfinished and to resume it.
    assert "NOT finished" in queued.content
    # The stall budget advanced so a pathological loop is eventually bounded.
    assert meta["_auto_resume_continuation_rounds"] == 1
    # The synthetic blank assistant message is not written to history.
    assert ctx.all_messages == []


@pytest.mark.asyncio
async def test_stall_resume_is_bounded():
    """A model that keeps stalling is not resumed forever."""
    from nanobot.session.turn_continuation import _MAX_AUTO_RESUME_ROUNDS

    meta: dict = {"_auto_resume_continuation_rounds": _MAX_AUTO_RESUME_ROUNDS}
    ctx = SimpleNamespace(
        session=SimpleNamespace(metadata=meta),
        msg=InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="loop"),
        session_key="feishu:c1",
        pending_queue=asyncio.Queue(),
        stop_reason="tool_error",
        final_content="boom",
        all_messages=[],
        visible_run_started_at=None,
        suppress_response=False,
    )

    assert await maybe_continue_turn(ctx) is False
    assert ctx.pending_queue.empty()
    # The turn is reported so the user is not left waiting on a dead loop.
    assert (
        should_stream_budget_response(
            stop_reason="tool_error",
            pending_queue_available=True,
            session_metadata=meta,
        )
        is True
    )


@pytest.mark.asyncio
async def test_stall_resume_does_not_fire_without_pending_queue():
    ctx = SimpleNamespace(
        session=SimpleNamespace(metadata={}),
        msg=InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="x"),
        session_key="feishu:c1",
        pending_queue=None,
        stop_reason="empty_final_response",
        final_content="",
        all_messages=[],
        visible_run_started_at=None,
        suppress_response=False,
    )
    assert await maybe_continue_turn(ctx) is False


@pytest.mark.asyncio
async def test_stall_resume_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv("NANOBOT_STALL_RESUME_ENABLED", "0")
    ctx = SimpleNamespace(
        session=SimpleNamespace(metadata={}),
        msg=InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="x"),
        session_key="feishu:c1",
        pending_queue=asyncio.Queue(),
        stop_reason="empty_final_response",
        final_content="",
        all_messages=[],
        visible_run_started_at=None,
        suppress_response=False,
    )

    assert await maybe_continue_turn(ctx) is False
    # With resume disabled the user sees the boundary response again.
    assert (
        should_stream_budget_response(
            stop_reason="empty_final_response",
            pending_queue_available=True,
            session_metadata={},
        )
        is True
    )


def test_stall_resume_budget_resets_for_a_new_run():
    """A fresh user turn earns a fresh stall allowance."""
    from nanobot.session.turn_continuation import (
        clear_internal_continuation_state,
        reset_auto_resume_rounds,
    )

    meta = {"_auto_resume_continuation_rounds": 5}
    reset_auto_resume_rounds(meta)
    assert "_auto_resume_continuation_rounds" not in meta
    # Clearing state for a run with no pending continuation also resets it.
    meta2 = {"_auto_resume_continuation_rounds": 3}
    clear_internal_continuation_state(meta2)
    assert "_auto_resume_continuation_rounds" not in meta2
