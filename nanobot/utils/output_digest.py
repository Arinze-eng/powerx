"""Helpers for condensing long command output into a load-bearing summary line.

Originally part of the ``sandbox_batch`` spill storage; kept standalone so the
context-governance decay pass (and anything else that summarises stale tool
output) can collapse old results without pulling in batch machinery.
"""

from __future__ import annotations

import re

_EXIT_RE = re.compile(r"\[exit=(\d+)\]")

#: Lines that carry no information (pytest progress dots, npm spinners,
#: pip bars) and would otherwise crowd out the real summary.
_NOISE_RE = re.compile(r"^[\s.*=_#\-]*$|\.{3,}\s*$|%\s*$")


def extract_exit_code(text: str) -> int | None:
    """Pull the backend's trailing ``[exit=N]`` marker out of command output."""
    matches = _EXIT_RE.findall(text)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except (TypeError, ValueError):
        return None


def informative_line(line: str) -> bool:
    """Whether *line* carries signal worth keeping in a condensed summary."""
    stripped = line.strip()
    if not stripped or len(stripped) < 3:
        return False
    return not _NOISE_RE.match(stripped)
