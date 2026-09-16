"""Internal turn continuation helpers.

This module keeps budget-boundary continuation policy out of ``AgentLoop``.
The loop calls a small set of helpers; those helpers decide whether an internal
continuation is allowed and, when it is, queue the next turn directly.
"""

from __future__ import annotations

import dataclasses
import os
from typing import TYPE_CHECKING, Any, Mapping, MutableMapping

from loguru import logger

from nanobot.session.goal_state import (
    goal_state_runtime_lines,
    sustained_goal_active,
    sustained_goal_turn,
)

if TYPE_CHECKING:
    from nanobot.agent.loop import TurnContext

INTERNAL_CONTINUATION_META = "_internal_continuation"
INTERNAL_CONTINUATION_KIND_META = "_internal_continuation_kind"
INTERNAL_CONTINUATION_PENDING_META = "_internal_continuation_pending"
INTERNAL_CONTINUATION_RUN_STARTED_AT_META = "_internal_continuation_run_started_at"
SKIP_USER_PERSIST_META = "_skip_user_persist"

_GOAL_CONTINUATION_KIND = "sustained_goal"
_STALL_CONTINUATION_KIND = "stall_resume"
_GOAL_CONTINUATION_SENDER = "system:continuation"
_GOAL_CONTINUATION_ROUNDS_KEY = "_sustained_goal_continuation_rounds"
_MAX_GOAL_CONTINUATION_ROUNDS = 12

# Stop reasons that mean the run stalled rather than finished. The model never
# decided it was done: it produced a blank message, or a tool failed hard. Long
# coding tasks routinely end this way (a big test run, a long build), and before
# this policy they were terminal, so the task silently died mid-flight. These are
# treated as resumable so the work continues from the saved context.
_STALL_STOP_REASONS = frozenset({"empty_final_response", "tool_error"})
_AUTO_RESUME_ROUNDS_KEY = "_auto_resume_continuation_rounds"
_MAX_AUTO_RESUME_ROUNDS = 12
_STRIPPED_INBOUND_META_KEYS = {
    INTERNAL_CONTINUATION_PENDING_META,
    "goal_requested",
    "original_command",
}


def internal_continuation_inbound(metadata: Mapping[str, Any] | None) -> bool:
    """True for an inbound message created by an internal continuation policy."""
    return bool(metadata and metadata.get(INTERNAL_CONTINUATION_META) is True)


def internal_continuation_pending(metadata: Mapping[str, Any] | None) -> bool:
    """True when the current turn scheduled an invisible continuation slice."""
    return bool(metadata and metadata.get(INTERNAL_CONTINUATION_PENDING_META) is True)


def internal_continuation_run_started_at(metadata: Mapping[str, Any] | None) -> float | None:
    """Return the user-visible run start propagated across continuation slices."""
    if not metadata:
        return None
    value = metadata.get(INTERNAL_CONTINUATION_RUN_STARTED_AT_META)
    if not isinstance(value, int | float):
        return None
    started_at = float(value)
    return started_at if started_at > 0 else None


def should_persist_user_message(metadata: Mapping[str, Any] | None) -> bool:
    """Return whether this inbound message should be persisted as user input."""
    if metadata and metadata.get(SKIP_USER_PERSIST_META) is True:
        return False
    return not internal_continuation_inbound(metadata)


