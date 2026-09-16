"""Regression test: Novita sandboxes must have their workspace directory.

The Novita "base" template ships no ``/workspace``. Passing it as ``cwd`` is a
hard error (``InvalidArgumentException: cwd '/workspace' does not exist``), and
command execution, listing and downloads all pass ``cwd=_WORKSPACE``. Previously
only the fresh-create path prepared the directory, so a sandbox resumed from
pause or reconnected by id could fail every operation until something else
happened to create it — which is what made the bug look intermittent.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanobot.agent.tools.novita_sandbox import _WORKSPACE, NovitaSandboxTool


class _FakeCommands:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_times = fail_times

    def run(self, command: str, **kwargs: Any) -> None:
        self.calls.append((command, kwargs.get("cwd")))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("sandbox not ready")


class _FakeSandbox:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.commands = _FakeCommands(fail_times=fail_times)
        self.killed = False

    def kill(self) -> None:
        self.killed = True


def _tool() -> NovitaSandboxTool:
    # __new__ avoids constructing the full Tool (config, store, clients).
    return NovitaSandboxTool.__new__(NovitaSandboxTool)


def test_ensure_workspace_creates_the_directory_from_root() -> None:
    tool = _tool()
    sandbox = _FakeSandbox()

    tool._ensure_workspace(sandbox)

    assert sandbox.commands.calls == [(f"mkdir -p {_WORKSPACE}", "/")]
    # Must run from "/" — the target directory does not exist yet.
    assert sandbox.commands.calls[0][1] == "/"


def test_ensure_workspace_is_idempotent() -> None:
    """mkdir -p means repeat calls are harmless; it must not raise."""
    tool = _tool()
    sandbox = _FakeSandbox()

    tool._ensure_workspace(sandbox)
    tool._ensure_workspace(sandbox)

    assert len(sandbox.commands.calls) == 2
    assert all(cmd == f"mkdir -p {_WORKSPACE}" for cmd, _ in sandbox.commands.calls)


def test_ensure_workspace_retries_transient_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sandbox still booting must be retried, not failed."""
    import time as time_mod

    monkeypatch.setattr(time_mod, "sleep", lambda _s: None)
    tool = _tool()
    sandbox = _FakeSandbox(fail_times=3)

    tool._ensure_workspace(sandbox)

    # 3 failures + 1 success
    assert len(sandbox.commands.calls) == 4


def test_ensure_workspace_raises_when_unpreparable(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_mod

    monkeypatch.setattr(time_mod, "sleep", lambda _s: None)
    tool = _tool()
    sandbox = _FakeSandbox(fail_times=99)

    with pytest.raises(RuntimeError, match="not ready"):
        tool._ensure_workspace(sandbox)

    # Exhausted its retries rather than giving up on the first failure.
    assert len(sandbox.commands.calls) == 6


def test_workspace_constant_is_absolute() -> None:
    assert _WORKSPACE.startswith("/")
    assert _WORKSPACE == "/workspace"