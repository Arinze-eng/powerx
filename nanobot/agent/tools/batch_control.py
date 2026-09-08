"""Control-flow primitives executed *inside* a sandbox_batch call.

Why this exists
---------------
``sandbox_batch`` already collapses N tool calls into one LLM round-trip, and
``runner`` coalesces lone sandbox calls into batches automatically. But a batch
was still straight-line code: to retry a failing command, loop over a set of
targets, or wait for a long build, the model had to come back and ask again —
one round-trip per decision. That is what makes an agent expensive, and it is
the difference between ~20 operations per call and hundreds.

These helpers move the *decision* into the sandbox, where it is free:

``retry_until``
    Re-run a body until a condition holds or attempts are exhausted. Turns
    "run tests, read failure, patch, run again" from four model turns into one.

``foreach``
    Apply a body to every item in a list or a file of targets. A 200-file codemod
    becomes a single operation instead of 200 observations.

``await``
    Poll a condition with sleeps in between. A 40-minute deploy costs no tokens,
    because nothing is sent to the provider while the sandbox waits.

Conditions are evaluated against structured facts (exit code, stdout/stderr text,
elapsed time) rather than by asking the model to interpret output.

Safety
------
Every construct is bounded before execution: iteration caps, total-attempt caps,
and wall-clock deadlines. Nested depth is limited so a hand-written plan cannot
recursively explode into unbounded compute without the model noticing.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

#: Maximum nesting depth for control-flow ops (loop inside loop inside loop…).
MAX_NEST_DEPTH = 3

#: Hard ceiling on total inner executions triggered by one composite op.
_MAX_TOTAL_ATTEMPTS = 2_000

#: Default cap on foreach items when the caller does not state one.
_DEFAULT_MAX_ITEMS = 500

#: Default cap on retry attempts.
_DEFAULT_MAX_ATTEMPTS = 12

#: Longest a single ``await`` may block, in seconds (40 min).
_MAX_AWAIT_SECONDS = 2_400

_MIN_POLL_INTERVAL = 0.5


class PlanError(ValueError):
    """Raised for malformed control-flow specs, before anything executes."""


@dataclass(slots=True)
class StepOutcome:
    """Structured result of running one body step."""

    text: str
    exit_code: int | None = None
    failed: bool = False

    @property
    def lowered(self) -> str:
        return self.text.lower()


@dataclass(slots=True)
class LoopReport:
    kind: str
    iterations: int = 0
    attempts: int = 0
    satisfied: bool = False
    stopped_reason: str = ""
    samples: list[str] = field(default_factory=list)
    children: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "iterations": self.iterations,
            "attempts": self.attempts,
            "satisfied": self.satisfied,
        }
        if self.stopped_reason:
            out["stopped"] = self.stopped_reason
        if self.samples:
            out["samples"] = self.samples
        if self.children:
            out["children"] = self.children
        return out


# ---------------------------------------------------------------------------
# condition evaluation
# ---------------------------------------------------------------------------

_EXIT_RE = re.compile(r"\[exit=(\d+)\]")
_OP_RE = re.compile(r"^\s*(\w+)\s*(<=|>=|==|!=|<|>)\s*(.+?)\s*$")


def parse_exit_code(text: str) -> int | None:
    matches = _EXIT_RE.findall(text or "")
    if not matches:
        return None
    try:
        return int(matches[-1])
    except (TypeError, ValueError):
        return None


def _coerce(value: str) -> Any:
    v = value.strip()
    if v[:1] in {'"', "'"} and v[:1] == v[-1:]:
        return v[1:-1]
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return None
    return v


def evaluate_condition(expr: str, outcome: StepOutcome, elapsed: float = 0.0) -> bool:
    """Evaluate a small safe expression language against a step outcome.

    Supported forms::

        exit == 0 / exit != 0 / exit < 2
        contains "all tests passed"
        not_contains "FAILED"
        ok                      # exit in (None, 0) and no error markers
        done                    # alias of ok
        elapsed >= 30           # seconds since the loop started
        true / false
    """
    if expr is None:
        return False
    expr = str(expr).strip()
    if not expr:
        return False
    low = expr.lower()

    if low in ("true", "ok", "done", "success"):
        return outcome.exit_code in (None, 0) and not outcome.failed
    if low in ("false", "never"):
        return False
    if low.startswith("elapsed"):
        m = _OP_RE.match(low)
        if not m:
            raise PlanError(f"bad elapsed condition: {expr!r}")
        return _compare(elapsed, m.group(2), float(m.group(3)), expr)
    if low.startswith("exit"):
        m = _OP_RE.match(low)
        if not m:
            raise PlanError(f"bad exit condition: {expr!r}")
        code = outcome.exit_code if outcome.exit_code is not None else -1
        return _compare(code, m.group(2), _coerce(m.group(3)), expr)
    if low.startswith("not_contains"):
        needle = _string_arg(low[len("not_contains"):], expr)
        return needle not in outcome.lowered
    if low.startswith("contains"):
        needle = _string_arg(low[len("contains"):], expr)
        return needle in outcome.lowered

    m = _OP_RE.match(low)
    if m and m.group(1) in ("stdout", "text"):
        raise PlanError(
            f"condition {expr!r} compares raw text; use contains/not_contains instead"
        )
    raise PlanError(f"unsupported condition: {expr!r}")


def _string_arg(rest: str, original: str) -> str:
    rest = rest.strip()
    if rest[:1] in {'"', "'"} and rest[:1] == rest[-1:] and len(rest) >= 2:
        return rest[1:-1].lower()
    if not rest:
        raise PlanError(f"condition missing argument: {original!r}")
    return rest.lower()


def _compare(left: Any, op: str, right: Any, original: str) -> bool:
    if right is None:
        raise PlanError(f"condition has a non-numeric comparison value: {original!r}")
    try:
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == "<":
            return left < right
        if op == ">":
            return left > right
        if op == "<=":
            return left <= right
        if op == ">=":
            return left >= right
    except TypeError as exc:  # mixed str/int comparison
        raise PlanError(f"incomparable condition {original!r}: {exc}") from exc
    raise PlanError(f"unknown operator in condition: {original!r}")


# ---------------------------------------------------------------------------
# spec validation (fail fast, before spending any compute)
# ---------------------------------------------------------------------------

_BODY_KEYS = ("body", "steps", "ops")


def _extract_body(spec: dict[str, Any], label: str) -> list[dict[str, Any]]:
    for key in _BODY_KEYS:
        if key in spec:
            body = spec[key]
            if isinstance(body, dict):
                body = [body]
            if not isinstance(body, list) or not body:
                raise PlanError(f"{label}: '{key}' must be a non-empty list of operations")
            cleaned: list[dict[str, Any]] = []
            for i, step in enumerate(body):
                if not isinstance(step, dict):
                    raise PlanError(f"{label}: body step {i} must be an object")
                if not str(step.get("action", "")).strip():
                    raise PlanError(f"{label}: body step {i} is missing 'action'")
                cleaned.append(step)
            return cleaned
    raise PlanError(f"{label}: missing 'body' (list of operations to repeat)")


def validate_control_flow(spec: dict[str, Any], *, depth: int = 0) -> None:
    """Recursively check a composite op's nested structure.

    Validation happens up front so a typo surfaces immediately instead of after
    the sandbox has burned half its budget on partial work.
    """
    if depth > MAX_NEST_DEPTH:
        raise PlanError(f"control flow nested deeper than {MAX_NEST_DEPTH} levels")
    action = str(spec.get("action", ""))
    if action == "retry_until":
        if not str(spec.get("until", "")).strip():
            raise PlanError("retry_until requires an 'until' condition")
        evaluate_condition(spec["until"], StepOutcome(text=""))  # syntax check
        body = _extract_body(spec, "retry_until")
        for step in body:
            validate_control_flow(step, depth=depth + 1)
    elif action == "foreach":
        has_items = isinstance(spec.get("items"), list)
        has_file = bool(str(spec.get("items_file", "")).strip())
        if not has_items and not has_file:
            raise PlanError("foreach requires 'items' (list) or 'items_file' (path)")
        body = _extract_body(spec, "foreach")
        for step in body:
            validate_control_flow(step, depth=depth + 1)
    elif action == "await":
        cond = str(spec.get("until", "")).strip()
        cmd = str(spec.get("command", "")).strip()
        if not cond and not cmd:
            raise PlanError("await requires either 'until' or a probe 'command'")
        if cond:
            evaluate_condition(cond, StepOutcome(text=""))


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

StepRunner = Callable[[dict[str, Any]], Awaitable[StepOutcome]]


async def run_retry_until(
    spec: dict[str, Any],
    run_step: StepRunner,
    *,
    deadline: float | None = None,
) -> LoopReport:
    """Repeat ``body`` until ``until`` holds or attempts run out."""
    report = LoopReport(kind="retry_until")
    until = str(spec.get("until", ""))
    max_attempts = _int_bound(spec.get("max_attempts"), _DEFAULT_MAX_ATTEMPTS, 1, 200)
    delay = _float_bound(spec.get("delay_sec"), 0.0, 0.0, 60.0)
    body = _extract_body(spec, "retry_until")

    last: StepOutcome = StepOutcome(text="")
    for attempt in range(1, max_attempts + 1):
        if deadline is not None and time.monotonic() >= deadline:
            report.stopped_reason = "deadline"
            break
        attempt_failed = False
        outcome = StepOutcome(text="")
        for step in body:
            outcome = await run_step(step)
            if outcome.failed:
                attempt_failed = True
                break
        last = outcome
        report.attempts = attempt
        if len(report.samples) < 3:
            report.samples.append(_brief(outcome.text))
        if not attempt_failed:
            try:
                satisfied = evaluate_condition(until, outcome, _elapsed(deadline))
            except PlanError as exc:
                report.stopped_reason = str(exc)
                return report
            if satisfied:
                report.satisfied = True
                return report
        if attempt < max_attempts and delay > 0:
            await asyncio.sleep(delay)
    if not report.stopped_reason:
        report.stopped_reason = "attempts_exhausted"
    if last.text and len(report.samples) < 4:
        report.samples.append("last:" + _brief(last.text))
    return report


async def run_foreach(
    spec: dict[str, Any],
    run_step: StepRunner,
    *,
    read_items: Callable[[str], Awaitable[list[str]]],
    deadline: float | None = None,
) -> LoopReport:
    """Apply ``body`` to each item, substituting ``{{item}}``/``{{index}}``."""
    report = LoopReport(kind="foreach")
    raw_items: list[str]
    if isinstance(spec.get("items"), list):
        raw_items = [str(x) for x in spec["items"]]
    else:
        loaded = await read_items(str(spec.get("items_file", "")).strip())
        if not isinstance(loaded, list):
            raise PlanError("items_file reader must return a list of strings")
        raw_items = [str(x) for x in loaded]
    max_items = _int_bound(spec.get("max_items"), _DEFAULT_MAX_ITEMS, 1, _MAX_TOTAL_ATTEMPTS)
    if len(raw_items) > max_items:
        raise PlanError(
            f"foreach has {len(raw_items)} items but max_items={max_items}; "
            "raise max_items or narrow the input"
        )
    body = _extract_body(spec, "foreach")
    fail_fast = _truthy(spec.get("stop_on_error", True))
    keep = _int_bound(spec.get("keep_reports"), 5, 0, 50)
    failed_items = 0

    for idx, item in enumerate(raw_items):
        if deadline is not None and time.monotonic() >= deadline:
            report.stopped_reason = "deadline"
            break
        report.iterations += 1
        item_failed = False
        for step in body:
            resolved = _substitute(step, item=item, index=idx)
            outcome = await run_step(resolved)
            report.attempts += 1
            if outcome.failed:
                item_failed = True
                break
        if item_failed:
            failed_items += 1
            if len(report.samples) < keep:
                report.samples.append(f"{item}: FAILED")
            if fail_fast:
                report.satisfied = False
                report.stopped_reason = f"item {idx} ({_brief(item)}) failed"
                return report
        elif len(report.samples) < keep and _truthy(spec.get("report_each")):
            report.samples.append(f"{item}: ok")
    # Only satisfied when every item actually succeeded; continuing past a
    # failure must not be reported as success.
    report.satisfied = failed_items == 0 and not report.stopped_reason
    if failed_items:
        report.stopped_reason = f"{failed_items} item(s) failed"
    return report


async def run_await(
    spec: dict[str, Any],
    run_step: StepRunner,
    *,
    deadline_for_op: Callable[[float], float],
) -> LoopReport:
    """Poll until a condition holds. Sleeping here costs zero LLM tokens."""
    report = LoopReport(kind="await")
    timeout = _float_bound(spec.get("timeout_sec"), 300.0, 1.0, _MAX_AWAIT_SECONDS)
    interval = _float_bound(spec.get("interval_sec"), 5.0, _MIN_POLL_INTERVAL, 300.0)
    until = str(spec.get("until", "")).strip()
    command = str(spec.get("command", "")).strip()
    stop_after = deadline_for_op(timeout)

    while True:
        remaining = stop_after - time.monotonic()
        if remaining <= 0:
            report.stopped_reason = "timeout"
            break
        if command:
            outcome = await run_step({"action": "run", "command": command})
        else:
            # Condition-only polling: measure progress via a cheap clock probe so
            # `elapsed >= N` style conditions still work without a real command.
            outcome = StepOutcome(text="[exit=0]")
        report.attempts += 1
        if not until:
            # No condition given: a successful probe is enough.
            report.satisfied = not outcome.failed
            if report.satisfied:
                break
        else:
            try:
                if evaluate_condition(until, outcome, elapsed=_elapsed(stop_after)):
                    report.satisfied = True
                    break
            except PlanError as exc:
                report.stopped_reason = str(exc)
                break
        if time.monotonic() >= stop_after:
            report.stopped_reason = "timeout"
            break
        await asyncio.sleep(min(interval, max(_MIN_POLL_INTERVAL, stop_after - time.monotonic())))
    if len(report.samples) < 2:
        report.samples.append(f"polled {report.attempts}x")
    return report


# ---------------------------------------------------------------------------
# formatting for the model
# ---------------------------------------------------------------------------

def render_report(report: LoopReport) -> str:
    """One line that answers the only question the model has: did it work?"""
    verdict = "SATISFIED" if report.satisfied else "NOT-SATISFIED"
    bits = [f"[{verdict}] {report.kind}"]
    if report.kind == "retry_until":
        bits.append(f"attempts={report.attempts}")
    elif report.kind == "foreach":
        bits.append(f"items={report.iterations}")
    else:
        bits.append(f"polls={report.attempts}")
    if report.stopped_reason:
        bits.append(f"stopped={report.stopped_reason}")
    if report.samples:
        bits.append("|".join(report.samples)[:240])
    return " ".join(bits)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _elapsed(deadline: float | None) -> float:
    if deadline is None:
        return 0.0
    return max(0.0, deadline - time.monotonic())


def _brief(text: str, limit: int = 90) -> str:
    stripped = " ".join((text or "").split())
    return stripped[:limit]


def _int_bound(value: Any, default: int, lo: int, hi: int) -> int:
    if value is None or value == "":
        return default
    try:
        n = int(float(str(value)))
    except (TypeError, ValueError) as exc:
        raise PlanError(f"expected an integer, got {value!r}") from exc
    return max(lo, min(n, hi))


def _float_bound(value: Any, default: float, lo: float, hi: float) -> float:
    if value is None or value == "":
        return default
    try:
        n = float(str(value))
    except (TypeError, ValueError) as exc:
        raise PlanError(f"expected a number, got {value!r}") from exc
    return max(lo, min(n, hi))


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", ""}
    return bool(value)


def _substitute(obj: Any, *, item: str, index: int) -> Any:
    """Recursively replace ``{{item}}`` / ``{{index}}`` placeholders in a body step."""
    if isinstance(obj, str):
        return obj.replace("{{item}}", item).replace("{{index}}", str(index))
    if isinstance(obj, list):
        return [_substitute(v, item=item, index=index) for v in obj]
    if isinstance(obj, dict):
        return {k: _substitute(v, item=item, index=index) for k, v in obj.items()}
    return obj
