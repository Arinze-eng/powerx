"""A reply that announces a tool step and then makes no call must not end the turn.

Observed in a live run: the model wrote "Now I have the reference format. Let me
create the comprehensive technical report for you..." with no tool call, the turn
closed as "Stopped 1 task(s)", and the user had to tell it to continue. The runner
now asks for the call once or twice, then ends the turn if the model will not make it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.runner import (
    _MAX_PROMISED_ACTION_RECOVERIES,
    AgentRunner,
    _announces_unfinished_step,
)
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.utils.runtime import PROMISED_ACTION_PROMPT

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars

_SCREENSHOT_REPLY = (
    "Now I have the reference format. Let me create the comprehensive technical "
    "report for you with your details and images."
)


def _tools(*, has_tools: bool) -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = (
        [{"type": "function", "function": {"name": "exec", "parameters": {}}}] if has_tools else []
    )
    tools.execute = AsyncMock()
    return tools


def _user_text(messages: list[dict]) -> list[str]:
    return [m.get("content") or "" for m in messages if m.get("role") == "user"]


@pytest.mark.parametrize(
    "text",
    [
        _SCREENSHOT_REPLY,
        "Okay. I will refactor the parser next and run the tests.",
        "Next I'll generate the PDF.",
    ],
)
def test_an_announced_action_is_detected(text: str) -> None:
    assert _announces_unfinished_step(text)


@pytest.mark.parametrize(
    "text",
    [
        "The report is ready at /home/user/report.pdf.",
        "Want me to create the report? Let me know which sections matter.",
        "Shall I create it?",
        # The word "I will" early in a long answer does not count: only the tail does.
        "I will explain the design first. " + "The design is sound. " * 60 + "All tests pass.",
        "",
    ],
)
def test_a_finished_or_question_reply_is_not_flagged(text: str) -> None:
    assert not _announces_unfinished_step(text)


@pytest.mark.asyncio
async def test_an_announced_step_gets_a_nudge_and_the_call_is_made() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(side_effect=[
        LLMResponse(content=_SCREENSHOT_REPLY, finish_reason="stop", tool_calls=[], usage={}),
        LLMResponse(content="report written to /workspace/report.docx", finish_reason="stop",
                    tool_calls=[], usage={}),
    ])

    result = await AgentRunner().run(make_run_spec(provider,
        initial_messages=[{"role": "user", "content": "write the technical report"}],
        tools=_tools(has_tools=True),
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert provider.chat_with_retry.await_count == 2
    assert PROMISED_ACTION_PROMPT in _user_text(result.messages)
    # The announcement stays in the transcript, so the model sees what it promised.
    assert any(
        m.get("role") == "assistant" and _SCREENSHOT_REPLY in (m.get("content") or "")
        for m in result.messages
    )
    assert result.final_content == "report written to /workspace/report.docx"
    assert result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_a_turn_without_tools_is_not_nudged() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content=_SCREENSHOT_REPLY, finish_reason="stop", tool_calls=[], usage={},
    ))

    result = await AgentRunner().run(make_run_spec(provider,
        initial_messages=[{"role": "user", "content": "write the report"}],
        tools=_tools(has_tools=False),
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    # With no tool to call, asking for a call would only loop.
    assert provider.chat_with_retry.await_count == 1
    assert PROMISED_ACTION_PROMPT not in _user_text(result.messages)


@pytest.mark.asyncio
async def test_the_nudge_is_bounded_for_a_model_that_never_acts() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content=_SCREENSHOT_REPLY, finish_reason="stop", tool_calls=[], usage={},
    ))

    await AgentRunner().run(make_run_spec(provider,
        initial_messages=[{"role": "user", "content": "write the report"}],
        tools=_tools(has_tools=True),
        model="test-model",
        max_iterations=50,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert provider.chat_with_retry.await_count == 1 + _MAX_PROMISED_ACTION_RECOVERIES


@pytest.mark.asyncio
async def test_a_question_to_the_user_is_not_nudged() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="Should I create the report as DOCX or PDF?", finish_reason="stop",
        tool_calls=[], usage={},
    ))

    result = await AgentRunner().run(make_run_spec(provider,
        initial_messages=[{"role": "user", "content": "write the report"}],
        tools=_tools(has_tools=True),
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert provider.chat_with_retry.await_count == 1
    assert result.final_content == "Should I create the report as DOCX or PDF?"
