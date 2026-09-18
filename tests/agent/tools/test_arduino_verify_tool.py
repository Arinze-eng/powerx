"""Unit tests for the arduino_verify tool's pure logic.

These do not need arduino-cli or node: they cover the static safety audit, the
diagram parsing, and the Naira BOM builder. The compile/simulate stages are
exercised end-to-end in the sandbox instead (see docs/arduino-verification.md).
"""

from __future__ import annotations

from nanobot.agent.tools.arduino_verify import (
    BOARDS,
    KADUNA_PRICES,
    build_bom,
    parse_diagram,
    safety_check,
)

DIAGRAM = {
    "version": 1,
    "parts": [
        {"type": "wokwi-arduino-uno", "id": "uno1"},
        {"type": "wokwi-servo", "id": "servo1", "name": "SG90 Servo"},
        {"type": "wokwi-pir", "id": "pir1", "name": "PIR Motion Sensor"},
    ],
    "connections": [["uno1:9", "servo1:PWM", "orange", ["v0"]]],
}


def test_parse_diagram_handles_missing_input() -> None:
    assert parse_diagram(None) == ([], [])
    parts, connections = parse_diagram(DIAGRAM)
    assert len(parts) == 3
    assert connections[0][0] == "uno1:9"


def test_clean_sketch_reports_ok() -> None:
    findings = safety_check("void setup() {}\nvoid loop() {}", "uno", None)
    assert [f["level"] for f in findings] == ["OK"]


def test_uart_pins_flagged() -> None:
    code = "const int sensorPin = 0;\nvoid setup(){ pinMode(0, INPUT); }\nvoid loop(){}"
    levels = {f["level"] for f in safety_check(code, "uno", None)}
    assert "WARN" in levels
    assert any("0" in f["issue"] for f in safety_check(code, "uno", None))


def test_delay_in_isr_is_critical() -> None:
    code = (
        "volatile int flag = 0;\n"
        "void onMotion() { delay(100); flag = 1; }\n"
        "void setup() { attachInterrupt(digitalPinToInterrupt(2), onMotion, RISING); }\n"
        "void loop() {}\n"
    )
    findings = safety_check(code, "uno", None)
    assert any(f["level"] == "CRITICAL" for f in findings)


def test_five_volt_sensor_on_three_volt_board_is_critical() -> None:
    diagram = {
        "parts": [
            {"type": "wokwi-arduino-uno", "id": "uno1"},
            {"type": "wokwi-hc-sr04", "id": "us1", "name": "HC-SR04 Ultrasonic"},
        ],
        "connections": [],
    }
    findings = safety_check("void setup(){} void loop(){}", "esp32", diagram)
    assert any(f["level"] == "CRITICAL" for f in findings)


def test_led_without_resistor_is_warned() -> None:
    diagram = {
        "parts": [{"type": "wokwi-led", "id": "led1", "name": "Red LED"}],
        "connections": [],
    }
    findings = safety_check("void setup(){} void loop(){}", "uno", diagram)
    assert any("resistor" in f["issue"].lower() for f in findings)


def test_bom_always_bills_the_board_once() -> None:
    bom = build_bom(DIAGRAM, "uno")
    items = [row["item"] for row in bom]
    assert items[0] == BOARDS["uno"]["label"]
    # The diagram's own Uno entry must not be billed a second time.
    assert sum(1 for i in items if "Uno" in i) == 1
    assert "Jumper Wire" in items
    assert all(row["unit_price"] > 0 for row in bom)


def test_bom_totals_are_naira_positive() -> None:
    bom = build_bom(DIAGRAM, "uno")
    total = sum(row["qty"] * row["unit_price"] for row in bom)
    assert total >= KADUNA_PRICES["arduino uno r3 clone"][0]


def test_bom_handles_empty_diagram() -> None:
    bom = build_bom(None, "uno")
    assert bom[0]["item"] == BOARDS["uno"]["label"]
    assert all(row["unit_price"] >= 0 for row in bom)


def test_esp32_is_three_volt_logic() -> None:
    assert BOARDS["esp32"]["logic_v"] == 3.3
    assert BOARDS["uno"]["logic_v"] == 5.0
