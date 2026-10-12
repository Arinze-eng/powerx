"""Provisioning the sandbox: the step that makes a dispatched turn possible.

The worker cannot run until the package is on the box, and the sandbox images do
not ship it. These tests cover the three things that has to be true for the
feature to work rather than to silently fall back forever:

  1. the provisioning command is built and chained in front of the worker,
  2. it is pinned to a commit, not to a branch that a cache can serve stale,
  3. a provisioning failure still lets an already-provisioned box run the turn.

The last one is the asymmetry that matters: provisioning is a refresh, not a
precondition. Turning a failed refresh into a failed turn would take a working
sandbox offline for a GitHub hiccup.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nanobot.remote_turn import bootstrap
from nanobot.remote_turn.bootstrap import (
    BOOTSTRAP_ENV,
    SANDBOX_HOME,
    SOURCE_DIR,
    VENV_DIR,
    bootstrap_enabled,
    dispatch_command,
    provision_command,
    worker_command,
)
from nanobot.remote_turn.dispatcher import SandboxTurnDispatcher
from nanobot.remote_turn.protocol import (
    ProviderCredentials,
    RemoteTurnEvent,
    RemoteTurnRequest,
    encode_event,
)

_INSTALLER = Path(__file__).resolve().parents[2] / "scripts" / "install_nanobot_sandbox.sh"


@pytest.fixture(autouse=True)
def _no_ambient_bootstrap_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test inherits a bootstrap setting from the environment."""
    monkeypatch.delenv(BOOTSTRAP_ENV, raising=False)


# --------------------------------------------------------------- the switch


def test_bootstrap_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defaulting on is the only safe default: a TTL'd sandbox is recycled, so a
    # box that is not re-provisioned is a turn that cannot run.
    assert bootstrap_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "skip", " 0 "])
