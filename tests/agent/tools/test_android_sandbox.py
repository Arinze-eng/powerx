"""Tests for the headless Android sandbox tool.

The tool is a thin forwarder, so what is worth testing is the contract it
forwards: the command it builds (a quoting bug here is invisible until it fails
live, on a 1.5 GB install), the version marker the bootstrap verifies, and the
JSON extraction that stands between the CLI's output and the model.
"""

from __future__ import annotations

import json

import pytest

from nanobot.agent.tools.android_sandbox import (
    _ACTIONS,
    _CLI_VERSION,
    AndroidSandboxTool,
    _parse_payload,
    _sh,
    _with_bootstrap,
    bootstrap_command,
    build_cli_command,
)


class TestVersionMarker:
    def test_matches_the_sandbox_cli(self) -> None:
        """The tool refuses to run a CLI that lacks this marker.

        A drift between the two constants does not fail loudly at import time --
        it silently rejects the freshly pushed CLI and keeps running an older
        cached one, which is exactly the failure the marker exists to prevent.
        """
        from pathlib import Path

        source = Path("scripts/android_cli.py").read_text(encoding="utf-8")
        assert f'CLI_VERSION = "{_CLI_VERSION}"' in source


class TestBootstrap:
    def test_pins_a_commit_and_verifies_the_version(self) -> None:
        command = bootstrap_command()
        # Resolves main -> SHA first, because the sandbox's egress path caches
        # raw.githubusercontent.com by PATH; a branch URL can serve a stale file.
        assert "api.github.com/repos/Arinze-eng/powerx/commits/main" in command
        assert "raw.githubusercontent.com/Arinze-eng/powerx/$_sha/scripts" in command
        assert _CLI_VERSION in command
        assert "android_cli.py" in command
        assert "install_android_sandbox.sh" in command

    def test_bootstrap_runs_before_every_command(self) -> None:
        wrapped = _with_bootstrap("python3 $HOME/.android_box/bin/android_cli.py doctor")
        assert wrapped.index("mkdir -p") < wrapped.index("android_cli.py doctor")


class TestQuoting:
    def test_tilde_stays_expandable(self) -> None:
        # '~/app.apk' would reach adb literally and fail as "not found".
        assert _sh("~/app.apk") == '"$HOME"/app.apk'
        assert _sh("~") == '"$HOME"'

    def test_hostile_paths_are_quoted(self) -> None:
        rendered = _sh("/sdcard/a b; rm -rf /.png")
        assert rendered.startswith("'") or rendered.startswith('"')
        assert "rm -rf /" not in rendered.replace("'", "") or ";" not in rendered.split("'")[0]


class TestCommandBuilding:
    def test_install_runs_the_installer(self) -> None:
        assert build_cli_command("install", {}) == "bash $HOME/.android_box/bin/install_android_sandbox.sh"

    def test_status_reads_the_marker_without_starting_work(self) -> None:
        command = build_cli_command("status", {})
        assert ".install.done" in command and "install.log" in command
        assert "bash" not in command

    def test_install_apk_carries_its_flags(self) -> None:
        command = build_cli_command(
            "install_apk", {"apk": "~/app.apk", "reinstall": True, "grant": True}
        )
        assert "--apk" in command and "--reinstall" in command and "--grant" in command
        assert "python3 $HOME/.android_box/bin/android_cli.py install_apk" in command

    def test_install_apk_omits_flags_that_were_not_asked_for(self) -> None:
        command = build_cli_command("install_apk", {"apk": "/tmp/a.apk"})
        assert "--grant" not in command and "--reinstall" not in command

    def test_coordinates_are_ints(self) -> None:
        command = build_cli_command("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration": 500})
        assert "--x1 1" in command and "--y2 4" in command and "--duration 500" in command

    def test_missing_optionals_are_left_out(self) -> None:
        command = build_cli_command("launch", {"package": "com.example"})
        assert command.endswith("launch --package com.example")

    def test_shell_command_survives_metacharacters(self) -> None:
        command = build_cli_command("shell", {"command": "ls -l /sdcard | wc -l"})
        assert "|" in command  # present, and shell-quoted for the sandbox
        assert command.count("'") >= 2

    def test_boolean_flag_only_when_true(self) -> None:
        assert "--wipe-data" in build_cli_command("reset", {"wipe_data": True})
        assert "--wipe-data" not in build_cli_command("reset", {"wipe_data": False})


class TestPayloadExtraction:
    def test_reads_the_json_object(self) -> None:
        rendered = 'noise\n{"ok": true, "state": "device"}\n[exit_code=0]'
        assert _parse_payload(rendered) == {"ok": True, "state": "device"}

    def test_ignores_braces_inside_strings(self) -> None:
        rendered = '{"ok": true, "text": "a } brace", "n": 1}'
        assert _parse_payload(rendered) == {"ok": True, "text": "a } brace", "n": 1}

    def test_survives_a_truncated_object(self) -> None:
        assert _parse_payload('{"ok": true, "x": "unterminated') is None

    def test_returns_none_without_json(self) -> None:
        assert _parse_payload("Traceback (most recent call last): ...") is None


class TestToolSurface:
    def test_name_and_actions(self) -> None:
        tool = AndroidSandboxTool()
        assert tool.name == "android_sandbox"
        schema = tool.parameters
        assert schema["required"] == ["action"]
        assert set(schema["properties"]["action"]["enum"]) == set(_ACTIONS)

    def test_every_action_has_a_deadline(self) -> None:
        from nanobot.agent.tools.android_sandbox import _TIMEOUTS

        assert set(_ACTIONS) <= set(_TIMEOUTS)

    def test_always_registered(self) -> None:
        # Gating in enabled() dropped the tool from the schema entirely, so the
        # model never saw it. Availability belongs at execute() time.
        assert AndroidSandboxTool.enabled(None) is True  # type: ignore[arg-type]

    def test_schema_is_json_serialisable(self) -> None:
        # A single unserialisable tool fails EVERY turn: the whole toolset ships
        # in one payload.
        json.dumps(AndroidSandboxTool().parameters)

    def test_description_states_the_setup_order(self) -> None:
        text = AndroidSandboxTool().description
        assert "doctor" in text and "install" in text and "status" in text
        # The prompt must not offload polling onto the user.
        assert "check back" in text

    @pytest.mark.asyncio
    async def test_unknown_action_is_rejected_without_a_sandbox(self) -> None:
        result = await AndroidSandboxTool().execute(action="explode")
        assert "Unknown action" in str(result)

    @pytest.mark.asyncio
    async def test_missing_sandbox_is_reported_not_crashed(self) -> None:
        result = await AndroidSandboxTool().execute(action="doctor")
        assert "sandbox" in str(result).lower()

    @pytest.mark.asyncio
    async def test_bad_timeout_is_reported(self) -> None:
        result = await AndroidSandboxTool().execute(action="doctor", timeout="soon")
        assert "bad_timeout" in str(result)
