"""The hard plan gate's policy: switches, approval parsing, and the store.

The gate itself is structural (see ``test_runner_plan_gate.py`` for the loop
behaviour). These tests pin the decisions around it: when it is on, when a turn
is allowed to stop and wait, what counts as the user approving, and the fact
that a pending plan is never resumed by an unrelated later ask.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from nanobot.agent import plan_gate
from nanobot.agent.plan_gate import (
    PLAN_APPROVAL_TTL_S,
    PLAN_AWAITING_STOP_REASON,
    PendingPlanStore,
    approval_prompt,
    approval_required,
    parse_plan_approval,
    plan_approval_mode,
    plan_gate_enabled,
)


# --- switches ---------------------------------------------------------------


def test_gate_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plan-first rule is a standing requirement, so the enforcement that
    makes it structural is on unless an operator says otherwise.

    ``delenv`` rather than a bare call because the agent suite opts out of the
    gate globally (see tests/agent/conftest.py); this asserts the value an
    unconfigured deployment actually ships.
    """
    monkeypatch.delenv("POWERX_PLAN_GATE", raising=False)
    assert plan_gate_enabled() is True


def test_gate_env_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWERX_PLAN_GATE", "off")
    assert plan_gate_enabled() is False


def test_gate_env_unparseable_keeps_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not silently disable a guard."""
    monkeypatch.setenv("POWERX_PLAN_GATE", "maybe")
    assert plan_gate_enabled() is True


def test_approval_mode_defaults_to_multi_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Waiting on a trivial ask is friction with no benefit; waiting on a batch
    or chained ask is the case where a wrong plan is expensive."""
    monkeypatch.delenv("POWERX_PLAN_APPROVAL", raising=False)
    assert plan_approval_mode() == "multi_step"


def test_approval_mode_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWERX_PLAN_APPROVAL", "off")
    assert plan_approval_mode() == "off"


def test_approval_mode_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWERX_PLAN_APPROVAL", "all")
    assert plan_approval_mode() == "all"


def test_approval_mode_truthy_is_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """``=1`` reads as "yes, require approval" -- the strictest reading."""
    monkeypatch.setenv("POWERX_PLAN_APPROVAL", "1")
    assert plan_approval_mode() == "all"


