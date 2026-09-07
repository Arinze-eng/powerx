"""Cron tool for scheduling reminders and tasks."""

# pyright: reportIncompatibleMethodOverride=false

from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import datetime
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronJobState, CronSchedule
from nanobot.session.keys import UNIFIED_SESSION_KEY

_CRON_PARAMETERS = tool_parameters_schema(
    action=StringSchema(
        "Action to perform",
        enum=[
            "add",
            "list",
            "remove",
            "update",
            "pause",
            "resume",
            "run_now",
            "info",
        ],
    ),
    name=StringSchema(
        "Short human-readable label for the job (e.g., 'weather-monitor', 'daily-standup'). "
        "For action='add' it defaults to the first 30 chars of message; for action='update' "
        "it renames the job identified by job_id."
    ),
    message=StringSchema(
        "Instruction for the agent to execute when the job triggers (e.g., 'Send a reminder "
        "to WeChat: xxx' or 'Check system status and report'). REQUIRED when action='add'; "
        "when provided with action='update' it replaces the job's instruction."
    ),
    every_seconds=IntegerSchema(
        description="Interval in seconds (for recurring tasks). Used by add and update.",
        minimum=1,
    ),
    cron_expr=StringSchema(
        "Cron expression like '0 9 * * *' (for scheduled tasks). Used by add and update."
    ),
    tz=StringSchema(
        "Optional IANA timezone for cron expressions (e.g. 'America/Vancouver'). "
        "When omitted with cron_expr, the tool's default timezone applies."
    ),
    at=StringSchema(
        "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00'). "
        "Naive values use the tool's default timezone. Used by add and update."
    ),
    job_id=StringSchema(
        "Job ID (obtain via action='list'). REQUIRED when action='remove', and also for "
        "action='update', 'pause', 'resume', 'run_now', and 'info'."
    ),
    required=["action"],
    description=(
        "Manage scheduled jobs. add requires a non-empty message plus one schedule "
        "(every_seconds, cron_expr, or at). update takes job_id plus any fields to change "
        "(name/message/schedule). pause/resume/remove/run_now/info take job_id. list needs "
        "only action. Per-action requirements are enforced at runtime so the top-level schema "
        "stays compatible with providers (e.g. OpenAI Codex/Responses) that reject "
        "oneOf/anyOf/allOf/enum/not at the root of function parameters."
    ),
)


