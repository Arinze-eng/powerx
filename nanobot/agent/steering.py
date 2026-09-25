"""Mid-run steering for the agent loop.

Ported semantics from CowAgent's ``agent/protocol/agent_stream.py``:

* A steer that arrives while the model is streaming **takes precedence over**
  the continuation the model just proposed. Tool calls the model already
  emitted are closed with synthetic results before the model is asked to
  reconsider, so history stays valid.
* Steers are drained at the top of every iteration, and again *between* tool
  calls within one iteration, so a long tool batch can be abandoned early.
* Abandoned tool calls are closed synthetically: OpenAI/Anthropic-style
  transcripts require every ``tool_call`` to be followed by a ``tool`` result,
  and a dangling call poisons every later turn.

This module is dependency-free on purpose: it is imported by ``runner.py`` and
must not pull in provider or session machinery.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

#: Cap on how many steer messages a single turn will absorb. Beyond this the
#: runs would never converge; the remainder is left in the inbox untouched.
MAX_STEERS_PER_TURN = 3

#: Cap on how many times one run may be steered overall.
MAX_STEER_CYCLES = 5

#: Cap on a single steer's length, so a paste cannot blow the context window.
MAX_STEER_CHARS = 4_000


def _truncate(text: str, limit: int = MAX_STEER_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[Steering message truncated: {len(text)} chars total]"


@dataclass
class SteeringUpdate:
    """One user steer, normalised."""

    text: str
    source: str = "user"

    def to_message(self) -> dict[str, Any]:
        """Render as a user message for the transcript.

        The steering wrapper is explicit so the model understands this is a
        mid-run correction rather than ordinary conversation, and so an auditor
        reading the transcript can tell the two apart.
        """
        return {
            "role": "user",
            "content": (
                "[Steering update — the user interrupted the run. "
                "Treat the following as the current instruction and adjust "
                "your plan accordingly. Do not repeat work already completed.]\n\n"
                f"{self.text}"
            ),
        }


@dataclass
class SteeringInbox:
    """A thread-safe, async-safe queue of pending steers for one run.

    Concurrency notes: pushes arrive from channel/web threads while the loop
    drains from the event loop, so every operation is guarded by a lock. The
    lock is never held across an ``await``, so it cannot deadlock the loop.
    """

    updates: list[SteeringUpdate] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _closed: bool = False

    def push(self, text: str, *, source: str = "user") -> bool:
        """Queue a steer. Returns False for empty input or a closed inbox."""
        text = (text or "").strip()
        if not text:
            return False
        with self._lock:
            if self._closed:
                return False
            self.updates.append(SteeringUpdate(text=_truncate(text), source=source))
            return True

    def pending(self) -> int:
        with self._lock:
            return len(self.updates)

    def has_pending(self) -> bool:
        return self.pending() > 0

    def drain(self, *, limit: int = MAX_STEERS_PER_TURN) -> list[SteeringUpdate]:
        """Take up to *limit* steers, leaving the rest queued."""
        with self._lock:
            taken = self.updates[:limit]
            del self.updates[:limit]
            return taken

    def close(self) -> None:
        """Refuse further steers (the run is finishing)."""
        with self._lock:
            self._closed = True

    def close_if_empty(self) -> bool:
        """Close the inbox if nothing is pending. Returns True when closed.

        Mirrors CowAgent's ``close_if_empty``: once the run is wrapping up and
        no steer is waiting, late arrivals should not resurrect it.
        """
        with self._lock:
            if self.updates:
                return False
            self._closed = True
            return True

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed


def build_steering_messages(updates: list[SteeringUpdate]) -> list[dict[str, Any]]:
    """Turn drained steers into transcript messages (one per steer)."""
    return [update.to_message() for update in updates]


def synthetic_tool_result_message(
    tool_call: Any,
    *,
    reason: str = "Abandoned: the user steered the run in a new direction.",
) -> dict[str, Any]:
    """A ``tool`` message closing a call that will not be executed.

    Every emitted ``tool_call`` must be answered, or provider-side validation
    rejects the transcript. This builds the minimal valid answer.
    """
    tool_call_id = getattr(tool_call, "id", None)
    if tool_call_id is None and isinstance(tool_call, dict):
        tool_call_id = tool_call.get("id")
    name = getattr(tool_call, "name", None)
    if name is None and isinstance(tool_call, dict):
        name = tool_call.get("name")
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": name,
        "content": reason,
    }


def close_pending_tool_calls(
    messages: list[dict[str, Any]],
    tool_calls: list[Any],
    *,
    reason: str = "Abandoned: the user steered the run in a new direction.",
) -> int:
    """Append synthetic tool results for *tool_calls*. Returns how many closed."""
    closed = 0
    for tool_call in tool_calls:
        messages.append(synthetic_tool_result_message(tool_call, reason=reason))
        closed += 1
    return closed


class SteeringConflict(Exception):
    """Raised when a steer cannot be honoured without corrupting history."""


def should_honour_steer(inbox: SteeringInbox | None, cycles: int) -> bool:
    """Whether a pending steer may still be honoured this run."""
    if inbox is None or cycles >= MAX_STEER_CYCLES:
        return False
    return inbox.has_pending()