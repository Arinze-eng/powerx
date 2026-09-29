"""Tool-level regressions for the Freestyle backend: it must actually run.

Two defects shipped together on the commit that added the Freestyle action
budget, and together they made a Freestyle sandbox completely unusable the
moment an administrator selected it.

* ``NovitaSandboxTool._execute_freestyle`` carried a stray ``@staticmethod``.
  The dispatcher calls it as ``self._execute_freestyle(action, kwargs, config,
  session_key)``, so the decorator bound ``action`` to the function's ``self``
  parameter and left ``session_key`` unfilled. Every Freestyle action — run,
  read, write, list, upload, install, fetch_url, reset — raised

      TypeError: NovitaSandboxTool._execute_freestyle() missing 1 required
      positional argument: 'session_key'

  before it touched a VM, which is exactly the reported "LLM can't use it".
  ``_execute_tenki`` and every sibling are plain instance methods; only this
  one carried the decorator.

* The GitHub credentials source-prefix was ``. <file> 2>/dev/null || true``.
  ``.`` is a POSIX *special* built-in, so on a guest whose ``/bin/sh`` is dash
  (i.e. a stock Ubuntu Freestyle VM) a missing file exits the shell immediately
  — before ``|| true`` can run — and the guest answers rc=2 with no output.
  The file is only written when a GitHub token is configured, so every
  deployment without one had every command come back empty and failed.

``tests/tools/test_tenki_tool_guards.py`` already had a Freestyle dispatch test,
but it monkeypatched ``_execute_freestyle`` away, which is precisely why neither
defect was caught. These tests drive the REAL method with a fake HTTP plane.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import re
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger as loguru_logger

from nanobot.agent.tools.freestyle_backend import FreestyleExecutionBackend
from nanobot.agent.tools.novita_sandbox import (
    NovitaSandboxTool,
    _clean_install_package_names,
    _git_creds_source_for,
)

SESSIONS = "/home/ubuntu/workspace"
KEY = "WUjYgZ9kifZmmLqXNTWvfM-JBMG5zrj7x4EsEga6pC1ZievE94SPp9bLVS2BzRmJauV"


class _Recorder:
    """The smallest faithful stand-in for the Freestyle HTTP plane."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.vm: dict[str, Any] | None = None

    def install(self, backend: FreestyleExecutionBackend) -> None:
        async def _request(
            method: str,
            path: str,
            *,
            lane: int | None = None,
            json_body: Any | None = None,
            params: dict[str, Any] | None = None,
            data: bytes | None = None,
            expect: tuple[int, ...] = (200, 201, 204),
            timeout: float = 120,
            raw: bool = False,
        ) -> Any:
            del lane, expect, data, raw
            if method == "POST" and path == "/v5/vms":
                self.vm = {
                    "id": "vm-0-1",
                    "slug": (json_body or {}).get("slug"),
                    "state": "running",
                    "resources": {"cpu": 4, "memory": 8192, "storage": 32768},
                }
                return dict(self.vm)
            if method == "GET" and path == "/v5/vms":
                slug = (params or {}).get("slug")
                rows = [] if slug and self.vm is None else ([self.vm] if self.vm else [])
                rows = [dict(r) for r in rows if r and (slug is None or r["slug"] == slug)]
                return {"vms": rows, "totalCount": len(rows), "runningCount": len(rows)}
            if path.endswith("/exec-await"):
                command = str((json_body or {}).get("command") or "")
                self.commands.append(command)
                return {"statusCode": 0, "stdout": f"ran: {command}", "stderr": ""}
            if method == "GET" and path.startswith("/v5/vms/vm-"):
                return dict(self.vm or {})
            return {}

        backend._request = _request  # type: ignore[assignment]


