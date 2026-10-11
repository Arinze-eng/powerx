"""Hard plan gate: the agent cannot act before it has stated a plan.

Why this exists
---------------
``shape_router`` ships the plan-first rule as a *directive*: an injected message
telling the model to state a visible plan before its first tool call. A directive
is advice. A model that ignores it -- or that emits a plan and tool calls in the
SAME message, so the plan is never separately visible -- still acts. This module
makes the rule structural instead.

How it is enforced
------------------
The FIRST provider call of a gated turn is sent with ``tools=None``. The model
therefore has nothing to call: it can only reply with text, and that text is the
plan. Tools are restored on the next iteration, and the plan is already in the
transcript as the model's own message, so the model then executes what it wrote.

That is why this is a gate and not a heuristic. There is no "did the model state
a plan?" judgement to get wrong, no plan-detection pattern to misfire, and no
way to skip the step: the capability to act is absent for exactly one call.

What it costs
-------------
One provider call on turns where the model would otherwise have planned and
acted in the same message. A turn that already obeys the directive spends that
call anyway; a turn that would have ignored it now spends it too. That is the
price of the guarantee, and it is bounded -- it never costs more than one call
per turn.

Approval
--------
Two switches, independent of each other:

* ``POWERX_PLAN_GATE`` (default on) -- the hard gate above. The plan is always
  stated first, and always visible.
* ``POWERX_PLAN_APPROVAL`` (default ``multi_step``) -- whether the turn then
  STOPS and waits for the user to approve the plan before any work happens.
  ``off`` never waits, ``multi_step`` waits only for asks the shape router
  positively recognises as multi-step, ``all`` waits on every gated turn.

``multi_step`` is the default because waiting on a trivial ask ("read this
file") is friction with no benefit, while a batch or chained ask is exactly the
case where a wrong plan is expensive.

Autonomous turns -- cron, session triggers, subagent results, internal
continuation slices, ephemeral consolidation -- are NEVER gated for approval.
There is no human present to approve, so waiting would deadlock the session.
They still get the plan gate: the plan is stated, then the work proceeds.

Fail-open
---------
Every step degrades to the behaviour that existed before this module. A plan
call that errors or comes back blank disarms the gate for the rest of the turn
and the loop runs as it does today. A pending plan that is never answered
expires. Nothing here can lose a turn.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Env switch for the hard gate. Default ON: the plan-first rule is a standing
#: requirement, not an experiment. An operator can turn it off without a
#: restart, matching ``POWERX_PLAN_FIRST`` and ``POWERX_SHAPE_ROUTER``.
PLAN_GATE_ENV = "POWERX_PLAN_GATE"

#: Env switch for the approval pause: ``off`` | ``multi_step`` (default) | ``all``.
PLAN_APPROVAL_ENV = "POWERX_PLAN_APPROVAL"

#: Where pending plans are persisted. Overridable so tests never touch the
#: operator's real store.
PLAN_STORE_ENV = "POWERX_PLAN_STORE"

#: ``stop_reason`` reported when a turn stops to wait for plan approval.
#:
#: Deliberately NOT one of ``turn_continuation``'s stall reasons, so a waiting
#: turn is never auto-resumed into execution behind the user's back -- the same
#: reasoning that keeps ``turn_budget_exceeded`` out of that set. Waiting is a
#: decision, not a stall.
PLAN_AWAITING_STOP_REASON = "plan_awaiting_approval"

#: How long an unanswered plan stays valid. Long enough to survive a user who
#: steps away from the chat, short enough that a forgotten plan never hijacks a
#: later, unrelated ask.
PLAN_APPROVAL_TTL_S = 3600.0

_DEFAULT_STORE_PATH = "~/.nanobot/pending_plans.json"

#: The note appended to the steering block on a gated turn.
#:
#: Phrased timelessly on purpose. The steering block rides on the request view of
#: EVERY iteration of the turn, so a note that only made sense on the first call
#: would have to be dropped on later calls -- which would change the request
#: prefix mid-turn and cost the provider prompt cache on every step after the
#: first. One wording that is true on every call keeps the prefix stable.
#: Starts with ``shape_router.STEER_HEADER`` ("[Standing rule") on purpose: this
#: block is a steering directive, not conversation, and everything that inspects
#: the request view uses that prefix to tell the two apart. It has to keep
#: working when the plan-first directive itself is switched off and this note is
#: the only steering text in the request.
#:
#: The ``Answer:`` escape exists because the gate is armed on EVERY task,
#: including a plain question. A question needs no tools, so a plan would
#: authorise nothing -- forcing one would spend a second request and show the
#: user the same answer twice. The marker is how the model says so in the one
#: reply it is given. Anything else is read as a plan, which is the safe
#: direction: a task that does need tools cannot be answered from memory by
#: accident, and the last line closes that door explicitly.
_PLAN_GATE_NOTE = (
    "[Standing rule — plan gate: this turn runs in two phases]\n"
    "On your FIRST reply of this turn, tools are DISABLED: you cannot call "
    "anything and must not try.\n"
    "- If the task needs tools at all, reply with the PLAN ONLY: a short numbered "
    "list of the steps you will take, in order.\n"
    "- If the task needs no tools -- a question you can answer from what you "
    "already know -- reply with `Answer:` followed by the answer and nothing "
    "else. The turn ends there.\n"
    "From your SECOND reply on, tools are restored and you execute the plan in "
    "order. Never answer from memory a task that needs tools."
)

#: Appended instead of the gate note when the user has already approved a plan
#: earlier in the conversation. Also constant for the whole turn, for the same
#: cache reason.
_PLAN_APPROVED_NOTE = (
    "[Standing rule — plan gate: the plan above is approved]\n"
    "Execute the plan you stated above, in order, using as few calls per step as "
    "the work allows. Do not restate or re-plan it. If a step fails, or something "
    "you learn changes the approach, say so plainly and state the revised plan."
)

#: What the user is told to send back to release the work. Kept short: it is
#: appended to the delivered message, not persisted.
_APPROVAL_PROMPT = (
    'Reply "go" to run this plan, or tell me what to change.'
)

#: Exact utterances that approve or reject a pending plan.
#:
#: Matched EXACTLY (after lowercasing, collapsing whitespace and stripping
#: trailing punctuation) rather than by prefix, because a prefix match turns
#: "ok, also add a test" into an approval of a plan that does not include the
#: test. A near-miss falls through to "treat it as a new request", which is
#: always safe: the user's words are then handled on their own terms.
_APPROVE_PHRASES = frozenset(
    {
        "1",
        "approve",
        "approved",
        "confirm",
        "confirmed",
        "continue",
        "do it",
        "do it now",
        "execute",
        "go",
        "go ahead",
        "go ahead please",
        "go now",
        "go on",
        "go please",
        "k",
        "lgtm",
        "ok",
        "ok go",
        "okay",
        "okay go",
        "please go",
        "proceed",
        "run it",
        "send it",
        "start",
        "sure",
        "y",
        "yeah",
        "yep",
        "yes",
        "yes do it",
        "yes go",
        "yes please",
        "yup",
    }
)

_REJECT_PHRASES = frozenset(
    {
        "2",
        "abort",
        "cancel",
        "cancel it",
        "dont",
        "don't",
        "hold on",
        "n",
        "no",
        "no thanks",
        "nope",
        "not yet",
        "reject",
        "rejected",
        "stop",
        "stop it",
        "wait",
    }
)

_STRIPPED_TRAILING = " \t\r\n.!?,:;"


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean env switch, treating unset as *default*."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def plan_gate_enabled() -> bool:
    """True when the hard plan gate may run on a task turn (live env check).

    Read live, like ``plan_first_enabled`` / ``shape_router_enabled``, so an
    operator can flip it without a restart.
    """
    return _env_flag(PLAN_GATE_ENV, True)


def plan_approval_mode() -> str:
    """Return the approval policy: ``"off"``, ``"multi_step"`` or ``"all"``.

    An unrecognised value warns and keeps ``multi_step`` rather than silently
    disabling the pause: a typo must not quietly remove a guard the operator
    meant to tighten.
    """
    raw = (os.environ.get(PLAN_APPROVAL_ENV) or "multi_step").strip().lower()
    if raw in {"0", "false", "no", "off", "never", "none", ""}:
        return "off"
    if raw in {"all", "always", "every", "1", "true", "yes", "on"}:
        return "all"
    if raw in {"multi_step", "multi-step", "multistep", "batch", "default"}:
        return "multi_step"
    logger.warning(
        "Unrecognised %s=%r; using multi_step", PLAN_APPROVAL_ENV, raw
    )
    return "multi_step"


def approval_required(shape: str | None, mode: str, *, interactive: bool) -> bool:
    """True when this turn must stop and wait for the user to approve its plan.

    *interactive* is computed by the caller because only it can see the channel
    and the message metadata; this module stays free of session imports so it can
    be unit-tested without a runtime.
    """
    if not interactive or mode == "off":
        return False
    if mode == "all":
        return True
    return shape == "multi_step"


#: Marker a plan-phase reply uses to say "this task needs no tools; this is the
#: finished answer". Public so tests and the runner agree on one spelling.
PLAN_DIRECT_ANSWER_MARKER = "Answer:"


def direct_answer(text: str | None) -> str | None:
    """Return the answer body when a plan-phase reply is a direct answer.

    ``None`` means "read it as a plan instead", which is what every reply that
    does not carry the marker gets. The marker is required rather than inferred
    from the shape of the text: a short prose reply from a task that DOES need
    tools ("I would need to read the file first") is not an answer, and guessing
    wrong there would end the turn with the work undone.
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped.lower().startswith(PLAN_DIRECT_ANSWER_MARKER.lower()):
        return None
    body = stripped[len(PLAN_DIRECT_ANSWER_MARKER) :].strip()
    return body or None


