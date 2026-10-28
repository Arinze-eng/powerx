from __future__ import annotations

import asyncio
import re
import shlex
import sys

import pytest

from nanobot.agent.tools.exec_session import ExecSessionManager
from nanobot.agent.tools.shell import ExecTool


def _python(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(code)}"


def _session_id(result: str) -> str:
    match = re.search(r"session_id: ([0-9a-f]{12})", result)
    assert match, result
    return match.group(1)


def test_resolve_timeout_policy() -> None:
    tool = ExecTool(timeout=60)

    assert tool._resolve_timeout(None) == 600
    assert tool._resolve_timeout(120) == 120
    assert tool._resolve_timeout(9999) == tool._MAX_TIMEOUT


def test_resolve_timeout_keeps_large_config_and_zero_means_unlimited() -> None:
    assert ExecTool(timeout=3600)._resolve_timeout(None) == 3600
    assert ExecTool(timeout=0)._resolve_timeout(None) is None


def test_default_commands_hand_off_after_config_budget() -> None:
    tool = ExecTool(timeout=60)

    prepared = tool._prepare_command("python3 -c 'print(1)'", None, None, None, None)
    assert not isinstance(prepared, str)
    assert prepared.timeout == 600
    assert prepared.background_after_s == 60


def test_explicit_timeout_is_a_hard_kill_without_handoff() -> None:
    tool = ExecTool(timeout=60)

    prepared = tool._prepare_command("python3 -c 'print(1)'", None, 120, None, None)
    assert not isinstance(prepared, str)
    assert prepared.timeout == 120
    assert prepared.background_after_s is None


def test_unlimited_config_has_no_handoff() -> None:
    tool = ExecTool(timeout=0)

    prepared = tool._prepare_command("python3 -c 'print(1)'", None, None, None, None)
    assert not isinstance(prepared, str)
    assert prepared.timeout is None
    assert prepared.background_after_s is None


@pytest.mark.asyncio
async def test_quick_command_returns_one_shot_result_without_session() -> None:
    tool = ExecTool(timeout=60, session_manager=ExecSessionManager())

    result = await tool.execute(command="echo quick-done")

    assert "quick-done" in result
    assert "Exit code: 0" in result
    assert "session_id" not in result


@pytest.mark.asyncio
async def test_slow_command_is_handed_off_and_finishes_in_session() -> None:
    manager = ExecSessionManager()
    tool = ExecTool(timeout=1, session_manager=manager)

    result = await tool.execute(
        command=_python("import time; time.sleep(2); print('finished-late')")
    )

    assert "moved to a background session" in result
    session_id = _session_id(result)

    poll = await manager.write(
        session_id=session_id,
        chars=None,
        close_stdin=False,
        terminate=False,
        yield_time_ms=10_000,
        max_output_chars=10_000,
    )

    assert poll.done
    assert poll.exit_code == 0
    assert "finished-late" in poll.output
    await manager.close_all()


@pytest.mark.asyncio
async def test_handed_off_command_is_killed_at_hard_deadline(monkeypatch) -> None:
    monkeypatch.setattr(ExecTool, "_MAX_TIMEOUT", 2)
    manager = ExecSessionManager()
    tool = ExecTool(timeout=1, session_manager=manager)

    result = await tool.execute(command=_python("import time; time.sleep(30)"))
    assert "moved to a background session" in result
    session_id = _session_id(result)

    # Nobody polls during the deadline; the watchdog must still kill the process.
    await asyncio.sleep(3.5)
    poll = await manager.write(
        session_id=session_id,
        chars=None,
        close_stdin=False,
        terminate=False,
        yield_time_ms=1000,
        max_output_chars=10_000,
    )

    assert poll.done
    assert poll.timed_out
    await manager.close_all()
