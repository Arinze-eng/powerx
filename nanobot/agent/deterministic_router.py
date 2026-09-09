"""Deterministic pre-LLM router for recurring read-only UniAbuja asks.

Manus-style cost discipline means the model should not be paid to perform a
lookup a rule can perform.  When the raw user text *unambiguously* names one of
the supported read-only families, the runner answers it by executing the SAME
registered tool the model would have called -- zero provider round-trips, zero
credit steps, live data (never a stale replay).

Supported families (all read-only, all identity-resolved by the tool itself):

* account/access status        -> ``uniabuja_student action=status``
* announcements                -> ``uniabuja_student query(announcements)``
* my own previous questions    -> ``uniabuja_student query(my_questions)``
* my own transcript/records    -> ``uniabuja_transcript student_lookup`` (no regno:
                                 the tool resolves the caller's own portal email)
* explicit regno lookup        -> ``uniabuja_transcript student_lookup`` with the
                                 extracted registration number (tool enforces the
                                 verified-administrator path for by-regno reads)

Fail-open by design: anything ambiguous, open-ended, write-shaped,
image-bearing, longer than a short ask, or whose tool is not registered on the
run falls through to the normal LLM path unchanged, so correctness is
preserved while routine lookups stop costing API calls.  When the matched tool
itself returns an error verdict (not signed in, access disabled by the
administrator, backend unreachable) that verdict IS the answer -- it is an
identity/configuration fact the model could only rephrase, never fix, so it is
delivered as-is with zero provider calls.  The runner only consults this module
when the run opts in (``enable_deterministic_router`` + router text + not an
image turn), so generic agents on the same runner are untouched.
"""

from __future__ import annotations

import os
import re

from nanobot.providers.base import ToolCallRequest

#: Only short, structured asks are routed.  A long or conversational message
#: almost always needs synthesis or follow-up, which is the model's job.
_MAX_TEXT_CHARS = 400
_MIN_TEXT_CHARS = 2

_REGNO_RE = re.compile(r"\b\d{2}/\d{2,5}[A-Za-z]+/\d+\b")

#: Strong do-verbs.  When the message *starts* with one of these the user is
#: asking for a mutation, a build, or a how-to -- never a read-only lookup.
_WRITE_START_RE = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+)?"
    r"(?:build|make|create|write|edit|update|delete|remove|upload|send|post|"
    r"publish|fix|deploy|install|configure|push|commit|pay|buy|submit|apply|"
    r"change|set|add|help me|teach me|show me how)\b"
)

#: A lookup performed *for* an explicit registration number.  The tool itself
#: enforces the verified-administrator branch, so a student who types a regno
#: that is not their own gets a clean tool error and the runner falls back.
_REGNO_VERB_RE = re.compile(
    r"\b(lookup|check|search|find|details?|record|transcript|result|profile|"
    r"score|status)\b"
)

_TRANSCRIPT_SELF_RE = re.compile(
    r"\bmy\s+(transcript|transcripts|record|records|result|results|courses?|"
    r"grades?|profile|statement|registration|details|academic)\b"
)
_ANNOUNCEMENTS_RE = re.compile(
    r"\b(announcements?|notices?|school news|latest (?:news|update|updates?)|"
    r"what'?s new)\b"
)
_MY_QUESTIONS_RE = re.compile(
    r"\b(?:my\s+(?:asked\s+)?questions?|question history|what have i asked|"
    r"(?:show|see)\s+my\s+(?:asked\s+)?questions?)\b"
)
_STATUS_RE = re.compile(
    r"\b(?:my\s+)?(?:account\s+)?(?:access|status)\b|"
    r"what can i (?:access|do|use)|access level|do i have access|access granted"
)
#: "Eligibility" is deliberately never routed: it is ambiguous between the
#: account-access sense (status) and the payment/graduation sense (a different
#: transcript action), so it always falls through to the model.
_ELIGIBILITY_RE = re.compile(r"eligib\w*", re.IGNORECASE)


def router_enabled() -> bool:
    """True when the deterministic router may answer turns.

    Read live so tests (and operators) can flip the env switch without a
    process restart.
    """
    return os.environ.get("POWERX_DETERMINISTIC_ROUTER", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def last_user_text(messages: list[dict[str, object]] | None) -> str | None:
    """The freshest user message's plain text, or None when it is absent.

    Only plain-string content qualifies: when the freshest user content is a
    block list (an image/attachment turn) the router must not fire, because the
    task needs OCR or visual reasoning the rules cannot provide.
    """
    for message in reversed(messages or ()):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        return None  # non-text content (image list, empty) -> not routable
    return None


def _looks_read_only(text: str) -> bool:
    """Reject write-shaped asks before any family matching."""
    if _WRITE_START_RE.search(text):
        return False
    # A request to change/update *my record* is not a lookup either.
    if re.search(r"\b(update|change|edit|remove|delete|fix)\s+my\b", text):
        return False
    return True


def deterministic_plan(text: str) -> ToolCallRequest | None:
    """Return the tool call that answers *text* with zero provider calls.

    Returns ``None`` when the message is not an unambiguous read-only UniAbuja
    ask -- the caller then runs the normal LLM path unchanged.
    """
    if not router_enabled():
        return None
    raw = text or ""
    normalized = re.sub(r"\s+", " ", raw).strip().lower()
    if not (_MIN_TEXT_CHARS <= len(normalized) <= _MAX_TEXT_CHARS):
        return None
    if not _looks_read_only(normalized):
        return None

    # 1. Explicit regno lookup -- most specific, checked first.  The regno is
    # matched against the ORIGINAL text (case preserved) so portal codes like
    # ``22/205EEE/172`` reach the tool unchanged; keywords use the lowercased
    # form.
    regno_match = _REGNO_RE.search(raw)
    if regno_match and _REGNO_VERB_RE.search(normalized):
        return ToolCallRequest(
            id="det-regno",
            name="uniabuja_transcript",
            arguments={
                "action": "student_lookup",
                "regno": regno_match.group(0),
            },
        )

    # 2. "My ..." families (own records, own questions).
    if _TRANSCRIPT_SELF_RE.search(normalized) and not _ELIGIBILITY_RE.search(normalized):
        return ToolCallRequest(
            id="det-transcript",
            name="uniabuja_transcript",
            arguments={"action": "student_lookup"},
        )
    if _MY_QUESTIONS_RE.search(normalized):
        return ToolCallRequest(
            id="det-my-questions",
            name="uniabuja_student",
            arguments={"action": "query", "resource": "my_questions"},
        )

    # 3. Announcements.
    if _ANNOUNCEMENTS_RE.search(normalized):
        return ToolCallRequest(
            id="det-announcements",
            name="uniabuja_student",
            arguments={"action": "query", "resource": "announcements"},
        )

    # 4. Account/access status -- most general, checked last so a status word
    # inside a more specific ask never hijacks it.
    if _STATUS_RE.search(normalized):
        return ToolCallRequest(
            id="det-status",
            name="uniabuja_student",
            arguments={"action": "status"},
        )

    return None