def plan_gate_note() -> str:
    """The note that tells the model why its first reply has no tools."""
    return _PLAN_GATE_NOTE


def plan_approved_note() -> str:
    """The note for a turn whose plan was already approved by the user."""
    return _PLAN_APPROVED_NOTE


def approval_prompt() -> str:
    """The line appended to a waiting turn's reply, telling the user how to go."""
    return _APPROVAL_PROMPT


def _normalize_reply(text: str | None) -> str:
    if not text:
        return ""
    collapsed = " ".join(text.strip().split())
    return collapsed.strip(_STRIPPED_TRAILING).lower()


def parse_plan_approval(text: str | None) -> str | None:
    """Classify a user reply to a waiting plan.

    Returns ``"approve"``, ``"reject"`` or ``None``. ``None`` means "not an
    answer to the plan" -- the caller treats the message as a fresh request,
    which is the only safe reading of an unrecognised reply.
    """
    normalized = _normalize_reply(text)
    if not normalized:
        return None
    if normalized in _REJECT_PHRASES:
        return "reject"
    if normalized in _APPROVE_PHRASES:
        return "approve"
    return None


def _store_path() -> Path:
    raw = (os.environ.get(PLAN_STORE_ENV) or "").strip() or _DEFAULT_STORE_PATH
    return Path(raw).expanduser()


