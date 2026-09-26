"""Tests for the ``engineering_draw`` sandbox tool.

Everything here runs offline against a fake sandbox. The live run is a separate
exercise against a real Novita sandbox, because the things that actually break
in production -- the OpenCASCADE install, the projection, a DXF that opens in a
real reader -- cannot be proved against a stub.

The cases chosen are the ones where this tool has historically gone wrong:
argument marshalling, the bootstrap's version gate, and the difference between
"the engine said no" and "the engine never ran".
"""

from __future__ import annotations

import json

import pytest

from nanobot.agent.tools.engineering_draw import (
    _CLI_VERSION,
    _parse_payload,
    _sandbox_tool,
    bootstrap_command,
    build_cli_command,
)


class FakeSandbox:
    """Minimal stand-in for the sandbox tool this forwards to."""

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
    from nanobot.agent.tools.engineering_draw import EngineeringDrawTool

    return EngineeringDrawTool(FakeContext(sandbox))


async def _run(sandbox, **kwargs):
    return await _tool(sandbox).execute(**kwargs)


# --------------------------------------------------------------------------- #
# Sandbox resolution
# --------------------------------------------------------------------------- #
def test_sandbox_tool_resolves_by_name() -> None:
    sandbox = FakeSandbox()
    assert _sandbox_tool(FakeContext(sandbox)) is sandbox


def test_sandbox_tool_prefers_novita_over_the_others() -> None:
    novita, runloop = FakeSandbox(), FakeSandbox()
    novita.name, runloop.name = "novita_sandbox", "runloop_sandbox"
    ctx = FakeContext()
    ctx.tool_registry = {"runloop_sandbox": runloop, "novita_sandbox": novita}
    assert _sandbox_tool(ctx) is novita


def test_sandbox_tool_survives_a_registry_without_get() -> None:
    """A registry exposing only iteration must still resolve the sandbox.

    This is the shape that once silently dropped the tool: the lookup raised, the
    exception was swallowed, and the model was told no sandbox was configured even
    though one was.
    """

    class IterableOnly:
        def __init__(self, tools):
            self._tools = tools

        def __iter__(self):
            return iter(self._tools)

    sandbox = FakeSandbox()
    ctx = FakeContext()
    ctx.tool_registry = IterableOnly([sandbox])
    assert _sandbox_tool(ctx) is sandbox


def test_sandbox_tool_returns_none_without_a_context() -> None:
    assert _sandbox_tool(None) is None


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #
def test_bootstrap_pins_a_commit_sha_and_gates_on_the_version() -> None:
    """A cached raw.github response must not be able to serve an old CLI.

    The sandbox caches raw.githubusercontent.com by path, so a branch URL can keep
    returning a revision several pushes old. The bootstrap therefore resolves main
    to a SHA through the API and refuses any file that does not carry the
    CLI_VERSION this tool requires.
    """
    command = bootstrap_command()
    assert "api.github.com/repos/Arinze-eng/powerx/commits/main" in command
    assert "raw.githubusercontent.com/Arinze-eng/powerx/$_sha/scripts" in command
    # The version gate itself: grep for the marker inside the fetch helper.
    assert "CLI_VERSION = " in command
    assert _CLI_VERSION in command
    # The date cache-buster must be double-quoted so the sandbox shell expands it.
    assert "'$(date +%s)'" not in command


def test_cli_version_matches_the_sandbox_cli() -> None:
    """The host tool and the sandbox CLI must agree, or the gate never passes.

    Read from the file rather than restated, because the failure mode is exactly
    the two drifting apart: the tool would demand a version the CLI does not carry
    and every call would fail the bootstrap check.
    """
    from pathlib import Path

    cli = Path(__file__).resolve().parents[3] / "scripts" / "engineering_draw_cli.py"
    text = cli.read_text()
    assert f'CLI_VERSION = "{_CLI_VERSION}"' in text, (
        "engineering_draw.py's _CLI_VERSION and scripts/engineering_draw_cli.py's "
        "CLI_VERSION have diverged; bump both together."
    )


# --------------------------------------------------------------------------- #
# Payload extraction
# --------------------------------------------------------------------------- #
def test_parse_payload_takes_the_last_balanced_object() -> None:
    """The sandbox wrapper appends [exit_code=N] and can interleave log lines."""
    rendered = (
        "log line with a {brace} in it\n"
        '{"ok": true, "action": "doctor"}\n'
        "[exit_code=0]"
    )
    assert _parse_payload(rendered) == {"ok": True, "action": "doctor"}


