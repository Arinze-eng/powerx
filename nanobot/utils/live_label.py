"""A live label for a tool call that is busy WATCHING something.

WHY THIS EXISTS: a long-running tool is invisible while it runs. The runner
already narrates the wait -- ``AgentProgressHook.on_tool_heartbeat`` fires every
few seconds with "reading X - still running (24s)" -- but that says only that
*something* is happening. For a tool whose whole job is to observe a market over
time, "still running" is exactly the wrong message: the user asked to see the
trading being watched, and a spinner that never changes its text cannot show
watching, only waiting.

So a tool that observes publishes what it is CURRENTLY seeing here, and the
heartbeat carries that instead of a generic label. The result is a progress line
that evolves while the tool blocks --:

    watching XAUUSD #41261482 buy 0.1 - bid 4167.30, +0.42R, guard live (24s)

The elapsed time stays on the end (see the hook: it is what stops the UI from
de-duplicating an unchanged string), and the observation in front is the newest
one the tool actually read.

Scope and honesty:

* A label is a STATEMENT ABOUT THE LAST FRAME, never a prediction. A tool must
  only publish a number it has read, and must say when it is between frames
  ("reading a 20 s frame"), so the line can never claim an observation it does
  not have.
* Labels go stale. ``live_label_for`` refuses one older than ``max_age_s``, so a
  crashed or hung tool cannot leave a frozen observation on screen forever.
* Keyed by tool name, because that is what the heartbeat knows. Two concurrent
  calls of the SAME tool (the runner can batch them) will therefore share one
  label -- the last publisher wins. That is a limitation, not a bug: the label is
  a status line, and the authoritative answer is always the tool's returned
  result.
"""

from __future__ import annotations

import threading
import time
from typing import Any

__all__ = [
    "publish_live_label",
    "clear_live_label",
    "live_label_for",
    "live_labels",
]

#: How long a label stays usable by default. Comfortably longer than the tool
#: heartbeat interval (8 s by default), so a healthy label is never dropped
#: between beats, and short enough that a dead tool stops narrating quickly.
DEFAULT_LIVE_LABEL_MAX_AGE_S = 30.0

#: A progress line is a hint, not a report: every surface renders it on one line
#: next to the tool name, so a long one is truncated by the UI anyway. The cap
#: keeps the interesting part (the number) from being the part that gets cut.
MAX_LIVE_LABEL_CHARS = 140

_LOCK = threading.Lock()
_LABELS: dict[str, tuple[str, float]] = {}


def publish_live_label(name: Any, text: Any, *, at: float | None = None) -> str | None:
    """Record ``text`` as the live label for tool ``name``; returns what landed.

    Returns the stored label (possibly trimmed) or ``None`` when there was
    nothing usable to store, so a caller can log exactly what it published.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(text, str):
        return None
    cleaned = " ".join(text.split())
    if not cleaned:
        return None
    if len(cleaned) > MAX_LIVE_LABEL_CHARS:
        cleaned = cleaned[: MAX_LIVE_LABEL_CHARS - 1].rstrip() + "…"
    with _LOCK:
        _LABELS[name] = (cleaned, time.monotonic() if at is None else float(at))
    return cleaned


def clear_live_label(name: Any) -> None:
    """Forget ``name``'s label. Called when the call that published it ends."""
    if not isinstance(name, str):
        return
    with _LOCK:
        _LABELS.pop(name, None)


def live_label_for(
    name: Any,
    *,
    max_age_s: float = DEFAULT_LIVE_LABEL_MAX_AGE_S,
    now: float | None = None,
) -> str | None:
    """The label for ``name``, or ``None`` if there is none or it has gone stale.

    Staleness is measured on a monotonic clock, so a system clock change cannot
    make a fresh label look old (or a dead one look alive).
    """
    if not isinstance(name, str):
        return None
    with _LOCK:
        row = _LABELS.get(name)
    if row is None:
        return None
    text, stamp = row
    current = time.monotonic() if now is None else float(now)
    if max_age_s is not None and current - stamp > float(max_age_s):
        return None
    return text


def live_labels(*, max_age_s: float = DEFAULT_LIVE_LABEL_MAX_AGE_S) -> dict[str, str]:
    """Every fresh label, for tests and diagnostics."""
    with _LOCK:
        rows = dict(_LABELS)
    current = time.monotonic()
    return {
        name: text
        for name, (text, stamp) in rows.items()
        if max_age_s is None or current - stamp <= float(max_age_s)
    }