class PendingPlanStore:
    """JSON-backed store of plans waiting for approval, keyed by session.

    One file, read-modify-write, atomic replace. Deliberately small: the record
    is a plan string, the shape that produced it and a timestamp, so the store
    cannot grow into a second transcript. Every failure is swallowed and logged
    -- losing a pending plan degrades to "the user is asked again", which is
    recoverable, whereas raising here would fail a turn that already succeeded.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl_s: float = PLAN_APPROVAL_TTL_S,
    ) -> None:
        self._path = path if path is not None else _store_path()
        self._ttl_s = ttl_s

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[str, Any]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            logger.warning("Could not read pending plans from %s", self._path)
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Pending plan store %s is not valid JSON; ignoring", self._path)
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            logger.warning("Could not persist pending plans to %s", self._path)

    def get(self, session_key: str | None) -> dict[str, Any] | None:
        """Return the live pending plan for *session_key*, or ``None``.

        A record past its TTL is dropped here rather than returned, so a stale
        plan can never be resumed by a later, unrelated ask.
        """
        if not session_key:
            return None
        record = self._load().get(session_key)
        if not isinstance(record, dict):
            return None
        plan = record.get("plan")
        if not isinstance(plan, str) or not plan.strip():
            return None
        try:
            age = time.time() - float(record.get("created_at"))
        except (TypeError, ValueError):
            age = self._ttl_s + 1.0
        if age > self._ttl_s or age < 0:
            self.clear(session_key)
            return None
        return {
            "plan": plan,
            "shape": record.get("shape"),
            "created_at": record.get("created_at"),
        }

    def put(
        self,
        session_key: str | None,
        plan: str,
        *,
        shape: str | None = None,
    ) -> None:
        """Record *plan* as waiting for approval in *session_key*."""
        if not session_key or not plan.strip():
            return
        data = self._load()
        data[session_key] = {
            "plan": plan,
            "shape": shape,
            "created_at": time.time(),
        }
        self._write(data)

    def clear(self, session_key: str | None) -> None:
        """Forget the pending plan for *session_key*, if any."""
        if not session_key:
            return
        data = self._load()
        if session_key in data:
            data.pop(session_key, None)
            self._write(data)


_STORE: PendingPlanStore | None = None


def pending_plans() -> PendingPlanStore:
    """The process-wide pending-plan store (created on first use)."""
    global _STORE
    if _STORE is None:
        _STORE = PendingPlanStore()
    return _STORE


def reset_pending_plans_store() -> None:
    """Drop the cached store so a test can rebind the path via env."""
    global _STORE
    _STORE = None