def test_parse_payload_ignores_braces_inside_strings() -> None:
    rendered = '{"ok": true, "note": "a { in a string } is not a brace"}'
    assert _parse_payload(rendered) == {
        "ok": True,
        "note": "a { in a string } is not a brace",
    }


def test_parse_payload_returns_none_for_no_json() -> None:
    assert _parse_payload("command not found") is None


# --------------------------------------------------------------------------- #
# Command building
# --------------------------------------------------------------------------- #
def test_build_cli_command_serialises_a_spec_and_quotes_it() -> None:
    command = build_cli_command("draw", {"spec": {"entities": [{"type": "line"}]}, "name": "flange"})
    assert "engineering_draw_cli.py draw" in command
    assert "--spec" in command and "--name flange" in command
    # The JSON must be shell-quoted, or the sandbox's shell eats the braces.
    assert "\"entities\"" not in command or "'" in command


def test_build_cli_command_joins_format_lists() -> None:
    command = build_cli_command("model", {"formats": ["step", "stl", "3mf"]})
    assert "--formats step,stl,3mf" in command


def test_build_cli_command_reaches_the_freecad_engine() -> None:
    """FreeCAD is the default engine, so its two actions must be reachable."""
    command = build_cli_command(
        "freecad",
        {"code": "b = doc.addObject('Part::Box', 'P')", "formats": ["step", "dxf"],
         "name": "bracket", "timeout": 600},
    )
    assert " freecad " in command
    assert "--formats step,dxf" in command
    assert "--name bracket" in command
    assert "--timeout 600" in command, "the engine's own deadline must travel too"


def test_build_cli_command_asks_for_the_viewport_render_by_itself() -> None:
    """Asking for a view IS asking for the render.

    A caller that passes ``preview_view`` and forgets ``preview`` would otherwise
    get a CAD window and no picture of it, which is the half of the request the
    user can actually see.
    """
    command = build_cli_command("freecad_gui", {"preview_view": "iso"})
    assert "--preview-view iso" in command
    assert "--preview png" in command


def test_build_cli_command_does_not_double_the_preview_flag() -> None:
    command = build_cli_command("freecad_gui", {"preview": "svg", "preview_view": "top"})
    assert command.count("--preview ") == 1
    assert "--preview svg" in command
    assert "--preview-view top" in command


def test_build_cli_command_keeps_freecad_only_flags_out_of_the_other_actions() -> None:
    """``--timeout`` is a FreeCAD flag; leaking it into another action is a bad request."""
    command = build_cli_command("draw", {"timeout": 600, "preview_view": "iso"})
    assert "--timeout" not in command
    assert "--preview-view" not in command


def test_build_cli_command_passes_a_code_snippet_through() -> None:
    command = build_cli_command("model", {"code": "result = Box(50,30,10) - Cylinder(6,40)"})
    assert "--code" in command
    assert "Box(50,30,10)" in command


def test_build_cli_command_takes_a_spec_path_unchanged() -> None:
    command = build_cli_command("model", {"spec": "/tmp/part.json"})
    assert "--spec /tmp/part.json" in command


def test_build_cli_command_install_runs_the_repo_installer() -> None:
    """Install must go through the installer, not a bare pip line.

    The installer is what does the apt-then-verify dance; a bare ``pip install``
    would report success while build123d still could not import.
    """
    command = build_cli_command("install", {})
    assert "install_engineering_draw.sh" in command
    assert "pip install" not in command


def test_build_cli_command_status_reads_the_marker_and_log() -> None:
    command = build_cli_command("status", {})
    assert ".install.done" in command
    assert "install.log" in command


# --------------------------------------------------------------------------- #
# execute()
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_execute_reports_a_missing_sandbox_without_claiming_success() -> None:
    result = await _run(None, action="draw")
    assert result.is_error
    assert "sandbox" in str(result).lower()


@pytest.mark.asyncio
async def test_execute_rejects_an_unknown_action() -> None:
    result = await _run(FakeSandbox(), action="sculpt")
    assert result.is_error
    assert "Unknown action" in str(result)


