"""Shared execution loop for tool-using agents."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from loguru import logger

from nanobot.agent.context_governance import (
    ContextGovernanceConfig,
    ContextGovernor,
)
from nanobot.agent.deterministic_router import deterministic_plan, router_enabled
from nanobot.agent.shape_router import (
    plan_preference_message,
    shape_router_enabled,
    should_steer_to_plan,
    steer_message_for,
)
from nanobot.agent.hook import AgentHook, AgentHookContext, AgentRunHookContext
from nanobot.agent.hooks.supabase_credit import CreditExhaustedError
from nanobot.agent.plan_cache import (
    make_plan_cache,
    normalize_task,
    plan_cache_enabled,
    plan_is_safe,
    substitute_variables,
    variables_compatible,
)
from nanobot.agent.task_cache import make_replay_cache, task_fingerprint_text
from nanobot.agent.task_router import task_recipe_plan
from nanobot.agent.tool_middleware import ToolMiddleware
from nanobot.agent.tools.registry import (
    ToolRegistry,
    is_tool_error_result,
    is_tool_terminal_result,
)
from nanobot.providers.base import (
    LLMProvider,
    LLMResponse,
    ProviderCallContext,
    ProviderConversationState,
    ToolCallRequest,
)
from nanobot.providers.conversation_state import (
    ProviderConversationStateController,
    allows_conversation_message_merge,
)
from nanobot.runtime_context import (
    RUNTIME_CONTEXT_MESSAGE_META,
    detach_runtime_context,
    reattach_runtime_context,
)
from nanobot.session.history_visibility import is_hidden_history_message
from nanobot.utils.helpers import (
    IncrementalThinkExtractor,
    build_assistant_message,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    extract_reasoning,
    strip_reasoning_tags,
    strip_think,
)
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.utils.prompt_templates import render_template
from nanobot.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    build_budget_exhausted_finalization_message,
    build_finalization_retry_message,
    build_goal_continue_message,
    build_length_recovery_message,
    is_blank_text,
    repeated_external_lookup_error,
    repeated_workspace_violation_error,
)

GoalContinueMessage = str | Callable[[], str | None]
ProgressCallback = Callable[[str], Awaitable[None]]
RetryWaitCallback = Callable[[str], Awaitable[None]]
CheckpointCallback = Callable[[dict[str, Any]], Awaitable[None]]
InjectionCallback = Callable[..., Awaitable[Iterable[Any] | None]]

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_ARREARAGE_ERROR_MESSAGE = (
    "The AI provider rejected the request because the API key is out of quota or the "
    "account is in arrears. Please top up / check the billing status of your API key and try again."
)
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"
_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3
#: A ``finish_reason="length"`` response is only worth continuing when it made
#: textual progress. A blank segment, or one byte-identical to the segment we
#: just appended, means the model is re-emitting the same truncated prefix:
#: continuing would pay another provider call for zero new work, forever. This
#: is the burn-loop (documented in tools/run_plan.py) and it is refused here.
#:
#: Length is deliberately NOT part of that test. An earlier version also refused
#: any segment under 16 characters, which mistook an ordinary early truncation
#: for a burn loop and ended the turn: a short segment is novel content, and
#: refusing it threw away the rest of the answer plus any tool call the model was
#: about to make. _MAX_LENGTH_RECOVERIES already bounds the cost of a model that
#: only ever emits a little at a time, so no length floor is needed.
_MAX_INJECTIONS_PER_TURN = 3
_MAX_INJECTION_CYCLES = 5

#: How long one tool call may run before the agent says it is still running.
#:
#: MEASURED 2026-09-24, from the loop's own event order: ``before_execute_tools``
#: publishes the hint for what the agent is about to do ("checking ..."), and the
#: next thing published is ``after_iteration``, once the tool has already
#: returned. A tool that blocks in between is therefore announced EXACTLY ONCE
#: and then silent for as long as it takes -- a step label that can sit unchanged
#: for three minutes while the call is working perfectly.
#:
#: A frozen label is not a neutral state. It is indistinguishable from a hung
#: agent, so every option the user has is a wrong one: wait and hope, send
#: another message (which queues behind the same blocked turn), or give up.
#: Timestamped updates make "slow" read as slow instead of as broken.
#:
#: 20 s is deliberately far longer than a normal call -- most finish inside it
#: and emit nothing, so this costs nothing on a healthy turn -- and short enough
#: that a genuinely long call ticks visibly instead of going quiet.
#:
#: [PERF 2026-09-24] Lowered 20 s -> 8 s and made env-tunable. The narration only
#: fires on calls that are ALREADY slow (a healthy call returns inside the
#: window), so a shorter interval costs nothing on the healthy path and makes a
#: slow call read as slow two-and-a-half times sooner. Override with
#: NANOBOT_TOOL_HEARTBEAT_S when a deployment wants a different cadence.
DEFAULT_TOOL_HEARTBEAT_SECONDS = 8.0


def _resolve_heartbeat_seconds(env_name: str, module_default: float) -> float:
    """Return the effective heartbeat interval in seconds.

    Precedence: ``NANOBOT_*_HEARTBEAT_S`` env override, else the module-level
    constant. Falling back to the module global (rather than the DEFAULT_*
    literal) is deliberate: the constant stays the single knob tests monkeypatch,
    while operators get an env override -- and an env value of ``0`` keeps the
    old explicit opt-out behaviour of a very long interval.
    """
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return module_default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid {}={!r}; using {}", env_name, raw, module_default)
        return module_default
    return value if value > 0 else module_default


def _tool_heartbeat_seconds() -> float:
    return _resolve_heartbeat_seconds("NANOBOT_TOOL_HEARTBEAT_S", TOOL_HEARTBEAT_SECONDS)


def _model_heartbeat_seconds() -> float:
    return _resolve_heartbeat_seconds("NANOBOT_MODEL_HEARTBEAT_S", MODEL_HEARTBEAT_SECONDS)


def _tool_concurrency_limit() -> int:
    """Maximum tool calls allowed to run at once inside a parallel batch.

    [PERF 2026-09-24] The batched path below used a bare ``asyncio.gather`` with
    no ceiling. That was harmless while nothing ever emitted more than one tool
    call per turn, but once ``parallel_tool_calls`` is advertised the model can
    legitimately return a dozen calls in one response -- and an unbounded gather
    runs all of them simultaneously. A burst of heavy tools (several browser
    sessions, big reads, model-backed sub-tools) then contends for the same CPU
    and sockets and finishes SLOWER than a bounded pool, while also spiking
    memory. Capping keeps the parallelism that matters (a handful of genuinely
    independent I/O-bound calls) without letting one turn stampede the host.

    Default 6: high enough that ordinary batched reads are never throttled,
    low enough to stay polite on a small container. Override with
    NANOBOT_MAX_PARALLEL_TOOLS. A non-positive or invalid value falls back to
    the default rather than silently serialising everything.
    """
    raw = os.environ.get("NANOBOT_MAX_PARALLEL_TOOLS")
    if raw is None or not raw.strip():
        return 6
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid NANOBOT_MAX_PARALLEL_TOOLS={!r}; using 6", raw)
        return 6
    return value if value > 0 else 6


async def _bounded_gather(awaitables: list[Any], limit: int) -> list[Any]:
    """``asyncio.gather`` with at most *limit* awaitables in flight at a time.

    Results keep the input order, matching ``asyncio.gather``, so callers do not
    need to know whether the batch was throttled. Exceptions propagate the same
    way they would from a plain gather.
    """
    if limit <= 1 or len(awaitables) <= 1:
        # Nothing to bound: a single awaitable, or an explicit request to
        # serialise. Await them in order.
        return [await item for item in awaitables]

    semaphore = asyncio.Semaphore(limit)

    async def _guarded(item: Any) -> Any:
        async with semaphore:
            return await item

    return list(await asyncio.gather(*(_guarded(item) for item in awaitables)))


TOOL_HEARTBEAT_SECONDS = DEFAULT_TOOL_HEARTBEAT_SECONDS

#: How long a model request may produce NOTHING before the turn says so.
#:
#: Shorter than the tool interval on purpose: a model request is on the
#: critical path of every single iteration, so its silence is the one users hit
#: most, whereas a long tool call is the exception. Still long enough that a
#: normal request -- which answers in a couple of seconds -- never emits here.
#:
#: [PERF 2026-09-24] Lowered 15 s -> 6 s for the same reason as the tool
#: interval: a healthy request is silent because it finished, not because it is
#: stuck, so a tighter window only changes how a genuinely slow request reads.
#: Override with NANOBOT_MODEL_HEARTBEAT_S.
DEFAULT_MODEL_HEARTBEAT_SECONDS = 6.0
MODEL_HEARTBEAT_SECONDS = DEFAULT_MODEL_HEARTBEAT_SECONDS


def _normalize_for_drift(text: str) -> str:
    """Coarse whitespace normalization for comparing a replayed step's output
    against what it produced when the plan was learned. We deliberately do NOT
    normalize volatile tokens (timestamps, PIDs, durations) — those legitimately
    change run-to-run and would make every replay look "drifted". Instead we
    compare only on exact content equality after collapsing runs of whitespace;
    a sandbox command whose real output changed (file edited, test added) will
    differ in substance and be caught. Commands that merely print a fresh clock
    value are rare among safe coding steps and, if they drift, simply cost one
    relearn — never a wrong answer.
    """
    return re.sub(r"\s+", " ", text or "").strip()



def _restore_outer_whitespace(content: str, original: str | None) -> str:
    """Restore boundary whitespace stripped while cleaning one recovered segment."""
    if not original:
        return content
    leading_size = len(original) - len(original.lstrip())
    trailing_size = len(original) - len(original.rstrip())
    leading = original[:leading_size]
    trailing = original[-trailing_size:] if trailing_size else ""
    return f"{leading}{content}{trailing}"


@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution."""

    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    runtime: LLMRuntime
    max_iterations: int
    max_tool_result_chars: int
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False
    workspace: Path | None = None
    session_key: str | None = None
    context_block_limit: int | None = None
    provider_retry_mode: str = "rate_limit_aware"
    progress_callback: ProgressCallback | None = None
    stream_progress_deltas: bool = True
    retry_wait_callback: RetryWaitCallback | None = None
    checkpoint_callback: CheckpointCallback | None = None
    injection_callback: InjectionCallback | None = None
    llm_timeout_s: float | None = None
    goal_active_predicate: Callable[[], bool] | None = None
    goal_continue_message: GoalContinueMessage | None = None
    finalize_on_max_iterations: bool = True
    provider_state: ProviderConversationState | None = None
    # Telegram OCR turns must never send raw image blocks to a model. This
    # covers the first request, post-tool continuations, and finalization
    # retries, including provider-owned state restored from an earlier turn.
    strip_image_content_before_provider: bool = False
    # When True, an identical task (same user text) that was completed recently
    # replays its stored final answer with ZERO provider calls. Enabled in the
    # agent loop; tests opt in explicitly.
    enable_replay_cache: bool = False
    # When True and ``deterministic_router_text`` names an unambiguous read-only
    # UniAbuja ask, the run answers it by executing the matching registered tool
    # directly -- ZERO provider calls. Set by the agent loop for fresh Telegram
    # user turns (and by tests explicitly); generic runs never consult it.
    enable_deterministic_router: bool = False
    # The raw user text the deterministic router classifies. Kept as its own
    # field so the loop can pass the pre-runtime-context message exactly.
    deterministic_router_text: str | None = None
    # Opt-in for the zero-call tool middleware layer (nanobot.agent.tool_middleware):
    # short-TTL caching + singleflight for slow-changing read-only lookups, and
    # deterministic post-tool formatting that ends the turn without paying the
    # model to re-render JSON it never authored. Applies only to governed tool
    # names; every other call executes exactly as before.
    tool_middleware: bool = False
    # Opt-in for the zero-call TASK PLAN cache (nanobot.agent.plan_cache): a task
    # whose tool-step plan was learned once replays its sandbox steps directly on
    # any structurally-identical repeat, with ZERO provider calls. Fail-open —
    # an unlearned, expired, or failing plan falls back to the normal LLM path.
    enable_plan_cache: bool = False
    # Mutable one-element counter of how many times this run actually hit the
    # configured LLM provider (every distinct model request, including
    # finalization and no-tools fallbacks, but NOT internal provider retries).
    # Mirrors Manus's "API called: N" telemetry so efficiency is observable.
    # Initialized by _run_core; safe under concurrency because it is per-spec.
    llm_calls: list[int] = field(default_factory=list)


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of a shared agent execution."""

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    had_injections: bool = False
    # Terminal tail to emit when the preceding final-content prefix was already streamed.
    pending_stream_content: str | None = None
    provider_state: ProviderConversationState | None = field(default=None, repr=False)


class _ZeroCallComplete(BaseException):
    """Control-flow signal: middleware rendered a final answer, end the turn.

    Deliberately BaseException (like asyncio.CancelledError) so the generic
    ``except Exception`` guards scattered through tool execution cannot swallow
    it; only the loop's own tool-result handling catches and honours it.
    """

    def __init__(self, content: str) -> None:
        super().__init__("zero-call completion")
        self.content = content


class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns."""

    def __init__(self) -> None:
        self.context_governor = ContextGovernor()
        # Zero-call tool middleware (short-TTL cache + singleflight for governed
        # read-only lookups). Per-runner so cached live data never leaks across
        # unrelated processes, and the in-flight map shares this loop.
        self.tool_middleware = ToolMiddleware()

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [
                    cast(dict[str, Any], item)
                    if isinstance(item, dict)
                    else {"type": "text", "text": str(item)}
                    for item in cast(list[Any], value)
                ]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    @classmethod
    def _append_injected_messages(
        cls,
        messages: list[dict[str, Any]],
        injections: list[dict[str, Any]],
    ) -> None:
        """Append injected user messages while preserving role alternation."""
        for injection in injections:
            if (
                messages
                and injection.get("role") == "user"
                and messages[-1].get("role") == "user"
                and not is_hidden_history_message(injection)
                and not is_hidden_history_message(messages[-1])
                and allows_conversation_message_merge(messages[-1])
            ):
                merged = dict(messages[-1])
                left_meta = merged.get("_meta")
                right_meta = injection.get("_meta")
                left_meta_dict = cast(dict[str, Any], left_meta) if isinstance(left_meta, dict) else None
                right_meta_dict = (
                    cast(dict[str, Any], right_meta) if isinstance(right_meta, dict) else None
                )
                left_marker = (
                    left_meta_dict.get(RUNTIME_CONTEXT_MESSAGE_META)
                    if left_meta_dict is not None
                    else None
                )
                right_marker = (
                    right_meta_dict.get(RUNTIME_CONTEXT_MESSAGE_META)
                    if right_meta_dict is not None
                    else None
                )
                left_marker_dict = (
                    cast(dict[str, Any], left_marker) if isinstance(left_marker, dict) else None
                )
                right_marker_dict = (
                    cast(dict[str, Any], right_marker) if isinstance(right_marker, dict) else None
                )
                empty_sources: list[str] = []
                empty_blocks: list[dict[str, Any]] = []
                detached_left = (
                    detach_runtime_context(merged.get("content"), left_marker_dict)
                    if left_marker_dict is not None
                    else (merged.get("content"), empty_sources, empty_blocks)
                )
                detached_right = (
                    detach_runtime_context(injection.get("content"), right_marker_dict)
                    if right_marker_dict is not None
                    else (injection.get("content"), empty_sources, empty_blocks)
                )
                if detached_left is not None and detached_right is not None:
                    left_content, left_sources, left_blocks = detached_left
                    right_content, right_sources, right_blocks = detached_right
                    merged_content = cls._merge_message_content(left_content, right_content)
                    context_blocks = [*left_blocks, *right_blocks]
                    if context_blocks:
                        merged_content, marker = reattach_runtime_context(
                            merged_content,
                            [*left_sources, *right_sources],
                            context_blocks,
                        )
                        internal_meta = dict(left_meta_dict) if left_meta_dict is not None else {}
                        if right_meta_dict is not None:
                            for key, value in right_meta_dict.items():
                                internal_meta.setdefault(key, value)
                        internal_meta[RUNTIME_CONTEXT_MESSAGE_META] = marker
                        merged["_meta"] = internal_meta
                    merged["content"] = merged_content
                else:
                    merged["content"] = cls._merge_message_content(
                        merged.get("content"),
                        injection.get("content"),
                    )
                messages[-1] = merged
                continue
            messages.append(injection)

    async def _try_drain_injections(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        assistant_message: dict[str, Any] | None,
        injection_cycles: int,
        *,
        conversation_state: ProviderConversationStateController | None = None,
        phase: str = "after error",
        iteration: int | None = None,
        allow_goal_continue: bool = False,
    ) -> tuple[bool, int]:
        """Drain pending injections. Returns (should_continue, updated_cycles).

        If injections are found and we haven't exceeded _MAX_INJECTION_CYCLES,
        append them to *messages* (and emit a checkpoint if *assistant_message*
        and *iteration* are both provided) and return (True, cycles+1) so the
        caller continues the iteration loop.  Otherwise return (False, cycles).
        """
        injections: list[dict[str, Any]] = []
        real_injection = False
        if injection_cycles < _MAX_INJECTION_CYCLES:
            injections = await self._drain_injections(spec)
            real_injection = bool(injections)
        if not injections and allow_goal_continue and assistant_message is not None:
            predicate = spec.goal_active_predicate
            if predicate is not None and predicate():
                injections = [self._build_goal_continue_message(spec)]
        if not injections:
            return False, injection_cycles
        if real_injection:
            injection_cycles += 1
        if assistant_message is not None:
            messages.append(assistant_message)
            if iteration is not None:
                checkpoint: dict[str, Any] = {
                    "phase": "final_response",
                    "iteration": iteration,
                    "model": spec.runtime.model,
                    "assistant_message": assistant_message,
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                }
                if conversation_state is not None:
                    checkpoint["provider_state"] = conversation_state.checkpoint(
                        messages
                    )
                await self._emit_checkpoint(
                    spec,
                    checkpoint,
                )
        self._append_injected_messages(messages, injections)
        if real_injection:
            logger.info(
                "Injected {} follow-up message(s) {} ({}/{})",
                len(injections), phase, injection_cycles, _MAX_INJECTION_CYCLES,
            )
        else:
            logger.info("Injected sustained-goal continuation {}", phase)
        return True, injection_cycles

    def _build_goal_continue_message(self, spec: AgentRunSpec) -> dict[str, str]:
        custom = spec.goal_continue_message
        if callable(custom):
            try:
                custom = custom()
            except Exception:
                logger.exception("goal_continue_message callback failed")
                custom = None
        return build_goal_continue_message(custom)

    async def _drain_injections(self, spec: AgentRunSpec) -> list[dict[str, Any]]:
        """Drain pending user messages via the injection callback.

        Returns normalized user messages (capped by
        ``_MAX_INJECTIONS_PER_TURN``), or an empty list when there is
        nothing to inject. Messages beyond the cap are logged so they
        are not silently lost.
        """
        if spec.injection_callback is None:
            return []
        try:
            signature = inspect.signature(spec.injection_callback)
            accepts_limit = (
                "limit" in signature.parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )
            if accepts_limit:
                items = await spec.injection_callback(limit=_MAX_INJECTIONS_PER_TURN)
            else:
                items = await spec.injection_callback()
        except Exception:
            logger.exception("injection_callback failed")
            return []
        if not items:
            return []
        injected_messages: list[dict[str, Any]] = []
        for item in items:
            if item is None:
                continue
            if isinstance(item, dict):
                message_item = cast(dict[str, Any], item)
                if message_item.get("role") == "user" and "content" in message_item:
                    if self._has_injection_content(message_item.get("content")):
                        injected_messages.append(message_item)
                continue
            content = getattr(item, "content") if hasattr(item, "content") else str(item)
            if self._has_injection_content(content):
                injected_messages.append({"role": "user", "content": content})
        if len(injected_messages) > _MAX_INJECTIONS_PER_TURN:
            dropped = len(injected_messages) - _MAX_INJECTIONS_PER_TURN
            logger.warning(
                "Injection callback returned {} messages, capping to {} ({} dropped)",
                len(injected_messages), _MAX_INJECTIONS_PER_TURN, dropped,
            )
            injected_messages = injected_messages[:_MAX_INJECTIONS_PER_TURN]
        return injected_messages

    @staticmethod
    def _has_injection_content(content: Any) -> bool:
        if content is None:
            return False
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            return bool(cast(list[Any], content))
        return True

    @staticmethod
    def _sanitize_image_content_for_provider(
        messages: list[dict[str, Any]],
        state: ProviderConversationState | None,
    ) -> ProviderConversationState | None:
        """Fail closed before a model request, including resumed state.

        Telegram OCR turns must never rely on a provider error to discover
        that a model cannot accept images. Public transcript messages are
        scrubbed in place; pending provider messages are copied and scrubbed;
        opaque provider payloads containing image blocks are discarded because
        they cannot be safely rewritten generically.
        """
        LLMProvider._strip_image_content_inplace(messages)
        if state is None:
            return None
        if LLMProvider._contains_image_content(state.payload):
            return None
        pending_messages = deepcopy(state.pending_messages)
        if LLMProvider._strip_image_content_inplace(pending_messages):
            return state.with_pending_messages(pending_messages)
        return state

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        hook = spec.hook or AgentHook()
        messages = list(spec.initial_messages)
        context = AgentRunHookContext(messages=deepcopy(messages))

        try:
            await hook.before_run(context)
            result = await self._run_core(spec, hook, messages)
        except asyncio.CancelledError as exc:
            context.messages = deepcopy(messages)
            context.stop_reason = "cancelled"
            context.error = None
            context.exception = exc
            raise
        except Exception as exc:
            if isinstance(exc, CreditExhaustedError):
                message = str(exc)
                context.messages = deepcopy(messages)
                context.stop_reason = "credit_exhausted"
                context.error = message
                context.final_content = message
                context.exception = None
                return AgentRunResult(
                    final_content=message,
                    messages=messages,
                    stop_reason="credit_exhausted",
                    error=message,
                )
            context.messages = deepcopy(messages)
            context.stop_reason = "error"
            context.error = f"Error: {type(exc).__name__}: {exc}"
            context.exception = exc
            await hook.on_error(context)
            raise
        else:
            context.messages = deepcopy(result.messages)
            context.final_content = result.final_content
            context.tools_used = list(result.tools_used)
            context.usage = dict(result.usage)
            context.stop_reason = result.stop_reason
            context.error = result.error
            context.tool_events = deepcopy(result.tool_events)
            context.had_injections = result.had_injections
            context.exception = None
            if context.error is not None:
                await hook.on_error(context)
            await hook.after_run(context)
            return result
        finally:
            context.messages = deepcopy(messages)
            if context.exception is None:
                await hook.on_finally(context)
            else:
                try:
                    await hook.on_finally(context)
                except Exception:
                    logger.exception(
                        "AgentHook.on_finally error after {}",
                        context.stop_reason or "run exception",
                    )

    async def _run_core(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
    ) -> AgentRunResult:
        final_content: str | None = None
        tools_used: list[str] = []
        # Ordered (name, arguments) of every tool the model actually chose this
        # run, captured so a successful completion can be stored as a replayable
        # task PLAN for zero-LLM repeats (see nanobot.agent.plan_cache).
        recorded_steps: list[dict[str, Any]] = []
        # Parallel to recorded_steps: the stdout each step produced at learn
        # time. Stored with the plan so replay can detect content drift and
        # refuse a stale answer (see _try_plan_replay).
        recorded_outputs: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        # Manus-style cost telemetry: how many times this run really hit the
        # configured LLM provider. Starts empty (0 visible calls); bumped once per
        # distinct model request by _request_model / _request_no_tools. Zero-call
        # replays, plan replays, deterministic-router answers and middleware
        # hits never touch it, so it honestly mirrors "API called: N".
        if not spec.llm_calls:
            spec.llm_calls.append(0)
        error: str | None = None
        stop_reason = "completed"
        tool_events: list[dict[str, str]] = []
        external_lookup_counts: dict[str, int] = {}
        # Per-turn throttle for repeated attempts against the same outside target.
        workspace_violation_counts: dict[str, int] = {}
        empty_content_retries = 0
        # Segments from one uninterrupted length-recovery chain. Tool work or
        # injected user input starts a new logical answer and clears the chain.
        length_recovery_parts: list[str] = []
        had_injections = False
        injection_cycles = 0
        compacted_tool_call_ids: set[str] = set()
        repeat_tool_state: dict[str, Any] = {"fingerprint": None, "count": 0}
        pending_stream_content: str | None = None
        provider_state = spec.provider_state
        if spec.strip_image_content_before_provider:
            provider_state = self._sanitize_image_content_for_provider(
                messages,
                provider_state,
            )
        conversation_state = ProviderConversationStateController(
            provider=spec.runtime.provider,
            model=spec.runtime.model,
            messages=messages,
            state=provider_state,
        )

        # --- Zero-call replay: identical task already completed recently -----
        # Fingerprint the user's task text; an exact repeat (same wording, same
        # workspace) served from disk costs ZERO provider calls and ZERO credit
        # steps. Only consulted when the run opts in via enable_replay_cache.
        replay_cache = None
        replay_task_text = ""
        cached_replay: str | None = None
        if spec.enable_replay_cache:
            replay_cache = make_replay_cache(spec.workspace)
            replay_task_text = task_fingerprint_text(spec.initial_messages)
            if replay_cache is not None and replay_task_text:
                cached_replay = replay_cache.get(replay_task_text)
        if cached_replay is not None:
            logger.info(
                "replaying identical task from cache for {} (0 provider calls)",
                spec.session_key or "default",
            )
            self._append_final_message(messages, cached_replay)
            final_content = cached_replay
            stop_reason = "completed"
            return AgentRunResult(
                final_content=final_content,
                messages=messages,
                tools_used=[],
                usage={"prompt_tokens": 0, "completion_tokens": 0, "replayed": 1, "llm_calls": 0},
                stop_reason=stop_reason,
                error=None,
                tool_events=[],
                had_injections=False,
                pending_stream_content=cached_replay,
                provider_state=conversation_state.finish(messages),
            )

        # --- Zero-call TASK PLAN replay: same task shape, sandbox re-runs ----
        # The model solved a structurally-identical task before; replay its
        # recorded tool steps directly in the sandbox with ZERO provider calls.
        # Only consulted when the run opts in via enable_plan_cache. Any step
        # failing or producing nothing falls through to the normal LLM path
        # (which will relearn and overwrite the plan) — never a wrong answer.
        plan_cache = None
        plan_norm = None
        if spec.enable_plan_cache and plan_cache_enabled():
            plan_cache = make_plan_cache(spec.workspace)
            # Fingerprint the user's own task text (same source the replay cache
            # uses) regardless of whether that layer is enabled for this run.
            plan_task_text = replay_task_text or task_fingerprint_text(spec.initial_messages)
            plan_norm = normalize_task(plan_task_text)
            if plan_cache is not None and plan_norm is not None:
                replayed = await self._try_plan_replay(
                    spec, plan_cache, plan_norm, messages, conversation_state
                )
                if replayed is not None:
                    return replayed

        # --- Zero-call deterministic router: rule answers read-only asks -------
        # A fresh, unambiguous UniAbuja lookup (status, announcements, my own
        # questions/records, an explicit regno read) is answered by executing
        # the SAME registered tool the model would call. No provider round-trip
        # and no credit step. Only consulted when the run opts in and the text
        # is present; a None plan or an unregistered tool falls through to the
        # normal LLM path untouched.
        deterministic_call: ToolCallRequest | None = None
        if spec.enable_deterministic_router and router_enabled() and spec.deterministic_router_text:
            deterministic_call = deterministic_plan(spec.deterministic_router_text)
        # --- Generic task recipe router (Manus-style command runner) ---------
        # When the narrow read-only router has nothing and the user's ask is a
        # recurring, read-only *coding/workspace* task (bug scan, run tests,
        # project structure), answer it with a single deterministic command
        # recipe — ZERO provider calls. The recipe runs where the user's code
        # lives: the sandbox (novita_sandbox -> Novita/VPS/Upstash) when the run
        # has one, else the local shell (exec). Fail-open: None falls through.
        if deterministic_call is None and spec.enable_deterministic_router and spec.deterministic_router_text:
            recipe_call = task_recipe_plan(spec.deterministic_router_text)
            if recipe_call is not None:
                deterministic_call = self._retarget_task_recipe(spec, recipe_call)
        if deterministic_call is not None and spec.tools.get(deterministic_call.name) is not None:
            logger.info(
                "deterministic router answering {} for {} (0 provider calls)",
                deterministic_call.name,
                spec.session_key or "default",
            )
            # The middleware layer may serve this lookup from the short-TTL live
            # cache (or collapse it into an in-flight duplicate) so even the
            # backend round-trip disappears for rapid repeats.
            tool_result = await self._middleware_execute(spec, deterministic_call)
            text_result = str(tool_result)
            self._append_final_message(messages, text_result)
            final_content = text_result
            stop_reason = "completed"
            return AgentRunResult(
                final_content=final_content,
                messages=messages,
                tools_used=[deterministic_call.name],
                usage={"prompt_tokens": 0, "completion_tokens": 0, "deterministic": 1, "llm_calls": 0},
                stop_reason=stop_reason,
                error=None,
                tool_events=[],
                had_injections=False,
                pending_stream_content=text_result,
                provider_state=conversation_state.finish(messages),
            )
        governance_config = ContextGovernanceConfig(
            provider=spec.runtime.provider,
            model=spec.runtime.model,
            tools=spec.tools,
            workspace=spec.workspace,
            session_key=spec.session_key,
            max_tool_result_chars=spec.max_tool_result_chars,
            context_window_tokens=spec.runtime.context_window_tokens,
            context_block_limit=spec.context_block_limit,
            max_tokens=spec.runtime.generation.max_tokens,
            inflight_start_index=len(spec.initial_messages),
        )

        # --- Deterministic SHAPE routing (Lever A: fewer DECISIONS) -----------
        # The Re-Act loop bills one provider call per decision. Batch-shaped and
        # clearly-chained asks ("for each of these 40 files, ...") do not need a
        # decision per item: the whole job is expressible as ONE `run_plan` (or
        # `python_code`) call. This classifies the task SHAPE with regexes --
        # never with an extra model call, which would defeat the purpose -- and
        # emits one short steering message for the model on that turn only.
        #
        # Deliberately narrow: anything exploratory, adaptive, conversational or
        # single-action classifies as not-multi_step and the turn runs EXACTLY as
        # it does today. The plan path commits the whole graph before any
        # observation, so force-steering exploratory work would trade graceful
        # partial progress for total failure. Re-Act stays the fallback.
        #
        # Scope guards mirror the deterministic router: image turns and turns
        # with an active sustained goal keep the full model path.
        steer_message: dict[str, Any] | None = None
        if (
            shape_router_enabled()
            and spec.deterministic_router_text is not None
            and not spec.strip_image_content_before_provider
            and should_steer_to_plan(
                spec.deterministic_router_text,
                plan_tool_available=spec.tools.get("run_plan") is not None,
                code_tool_available=spec.tools.get("python_code") is not None,
            )
        ):
            steer_message = steer_message_for(spec.deterministic_router_text)
            logger.info(
                "shape router steering {} to the one-call plan path "
                "(multi-step ask, plan_tool={}, code_tool={})",
                spec.session_key or "default",
                spec.tools.get("run_plan") is not None,
                spec.tools.get("python_code") is not None,
            )

        for iteration in range(spec.max_iterations):
            if spec.strip_image_content_before_provider:
                # Injections and recovery/finalization messages are appended
                # between iterations, so scrub again immediately before model
                # preparation rather than only sanitizing the initial turn.
                self._sanitize_image_content_for_provider(messages, None)
            # Keep the persisted conversation untouched. Context governance
            # may repair or compact historical messages for the model, but
            # those synthetic edits must not shift the append boundary used
            # later when the caller saves only the new turn. A governance
            # failure must stop the run instead of sending an ungoverned copy.
            messages_for_model = self.context_governor.prepare_for_model(
                governance_config,
                messages,
                compacted_tool_call_ids,
            )
            # The steering hint rides on the REQUEST VIEW ONLY, appended last so
            # it never invalidates the cached static prefix. `messages` (the
            # transcript the caller persists) is never touched, so history,
            # replays, and every later turn are unaware it ever existed.
            request_messages = (
                [*messages_for_model, steer_message]
                if steer_message is not None
                else messages_for_model
            )
            context = AgentHookContext(
                iteration=iteration,
                messages=messages,
                session_key=spec.session_key,
            )
            await hook.before_iteration(context)
            provider_context = conversation_state.prepare_request(
                messages,
                context_window_tokens=spec.runtime.context_window_tokens,
                model_messages=messages_for_model,
                supplemental_messages=(
                    [steer_message] if steer_message is not None else None
                ),
            )
            response = await self._request_model(
                spec,
                request_messages,
                hook,
                context,
                conversation_state=conversation_state,
                provider_context=provider_context,
            )
            conversation_state.observe_response(response, messages)
            context.response = response
            context.tool_calls = list(response.tool_calls)

            original_content = response.content
            reasoning_text, cleaned_content = extract_reasoning(
                response.reasoning_content,
                response.thinking_blocks,
                response.content,
            )
            response.content = cleaned_content
            raw_usage = self._usage_or_estimate(spec, messages_for_model, response)
            context.usage = dict(raw_usage)
            self._accumulate_usage(usage, raw_usage)
            # Live API-call count: surface how many distinct model requests have
            # hit the provider SO FAR this turn (this one included), so progress
            # hooks can stream an honest "API calls: N" while the task runs —
            # not just at completion. Cost discipline made visible.
            if spec.llm_calls:
                context.usage["llm_calls"] = spec.llm_calls[0]
            if reasoning_text and not context.streamed_reasoning:
                await hook.emit_reasoning(reasoning_text)
                await hook.emit_reasoning_end()
                context.streamed_reasoning = True

            if response.should_execute_tools:
                context.tool_calls = list(response.tool_calls)
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=True)

                assistant_message = build_assistant_message(
                    response.content or "",
                    tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                assistant_message = conversation_state.project_response_message(
                    assistant_message,
                    response,
                )
                messages.append(assistant_message)
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "awaiting_tools",
                        "iteration": iteration,
                        "model": spec.runtime.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                    },
                )

                await hook.before_execute_tools(context)

                results, new_events, fatal_error = await self._execute_tools(
                    spec,
                    response.tool_calls,
                    external_lookup_counts,
                    workspace_violation_counts,
                    hook,
                    context,
                    repeat_tool_state=repeat_tool_state,
                )
                tool_events.extend(new_events)
                tools_used.extend(
                    tool_call.name
                    for tool_call, event in zip(response.tool_calls, new_events)
                    if event.get("status") == "ok"
                )
                # Capture the concrete steps taken this iteration so a clean
                # completion can be distilled into a replayable plan. Only
                # successful calls are recorded — a failed step is not part of a
                # trustworthy recipe. We also keep each step's output so replay
                # can detect content drift (the workspace changed) and refuse to
                # serve a stale answer.
                for tool_call, event, result in zip(
                    response.tool_calls, new_events, results
                ):
                    if event.get("status") == "ok":
                        recorded_steps.append(
                            {
                                "name": tool_call.name,
                                "arguments": tool_call.arguments
                                if isinstance(tool_call.arguments, dict)
                                else {},
                            }
                        )
                        recorded_outputs.append(str(result))

                # --- Zero-call middleware completion --------------------------
                # The model already fetched the data; when the middleware
                # rendered it into chat text there is nothing left to reason
                # about. Persist the tool messages + final answer and end the
                # turn WITHOUT the extra provider call that would only
                # re-render JSON the model never authored.
                if isinstance(fatal_error, _ZeroCallComplete):
                    formatted = fatal_error.content
                    for tool_call, result in zip(response.tool_calls, results):
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": tool_call.name,
                                "content": self.context_governor.normalize_tool_result(
                                    governance_config,
                                    tool_call.id,
                                    tool_call.name,
                                    result,
                                ),
                            }
                        )
                    self._append_final_message(messages, formatted)
                    usage["prompt_tokens"] += response.usage.get("prompt_tokens", 0)
                    usage["completion_tokens"] += response.usage.get("completion_tokens", 0)
                    usage["middleware_formatted"] = int(
                        usage.get("middleware_formatted", 0)
                    ) + 1
                    usage["llm_calls"] = spec.llm_calls[0] if spec.llm_calls else 0
                    stop_reason = "completed"
                    return AgentRunResult(
                        final_content=formatted,
                        messages=messages,
                        tools_used=tools_used,
                        usage=usage,
                        stop_reason=stop_reason,
                        error=None,
                        tool_events=tool_events,
                        had_injections=had_injections,
                        pending_stream_content=formatted,
                        provider_state=conversation_state.finish(messages),
                    )

                context.tool_results = list(results)
                context.tool_events = list(new_events)
                completed_tool_results: list[dict[str, Any]] = []
                for tool_call, result in zip(response.tool_calls, results):
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": self.context_governor.normalize_tool_result(
                            governance_config,
                            tool_call.id,
                            tool_call.name,
                            result,
                        ),
                    }
                    messages.append(tool_message)
                    completed_tool_results.append(tool_message)
                if fatal_error is not None:
                    error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                    final_content = error
                    stop_reason = "tool_error"
                    self._append_final_message(messages, final_content)
                    context.final_content = final_content
                    context.error = error
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    should_continue, injection_cycles = await self._try_drain_injections(
                        spec, messages, None, injection_cycles,
                        phase="after tool error",
                    )
                    if should_continue:
                        had_injections = True
                        length_recovery_parts.clear()
                        continue
                    break
                checkpoint_model_messages = (
                    self.context_governor.prepare_for_model(
                        governance_config,
                        messages,
                        compacted_tool_call_ids,
                    )
                    if response.provider_state is not None
                    else None
                )
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "tools_completed",
                        "iteration": iteration,
                        "model": spec.runtime.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": completed_tool_results,
                        "pending_tool_calls": [],
                        "provider_state": conversation_state.checkpoint(
                            messages,
                            model_messages=checkpoint_model_messages,
                        ),
                    },
                )
                empty_content_retries = 0
                length_recovery_parts.clear()

                # --- Zero-extra-call terminal completion --------------------
                # When a tool result declares the task complete (terminal=True,
                # e.g. novita_sandbox terminal result), the turn ENDS here: the
                # tool already produced the user-facing final answer, so the
                # usual "ask the model again for a closing message" round-trip
                # is skipped entirely. One provider call for the whole task.
                terminal_final: str | None = None
                for result in results:
                    if is_tool_terminal_result(result):
                        terminal_final = getattr(result, "final_message", None) or str(result)
                        break
                if terminal_final is not None:
                    final_content = terminal_final
                    self._append_final_message(messages, final_content)
                    context.final_content = final_content
                    context.stop_reason = "completed"
                    await hook.after_iteration(context)
                    break

                # Checkpoint 1: drain injections after tools, before next LLM call
                _drained, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after tool execution",
                )
                if _drained:
                    had_injections = True
                await hook.after_iteration(context)
                continue

            if response.has_tool_calls:
                logger.warning(
                    "Ignoring tool calls under finish_reason='{}' for {}",
                    response.finish_reason,
                    spec.session_key or "default",
                )

            clean = hook.finalize_content(context, response.content)
            if (
                response.finish_reason
                not in {"error", "length", "refusal", "content_filter"}
                and is_blank_text(clean)
            ):
                empty_content_retries += 1
                if empty_content_retries < _MAX_EMPTY_RETRIES:
                    logger.warning(
                        "Empty response on turn {} for {} ({}/{}); retrying",
                        iteration,
                        spec.session_key or "default",
                        empty_content_retries,
                        _MAX_EMPTY_RETRIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=False)
                    await hook.after_iteration(context)
                    continue
                logger.warning(
                    "Empty response on turn {} for {} after {} retries; attempting finalization",
                    iteration,
                    spec.session_key or "default",
                    empty_content_retries,
                )
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=False)
                retry_messages = self._finalization_retry_messages(messages_for_model)
                response = await self._request_finalization_retry(
                    spec,
                    messages_for_model,
                    transcript=messages,
                    conversation_state=conversation_state,
                )
                retry_usage = self._usage_or_estimate(spec, retry_messages, response)
                self._accumulate_usage(usage, retry_usage)
                raw_usage = self._merge_usage(raw_usage, retry_usage)
                context.response = response
                context.usage = dict(raw_usage)
                context.tool_calls = list(response.tool_calls)
                original_content = response.content
                clean = hook.finalize_content(context, response.content)

            if response.finish_reason == "length":
                segment = _restore_outer_whitespace(clean or "", original_content)
                # --- burn-loop guard (see _MAX_LENGTH_RECOVERIES) --------------
                # Continuing a truncated response is only worthwhile if the
                # segment actually advanced the answer. When the model re-emits
                # a blank or byte-identical truncated prefix, every further
                # replay is a paid provider call that produces no new work --
                # the exact failure documented in tools/run_plan.py. Refuse to
                # replay and finish with whatever we already have instead.
                prior = "".join(length_recovery_parts)
                stalled = not segment.strip() or (
                    bool(prior) and segment.strip() == prior.strip()
                )
                if (
                    not stalled
                    and len(length_recovery_parts) < _MAX_LENGTH_RECOVERIES
                ):
                    length_recovery_parts.append(segment)
                    logger.info(
                        "Output truncated on turn {} for {} ({}/{}); continuing",
                        iteration,
                        spec.session_key or "default",
                        len(length_recovery_parts),
                        _MAX_LENGTH_RECOVERIES,
                    )
                    if hook.wants_streaming():
                        context.stream_continues_current_message = True
                        await hook.on_stream_end(context, resuming=True)
                    messages.append(conversation_state.project_response_message(
                        build_assistant_message(
                            clean,
                            reasoning_content=response.reasoning_content,
                            thinking_blocks=response.thinking_blocks,
                        ),
                        response,
                    ))
                    messages.append(build_length_recovery_message(clean or ""))
                    await hook.after_iteration(context)
                    continue
                if stalled:
                    logger.warning(
                        "Refusing truncated-replay burn loop on turn {} for {} "
                        "({}-char segment, no new content); finishing with the {} "
                        "char(s) already produced",
                        iteration,
                        spec.session_key or "default",
                        len(segment.strip()),
                        len(prior),
                    )
                else:
                    logger.info(
                        "Length recovery exhausted on turn {} for {} after {} "
                        "segment(s); finishing",
                        iteration,
                        spec.session_key or "default",
                        len(length_recovery_parts),
                    )

            # Some streaming providers recover with a complete response but no
            # content deltas. When an earlier length segment is already visible,
            # emit this terminal segment into the same stream; otherwise the
            # regular full response would duplicate the visible prefix.
            if (
                length_recovery_parts
                and hook.wants_streaming()
                and not context.streamed_content
                and response.finish_reason != "error"
                and not is_blank_text(clean)
            ):
                await hook.on_stream(
                    context,
                    _restore_outer_whitespace(clean or "", original_content),
                )
                context.streamed_content = True

            assistant_message: dict[str, Any] | None = None
            if response.finish_reason != "error" and not is_blank_text(clean):
                assistant_message = build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                assistant_message = conversation_state.project_response_message(
                    assistant_message,
                    response,
                )

            # Check for mid-turn injections BEFORE signaling stream end.
            # If injections are found we keep the stream alive (resuming=True)
            # so streaming channels don't prematurely finalize the card.
            should_continue, injection_cycles = await self._try_drain_injections(
                spec, messages, assistant_message, injection_cycles,
                conversation_state=conversation_state,
                phase="after final response",
                iteration=iteration,
                allow_goal_continue=(
                    response.finish_reason not in {"refusal", "content_filter"}
                ),
            )
            if should_continue:
                had_injections = True

            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=should_continue)

            if should_continue:
                length_recovery_parts.clear()
                await hook.after_iteration(context)
                continue

            if response.finish_reason == "error":
                if LLMProvider.is_arrearage_response(response):
                    final_content = _ARREARAGE_ERROR_MESSAGE
                else:
                    final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
                stop_reason = "error"
                error = final_content
                self._append_model_error_placeholder(messages)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after LLM error",
                )
                if should_continue:
                    had_injections = True
                    length_recovery_parts.clear()
                    continue
                break
            if is_blank_text(clean):
                final_content = EMPTY_FINAL_RESPONSE_MESSAGE
                stop_reason = "empty_final_response"
                error = final_content
                self._append_final_message(messages, final_content)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after empty response",
                )
                if should_continue:
                    had_injections = True
                    length_recovery_parts.clear()
                    continue
                break

            messages.append(
                assistant_message
                or conversation_state.project_response_message(
                    build_assistant_message(
                        clean,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    ),
                    response,
                )
            )
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "final_response",
                    "iteration": iteration,
                    "model": spec.runtime.model,
                    "assistant_message": messages[-1],
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                    "provider_state": conversation_state.checkpoint(messages),
                },
            )
            if length_recovery_parts:
                final_content = (
                    "".join(length_recovery_parts)
                    + _restore_outer_whitespace(clean or "", original_content)
                ).strip()
            else:
                final_content = clean
            context.final_content = final_content
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            break
        else:
            stop_reason = "max_iterations"
            # Drain any remaining injections so they are appended to the
            # conversation history instead of being re-published as
            # independent inbound messages by _dispatch's finally block.
            # We include them before the no-tools finalization pass so the
            # final response can account for every known follow-up.
            drained_after_max_iterations, injection_cycles = await self._try_drain_injections(
                spec, messages, None, injection_cycles,
                phase="after max_iterations",
            )
            if drained_after_max_iterations:
                had_injections = True
            terminal_content = None
            if spec.finalize_on_max_iterations:
                terminal_content = await self._try_finalize_after_max_iterations(
                    spec,
                    hook,
                    messages,
                    usage,
                    conversation_state,
                )
            if terminal_content is None:
                terminal_content = self._max_iterations_fallback(spec)
            if length_recovery_parts:
                terminal_tail = f"\n\n{terminal_content.lstrip()}"
                final_content = (
                    "".join(length_recovery_parts).rstrip() + terminal_tail
                ).strip()
                pending_stream_content = terminal_tail
            else:
                final_content = terminal_content
            self._append_final_message(messages, terminal_content)

        # Store the completed answer so an identical future task replays with
        # zero provider calls. Only meaningful, successful completions are kept.
        if (
            replay_cache is not None
            and replay_task_text
            and stop_reason == "completed"
            and final_content
        ):
            replay_cache.put(replay_task_text, final_content)

        # Store the successful step sequence as a replayable PLAN so a future
        # task of the same SHAPE re-runs in the sandbox with zero LLM calls.
        # Requires: plan caching on, a meaningful normalized task, at least one
        # recorded step, and every step safe to replay (no side-effecting tools).
        if (
            plan_cache is not None
            and plan_norm is not None
            and stop_reason == "completed"
            and final_content
            and recorded_steps
            and plan_is_safe(recorded_steps)
        ):
            stored = plan_cache.put(
                plan_norm,
                recorded_steps,
                final_answer=final_content or "",
                expected_outputs=recorded_outputs[: len(recorded_steps)],
            )
            if stored is not None:
                logger.info(
                    "plan cache: stored {}-step plan for '{}' (0-call replay ready)",
                    len(recorded_steps),
                    plan_norm.template[:60],
                )

        usage["llm_calls"] = spec.llm_calls[0] if spec.llm_calls else 0
        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
            error=error,
            tool_events=tool_events,
            had_injections=had_injections,
            pending_stream_content=pending_stream_content,
            provider_state=conversation_state.finish(messages),
        )

    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": spec.runtime.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
        }
        generation = spec.runtime.generation
        kwargs["temperature"] = generation.temperature
        kwargs["max_tokens"] = generation.max_tokens
        kwargs["reasoning_effort"] = generation.reasoning_effort
        return kwargs

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
        *,
        malformed_retry: bool = False,
        conversation_state: ProviderConversationStateController,
        provider_context: ProviderCallContext | None = None,
    ) -> LLMResponse:
        timeout_s: float | None = spec.llm_timeout_s
        if timeout_s is None:
            # Default to a finite timeout to avoid per-session lock starvation when an LLM
            # request hangs indefinitely (e.g. gateway/network stall).
            # Set NANOBOT_LLM_TIMEOUT_S=0 to disable.
            #
            # [FIX 2026-09-17] Raised 300s -> 1800s. Long coding/deploy turns
            # routinely need more than 5 minutes of *model* wall-clock time, and
            # the 300s ceiling was killing them mid-run: the turn died with
            # "Error calling LLM: timed out after 300s", the streamed reasoning
            # stopped, and the user had to re-ask before the (already produced)
            # result surfaced. 30 min keeps the anti-starvation guarantee that
            # motivated the finite default while not truncating real work.
            raw = os.environ.get("NANOBOT_LLM_TIMEOUT_S", "1800").strip()
            try:
                timeout_s = float(raw)
            except (TypeError, ValueError):
                timeout_s = 1800.0
        if timeout_s <= 0:
            timeout_s = None

        kwargs = self._build_request_kwargs(
            spec,
            messages,
            tools=spec.tools.get_definitions(),
        )
        wants_streaming = hook.wants_streaming()
        progress_callback = spec.progress_callback
        wants_progress_streaming = (
            not wants_streaming
            and spec.stream_progress_deltas
            and progress_callback is not None
            and getattr(spec.runtime.provider, "supports_progress_deltas", False) is True
        )

        progress_state: dict[str, bool] | None = None
        active_hosted_tools: dict[str, dict[str, Any]] = {}
        request_started_at = 0.0
        first_output_at: float | None = None
        generation_started_at: float | None = None
        generation_elapsed_s = 0.0

        def _generation_delta(delta: str) -> None:
            nonlocal first_output_at, generation_started_at
            if not delta:
                return
            now = time.perf_counter()
            if first_output_at is None:
                first_output_at = now
            if generation_started_at is None:
                generation_started_at = now

        def _pause_generation() -> None:
            nonlocal generation_elapsed_s, generation_started_at
            if generation_started_at is None:
                return
            generation_elapsed_s += max(0.0, time.perf_counter() - generation_started_at)
            generation_started_at = None

        async def _provider_tool_event(event: dict[str, Any]) -> None:
            if event.get("kind") != "hosted_tool":
                return
            await hook.on_provider_tool_event(context, event)
            call_id = event.get("call_id")
            if not call_id:
                return
            call_id = str(call_id)
            if event.get("phase") == "start":
                active_hosted_tools[call_id] = dict(event)
            elif event.get("phase") in {"end", "error"}:
                active_hosted_tools.pop(call_id, None)

        if wants_streaming:
            thinking_buf = ""

            async def _stream(delta: str) -> None:
                _generation_delta(delta)
                if delta:
                    context.streamed_content = True
                await hook.on_stream(context, delta)

            async def _thinking(delta: str) -> None:
                nonlocal thinking_buf
                if not delta:
                    return
                _generation_delta(delta)
                prev_clean = strip_reasoning_tags(thinking_buf)
                thinking_buf += delta
                new_clean = strip_reasoning_tags(thinking_buf)
                incremental = new_clean[len(prev_clean):]
                if incremental:
                    context.streamed_reasoning = True
                    await hook.emit_reasoning(incremental)

            async def _stream_recover() -> None:
                _pause_generation()
                await hook.on_stream_end(context, resuming=True)

            coro = spec.runtime.provider.chat_stream_with_retry(
                **kwargs,
                provider_context=provider_context,
                on_content_delta=_stream,
                on_thinking_delta=_thinking,
                on_tool_call_delta=_provider_tool_event,
                on_stream_recover=_stream_recover,
            )
        elif wants_progress_streaming:
            stream_buf = ""
            think_extractor = IncrementalThinkExtractor()
            progress_state = {"reasoning_open": False}

            async def _stream_progress(delta: str) -> None:
                nonlocal stream_buf
                if not delta:
                    return
                _generation_delta(delta)
                prev_clean = strip_think(stream_buf)
                stream_buf += delta
                new_clean = strip_think(stream_buf)
                incremental = new_clean[len(prev_clean):]

                if await think_extractor.feed(stream_buf, hook.emit_reasoning):
                    context.streamed_reasoning = True
                    progress_state["reasoning_open"] = True

                if incremental:
                    if progress_state["reasoning_open"]:
                        await hook.emit_reasoning_end()
                        progress_state["reasoning_open"] = False
                    context.streamed_content = True
                    callback = progress_callback
                    if callback is not None:
                        await callback(incremental)

            coro = spec.runtime.provider.chat_stream_with_retry(
                **kwargs,
                provider_context=provider_context,
                on_content_delta=_stream_progress,
                on_tool_call_delta=_provider_tool_event,
            )
        else:
            coro = spec.runtime.provider.chat_with_retry(
                **kwargs,
                provider_context=provider_context,
            )

        # Streaming requests also have provider-level idle timeouts
        # (NANOBOT_STREAM_IDLE_TIMEOUT_S), but a stream that keeps producing
        # very slow deltas can still run forever. Use a more generous wall-clock
        # timeout for streaming while preserving NANOBOT_LLM_TIMEOUT_S=0 as an
        # opt-out for all LLM wall-clock timeouts.
        is_streaming_request = wants_streaming or wants_progress_streaming
        # In rate_limit_aware mode the provider owns retry/cooldown waits, so a
        # legitimate long Retry-After window must not be killed by the turn's
        # wall-clock budget. Bound only the actual model round-trips and let the
        # provider loop run as long as it needs (it still stops on real death
        # spirals). Other modes keep the fixed outer timeout unchanged.
        rate_limit_aware_mode = (
            spec.provider_retry_mode == "rate_limit_aware"
        )
        if rate_limit_aware_mode:
            # Generous ceiling: per-call timeouts are enforced inside the
            # provider; this only guards against an unbounded hang.
            outer_timeout_s = None
        else:
            outer_timeout_s = (
                max(300.0, timeout_s * 2)
                if is_streaming_request and timeout_s is not None
                else timeout_s
            )

        request_started_at = time.perf_counter()
        # Count this distinct model request (Manus-style "API called: N").
        # Internal provider retries are handled inside chat_with_retry and do NOT
        # bump this counter — only a real request to the configured LLM counts.
        spec.llm_calls[0] += 1
        # Narrate the wait before the first token. Streaming cannot cover this
        # window -- there is nothing on the wire to stream -- so without this the
        # turn's first visible sign of life is the model's first token, however
        # long that takes. Cancelled in ``finally`` so a returned, timed-out or
        # cancelled request can never leave a narrator ticking behind it.
        model_watch = asyncio.ensure_future(
            self._watch_model_wait(
                hook, context, request_started_at, lambda: first_output_at is not None
            )
        )
        try:
            response = (
                await coro if outer_timeout_s is None
                else await asyncio.wait_for(coro, timeout=outer_timeout_s)
            )
        except asyncio.TimeoutError:
            if outer_timeout_s is None:
                response = LLMResponse(
                    content="Error calling LLM: stream stalled",
                    finish_reason="error",
                    error_kind="timeout",
                )
            else:
                response = LLMResponse(
                    content=f"Error calling LLM: timed out after {outer_timeout_s:g}s",
                    finish_reason="error",
                    error_kind="timeout",
                )
        finally:
            # Stop the narrator however the request ended -- answered, timed out,
            # or the turn cancelled out from under it. CancelledError propagates
            # untouched, so cancelling the turn still cancels the request.
            model_watch.cancel()
            with suppress(asyncio.CancelledError):
                await model_watch
        _pause_generation()
        if first_output_at is not None:
            response.ttft_ms = max(0, round((first_output_at - request_started_at) * 1000))
        if generation_elapsed_s > 0:
            response.generation_ms = max(1, round(generation_elapsed_s * 1000))
        # chat_stream_with_retry may recover internally, so only fail unfinished
        # hosted calls after the provider returns its final error response.
        if response.finish_reason == "error":
            for event in list(active_hosted_tools.values()):
                await _provider_tool_event({
                    **event,
                    "phase": "error",
                    "result": None,
                    "error": response.content
                    or "Model request failed before the provider-hosted tool completed.",
                })
        if progress_state and progress_state.get("reasoning_open"):
            await hook.emit_reasoning_end()
        dropped, all_dropped, original_finish_reason = (
            self._drop_malformed_tool_calls(response)
        )
        if (
            all_dropped
            and original_finish_reason in ("tool_calls", "function_call")
            and not malformed_retry
        ):
            logger.warning(
                "Retrying LLM request after all {} malformed tool call(s) were dropped",
                dropped,
            )
            retry_messages = self._malformed_tool_call_retry_messages(
                messages, response.content,
            )
            return await self._request_model(
                spec, retry_messages, hook, context,
                malformed_retry=True,
                conversation_state=conversation_state,
                provider_context=conversation_state.independent_request_context(
                    context_window_tokens=spec.runtime.context_window_tokens,
                ),
            )
        if (
            all_dropped
            and original_finish_reason in ("tool_calls", "function_call")
            and malformed_retry
        ):
            logger.warning(
                "Malformed tool calls persisted after retry; falling back to no-tools request",
            )
            fallback_messages = self._malformed_tool_call_retry_messages(
                messages, response.content,
            )
            return await self._request_no_tools(
                spec,
                fallback_messages,
                provider_context=conversation_state.independent_request_context(
                    context_window_tokens=spec.runtime.context_window_tokens,
                ),
            )
        return response

    @staticmethod
    def _drop_malformed_tool_calls(
        response: LLMResponse,
    ) -> tuple[int, bool, str | None]:
        """Strip tool calls whose name is missing/non-string from the response.

        Returns (dropped_count, all_dropped, original_finish_reason).

        A degenerate call (name=None or "") cannot be executed, and if it were
        persisted into the assistant message it would be replayed on every
        subsequent turn, causing upstream validation errors
        (``tool_use.name: Input should be a valid string``) that permanently
        wedge the session. Dropping it here keeps it out of execution, the
        assistant message, and the saved history in one place.
        """
        calls = getattr(response, "tool_calls", None)
        if not calls:
            return (0, False, getattr(response, "finish_reason", None))
        valid = [tc for tc in calls if tc.has_valid_name()]
        if len(valid) == len(calls):
            return (0, False, getattr(response, "finish_reason", None))
        dropped = len(calls) - len(valid)
        original_finish_reason = getattr(response, "finish_reason", None)
        logger.warning(
            "Dropped {} malformed tool call(s) with missing/non-string name "
            "from LLM response (finish_reason={!r})",
            dropped,
            original_finish_reason,
        )
        response.tool_calls = valid
        # The opaque candidate still contains every raw function_call item.
        # Advancing it after dropping even one call would replay an unmatched
        # call without a corresponding tool output on the next request.
        response.provider_state = None
        if not valid:
            response.finish_reason = "stop"
        return (dropped, not valid, original_finish_reason)

    @staticmethod
    def _malformed_tool_call_retry_messages(
        messages: list[dict[str, Any]],
        assistant_text: str | None,
    ) -> list[dict[str, Any]]:
        retry_messages = list(messages)
        note = (
            "The previous model response attempted to call tools, but every tool call "
            "was malformed: the tool_use blocks had missing or non-string tool names. "
            "Do not answer with a promise to use tools. Either call the required tools again "
            "using valid tool names from the provided tool list and JSON object inputs, or give "
            "a final answer only if no tool is required."
        )
        if assistant_text:
            note += (
                f"\n\nPrevious assistant text before the malformed calls:\n"
                f"{assistant_text}"
            )
        retry_messages.append({"role": "user", "content": note})
        return retry_messages

    async def _request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        transcript: list[dict[str, Any]],
        conversation_state: ProviderConversationStateController,
    ) -> LLMResponse:
        retry_messages = self._finalization_retry_messages(messages)
        provider_context = conversation_state.prepare_request(
            transcript,
            context_window_tokens=spec.runtime.context_window_tokens,
            supplemental_messages=[retry_messages[-1]],
        )
        response = await self._request_no_tools(
            spec,
            retry_messages,
            provider_context=provider_context,
        )
        conversation_state.observe_response(
            response,
            transcript,
            adopt_candidate_state=False,
        )
        return response

    @staticmethod
    def _finalization_retry_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        retry_messages = list(messages)
        retry_messages.append(build_finalization_retry_message())
        return retry_messages

    async def _try_finalize_after_max_iterations(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
        conversation_state: ProviderConversationStateController,
    ) -> str | None:
        retry_messages = self._budget_exhausted_finalization_messages(messages)
        try:
            response = await self._request_no_tools(
                spec,
                retry_messages,
                provider_context=conversation_state.independent_request_context(
                    context_window_tokens=spec.runtime.context_window_tokens,
                ),
            )
        except Exception:
            logger.exception(
                "Budget-exhausted finalization failed for {}; using fallback",
                spec.session_key or "default",
            )
            return None

        raw_usage = self._usage_or_estimate(spec, retry_messages, response)
        self._accumulate_usage(usage, raw_usage)
        if response.finish_reason == "error" or response.has_tool_calls:
            logger.warning(
                "Budget-exhausted finalization returned finish_reason='{}' "
                "with {} tool call(s) for {}; using fallback",
                response.finish_reason,
                len(response.tool_calls),
                spec.session_key or "default",
            )
            return None

        context = AgentHookContext(
            iteration=spec.max_iterations,
            messages=messages,
            response=response,
            usage=dict(raw_usage),
            session_key=spec.session_key,
        )
        clean = hook.finalize_content(context, response.content)
        if is_blank_text(clean):
            return None
        return clean

    async def _request_no_tools(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        provider_context: ProviderCallContext | None = None,
    ) -> LLMResponse:
        kwargs = self._build_request_kwargs(
            spec,
            messages,
            tools=None,
        )
        spec.llm_calls[0] += 1
        return await spec.runtime.provider.chat_with_retry(
            **kwargs,
            provider_context=provider_context,
        )

    @staticmethod
    def _budget_exhausted_finalization_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        retry_messages = list(messages)
        retry_messages.append(build_budget_exhausted_finalization_message())
        return retry_messages

    @staticmethod
    def _max_iterations_fallback(spec: AgentRunSpec) -> str:
        if spec.max_iterations_message:
            return spec.max_iterations_message.format(
                max_iterations=spec.max_iterations,
            )
        return render_template(
            "agent/max_iterations_message.md",
            strip=True,
            max_iterations=spec.max_iterations,
        )

    def _usage_or_estimate(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        response: LLMResponse,
    ) -> dict[str, int]:
        usage = self._usage_dict(response.usage)
        total = self._usage_total(usage)
        if total > 0:
            usage["total_tokens"] = total
            usage.setdefault("provider_tokens", total)
        elif response.finish_reason == "error":
            return {}
        else:
            usage = self._estimate_response_usage(spec, messages, response)
        completion = usage.get("completion_tokens", 0)
        if response.generation_ms is not None and completion > 0:
            usage["generation_ms"] = response.generation_ms
            usage["measured_completion_tokens"] = completion
        if response.ttft_ms is not None:
            usage["ttft_ms"] = response.ttft_ms
            usage["timed_requests"] = 1
        return usage

    def _estimate_response_usage(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        response: LLMResponse,
    ) -> dict[str, int]:
        try:
            tools = spec.tools.get_definitions()
        except Exception:
            tools = None
        prompt_tokens, _ = estimate_prompt_tokens_chain(
            spec.runtime.provider,
            spec.runtime.model,
            messages,
            tools,
        )
        assistant_message = build_assistant_message(
            response.content or "",
            tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
            reasoning_content=response.reasoning_content,
            thinking_blocks=response.thinking_blocks,
        )
        completion_tokens = estimate_message_tokens(assistant_message)
        total_tokens = max(0, prompt_tokens) + max(0, completion_tokens)
        if total_tokens <= 0:
            return {}
        return {
            "prompt_tokens": max(0, prompt_tokens),
            "completion_tokens": max(0, completion_tokens),
            "total_tokens": total_tokens,
            "estimated_tokens": total_tokens,
        }

    @staticmethod
    def _usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        if not usage:
            return {}
        result: dict[str, int] = {}
        for key, value in usage.items():
            try:
                result[key] = int(value or 0)
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _usage_total(usage: dict[str, int]) -> int:
        return max(0, usage.get("total_tokens", 0) or (
            usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        ))

    @staticmethod
    def _accumulate_usage(target: dict[str, int], addition: dict[str, int]) -> None:
        for key, value in addition.items():
            target[key] = target.get(key, 0) + value

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        merged = dict(left)
        for key, value in right.items():
            merged[key] = merged.get(key, 0) + value
        return merged

    async def _execute_tool_with_heartbeat(
        self,
        hook: AgentHook | None,
        context: AgentHookContext | None,
        tool_call: ToolCallRequest,
        run: Awaitable[Any],
    ) -> Any:
        """Await one tool call, reporting elapsed time while it blocks.

        The call is driven as a task so the wait can be sliced: every
        ``TOOL_HEARTBEAT_SECONDS`` the hook is told how long it has been running,
        and the moment the tool returns the result is handed straight back.

        WHY A TASK AND NOT ``asyncio.wait_for``: the call is not being bounded,
        it is being OBSERVED. ``wait_for`` would impose a deadline this has no
        business imposing -- many tools are legitimately slow, and cutting one
        off would turn a slow answer into no answer. ``asyncio.wait`` with a
        timeout only ends the *wait*, never the work.

        CANCELLATION MUST SURVIVE: a cancelled turn has to cancel the tool it is
        blocked on, not leak it. ``ensure_future`` copies the current context, so
        the tool runs in the same context as before, and the task is cancelled
        both when the await is cancelled and in ``finally`` if the caller returned
        early for any other reason. ``CancelledError`` is never converted -- it
        propagates from ``task.result()`` exactly as it would from a direct await.

        A hook that raises is swallowed on purpose: this runs while the turn is
        blocked, and a progress label that throws must not take down the tool call
        it is only narrating.
        """
        if hook is None or context is None:
            return await run

        task = asyncio.ensure_future(run)
        started = time.perf_counter()
        try:
            while True:
                done, _ = await asyncio.wait(
                    {task}, timeout=_tool_heartbeat_seconds()
                )
                if done:
                    return task.result()
                try:
                    await hook.on_tool_heartbeat(
                        context, tool_call, time.perf_counter() - started
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - narration must never break the tool
                    logger.debug(
                        "tool heartbeat hook failed for {}", tool_call.name, exc_info=True
                    )
        finally:
            if not task.done():
                task.cancel()

    @staticmethod
    async def _watch_model_wait(
        hook: AgentHook,
        context: AgentHookContext,
        started: float,
        has_output: Callable[[], bool],
    ) -> None:
        """Report elapsed time while a model request sits silent.

        WHY A WATCHER TASK AND NOT A WRAPPER: the request path owns its own
        timeout (``asyncio.wait_for`` on the outer wall clock) and its own
        cancellation semantics, and it is the hottest code in the loop. Slicing
        the await the way ``_execute_tool_with_heartbeat`` does would mean
        restructuring that timeout to keep firing, so this observes from beside
        the request instead of inside its await.

        Stops as soon as ``has_output`` reports the model has started talking,
        which is what keeps it from competing with real streamed deltas: once
        output exists, the stream itself is the progress signal and a second
        narrator would only be noise. For a non-streaming request nothing ever
        sets output, and the watcher legitimately ticks for the whole call --
        which is the case that most needs it, since that request is otherwise
        completely silent end to end.

        The hook is never allowed to break the turn: this runs concurrently with
        the request, and a progress label that throws must not surface as a
        failed model call.
        """
        while True:
            await asyncio.sleep(_model_heartbeat_seconds())
            if has_output():
                return
            try:
                await hook.on_model_heartbeat(context, time.perf_counter() - started)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - narration must never break the request
                logger.debug("model heartbeat hook failed", exc_info=True)

    async def _execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        hook: AgentHook | None = None,
        context: AgentHookContext | None = None,
        repeat_tool_state: dict[str, Any] | None = None,
    ) -> tuple[list[Any], list[dict[str, str]], BaseException | None]:
        hook = hook or AgentHook()
        context = context or AgentHookContext(iteration=0, messages=[])
        batches = self._partition_tool_batches(spec, tool_calls)
        tool_results: list[tuple[Any, dict[str, str], BaseException | None]] = []
        for batch in batches:
            if spec.concurrent_tools and len(batch) > 1:
                # Bounded, not a bare gather: the model may now emit many
                # parallel calls in one turn, and running all of them at once
                # contends for CPU/sockets and can finish slower than a capped
                # pool. See _tool_concurrency_limit.
                batch_results = await _bounded_gather(
                    [
                        self._run_tool(
                            spec,
                            tool_call,
                            external_lookup_counts,
                            workspace_violation_counts,
                            hook,
                            context,
                            repeat_tool_state=repeat_tool_state,
                        )
                        for tool_call in batch
                    ],
                    _tool_concurrency_limit(),
                )
                tool_results.extend(batch_results)
            else:
                batch_results: list[tuple[Any, dict[str, str], BaseException | None]] = []
                for tool_call in batch:
                    result = await self._run_tool(
                        spec,
                        tool_call,
                        external_lookup_counts,
                        workspace_violation_counts,
                        hook,
                        context,
                        repeat_tool_state=repeat_tool_state,
                    )
                    tool_results.append(result)
                    batch_results.append(result)

        results: list[Any] = []
        events: list[dict[str, str]] = []
        fatal_error: BaseException | None = None
        for result, event, error in tool_results:
            results.append(result)
            events.append(event)
            if error is not None and fatal_error is None:
                fatal_error = error

        # --- Zero-call post-tool formatting ---------------------------------
        # When every call this iteration is a governed read-only UniAbuja
        # lookup and all succeeded with structured payloads, the answer is
        # already complete data — render it with pure code and end the turn
        # instead of paying the model to re-render JSON. Any miss (multi-call,
        # error verdict, unparseable output) falls through unchanged.
        if (
            fatal_error is None
            and getattr(spec, "tool_middleware", False)
            and tool_calls
        ):
            finalized = self._try_zero_call_finalize(tool_calls, tool_results)
            if finalized is not None:
                return results, events, finalized
        return results, events, fatal_error

    def _try_zero_call_finalize(
        self,
        tool_calls: list[ToolCallRequest],
        tool_results: list[tuple[Any, dict[str, str], BaseException | None]],
    ) -> BaseException | None:
        """Return a ``_ZeroCallComplete`` sentinel when the turn can end here.

        Conditions are strict on purpose: exactly one tool call, governed by
        the middleware, successful, and its output deterministically
        renderable. Everything else keeps today's behaviour (model sees the
        payload and continues).
        """
        if len(tool_calls) != 1 or len(tool_results) != 1:
            return None
        call = tool_calls[0]
        result, _event, error = tool_results[0]
        if not self.tool_middleware.handles(call) or error is not None:
            return None
        if is_tool_error_result(result):
            # Error verdicts stay in-loop: the model may recover (retry,
            # explain, ask for a regno) instead of parroting a failure.
            return None
        from nanobot.agent.tool_middleware import render_uniabuja_output

        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        try:
            formatted = render_uniabuja_output(call.name, arguments, str(result))
        except Exception:  # pragma: no cover - rendering must never break a turn
            logger.exception("tool middleware: renderer crashed, falling back to model")
            return None
        if not formatted:
            return None
        logger.info(
            "tool middleware: {} formatted with 0 further provider calls",
            call.name,
        )
        return _ZeroCallComplete(formatted)

    @staticmethod
    def _tool_fingerprint(tool_call: ToolCallRequest) -> str:
        try:
            args = json.dumps(
                tool_call.arguments,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            args = repr(tool_call.arguments)
        return f"{tool_call.name}:{args}"

    async def _run_tool(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        hook: AgentHook | None = None,
        context: AgentHookContext | None = None,
        repeat_tool_state: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        hook = hook or AgentHook()
        context = context or AgentHookContext(iteration=0, messages=[])
        hint = "\n\n[Analyze the error above and try a different approach.]"
        repeat_tool_state = repeat_tool_state if repeat_tool_state is not None else {"fingerprint": None, "count": 0}
        fingerprint = self._tool_fingerprint(tool_call)
        if fingerprint == repeat_tool_state.get("fingerprint"):
            repeat_tool_state["count"] = int(repeat_tool_state.get("count") or 0) + 1
        else:
            repeat_tool_state["fingerprint"] = fingerprint
            repeat_tool_state["count"] = 1
        lookup_error = repeated_external_lookup_error(
            tool_call.name,
            tool_call.arguments,
            external_lookup_counts,
        )
        if lookup_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "repeated external lookup blocked",
            }
            if spec.fail_on_tool_error:
                return lookup_error + hint, event, RuntimeError(lookup_error)
            return lookup_error + hint, event, None
        if int(repeat_tool_state["count"]) > 2:
            detail = (
                "The exact same tool call was repeated without an intervening change. "
                "This attempt was blocked; inspect the latest result and choose a different action."
            )
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "identical tool call blocked",
            }
            if spec.fail_on_tool_error:
                return detail + hint, event, RuntimeError(detail)
            return detail + hint, event, None
        prepare_call = cast(
            Callable[[str, Any], object] | None,
            getattr(spec.tools, "prepare_call", None),
        )
        tool, params, prep_error = None, tool_call.arguments, None
        if callable(prepare_call):
            prepared = prepare_call(tool_call.name, tool_call.arguments)
            if isinstance(prepared, tuple):
                prepared_tuple = cast(tuple[object, ...], prepared)
                if len(prepared_tuple) == 3:
                    tool, params, prep_error = cast(tuple[Any, Any, str | None], prepared_tuple)
        if prep_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": prep_error.split(": ", 1)[-1][:120],
            }
            handled = self._classify_violation(
                raw_text=prep_error,
                soft_payload=prep_error + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            return prep_error + hint, event, (
                RuntimeError(prep_error) if spec.fail_on_tool_error else None
            )
        await hook.before_execute_tool(context, tool_call, tool, params)
        try:
            if tool is not None:
                result = await self._execute_tool_with_heartbeat(
                    hook, context, tool_call, tool.execute(**params)
                )
            else:
                result = await self._execute_tool_with_heartbeat(
                    hook, context, tool_call, spec.tools.execute(tool_call.name, params)
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await hook.on_execute_tool_error(context, tool_call, tool, params, exc)
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": str(exc),
            }
            payload = f"Error: {type(exc).__name__}: {exc}"
            handled = self._classify_violation(
                raw_text=str(exc),
                # Preserve legacy exception payloads without the retry hint.
                soft_payload=payload,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if spec.fail_on_tool_error:
                return payload, event, exc
            return payload, event, None

        if is_tool_error_result(result):
            await hook.on_execute_tool_error(context, tool_call, tool, params, result)
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": result.replace("\n", " ").strip()[:120],
            }
            handled = self._classify_violation(
                raw_text=result,
                soft_payload=result + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if spec.fail_on_tool_error:
                return result + hint, event, RuntimeError(result)
            return result + hint, event, None

        await hook.after_execute_tool(context, tool_call, tool, params, result)

        detail = "" if result is None else str(result)
        detail = detail.replace("\n", " ").strip()
        if not detail:
            detail = "(empty)"
        elif len(detail) > 120:
            detail = detail[:120] + "..."
        return result, {"name": tool_call.name, "status": "ok", "detail": detail}, None

    # SSRF is a hard security block at the tool boundary, but the agent turn
    # should recover conversationally instead of aborting the runtime.
    _SSRF_MARKERS: tuple[str, ...] = (
        "internal/private url detected",
        "private/internal address",
        "private address",
    )
    _SSRF_BOUNDARY_NOTE: str = (
        "This is a non-bypassable security boundary. Stop trying to access "
        "private/internal URLs. Do not retry with curl, wget, encoded IPs, "
        "alternate DNS, redirects, proxies, or another tool. Ask the user for "
        "local files, logs, screenshots, or an explicit safe public URL instead. "
        "If the user explicitly trusts this private URL, ask them to whitelist "
        "the exact IP/CIDR via tools.ssrfWhitelist."
    )

    # Non-SSRF boundary markers returned to the LLM as recoverable tool errors.
    _WORKSPACE_VIOLATION_MARKERS: tuple[str, ...] = (
        "outside the configured workspace",
        "outside allowed directory",
        "working_dir is outside",
        "working_dir could not be resolved",
        "path outside working dir",
        "path traversal detected",
    )

    @classmethod
    def _is_ssrf_violation(cls, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(marker in lowered for marker in cls._SSRF_MARKERS)

    @classmethod
    def _is_workspace_violation(cls, text: str) -> bool:
        """True when *text* looks like any policy boundary rejection."""
        if not text:
            return False
        lowered = text.lower()
        if cls._is_ssrf_violation(lowered):
            return True
        return any(marker in lowered for marker in cls._WORKSPACE_VIOLATION_MARKERS)

    def _classify_violation(
        self,
        *,
        raw_text: str,
        soft_payload: str,
        event: dict[str, str],
        tool_call: ToolCallRequest,
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None] | None:
        """Classify safety-boundary failures, or return ``None`` to pass through."""
        if self._is_ssrf_violation(raw_text):
            logger.warning(
                "Tool {} blocked by SSRF guard; returning non-retryable tool error: {}",
                tool_call.name,
                raw_text.replace("\n", " ").strip()[:200],
            )
            event["detail"] = self._event_detail("ssrf_violation: ", raw_text)
            return self._ssrf_soft_payload(raw_text), event, None

        if self._is_workspace_violation(raw_text):
            escalation = repeated_workspace_violation_error(
                tool_call.name,
                tool_call.arguments,
                workspace_violation_counts,
            )
            event["detail"] = self._event_detail("workspace_violation: ", raw_text)
            if escalation is not None:
                logger.warning(
                    "Tool {} hit workspace boundary repeatedly; escalating hint",
                    tool_call.name,
                )
                event["detail"] = self._event_detail(
                    "workspace_violation_escalated: ",
                    raw_text,
                )
                return escalation, event, None
            return soft_payload, event, None

        return None

    @classmethod
    def _ssrf_soft_payload(cls, raw_text: str) -> str:
        text = raw_text.strip() or "Error: request blocked by SSRF guard"
        return f"{text}\n\n{cls._SSRF_BOUNDARY_NOTE}"

    @staticmethod
    def _event_detail(prefix: str, text: str, limit: int = 160) -> str:
        return (prefix + text.replace("\n", " ").strip())[:limit]

    async def _emit_checkpoint(
        self,
        spec: AgentRunSpec,
        payload: dict[str, Any],
    ) -> None:
        callback = spec.checkpoint_callback
        if callback is not None:
            await callback(payload)

    async def _try_plan_replay(
        self,
        spec: AgentRunSpec,
        plan_cache: Any,
        norm: Any,
        messages: list[dict[str, Any]],
        conversation_state: Any,
    ) -> AgentRunResult | None:
        """Replay a learned task PLAN with ZERO provider calls, or fall through.

        Returns an ``AgentRunResult`` only when every recorded step re-executes
        successfully and yields non-empty output; otherwise returns None so the
        normal LLM path runs (and can relearn/overwrite the plan). A stale or
        incompatible plan therefore never produces a wrong answer — at worst it
        costs one wasted sandbox attempt before the model takes over.
        """
        plan = plan_cache.get_fuzzy(norm)
        if plan is None or not variables_compatible(norm, plan):
            return None
        steps = substitute_variables(plan.steps, plan.variables, norm.variables)
        # Re-validate after substitution: swapping values must not smuggle in an
        # unsafe action (e.g. a variable that turns 'run' into 'upload').
        if not plan_is_safe(steps):
            return None

        results: list[Any] = []
        events: list[dict[str, str]] = []
        for index, step in enumerate(steps):
            tool = spec.tools.get(step["name"])
            if tool is None:
                logger.info("plan cache: tool {} missing, falling back to model", step["name"])
                return None
            try:
                result = await spec.tools.execute(step["name"], dict(step["arguments"]))
            except Exception:
                logger.exception("plan cache: replay step failed, falling back to model")
                return None
            if is_tool_error_result(result):
                logger.info("plan cache: replay step errored, falling back to model")
                return None
            text = str(result).strip()
            if not text:
                return None
            # --- Content-aware drift guard ---------------------------------
            # If we recorded what this step produced when the plan was learned,
            # compare it now. A different output means the workspace moved under
            # us (file edited, test added), so the cached prose answer could be
            # stale — discard the plan and let the model relearn rather than
            # confidently serve an outdated summary.
            expected = plan.expected_outputs[index] if index < len(plan.expected_outputs) else ""
            if expected and _normalize_for_drift(expected) != _normalize_for_drift(text):
                logger.info(
                    "plan cache: step {} output drifted, falling back to model", index
                )
                return None
            results.append(text)
            events.append({"name": step["name"], "status": "ok"})
            # Mirror history so the session transcript shows the work happened.
            call_id = f"plan-replay-{index}"
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": step["name"],
                                "arguments": json.dumps(step["arguments"]),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": step["name"],
                    "content": text,
                }
            )

        # Serve the MODEL'S SYNTHESIZED ANSWER captured at save time, not the
        # raw stdout of the last command. This is the whole point: a "check for
        # bugs" reply is the prose summary the model wrote, which previously got
        # thrown away so every repeat re-paid full price. Falls back to the last
        # step's output only if no answer was stored (older plans).
        final_content = plan.final_answer.strip() or results[-1]
        self._append_final_message(messages, final_content)
        logger.info(
            "plan cache: replayed {}-step plan for '{}' (0 provider calls)",
            len(steps),
            norm.template[:60],
        )
        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=[s["name"] for s in steps],
            usage={"prompt_tokens": 0, "completion_tokens": 0, "plan_replayed": 1, "llm_calls": 0},
            stop_reason="completed",
            error=None,
            tool_events=events,
            had_injections=False,
            pending_stream_content=final_content,
            provider_state=conversation_state.finish(messages),
        )

    async def _middleware_execute(
        self, spec: AgentRunSpec, tool_call: ToolCallRequest
    ) -> Any:
        """Execute *tool_call* through the middleware when the run opts in.

        Fail-open: any problem inside the layer falls back to a plain registry
        execution so a caching optimisation can never break an answer path.
        """
        if getattr(spec, "tool_middleware", False) and self.tool_middleware.handles(tool_call):
            try:
                return await self.tool_middleware.execute(spec.tools, tool_call)
            except Exception:
                logger.exception(
                    "Tool middleware failed for {}, executing directly", tool_call.name
                )
        return await spec.tools.execute(tool_call.name, tool_call.arguments)

    @staticmethod
    def _retarget_task_recipe(
        spec: AgentRunSpec, recipe_call: ToolCallRequest
    ) -> ToolCallRequest | None:
        """Pick the command runner that actually exists on this run.

        The task router emits recipes targeting ``novita_sandbox`` because that
        is where the user's project files live (Novita / VPS / Upstash sandbox).
        When a deployment has no sandbox tool but does expose the local shell,
        we transparently re-target the same read-only command to ``exec`` so the
        zero-call answer still works. If neither tool is registered the recipe
        returns None and the turn falls through to the normal LLM path — never
        a crash, never a wrong answer.
        """
        if spec.tools.get("novita_sandbox") is not None:
            return recipe_call
        if spec.tools.get("exec") is None:
            return None
        # Re-target the {action:"run", command} call onto the local shell.
        args_raw = recipe_call.arguments
        if isinstance(args_raw, dict) and str(args_raw.get("action", "")).lower() == "run":
            command = args_raw.get("command")
            if isinstance(command, str) and command.strip():
                args: dict[str, Any] = {"command": command}
                timeout = args_raw.get("timeout")
                if isinstance(timeout, int):
                    args["timeout"] = min(max(timeout, 1), 600)
                return ToolCallRequest(id=recipe_call.id, name="exec", arguments=args)
        return None


    @staticmethod
    def _append_final_message(messages: list[dict[str, Any]], content: str | None) -> None:
        if not content:
            return
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            if messages[-1].get("content") == content:
                return
            messages[-1] = build_assistant_message(content)
            return
        messages.append(build_assistant_message(content))

    @staticmethod
    def _append_model_error_placeholder(messages: list[dict[str, Any]]) -> None:
        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            return
        messages.append(build_assistant_message(_PERSISTED_MODEL_ERROR_PLACEHOLDER))

    def _partition_tool_batches(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> list[list[ToolCallRequest]]:
        if not spec.concurrent_tools:
            return [[tool_call] for tool_call in tool_calls]

        batches: list[list[ToolCallRequest]] = []
        current: list[ToolCallRequest] = []
        for tool_call in tool_calls:
            get_tool = cast(Callable[[str], Any] | None, getattr(spec.tools, "get", None))
            tool = get_tool(tool_call.name) if callable(get_tool) else None
            can_batch = bool(tool and tool.concurrency_safe)
            if can_batch:
                current.append(tool_call)
                continue
            if current:
                batches.append(current)
                current = []
            batches.append([tool_call])
        if current:
            batches.append(current)
        return batches
