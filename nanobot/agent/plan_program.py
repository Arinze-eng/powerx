"""Plan-program executor: the AgentScript idea made real for this runtime.

AgentScript's core insight (github.com/AgentScript-AI/agentscript) is that a
Re-Act agent pays one LLM round-trip per decision, so a task with N steps costs
N+1 calls. Its fix: make the model emit ONE *program* describing all the work,
then run that program in a deterministic interpreter where loops and branches
execute WITHOUT re-consulting the model. 50 files scanned in a loop = 1 call.

This module is that mechanism, adapted to PowerX's Python + sandbox architecture.
Rather than a bespoke JS parser (overkill and risky here), the model expresses a
plan as structured JSON via the ``run_plan`` tool. The executor walks that plan
and drives your EXISTING tools (novita_sandbox / exec / read_file / grep / ...),
flowing results into named variables and iterating over collections — all with
ZERO additional provider calls. The only time the model is involved again is if
the whole plan fails and we fall back to normal Re-Act.

A plan looks like::

    {
      "steps": [
        {"id": "files", "tool": "exec", "args": {"command": "unzip -o x.zip -d out && find out -name '*.py'"}},
        {"id": "scan", "foreach": "$files.split('\\n')", "as": "f", "do": [
            {"tool": "read_file", "args": {"path": "$f"}, "id": "body"},
            {"tool": "exec", "args": {"command": "python3 -m py_compile $f"}}
        ]},
        {"parallel": [
            {"id": "lint", "tool": "exec", "args": {"command": "ruff check ."}},
            {"id": "tests", "tool": "exec", "args": {"command": "pytest -q"}}
        ]}
      ],
      "output": "Scan complete"
    }

The ``parallel`` construct runs its branches CONCURRENTLY (LLM-compiler
style): N independent tool calls land in the wall-clock time of the
slowest branch, with zero extra provider calls.

Design guarantees:
* Deterministic & safe: only registered tools run; a hard cap bounds total
  executed steps so a runaway loop can't hang or blow cost. No eval/exec of
  arbitrary code — `$var` interpolation is pure string substitution.
* Fail-open: any structural problem raises PlanProgramError, which the caller
  treats as "don't use the plan path" — behaviour falls back to the model.
* Observability: returns per-step results so the runner can record them as a
  replayable plan and show honest command/step counts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# A single execute(name, args) coroutine provided by the caller (the tool
# registry). Returns whatever the tool returns (usually a ToolResult/str).
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]


class PlanProgramError(Exception):
    """Raised when a plan is malformed or unsafe. Caller falls back to model."""


#: Hard ceiling on total executed tool calls across ALL loop iterations. This is
#: the safety valve: even a pathological foreach over 10k items cannot exceed it,
#: so a plan can never silently turn into thousands of billed commands.
MAX_EXECUTED_STEPS = int(__import__("os").environ.get("POWERX_PLAN_MAX_STEPS", "2000"))

#: Max nesting depth for foreach/do blocks — guards against recursive structures.
MAX_DEPTH = 6

_VAR_RE = re.compile(r"\$\{?([a-zA-Z_][a-zA-Z0-9_]*)\}?")
_SPLIT_RE = re.compile(r"^\$\{?([a-zA-Z_][a-zA-Z0-9_]*)\}?\.split\((['\"])(.*?)\2\)$")


@dataclass(slots=True)
class StepOutcome:
    """One executed leaf step, recorded for telemetry + plan caching."""

    name: str
    arguments: dict[str, Any]
    result: Any
    ok: bool
    #: The plan's step ``id`` (when given) so parallel branches can publish
    #: their results back into the parent scope after ``gather``.
    id: str | None = None


@dataclass(slots=True)
class PlanResult:
    """Outcome of executing a whole plan-program."""

    outputs: list[StepOutcome] = field(default_factory=list)
    final: str | None = None
    executed_steps: int = 0

    @property
    def failed(self) -> bool:
        return any(not o.ok for o in self.outputs)


#: Canonical failure markers produced by the sandbox runners. The novita/VPS/upstash
#: backends render a command result with an explicit trailing ``[exit_code=N]`` marker
#: (see novita_sandbox._output) — a nonzero N is an unambiguous failure regardless of
#: whether stdout happened to start with "Error:". The plan executor MUST treat that
#: as an error so a failed command inside a run_plan is reported honestly (and the
#: model isn't fed a false "0 failures" summary that makes it waste more calls).
_EXIT_CODE_RE = re.compile(r"(?:^|\D)\[exit_code\s*=\s*(-?\d+)\]")
#: Common human-readable failure signals worth treating as errors even without the
#: structured exit-code marker (e.g. host exec backends that don't render one), or
#: when the error line is ANSI-coloured so the plain "Error:" prefix match misses it.
_ERR_HINTS = (
    "command not found",
    "no such file or directory",
    "not found",
    "permission denied",
    "is not recognized",
    "failed",
    # Python tracebacks render as "Traceback (most recent call last):" then
    # "FileNotFoundError: ..." / "SyntaxError: ..." etc. Catching these keeps a
    # failed py_compile/unit run from being reported as success.
    "traceback (most recent call last)",
    "file not found error",
    "filenotfounderror",
)


def _is_error_result(result: Any) -> bool:
    text = str(result)
    stripped = text.lstrip()
    if stripped.startswith(("Error:", "error:")) or "[Analyze the error" in text:
        return True
    # Some backends prepend ANSI colour codes to an "Error:"/"error:" line; strip
    # simple ANSI escapes so those still match the prefix rule above.
    ansi_stripped = _strip_ansi(stripped)
    if ansi_stripped.startswith(("Error:", "error:")):
        return True
    # Canonical sandbox signal: a [exit_code=N] marker with a nonzero N.
    exit_codes = _EXIT_CODE_RE.findall(text)
    if exit_codes:
        return any(code != "0" for code in exit_codes)
    # Host-exec fallback: heuristic scan for shell/OS failure phrasing. A bare
    # historic mention in multi-line output is gated to the first few lines so a
    # file that merely *contains* "failed" far down isn't misclassified. A Python
    # Traceback is only meaningful when it actually contains a raised exception
    # line, so the whole text is checked for that one signal.
    head_lower = [line.lower() for line in stripped.splitlines()[:5] if line.strip()]
    if any(hint in line for hint in _ERR_HINTS for line in head_lower):
        return True
    if "traceback (most recent call last)" in text.lower():
        return any(sym in text.lower() for sym in ("error:", "exception:", "error\n", "filenotfounderror", "syntaxerror"))
    return False


#: ANSI SGR escape sequences (color/bold reset etc.) that some backends wrap around
#: error lines, e.g. `\x1b[31mError:\x1b[0m ...`. Stripping them lets the plain
#: "Error:" prefix rule still match.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _interpolate(value: Any, scope: dict[str, Any]) -> Any:
    """Replace ``$var`` / ``${var}`` tokens inside strings (and recurse through
    dicts/lists) with values from the current scope. Pure text substitution —
    nothing is evaluated, so there is no code-injection surface.

    If a string is EXACTLY one ``$var`` reference, substitute with the raw value
    (preserving type: lists stay lists, ints stay ints). Otherwise splice the
    stringified value into the surrounding text.
    """
    if isinstance(value, dict):
        return {k: _interpolate(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, scope) for v in value]
    if not isinstance(value, str):
        return value

    # Whole-string single reference → keep native type.
    exact = _VAR_RE.fullmatch(value.strip())
    if exact:
        name = exact.group(1)
        if name not in scope:
            raise PlanProgramError(f"undefined variable ${name}")
        return scope[name]

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in scope:
            raise PlanProgramError(f"undefined variable ${name}")
        val = scope[name]
        return val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)

    return _VAR_RE.sub(repl, value)


def _resolve_foreach_collection(expr: Any, scope: dict[str, Any]) -> list[Any]:
    """Resolve a ``foreach`` expression to an iterable list.

    Supports: a plain ``$var`` already holding a list, or ``$var.split('sep')``
    to turn a prior tool's stdout (e.g. a newline-delimited file list) into a
    list to iterate. That split form is what makes 'scan every file found in
    step 1' expressible without the model ever looping in its own head.
    """
    if isinstance(expr, list):
        return _interpolate(expr, scope)
    if not isinstance(expr, str):
        raise PlanProgramError("foreach must be a string reference or a list")

    m = _SPLIT_RE.match(expr.strip())
    if m:
        name, _, sep = m.group(1), m.group(2), m.group(3)
        if name not in scope:
            raise PlanProgramError(f"undefined variable ${name}")
        raw = str(scope[name])
        sep_real = sep.encode().decode("unicode_escape") if "\\" in sep else sep
        parts = raw.split(sep_real) if sep_real else raw.splitlines()
        return [p for p in (x.strip("\r") for x in parts) if p != ""]

    exact = _VAR_RE.fullmatch(expr.strip())
    if exact:
        name = exact.group(1)
        if name not in scope:
            raise PlanProgramError(f"undefined variable ${name}")
        val = scope[name]
        if isinstance(val, list):
            return val
        # Scalar: iterate over its lines as a convenience fallback.
        return [ln for ln in str(val).splitlines() if ln.strip()]

    raise PlanProgramError(f"cannot resolve foreach expression: {expr!r}")


async def _run_block(
    steps: list[Any],
    scope: dict[str, Any],
    execute: ToolExecutor,
    outcomes: list[StepOutcome],
    budget: list[int],
    depth: int,
) -> None:
    if depth > MAX_DEPTH:
        raise PlanProgramError("plan nested too deeply")
    if not isinstance(steps, list):
        raise PlanProgramError("block body must be a list of steps")

    for raw in steps:
        if not isinstance(raw, dict):
            raise PlanProgramError("each step must be an object")

        # --- Loop construct -------------------------------------------------
        if "foreach" in raw:
            item_var = str(raw.get("as") or "item")
            body = raw.get("do")
            if not isinstance(body, list):
                raise PlanProgramError("foreach requires a 'do' list")
            collection = _resolve_foreach_collection(raw["foreach"], scope)
            for index, element in enumerate(collection):
                # Each iteration gets its own child scope inheriting the parent,
                # exposing both $item and $index (mirrors AgentScript .map()).
                iter_scope = dict(scope)
                iter_scope[item_var] = element
                iter_scope.setdefault("index", index)
                await _run_block(body, iter_scope, execute, outcomes, budget, depth + 1)
                # Publish last loop-body var names back up? No — keep isolated
                # to avoid accidental cross-iteration leakage.
            continue

        # --- Parallel construct (LLM-compiler fan-out) -----------------------
        # {"parallel": [step, step, ...]} runs independent steps CONCURRENTLY
        # via asyncio.gather — N tool calls in the wall-clock time of the
        # slowest one, still zero provider calls. Each branch is a normal
        # leaf step; step ids are published into the parent scope after the
        # gather so downstream steps can reference $branch results.
        if "parallel" in raw:
            branches = raw["parallel"]
            if not isinstance(branches, list) or not branches:
                raise PlanProgramError("parallel requires a non-empty list of steps")
            if budget[0] + len(branches) > MAX_EXECUTED_STEPS:
                raise PlanProgramError(
                    f"plan exceeded the {MAX_EXECUTED_STEPS}-step safety cap; aborting"
                )

            async def _run_branch(branch: Any) -> list[StepOutcome]:
                branch_outcomes: list[StepOutcome] = []
                # Each branch runs in a child scope so concurrent branches
                # cannot stomp each other's variables mid-flight.
                await _run_block([branch], dict(scope), execute, branch_outcomes, budget, depth + 1)
                return branch_outcomes

            gathered = await asyncio.gather(
                *(_run_branch(branch) for branch in branches),
                return_exceptions=True,
            )
            for item in gathered:
                if isinstance(item, BaseException):
                    raise PlanProgramError(f"parallel branch failed: {item}") from item
                for outcome in item:
                    outcomes.append(outcome)
                    if outcome.id:
                        scope[outcome.id] = str(outcome.result)
            continue

        # --- Leaf tool step -------------------------------------------------
        tool_name = raw.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            raise PlanProgramError("step needs a 'tool' name (or a 'foreach')")

        if budget[0] >= MAX_EXECUTED_STEPS:
            raise PlanProgramError(
                f"plan exceeded the {MAX_EXECUTED_STEPS}-step safety cap; aborting"
            )

        args = _interpolate(raw.get("args") or {}, scope)
        if not isinstance(args, dict):
            raise PlanProgramError("step 'args' must be an object")

        result = await execute(tool_name, args)
        budget[0] += 1
        ok = not _is_error_result(result)
        step_id = raw.get("id")
        outcomes.append(
            StepOutcome(
                name=tool_name,
                arguments=args,
                result=result,
                ok=ok,
                id=step_id if isinstance(step_id, str) and step_id else None,
            )
        )

        if isinstance(step_id, str) and step_id:
            # Expose the step's textual output for later $refs / foreach.split.
            scope[step_id] = str(result)


async def fan_out(
    calls: list[tuple[str, dict[str, Any]]],
    execute: ToolExecutor,
) -> list[StepOutcome]:
    """LLM-compiler fan-out/fan-in: run N independent tool calls at once.

    One round-trip's wall clock, zero sequential waiting, zero provider
    calls — the caller passes ``(tool_name, args)`` pairs and gets every
    outcome back in call order.
    """
    if not calls:
        return []

    async def _one(tool_name: str, args: dict[str, Any]) -> StepOutcome:
        result = await execute(tool_name, args)
        return StepOutcome(name=tool_name, arguments=args, result=result, ok=not _is_error_result(result))

    gathered = await asyncio.gather(
        *(_one(name, args) for name, args in calls),
        return_exceptions=True,
    )
    results: list[StepOutcome] = []
    for index, item in enumerate(gathered):
        if isinstance(item, BaseException):
            name, args = calls[index]
            results.append(
                StepOutcome(name=name, arguments=args, result=f"Error: {item}", ok=False)
            )
        else:
            results.append(item)
    return results


def parse_plan(arguments: Any) -> dict[str, Any]:
    """Coerce a tool-call argument blob into a validated plan dict.

    Accepts either an already-parsed dict or a JSON string (models sometimes
    stringify). Raises PlanProgramError on anything unusable so the caller can
    fall back to normal Re-Act instead of crashing the turn.
    """
    plan = arguments
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except (ValueError, TypeError) as exc:
            raise PlanProgramError(f"plan is not valid JSON: {exc}") from exc
    if not isinstance(plan, dict):
        raise PlanProgramError("plan must be an object")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PlanProgramError("plan needs a non-empty 'steps' list")
    return plan


def plan_outline(plan: dict[str, Any], max_steps: int = 30) -> list[dict[str, str]]:
    """Display outline (JSON-safe) of a plan's top-level steps for the UI."""
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list):
        return []
    outline: list[dict[str, str]] = []
    for index, raw in enumerate(steps[:max_steps]):
        outline.append({"id": f"s{index}", "text": _step_text(raw)})
    return outline


