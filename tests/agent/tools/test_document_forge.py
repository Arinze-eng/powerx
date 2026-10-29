"""Tests for the ``document_forge`` sandbox tool.

Offline, against a fake sandbox. The CLI itself is exercised for real in the
sandbox; these tests pin the things that break on the host: argument
marshalling, the bootstrap's version gate, the background contract, and the
difference between "the engine refused" and "the engine never ran".
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from nanobot.agent.tools.document_forge import (
    _CLI_VERSION,
    _SPEC_DIR,
    _SPILL_THRESHOLD_CHARS,
    DocumentForgeTool,
    bootstrap_command,
    build_cli_command,
    spill_commands,
    stage_spec,
)
from nanobot.agent.tools.engineering_draw import _parse_payload

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_SCRIPT = REPO_ROOT / "scripts" / "document_forge_cli.py"


class FakeSandbox:
    name = "novita_sandbox"

    def __init__(self, response: str = "", error: Exception | None = None) -> None:
        self.commands: list[str] = []
        self.timeouts: list[int] = []
        self._response = response
        self._error = error

    async def execute(self, **kwargs):
        self.commands.append(kwargs["command"])
        self.timeouts.append(kwargs.get("timeout"))
        if self._error is not None:
            raise self._error
        return self._response


class FakeContext:
    def __init__(self, sandbox=None) -> None:
        self.tool_registry = {"novita_sandbox": sandbox} if sandbox else {}


def _tool(sandbox):
    return DocumentForgeTool(FakeContext(sandbox))


async def _run(sandbox, **kwargs):
    return await _tool(sandbox).execute(**kwargs)


def _payload_text(obj: dict) -> str:
    return "bootstrap noise\n" + json.dumps(obj) + "\n"


# --------------------------------------------------------------------------- #
# Version gate
# --------------------------------------------------------------------------- #
def test_cli_version_matches_the_sandbox_cli() -> None:
    text = CLI_SCRIPT.read_text(encoding="utf-8")
    assert f'CLI_VERSION = "{_CLI_VERSION}"' in text, (
        "document_forge.py's _CLI_VERSION and scripts/document_forge_cli.py's "
        "CLI_VERSION have diverged; bump both together."
    )


def test_bootstrap_pins_a_commit_sha_and_gates_on_the_version() -> None:
    command = bootstrap_command()
    assert "api.github.com/repos/Arinze-eng/powerx/commits/main" in command
    assert "document_forge_cli.py" in command
    assert _CLI_VERSION in command
    assert "grep -q" in command


# --------------------------------------------------------------------------- #
# Command marshalling
# --------------------------------------------------------------------------- #
def test_doctor_and_job_commands() -> None:
    assert build_cli_command("doctor", {}).endswith(" doctor")
    cmd = build_cli_command("job", {"job_id": "0a8cbd692b6b"})
    assert cmd.endswith("job --job-id 0a8cbd692b6b")


def test_install_is_always_detached() -> None:
    assert build_cli_command("install", {}).endswith("install --background")


def test_create_serialises_a_dict_spec_and_quotes_it() -> None:
    spec = {"title": 'It\'s a "test"', "blocks": [{"type": "paragraph", "text": "hi $HOME"}]}
    cmd = build_cli_command("create", {"spec": spec, "to": "docx,pdf"})
    assert "create --spec" in cmd
    assert "--to docx,pdf" in cmd
    # Round-trip the quoted argument the way a POSIX shell would.
    import shlex

    tokens = shlex.split(cmd)
    spec_token = tokens[tokens.index("--spec") + 1]
    assert json.loads(spec_token) == spec


def test_create_takes_a_spec_path_unchanged() -> None:
    cmd = build_cli_command("create", {"spec": "~/specs/report.json"})
    assert "--spec '~/specs/report.json'" in cmd or "--spec ~/specs/report.json" in cmd


def test_convert_passes_file_target_and_sheet() -> None:
    cmd = build_cli_command(
        "convert", {"file": "~/document_outputs/book.xlsx", "to": "csv", "sheet": "Q1"}
    )
    assert "convert --file" in cmd
    assert "--to csv" in cmd
    assert "--sheet Q1" in cmd


def test_image_fetch_uses_url_and_crop_uses_file() -> None:
    fetch = build_cli_command("image", {"op": "fetch", "url": "https://example.com/a.png"})
    assert "--op fetch" in fetch and "--url" in fetch and "--file" not in fetch
    crop = build_cli_command("image", {"op": "crop", "file": "a.png", "crop": "16:9"})
    assert "--op crop" in crop and "--crop 16:9" in crop


def test_image_resize_passes_width_px_as_an_integer() -> None:
    cmd = build_cli_command("image", {"op": "resize", "file": "a.png", "width_px": "320"})
    assert "--width-px 320" in cmd


def test_insert_passes_position_and_width() -> None:
    cmd = build_cli_command(
        "insert",
        {"file": "r.docx", "image": "logo.png", "position": "after:Summary", "width_cm": 4},
    )
    assert "--image logo.png" in cmd
    assert "--position 'after:Summary'" in cmd or "--position after:Summary" in cmd
    assert "--width-cm 4.0" in cmd


def test_background_flag_is_forwarded() -> None:
    cmd = build_cli_command("create", {"spec": {"blocks": []}, "background": True})
    assert cmd.endswith("--background")


def test_chart_forwards_spec_and_out_dir() -> None:
    cmd = build_cli_command("chart", {"spec": {"labels": ["a"]}, "out_dir": "~/charts"})
    assert "chart --spec" in cmd and "--out-dir" in cmd


# --------------------------------------------------------------------------- #
# Execution contract
# --------------------------------------------------------------------------- #
def test_missing_sandbox_is_reported_without_claiming_success() -> None:
    import asyncio

    result = asyncio.run(_tool(None).execute(action="doctor"))
    assert "sandbox" in str(result).lower()
    assert getattr(result, "is_error", False) is True


@pytest.mark.asyncio
async def test_unknown_action_is_rejected() -> None:
    result = await _run(FakeSandbox(), action="explode")
    assert "Unknown action" in str(result)


@pytest.mark.asyncio
async def test_execute_wraps_the_command_in_the_bootstrap() -> None:
    sandbox = FakeSandbox(_payload_text({"ok": True, "action": "doctor"}))
    await _run(sandbox, action="doctor")
    assert sandbox.commands and "document_forge_cli.py" in sandbox.commands[0]
    assert "api.github.com" in sandbox.commands[0]
    assert sandbox.commands[0].rstrip().endswith("1>&2") or "tail -c 400" in sandbox.commands[0]


@pytest.mark.asyncio
async def test_execute_returns_the_engine_payload_verbatim() -> None:
    payload = {"ok": True, "action": "convert", "outputs": [{"path": "/x/a.pdf"}]}
    result = await _run(
        FakeSandbox(_payload_text(payload)), action="convert", file="a.docx", to="pdf"
    )
    assert json.loads(str(result)) == payload


@pytest.mark.asyncio
async def test_engine_refusal_is_marked_as_an_error() -> None:
    refusal = {"ok": False, "action": "image", "error": "fetch_failed", "message": "404"}
    result = await _run(FakeSandbox(_payload_text(refusal)), action="image", op="fetch", url="x")
    assert getattr(result, "is_error", False) is True
    assert json.loads(str(result))["error"] == "fetch_failed"


@pytest.mark.asyncio
async def test_no_json_is_distinguished_from_a_refusal() -> None:
    result = await _run(FakeSandbox("bootstrap failed, no output"), action="create", spec={})
    body = json.loads(str(result))
    assert body["error"] == "no_json_from_engine"
    assert getattr(result, "is_error", False) is True


@pytest.mark.asyncio
async def test_sandbox_exception_is_reported_as_retryable() -> None:
    result = await _run(FakeSandbox(error=RuntimeError("boom")), action="doctor")
    assert "could not run" in str(result)
    assert "Retry" in str(result)


@pytest.mark.asyncio
async def test_non_numeric_timeout_is_rejected() -> None:
    result = await _run(FakeSandbox(), action="doctor", timeout="soon")
    assert json.loads(str(result))["error"] == "bad_timeout"


@pytest.mark.asyncio
async def test_timeout_is_capped() -> None:
    sandbox = FakeSandbox(_payload_text({"ok": True}))
    await _run(sandbox, action="doctor", timeout=99999)
    assert sandbox.timeouts == [900]


@pytest.mark.asyncio
async def test_install_is_forced_to_background_and_tells_the_model_to_poll() -> None:
    sandbox = FakeSandbox(
        _payload_text({"ok": True, "action": "install", "job_id": "abc123abc123"})
    )
    await _run(sandbox, action="install", background=False)
    assert "install --background" in sandbox.commands[0]


def test_every_enum_is_a_list_of_plain_strings() -> None:
    schema = DocumentForgeTool(None).parameters
    for name, prop in schema["properties"].items():
        if "enum" in prop:
            assert isinstance(prop["enum"], list), name
            assert all(isinstance(v, str) for v in prop["enum"]), name


def test_the_tool_is_always_enabled_and_named() -> None:
    tool = DocumentForgeTool(None)
    assert DocumentForgeTool.enabled(FakeContext()) is True
    assert tool.name == "document_forge"
    assert re.fullmatch(r"[a-z_]+", tool.name)


def test_payload_parser_reads_the_cli_last_line() -> None:
    text = "Matplotlib is building the font cache\n" + json.dumps({"ok": True, "action": "chart"})
    assert _parse_payload(text) == {"ok": True, "action": "chart"}


def test_missing_docx_returns_json_not_a_traceback(tmp_path) -> None:
    """A lazy ``from docx import ...`` must surface as missing_dependency JSON."""
    import subprocess
    import sys

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"title": "t", "blocks": [{"type": "paragraph", "text": "x"}]}))
    # ``sys.modules[name] = None`` makes any import of it raise ImportError.
    bootstrap = (
        "import sys, runpy; sys.modules['docx'] = None; "
        "sys.argv = ['document_forge_cli', 'create', '--spec', sys.argv[1], "
        "'--out-dir', sys.argv[2]]; "
        "runpy.run_path(%r, run_name='__main__')" % str(CLI_SCRIPT)
    )
    env = {"DOCUMENT_FORGE_HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [sys.executable, "-c", bootstrap, str(spec), str(tmp_path / "out")],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert "Traceback" not in proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"] == "missing_dependency"
    assert proc.returncode == 1


# --------------------------------------------------------------------------- #
# Large specs are staged to a file, not pushed through the command line
# --------------------------------------------------------------------------- #
MAX_COMMAND_CHARS = 12_000  # the sandbox refuses anything longer


class LocalSandbox:
    """Runs the staging commands for real in a temp dir; canned output otherwise.

    ``/workspace`` is rewritten to a temp root so the file operations land on disk
    where the test can read them back.
    """

    name = "novita_sandbox"

    def __init__(self, root: Path, payload: str = "", check_override: str | None = None):
        self.root = root
        self.commands: list[str] = []
        self._payload = payload
        self._check_override = check_override

    async def execute(self, **kwargs):
        command = kwargs["command"]
        self.commands.append(command)
        if "base64 -d" in command or "wc -c" in command:
            if "wc -c" in command and self._check_override is not None:
                return self._check_override
            local = command.replace("/workspace", str(self.root))
            done = subprocess.run(["bash", "-c", local], capture_output=True, text=True, timeout=60)
            return done.stdout + done.stderr
        return self._payload


def _big_spec(chars: int = 60_000) -> str:
    """A spec of roughly ``chars`` characters with quotes, backslashes and unicode."""
    blocks: list[dict] = []
    size = 0
    while size < chars:
        block = {"type": "paragraph", "text": f"Line {len(blocks)}: \"q\" 'x' \\ $HOME é ✓\n\ttab"}
        blocks.append(block)
        size += len(json.dumps(block, ensure_ascii=False)) + 2
    return json.dumps({"title": "Big report", "blocks": blocks}, ensure_ascii=False)


def test_spill_commands_stay_under_the_command_cap(tmp_path) -> None:
    data = bytes(range(256)) * 2000  # 512,000 bytes, every byte value
    commands = spill_commands(data, f"{tmp_path}/x.json")
    assert len(commands) > 1
    assert all(len(c) < MAX_COMMAND_CHARS for c in commands)


@pytest.mark.asyncio
async def test_staged_spec_reproduces_the_input_exactly(tmp_path) -> None:
    text = _big_spec(90_000)
    sandbox = LocalSandbox(tmp_path)
    path = await stage_spec(sandbox, text)
    assert path.startswith(_SPEC_DIR + "/") and path.endswith(".json")
    on_disk = (tmp_path / path[len("/workspace/") :]).read_bytes()
    assert on_disk == text.encode("utf-8")
    assert all(len(c) < MAX_COMMAND_CHARS for c in sandbox.commands)


@pytest.mark.asyncio
async def test_big_create_spec_is_passed_as_a_staged_path(tmp_path) -> None:
    text = _big_spec()
    assert len(text) > _SPILL_THRESHOLD_CHARS
    sandbox = LocalSandbox(tmp_path, payload=_payload_text({"ok": True, "action": "create"}))
    result = await _run(sandbox, action="create", spec=text, to="docx")
    assert not result.is_error
    final = sandbox.commands[-1]
    assert "create --spec /workspace/.document_forge/specs/" in final.replace("'", "")
    assert all(len(c) < MAX_COMMAND_CHARS for c in sandbox.commands)
    staged = re.search(r"/workspace/\.document_forge/specs/[0-9a-f]+\.json", final)
    assert staged is not None
    assert (tmp_path / staged.group(0)[len("/workspace/") :]).read_text(encoding="utf-8") == text


@pytest.mark.asyncio
async def test_short_spec_stays_inline(tmp_path) -> None:
    spec = {"title": "Short", "blocks": [{"type": "paragraph", "text": "hi"}]}
    sandbox = LocalSandbox(tmp_path, payload=_payload_text({"ok": True, "action": "create"}))
    await _run(sandbox, action="create", spec=spec)
    assert not any("base64 -d" in c for c in sandbox.commands)
    assert "create --spec" in sandbox.commands[-1]
    assert "specs/" not in sandbox.commands[-1]


@pytest.mark.asyncio
async def test_a_failed_size_check_refuses_to_run_the_engine(tmp_path) -> None:
    sandbox = LocalSandbox(tmp_path, check_override="0\n", payload=_payload_text({"ok": True}))
    result = await _run(sandbox, action="create", spec=_big_spec())
    assert result.is_error
    body = json.loads(str(result))
    assert body["error"] == "spec_staging_failed"
    assert not any("create --spec" in c for c in sandbox.commands)


@pytest.mark.asyncio
async def test_a_sandbox_error_while_staging_is_reported_not_raised(tmp_path) -> None:
    class Broken(LocalSandbox):
        async def execute(self, **kwargs):
            if "base64 -d" in kwargs["command"]:
                raise RuntimeError("sandbox offline")
            return await super().execute(**kwargs)

    result = await _run(Broken(tmp_path), action="chart", spec=_big_spec())
    assert result.is_error
    body = json.loads(str(result))
    assert body["error"] == "spec_staging_failed"
    assert "sandbox offline" in body["detail"]