def should_stream_budget_response(
    *,
    stop_reason: str,
    pending_queue_available: bool,
    session_metadata: Mapping[str, Any] | None,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether the budget-boundary response should be sent to the user.

    Suppressed whenever a continuation slice owns the boundary — either a
    sustained goal continuing past its tool budget, or a stalled run being
    resumed — so the user does not see a "stopped" message mid-task.
    """
    if _resumable_boundary(
        stop_reason=stop_reason,
        pending_queue_available=pending_queue_available,
        session_metadata=session_metadata,
        message_metadata=message_metadata,
    ):
        return False
    return True


def should_finalize_on_max_iterations(
    *,
    pending_queue_available: bool,
    session_metadata: Mapping[str, Any] | None,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether a max-iteration boundary should produce a final response.

    When a sustained goal can continue internally, the current runner slice
    should stop without spending an extra no-tools finalization call. The next
    queued continuation slice owns the eventual user-visible response.
    """
    return not (
        pending_queue_available
        and _goal_continuation_available(
            session_metadata,
            message_metadata=message_metadata,
        )
    )


def stall_is_resumable(stop_reason: str) -> bool:
    """True when *stop_reason* means the run stalled instead of finishing.

    ``empty_final_response`` and ``tool_error`` are not decisions — the model
    produced a blank reply, or a tool failed. Long coding tasks frequently end
    this way, and treating them as terminal is what made long-running work stop
    mid-task. Callers use this to continue instead of surfacing a dead turn.
    """
    return str(stop_reason or "") in _STALL_STOP_REASONS


def _auto_resume_available(
    *,
    stop_reason: str,
    pending_queue_available: bool,
    session_metadata: Mapping[str, Any] | None,
    message_metadata: Mapping[str, Any] | None = None,
    max_rounds: int = _MAX_AUTO_RESUME_ROUNDS,
) -> bool:
    """Return whether a stalled run may be resumed automatically.

    Applies to every session, not just sustained goals, because a stalled coding
    task is the common case. The round budget bounds a pathological loop: a model
    that keeps stalling is not resumed forever.
    """
    if not stall_is_resumable(stop_reason) or not pending_queue_available:
        return False
    if internal_continuation_rounds(session_metadata, _AUTO_RESUME_ROUNDS_KEY) >= max(0, max_rounds):
        return False
    if max_rounds <= 0:
        return False
    return True


def _resumable_boundary(
    *,
    stop_reason: str,
    pending_queue_available: bool,
    session_metadata: Mapping[str, Any] | None,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """True when any continuation policy owns the boundary.

    Both the sustained-goal budget policy and the stall-resume policy suppress
    the user-visible finalization, because the queued continuation slice owns the
    eventual response.
    """
    if (
        pending_queue_available
        and stop_reason == "max_iterations"
        and _goal_continuation_available(
            session_metadata,
            message_metadata=message_metadata,
        )
    ):
        return True
    return _auto_resume_available_with_env(
        stop_reason=stop_reason,
        pending_queue_available=pending_queue_available,
        session_metadata=session_metadata,
        message_metadata=message_metadata,
    )


def internal_continuation_rounds(
    metadata: Mapping[str, Any] | None, key: str
) -> int:
    """Read a continuation round counter, tolerating absent/garbage values."""
    try:
        return int((metadata or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


async def maybe_continue_turn(ctx: TurnContext) -> bool:
    """Queue an internal continuation for *ctx* when policy allows it.

    Two policies can own the boundary:

    * a **sustained goal** that reached its tool-call budget continues (existing
      behaviour);
    * a **stalled run** (blank reply or hard tool failure) is resumed so a long
      coding task does not silently stop mid-flight.
    """
    if ctx.session is None or ctx.pending_queue is None:
        return False

    goal_boundary = ctx.stop_reason == "max_iterations" and _goal_continuation_available(
        ctx.session.metadata,
        message_metadata=ctx.msg.metadata,
    )
    stall_boundary = _auto_resume_available_with_env(
        stop_reason=ctx.stop_reason,
        pending_queue_available=True,
        session_metadata=ctx.session.metadata,
        message_metadata=ctx.msg.metadata,
    )
    if not goal_boundary and not stall_boundary:
        return False

    metadata = _internal_continuation_metadata(
        ctx.msg.metadata,
        run_started_at=ctx.visible_run_started_at,
        kind=_GOAL_CONTINUATION_KIND if goal_boundary else _STALL_CONTINUATION_KIND,
    )
    content = (
        _goal_continuation_prompt(ctx.session.metadata)
        if goal_boundary
        else _stall_resume_prompt(ctx.stop_reason)
    )
    messages = _strip_terminal_assistant(ctx.all_messages, ctx.final_content)
    if goal_boundary:
        _increment_goal_continuation_round(ctx.session.metadata)
    else:
        _increment_auto_resume_round(ctx.session.metadata)

    logger.info(
        "Turn stalled ({}) with work outstanding; scheduling internal continuation",
        ctx.stop_reason,
    )
    ctx.msg.metadata[INTERNAL_CONTINUATION_PENDING_META] = True
    ctx.final_content = ""
    ctx.all_messages = messages
    ctx.suppress_response = True
    await ctx.pending_queue.put(
        dataclasses.replace(
            ctx.msg,
            sender_id=_GOAL_CONTINUATION_SENDER,
            content=content,
            media=[],
            metadata=metadata,
            session_key_override=ctx.session_key,
        )
    )
    return True


def prepare_save_boundary(ctx: TurnContext) -> None:
    """Prepare continuation bookkeeping and the history append boundary."""
    if ctx.session is not None:
        clear_internal_continuation_state(ctx.session.metadata)

    ctx.save_skip = _save_skip_for_turn(
        message_metadata=ctx.msg.metadata,
        initial_message_count=len(ctx.initial_messages),
        history_count=len(ctx.history),
        input_persisted_early=ctx.input_persisted_early,
    )


def clear_internal_continuation_state(metadata: MutableMapping[str, Any]) -> None:
    """Reset policy bookkeeping once its owning runtime mode is inactive."""
    if not sustained_goal_active(metadata):
        reset_goal_continuation_rounds(metadata)
    # The stall-resume budget is per run, not per goal: a fresh user turn earns a
    # fresh allowance so a later task is not blocked by an earlier stall.
    if not internal_continuation_pending(metadata):
        reset_auto_resume_rounds(metadata)


def reset_goal_continuation_rounds(metadata: MutableMapping[str, Any]) -> None:
    """Start a newly created or replaced goal with a fresh continuation budget."""
    metadata.pop(_GOAL_CONTINUATION_ROUNDS_KEY, None)


def reset_auto_resume_rounds(metadata: MutableMapping[str, Any]) -> None:
    """Clear the stall-resume budget for a new user-visible run."""
    metadata.pop(_AUTO_RESUME_ROUNDS_KEY, None)


def _increment_auto_resume_round(session_metadata: MutableMapping[str, Any]) -> None:
    rounds = internal_continuation_rounds(session_metadata, _AUTO_RESUME_ROUNDS_KEY)
    session_metadata[_AUTO_RESUME_ROUNDS_KEY] = rounds + 1


def auto_resume_enabled() -> bool:
    """Whether a stalled run may be resumed. Operators disable via env."""
    value = str(os.getenv("NANOBOT_STALL_RESUME_ENABLED", "") or "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _stall_resume_prompt(stop_reason: str) -> str:
    """Prompt used to resume a run that stalled without finishing."""
    reason = (
        "the previous step produced an empty response"
        if stop_reason == "empty_final_response"
        else "a tool call failed"
    )
    return (
        f"The previous step stopped unexpectedly because {reason}. The task is "
        "NOT finished. Resume it now from the saved context: re-check the "
        "current state (inspect files, re-run the failing or incomplete step), "
        "then continue until the objective is genuinely complete. Do not "
        "restate the plan or apologise for the interruption, and do not mention "
        "this continuation boundary to the user."
    )


def _auto_resume_available_with_env(
    *,
    stop_reason: str,
    pending_queue_available: bool,
    session_metadata: Mapping[str, Any] | None,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Environment-gated variant of :func:`_auto_resume_available`."""
    if not auto_resume_enabled():
        return False
    return _auto_resume_available(
        stop_reason=stop_reason,
        pending_queue_available=pending_queue_available,
        session_metadata=session_metadata,
        message_metadata=message_metadata,
    )


def _save_skip_for_turn(
    *,
    message_metadata: Mapping[str, Any] | None,
    initial_message_count: int,
    history_count: int,
    input_persisted_early: bool,
) -> int:
    """Return the persisted-message append boundary for this turn."""
    if message_metadata and message_metadata.get(SKIP_USER_PERSIST_META) is True:
        return initial_message_count
    if internal_continuation_inbound(message_metadata):
        return initial_message_count
    # build_messages may merge the current message into a same-role history tail.
    # Runner-appended messages start at initial_message_count in either shape.
    has_standalone_current = initial_message_count > 1 + history_count
    if has_standalone_current and not input_persisted_early:
        return initial_message_count - 1
    return initial_message_count


def _goal_continuation_available(
    session_metadata: Mapping[str, Any] | None,
    *,
    message_metadata: Mapping[str, Any] | None = None,
    max_rounds: int = _MAX_GOAL_CONTINUATION_ROUNDS,
) -> bool:
    if not sustained_goal_turn(session_metadata, message_metadata=message_metadata):
        return False
    if not sustained_goal_active(session_metadata):
        return False
    try:
        rounds = int((session_metadata or {}).get(_GOAL_CONTINUATION_ROUNDS_KEY) or 0)
    except (TypeError, ValueError):
        rounds = 0
    return rounds < max(0, max_rounds)


def _increment_goal_continuation_round(session_metadata: MutableMapping[str, Any]) -> None:
    try:
        rounds = int(session_metadata.get(_GOAL_CONTINUATION_ROUNDS_KEY) or 0)
    except (TypeError, ValueError):
        rounds = 0
    session_metadata[_GOAL_CONTINUATION_ROUNDS_KEY] = rounds + 1


def _internal_continuation_metadata(
    message_metadata: Mapping[str, Any] | None,
    *,
    run_started_at: float | None = None,
    kind: str = _GOAL_CONTINUATION_KIND,
) -> dict[str, Any]:
    metadata = dict(message_metadata or {})
    metadata[INTERNAL_CONTINUATION_META] = True
    metadata[INTERNAL_CONTINUATION_KIND_META] = kind
    if run_started_at is not None:
        metadata[INTERNAL_CONTINUATION_RUN_STARTED_AT_META] = float(run_started_at)
    for key in _STRIPPED_INBOUND_META_KEYS:
        metadata.pop(key, None)
    return metadata


def _goal_continuation_prompt(metadata: Mapping[str, Any] | None) -> str:
    lines = goal_state_runtime_lines(metadata)
    if lines:
        goal = "\n".join(lines)
        return (
            "Continue the active sustained goal after the previous turn reached "
            "its tool-call budget.\n\n"
            f"{goal}\n\n"
            "Continue from the saved context. Do not mention the continuation "
            "boundary to the user. Use tools as needed, and call update_goal "
            "with action='complete' when the objective is truly finished."
        )
    return (
        "Continue the active sustained goal after the previous turn reached "
        "its tool-call budget. Continue from the saved context. Do not mention "
        "the continuation boundary to the user. Use tools as needed, and call "
        "update_goal with action='complete' when the objective is truly finished."
    )


def _strip_terminal_assistant(
    messages: list[dict[str, Any]],
    final_content: str | None,
) -> list[dict[str, Any]]:
    """Drop the synthetic max-iteration assistant message before saving history."""
    if not messages:
        return messages
    last = messages[-1]
    if last.get("role") != "assistant":
        return messages
    if final_content is None or last.get("content") != final_content:
        return messages
    if last.get("tool_calls"):
        return messages
    return messages[:-1]
