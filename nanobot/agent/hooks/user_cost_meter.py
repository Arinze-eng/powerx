"""Agent hook that counts one turn into the user's own cost meter.

The meter is per-user and persistent, so the counting cannot happen in the WebUI:
the client can see one turn's events and can persist nothing. It happens here,
host-side, next to the tools — and it reuses the classifiers that already exist
rather than writing a second table of tool names that would drift from the feed
beside it:

* ``commands`` and ``pages`` come from :func:`nanobot.agent.activity.classify_kind`,
  the same function the live terminal feed classifies with.
* ``files`` comes from :func:`nanobot.utils.file_edit_events.resolve_file_edit_paths`,
  the same resolver the thread's file rows are built from.
* ``api_calls`` comes from ``usage["llm_calls"]``, the runner's own count of
  distinct requests that reached the configured model.

WHY THE IDENTITY COMES FROM TURN METADATA.

The factory is handed the same :class:`AgentTurnHookContext` the billing hooks
already use, and reads the same ``supabase_user_id`` entry they trust. A turn
with no resolvable identity creates no hook and writes nothing: a meter keyed by
a guess is worse than no meter, because it would file one user's work under
another user's name.

WHY THE WRITE IS DEFERRED TO THE END OF THE RUN.

One write per turn, not one per tool call. A 2000-command job is exactly the job
this meter exists to show, and 2000 read-modify-writes of a shared JSON file
would be a worse problem than the spend it describes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent import activity
from nanobot.agent.hook import (
    AgentHook,
    AgentHookContext,
    AgentRunHookContext,
    AgentTurnHookContext,
)
from nanobot.providers.base import ToolCallRequest
from nanobot.utils.file_edit_events import resolve_file_edit_paths
from nanobot.webui.user_cost import COUNTER_KEYS, record_user_cost

#: Counters a single tool call can move. ``api_calls`` and ``turns`` are not
#: here: those come from the runner's usage and from the run itself, not from a
#: tool.
_TOOL_COUNTER_KEYS = ("commands", "pages", "files", "steps")

#: Channels whose turns carry an authenticated Supabase identity in metadata.
_IDENTIFIED_CHANNELS = {"websocket": "webui", "webui": "webui", "api": "api"}


def count_tool_call(name: str, arguments: Any = None) -> dict[str, int]:
    """Reduce one tool call to the counters it moves.

    Every call is one ``step``. On top of that a call is a command if it ran
    something on a machine, a page if it opened one, and neither otherwise — a
    search, a message and a file read all land in ``steps`` alone, which is what
    keeps ``steps`` an honest denominator instead of a second name for commands.
    """
    counters = {key: 0 for key in _TOOL_COUNTER_KEYS}
    counters["steps"] = 1
    kind = activity.classify_kind(name, arguments)
    if kind in (activity.KIND_COMMAND, activity.KIND_SANDBOX):
        counters["commands"] = 1
    elif kind == activity.KIND_NAV:
        counters["pages"] = 1
    return counters


class UserCostMeterHook(AgentHook):
    """Accumulate a turn's counters and write them to the user's meter once."""

    def __init__(
        self,
        *,
        user_id: str,
        scope: str,
        timezone_name: str | None = None,
        workspace: Path | None = None,
    ) -> None:
        super().__init__()
        self._user_id = user_id
        self._scope = scope
        self._timezone_name = timezone_name
        self._workspace = workspace
        self._counters: dict[str, int] = {key: 0 for key in COUNTER_KEYS}
        #: Highest cumulative ``llm_calls`` seen this turn. Monotone on purpose:
        #: the runner reports a running total, so taking the max can only ever
        #: settle on the true figure and never double-count a retried iteration.
        self._api_calls = 0
        self._files_seen: set[str] = set()
        self._flushed = False

    def _accumulate(self, delta: dict[str, int]) -> None:
        for key, value in delta.items():
            if key in self._counters:
                self._counters[key] += value

    async def after_iteration(self, context: AgentHookContext) -> None:
        usage = context.usage if isinstance(context.usage, dict) else {}
        observed = usage.get("llm_calls")
        if isinstance(observed, int) and observed > self._api_calls:
            self._api_calls = observed

    async def after_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
        result: Any,
    ) -> None:
        try:
            name = str(getattr(tool_call, "name", "") or "")
            arguments = getattr(tool_call, "arguments", None)
            self._accumulate(count_tool_call(name, arguments))
            self._accumulate({"files": self._count_written_files(name, tool, params)})
        except Exception:  # noqa: BLE001 - metering must never break a tool call
            logger.debug("user cost meter failed to count a tool call", exc_info=True)

    def _count_written_files(self, name: str, tool: Any, params: Any) -> int:
        """Count the paths this call actually wrote, once each.

        The resolver is asked for paths rather than the tracker helper being
        reused: the tracker reads every file it touches to snapshot it before the
        edit, and this hook only needs the count. Deduped per turn because a
        patch and a follow-up edit to the same file are one file the user paid
        for, which is also how the thread's file rows fold them.
        """
        if not isinstance(params, dict):
            return 0
        written = 0
        for path in resolve_file_edit_paths(name, tool, self._workspace, params):
            key = str(path)
            if key in self._files_seen:
                continue
            self._files_seen.add(key)
            written += 1
        return written

    async def on_finally(self, context: AgentRunHookContext) -> None:
        """Flush once, on the path that always runs.

        ``after_run`` is skipped entirely when a turn is stopped for billing (the
        runner returns from its own except block), and a stopped turn is exactly
        the turn whose spend a user is most likely to be looking at. ``on_finally``
        runs on both paths, so the meter is written from here and guarded against
        writing twice.
        """
        if self._flushed:
            return
        self._flushed = True
        counters = dict(self._counters)
        counters["turns"] = 1
        usage = context.usage if isinstance(context.usage, dict) else {}
        reported = usage.get("llm_calls")
        authoritative = reported if isinstance(reported, int) and reported >= 0 else 0
        counters["api_calls"] = max(self._api_calls, authoritative)
        try:
            await asyncio.to_thread(
                record_user_cost,
                self._user_id,
                counters,
                scope=self._scope,
                timezone_name=self._timezone_name,
            )
        except Exception:  # noqa: BLE001 - metering is observability, never a stop
            logger.debug("user cost meter failed to flush", exc_info=True)


def create_user_cost_meter_hook(context: AgentTurnHookContext) -> AgentHook | None:
    """Create the per-user meter for a turn that carries an identity.

    Returns ``None`` for any turn this hook could not file: an unidentified
    channel, or a WebUI/API turn whose metadata has no user id. That is the same
    gate the credit hook uses, so a turn that cannot be billed is also a turn
    that is not metered — the two answers come from one fact rather than two.
    """
    scope = _IDENTIFIED_CHANNELS.get(context.channel)
    if scope is None:
        return None
    user_id = (context.metadata or {}).get("supabase_user_id")
    user_id = str(user_id).strip() if user_id else ""
    if not user_id:
        return None
    return UserCostMeterHook(
        user_id=user_id,
        scope=scope,
        workspace=context.workspace,
        timezone_name=context.timezone_name or None,
    )


__all__ = ["UserCostMeterHook", "count_tool_call", "create_user_cost_meter_hook"]
