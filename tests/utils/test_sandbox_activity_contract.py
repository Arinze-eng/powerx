"""The wire contract the Live screen's terminal feed is built on.

The feed does not read the sandbox — it folds the agent's own tool calls out of
the progress events the runner already broadcasts, which is what makes it work
identically on Novita, Runloop, Daytona, a VPS and a laptop. That choice moves
the whole feature onto one assumption: **every tool call arrives with its name,
its arguments and its call id.**

That assumption is invisible from the WebUI, so it is asserted here from the
producer's side. If a future change stops populating ``arguments``, or drops the
``call_id`` that joins a start frame to its end frame, the panel does not error —
it silently empties, which is indistinguishable from "the agent did nothing".
These tests make that show up as a failure instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nanobot.agent.hook import AgentHookContext  # noqa: F401 - imported first on purpose
from nanobot.providers.base import ToolCallRequest
from nanobot.utils.progress_events import (
    build_tool_event_finish_payloads,
    build_tool_event_start_payload,
)


@dataclass
class _HookContext:
    """The slice of ``AgentHookContext`` the finish payload reads."""

    tool_calls: list[ToolCallRequest]
    tool_results: list[Any]
    tool_events: list[dict[str, str]]


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments)


def test_a_shell_command_arrives_as_a_start_payload_the_feed_can_render() -> None:
    """``exec`` must reach the wire with the command itself, not just its name."""
    payload = build_tool_event_start_payload(
        _call("c1", "exec", {"command": "ls -la /workspace"})
    )
    assert payload["phase"] == "start"
    assert payload["name"] == "exec"
    assert payload["call_id"] == "c1"
    assert payload["arguments"] == {"command": "ls -la /workspace"}


def test_an_mt5_order_arrives_with_the_fields_the_feed_classifies_on() -> None:
    """A trade row needs the action and the side/symbol/volume to be named.

    Without ``action`` the feed cannot tell an order from a quote, so every line
    would render as a neutral sandbox action and a live order would be as easy to
    miss in the feed as it is on the desktop — the exact defect being fixed.
    """
    payload = build_tool_event_start_payload(
        _call(
            "c2",
            "mt5_sandbox",
            {"action": "order", "side": "buy", "symbol": "EURUSD", "volume": 0.01},
        )
    )
    arguments = payload["arguments"]
    assert arguments["action"] == "order"
    assert arguments["side"] == "buy"
    assert arguments["symbol"] == "EURUSD"
    assert arguments["volume"] == 0.01


def test_the_call_id_joins_a_start_frame_to_its_finish_frame() -> None:
    """The feed closes a row by matching ``call_id``; a mismatch leaves it running.

    A row stuck on "running" forever is worse than no row: it claims a command is
    still executing when it finished minutes ago.
    """
    call = _call("c3", "exec", {"command": "make"})
    context = _HookContext(
        tool_calls=[call],
        tool_results=["done"],
        tool_events=[{"status": "ok"}],
    )
    start = build_tool_event_start_payload(call)
    finishes = build_tool_event_finish_payloads(context)  # type: ignore[arg-type]

    assert len(finishes) == 1
    assert finishes[0]["call_id"] == start["call_id"] == "c3"
    assert finishes[0]["phase"] == "end"


def test_a_failed_command_finishes_as_an_error_frame_with_a_reason() -> None:
    """The feed colours a failure from ``phase`` and shows ``error`` as its detail."""
    call = _call("c4", "exec", {"command": "make"})
    context = _HookContext(
        tool_calls=[call],
        tool_results=["Error: No such file: Makefile"],
        tool_events=[{"status": "error", "detail": "boom"}],
    )
    finishes = build_tool_event_finish_payloads(context)  # type: ignore[arg-type]

    assert finishes[0]["phase"] == "error"
    assert "Makefile" in finishes[0]["error"]


def test_arguments_survive_a_tool_call_that_carries_none() -> None:
    """A call with no arguments must still produce a payload, not raise.

    Lifecycle tools dispatch without arguments; the runner emits events for every
    call in the batch, so a raise here would take the whole iteration's progress
    with it.
    """
    payload = build_tool_event_start_payload(_call("c5", "exec", {}))
    assert payload["arguments"] == {}
