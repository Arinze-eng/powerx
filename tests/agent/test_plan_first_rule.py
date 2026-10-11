"""Every task carries the standing plan-first rule.

The requirement: every task lays out a plan, the USER can see it, and the agent
follows it rather than improvising. The rule must therefore be present on EVERY
task turn -- not only the batch-shaped ones the routing hints cover.

The distinction these tests protect is that plan-first is a DISCIPLINE (order and
visibility), not a routing decision (express the work as one ``run_plan`` call).
The routing hints may only touch shapes this module recognises positively,
because the plan path commits the whole graph before any observation; the
plan-first rule commits nothing and is therefore safe on every shape, including
exploratory ones.
"""

from __future__ import annotations

import pytest

from nanobot.agent.shape_router import (
    _PLAN_FIRST_MESSAGE,
    _STEER_MESSAGE,
    classify_task_shape,
    compose_steer_message,
    plan_first_enabled,
    plan_first_message,
    steer_message_for,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POWERX_PLAN_FIRST", raising=False)
    monkeypatch.delenv("POWERX_SHAPE_ROUTER", raising=False)


# --- the rule is on by default and universal --------------------------------


def test_plan_first_is_on_by_default() -> None:
    assert plan_first_enabled() is True


def test_plan_first_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWERX_PLAN_FIRST", "0")
    assert plan_first_enabled() is False


@pytest.mark.parametrize(
    "ask",
    [
        "what is the capital of France",          # single / conversational
        "read the file and tell me what it says", # single action
        "build the whole app with backend and frontend",  # project
        "for each of these files, add a header",  # batch
        "investigate why the build fails and fix it",  # exploratory
        "",
        "hi",
    ],
)
def test_every_task_gets_the_plan_rule(ask: str) -> None:
    """The headline requirement: no task shape is exempt."""
    message = compose_steer_message(ask)
    assert message is not None
    assert message["role"] == "user"
    assert "PLAN" in message["content"]


def test_the_rule_requires_the_plan_be_visible() -> None:
    """A plan the user cannot see does not satisfy the requirement."""
    content = _PLAN_FIRST_MESSAGE
    assert "VISIBLE" in content
    assert "user must be able to read it" in content


def test_the_rule_requires_following_the_plan() -> None:
    content = _PLAN_FIRST_MESSAGE
    assert "FOLLOW IT" in content
    assert "in order" in content


def test_the_rule_forbids_silent_course_changes() -> None:
    """"Thinks anyhow and randomly" is exactly a silent change of direction."""
    content = _PLAN_FIRST_MESSAGE
    assert "Silently changing course" in content


def test_the_rule_keeps_the_observe_then_decide_escape_hatch() -> None:
    """The rule must not trap a task that needs a result before the next step.

    This is the difference between plan-first and plan-only, and it is what makes
    the rule safe to apply to exploratory work.
    """
    content = _PLAN_FIRST_MESSAGE
    assert "one result before you can" in content


def test_the_rule_does_not_force_a_single_plan_call() -> None:
    """It must not read as "express everything as one run_plan call"."""
    content = _PLAN_FIRST_MESSAGE
    assert "not about expressing everything" in content


def test_plan_first_message_shape() -> None:
    message = plan_first_message()
    assert message["role"] == "user"
    assert message["content"] == _PLAN_FIRST_MESSAGE


# --- composition ------------------------------------------------------------


def test_batch_ask_gets_both_plan_rule_and_routing_hint() -> None:
    """A recognised shape gets its hint *as well as* the standing rule."""
    message = compose_steer_message("for each of these files, add a header")
    assert message is not None
    assert _PLAN_FIRST_MESSAGE in message["content"]
    assert "run_plan" in message["content"]


def test_exploratory_ask_gets_the_rule_but_no_routing_hint() -> None:
    """Force-routing exploratory work trades partial progress for total failure."""
    ask = "investigate why the build is failing and then decide what to do"
    assert classify_task_shape(ask) != "multi_step"
    message = compose_steer_message(ask)
    assert message is not None
    assert _PLAN_FIRST_MESSAGE in message["content"]
    assert "Routing hint" not in message["content"]


def test_composition_is_one_message() -> None:
    """Both directives must travel in ONE message: it is re-sent every iteration."""
    message = compose_steer_message("for each of these files, add a header")
    assert message is not None
    assert message["content"].count("[Standing rule") == 1
    assert message["content"].count("[Routing hint") == 1
    # One message, not two.
    assert isinstance(message["content"], str)


def test_routing_hint_is_gated_on_the_tool_it_names() -> None:
    """A hint that names run_plan must not reach a registry without it."""
    message = compose_steer_message(
        "for each of these files, add a header",
        plan_tool_available=False,
        code_tool_available=False,
    )
    assert message is not None
    assert _PLAN_FIRST_MESSAGE in message["content"]
    assert "run_plan" not in message["content"]


def test_nothing_to_say_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the rule off and no recognised shape, a turn pays nothing."""
    monkeypatch.setenv("POWERX_PLAN_FIRST", "0")
    assert compose_steer_message("what is the capital of France") is None


# --- the batch hint fallback ------------------------------------------------


def test_plain_batch_ask_now_gets_a_routing_hint() -> None:
    """Regression: this returned None, so a plain batch ask walked the loop.

    ``should_steer_to_plan`` routes every multi_step turn to
    ``steer_message_for``, but that function only handled the library and project
    shapes -- so the generic batch hint was unreachable and a plain batch ask
    received no hint at all.
    """
    hint = steer_message_for("for each of these files, add a header")
    assert hint is not None
    assert hint["content"] == _STEER_MESSAGE


def test_specialist_shapes_still_win() -> None:
    """The fallback must not shadow the library/project hints."""
    library = steer_message_for("generate 10 images of cats")
    assert library is not None
    assert library["content"] != _STEER_MESSAGE


def test_non_multi_step_still_has_no_routing_hint() -> None:
    """The fallback must not widen routing to every task."""
    assert steer_message_for("what is the capital of France") is None
