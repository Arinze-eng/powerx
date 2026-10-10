"""Runtime-specific helper functions and constants."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Sequence, cast

from loguru import logger

from nanobot.utils.helpers import stringify_text_blocks

_MAX_REPEAT_EXTERNAL_LOOKUPS = 2

# Third same-target workspace violation in a turn escalates to "stop retrying".
_MAX_REPEAT_WORKSPACE_VIOLATIONS = 2

#: How many times one call, spelled the same way, may run before the agent
#: refuses it. Three: the first two give a model that is genuinely retrying a
#: fair chance, and by the third the call has demonstrably not changed anything.
MAX_IDENTICAL_TOOL_CALLS = 2

#: Wall-clock ceiling for a single turn, in seconds.
#:
#: ``max_iterations`` bounds a turn by COUNT, and a count is the wrong unit for
#: the thing a user actually experiences. 200 iterations of a fast read-only
#: lookup is seconds; 200 iterations of a browser session, a sandbox build or a
#: video edit is hours, because each iteration's cost is set by the TOOL, not by
#: the loop. MEASURED in production: one turn ran 119 consecutive browser calls
#: over 400 s of pure round-trip time, and the reported video-edit turn ran past
#: two hours -- both well inside a 200-iteration budget. The iteration count
#: never fired, because it was never the binding constraint.
#:
#: This bounds the turn by TIME instead, and it is enforced in the loop, not in
#: a provider client, so it applies to whatever model the admin configured.
#: Crossing it does not kill the turn: the runner makes one final no-tools call
#: and answers with what it already has, exactly as it does at the iteration
#: ceiling. Default 1800 s (30 min) -- long enough that a genuinely long task
#: finishes, short enough that "2h+" cannot happen. Override per deployment with
#: ``NANOBOT_TURN_BUDGET_S``; a value of ``0`` disables the budget.
TURN_BUDGET_SECONDS = 1800.0

_LENGTH_RECOVERY_TAIL_CHARS = 64

EMPTY_FINAL_RESPONSE_MESSAGE = (
    "I completed the tool steps but couldn't produce a final answer. "
    "Please try again or narrow the task."
)

FINALIZATION_RETRY_PROMPT = (
    "Please provide your response to the user based on the conversation above."
)

BUDGET_EXHAUSTED_FINALIZATION_PROMPT = (
    "The tool-call budget for this turn is exhausted. Based only on the "
    "conversation and tool results above, provide a concise final response to "
    "the user. Do not call or request tools. Do not claim the task is complete "
    "unless the evidence above clearly shows it is complete. State what was "
    "done, what remains, and the best next step if anything is incomplete."
)

LENGTH_RECOVERY_PROMPT = (
    "The previous assistant response was cut off. Continue the same response from its "
    "exact endpoint. Output only new continuation text in the same language and style. "
    "Do not acknowledge this instruction, restart the response, repeat its title or any "
    "existing text, recap, or apologize."
)

SUSTAINED_GOAL_CONTINUE_PROMPT = (
    "You have an active sustained goal. Please continue working toward the "
    "objective using your tools, or call update_goal with action='complete' "
    "if the work is truly finished."
)


def turn_budget_seconds() -> float:
    """Return the effective per-turn wall-clock budget in seconds.

    Precedence: ``NANOBOT_TURN_BUDGET_S`` env override, else
    :data:`TURN_BUDGET_SECONDS`. The module global is the fallback rather than a
    literal so tests can monkeypatch one knob.

    An explicit ``0`` (or any non-positive value) disables the budget -- the
    documented opt-out for an operator who wants a turn to run unbounded. An
    unparseable value is ignored with a warning and the default is used, so a
    typo cannot silently remove the ceiling.
    """
    raw = os.environ.get("NANOBOT_TURN_BUDGET_S")
    if raw is None or not raw.strip():
        return TURN_BUDGET_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring invalid NANOBOT_TURN_BUDGET_S={!r}; using {}",
            raw,
            TURN_BUDGET_SECONDS,
        )
        return TURN_BUDGET_SECONDS
    return value if value > 0 else 0.0


def empty_tool_result_message(tool_name: str) -> str:
    """Short prompt-safe marker for tools that completed without visible output."""
    return f"({tool_name} completed with no output)"


def ensure_nonempty_tool_result(tool_name: str, content: Any) -> Any:
    """Replace semantically empty tool results with a short marker string."""
    if content is None:
        return empty_tool_result_message(tool_name)
    if isinstance(content, str) and not content.strip():
        return empty_tool_result_message(tool_name)
    if isinstance(content, list):
        if not content:
            return empty_tool_result_message(tool_name)
        text_payload = stringify_text_blocks(cast(list[Any], content))
        if text_payload is not None and not text_payload.strip():
            return empty_tool_result_message(tool_name)
    return cast(Any, content)


def is_blank_text(content: str | None) -> bool:
    """True when *content* is missing or only whitespace."""
    return content is None or not content.strip()


def build_finalization_retry_message() -> dict[str, str]:
    """A short no-tools-allowed prompt for final answer recovery."""
    return {"role": "user", "content": FINALIZATION_RETRY_PROMPT}


def build_budget_exhausted_finalization_message() -> dict[str, str]:
    """Prompt the model for a no-tools final response after budget exhaustion."""
    return {"role": "user", "content": BUDGET_EXHAUSTED_FINALIZATION_PROMPT}


def build_length_recovery_message(content: str) -> dict[str, str]:
    """Prompt the model to continue after hitting output token limit."""
    tail = content[-_LENGTH_RECOVERY_TAIL_CHARS:]
    prompt = (
        f"{LENGTH_RECOVERY_PROMPT}\n\n"
        "The following tail was already delivered to the user. Treat it as immutable "
        "context and do not output it again:\n"
        "<already_delivered_tail>\n"
        f"{tail}\n"
        "</already_delivered_tail>\n"
        "Begin with the text that belongs immediately after this tail."
    )
    return {"role": "user", "content": prompt}


TRUNCATED_TOOL_CALL_PROMPT = (
    "The previous assistant response hit the output token limit while it was still "
    "writing a tool call, so that call was discarded and NOTHING was executed -- the "
    "workspace and the conversation are unchanged. Asking for the same call again "
    "would be cut off in the same place.\n\n"
    "Re-issue the work so that a single response fits inside the limit:\n"
    "- Large files: write them in sections. Create the file with its first section, "
    "then extend it with further calls, instead of emitting the whole file at once.\n"
    "- Batch less: split unrelated edits or files across separate tool calls rather "
    "than combining them into one response.\n"
    "Keep the same paths and continue exactly where the discarded call left off. Do "
    "not explain this to the user or repeat work that already succeeded."
)


def build_truncated_tool_call_message(tool_name: str | None = None) -> dict[str, str]:
    """Prompt the model to re-issue a tool call that the output limit truncated.

    The call never ran, so there is no tool result to react to and no partial
    text to continue -- the only useful next move is the same work, expressed so
    that one response can hold it.
    """
    if tool_name:
        prefix = f"Your `{tool_name}` call was cut off before it could run."
    else:
        prefix = "Your tool call was cut off before it could run."
    return {"role": "user", "content": f"{prefix} {TRUNCATED_TOOL_CALL_PROMPT}"}


def build_goal_continue_message(custom: str | None = None) -> dict[str, str]:
    """Prompt the model to continue when a sustained goal is still active."""
    return {"role": "user", "content": custom or SUSTAINED_GOAL_CONTINUE_PROMPT}


def external_lookup_signature(tool_name: str, arguments: Any) -> str | None:
    """Stable signature for repeated external lookups we want to throttle."""
    if not isinstance(arguments, dict):
        return None
    arguments = cast(dict[str, Any], arguments)
    if tool_name == "web_fetch":
        url = str(arguments.get("url") or "").strip()
        if url:
            return f"web_fetch:{url.lower()}"
    if tool_name == "web_search":
        query = str(arguments.get("query") or arguments.get("search_term") or "").strip()
        if query:
            return f"web_search:{query.lower()}"
    return None


#: Argument values that mean "not supplied". An explicit empty string, an empty
#: list and a null are the same request as a key that was left out entirely.
_EMPTY_ARGUMENT_VALUES: tuple[Any, ...] = (None, "", [], {})

#: How many of the most recent calls the alternation detector looks at, and how
#: many full cycles of the pair it needs before calling it a loop. Six calls is
#: three cycles of A, B -- high confidence, and deliberately more than the four
#: calls a minimal A,B,A,B rule would need, because an agent legitimately
#: alternating two probes early in a task must not be stopped.
STUCK_WINDOW = 6


def stuck_pattern(recent: Sequence[str]) -> str | None:
    """Return a description when *recent* call identities are an unproductive loop.

    Catches the shape a consecutive-repeat guard is blind to: **alternation**.
    A model that re-issues A, B, A, B never repeats a call twice in a row, so a
    guard that only compares against the previous call never fires -- while the
    turn pays a full round trip per call and executes the same two things
    forever. Standard agent frameworks (OpenHands' stuck detector, LangGraph's
    ``StuckLoopDetection``) all carry this check alongside the consecutive one;
    a consecutive-only guard is the gap that produced the measured 119-iteration
    turn this module exists to bound.

    Three full cycles (A, B, A, B, A, B) are required rather than the minimum
    four calls, so a task that legitimately alternates two probes once or twice
    is untouched. Returns a short reason, or None when nothing is stuck.
    """
    window = [item for item in recent][-STUCK_WINDOW:]
    if len(window) < STUCK_WINDOW:
        return None
    even = window[0::2]
    odd = window[1::2]
    if len(set(even)) == 1 and len(set(odd)) == 1 and even[0] != odd[0]:
        return "an alternating pair of calls repeated three times"
    return None


def normalize_tool_arguments(tool_name: str, arguments: Any) -> dict[str, Any]:
    """Return *arguments* in the canonical spelling used to identify a call.

    Two calls that ask for the same thing must normalize to the same mapping.
    Without this the repeat guard compares *spellings* instead of intents, and a
    model that only reformats its arguments -- ``{"url": X}`` one step,
    ``{"url": X, "target": null}`` the next -- escapes it forever while issuing
    the same request.

    Normalization drops keys that were supplied empty (an omitted optional and an
    explicit null are one request) and strips surrounding whitespace from string
    values. It never reorders meaning: values that differ in substance stay
    different, so a genuinely different call is still a different call.
    """
    if not isinstance(arguments, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key, value in cast(dict[str, Any], arguments).items():
        if isinstance(value, str):
            value = value.strip()
        if value in _EMPTY_ARGUMENT_VALUES:
            continue
        normalized[str(key)] = value
    return normalized


def repeated_external_lookup_error(
    tool_name: str,
    arguments: Any,
    seen_counts: dict[str, int],
) -> str | None:
    """Block repeated external lookups after a small retry budget."""
    signature = external_lookup_signature(tool_name, arguments)
    if signature is None:
        return None
    count = seen_counts.get(signature, 0) + 1
    seen_counts[signature] = count
    if count <= _MAX_REPEAT_EXTERNAL_LOOKUPS:
        return None
    logger.warning(
        "Blocking repeated external lookup {} on attempt {}",
        signature[:160],
        count,
    )
    return (
        "Error: repeated external lookup blocked. "
        "Use the results you already have to answer, or try a meaningfully different source."
    )


# Workspace-boundary violations are soft errors, with per-target throttling.

_OUTSIDE_PATH_PATTERN = re.compile(r"(?:^|[\s|>'\"])((?:/[^\s\"'>;|<]+)|(?:~[^\s\"'>;|<]+))")


def workspace_violation_signature(
    tool_name: str,
    arguments: Any,
) -> str | None:
    """Return a stable cross-tool signature for the outside-workspace target."""
    if not isinstance(arguments, dict):
        return None
    arguments = cast(dict[str, Any], arguments)
    for key in ("path", "file_path", "target", "source", "destination"):
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            return _normalize_violation_target(val.strip())

    if tool_name in {"exec", "shell"}:
        cmd = str(arguments.get("command") or "").strip()
        if cmd:
            match = _OUTSIDE_PATH_PATTERN.search(cmd)
            if match:
                return _normalize_violation_target(match.group(1))
        cwd = str(arguments.get("working_dir") or "").strip()
        if cwd:
            return _normalize_violation_target(cwd)

    return None


def _normalize_violation_target(raw: str) -> str:
    """Normalize *raw* path so that equivalent spellings collide on the same key."""
    try:
        normalized = Path(raw).expanduser().resolve().as_posix()
    except Exception:
        normalized = raw.replace("\\", "/")
    return f"violation:{normalized}".lower()


def repeated_workspace_violation_error(
    tool_name: str,
    arguments: Any,
    seen_counts: dict[str, int],
) -> str | None:
    """Return an escalated error after repeated bypass attempts."""
    signature = workspace_violation_signature(tool_name, arguments)
    if signature is None:
        return None
    count = seen_counts.get(signature, 0) + 1
    seen_counts[signature] = count
    if count <= _MAX_REPEAT_WORKSPACE_VIOLATIONS:
        return None
    logger.warning(
        "Escalating repeated workspace bypass attempt {} (attempt {})",
        signature[:160],
        count,
    )
    target = signature.split("violation:", 1)[1] if "violation:" in signature else signature
    return (
        "Error: refusing repeated workspace-bypass attempts.\n"
        f"You have tried to access '{target}' (or an equivalent path) "
        f"{count} times in this turn. This is a hard policy boundary -- "
        "switching tools, shell tricks, working_dir overrides, symlinks, "
        "or base64 piping will NOT change the answer. Stop retrying. "
        "If the user genuinely needs this resource, tell them you cannot "
        "access it and ask how they want to proceed (e.g. copy the file "
        "into the workspace, or disable restrict_to_workspace for this run)."
    )
