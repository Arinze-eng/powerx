"""Deterministic task-SHAPE router: the Lever-A steering layer.

Why this exists
---------------
The runner's Re-Act loop pays one provider call per decision.  ``run_plan`` and
``python_code`` already exist and can execute N steps per call, but they are only
*used* when the model happens to choose them.  For loop-shaped asks ("for each of
these files, ...", "rename all the .txt files") the model tends to walk the loop
itself, one call per iteration -- which is precisely the cost profile we are
trying to remove.

This module makes that choice deterministic and free.  It classifies the *shape*
of a task with regexes only (no LLM call to decide routing -- that would defeat
the entire purpose) and, when the shape is batch/chained, emits ONE short
steering message that tells the model to express the whole job as a single
``run_plan`` (or ``python_code``) call.

Relationship to the existing routers
------------------------------------
``deterministic_router`` and ``task_router`` *answer* a turn with zero calls.
This module never answers anything: it cannot, because it does not know the
answer.  It only biases HOW the model spends its calls, and it degrades to
nothing when the shape is ambiguous.  The three layers compose:

* deterministic_router -> the answer is a rule   (0 calls)
* task_router          -> the answer is a recipe (0 calls)
* shape_router         -> the answer is work, but it can be done in 1 call
                          instead of N

Fail-open by design
-------------------
Anything exploratory, adaptive, conversational, or single-action returns
``None`` and the turn runs exactly as it does today.  That is deliberate: the
plan path commits the whole graph before any observation, so forcing it on
exploratory work converts graceful partial progress into total failure.  Re-Act
stays the fallback for everything this module does not positively recognise.
"""

from __future__ import annotations

import os
import re

#: Matches the deterministic router's ceiling: a wall of prose is far more
#: likely to be an explanation request than a batch job.
_MAX_TEXT_CHARS = 400
_MIN_TEXT_CHARS = 8

# Explicit, unambiguous iteration markers. These name a loop in the user's own
# words, which is the strongest available signal that the work is batch-shaped.
_LOOP_RE = re.compile(
    r"\b(?:for\s+each|for\s+every|each\s+of\s+(?:the|these|those|my)|"
    r"every\s+(?:file|one|item|entry|row|record|module|test|folder|"
    r"directory|package|function|class|branch|commit|asset)|"
    r"all\s+(?:of\s+)?(?:(?:the|these|those|my)\s+)?(?:[\w.\-]+\s+)?"
    r"(?:files?|rows?|records?|entries|items|modules?|tests?|folders?|"
    r"directories|packages?|functions?|classes?|branches|commits?|assets?)\b|"
    r"(?:batch|bulk)\s+(?:process|update|rename|convert|apply|edit)|"
    r"loop\s+over|iterate\s+over|across\s+all)\b",
    re.IGNORECASE,
)

# A counted set of items: "these 12 files", "the 30 tests", "5 modules".
_COUNTED_SET_RE = re.compile(
    r"\b(?:the|these|those|my|all)?\s*\d{2,}\s+"
    r"(?:files?|rows?|records?|entries|items|modules?|tests?|folders?|"
    r"directories|packages?|functions?|classes?|lines?|assets?|images?)\b",
    re.IGNORECASE,
)
# Small explicit counts only count when paired with a distributing determiner,
# because "2 files" alone is often a single read.
_SMALL_COUNTED_SET_RE = re.compile(
    r"\b(?:these|those|each\s+of\s+the|all\s+of\s+the)\s+\d{1,2}\s+"
    r"(?:files?|rows?|records?|entries|items|modules?|tests?|folders?|"
    r"directories|packages?|functions?|classes?|assets?|images?)\b",
    re.IGNORECASE,
)

# Chained work: a sequence of dependent steps the user spelled out.
_CHAIN_RE = re.compile(
    r"(?:\bthen\b|\bafter\s+that\b|\bafterwards\b|\bfollowed\s+by\b|"
    r"\bnext\b\s*,|\bonce\s+(?:that|you)\b|\band\s+finally\b)",
    re.IGNORECASE,
)
# Three or more imperative actions in one ask is a program, not a single call.
_ACTION_VERB_RE = re.compile(
    r"\b(?:read|load|open|parse|extract|convert|transform|clean|normalise|"
    r"normalize|merge|combine|split|filter|sort|rename|move|copy|write|"
    r"save|export|generate|create|build|compile|run|test|scan|lint|compile|"
    r"verify|check|validate|summari[sz]e|report|upload|download|fetch|"
    r"install|replace|update|append|aggregate|compute|calculate)\b",
    re.IGNORECASE,
)
# Continuation conjunctions that glue a second/third action onto the first.
_ACTION_GLUE_RE = re.compile(
    r"(?:\band\s+then\b|\band\b\s*,\s*|\band\b\s+(?=[a-z]+ing\b)|"
    r"\bthen\b|,\s*then\b|\band\s+also\b)",
    re.IGNORECASE,
)

