"""Tests for the diagram renderer, capability check, and scenario plumbing."""

from __future__ import annotations

import json

from nanobot.agent.tools.arduino_diagram import diagram_summary, render_svg
from nanobot.agent.tools.arduino_verify import capability_check, simulate

DIAGRAM = {
    "version": 1,
    "parts": [
        {"type": "wokwi-arduino-uno", "id": "uno1"},
        {"type": "wokwi-servo", "id": "servo1", "name": "SG90 Servo"},
        {"type": "wokwi-led", "id": "red1", "name": "Red LED"},
        {"type": "wokwi-resistor", "id": "r1"},
    ],
    "connections": [
        ["uno1:9", "servo1:PWM", "orange", ["v0"]],
        ["uno1:5V", "servo1:V+", "red", ["v0"]],
        ["uno1:GND", "servo1:GND", "black", ["v0"]],
        ["uno1:8", "red1:A", "red", ["v0"]],
    ],
}


# --------------------------------------------------------------------------- #
# Diagram renderer
# --------------------------------------------------------------------------- #


def test_render_svg_is_valid_standalone_svg() -> None:
    svg = render_svg(DIAGRAM, title="Traffic Light")
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    assert "xmlns=" in svg


def test_render_svg_includes_every_part_and_connection() -> None:
    svg = render_svg(DIAGRAM)
    for pid in ("uno1", "servo1", "red1", "r1"):
        assert pid in svg, f"missing component box for {pid}"
    # Every connection should produce a labelled wire badge.
    for pin in ("PWM", "V+", "GND", "A"):
        assert pin in svg


def test_render_svg_escapes_untrusted_names() -> None:
    hostile = {
        "parts": [{"type": "wokwi-led", "id": "x", "name": "<script>alert(1)</script>"}],
        "connections": [],
    }
    svg = render_svg(hostile)
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


def test_render_svg_handles_missing_diagram() -> None:
    svg = render_svg(None)
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")


def test_render_svg_creates_missing_part_entries() -> None:
    # A connection referencing an undeclared part must not crash the renderer.
    dangling = {"parts": [{"type": "wokwi-arduino-uno", "id": "uno1"}],
                "connections": [["uno1:9", "ghost1:IN", "green", ["v0"]]]}
    svg = render_svg(dangling)
    assert "ghost1" in svg


def test_diagram_summary_counts_kinds() -> None:
    summary = diagram_summary(DIAGRAM)
    assert summary["part_count"] == 4
    assert summary["connection_count"] == 4
    assert summary["connection_kinds"]["power"] == 1
    assert summary["connection_kinds"]["ground"] == 1


# --------------------------------------------------------------------------- #
# Capability check
# --------------------------------------------------------------------------- #


def test_voice_on_uno_without_module_is_impossible() -> None:
    code = "// voice recognition for next page\nvoid setup(){} void loop(){}"
    findings = capability_check(code, "uno", {"parts": [], "connections": []})
    assert any(f["level"] == "IMPOSSIBLE" for f in findings)


def test_voice_on_uno_with_module_is_allowed() -> None:
    code = (
        "#include <SoftwareSerial.h>\n"
        "// voice recognition module on SoftwareSerial\n"
        "void setup(){} void loop(){}"
    )
    findings = capability_check(code, "uno", {"parts": [], "connections": []})
    assert not any("voice" in f["issue"].lower() for f in findings)


def test_voice_on_esp32_is_allowed() -> None:
    code = "// voice recognition\nvoid setup(){} void loop(){}"
    findings = capability_check(code, "esp32", None)
    assert not any(f["level"] == "IMPOSSIBLE" for f in findings)


def test_wifi_on_uno_without_module_is_impossible() -> None:
    code = "// fetch data over wifi and post to mqtt\nvoid setup(){} void loop(){}"
    findings = capability_check(code, "uno", None)
    assert any("wi" in f["issue"].lower() for f in findings)


def test_plain_sketch_has_no_capability_findings() -> None:
    findings = capability_check("void setup(){} void loop(){}", "uno", None)
    assert findings == []


# --------------------------------------------------------------------------- #
# Scenario plumbing
# --------------------------------------------------------------------------- #


def test_scenario_is_written_and_passed_to_the_emulator(tmp_path) -> None:
    """The harness must receive a scenario file when stimulus is supplied."""
    hex_file = tmp_path / "sketch.ino.hex"
    hex_file.write_text(":00000001FF\n")
    scenario = {"inputs": [{"pin": 4, "at_ms": 500, "state": "low", "hold_ms": 100}]}

    # No node in this environment -> the call reports failure, but it must have
    # attempted the emulator and written the scenario file.
    result = simulate(str(hex_file), ms=1000, scenario=scenario)
    assert isinstance(result, dict)
    scenario_file = tmp_path / "scenario.json"
    assert scenario_file.exists()
    assert json.loads(scenario_file.read_text()) == scenario


def test_simulate_rejects_missing_firmware() -> None:
    result = simulate("/nonexistent/firmware.hex")
    assert result["ok"] is False