@pytest.mark.asyncio
async def test_execute_wraps_the_command_in_the_bootstrap() -> None:
    sandbox = FakeSandbox('{"ok": true, "action": "doctor", "ready": true}')
    await _run(sandbox, action="doctor")
    command = sandbox.commands[0]
    assert "commits/main" in command, "the bootstrap must run before the CLI"
    assert "engineering_draw_cli.py doctor" in command


@pytest.mark.asyncio
async def test_execute_returns_the_engine_payload_verbatim() -> None:
    payload = {"ok": True, "action": "model", "measured": {"volume_mm3": 12345.6}}
    sandbox = FakeSandbox(json.dumps(payload))
    result = await _run(sandbox, action="model", spec={"kind": "box", "length": 10, "width": 10, "height": 10})
    assert not result.is_error
    assert json.loads(str(result)) == payload


@pytest.mark.asyncio
async def test_execute_marks_an_engine_refusal_as_an_error() -> None:
    """A ``{"ok": false}`` from the CLI must surface as an error, not as output.

    Otherwise the agent reads a named failure (a bad field, a missing dependency)
    as if the action had produced something, and reports a drawing that does not
    exist.
    """
    payload = {"ok": False, "error": "a rect needs its size", "next": "Pass p1/p2."}
    sandbox = FakeSandbox(json.dumps(payload))
    result = await _run(sandbox, action="draw")
    assert result.is_error
    assert "a rect needs its size" in str(result)


@pytest.mark.asyncio
async def test_execute_distinguishes_no_json_from_a_refusal() -> None:
    """No JSON means the harness/bootstrap failed, not that the request was bad."""
    sandbox = FakeSandbox("bash: python3: command not found")
    result = await _run(sandbox, action="doctor")
    assert result.is_error
    payload = json.loads(str(result))
    assert payload["error"] == "no_json_from_engine"
    assert "install" in payload["next"]


@pytest.mark.asyncio
async def test_execute_reports_a_sandbox_exception_as_retryable() -> None:
    sandbox = FakeSandbox(error=RuntimeError("sandbox busy"))
    result = await _run(sandbox, action="model")
    assert result.is_error
    assert "sandbox busy" in str(result)


@pytest.mark.asyncio
async def test_execute_rejects_a_non_numeric_timeout() -> None:
    result = await _run(FakeSandbox(), action="model", timeout="soon")
    assert result.is_error
    assert json.loads(str(result))["error"] == "bad_timeout"


@pytest.mark.asyncio
async def test_install_starts_detached_and_tells_the_model_to_poll() -> None:
    """The install outlives any single sandbox command, so it must not block.

    And the model must be told to poll ``status`` itself -- handing that job to
    the user is the failure this guards against.
    """
    sandbox = FakeSandbox("install_started")
    result = await _run(sandbox, action="install")
    assert not result.is_error
    assert "nohup" in sandbox.commands[0]
    payload = json.loads(str(result))
    assert payload["started"] is True
    assert "status" in payload["note"]
    assert "Do not tell the user to check back" in payload["note"]


@pytest.mark.asyncio
async def test_status_does_not_report_json_it_does_not_have() -> None:
    sandbox = FakeSandbox("--- install.done ---\nready")
    result = await _run(sandbox, action="status")
    assert not result.is_error
    assert "ready" in json.loads(str(result))["sandbox_report"]


# --------------------------------------------------------------------------- #
# Schema safety
# --------------------------------------------------------------------------- #
def test_every_enum_is_a_list_of_plain_strings() -> None:
    """A non-string enum makes some gateways reject the ENTIRE request.

    All tools ship in one payload, so one bad tool breaks every turn: the agent
    sees only "upstream error" and nothing points at the culprit.
    """
    tool = _tool(FakeSandbox())
    schema = tool.parameters

    def walk(node, path="parameters"):
        if isinstance(node, dict):
            if "enum" in node:
                assert isinstance(node["enum"], list), f"{path}.enum is not a list"
                for value in node["enum"]:
                    assert isinstance(value, str), f"{path}.enum has a non-string {value!r}"
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(schema)
    assert schema["properties"]["action"]["enum"] == [
        "doctor", "install", "status", "freecad", "freecad_gui", "model", "draw",
        "project", "section", "inspect", "export",
    ]
