"""API-call optimizer facade: one entry point wiring every saving layer.

The individual layers each live in their own module:

* ``agent_planner``   — agentic planning + persistent plan memory (1 compose
  call per novel task, then free replays)
* ``memoize``         — in-memory LRU + disk memoization of tool calls
* ``tool_router``     — declarative pre-LLM intent -> tool-call router
* ``reflection``      — deterministic validation, retry-once, failure memory
* ``plan_program``    — the plan interpreter (now with a parallel construct)
* ``deterministic_router`` / ``task_router`` / ``plan_cache`` /
  ``task_cache`` — the pre-existing layers this stack builds on

This facade composes them into the recommended turn pipeline, in the order
that spends the FEWEST provider calls:

1. route short routine asks to a tool with zero LLM calls;
2. otherwise plan (recall-or-compose ONCE) and execute deterministically;
3. wrap the executor so every tool call is memoized on the persistent disk;
4. reflect over failures: retry once, remember, score the plan.

Everything is fail-open: every helper returns ``None``/falls through when
its layer is disabled, unsure, or errors, so callers keep their normal LLM
path as the fallback.
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.agent_planner import AgentPlanner, make_planner
from nanobot.agent.memoize import MemoCache, memoizing_executor
from nanobot.agent.plan_program import ToolExecutor, execute_plan, fan_out, parse_plan
from nanobot.agent.reflection import FailureMemory, reflect_plan
from nanobot.agent.tool_router import ToolRouter, default_router

#: Flag: set POWERX_OPTIMIZER=0 to bypass the whole stack.
_ENABLED = os.environ.get("POWERX_OPTIMIZER", "1").strip().lower() not in {
    "0", "false", "no", "off",
}

#: Volatile tools must never be memoized: their result is per-call state.
_DEFAULT_VOLATILE = frozenset({"date", "random", "uuid", "now"})


def optimizer_enabled() -> bool:
    return _ENABLED


def is_volatile(tool_name: str) -> bool:
    return tool_name.split(".")[0].lower() in _DEFAULT_VOLATILE


class ApiOptimizer:
    """Compose the saving layers for one agent turn."""

    def __init__(
        self,
        *,
        router: ToolRouter | None = None,
        planner: AgentPlanner | None = None,
        memo: MemoCache | None = None,
        failures: FailureMemory | None = None,
    ) -> None:
        self.router = router or default_router()
        self.planner = planner or make_planner()
        self.memo = memo or MemoCache(namespace="optimizer_tools")
        self.failures = failures or FailureMemory()

    # -- layer 1: zero-call routing ------------------------------------------

    def route(self, text: str) -> tuple[str, dict[str, Any]] | None:
        """Pre-LLM tool route for a short routine ask, or None."""
        return self.router.route(text)

    # -- layers 2-4: plan, execute memoized, reflect ---------------------------

    def memoized_executor(self, execute: ToolExecutor) -> ToolExecutor:
        """Wrap a tool executor with persistent memoization."""
        return memoizing_executor(execute, self.memo, cacheable=lambda name: not is_volatile(name))

    async def optimize(
        self,
        task_text: str,
        compose_plan: Callable[[str], Awaitable[dict[str, Any] | None]],
        execute: ToolExecutor,
    ) -> dict[str, Any] | None:
        """Run the full pipeline for a task. Returns a summary dict or None.

        ``compose_plan`` is the ONLY provider-calling hook and it fires at
        most once (only when plan memory has no usable plan). Everything
        after it is deterministic: plan interpretation, memoized tool calls,
        and the reflection retry loop.
        """
        if not _ENABLED:
            return None
        memoized = self.memoized_executor(execute)
        outcome = await self.planner.plan_and_execute(
            task_text,
            compose_plan,
            memoized,
            run_plan=execute_plan,
        )
        if outcome is None:
            return None
        result, plan = outcome
        reflection = await reflect_plan(
            getattr(result, "outputs", []), memoized, failures=self.failures
        )
        if not reflection["ok"]:
            # The plan produced unrecoverable failures: demote it so the next
            # ask recomposes instead of replaying the same losing steps.
            self.planner.remember(
                task_text, plan, success=False, error="; ".join(reflection["reasons"])
            )
        return {
            "final": getattr(result, "final", None),
            "executed_steps": getattr(result, "executed_steps", 0),
            "reflection": reflection,
            "plan": plan,
        }

    async def run_parallel(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        execute: ToolExecutor,
    ) -> list[Any]:
        """Fan out N independent tool calls concurrently (LLM-compiler mode)."""
        memoized = self.memoized_executor(execute)
        outcomes = await fan_out(calls, memoized)
        for outcome in outcomes:
            if not outcome.ok:
                self.failures.record(outcome.name, outcome.arguments, str(outcome.result)[:200])
        return outcomes
