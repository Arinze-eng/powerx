"""Bounded task-mode policy for Telegram turns.

This module deliberately uses conservative deterministic signals instead of an
extra classifier call. The model receives planning guidance as runtime context,
while the user's original message and public history remain unchanged.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from nanobot.runtime_context import RuntimeContextBlock, wrap_runtime_context_lines

DELIBERATE_TASK_META = "telegram_deliberate_task"
DELIBERATE_ROUNDS_META = "_telegram_deliberate_rounds"
DELIBERATE_TASK_KIND = "telegram_deliberate"
DELIBERATE_CONTEXT_SOURCE = "telegram-deliberate"

# A normal question should stay fast. These are verbs that usually imply work
# rather than an answer-only request.
_ACTION_RE = re.compile(
    r"\b(?:build|create|implement|fix|debug|diagnose|deploy|redeploy|test|verify|"
    r"integrate|configure|migrate|refactor|automate|install|setup|set up|update|"
    r"modify|change|rewrite|analy[sz]e|inspect|research|generate|upload|download|"
    r"commit|push|publish|run|execute|review|design|develop)\b",
    re.IGNORECASE,
)
_COMPLEXITY_RE = re.compile(
    r"\b(?:and then|after that|before|first|next|finally|step|steps|multiple|"
    r"end[- ]to[- ]end|long[- ]running|reasonable time|entire|whole|repository|"
    r"repo|code|file|files|sandbox|cicd|ci/cd|pipeline|database|api|render|"
    r"supabase|telegram|github|production|local|server|service|integration)\b",
    re.IGNORECASE,
)


def is_deliberate_telegram_task(content: str, media: list[str] | None = None) -> bool:
    """Return whether a Telegram turn warrants planning and bounded continuation."""
    if media:
        return True
    text = " ".join((content or "").split()).strip()
    if not text or text.startswith("/"):
        return False
    words = re.findall(r"\b\w+[\w/-]*\b", text)
    action = bool(_ACTION_RE.search(text))
    complexity = bool(_COMPLEXITY_RE.search(text))
    if action and complexity:
        return True
    if action and len(words) >= 24:
        return True
    return len(text) >= 320 and action


def deliberate_task_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    content: str,
    media: list[str] | None = None,
) -> dict[str, Any]:
    """Copy metadata and mark complex Telegram turns for deliberate execution."""
    result = dict(metadata or {})
    if is_deliberate_telegram_task(content, media):
        result[DELIBERATE_TASK_META] = True
        result.setdefault("telegram_task_kind", DELIBERATE_TASK_KIND)
    return result


def is_deliberate_turn(
    session_metadata: Mapping[str, Any] | None,
    *,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether this turn is an automatically deliberate Telegram turn."""
    for source in (message_metadata, session_metadata):
        if source and source.get(DELIBERATE_TASK_META) is True:
            return True
    return False


def deliberate_runtime_context() -> RuntimeContextBlock:
    """Return model-only guidance for careful, visible, bounded execution."""
    return RuntimeContextBlock(
        source=DELIBERATE_CONTEXT_SOURCE,
        content=wrap_runtime_context_lines([
            "Deliberate execution mode is active for this complex Telegram task.",
            "Before the first tool call, form a concise private plan with milestones and success checks; do not reveal hidden chain-of-thought.",
            "Work one smallest safe step at a time. After each meaningful tool result, verify it and update the user with a short milestone when useful.",
            "Do not repeat an unchanged tool call. If a step fails, inspect the error and change the approach.",
            "Use the available execution budget for a complete, verified result instead of rushing. If blocked, explain the blocker and ask only for the missing input.",
            "Finish only after checking the real result. Keep progress summaries concise and separate from the final answer.",
        ]),
    )


__all__ = [
    "DELIBERATE_CONTEXT_SOURCE",
    "DELIBERATE_ROUNDS_META",
    "DELIBERATE_TASK_KIND",
    "DELIBERATE_TASK_META",
    "deliberate_runtime_context",
    "deliberate_task_metadata",
    "is_deliberate_telegram_task",
    "is_deliberate_turn",
]
