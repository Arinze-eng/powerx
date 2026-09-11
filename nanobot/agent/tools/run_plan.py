"""``run_plan`` — one model call, whole multi-step task executed deterministically.

This is the AgentScript mechanism surfaced as a normal PowerX tool. Instead of
the Re-Act pattern (model → one tool → model → next tool … = N billed calls),
the model emits ONE ``run_plan`` describing the entire job — including loops over
dynamic data — and the plan-program executor runs every step against the real
registered tools with ZERO further provider round-trips.

Why a *tool* rather than a new loop: it composes with everything already in the
runner (credits, hooks, telemetry, the registry's own safety/validation). The
executor needs to call sibling tools by name, so we inject the live registry via
a ContextVar-style setter set once per run (see ``bind_registry``); when no
registry is bound the tool errors cleanly and the model just falls back to normal
step-by-step behaviour — never a crash.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from nanobot.agent.plan_program import PlanProgramError, execute_plan, parse_plan
from nanobot.agent.tools.base import Tool, ToolResult


def _plan_json_len(plan: dict[str, Any]) -> int:
    """Serialized size (chars) of a parsed plan, used by the compactness guard."""
    try:
        return len(json.dumps(plan, separators=(",", ":"), default=str))
    except Exception:  # pragma: no cover - serialization should not fail here
        return 0


class RunPlanTool(Tool):
    """Expose deterministic plan execution to the model as a single tool."""

    # Registered explicitly by AgentLoop._register_default_tools (which also
    # binds the live registry via bind_registry). Opt out of plugin-loader
    # auto-discovery, which would register an UNBOUND instance that can never
    # reach sibling tools.
    _plugin_discoverable = False

    def __init__(self) -> None:
        # Late-bound to the active registry so plan steps can invoke real tools.
        self._registry: Any | None = None

    # -- binding ------------------------------------------------------------
    def bind_registry(self, registry: Any) -> None:
        """Attach the live ToolRegistry for this session's executions."""
        self._registry = registry

    @property
    def name(self) -> str:
        return "run_plan"

    @property
    def description(self) -> str:
        return (
            "Execute an ENTIRE multi-step task in ONE call with zero extra model "
            "round-trips. Provide a JSON plan whose 'steps' list chains tool calls; "
            "each step stores its output under 'id' so later steps reference it as "
            "$id, and 'foreach' iterates a prior result (e.g. \"$files.split('\\n')\") "
            "running its 'do' sub-steps per item WITHOUT calling the model again. "
            "Use this INSTEAD of many separate sandbox/read/exec calls whenever a "
            "task needs more than ~2 steps or must loop over files/items. It is far "
            "cheaper: hundreds of commands still cost one model call."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "object",
                    "description": (
                        "The plan program. Shape: {\"steps\": [ ... ], \"output\": "
                        "\"final summary text (may use $vars)\"}. A leaf step is "
                        "{\"tool\": \"name\", \"args\": {...}, \"id\": \"varname\"}; "
                        "reference earlier outputs as $varname inside args. A loop "
                        "step is {\"foreach\": \"$varname.split('\\n')\", \"as\": "
                        "\"item\", \"do\": [ leaf steps using $item ]}."
                    ),
                }
            },
            "required": ["plan"],
        }

    async def execute(self, **kwargs: Any) -> Any:
        registry = self._registry
        if registry is None:  # pragma: no cover - defensive
            return ToolResult.error(
                "run_plan is not wired to a tool registry on this run; do the work "
                "with ordinary tool calls instead."
            )

        raw_plan = kwargs.get("plan")
        try:
            plan = parse_plan(raw_plan)
        except PlanProgramError as exc:
            return ToolResult.error(
                f"Invalid plan: {exc}. Resubmit a well-formed {{\"steps\": [...]}} "
                "or fall back to individual tool calls."
            )

        # --- compactness guard ------------------------------------------------
        # Empirically (heavy real-model testing) a single plan that is too large —
        # e.g. embeds a big inline shell/python string, or explodes into far too many
        # nested steps — can cause the model to blow its output-token budget and return
        # finish_reason="length", which the runner then merely replays turn after turn
        # (0 tool commands executed, all calls burned). Catching it here turns that
        # silent waste into a clear, actionable error the model can recover from in
        # one cheap retry, instead of N identical oversized replays.
        # The values are deliberately generous (well above any sane generated plan)
        # so only genuinely pathological payloads are rejected.
        text_len = _plan_json_len(plan)
        step_count = sum(1 for s in plan.get("steps", []))
        if text_len > 20000 or step_count > 60:
            logger.warning(
                "run_plan rejected for size: {:,} chars / {} top-level steps",
                text_len,
                step_count,
            )
            return ToolResult.error(
                "Plan is too large to execute safely in one call "
                f"({text_len:,} chars, {step_count} top-level steps). Split it into "
                "smaller run_plan calls (batch ~5-10 steps at a time) or fall back to "
                "normal step-by-step tool calls."
            )

        async def _execute(name: str, args: dict[str, Any]) -> Any:
            # Guard: only allow tools that actually exist, so a hallucinated name
            # fails as a normal tool error (caught by the executor) rather than
            # crashing the whole plan.
            if not registry.has(name):
                return ToolResult.error(f"Error: unknown tool '{name}' in plan")
            return await registry.execute(name, args)

        try:
            result = await execute_plan(plan, _execute)
        except PlanProgramError as exc:
            logger.info("run_plan aborted, falling back to model: {}", exc)
            return ToolResult.error(
                f"Plan could not complete: {exc}. Continue with normal step-by-step "
                "tool calls."
            )
        except Exception as exc:  # pragma: no cover - unexpected runtime fault
            logger.exception("run_plan crashed unexpectedly")
            return ToolResult.error(
                f"Plan execution failed: {exc}. Fall back to individual tool calls."
            )

        return self._render(result)

    @staticmethod
    def _render(result: Any) -> Any:
        """Turn a PlanResult into a compact, model-friendly combined report."""
        lines: list[str] = []
        failures = sum(1 for o in result.outputs if not o.ok)
        lines.append(
            f"[run_plan: {result.executed_steps} step(s) executed, "
            f"{failures} failure(s), 0 additional model calls]"
        )
        for idx, outcome in enumerate(result.outputs):
            status = "ok" if outcome.ok else "ERROR"
            body = str(outcome.result)
            if len(body) > 4000:
                body = body[:4000] + "\n…(truncated)"
            lines.append(f"--- step {idx} [{outcome.name}] {status} ---\n{body}")
        if result.final:
            lines.append(f"=== SUMMARY ===\n{result.final}")
        return ToolResult("\n".join(lines))