def test_approval_mode_typo_keeps_multi_step(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWERX_PLAN_APPROVAL", "sometimes")
    assert plan_approval_mode() == "multi_step"


# --- who waits --------------------------------------------------------------


def test_multi_step_interactive_waits() -> None:
    assert approval_required("multi_step", "multi_step", interactive=True) is True


def test_single_action_interactive_does_not_wait() -> None:
    """'read this file' must not cost a round trip to approve."""
    assert approval_required("single", "multi_step", interactive=True) is False


def test_exploratory_interactive_does_not_wait() -> None:
    assert approval_required("exploratory", "multi_step", interactive=True) is False


def test_autonomous_turn_never_waits() -> None:
    """No human is present on a cron job or a subagent result, so waiting would
    block the session until the pending plan expired."""
    for shape in ("multi_step", "single", "exploratory"):
        assert approval_required(shape, "multi_step", interactive=False) is False
    assert approval_required("multi_step", "all", interactive=False) is False


def test_off_mode_never_waits() -> None:
    assert approval_required("multi_step", "off", interactive=True) is False


def test_all_mode_waits_on_every_shape() -> None:
    for shape in ("multi_step", "single", "exploratory"):
        assert approval_required(shape, "all", interactive=True) is True


def test_unknown_shape_does_not_wait() -> None:
    """An unclassifiable ask is exactly the exploratory case the plan path must
    not force, so it must not be gated for approval either."""
    assert approval_required(None, "multi_step", interactive=True) is False


# --- reading the user's reply ----------------------------------------------


@pytest.mark.parametrize(
    "reply",
    ["go", "Go", "go!", "GO AHEAD", "yes", "y", "ok", "okay", "proceed",
     "approved", "do it", "continue", "lgtm", "  yes  ", "1", "start"],
)
def test_approvals_are_recognised(reply: str) -> None:
    assert parse_plan_approval(reply) == "approve"


@pytest.mark.parametrize(
    "reply", ["no", "nope", "stop", "cancel", "wait", "hold on", "not yet", "2"]
)
def test_rejections_are_recognised(reply: str) -> None:
    assert parse_plan_approval(reply) == "reject"


def test_a_new_request_is_not_an_approval() -> None:
    """The dangerous false positive: a message that merely STARTS with an
    affirmative but asks for something else must not release a plan that does
    not include the new work."""
    assert parse_plan_approval("ok, also add a test") is None
    assert parse_plan_approval("yes but use python") is None
    assert parse_plan_approval("go ahead and rename the folder too") is None


def test_a_long_message_is_never_an_approval() -> None:
    """Even an all-affirmative-sounding paragraph is a fresh ask: a plan is
    released only by an unambiguous, short reply."""
    assert parse_plan_approval("yes " * 40) is None


def test_empty_and_none_are_not_approvals() -> None:
    assert parse_plan_approval(None) is None
    assert parse_plan_approval("") is None
    assert parse_plan_approval("   ") is None


def test_unrelated_text_is_not_an_approval() -> None:
    assert parse_plan_approval("what files are in this repo?") is None


# --- the stopping reason ----------------------------------------------------


def test_awaiting_stop_reason_is_not_resumable() -> None:
    """A turn waiting for approval is making a decision, not stalling. If this
    reason were resumable, the policy would execute the plan the user was still
    reading -- the exact thing approval exists to prevent."""
    from nanobot.session import turn_continuation

    assert turn_continuation.stall_is_resumable(PLAN_AWAITING_STOP_REASON) is False


# --- the store --------------------------------------------------------------


def test_store_round_trips_a_plan(tmp_path: Path) -> None:
    store = PendingPlanStore(tmp_path / "plans.json")
    store.put("sess-1", "1. do a thing\n2. do another", shape="multi_step")
    record = store.get("sess-1")
    assert record is not None
    assert record["plan"] == "1. do a thing\n2. do another"
    assert record["shape"] == "multi_step"


def test_store_is_per_session(tmp_path: Path) -> None:
    store = PendingPlanStore(tmp_path / "plans.json")
    store.put("a", "plan a")
    store.put("b", "plan b")
    assert store.get("a")["plan"] == "plan a"
    assert store.get("b")["plan"] == "plan b"
    assert store.get("c") is None


def test_store_clear_removes_only_that_session(tmp_path: Path) -> None:
    store = PendingPlanStore(tmp_path / "plans.json")
    store.put("a", "plan a")
    store.put("b", "plan b")
    store.clear("a")
    assert store.get("a") is None
    assert store.get("b")["plan"] == "plan b"


def test_expired_plan_is_not_returned(tmp_path: Path) -> None:
    """A plan the user walked away from must not be resumed an hour later by an
    unrelated ask."""
    store = PendingPlanStore(tmp_path / "plans.json", ttl_s=0.01)
    store.put("sess", "1. stale step")
    time.sleep(0.05)
    assert store.get("sess") is None


def test_expired_plan_is_dropped_from_disk(tmp_path: Path) -> None:
    """Reading it must also clean it up, so the file cannot accumulate dead
    records for sessions nobody returns to."""
    path = tmp_path / "plans.json"
    store = PendingPlanStore(path, ttl_s=0.01)
    store.put("sess", "1. stale step")
    time.sleep(0.05)
    store.get("sess")
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_a_future_dated_record_is_not_resumed(tmp_path: Path) -> None:
    """Clock skew must not produce a plan that never expires."""
    path = tmp_path / "plans.json"
    path.write_text(
        json.dumps({"sess": {"plan": "x", "created_at": time.time() + 10_000}}),
        encoding="utf-8",
    )
    assert PendingPlanStore(path).get("sess") is None


def test_corrupt_store_degrades_to_no_plan(tmp_path: Path) -> None:
    """Losing a pending plan costs one re-plan. Raising here would fail a turn
    that had already succeeded, which is far worse."""
    path = tmp_path / "plans.json"
    path.write_text("not json at all", encoding="utf-8")
    store = PendingPlanStore(path)
    assert store.get("sess") is None
    store.put("sess", "1. fresh plan")
    assert store.get("sess")["plan"] == "1. fresh plan"


def test_store_ignores_blank_and_keyless_plans(tmp_path: Path) -> None:
    store = PendingPlanStore(tmp_path / "plans.json")
    store.put("sess", "   ")
    store.put(None, "a plan")
    assert store.get("sess") is None
    assert store.get(None) is None


def test_store_records_are_self_describing(tmp_path: Path) -> None:
    store = PendingPlanStore(tmp_path / "plans.json")
    store.put("sess", "1. step", shape="multi_step")
    raw = json.loads((tmp_path / "plans.json").read_text(encoding="utf-8"))
    assert set(raw["sess"]) == {"plan", "shape", "created_at"}


def test_ttl_is_an_hour() -> None:
    assert PLAN_APPROVAL_TTL_S == 3600.0


# --- the text shown to the user --------------------------------------------


def test_approval_prompt_names_the_word_that_releases_it() -> None:
    """The prompt has to agree with the parser, or the user is told to send
    something that will not work."""
    prompt = approval_prompt()
    assert "go" in prompt.lower()
    assert parse_plan_approval("go") == "approve"


def test_gate_note_is_recognised_as_steering() -> None:
    """The note is appended to the request view, so it must carry the same
    header as every other steering block. Otherwise code that inspects 'the last
    user message' (attachment injection, prompt assertions) reads the directive
    as if the user had typed it."""
    from nanobot.agent.shape_router import is_steer_message

    assert is_steer_message(plan_gate.plan_gate_note()) is True
    assert is_steer_message(plan_gate.plan_approved_note()) is True


def test_gate_note_says_tools_are_disabled() -> None:
    """The model must be told why its first reply cannot call anything, or it
    reports a broken environment instead of stating a plan."""
    note = plan_gate.plan_gate_note().lower()
    assert "tools are disabled" in note
    assert "plan" in note


def test_gate_note_offers_the_no_tools_escape() -> None:
    """The gate is armed on every task, including a plain question. The model
    needs a way to say "this needs no tools" in the one reply it gets, or a
    question is answered twice and billed twice."""
    note = plan_gate.plan_gate_note()
    assert "Answer:" in note
    assert "needs no tools" in note


def test_gate_note_forbids_answering_work_from_memory() -> None:
    """The escape must not become a licence to skip the work: the whole point of
    the gate is that a task needing tools is not answered without them."""
    note = plan_gate.plan_gate_note().lower()
    assert "never answer from memory" in note


# --- the no-tools escape ----------------------------------------------------


def test_direct_answer_marker_is_read() -> None:
    assert plan_gate.direct_answer("Answer: 4") == "4"
    assert plan_gate.direct_answer("answer: Paris") == "Paris"
    assert plan_gate.direct_answer("  Answer:  two plus two is four  ") == "two plus two is four"


def test_a_plan_is_not_a_direct_answer() -> None:
    """Only the marker makes it an answer. Prose from a task that needs tools
    must stay a plan, or the turn would end with the work undone."""
    assert plan_gate.direct_answer("1. Read the file\n2. Summarise it") is None
    assert plan_gate.direct_answer("I will need to read the file first") is None
    assert plan_gate.direct_answer("Answering your question: no") is None


def test_a_marker_with_nothing_after_it_is_not_an_answer() -> None:
    assert plan_gate.direct_answer("Answer:") is None
    assert plan_gate.direct_answer("Answer:   ") is None
    assert plan_gate.direct_answer(None) is None


def test_approved_note_forbids_re_planning() -> None:
    """Re-planning an approved plan spends a call to ask a question the user has
    already answered."""
    note = plan_gate.plan_approved_note().lower()
    assert "approved" in note
    assert "do not restate or re-plan" in note


# --- a plan wearing the answer marker ---------------------------------------


def test_a_marked_plan_is_not_an_answer() -> None:
    """Observed on live traffic: told only to reply with the plan, the model
    wrote ``Answer: Plan: ...``. Taken at face value that ends the turn holding a
    plan, with nothing done -- on every "Continue" the user sent."""
    assert plan_gate.direct_answer("Answer: Plan: 1. Do X\n2. Do Y") is None
    assert plan_gate.direct_answer("Answer: plan") is None
    assert plan_gate.direct_answer("Answer: **Plan**\n1. Do X") is None
    assert plan_gate.direct_answer("Answer: Planning to do X") is None


def test_a_real_answer_after_the_marker_still_works() -> None:
    """The refusal is about the word "plan", not about length or lists: a real
    answer that enumerates is still delivered in one call."""
    assert plan_gate.direct_answer("Answer: Paris") == "Paris"
    assert plan_gate.direct_answer("Answer: 1. Paris 2. Lyon") == "1. Paris 2. Lyon"


# --- continuations ----------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "continue",
        "Continue",
        "continue please",
        "Continue and give me zip",
        "Continue implementing stop pausing",
        "carry on",
        "carry on with the migration",
        "keep going",
        "resume",
        "resume where you left off",
        "go on",
        "finish it",
        "next step",
        "next",
        "proceed",
    ],
)
def test_a_continuation_is_recognised(reply: str) -> None:
    """Live traffic is why these exist: the user repeated "continue" and the
    agent re-planned and handed back the same plan each time."""
    assert plan_gate.is_continuation(reply) is True


@pytest.mark.parametrize(
    "reply",
    [
        "rename all the .txt files",
        "ok, also add a test",
        "yes but use python",
        "go ahead and rename the folder too",
        "",
        None,
        "what files are in this repo?",
    ],
)
def test_new_work_is_not_a_continuation(reply) -> None:
    """A message carrying new work must still plan: releasing a held plan for it
    would run the old plan and silently drop the new ask."""
    assert plan_gate.is_continuation(reply) is False


def test_a_continuation_releases_a_held_plan() -> None:
    """The user saying "continue" is an approval of the plan they are looking at.
    Without this the held plan is dropped and the task is planned all over
    again, which is the repetition the user reported."""
    assert parse_plan_approval("Continue and give me zip") == "approve"
    assert parse_plan_approval("continue implementing stop pausing") == "approve"
    assert parse_plan_approval("carry on") == "approve"
