"""The shell tool must be able to run its command in the execution sandbox.

``ExecTool`` spawned every command against the gateway itself. The container is
0.2 vCPU with a 488 MiB ceiling the process already sits at 78-90% of, so a
compile or an install competes with the agent loop for the same two tenths of a
core and its page cache is charged to the cgroup that decides whether the
container survives.

These tests pin the policy layer around the offload, not the transport: the
transport is ``workspace_bridge.run_remote``, which already runs on every
backend. What matters here is that the path is opt-in, that it never silently
runs a command somewhere other than where the caller thinks, and that the guards
still run before it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nanobot.agent.tools import sandbox_shell
from nanobot.agent.tools.sandbox_shell import (
    SHELL_SANDBOX_ENV,
    SandboxShellError,
    map_remote_cwd,
    run_in_sandbox,
    sandbox_shell_enabled,
)
from nanobot.agent.tools.shell import ExecTool


class FakeExecutor:
    """A resolved sandbox handle. ``available`` is all the policy layer reads."""

    def __init__(self, name: str = "freestyle", *, available: bool = True) -> None:
        self.name = name
        self._available = available

    @property
    def available(self) -> bool:
        return self._available


def _install_remote(monkeypatch, *, available=True, remote_root="/home/ubuntu/workspace"):
    """Point the policy layer at a fake sandbox and record what it asked to run."""
    ran: list[tuple[str, int | None]] = []
    executor = FakeExecutor(available=available)

    async def _resolve(session_key=None):  # noqa: ANN001 - test double
        return executor

    async def _root(executor, *, session_key=None):  # noqa: ANN001 - test double
        return remote_root

    async def _run(command, *, timeout=120, executor=None):  # noqa: ANN001 - test double
        ran.append((command, timeout))
        return True, "remote output"

    monkeypatch.setattr(sandbox_shell, "resolve_remote_executor", _resolve)
    monkeypatch.setattr(sandbox_shell, "executor_workspace_root", _root)
    monkeypatch.setattr(sandbox_shell, "run_remote", _run)
    return ran


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #


def test_the_path_is_off_unless_asked(monkeypatch) -> None:
    """Every existing turn must behave exactly as it did before this existed."""
    monkeypatch.delenv(SHELL_SANDBOX_ENV, raising=False)
    assert sandbox_shell_enabled() is False

    monkeypatch.setenv(SHELL_SANDBOX_ENV, "sandbox")
    assert sandbox_shell_enabled() is True


def test_an_unset_gate_never_touches_the_sandbox(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(SHELL_SANDBOX_ENV, raising=False)
    ran = _install_remote(monkeypatch)

    async def run() -> str:
        return await ExecTool(working_dir=str(tmp_path), timeout=5).execute(command="echo hi")

    result = asyncio.run(run())

    assert ran == [], "the sandbox was consulted with the gate off"
    assert "hi" in result
    assert "sandbox" not in result


# --------------------------------------------------------------------------- #
# mapping the working directory
# --------------------------------------------------------------------------- #


def test_a_workspace_path_maps_onto_the_sandbox_workspace() -> None:
    assert (
        map_remote_cwd("/home/user/powerx/app", "/home/user/powerx", "/home/ubuntu/workspace")
        == "/home/ubuntu/workspace/app"
    )


def test_the_workspace_root_itself_maps_to_the_sandbox_root() -> None:
    assert (
        map_remote_cwd("/home/user/powerx", "/home/user/powerx", "/home/ubuntu/workspace")
        == "/home/ubuntu/workspace"
    )


def test_a_path_outside_the_workspace_does_not_map() -> None:
    """There is no faithful translation, so the caller must refuse."""
    assert map_remote_cwd("/etc", "/home/user/powerx", "/home/ubuntu/workspace") is None


def test_an_unknown_remote_root_does_not_map() -> None:
    """No root means no anchor, and a guess would run the command in '/'."""
    assert map_remote_cwd("/home/user/powerx", "/home/user/powerx", None) is None


def test_a_missing_workspace_root_does_not_map() -> None:
    assert map_remote_cwd("/home/user/powerx", None, "/home/ubuntu/workspace") is None


# --------------------------------------------------------------------------- #
# the offload itself
# --------------------------------------------------------------------------- #


def test_an_enabled_gate_runs_the_command_in_the_sandbox(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SHELL_SANDBOX_ENV, "1")
    ran = _install_remote(monkeypatch)

    async def run() -> str:
        return await ExecTool(working_dir=str(tmp_path), timeout=5).execute(command="echo hi")

    result = asyncio.run(run())

    assert len(ran) == 1, "the command did not reach the sandbox"
    command, _timeout = ran[0]
    assert command.startswith("cd "), "the cwd must be carried into the sandbox"
    assert command.endswith("echo hi")
    assert "remote output" in result
    assert "ran in the sandbox" in result, "the caller must be able to tell where it ran"


def test_the_remote_command_carries_the_mapped_directory(tmp_path, monkeypatch) -> None:
    """A subdirectory of the workspace maps to the matching sandbox subdirectory.

    The tool's own ``working_dir`` is the workspace root, so a per-call
    ``working_dir`` is what puts the command somewhere below it.
    """
    monkeypatch.setenv(SHELL_SANDBOX_ENV, "1")
    ran = _install_remote(monkeypatch)
    workspace = tmp_path / "ws"
    (workspace / "app").mkdir(parents=True)

    async def run() -> str:
        tool = ExecTool(working_dir=str(workspace), timeout=5)
        return await tool.execute(command="pwd", working_dir=str(workspace / "app"))

    asyncio.run(run())

    assert ran, "nothing was sent to the sandbox"
    assert "cd /home/ubuntu/workspace/app && pwd" == ran[0][0]


def test_a_command_at_the_workspace_root_runs_at_the_sandbox_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SHELL_SANDBOX_ENV, "1")
    ran = _install_remote(monkeypatch)

    async def run() -> str:
        return await ExecTool(working_dir=str(tmp_path), timeout=5).execute(command="pwd")

    asyncio.run(run())

    assert ran[0][0] == "cd /home/ubuntu/workspace && pwd"


def test_an_unreachable_sandbox_falls_back_to_the_host(tmp_path, monkeypatch) -> None:
    """A transport problem is not a reason to fail a command that has a home."""
    monkeypatch.setenv(SHELL_SANDBOX_ENV, "1")
    ran = _install_remote(monkeypatch, available=False)

    async def run() -> str:
        return await ExecTool(working_dir=str(tmp_path), timeout=5).execute(command="echo hi")

    result = asyncio.run(run())

    assert ran == []
    assert "hi" in result
    assert "ran in the sandbox" not in result


def test_an_unmappable_directory_is_refused_not_fallen_back(tmp_path, monkeypatch) -> None:
    """Running it against the host workspace would answer a different question."""
    monkeypatch.setenv(SHELL_SANDBOX_ENV, "1")
    ran = _install_remote(monkeypatch)

    async def run() -> str:
        tool = ExecTool(working_dir=str(tmp_path), timeout=5)
        return await tool.execute(command="pwd", working_dir="/etc")

    result = asyncio.run(run())

    assert ran == [], "a command with no faithful directory was still run somewhere"
    assert result.startswith("Error:")


# --------------------------------------------------------------------------- #
# what must be refused rather than silently moved
# --------------------------------------------------------------------------- #


def test_a_yield_time_ms_session_is_refused(tmp_path, monkeypatch) -> None:
    """The session_id names a local process; no backend exposes one."""
    monkeypatch.setenv(SHELL_SANDBOX_ENV, "1")
    ran = _install_remote(monkeypatch)

    async def run() -> str:
        tool = ExecTool(working_dir=str(tmp_path), timeout=5)
        return await tool.execute(command="echo hi", yield_time_ms=100)

    result = asyncio.run(run())

    assert ran == [], "a pid-bound session was shipped to the sandbox"
    assert result.startswith("Error:")
    assert "yield_time_ms" in result


def test_a_bwrap_wrapped_command_is_refused(tmp_path, monkeypatch) -> None:
    """The wrapper carries host bind mounts that do not exist in the sandbox."""
    monkeypatch.setenv(SHELL_SANDBOX_ENV, "1")
    ran = _install_remote(monkeypatch)

    async def run() -> str:
        tool = ExecTool(working_dir=str(tmp_path), timeout=5, sandbox="bwrap")
        return await tool.execute(command="echo hi")

    result = asyncio.run(run())

    assert ran == [], "a host bwrap invocation was shipped to the sandbox"
    assert result.startswith("Error:")


# --------------------------------------------------------------------------- #
# the guards still come first
# --------------------------------------------------------------------------- #


def test_the_guards_still_block_before_the_offload(tmp_path, monkeypatch) -> None:
    """The sandbox path must not become a way around the deny list."""
    monkeypatch.setenv(SHELL_SANDBOX_ENV, "1")
    ran = _install_remote(monkeypatch)

    async def run() -> str:
        tool = ExecTool(working_dir=str(tmp_path), timeout=5)
        return await tool.execute(command="rm -rf /")

    result = asyncio.run(run())

    assert ran == [], "a denied command reached the sandbox"
    assert result.startswith("Error:")


# --------------------------------------------------------------------------- #
# the seam
# --------------------------------------------------------------------------- #


def test_run_in_sandbox_reports_an_unreachable_sandbox_as_degradable(monkeypatch) -> None:
    _install_remote(monkeypatch, available=False)

    with pytest.raises(SandboxShellError) as caught:
        asyncio.run(run_in_sandbox("echo hi"))

    assert caught.value.degradable is True


def test_run_in_sandbox_reports_an_unmappable_directory_as_final(monkeypatch) -> None:
    _install_remote(monkeypatch)

    with pytest.raises(SandboxShellError) as caught:
        asyncio.run(run_in_sandbox("echo hi", host_cwd="/etc", workspace_root="/home/user/x"))

    assert caught.value.degradable is False


def test_the_session_key_is_read_on_the_calling_thread(monkeypatch) -> None:
    """The resolver is a lookup of live in-process handles keyed by this value.

    A worker thread has no request context, and ``novita_sandbox._session_key()``
    answers the literal ``"unknown"`` there -- which resolves to no handle at all,
    so the sandbox would read as unreachable.
    """
    seen: list[object] = []
    monkeypatch.setattr(sandbox_shell, "current_request_session_key", lambda: "cli:one")

    assert sandbox_shell.calling_session_key() == "cli:one"
    seen.append("cli:one")
    assert seen == ["cli:one"]


def test_the_module_is_not_imported_for_its_side_effects() -> None:
    """Importing it must not enable anything or reach for a backend."""
    assert Path(sandbox_shell.__file__).name == "sandbox_shell.py"
    assert SHELL_SANDBOX_ENV == "NANOBOT_SHELL_SANDBOX"