def test_the_switch_can_turn_provisioning_off(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(BOOTSTRAP_ENV, value)

    assert bootstrap_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", ""])
def test_anything_else_leaves_it_on(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(BOOTSTRAP_ENV, value)

    assert bootstrap_enabled() is True


# ------------------------------------------------------------- the commands


def test_provisioning_is_pinned_to_a_commit_not_a_branch() -> None:
    # The sandbox's egress caches raw.githubusercontent.com by path, so a branch
    # URL has been measured serving a revision several pushes old. Resolving
    # main to a SHA first is what stops the sandbox running stale code.
    command = provision_command()

    assert "api.github.com/repos/" in command
    assert "commits/main" in command
    assert "raw.githubusercontent.com" in command
    # The SHA is interpolated into the raw URL at run time.
    assert "/$_sha/scripts" in command


def test_provisioning_keeps_a_branch_fallback_for_when_the_api_is_down() -> None:
    command = provision_command()

    assert bootstrap.RAW_BASE in command
    # A fetch that cannot be parsed as shell is rejected rather than executed.
    assert "sh -n" in command


def test_provisioning_and_the_worker_are_chained_best_effort() -> None:
    command = dispatch_command("/tmp/nanobot_turn_request.json")

    assert "install_nanobot_sandbox.sh" in command
    assert "-m nanobot.remote_turn.worker /tmp/nanobot_turn_request.json" in command
    # Provisioning comes first.
    assert command.index("install_nanobot_sandbox.sh") < command.index("-m nanobot")
    # And it is closed as `);` -- best-effort, not gated. `&&` there would stop an
    # already-provisioned box from running the turn whenever a refresh failed,
    # which is exactly how the dispatcher avoids treating a failed provision.
    assert ");" in command, "the provisioning step is not best-effort"
    assert ") &&" not in command
    # The worker is what follows the provisioning group.
    assert command.rpartition(");")[2].lstrip().startswith("PYTHONPATH")


def test_the_provisioning_step_cannot_kill_the_worker() -> None:
    """The failure that would make a GitHub hiccup drop a live turn.

    ``provision_command`` ends in ``exit 1`` when it cannot fetch the installer.
    Unwrapped, that ``exit`` terminates the whole shell before the worker starts,
    so a *refresh* failure would silently become a *turn* failure -- the one
    outcome provisioning must never cause. The snippet is therefore wrapped in a
    subshell, and this test is what keeps it wrapped.
    """
    command = dispatch_command("/tmp/req.json")

    # The provisioning block must be opened with a subshell...
    assert "( " in command
    # ...and the worker must sit outside it, after the group closes.
    worker_at = command.index("-m nanobot.remote_turn.worker")
    assert command.index(");") < worker_at == command.rindex("-m nanobot")


def test_the_provisioning_snippet_does_not_leak_into_the_worker_environment() -> None:
    # Same reason the subshell is there: its helper variables (_sha, _ok, _inst)
    # are shell-local and must not travel into the worker's process environment.
    command = dispatch_command("/tmp/req.json")

    assert command.count("(") == command.count(")")


def test_turning_the_switch_off_leaves_only_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BOOTSTRAP_ENV, "0")

    command = dispatch_command("/tmp/req.json")

    assert "install_nanobot_sandbox.sh" not in command
    assert "-m nanobot.remote_turn.worker /tmp/req.json" in command


def test_the_worker_prefers_the_provisioned_venv() -> None:
    command = worker_command("/tmp/req.json")

    assert f"{VENV_DIR}/bin/python" in command
    assert "python3" in command  # the fallback, for a self-provisioned box
    assert f"PYTHONPATH={SOURCE_DIR}" in command


def test_the_installer_lands_under_the_sandbox_home() -> None:
    assert bootstrap.INSTALLER_PATH.startswith(SANDBOX_HOME)


# --------------------------------------------------------- the installer file


def test_the_installer_is_shipped_with_the_code() -> None:
    # The installer is fetched *from this repo*, so it has to be in it. A missing
    # file would make every provisioning step fail at run time, which is exactly
    # the class of bug (a script absent from the deployment) the Dockerfile's COPY
    # comments keep re-learning.
    assert _INSTALLER.is_file(), f"missing {_INSTALLER}"
    assert _INSTALLER.stat().st_size > 1000


def test_the_installer_parses_as_shell() -> None:
    shell = shutil.which("bash") or shutil.which("sh")
    if shell is None:  # pragma: no cover - a box with no shell at all
        pytest.skip("no shell available")

    result = subprocess.run(
        [shell, "-n", str(_INSTALLER)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_the_installer_never_prints_to_stdout() -> None:
    # The dispatcher parses the worker's stdout as JSONL. The installer runs on
    # that same command line, so a bare print from it would be indistinguishable
    # from a turn result.
    text = _INSTALLER.read_text(encoding="utf-8")

    assert ">&2" in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("echo "):
            assert ">&2" in stripped, f"stdout leak: {stripped}"


# ------------------------------------------------------- through a dispatch


class _Executor:
    def __init__(self, name: str = "novita") -> None:
        self.name = name

    @property
    def available(self) -> bool:
        return True


def _request() -> RemoteTurnRequest:
    return RemoteTurnRequest(
        session_key="telegram:7",
        messages=[{"role": "user", "content": "hi"}],
        credentials=ProviderCredentials(provider="deepseek", model="deepseek-chat"),
    )


def _final() -> str:
    return encode_event(
        RemoteTurnEvent(
            kind="final",
            text="done",
            data={"messages": [{"role": "assistant", "content": "done"}]},
        )
    )


def _stub(monkeypatch: pytest.MonkeyPatch, *, provision_ok: bool) -> list[str]:
    """Record every remote command; answer the provision step per *provision_ok*."""
    from nanobot.agent.tools import workspace_bridge

    commands: list[str] = []

    async def _write(files: Any, remote_dir: str, *, executor: Any = None) -> tuple[bool, str]:
        return True, "ok"

    async def _run(command: str, *, timeout: int = 120, executor: Any = None) -> tuple[bool, str]:
        commands.append(command)
        if "-m nanobot.remote_turn.worker" in command:
            return True, _final()
        return provision_ok, "" if provision_ok else "github unreachable"

    monkeypatch.setattr(workspace_bridge, "write_files_to_sandbox", _write)
    monkeypatch.setattr(workspace_bridge, "run_remote", _run)
    return commands


def _dispatcher() -> SandboxTurnDispatcher:
    async def _factory() -> Any:
        return _Executor()

    return SandboxTurnDispatcher(timeout_s=30, executor_factory=_factory)


async def test_a_dispatch_provisions_before_it_runs_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = _stub(monkeypatch, provision_ok=True)

    outcome = await _dispatcher().dispatch(_request())

    assert outcome.ok is True
    assert len(commands) == 2
    assert "install_nanobot_sandbox.sh" in commands[0]
    assert "-m nanobot.remote_turn.worker" in commands[1]


async def test_a_failed_provision_still_runs_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Provisioning is a refresh. A sandbox an earlier turn already provisioned
    # must keep working when GitHub is briefly unreachable -- turning a failed
    # refresh into a failed turn would take a healthy sandbox offline.
    commands = _stub(monkeypatch, provision_ok=False)

    outcome = await _dispatcher().dispatch(_request())

    assert outcome.ok is True
    assert len(commands) == 2
    assert "-m nanobot.remote_turn.worker" in commands[1]


async def test_the_switch_off_skips_provisioning_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BOOTSTRAP_ENV, "0")
    commands = _stub(monkeypatch, provision_ok=True)

    outcome = await _dispatcher().dispatch(_request())

    assert outcome.ok is True
    assert len(commands) == 1
    assert "-m nanobot.remote_turn.worker" in commands[0]
