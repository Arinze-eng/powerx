"""Reflection: validate outcomes cheaply, retry once, remember failures.

The executor layers above (plan programs, routers, memoized tool calls) are
deterministic — so when a step misbehaves, paying the model to "think about
it" is usually waste. This module implements the self-correction loop
WITHOUT provider calls:

* **Validate** each outcome with the same deterministic error signals the
  plan executor already trusts (exit-code markers, error prefixes).
* **Retry once** a failed step, re-running the exact registered tool —
  transient sandbox hiccups are recovered for zero additional reasoning.
* **Remember** failures persistently (disk under the Northflank data dir):
  a (tool, args-fingerprint) that failed repeatedly is marked known-bad and
  skipped early next time instead of being retried forever, and the plan
  that produced it gets demoted in plan memory.

Fail-open: validation can only decide "fine" vs "retry once vs give up" —
it never invents a successful result. If anything here errors, the caller's
normal behavior is unchanged.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.memoize import canonical_fingerprint
from nanobot.config.paths import get_persistent_data_dir

#: Flag: set POWERX_REFLECTION=0 to disable the layer entirely.
_ENABLED = os.environ.get("POWERX_REFLECTION", "1").strip().lower() not in {
    "0", "false", "no", "off",
}

#: A step fingerprint that failed at least this many recorded times is
#: known-bad: retried once at most, then given up early forever after.
_KNOWN_BAD_THRESHOLD = 3

#: Failure memory is capped so disk use stays bounded.
_MAX_FAILURES = 500

PlanExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]


def reflection_enabled() -> bool:
    return _ENABLED


@dataclass(slots=True)
class ReflectionVerdict:
    """Outcome of reflecting on one step execution."""

    ok: bool
    reason: str | None = None
    attempts: int = 1
    result: Any = None
    retried: bool = False


class FailureMemory:
    """Persistent (tool, fingerprint) -> failure-count store."""

    def __init__(self, root: Path | None = None) -> None:
        self._path = (root or get_persistent_data_dir("reflection")) / "failures.json"
        self._data: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._path.is_file():
                payload = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    self._data = payload
        except (OSError, ValueError, TypeError):
            self._data = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data), encoding="utf-8")
            self._trim()
        except (OSError, TypeError, ValueError) as exc:
            logger.debug("failure memory save failed: {}", exc)

    def _trim(self) -> None:
        if len(self._data) <= _MAX_FAILURES:
            return
        ranked = sorted(
            self._data.items(),
            key=lambda kv: float(kv[1].get("last_ts") or 0),
            reverse=True,
        )
        self._data = dict(ranked[:_MAX_FAILURES])

    def record(self, tool_name: str, args: dict[str, Any], reason: str) -> int:
        """Count one more failure for this exact call. Returns the new count."""
        self._load()
        fingerprint = canonical_fingerprint(tool_name, args)
        entry = self._data.setdefault(fingerprint, {"tool": tool_name, "count": 0})
        entry["count"] = int(entry.get("count") or 0) + 1
        entry["last_ts"] = time.time()
        entry["last_reason"] = (reason or "")[:400]
        entry["last_args"] = args
        self._save()
        return int(entry["count"])

    def known_bad(self, tool_name: str, args: dict[str, Any]) -> bool:
        """True when this exact call has failed at least the threshold."""
        self._load()
        fingerprint = canonical_fingerprint(tool_name, args)
        entry = self._data.get(fingerprint)
        return bool(entry) and int(entry.get("count") or 0) >= _KNOWN_BAD_THRESHOLD

    def clear(self, tool_name: str, args: dict[str, Any]) -> None:
        """Forget failures for a call that finally succeeded."""
        self._load()
        fingerprint = canonical_fingerprint(tool_name, args)
        if fingerprint in self._data:
            del self._data[fingerprint]
            self._save()


def validate_result(result: Any) -> tuple[bool, str | None]:
    """Deterministic outcome validation. Returns (ok, failure_reason)."""
    # plan_program already ships the exact signals the executors trust; import
    # lazily to avoid any import-cycle risk.
    from nanobot.agent.plan_program import _is_error_result

    if result is None:
        return False, "empty result"
    if isinstance(result, Exception):
        return False, f"raised {type(result).__name__}"
    try:
        if _is_error_result(result):
            text = str(result)[:200].replace("\n", " ")
            return False, text
    except Exception:  # noqa: BLE001 - validator must never break a run
        return True, None
    return True, None


async def reflect_step(
    tool_name: str,
    args: dict[str, Any],
    execute: PlanExecutor,
    *,
    failures: FailureMemory | None = None,
    max_attempts: int = 2,
) -> ReflectionVerdict:
    """Run one tool step and, on deterministic failure, retry it ONCE.

    A call already known-bad gets a single attempt (no retry burn), and its
    failure is re-recorded. A call that succeeds on retry clears its bad
    record. Zero provider calls anywhere in this loop.
    """
    memory = failures or FailureMemory()
    skip_retry = memory.known_bad(tool_name, args)

    result = await execute(tool_name, args)
    ok, reason = validate_result(result)
    attempts = 1

    if ok:
        memory.clear(tool_name, args)
        return ReflectionVerdict(ok=True, result=result, attempts=attempts)

    memory.record(tool_name, args, reason or "unknown")
    if skip_retry or attempts >= max_attempts:
        return ReflectionVerdict(ok=False, reason=reason, result=result, attempts=attempts)

    logger.info("reflection: retrying {} once after failure: {}", tool_name, reason)
    result = await execute(tool_name, args)
    attempts += 1
    ok, retry_reason = validate_result(result)
    if ok:
        memory.clear(tool_name, args)
        return ReflectionVerdict(ok=True, result=result, attempts=attempts, retried=True)
    memory.record(tool_name, args, retry_reason or "unknown")
    return ReflectionVerdict(
        ok=False, reason=retry_reason or reason, result=result, attempts=attempts, retried=True
    )


async def reflect_plan(
    outcomes: list[Any],
    execute: PlanExecutor,
    *,
    failures: FailureMemory | None = None,
) -> dict[str, Any]:
    """Reflect over a whole executed plan's outcomes.

    Re-runs each failed step once through ``reflect_step`` and returns a
    summary the planner uses to score the plan:
    ``{"ok": bool, "retried": int, "recovered": int, "still_failed": int,
       "reasons": [...]}``
    """
    memory = failures or FailureMemory()
    retried = recovered = still_failed = 0
    reasons: list[str] = []

    for outcome in outcomes:
        if getattr(outcome, "ok", True):
            continue
        tool_name = getattr(outcome, "name", "")
        args = getattr(outcome, "arguments", {}) or {}
        verdict = await reflect_step(
            tool_name, args, execute, failures=memory, max_attempts=2
        )
        retried += 1
        if verdict.ok:
            recovered += 1
            outcome.ok = True
            outcome.result = verdict.result
        else:
            still_failed += 1
            if verdict.reason:
                reasons.append(verdict.reason)

    return {
        "ok": still_failed == 0,
        "retried": retried,
        "recovered": recovered,
        "still_failed": still_failed,
        "reasons": reasons[:10],
    }