def _short_text(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _step_text(raw: Any) -> str:
    if not isinstance(raw, dict):
        return "step"
    if "foreach" in raw:
        return "Loop over " + _short_text(raw.get("foreach"))
    if "parallel" in raw:
        branches = raw.get("parallel")
        count = len(branches) if isinstance(branches, list) else 0
        return f"Run {count} steps in parallel"
    tool = raw.get("tool")
    args = raw.get("args")
    detail = ""
    if isinstance(args, dict) and args:
        first = next(iter(args.values()))
        detail = _short_text(first)
    if detail:
        return f"{tool}: {detail}"
    return str(tool or "step")


def _plan_progress_payload(
    outline: list[dict[str, str]],
    statuses: list[str],
    *,
    phase: str,
    current: int | None = None,
    executed: int = 0,
) -> dict[str, Any]:
    steps = [
        {"id": step["id"], "text": step["text"], "status": status}
        for step, status in zip(outline, statuses, strict=False)
    ]
    payload: dict[str, Any] = {"phase": phase, "steps": steps, "executed": executed}
    if current is not None:
        payload["current"] = current
    return payload


async def execute_plan(
    plan: dict[str, Any],
    execute: ToolExecutor,
    *,
    on_step: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> PlanResult:
    """Run a parsed plan-program deterministically. Never calls the LLM.

    ``execute`` is an async callable ``(tool_name, args_dict) -> result`` —
    typically bound to the live ToolRegistry so real sandbox/file tools run.
    ``on_step`` (optional) receives a live JSON-safe snapshot on start, on step
    transition, and on plan completion so the UI can render Manus-style step progress.
    """
    outcomes: list[StepOutcome] = []
    scope: dict[str, Any] = {}
    budget = [0]
    steps = plan["steps"]
    outline = plan_outline(plan)
    statuses = ["pending"] * len(outline)

    async def _emit(phase: str, current: int | None = None, status: str | None = None) -> None:
        if on_step is None:
            return
        if status is not None and current is not None and current < len(statuses):
            statuses[current] = status
        with contextlib.suppress(Exception):
            await on_step(
                _plan_progress_payload(
                    outline, statuses, phase=phase, current=current, executed=budget[0]
                )
            )

    await _emit("start")
    for index, raw in enumerate(steps):
        await _emit("step", current=index, status="running")
        before = len(outcomes)
        try:
            await _run_block([raw], scope, execute, outcomes, budget, depth=0)
        except PlanProgramError:
            await _emit("failed", current=index, status="failed")
            raise
        produced = outcomes[before:]
        is_ok = all(o.ok for o in produced)
        await _emit("step", current=index, status="done" if is_ok else "failed")

    final = plan.get("output")
    if isinstance(final, str):
        # Allow the summary line to reference collected variables too.
        try:
            final = _interpolate(final, scope)
        except PlanProgramError:
            pass  # cosmetic only; keep the literal if a ref is missing
    await _emit("done")
    return PlanResult(outputs=outcomes, final=final, executed_steps=budget[0])