@tool_parameters(_CRON_PARAMETERS)
class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(self, cron_service: CronService, default_timezone: str = "UTC"):
        self._cron = cron_service
        self._default_timezone = default_timezone
        self._in_cron_context: ContextVar[bool] = ContextVar("cron_in_context", default=False)

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return ctx.cron_service is not None

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        cron_service = ctx.cron_service
        if cron_service is None:
            raise RuntimeError("CronTool requires an initialized cron service")
        return cls(cron_service=cron_service, default_timezone=ctx.timezone)

    @staticmethod
    def _request_route() -> tuple[str, str, str, dict[str, Any]]:
        """Return routing from the authoritative request snapshot."""
        ctx = current_request_context()
        if ctx is None:
            return "", "", "", {}
        raw_key = f"{ctx.channel}:{ctx.chat_id}" if ctx.channel and ctx.chat_id else ""
        session_key = (
            raw_key if ctx.session_key == UNIFIED_SESSION_KEY else (ctx.session_key or "")
        )
        return session_key, ctx.channel or "", ctx.chat_id or "", dict(ctx.metadata or {})

    def set_cron_context(self, active: bool) -> Token[bool]:
        """Mark whether the tool is executing inside a cron job callback."""
        return self._in_cron_context.set(active)

    def reset_cron_context(self, token: Token[bool]) -> None:
        """Restore previous cron context."""
        self._in_cron_context.reset(token)

    @staticmethod
    def _validate_timezone(tz: str) -> str | None:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(tz)
        except (KeyError, Exception):
            return ToolResult.error(f"Error: unknown timezone '{tz}'")
        return None

    def _display_timezone(self, schedule: CronSchedule) -> str:
        """Pick the most human-meaningful timezone for display."""
        return schedule.tz or self._default_timezone

    @staticmethod
    def _format_timestamp(ms: int, tz_name: str) -> str:
        from zoneinfo import ZoneInfo

        dt = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz_name))
        return f"{dt.isoformat()} ({tz_name})"

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Schedule and manage time-based tasks. Actions: add, list, info, update, remove, "
            "pause, resume, run_now. Supports one-shot ('at'), interval ('every_seconds'), and "
            "cron-expression ('cron_expr' + optional 'tz') schedules. "
            f"If tz is omitted, cron expressions and naive ISO times default to {self._default_timezone}."
        )

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        action = params.get("action")
        if action == "add" and not str(params.get("message") or "").strip():
            errors.append("message is required when action='add'")
        if action in {"remove", "update", "pause", "resume", "run_now", "info"} and not str(
            params.get("job_id") or ""
        ).strip():
            errors.append(f"job_id is required when action='{action}'")
        return errors

    async def execute(
        self,
        action: str,
        name: str | None = None,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
    ) -> str:
        if action == "add":
            if self._in_cron_context.get():
                return ToolResult.error("Error: cannot schedule new jobs from within a cron job execution")
            return self._add_job(name, message, every_seconds, cron_expr, tz, at)
        elif action == "list":
            return self._list_jobs()
        elif action == "info":
            return self._job_info(job_id)
        elif action == "update":
            return self._update_job(job_id, name, message, every_seconds, cron_expr, tz, at)
        elif action == "remove":
            return self._remove_job(job_id)
        elif action == "pause":
            return self._set_enabled(job_id, False, "Paused")
        elif action == "resume":
            return self._set_enabled(job_id, True, "Resumed")
        elif action == "run_now":
            return await self._run_now(job_id)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        name: str | None,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
    ) -> str:
        if not message:
            return ToolResult.error(
                "Error: cron action='add' requires a non-empty 'message' parameter "
                "describing what to do when the job triggers "
                "(e.g. the reminder text). Retry including message=\"...\"."
            )
        session_key, origin_channel, origin_chat_id, origin_metadata = self._request_route()
        # General fallback: a cron job must be schedulable from ANY context
        # (chat sessions, the public API, or fully autonomous runs). When no
        # chat-bound request context is available, bind the job to a stable
        # general session so it still schedules and runs through the general
        # delivery path instead of being refused like the old code did.
        if not (session_key and origin_channel and origin_chat_id):
            session_key = session_key or "general"
            origin_channel = origin_channel or "webui"
            origin_chat_id = origin_chat_id or "general"
        if tz and not cron_expr:
            return ToolResult.error("Error: tz can only be used with cron_expr")
        if tz:
            if err := self._validate_timezone(tz):
                return err

        # Build schedule
        result = self._build_schedule(every_seconds, cron_expr, tz, at)
        if len(result) == 1:  # error string returned
            return result[0]
        schedule, delete_after = result

        job = self._cron.add_job(
            name=name or message[:30],
            schedule=schedule,
            message=message,
            delete_after_run=delete_after,
            session_key=session_key,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            origin_metadata=origin_metadata,
        )
        return f"Created job '{job.name}' (id: {job.id})"

    def _build_schedule(
        self,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
    ) -> "tuple[str] | tuple[CronSchedule, bool]":
        """Build a CronSchedule from one of the timing params.

        Returns ``(schedule, delete_after_run)`` on success, or a single-element
        tuple containing an error string (to be returned directly by callers).
        """
        delete_after = False
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            effective_tz = tz or self._default_timezone
            if err := self._validate_timezone(effective_tz):
                return (err,)
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=effective_tz)
        elif at:
            from zoneinfo import ZoneInfo

            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return (
                    f"Error: invalid ISO datetime format '{at}'. Expected format: YYYY-MM-DDTHH:MM:SS",
                )
            if dt.tzinfo is None:
                if err := self._validate_timezone(self._default_timezone):
                    return (err,)
                dt = dt.replace(tzinfo=ZoneInfo(self._default_timezone))
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return ("Error: either every_seconds, cron_expr, or at is required",)
        return (schedule, delete_after)

    def _update_job(
        self,
        job_id: str | None,
        name: str | None,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
    ) -> str:
        if not job_id:
            return ToolResult.error("Error: cron action='update' requires 'job_id'")
        has_new_schedule = any([every_seconds, cron_expr, at])
        if tz and not cron_expr:
            return ToolResult.error("Error: tz can only be used with cron_expr")
        if has_new_schedule:
            result = self._build_schedule(every_seconds, cron_expr, tz, at)
            if len(result) == 1:
                return result[0]
            schedule, delete_after = result  # type: ignore[misc]
        else:
            schedule = None
            delete_after = None

        kwargs: dict[str, Any] = {}
        if name is not None:
            kwargs["name"] = name
        if message:
            kwargs["message"] = message
        if schedule is not None:
            kwargs["schedule"] = schedule
        if delete_after is not None:
            kwargs["delete_after_run"] = delete_after
        if not kwargs:
            return ToolResult.error(
                "Error: nothing to update — provide at least one of name, message, "
                "or a new schedule (every_seconds/cron_expr/at)."
            )

        result = self._cron.update_job(job_id, **kwargs)
        if result == "not_found":
            return f"Job {job_id} not found"
        if result == "protected":
            return (
                f"Cannot update job `{job_id}`. This is a protected system-managed cron job."
            )
        job = result
        return f"Updated job '{job.name}' (id: {job.id})"

    def _set_enabled(self, job_id: str | None, enabled: bool, verb: str) -> str:
        if not job_id:
            return ToolResult.error(f"Error: cron action='{verb.lower()}' requires 'job_id'")
        job = self._cron.enable_job(job_id, enabled)
        if job is None:
            return f"Job {job_id} not found"
        return f"{verb} job '{job.name}' (id: {job.id})"

    async def _run_now(self, job_id: str | None) -> str:
        if not job_id:
            return ToolResult.error("Error: cron action='run_now' requires 'job_id'")
        if self._cron.get_job(job_id) is None:
            return f"Job {job_id} not found"
        ran = await self._cron.run_job(job_id, force=True)
        if not ran:
            return f"Could not run job {job_id}"
        # Re-fetch: execution may have advanced state on a freshly loaded store.
        job = self._cron.get_job(job_id)
        status = (job.state.last_status if job else None) or "unknown"
        detail = f" ({job.state.last_error})" if job and job.state.last_error else ""
        name = job.name if job else job_id
        return f"Ran job '{name}' (id: {job_id}) — status: {status}{detail}"

    def _job_info(self, job_id: str | None) -> str:
        if not job_id:
            return ToolResult.error("Error: cron action='info' requires 'job_id'")
        job = self._cron.get_job(job_id)
        if job is None:
            return f"Job {job_id} not found"
        lines = [
            f"Job '{job.name}' (id: {job.id})",
            f"  Enabled: {job.enabled}",
            f"  Timing: {self._format_timing(job.schedule)}",
            f"  Message: {job.payload.message}",
        ]
        lines.extend(self._format_state(job.state, job.schedule))
        if job.state.run_history:
            recent = job.state.run_history[-5:]
            hist = ", ".join(
                f"{r.status}@{self._format_timestamp(r.run_at_ms, self._display_timezone(job.schedule)).split(' ')[0]}"
                for r in recent
            )
            lines.append(f"  Recent runs: {hist}")
        return "\n".join(lines)

    def _format_timing(self, schedule: CronSchedule) -> str:
        """Format schedule as a human-readable timing string."""
        if schedule.kind == "cron":
            tz = f" ({schedule.tz})" if schedule.tz else ""
            return f"cron: {schedule.expr}{tz}"
        if schedule.kind == "every" and schedule.every_ms:
            ms = schedule.every_ms
            if ms % 3_600_000 == 0:
                return f"every {ms // 3_600_000}h"
            if ms % 60_000 == 0:
                return f"every {ms // 60_000}m"
            if ms % 1000 == 0:
                return f"every {ms // 1000}s"
            return f"every {ms}ms"
        if schedule.kind == "at" and schedule.at_ms:
            return f"at {self._format_timestamp(schedule.at_ms, self._display_timezone(schedule))}"
        return schedule.kind

    def _format_state(self, state: CronJobState, schedule: CronSchedule) -> list[str]:
        """Format job run state as display lines."""
        lines: list[str] = []
        display_tz = self._display_timezone(schedule)
        if state.last_run_at_ms:
            info = (
                f"  Last run: {self._format_timestamp(state.last_run_at_ms, display_tz)}"
                f" — {state.last_status or 'unknown'}"
            )
            if state.last_error:
                info += f" ({state.last_error})"
            lines.append(info)
        if state.next_run_at_ms:
            lines.append(f"  Next run: {self._format_timestamp(state.next_run_at_ms, display_tz)}")
        return lines

    @staticmethod
    def _system_job_purpose(job: CronJob) -> str:
        if job.name == "dream":
            return "Dream memory consolidation for long-term memory."
        return "System-managed internal job."

    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines: list[str] = []
        for j in jobs:
            timing = self._format_timing(j.schedule)
            parts = [f"- {j.name} (id: {j.id}, {timing})"]
            if j.payload.kind == "system_event":
                parts.append(f"  Purpose: {self._system_job_purpose(j)}")
                parts.append("  Protected: visible for inspection, but cannot be removed.")
            parts.extend(self._format_state(j.state, j.schedule))
            lines.append("\n".join(parts))
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return ToolResult.error("Error: job_id is required for remove")
        result = self._cron.remove_job(job_id)
        if result == "removed":
            return f"Removed job {job_id}"
        if result == "protected":
            job = self._cron.get_job(job_id)
            if job and job.name == "dream":
                return (
                    "Cannot remove job `dream`.\n"
                    "This is a system-managed Dream memory consolidation job for long-term memory.\n"
                    "It remains visible so you can inspect it, but it cannot be removed."
                )
            return (
                f"Cannot remove job `{job_id}`.\n"
                "This is a protected system-managed cron job."
            )
        return f"Job {job_id} not found"
