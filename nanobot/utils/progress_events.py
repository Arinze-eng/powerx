"""Structured progress-event helpers shared by agent runtimes."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from nanobot.agent import activity
from nanobot.agent.hook import AgentHookContext


def on_progress_accepts_tool_events(cb: Callable[..., Any]) -> bool:
    return _on_progress_accepts(cb, "tool_events")


def on_progress_accepts_usage(cb: Callable[..., Any]) -> bool:
    return _on_progress_accepts(cb, "usage")


def on_progress_accepts_file_edit_events(cb: Callable[..., Any]) -> bool:
    return _on_progress_accepts(cb, "file_edit_events")


def _on_progress_accepts(cb: Callable[..., Any], name: str) -> bool:
    try:
        sig = inspect.signature(cb)
    except (TypeError, ValueError):
        return False
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return True
    return name in sig.parameters


async def invoke_on_progress(
    on_progress: Callable[..., Awaitable[None]],
    content: str,
    *,
    tool_hint: bool = False,
    tool_events: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
) -> None:
    if tool_events and on_progress_accepts_tool_events(on_progress):
        if usage is not None and on_progress_accepts_usage(on_progress):
            await on_progress(
                content,
                tool_hint=tool_hint,
                tool_events=tool_events,
                usage=usage,
            )
            return
        await on_progress(content, tool_hint=tool_hint, tool_events=tool_events)
        return
    await on_progress(content, tool_hint=tool_hint)


async def invoke_file_edit_progress(
    on_progress: Callable[..., Awaitable[None]],
    file_edit_events: list[dict[str, Any]],
) -> None:
    if not file_edit_events or not on_progress_accepts_file_edit_events(on_progress):
        return
    await on_progress("", file_edit_events=file_edit_events)


def _tool_event_activity(tool_call: Any) -> tuple[dict[str, Any], str | None]:
    """The arguments a tool event carries, and what kind of row it makes.

    One function because both answers must come from the same raw arguments:
    classification reads which keys are present, and the bounded copy has already
    dropped some of them.

    Tool events reach every WebUI client and are persisted in the transcript, so a
    shell command's token or a browser's form password would be durable and
    broadcast. `nanobot.agent.activity` decides what survives; every other tool is
    passed through untouched, because the chat transcript renders arguments for its
    own reasons and this is not the place to second-guess it.

    `kind` is decided host-side for the same reason the tool set is: the WebUI
    cannot see which tools exist. It ships to clients that cannot be redeployed in
    step with this server, so a tool name checked in TypeScript is a tool that
    silently stops appearing in the feed the moment it is added or renamed. Here it
    is a row the client renders and never a name it matches on.
    """
    raw = getattr(tool_call, "arguments", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    name = str(getattr(tool_call, "name", "") or "")
    return (
        cast(dict[str, Any], activity.tool_event_arguments(name, raw)),
        activity.classify_kind(name, raw),
    )


def build_tool_event_start_payload(tool_call: Any) -> dict[str, Any]:
    arguments, kind = _tool_event_activity(tool_call)
    return {
        "version": 1,
        "phase": "start",
        "call_id": str(getattr(tool_call, "id", "") or ""),
        "name": getattr(tool_call, "name", ""),
        "arguments": arguments,
        "result": None,
        "error": None,
        # A started call has no outcome yet. Stated rather than omitted so a
        # client can read one shape for every frame.
        "outcome": None,
        # `None` means the live feed has no row for this call. A value rather than
        # an omission, so "this call is not for the feed" is a fact the producer
        # states and not one the client infers from a tool-name set it cannot keep
        # current.
        "kind": kind,
        "files": [],
        "embeds": [],
    }


def tool_event_result_extras(result: Any) -> tuple[list[Any], list[Any]]:
    if not isinstance(result, dict):
        return [], []
    result_data = cast(dict[str, Any], result)
    raw_files = result_data.get("files")
    raw_embeds = result_data.get("embeds")
    files: list[Any] = cast(list[Any], raw_files) if isinstance(raw_files, list) else []
    embeds: list[Any] = cast(list[Any], raw_embeds) if isinstance(raw_embeds, list) else []
    return files, embeds


def build_tool_event_finish_payloads(context: AgentHookContext) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    count = min(len(context.tool_calls), len(context.tool_results), len(context.tool_events))
    for idx in range(count):
        tool_call = context.tool_calls[idx]
        result = context.tool_results[idx]
        event = context.tool_events[idx]
        status = event.get("status")
        phase = "end" if status == "ok" else "error"
        files, embeds = tool_event_result_extras(result)
        arguments, kind = _tool_event_activity(tool_call)
        payload = {
            "version": 1,
            "phase": phase,
            "call_id": str(getattr(tool_call, "id", "") or ""),
            "name": getattr(tool_call, "name", ""),
            "arguments": arguments,
            "result": result if phase == "end" else None,
            "error": None,
            "kind": kind,
            "files": files,
            "embeds": embeds,
        }
        if phase == "error":
            if isinstance(result, str) and result.strip():
                payload["error"] = result.strip()
            else:
                payload["error"] = str(event.get("detail") or "Tool execution failed")
        # "It failed" and "a guard said no" are different facts. The MT5 live-trading
        # gate, the workspace path guard and an unconfigured sandbox all produce a
        # result that reads like an error and is a working safety control, so the
        # producer names the outcome instead of leaving it to be guessed at.
        payload["outcome"] = activity.classify_outcome(phase, payload.get("error"))
        payloads.append(payload)
    return payloads
