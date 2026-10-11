"""The per-request message cap: stop re-sending the whole transcript every step.

Every other step in ``prepare_for_model`` is budget-triggered -- it fires only
when the prompt would overflow the provider's window. On a large window none of
them fire, so a turn re-sends its entire growing transcript on every iteration.

MEASURED in production (cdnai/powerx, 2026-09-28): one turn reached 287 messages
by iteration 119 while re-sending all of them every step. It did not die of
context -- it died of MEMORY. The container is 512 MiB and the kernel recycled it
mid-turn. Memory tracks the message COUNT and the bytes held, not the provider's
token budget, so a token-budgeted guard cannot bound it.

These tests pin the count bound, the legality of what it produces, that the
original ask survives, and that the persisted transcript is never touched.
"""

from __future__ import annotations

import pytest

from nanobot.agent.context_governance import (
    ENV_MAX_REQUEST_MESSAGES,
    MAX_REQUEST_MESSAGES,
    ContextGovernanceConfig,
    ContextGovernor,
)


def _config(**overrides) -> ContextGovernanceConfig:
    base = dict(
        provider=None,  # unused by cap_request_messages
        model="test-model",
        tools=None,
        workspace=None,
        session_key="s",
        max_tool_result_chars=1000,
        context_window_tokens=None,  # deliberately huge/unset: nothing else fires
    )
    base.update(overrides)
    return ContextGovernanceConfig(**base)


def _tool_result(i: int) -> dict:
    return {"role": "tool", "tool_call_id": f"c{i}", "name": "read_file", "content": f"r{i}"}


def _assistant_call(i: int) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ],
    }


def _long_turn(pairs: int) -> list[dict]:
    """A realistic growing tool-calling turn: user, then assistant/tool pairs."""
    messages = [{"role": "user", "id": "u1", "content": "do the thing"}]
    for i in range(pairs):
        messages.append(_assistant_call(i))
        messages.append(_tool_result(i))
    return messages


# --- the bound --------------------------------------------------------------


def test_cap_bounds_the_request_view_by_count() -> None:
    """The headline behaviour: 120 pairs must not be re-sent in full."""
    governor = ContextGovernor()
    messages = _long_turn(60)  # 121 messages
    capped = governor.cap_request_messages(messages, limit=20)

    assert len(capped) <= 21  # 20 + the re-anchored user turn


def test_short_turn_is_untouched() -> None:
    """A normal turn must pay nothing for this -- identity, not a copy."""
    governor = ContextGovernor()
    messages = _long_turn(3)
    assert governor.cap_request_messages(messages, limit=40) is messages


def test_system_messages_are_always_kept() -> None:
    """They are the pinned instruction block and must never be dropped."""
    governor = ContextGovernor()
    messages = [{"role": "system", "content": "you are an agent"}] + _long_turn(60)
    capped = governor.cap_request_messages(messages, limit=10)

    assert capped[0]["role"] == "system"
    assert sum(1 for m in capped if m.get("role") == "system") == 1


def test_original_user_ask_is_re_anchored() -> None:
    """Dropping the task statement would be worse than the bytes it saves."""
    governor = ContextGovernor()
    messages = _long_turn(60)
    capped = governor.cap_request_messages(messages, limit=10)

    first_non_system = next(m for m in capped if m.get("role") != "system")
    assert first_non_system.get("role") == "user"
    assert first_non_system.get("content") == "do the thing"


def test_persisted_transcript_is_not_mutated() -> None:
    """The contract every governance step honours: the model copy is a view."""
    governor = ContextGovernor()
    messages = _long_turn(60)
    before = [dict(m) for m in messages]

    governor.cap_request_messages(messages, limit=10)

    assert [dict(m) for m in messages] == before
    assert len(messages) == 121


def test_capped_view_is_a_legal_request() -> None:
    """No orphan tool result may lead the list: the provider rejects that."""
    governor = ContextGovernor()
    messages = _long_turn(60)
    capped = governor.cap_request_messages(messages, limit=15)

    if capped and capped[0].get("role") == "tool":
        pytest.fail("request view must not begin with a tool result")


def test_cap_of_one_still_produces_a_request() -> None:
    """Degenerate limit must not produce an empty or illegal list."""
    governor = ContextGovernor()
    messages = _long_turn(60)
    capped = governor.cap_request_messages(messages, limit=1)

    assert capped
    assert capped[0].get("role") != "tool"


# --- resolution order -------------------------------------------------------


def test_explicit_config_wins() -> None:
    governor = ContextGovernor()
    assert governor.request_message_cap(_config(max_request_messages=7)) == 7


def test_env_override_used_when_config_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_MAX_REQUEST_MESSAGES, "12")
    governor = ContextGovernor()
    assert governor.request_message_cap(_config()) == 12


def test_env_garbage_keeps_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not silently remove the bound on a memory-limited host."""
    monkeypatch.setenv(ENV_MAX_REQUEST_MESSAGES, "lots")
    governor = ContextGovernor()
    assert governor.request_message_cap(_config()) == MAX_REQUEST_MESSAGES


def test_env_zero_keeps_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike the turn budget, 0 is NOT an opt-out here -- memory is not optional."""
    monkeypatch.setenv(ENV_MAX_REQUEST_MESSAGES, "0")
    governor = ContextGovernor()
    assert governor.request_message_cap(_config()) == MAX_REQUEST_MESSAGES


def test_default_cap_is_bounded() -> None:
    assert 0 < MAX_REQUEST_MESSAGES <= 100
