"""Agentic planner: plan a task once, remember it, replay it for free.

The planner is the top of the zero-API-call stack. For a user task it:

1. **Recalls** a learned plan from persistent plan memory (disk under the
   Northflank data dir) when the task normalizes to a known template —
   zero provider calls.
2. Otherwise **composes** the plan exactly once through a caller-provided
   async ``compose_plan`` hook (the one LLM round-trip this layer ever pays).
3. **Executes** the plan step-by-step through the deterministic
   ``plan_program`` interpreter — loops and steps run with no model in the
   loop, mirroring the Manus command/API ratio.
4. **Remembers** the outcome: successful plans are stored under their
   normalized template for instant recall; plans that failed more than they
   succeeded are demoted and eventually skipped so a bad plan is never
   replayed forever.

Fail-open: if compose fails, memory is unreadable, or the plan itself raises,
the caller falls back to the normal LLM path. Memory can only *save* calls,
never change an answer's correctness path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.config.paths import get_persistent_data_dir

#: Flag: set POWERX_PLANNER=0 to disable plan memory entirely.
_ENABLED = os.environ.get("POWERX_PLANNER", "1").strip().lower() not in {
    "0", "false", "no", "off",
}

#: A plan that has failed at least this many more times than it succeeded is
#: considered learned-bad and skipped in favor of a fresh compose.
_MAX_NET_FAILURES = 2

#: Max stored plans (bounded disk use; oldest by last-use are trimmed).
_MAX_PLANS = 300

#: A single async ``(tool_name, args) -> result`` executor (plan_program).
PlanExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]
#: Async composer: task text -> plan dict (the single LLM-paid step).
PlanComposer = Callable[[str], Awaitable[dict[str, Any] | None]]


def planner_enabled() -> bool:
    return _ENABLED


#: Variable bits stripped from a task so structurally similar asks share a
#: template ("build a script that prints hello" ≡ "...prints goodbye").
_TEMPLATE_BITS = re.compile(
    r"(?x)"
    r"\b\d+(?:\.\d+)?\b"              # numbers
    r"|[\w./~-]+\.[A-Za-z0-9]{1,6}\b" # file names with extensions
    r"|/[\w./~-]+"                    # absolute paths
    r"|['\"].*?['\"]"                 # quoted strings
)


def normalize_task(text: str) -> str:
    """Collapse a task to its structural template."""
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    normalized = _TEMPLATE_BITS.sub("<x>", normalized)
    normalized = re.sub(r"(<x>(\s+<x>)+)", "<x>", normalized)
    return normalized.strip()


def template_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_task(text).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class PlanMemoryEntry:
    """One learned plan: the program plus its lifetime success record."""

    template: str
    plan: dict[str, Any]
    success_count: int = 0
    failure_count: int = 0
    last_used: float = 0.0
    last_error: str | None = None

    @property
    def net_failures(self) -> int:
        return self.failure_count - self.success_count

    @property
    def learned_bad(self) -> bool:
        return self.net_failures >= _MAX_NET_FAILURES

    def to_json(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "plan": self.plan,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_used": self.last_used,
            "last_error": self.last_error,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "PlanMemoryEntry":
        return cls(
            template=str(payload.get("template") or ""),
            plan=payload.get("plan") or {},
            success_count=int(payload.get("success_count") or 0),
            failure_count=int(payload.get("failure_count") or 0),
            last_used=float(payload.get("last_used") or 0.0),
            last_error=payload.get("last_error"),
        )


class AgentPlanner:
    """Plan memory over the persistent data dir, wired to plan_program."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or get_persistent_data_dir("plan_memory")
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.debug("plan memory init failed: {}", exc)

    # -- memory -------------------------------------------------------------

    def _path(self, fingerprint: str) -> Path:
        return self._root / f"{fingerprint}.json"

    def recall(self, task_text: str) -> dict[str, Any] | None:
        """Return the learned plan for a structurally similar task, or None."""
        if not _ENABLED:
            return None
        path = self._path(template_fingerprint(task_text))
        try:
            if not path.is_file():
                return None
            entry = PlanMemoryEntry.from_json(json.loads(path.read_text(encoding="utf-8")))
            if entry.learned_bad:
                logger.info("plan memory: template {} learned-bad, recomposing", entry.template[:48])
                return None
            entry.last_used = time.time()
            path.write_text(json.dumps(entry.to_json(), ensure_ascii=False), encoding="utf-8")
            logger.info("plan memory: replaying learned plan ({})", entry.template[:48])
            return entry.plan
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("plan memory read failed: {}", exc)
            return None

    def remember(
        self,
        task_text: str,
        plan: dict[str, Any],
        *,
        success: bool,
        error: str | None = None,
    ) -> bool:
        """Store (or re-score) the plan under the task's template."""
        if not _ENABLED or not isinstance(plan, dict):
            return False
        fingerprint = template_fingerprint(task_text)
        path = self._path(fingerprint)
        try:
            if path.is_file():
                entry = PlanMemoryEntry.from_json(json.loads(path.read_text(encoding="utf-8")))
            else:
                entry = PlanMemoryEntry(template=normalize_task(task_text), plan=plan)
            # The latest plan wins: a recomposed plan supersedes the old one.
            entry.plan = plan
            entry.last_used = time.time()
            if success:
                entry.success_count += 1
                entry.last_error = None
            else:
                entry.failure_count += 1
                entry.last_error = (error or "unknown error")[:400]
            path.write_text(json.dumps(entry.to_json(), ensure_ascii=False), encoding="utf-8")
            self._trim()
            return True
        except (OSError, TypeError, ValueError) as exc:
            logger.debug("plan memory write failed: {}", exc)
            return False

    def forget(self, task_text: str) -> None:
        """Drop a learned plan (e.g. after a reflection proved it wrong)."""
        try:
            self._path(template_fingerprint(task_text)).unlink(missing_ok=True)
        except OSError:
            pass

    def _trim(self) -> None:
        try:
            entries = sorted(
                self._root.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for stale in entries[_MAX_PLANS:]:
                stale.unlink(missing_ok=True)
        except OSError:
            pass

    # -- planning + execution ------------------------------------------------

    async def plan_and_execute(
        self,
        task_text: str,
        compose_plan: PlanComposer,
        execute: PlanExecutor,
        *,
        run_plan: Callable[[dict[str, Any], PlanExecutor], Awaitable[Any]],
    ) -> tuple[Any, dict[str, Any] | None] | None:
        """Recall-or-compose a plan, execute it, and remember the outcome.

        Returns ``(plan_result, plan)`` on success, or ``None`` when no plan
        could be produced (caller falls back to the normal LLM path).
        ``run_plan`` is typically ``plan_program.execute_plan``.
        """
        plan = self.recall(task_text)
        composed = False
        if plan is None:
            plan = await compose_plan(task_text)  # the ONE provider call
            composed = True
            if not isinstance(plan, dict):
                return None
        try:
            result = await run_plan(plan, execute)
        except Exception as exc:  # noqa: BLE001 - fail-open by design
            logger.debug("plan execution failed: {}", exc)
            self.remember(task_text, plan, success=False, error=str(exc))
            return None
        failed = bool(getattr(result, "failed", False))
        self.remember(
            task_text,
            plan,
            success=not failed,
            error=str(getattr(result, "final", "") or "")[:400] or None,
        )
        logger.info(
            "planner: task executed via {} plan ({} steps)",
            "replayed" if not composed else "freshly composed",
            getattr(result, "executed_steps", 0),
        )
        return result, plan


def make_planner() -> AgentPlanner:
    return AgentPlanner()