# Exploratory / adaptive / conversational asks. These are Re-Act's home turf:
# the next step genuinely depends on what the previous step returned, so the
# whole-graph plan path would be a downgrade. Any hit here wins outright, before
# any batch signal is considered.
_EXPLORATORY_RE = re.compile(
    r"(?:"
    r"^\s*(?:why|how|what|who|when|where|which|explain|describe|tell\s+me|"
    r"summari[sz]e\s+what|walk\s+me\s+through)\b|"
    r"\bwhat\s+(?:do\s+you\s+think|happened|went\s+wrong|does\s+this\s+mean)\b|"
    r"\bcheck\s+on\s+(?:what|that|it|the\s+result)\b|"
    r"\b(check|look)\s+(?:at\s+)?(?:what\s+)?(?:you\s+)?(?:just\s+)?"
    r"(?:found|did|got|returned|produced)\b|"
    r"\b(?:dig\s+(?:into|deeper)|investigate|find\s+out\s+why|diagnose|"
    r"debug\s+why|figure\s+out\s+why|root\s+cause)\b|"
    r"\bfollow[- ]?up\b|"
    r"\bgo\s+(?:on|ahead|deeper)\b|"
    r"\btell\s+me\s+more\b|"
    r"\bis\s+it\s+(?:ok|okay|fine|safe|correct)\b|"
    r"\bdo\s+you\s+(?:think|agree|recommend)\b|"
    r"\b(?:hi|hello|hey|thanks|thank\s+you|ok|okay|cool|nice)\b\s*[!.]?\s*$"
    r")",
    re.IGNORECASE,
)

# Single-action asks: one clear imperative, no loop and no chain.
_SINGLE_ACTION_RE = re.compile(
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+|kindly\s+)?"
    r"(?:read|open|show|cat|print|list|find|search|locate|get|fetch|"
    r"run|execute|write|create|delete|remove|rename|move|copy|"
    r"install|upload|download|check)\b",
    re.IGNORECASE,
)

#: The steering message. Short on purpose: it is injected on every multi-step
#: turn, so every wasted token here is paid on every call of that turn. It also
#: preserves the Re-Act escape hatch explicitly -- the plan path must never
#: trap a turn that needs an observation before it can decide.
_STEER_MESSAGE = (
    "[Routing hint — task shape: multi-step/batch]\n"
    "This task executes the same work over many items, or chains several "
    "dependent steps. Do NOT make one model call per item or per step.\n"
    "- Express the WHOLE job as ONE `run_plan` call: put the steps in `steps`, "
    "and use a `foreach` step to iterate the item list (e.g. "
    "`{\"foreach\": \"$items.split('\\n')\", \"as\": \"item\", \"do\": [...]}`). "
    "Every loop iteration and every step then runs with ZERO extra model calls.\n"
    "- If the job is pure data/logic work (no sandbox needed), ONE `python_code` "
    "call is equally correct.\n"
    "- Keep the plan compact: batch at most ~10 steps per `run_plan` call. An "
    "oversized plan is rejected, and a truncated one is not executed at all.\n"
    "- Escape hatch: if you genuinely cannot know the next step until you have "
    "seen the previous result, ignore this hint and use ordinary tool calls. "
    "Partial progress is always better than a failed plan."
)


def shape_router_enabled() -> bool:
    """True when the shape router may steer turns (live env check).

    Read live so tests and operators can flip the switch without a restart,
    matching ``router_enabled`` / ``task_router_enabled``.
    """
    raw = os.environ.get("POWERX_SHAPE_ROUTER", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def classify_task_shape(text: str | None) -> str:
    """Classify a user ask as ``"multi_step"``, ``"exploratory"`` or ``"single"``.

    Deterministic and conservative. Only a positive, unambiguous batch/chain
    signal yields ``"multi_step"``; everything else is left to the model.
    """
    raw = text or ""
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not (_MIN_TEXT_CHARS <= len(normalized) <= _MAX_TEXT_CHARS):
        return "single"

    # Exploratory wins outright: adaptivity cannot be traded for call count.
    if _EXPLORATORY_RE.search(normalized):
        return "exploratory"

    if _LOOP_RE.search(normalized):
        return "multi_step"
    if _COUNTED_SET_RE.search(normalized):
        return "multi_step"
    if _SMALL_COUNTED_SET_RE.search(normalized):
        return "multi_step"
    if _CHAIN_RE.search(normalized):
        # A chain needs real work on both sides of the conjunction, otherwise
        # "read the file, then tell me what it says" would be misrouted.
        if len(_ACTION_VERB_RE.findall(normalized)) >= 2:
            return "multi_step"
    # Three-plus glued actions is a program regardless of the words used.
    if _ACTION_GLUE_RE.search(normalized):
        if len(_ACTION_VERB_RE.findall(normalized)) >= 3:
            return "multi_step"
    if _SINGLE_ACTION_RE.search(normalized):
        return "single"
    return "single"


def should_steer_to_plan(
    text: str | None,
    *,
    plan_tool_available: bool,
    code_tool_available: bool,
) -> bool:
    """True when this turn should be steered onto a one-call plan/program path.

    ``plan_tool_available`` / ``code_tool_available`` come from the live tool
    registry on this run, so a deployment that exposes neither is never steered
    toward a tool that does not exist.
    """
    if not shape_router_enabled():
        return False
    if not (plan_tool_available or code_tool_available):
        return False
    return classify_task_shape(text) == "multi_step"


def plan_preference_message() -> dict[str, str]:
    """The one-off steering message, shaped for OpenAI-style message lists.

    Returned as a ``user`` message (not ``system``) so it survives providers
    that only honour a single leading system prompt, and appended to the
    per-request model view only -- never to the persisted transcript.
    """
    return {"role": "user", "content": _STEER_MESSAGE}