def _freestyle_tool(monkeypatch: pytest.MonkeyPatch) -> tuple[NovitaSandboxTool, _Recorder]:
    execution = SimpleNamespace(
        backend="freestyle",
        freestyle=SimpleNamespace(api_key="", api_keys=[KEY]),
    )
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))

    backend = FreestyleExecutionBackend(
        SimpleNamespace(
            api_key="",
            api_keys=[KEY],
            memory_mb=8192,
            cpu_cores=4,
            disk_size_gb=0,
            max_duration_seconds=3600,
            idle_pause_seconds=300,
            tag="powerx",
            snapshot_id="",
            fetch_allow_hosts="",
            persist_workspace=True,
        ),
        sandbox_name="px-fs-dispatch",
    )
    recorder = _Recorder()
    recorder.install(backend)
    monkeypatch.setattr(
        NovitaSandboxTool, "_freestyle_backend", lambda self, config, key: backend
    )
    # A GitHub token on the test host would make the real seeding path run
    # mkdir/write against the stub; the source prefix is added either way, which
    # is the part under test.
    monkeypatch.setattr(
        "nanobot.agent.tools.novita_sandbox._git_creds_script", lambda: ""
    )
    return NovitaSandboxTool(), recorder


def test_the_dispatcher_can_call_execute_freestyle_with_four_arguments() -> None:
    """The exact shape of the shipped bug, asserted structurally.

    A ``@staticmethod`` here binds the first positional argument to ``self`` and
    leaves ``session_key`` unfilled, so the call the dispatcher makes raises
    ``TypeError``. Naming the parameters keeps the failure obvious.
    """
    descriptor = inspect.getattr_static(NovitaSandboxTool, "_execute_freestyle")
    assert inspect.isfunction(descriptor), (
        "_execute_freestyle must stay an instance method: a staticmethod makes "
        "every Freestyle action raise TypeError before it reaches the VM"
    )
    assert list(inspect.signature(descriptor).parameters)[:5] == [
        "self",
        "action",
        "kwargs",
        "config",
        "session_key",
    ]


def test_execute_reaches_the_real_freestyle_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``action=run`` must come back with output, not a TypeError."""
    tool, recorder = _freestyle_tool(monkeypatch)

    result = asyncio.run(tool.execute(action="run", command="echo hi"))

    assert "echo hi" in str(result), f"the Freestyle dispatch returned {result!r}"
    assert "ran: " in str(result)
    assert "TypeError" not in str(result)


def test_a_run_command_is_prefixed_by_the_credential_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, recorder = _freestyle_tool(monkeypatch)

    asyncio.run(tool.execute(action="run", command="pwd && echo hi"))

    assert recorder.commands, "no command reached the VM"
    # ``[ -r ]``, not ``[ -f ]``: an existing-but-unreadable credential file is
    # just as fatal to a non-interactive dash as a missing one (see the guard).
    assert recorder.commands[-1].startswith("[ -r "), recorder.commands[-1]
    assert recorder.commands[-1].endswith("pwd && echo hi")


def test_write_and_read_reach_the_real_freestyle_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every action went through the same broken call, not just ``run``."""
    tool, _ = _freestyle_tool(monkeypatch)

    written = asyncio.run(tool.execute(action="write", path="a.txt", content="hello"))
    assert "in the Freestyle VM" in str(written)
    assert "Tenki" not in str(written)
    assert isinstance(asyncio.run(tool.execute(action="read", path="a.txt")), str)


