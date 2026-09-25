"""Agent hook that adapts runner events into channel progress UI."""

from __future__ import annotations

import inspect
import json
from typing import Any, Awaitable, Callable, cast

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.providers.base import ToolCallRequest
from nanobot.utils.helpers import IncrementalThinkExtractor, strip_think
from nanobot.utils.live_label import live_label_for
from nanobot.utils.progress_events import (
    build_tool_event_finish_payloads,
    build_tool_event_start_payload,
    invoke_on_progress,
    on_progress_accepts_tool_events,
)
from nanobot.utils.tool_hints import format_tool_hints


def _format_elapsed(seconds: float) -> str:
    """Seconds as the shortest honest form: ``45s``, ``2m10s``, ``1h02m``.

    Whole seconds only. A tool that has been blocked for two and a bit minutes
    reads as "2m10s" -- sub-second precision here would be noise on a number that
    exists to say "this is still going".
    """
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


class AgentProgressHook(AgentHook):
    """Translate runner lifecycle events into user-visible progress signals."""

    def __init__(
        self,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        *,
        session_key: str | None = None,
        tool_hint_max_length: int = 40,
        on_iteration: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__(reraise=True)
        self._on_progress = on_progress
        self._on_stream = on_stream
        self._on_stream_end = on_stream_end
        self._session_key = session_key
        self._tool_hint_max_length = tool_hint_max_length
        self._on_iteration = on_iteration
        self._stream_buf = ""
        self._think_extractor = IncrementalThinkExtractor()
        self._reasoning_open = False

    def wants_streaming(self) -> bool:
        return self._on_stream is not None

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        if not text:
            return None
        return strip_think(text) or None

    def _tool_hint(self, tool_calls: list[Any]) -> str:
        return format_tool_hints(tool_calls, max_length=self._tool_hint_max_length)

    @staticmethod
    def _live_usage(context: AgentHookContext) -> dict[str, int] | None:
        """Return a minimal live usage snapshot for the UI's cost panel.

        Only ``llm_calls`` is streamed mid-turn (the honest count of distinct
        model requests so far). Token totals are withheld until completion to
        avoid implying a per-step billing figure the user can't yet trust.
        Returns ``None`` when no counter is available so older paths are
        unaffected.
        """
        usage = context.usage or {}
        llm_calls = usage.get("llm_calls")
        if isinstance(llm_calls, int) and llm_calls >= 0:
            return {"llm_calls": llm_calls}
        return None

    @staticmethod
    def _on_progress_accepts(cb: Callable[..., Any], name: str) -> bool:
        try:
            sig = inspect.signature(cb)
        except (TypeError, ValueError):
            return False
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            return True
        return name in sig.parameters

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        prev_clean = strip_think(self._stream_buf)
        self._stream_buf += delta
        new_clean = strip_think(self._stream_buf)
        incremental = new_clean[len(prev_clean) :]

        if await self._think_extractor.feed(self._stream_buf, self.emit_reasoning):
            context.streamed_reasoning = True

        if incremental:
            # Answer text has started; close the reasoning segment so the UI can
            # lock the bubble before the answer renders below it.
            await self.emit_reasoning_end()
            if self._on_stream:
                await self._on_stream(incremental)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self.emit_reasoning_end()
        if self._on_stream_end:
            kwargs: dict[str, bool] = {"resuming": resuming}
            if (
                context.stream_continues_current_message
                and self._on_progress_accepts(self._on_stream_end, "merge_next")
            ):
                kwargs["merge_next"] = True
            await self._on_stream_end(**kwargs)
        self._stream_buf = ""
        self._think_extractor.reset()

    async def before_iteration(self, context: AgentHookContext) -> None:
        if self._on_iteration:
            self._on_iteration(context.iteration)
        logger.debug(
            "Starting agent loop iteration {} for session {}",
            context.iteration,
            self._session_key,
        )

    async def on_provider_tool_event(
        self,
        context: AgentHookContext,
        event: dict[str, Any],
    ) -> None:
        if not self._on_progress:
            return
        phase = event.get("phase")
        name = event.get("name")
        call_id = event.get("call_id")
        if (
            phase not in {"start", "end", "error"}
            or not isinstance(name, str)
            or not name
            or not call_id
        ):
            return
        arguments = event.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        payload: dict[str, Any] = {
            "version": 1,
            "phase": phase,
            "call_id": str(call_id),
            "name": name,
            "arguments": arguments,
            "result": event.get("result") if phase == "end" else None,
            "error": event.get("error") if phase == "error" else None,
            "files": [],
            "embeds": [],
        }
        if phase == "start":
            await self.emit_reasoning_end()
            tool_call = ToolCallRequest(id=str(call_id), name=name, arguments=arguments)
            tool_hint = self._strip_think(self._tool_hint([tool_call])) or name
            await invoke_on_progress(
                self._on_progress,
                tool_hint,
                tool_hint=True,
                tool_events=[payload],
            )
            logger.info(
                "Provider-hosted tool call: {}({})",
                name,
                json.dumps(arguments, ensure_ascii=False)[:200],
            )
            return
        if on_progress_accepts_tool_events(self._on_progress):
            await invoke_on_progress(
                self._on_progress,
                "",
                tool_hint=False,
                tool_events=[payload],
            )

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        if self._on_progress:
            if not self._on_stream and not context.streamed_content:
                thought = self._strip_think(context.response.content if context.response else None)
                if thought:
                    await self._on_progress(thought)
            tool_hint = self._strip_think(self._tool_hint(context.tool_calls))
            tool_events = [build_tool_event_start_payload(tc) for tc in context.tool_calls]
            await invoke_on_progress(
                self._on_progress,
                cast(str, tool_hint),
                tool_hint=True,
                tool_events=tool_events,
                usage=self._live_usage(context),
            )
        for tc in context.tool_calls:
            args_str = json.dumps(tc.arguments, ensure_ascii=False)
            logger.info("Tool call: {}({})", tc.name, args_str[:200])

    async def on_tool_heartbeat(
        self,
        context: AgentHookContext,
        tool_call: Any,
        elapsed_s: float,
    ) -> None:
        """Say what a blocked tool is doing, and for how long.

        Two shapes, in order of preference:

        * A tool that OBSERVES something over time publishes a live label (see
          ``nanobot.utils.live_label``). When one is fresh we send that instead
          of the generic hint, because "watching XAUUSD - bid 4167.30, +0.42R"
          is the thing the user asked to see, and "still running" tells them
          nothing except that they are still waiting. The label is always a
          statement about the LAST frame the tool actually read, never a
          prediction.
        * Every other tool gets the hint it always got: "checking X - still
          running (24s)".

        Both reuse the tool-hint channel rather than inventing a new event type,
        so every surface that already renders "checking ..." renders this too and
        nothing has to learn a new message shape. The hint is re-sent with the
        elapsed time appended, which is the part that makes it progress: an
        identical string repeated would be deduped by the UI and would look
        exactly as frozen as saying nothing. That is also why the elapsed time
        stays on a live label -- without it, a tool whose observation is stable
        across beats ("guard live, +0.10R") would render as a frozen line.

        Failure here is already handled by the caller (the runner swallows it), so
        this stays deliberately small.
        """
        if not self._on_progress:
            return
        name = getattr(tool_call, "name", None)
        live = live_label_for(name)
        if live:
            await invoke_on_progress(
                self._on_progress,
                f"{live} - {_format_elapsed(elapsed_s)} in",
                tool_hint=True,
                usage=self._live_usage(context),
            )
            return
        base = self._tool_hint([tool_call])
        label = self._strip_think(base) or (name if isinstance(name, str) and name else "tool")
        await invoke_on_progress(
            self._on_progress,
            f"{label} - still running ({_format_elapsed(elapsed_s)})",
            tool_hint=True,
            usage=self._live_usage(context),
        )

    async def on_model_heartbeat(
        self,
        context: AgentHookContext,
        elapsed_s: float,
    ) -> None:
        """Say that the model request is still in flight, and for how long.

        Published on the non-tool-hint progress lane, which is the one every
        surface already renders as the "thinking / processing" state, so this
        needs no new event type and no client change. Deliberately NOT a tool
        hint: no tool is running, and reusing that lane would tell the user the
        agent was doing something it is not.

        The elapsed time is what makes it progress -- an unchanging string would
        be deduped by the UI and would look exactly as frozen as the state it is
        there to explain. Failure is handled by the caller.
        """
        if not self._on_progress:
            return
        await invoke_on_progress(
            self._on_progress,
            f"waiting for the model ({_format_elapsed(elapsed_s)})",
            usage=self._live_usage(context),
        )

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        """Publish a reasoning chunk; channel plugins decide whether to render."""
        if (
            self._on_progress
            and reasoning_content
            and self._on_progress_accepts(self._on_progress, "reasoning")
        ):
            self._reasoning_open = True
            await self._on_progress(reasoning_content, reasoning=True)

    async def emit_reasoning_end(self) -> None:
        """Close the current reasoning stream segment, if any was open."""
        if self._reasoning_open and self._on_progress:
            self._reasoning_open = False
            await self._on_progress("", reasoning_end=True)
        else:
            self._reasoning_open = False

    async def after_iteration(self, context: AgentHookContext) -> None:
        if (
            self._on_progress
            and context.tool_calls
            and context.tool_events
            and on_progress_accepts_tool_events(self._on_progress)
        ):
            tool_events = build_tool_event_finish_payloads(context)
            if tool_events:
                await invoke_on_progress(
                    self._on_progress,
                    "",
                    tool_hint=False,
                    tool_events=tool_events,
                    usage=self._live_usage(context),
                )
        u = context.usage or {}
        logger.debug(
            "LLM usage: prompt={} completion={} cached={}",
            u.get("prompt_tokens", 0),
            u.get("completion_tokens", 0),
            u.get("cached_tokens", 0),
        )

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return self._strip_think(content)
