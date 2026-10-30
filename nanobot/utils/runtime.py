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


def time_note_enabled() -> bool:
    """Whether each model request carries the elapsed/remaining time note.

    On by default. ``POWERX_TIME_NOTE=off`` turns it off, which is the switch
    to use for an A/B comparison or to roll back without a code change.
    """
    return os.environ.get("POWERX_TIME_NOTE", "on").strip().lower() != "off"


def turn_time_note(elapsed_s: float, budget_s: float) -> dict[str, str]:
    """A per-request note telling the model how much of the turn budget is gone.

    The note is built fresh for each request and appended to the request view
    only, never to the saved transcript, so it does not accumulate and does not
    break the cached prefix. It is the model's only clock: without it the model
    cannot tell a 2-minute turn from one that is 25 minutes in.
    """
    used_s = max(0.0, elapsed_s)
    left_s = max(0.0, budget_s - used_s)
    if left_s <= budget_s * 0.25:
        pacing = (
            "Less than a quarter of the time is left. Wrap up now: finish the "
            "step in progress, then give the user what is done and what remains."
        )
    else:
        pacing = (
            "Keep the remaining work to as few model steps as possible. Batch "
            "independent tool calls into one step, and do not spend a step on "
            "anything the user did not ask for."
        )
    return {
        "role": "user",
        "content": (
            f"[Time check] {used_s / 60.0:.0f} of {budget_s / 60.0:.0f} minutes used, "
            f"about {left_s / 60.0:.0f} minutes left in this turn. {pacing}"
        ),
    }


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


PROMISED_ACTION_PROMPT = (
    "Your last reply said what you were about to do, but it made no tool call, so "
    "nothing was executed and the turn has stopped with the work unfinished.\n\n"
    "Do the step you described now: make the tool call that performs it. If the work "
    "is already complete, reply with the result and do not announce further steps. "
    "Do not ask the user to restart or continue."
)


def build_promised_action_message() -> dict[str, str]:
    """Prompt the model to perform a step it announced but never called a tool for."""
    return {"role": "user", "content": PROMISED_ACTION_PROMPT}


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

#: How many of the most recent calls the cycle detector looks at. Nine is three
#: full cycles of a three-call loop; the two-call check needs only six of them.
#: Three cycles everywhere: a task that legitimately repeats a short sequence
#: once or twice (probe, read, probe) must not be stopped.
STUCK_WINDOW = 9


def stuck_pattern(recent: Sequence[str]) -> str | None:
    """Return a description when *recent* call identities are an unproductive loop.

    A consecutive-repeat guard is blind to a model that cycles through several
    calls: it never issues the same call twice in a row, yet the turn pays a
    full round trip per call and runs the same few things forever. Standard
    agent frameworks (OpenHands' stuck detector, LangGraph's
    ``StuckLoopDetection``) carry this check alongside the consecutive one.

    Checked for cycles of two and three distinct calls, each repeated three
    times in full (A,B,A,B,A,B and A,B,C,A,B,C,A,B,C). Returns a short reason,
    or None when nothing is stuck.
    """
    window = [item for item in recent][-STUCK_WINDOW:]
    for period in (2, 3):
        span = period * 3
        if len(window) < span:
            continue
        tail = window[-span:]
        block = tail[:period]
        if len(set(block)) != period:
            continue  # a cycle must be made of distinct calls
        if all(tail[i] == block[i % period] for i in range(span)):
            if period == 2:
                return "an alternating pair of calls repeated three times"
            return "a cycle of three calls repeated three times"
    return None


#: Phrases that ask the agent to throw away the attempt in progress and begin
#: again. Matched only in short messages, so a long instruction that happens to
#: contain "again" is never read as a restart.
_RESTART_RE = re.compile(
    r"\b(?:start|begin|restart|redo)\s+(?:all\s+)?over\b"
    r"|\b(?:start|begin)\s+(?:all\s+)?(?:again|afresh)\b"
    r"|\ball over again\b"
    r"|\bfrom (?:the )?(?:beginning|start|scratch)\b"
    r"|^\s*restart\s*[.!]*\s*$"
)
_RESTART_MAX_WORDS = 10


def is_restart_request(text: str) -> bool:
    """True when *text* is a short message asking the agent to start over."""
    cleaned = " ".join(text.lower().split())
    if not cleaned or len(cleaned.split()) > _RESTART_MAX_WORDS:
        return False
    return bool(_RESTART_RE.search(cleaned))


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
