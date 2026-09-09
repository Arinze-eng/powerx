"""Zero-call tool middleware: format, cache, and dedupe — no LLM round-trips.

Manus-style cost discipline has three layers already: the deterministic router
answers unambiguous read-only asks before any provider call, ``sandbox_batch``
collapses N sandbox ops into one round-trip, and the replay cache serves an
identical *task* from disk. This module adds the fourth layer — everything that
happens **after** a tool has run should never cost another API call unless it
genuinely needs reasoning:

* **Deterministic formatting.** A structured-data tool (UniAbuja lookups)
  returns JSON; the model is normally paid to re-render that JSON for chat.
  When the output parses cleanly as a known envelope, we render it with a pure
  function and stop — zero further calls. The model only sees the payload when
  formatting is ambiguous or carries an error verdict it may want to rephrase
  or act on.

* **Short-TTL live cache.** Announcements and account status change slowly
  relative to how fast students ask. Repeats inside the TTL window are served
  straight from disk: not even the tool runs. Transcript and by-regno reads are
  never cached (per-record correctness beats micro-costs).

* **In-flight singleflight.** Five students pinging "any announcements?" at
  once execute the lookup ONCE; the others await the same result instead of
  hammering the backend (and its per-query cost) five times.

Fail-open everywhere, mirroring the deterministic router: any parse miss,
unexpected envelope shape, cache-store failure, or exception falls through to
the normal in-loop behaviour, so correctness is preserved while routine turns
stop costing calls. Gated per-run via ``spec.tool_middleware`` so generic agents
on the same runner are untouched.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from nanobot.agent.tools.registry import ToolRegistry, is_tool_error_result
from nanobot.providers.base import ToolCallRequest

#: Middleware only exists for agent runs that opt in explicitly.
_TOOL_NAMES = frozenset({"uniabuja_student", "uniabuja_transcript"})

#: Which query resources tolerate a short staleness window, in seconds.
#: Announcements update a few times a day; access status changes when an admin
#: toggles it. "my_questions" is deliberately NOT here — a student who just
#: submitted a question expects their next "show my questions" to include it.
_STABLE_RESOURCES: dict[str, float] = {
    "announcements": 120.0,
}

#: Status reads are identity-scoped but effectively configuration facts; a
#: two-minute window absorbs retry storms without meaningfully staling.
_STATUS_TTL_SECONDS = 120.0

#: Guard against pathological inputs reaching the renderer / cache key.
_MAX_CACHE_KEY_CHARS = 2_000


def _ttl_multiplier() -> float:
    """Scale factor for cache TTLs (tests shrink it; operators can tune it)."""
    raw = os.environ.get("POWERX_TOOL_MIDDLEWARE_TTL_SCALE", "1").strip()
    try:
        value = float(raw)
    except ValueError:
        return 1.0
    return value if value > 0 else 1.0



def middleware_enabled() -> bool:
    """Live env switch so operators can disable the layer without a restart."""
    return os.environ.get("POWERX_TOOL_MIDDLEWARE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


# ---------------------------------------------------------------------------
# Pure rendering: structured tool output -> chat text, zero tokens spent
# ---------------------------------------------------------------------------


def _first_str(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _fmt_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None or (isinstance(value, str) and not value.strip()):
        return "-"
    return str(value)


def _render_rows(rows: list[Any], max_rows: int = 8) -> list[str]:
    lines: list[str] = []
    for row in rows[:max_rows]:
        if not isinstance(row, dict):
            lines.append(f"• {_fmt_value(row)}")
            continue
        # Prefer human-meaningful fields when present; otherwise compact pairs.
        title = _first_str(
            row, "title", "subject", "headline", "name", "question", "message", "text"
        )
        when = _first_str(row, "date", "created_at", "posted_at", "published_at", "time")
        detail = _first_str(row, "body", "content", "description", "summary")
        if title:
            suffix = f" ({when})" if when else ""
            lines.append(f"• {title}{suffix}")
            if detail:
                clipped = detail if len(detail) <= 160 else detail[:157].rstrip() + "..."
                lines.append(f"  {clipped}")
        else:
            pairs = ", ".join(
                f"{k}: {_fmt_value(v)}"
                for k, v in list(row.items())[:6]
                if v not in (None, "")
            )
            lines.append(f"• {pairs}" if pairs else "• (empty row)")
    if len(rows) > max_rows:
        lines.append(f"…and {len(rows) - max_rows} more.")
    return lines


def render_uniabuja_output(tool_name: str, arguments: dict[str, Any], raw: str) -> str | None:
    """Return chat-ready text for a structured UniAbuja tool output, or None.

    ``None`` means "not confidently renderable" — the caller then feeds the
    payload back into the loop exactly as today. Only well-formed envelopes
    (``{"ok": true, ...}`` for the student tool, ``{"regno": ...}`` shaped
    records for the transcript tool) are rendered; anything else is left to
    the model.
    """
    text = (raw or "").strip()
    if not text or not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    action = str(arguments.get("action") or "").strip().lower()

    if tool_name == "uniabuja_student":
        if data.get("ok") is not True:
            return None  # error/partial envelope -> let the model decide
        if action == "status":
            lines = ["👤 Account status"]
            skip = {"ok", "action"}
            for key, value in data.items():
                if key in skip:
                    continue
                label = str(key).replace("_", " ")
                lines.append(f"• {label}: {_fmt_value(value)}")
            return "\n".join(lines)
        if action == "query":
            resource = str(arguments.get("resource") or "").strip().lower()
            rows = data.get("rows")
            if not isinstance(rows, list):
                return None
            heading = {
                "announcements": "📢 Announcements",
                "my_questions": "❓ My questions",
                "knowledge": "📚 Knowledge base",
            }.get(resource, f"📋 {resource or 'Results'}")
            if not rows:
                return f"{heading}\nNothing found right now."
            return "\n".join([heading, *_render_rows(rows)])
        return None

    if tool_name == "uniabuja_transcript" and action == "student_lookup":
        # The transcript tool renders SQL results as text; only fully
        # structured (JSON object) outputs are safe to format deterministically.
        if data.get("ok") is not True:
            return None
        student = data.get("student")
        courses = data.get("courses") or data.get("results")
        lines = ["🎓 Student record"]
        if isinstance(student, dict):
            name = _first_str(student, "full_name", "name") or " ".join(
                part
                for part in (
                    _first_str(student, "first_name"),
                    _first_str(student, "other_name"),
                    _first_str(student, "surname", "last_name"),
                )
                if part
            )
            if name:
                lines.append(f"• Name: {name}")
            for key in ("regno", "prog_id", "programme", "session_of_entry", "status", "level"):
                if key in student:
                    lines.append(f"• {str(key).replace('_', ' ')}: {_fmt_value(student[key])}")
        elif student is not None:
            return None
        if isinstance(courses, list):
            if courses:
                lines.append("• Courses:")
                lines.extend(_render_rows(courses, max_rows=15))
            else:
                lines.append("• No course results found.")
        return "\n".join(lines) if len(lines) > 1 else None

    return None


# ---------------------------------------------------------------------------
# Short-TTL live cache + in-flight singleflight
# ---------------------------------------------------------------------------


@dataclass
class ToolMiddleware:
    """Per-process middleware applied to selected tool calls pre-execution.

    Instances are created per runner (the agent loop owns one long-lived
    runner), so cache state is scoped to the server lifetime — matching how
    the replay cache is scoped — and never leaks across unrelated processes.
    """

    _cache: dict[str, tuple[float, Any]] = field(default_factory=dict)
    _inflight: dict[str, asyncio.Future] = field(default_factory=dict)

    def handles(self, call: ToolCallRequest) -> bool:
        """Whether this middleware governs *call* for the current run."""
        if not middleware_enabled():
            return False
        if call.name not in _TOOL_NAMES:
            return False
        return isinstance(call.arguments, dict)

    @staticmethod
    def _ttl_for_call(call: ToolCallRequest) -> float:
        args = call.arguments if isinstance(call.arguments, dict) else {}
        action = str(args.get("action") or "").strip().lower()
        if call.name == "uniabuja_student" and action == "query":
            resource = str(args.get("resource") or "").strip().lower()
            base = _STABLE_RESOURCES.get(resource, 0.0)
        elif call.name == "uniabuja_student" and action == "status":
            base = _STATUS_TTL_SECONDS
        else:
            base = 0.0
        return base * _ttl_multiplier() if base > 0 else 0.0

    def cache_key(self, call: ToolCallRequest) -> str | None:
        """Stable identity for cacheable (slow-changing) lookups, else None."""
        ttl = self._ttl_for_call(call)
        if ttl <= 0:
            return None  # transcript reads & volatile resources: never cached
        args = call.arguments if isinstance(call.arguments, dict) else {}
        key = f"{call.name}:{str(args.get('action') or '').strip().lower()}:{str(args.get('resource') or '').strip().lower()}"
        if len(key) > _MAX_CACHE_KEY_CHARS:
            return None
        return key

    def get_cached(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, result = entry
        if time.monotonic() >= expires_at:
            self._cache.pop(key, None)
            return None
        return result

    def put_cached(self, key: str, result: Any, ttl: float) -> None:
        # Bound memory: drop expired entries opportunistically.
        if len(self._cache) > 128:
            now = time.monotonic()
            for stale in [k for k, (exp, _) in self._cache.items() if now >= exp]:
                self._cache.pop(stale, None)
        self._cache[key] = (time.monotonic() + max(5.0, ttl), result)

    async def execute(self, registry: ToolRegistry, call: ToolCallRequest) -> Any:
        """Run *call* through cache/singleflight, returning the tool result.

        Mirrors ``ToolRegistry.execute`` semantics so the outcome is
        indistinguishable from the in-loop path — except that repeats inside
        the TTL window never reach the backend and concurrent duplicates
        collapse into one execution.
        """
        key = self.cache_key(call)
        if key is None:
            return await registry.execute(call.name, call.arguments)

        cached = self.get_cached(key)
        if cached is not None:
            return cached

        existing = self._inflight.get(key)
        if existing is not None:
            try:
                return await asyncio.shield(existing)
            except asyncio.CancelledError:
                raise
            except BaseException:
                # The leader failed; do not poison followers — fall through
                # and execute ourselves.
                pass

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await registry.execute(call.name, call.arguments)
            if not is_tool_error_result(result):
                self.put_cached(key, result, self._ttl_for_call(call))
            if not future.done():
                future.set_result(result)
            return result
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            self._inflight.pop(key, None)
            # A follower whose await was cancelled would otherwise leave an
            # "exception never retrieved" warning; consume it quietly.
            try:
                if future.done() and not future.cancelled():
                    future.exception()
            except BaseException:
                pass



# ---------------------------------------------------------------------------
# Runner integration notes
# ---------------------------------------------------------------------------
# The runner owns the only two touch points:
#   1. pre-execution — deterministic-router answers go through
#      ToolMiddleware.execute() so rapid repeats skip even the backend;
#   2. post-execution — a successful governed call whose output renders via
#      render_uniabuja_output() ends the turn with zero further provider calls
#      (see AgentRunner._try_zero_call_finalize).
# try_zero_call_completion is kept as the public helper for tests and future
# callers that want the full "format or fall through" decision in one place.

async def try_zero_call_completion(
    spec: Any,
    messages: list[dict[str, Any]],
    call: ToolCallRequest,
    result: Any,
) -> str | None:
    """Return chat-ready final text for *result*, or None to continue the loop.

    Strictly fail-open: non-governed tools, error verdicts (the model may
    recover), unparseable payloads, and renderer crashes all return None so
    the caller keeps today's in-loop behaviour.
    """
    if not middleware_enabled():
        return None
    if spec is None or not getattr(spec, "tool_middleware", False):
        return None
    if call.name not in _TOOL_NAMES:
        return None
    if is_tool_error_result(result):
        # Error verdicts stay in-loop: the model may recover (retry, explain,
        # ask for a regno). Formatting them as finals could mask recovery paths.
        return None
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    try:
        formatted = render_uniabuja_output(call.name, arguments, str(result))
    except Exception:  # pragma: no cover - renderer must never break a turn
        logger.exception("tool middleware: renderer crashed, falling back to model")
        return None
    return formatted or None
