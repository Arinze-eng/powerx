from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.command.builtin import cmd_uniabuja
from nanobot.command.router import CommandContext


@pytest.mark.asyncio
async def test_uniabuja_student_command_invokes_gateway_tool_directly() -> None:
    tool = SimpleNamespace(execute=AsyncMock(return_value='[{"regno":"22/205EEE/172"}]'))
    loop = SimpleNamespace(
        tools=SimpleNamespace(get=lambda name: tool if name == "uniabuja_transcript" else None),
        workspace=Path("/tmp/workspace"),
    )
    msg = InboundMessage(
        channel="telegram",
        sender_id="7757072055|allisonarinze",
        chat_id="123",
        content="/uniabuja student 22/205EEE/172",
        metadata={"user_id": 7757072055, "supabase_user_id": "user-id"},
    )
    ctx = CommandContext(msg=msg, session=None, key="telegram:123", raw="/uniabuja", args="student 22/205EEE/172", loop=loop)

    result = await cmd_uniabuja(ctx)

    assert result is not None
    assert "22/205EEE/172" in result.content
    tool.execute.assert_awaited_once_with(action="student_lookup", regno="22/205EEE/172")


@pytest.mark.asyncio
async def test_uniabuja_command_returns_usage_without_arguments() -> None:
    loop = SimpleNamespace(
        tools=SimpleNamespace(get=lambda _name: None),
        workspace=Path("/tmp/workspace"),
    )
    msg = InboundMessage(channel="telegram", sender_id="1|student", chat_id="123", content="/uniabuja")
    ctx = CommandContext(msg=msg, session=None, key="telegram:123", raw="/uniabuja", args="", loop=loop)

    result = await cmd_uniabuja(ctx)

    assert result is not None
    assert "/uniabuja student" in result.content