def test_the_creds_prefix_cannot_kill_a_posix_shell(tmp_path: Path) -> None:
    """The measured dash failure: a missing file must be a no-op, not an exit.

    ``sh -c '. /nope 2>/dev/null || true; echo A'`` prints nothing and exits 2
    on dash, because sourcing is a POSIX special built-in and its failure ends a
    non-interactive shell on the spot.
    """
    missing = str(tmp_path / "workspace")
    prefix = _git_creds_source_for(missing)

    proc = subprocess.run(
        ["/bin/sh", "-c", prefix + "echo alive"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "alive"


def test_the_creds_prefix_still_sources_a_file_that_exists(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / ".nanobot").mkdir(parents=True)
    (root / ".nanobot" / "github-env.sh").write_text("export GITHUB_TOKEN=ok\n")

    proc = subprocess.run(
        ["/bin/sh", "-c", _git_creds_source_for(str(root)) + 'echo "$GITHUB_TOKEN"'],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout.strip() == "ok"


def test_the_creds_guard_asks_for_a_readable_file() -> None:
    """``[ -f ]`` was the wrong predicate: it asks whether the file is *there*.

    MEASURED FAILURE #2 (2026-09-29): the deployed service left a
    ``-rw------- root root`` credential file inside an ``ubuntu``-owned
    workspace. The file existed, so ``[ -f ]`` passed, ``.`` was attempted, and
    dash aborted the whole non-interactive shell with rc=2 and no output — for
    every command, including a bare ``pwd``. The source prefix must therefore
    test *readability* (which also covers absence), must refuse a file whose
    contents cannot parse, and must never decide the command's own exit status.
    """
    prefix = _git_creds_source_for("/workspace")

    assert "[ -r " in prefix, prefix
    assert "sh -n " in prefix, prefix
    assert "[ -f " not in prefix, prefix
    assert prefix.endswith("|| true; "), prefix


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root reads a 0600 file regardless of mode, so the failure cannot be staged",
)
def test_the_creds_prefix_survives_an_unreadable_file(tmp_path: Path) -> None:
    """The exact deployed scenario, staged: the file exists but cannot be read."""
    root = tmp_path / "workspace"
    (root / ".nanobot").mkdir(parents=True)
    creds = root / ".nanobot" / "github-env.sh"
    creds.write_text("export GITHUB_TOKEN=ok\n")
    creds.chmod(0o000)

    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", _git_creds_source_for(str(root)) + "pwd && echo alive"],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        creds.chmod(0o600)

    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert "alive" in proc.stdout, proc.stdout


def test_the_creds_prefix_survives_a_corrupt_credential_file(tmp_path: Path) -> None:
    """A half-written source aborts the shell too, so the guard parses it first."""
    root = tmp_path / "workspace"
    (root / ".nanobot").mkdir(parents=True)
    (root / ".nanobot" / "github-env.sh").write_text("export GITHUB_TOKEN='unclosed\nif [\n")

    proc = subprocess.run(
        ["/bin/sh", "-c", _git_creds_source_for(str(root)) + "echo alive"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.strip() == "alive"


def test_the_creds_prefix_never_decides_the_commands_exit_status(tmp_path: Path) -> None:
    prefix = _git_creds_source_for(str(tmp_path / "workspace"))

    proc = subprocess.run(
        ["/bin/sh", "-c", prefix + "exit 7"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 7, (proc.returncode, proc.stdout, proc.stderr)


class _SeedBackend:
    """A backend whose ``run`` really executes, so the seed is really exercised.

    ``write`` is left able to produce an unreadable file on purpose: the
    deployed writer did exactly that, and the whole bug was that the seeding
    path trusted it. ``STUBBED`` names the commands the guest refuses (chmod,
    sudo) so that the file stays unreadable for the whole verify — a test that
    runs as the file's own owner cannot otherwise reproduce a file that neither
    ``chmod`` nor ``chown`` can rescue.
    """

    STUBBED = ("chmod", "sudo")

    def __init__(self, root: Path, *, mode: int = 0o600, stub_unrepairable: bool = False) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.mode = mode
        self.stub_unrepairable = stub_unrepairable
        self.commands: list[str] = []
        self.stubdir = root / "stubbin"
        if stub_unrepairable:
            self.stubdir.mkdir(parents=True, exist_ok=True)
            for name in self.STUBBED:
                stub = self.stubdir / name
                stub.write_text("#!/bin/sh\nexit 1\n")
                stub.chmod(0o755)

    async def run(self, command: str, timeout: int | None = None, cwd: str | None = None) -> str:
        self.commands.append(command)
        env = {"PATH": f"{self.stubdir}:/usr/bin:/bin", "HOME": str(self.root)}
        proc = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return f"statusCode={proc.returncode}\n{proc.stdout}{proc.stderr}"
        return proc.stdout

    async def write(self, path: str, content: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(self.mode)


def _seed(backend: _SeedBackend, root: Path) -> None:
    # ``_seed_git_credentials`` never touches ``self``, so it can be driven
    # without building a configured tool (which would load real settings).
    asyncio.run(NovitaSandboxTool._seed_git_credentials(None, backend, str(root)))


def _stub_creds_script(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nanobot.agent.tools.novita_sandbox._git_creds_script",
        lambda: "#!/bin/sh\nexport GITHUB_TOKEN=stub\n",
    )


def test_seeding_verifies_that_the_file_it_wrote_is_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_creds_script(monkeypatch)
    root = tmp_path / "workspace"
    backend = _SeedBackend(root)

    _seed(backend, root)

    path = root / ".nanobot" / "github-env.sh"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    verify = backend.commands[-1]
    assert "[ -r " in verify
    assert "sudo -n chown" in verify
    assert "rm -f " in verify


def test_seeding_removes_a_credential_file_nothing_can_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credential file nobody can read authenticates nothing and kills every
    command, so the seeder deletes it and says so instead of leaving a trap."""
    _stub_creds_script(monkeypatch)
    root = tmp_path / "workspace"
    backend = _SeedBackend(root, mode=0o000, stub_unrepairable=True)

    warnings: list[str] = []
    handler = loguru_logger.add(
        lambda message: warnings.append(message.record["message"]), level="WARNING"
    )
    try:
        _seed(backend, root)
    finally:
        loguru_logger.remove(handler)

    path = root / ".nanobot" / "github-env.sh"
    assert not path.exists(), "an unreadable credential file must not be left behind"
    assert any("not readable" in message for message in warnings), warnings
    # and the sandbox still works, which is the entire point
    proc = subprocess.run(
        ["/bin/sh", "-c", _git_creds_source_for(str(root)) + "echo alive"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.strip() == "alive"


def test_only_one_freestyle_session_store_is_defined() -> None:
    """The class was defined twice; the second silently shadowed the first.

    A duplicate is invisible at runtime — it just replaces the earlier
    definition — so a source-level guard is the only thing that would catch it
    coming back.
    """
    source = Path("nanobot/agent/tools/novita_sandbox.py").read_text(encoding="utf-8")
    assert len(re.findall(r"^class _FreestyleSessionStore\b", source, re.M)) == 1
    assert len(re.findall(r"^_FREESTYLE_STORE = _FreestyleSessionStore\(\)$", source, re.M)) == 1


# --------------------------------------------------------------------------
# action=install read only ``packages``, so a call shaped like ``run`` died.
#
# The deployed instance's Northflank log shows the exact failure:
#
#   Tool call: novita_sandbox({"action": "install", "command": "nmap"})
#   File "/app/nanobot/agent/tools/novita_sandbox.py", line 3696,
#     in _execute_freestyle_inner
#       result = await backend.install_packages(packages, timeout=timeout)
#                   ...  └ []
#   ValueError: no valid package names supplied
#   ERROR | - | Freestyle VM operation failed
#
# ``packages`` was empty because the packages rode in on ``command`` — the key
# ``run`` takes — and every backend raises ValueError for an empty list. The
# exception left the tool as an opaque "VM operation failed", so the task's
# package install could never succeed.
# --------------------------------------------------------------------------


def _vm_ran(recorder: _Recorder, needle: str) -> bool:
    """``run`` records several execs (script, code, log, cleanup)."""
    return any(needle in command for command in recorder.commands)


def test_install_reads_packages_from_the_run_style_command_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact shipped call shape: {"action": "install", "command": "nmap"}."""
    tool, recorder = _freestyle_tool(monkeypatch)

    result = asyncio.run(tool.execute(action="install", command="nmap"))

    assert "no valid package names" not in str(result), result
    assert "installation result" in str(result), result
    assert _vm_ran(recorder, "install -y -qq nmap"), recorder.commands


def test_install_unwraps_a_whole_apt_line_sent_as_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Models paste the command they would have run, not bare names."""
    tool, recorder = _freestyle_tool(monkeypatch)

    asyncio.run(
        tool.execute(
            action="install",
            command="sudo apt-get update && sudo apt-get install -y nmap curl",
        )
    )

    # The tool rebuilt the line from the package names alone.
    assert _vm_ran(recorder, "install -y -qq nmap curl"), recorder.commands


def test_clean_install_package_names_keeps_names_and_drops_command_traffic() -> None:
    assert _clean_install_package_names("nmap curl") == ["nmap", "curl"]
    assert _clean_install_package_names("nmap,curl") == ["nmap", "curl"]
    assert _clean_install_package_names(["nmap", "curl jq"]) == ["nmap", "curl", "jq"]
    assert _clean_install_package_names("sudo apt-get install -y nmap") == ["nmap"]
    assert _clean_install_package_names("DEBIAN_FRONTEND=noninteractive nmap") == ["nmap"]
    assert _clean_install_package_names("libc6-dev python3-pip g++") == [
        "libc6-dev",
        "python3-pip",
        "g++",
    ]
    assert _clean_install_package_names("") == []
    assert _clean_install_package_names(None) == []
    assert _clean_install_package_names("&& || ; |") == []
    # A shell metacharacter-laden payload must not smuggle an operator through.
    assert _clean_install_package_names("nmap && curl") == ["nmap", "curl"]
    assert _clean_install_package_names("nmap;rm -rf /") == ["nmap", "rm"]


def test_install_without_any_package_names_returns_a_usable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never raise: tell the model which argument it should have used."""
    tool, recorder = _freestyle_tool(monkeypatch)

    result = asyncio.run(tool.execute(action="install"))

    assert getattr(result, "is_error", False), repr(result)
    assert "packages" in str(result)
    assert "command" in str(result)
    assert not recorder.commands, "an empty install still touched the VM"


def test_a_declared_packages_argument_still_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, recorder = _freestyle_tool(monkeypatch)

    asyncio.run(tool.execute(action="install", packages="jq,ripgrep"))

    assert _vm_ran(recorder, "install -y -qq jq ripgrep"), recorder.commands


def test_the_live_tool_schema_exposes_packages() -> None:
    """The schema the MODEL sees has to offer ``packages``, or install is uncallable.

    ``@tool_parameters`` rebinds ``cls.parameters`` after the class body runs, so
    a parameter declared only in the class-body dict never reaches a provider.
    ``packages`` was missing from the live schema, which left ``action=install``
    with no legal way to name a package: the model sent the one key it had
    (``command``), the handler read only ``packages``, and the install died with
    "no valid package names supplied" — the failure in the Northflank log.
    """
    props = NovitaSandboxTool().parameters["properties"]

    assert "packages" in props, sorted(props)
    assert "install" in props["command"]["description"]
    assert "install" in props["packages"]["description"]


def test_the_shadowed_parameters_dict_mirrors_the_live_schema() -> None:
    """One schema in two places: keep the decorator and the dead property in step."""
    live = set(NovitaSandboxTool().parameters["properties"])
    source = Path("nanobot/agent/tools/novita_sandbox.py").read_text(encoding="utf-8")

    declared: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.ClassDef) and node.name == "NovitaSandboxTool"):
            continue
        for item in node.body:
            if not (isinstance(item, ast.FunctionDef) and item.name == "parameters"):
                continue
            for sub in ast.walk(item):
                if not isinstance(sub, ast.Dict):
                    continue
                keys = [k.value for k in sub.keys if isinstance(k, ast.Constant)]
                if "properties" not in keys:
                    continue
                props = sub.values[keys.index("properties")]
                declared = {k.value for k in props.keys if isinstance(k, ast.Constant)}

    assert declared, "the class-body parameters dict was not found"
    assert declared == live, f"only in one copy: {declared ^ live}"
