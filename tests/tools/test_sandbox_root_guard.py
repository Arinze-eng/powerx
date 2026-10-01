"""The wrong-workspace-root guard on the sandbox tool.

Measured turn (2026-10-01): the model ran ``cd /home/ubuntu/workspace`` — the
Freestyle root — while the active backend was Novita, whose root is
``/workspace``. ``set -e`` ended the command on the failing ``cd``, so the
project the user asked to deploy was never created; the ``web_dev deploy`` that
followed had nothing to ship and the turn ended in a refusal.

The command still fails. What the guard adds is the reason, inside the tool
result the model always reads.
"""

from __future__ import annotations

import pytest

from nanobot.agent.tools import novita_sandbox as nb
from nanobot.agent.tools.base import ToolResult


def test_a_foreign_root_in_the_command_is_named() -> None:
    assert (
        nb._wrong_workspace_root("cd /home/ubuntu/workspace && ls", "novita")
        == "/home/ubuntu/workspace"
    )
    assert nb._wrong_workspace_root("cd /workspace/app && npm run build", "novita") is None
    assert nb._wrong_workspace_root("ls -la && du -sh app", "novita") is None
    assert nb._wrong_workspace_root("", "novita") is None


def test_a_nested_root_is_not_a_mix_up() -> None:
    # upstash's root sits under novita's, and vice versa: naming either is not
    # the mistake this guard is for.
    assert nb._wrong_workspace_root("cd /workspace/home/app", "novita") is None
    assert nb._wrong_workspace_root("cd /workspace/app", "upstash") is None


def test_an_unknown_backend_gets_no_guess() -> None:
    assert nb._wrong_workspace_root("cd /workspace", "vps") is None


def test_the_note_names_the_active_root_and_the_retry() -> None:
    out = nb._annotate_foreign_workspace_root(
        "run",
        {"command": "set -e\ncd /home/ubuntu/workspace\nrm -rf app\nmkdir -p app-tmp"},
        "novita",
        "cd: no such file or directory: /home/ubuntu/workspace\n[exit_code=1]",
    )
    assert isinstance(out, str)
    assert "/home/ubuntu/workspace" in out
    assert "does not exist in this sandbox" in out
    assert "The active backend is novita" in out
    # The point of the note: the model must not report the project as missing.
    assert "never created here" in out


def test_a_successful_command_is_left_alone() -> None:
    text = "total 0\n[exit_code=0]"
    assert (
        nb._annotate_foreign_workspace_root(
            "run", {"command": "ls /home/ubuntu/workspace"}, "novita", text
        )
        == text
    )


def test_an_unrelated_failure_is_left_alone() -> None:
    text = "boom\n[exit_code=1]"
    assert (
        nb._annotate_foreign_workspace_root(
            "run", {"command": "npm run build"}, "novita", text
        )
        == text
    )


def test_silence_when_nothing_says_the_command_failed() -> None:
    text = "wrote 12 bytes"
    assert (
        nb._annotate_foreign_workspace_root(
            "run", {"command": "cat /home/ubuntu/workspace/x"}, "novita", text
        )
        == text
    )


def test_no_such_file_output_is_enough_evidence_on_its_own() -> None:
    out = nb._annotate_foreign_workspace_root(
        "run",
        {"command": "cd /home/ubuntu/workspace"},
        "novita",
        "cd: /home/ubuntu/workspace: No such file or directory",
    )
    assert "does not exist in this sandbox" in out


def test_other_actions_and_errors_are_untouched() -> None:
    error = ToolResult.error("command is required")
    assert (
        nb._annotate_foreign_workspace_root(
            "run", {"command": "cd /home/ubuntu/workspace"}, "novita", error
        )
        is error
    )
    text = "[exit_code=1]"
    assert (
        nb._annotate_foreign_workspace_root(
            "write", {"path": "/home/ubuntu/workspace/a"}, "novita", text
        )
        == text
    )


@pytest.mark.asyncio
async def test_execute_annotates_through_the_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is wired into ``execute``, so every backend gets it."""
    tool = nb.NovitaSandboxTool()
    monkeypatch.setattr(tool, "_selected_backend", lambda: ("novita", None))

    async def fake_dispatch(**_kwargs: object) -> str:
        return "cd: no such file or directory: /home/ubuntu/workspace\n[exit_code=1]"

    monkeypatch.setattr(tool, "_dispatch", fake_dispatch)

    out = await tool.execute(action="run", command="cd /home/ubuntu/workspace")

    assert "The active backend is novita" in str(out)


@pytest.mark.asyncio
async def test_execute_still_returns_a_clean_result_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = nb.NovitaSandboxTool()
    monkeypatch.setattr(tool, "_selected_backend", lambda: ("novita", None))

    async def fake_dispatch(**_kwargs: object) -> str:
        return "hello\n[exit_code=0]"

    monkeypatch.setattr(tool, "_dispatch", fake_dispatch)

    assert await tool.execute(action="run", command="echo hello") == "hello\n[exit_code=0]"
