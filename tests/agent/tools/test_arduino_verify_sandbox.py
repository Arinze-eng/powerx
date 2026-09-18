"""Tests for the sandbox dispatch path of arduino_verify.

Uses a fake sandbox object so the compile/simulate-in-sandbox wiring is covered
without any network access. The live Novita run is exercised manually against a
real sandbox (see docs/arduino-verification.md).
"""

from __future__ import annotations

import json

from nanobot.agent.tools.arduino_verify import (
    SANDBOX_SETUP_CMD,
    sandbox_install_command,
    verify_in_sandbox,
)

HEX_JSON = {
    "ok": True,
    "firmware_bytes": 3260,
    "sim_ms": 9000,
    "serial": "TRAFFIC LIGHT: ready\r\nGREEN: go\r\nYELLOW: caution\r\nRED: stop\r\n",
    "serial_lines": ["TRAFFIC LIGHT: ready", "GREEN: go", "YELLOW: caution", "RED: stop"],
    "expect_found": True,
    "active_pins": [{"pin": 8, "toggles": 2}],
}


class FakeSandbox:
    """Minimal stand-in for a powerx sandbox tool / Novita adapter."""

    def __init__(self, compile_ok: bool = True) -> None:
        self.commands: list[str] = []
        self.written: dict[str, str] = {}
        self._compile_ok = compile_ok
        self.files = _FakeFiles(self)

    def run(self, command: str) -> tuple[int, str]:
        self.commands.append(command)
        if "arduino-cli compile" in command:
            if not self._compile_ok:
                return 1, "error: expected ';' before '}'"
            return 0, "Sketch uses 3260 bytes (10%) of program storage space."
        if "arduino_sim.js" in command:
            return 0, json.dumps(HEX_JSON)
        if "arduino-cli version" in command:
            return 0, "arduino-cli  Version: 1.5.1"
        return 0, "SCAFFOLD_OK"


class _FakeFiles:
    def __init__(self, parent: FakeSandbox) -> None:
        self._parent = parent

    def write(self, path: str, content: str) -> None:
        self._parent.written[path] = content


def test_install_command_uses_the_repo_installer() -> None:
    url = "https://raw.githubusercontent.com/Arinze-eng/powerx/main/scripts/install_arduino_sandbox.sh"
    cmd = sandbox_install_command(url)
    assert url in cmd
    assert "install_arduino_sandbox.sh" in SANDBOX_SETUP_CMD


def test_verify_in_sandbox_compiles_and_simulates() -> None:
    sandbox = FakeSandbox()
    result = verify_in_sandbox(
        sandbox,
        "void setup(){}\nvoid loop(){}",
        board="uno",
        expect="RED: stop",
        ms=9000,
    )
    assert result["compiled"] is True
    assert result["simulated"] is True
    assert result["where"] == "sandbox"
    assert result["compile"]["flash_bytes"] == 3260
    assert result["simulation"]["expect_found"] is True


def test_verify_in_sandbox_uploads_the_sketch() -> None:
    sandbox = FakeSandbox()
    verify_in_sandbox(sandbox, "void setup(){}\nvoid loop(){}")
    assert any(path.endswith("sketch.ino") for path in sandbox.written)


def test_verify_in_sandbox_invokes_the_emulator_with_expectation() -> None:
    sandbox = FakeSandbox()
    verify_in_sandbox(sandbox, "void setup(){}", expect="Servo moving", ms=1500)
    sim_calls = [c for c in sandbox.commands if "arduino_sim.js" in c]
    assert sim_calls, "the emulator should have been invoked in the sandbox"
    assert "--expect" in sim_calls[0]
    assert "Servo moving" in sim_calls[0]
    assert "--ms 1500" in sim_calls[0]


def test_verify_in_sandbox_reports_compile_failure_honestly() -> None:
    sandbox = FakeSandbox(compile_ok=False)
    result = verify_in_sandbox(sandbox, "void setup(){ broken")
    assert result["compiled"] is False
    assert result["simulated"] is False
    assert "expected" in result["compile"]["log"]
    # The emulator must not run when compilation failed.
    assert not any("arduino_sim.js" in c for c in sandbox.commands)


def test_verify_in_sandbox_rejects_a_non_sandbox_object() -> None:
    result = verify_in_sandbox(object(), "void setup(){}")
    assert result["ok"] is False
    assert "run()" in result["log"]
