"""Tests for Raspberry Pi support: the simulator, safety audit, and pipeline."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from nanobot.agent.tools.arduino_verify import (
    PI_BOARDS,
    build_pi_bom,
    pi_safety_check,
    verify_pi,
)

PI_HAS_PYTHON = shutil.which("python3") is not None or sys.executable is not None

GOOD_PROGRAM = '''
import time
import RPi.GPIO as GPIO

LED_PIN = 27
BUTTON_PIN = 17

def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(LED_PIN, GPIO.OUT)
    print("ready")
    for _ in range(3):
        GPIO.output(LED_PIN, GPIO.HIGH)
        time.sleep(0.2)
        GPIO.output(LED_PIN, GPIO.LOW)
        time.sleep(0.2)
    print("done")

if __name__ == "__main__":
    main()
'''

GPIOZERO_PROGRAM = '''
from gpiozero import LED, Button
from time import sleep

led = LED(17)
btn = Button(27)

print("gpiozero ready")
led.on()
sleep(1)
led.off()
print("gpiozero done")
'''


# --------------------------------------------------------------------------- #
# Safety audit
# --------------------------------------------------------------------------- #


def test_clean_program_is_not_flagged_critical() -> None:
    findings = pi_safety_check(GOOD_PROGRAM, "pi4")
    assert not any(f["level"] == "CRITICAL" for f in findings)


def test_power_rail_in_a_comment_is_not_a_hazard() -> None:
    """A module *powered* from 5V is fine; only a 5V signal is dangerous."""
    code = "# PIR VCC -> 5V pin 2\nimport RPi.GPIO as GPIO\nGPIO.setup(17, GPIO.IN)\n"
    findings = pi_safety_check(code, "pi4")
    assert not any(f["level"] == "CRITICAL" for f in findings)


def test_five_volt_signal_is_critical() -> None:
    code = "import RPi.GPIO as GPIO\n# HC-SR04 echo returns 5V\nGPIO.setup(17, GPIO.IN)\n"
    findings = pi_safety_check(code, "pi4")
    assert any(f["level"] == "CRITICAL" for f in findings)


def test_i2c_pin_reuse_is_warned() -> None:
    code = "import RPi.GPIO as GPIO\nGPIO.setup(3, GPIO.OUT)\n"
    findings = pi_safety_check(code, "pi4")
    assert any("2/3" in f["issue"] or "pins" in f["issue"].lower() for f in findings)


def test_servo_requires_external_power() -> None:
    code = "from gpiozero import Servo\ns = Servo(17)\ns.value = 1\n"
    findings = pi_safety_check(code, "pi4")
    assert any("current" in f["issue"].lower() for f in findings)


def test_safety_is_clean_for_led_with_resistor() -> None:
    code = "import RPi.GPIO as GPIO\n# LED via 330 ohm resistor\nGPIO.setup(27, GPIO.OUT)\n"
    findings = pi_safety_check(code, "pi4")
    assert not any(f["level"] == "CRITICAL" for f in findings)


# --------------------------------------------------------------------------- #
# BOM
# --------------------------------------------------------------------------- #


def test_pi_bom_always_includes_board_and_storage() -> None:
    bom = build_pi_bom(None, "pi4")
    items = " ".join(row["item"] for row in bom)
    assert "Raspberry Pi 4" in items
    assert "microSD" in items
    assert "power supply" in items.lower()


def test_pi_bom_pricing_is_positive() -> None:
    bom = build_pi_bom(None, "pi5")
    assert all(row["unit_price"] > 0 for row in bom)
    assert bom[0]["item"] == PI_BOARDS["pi5"]["label"]


# --------------------------------------------------------------------------- #
# End-to-end pipeline
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not PI_HAS_PYTHON, reason="python3 not available")
def test_pi_pipeline_runs_a_real_program() -> None:
    result = verify_pi(GOOD_PROGRAM, "pi4", None, expect="done", ms=5000)
    assert result["platform"] == "raspberry-pi"
    assert result["compiled"] is True
    assert result["simulated"] is True
    assert result["simulation"]["expect_found"] is True
    serial = result["simulation"]["serial"]
    assert "ready" in serial and "done" in serial
    # The LED pin must have been driven.
    assert result["simulation"]["pin_toggles"].get("27", 0) >= 2


@pytest.mark.skipif(not PI_HAS_PYTHON, reason="python3 not available")
def test_pi_pipeline_supports_gpiozero() -> None:
    result = verify_pi(GPIOZERO_PROGRAM, "pi4", None, ms=4000)
    assert result["simulated"] is True
    assert "gpiozero" in result["simulation"]["serial"]
    assert result["simulation"]["pin_toggles"].get("17", 0) >= 2


@pytest.mark.skipif(not PI_HAS_PYTHON, reason="python3 not available")
def test_pi_pipeline_reports_syntax_errors() -> None:
    result = verify_pi("def broken(:\n    pass\n", "pi4")
    assert result["compiled"] is False
    assert result["confidence_pct"] == 0
    assert "syntax" in json.dumps(result["compile"]).lower()


@pytest.mark.skipif(not PI_HAS_PYTHON, reason="python3 not available")
def test_pi_pipeline_drives_an_input_from_a_scenario() -> None:
    program = '''
import RPi.GPIO as GPIO
GPIO.setmode(GPIO.BCM)
GPIO.setup(17, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
for _ in range(5):
    if GPIO.input(17) == GPIO.HIGH:
        print("PIR TRIGGERED")
        break
print("scanned")
'''
    scenario = {"inputs": [{"pin": 17, "at_ms": 100, "state": "high"}]}
    result = verify_pi(program, "pi4", None, expect="scanned", ms=3000, scenario=scenario)
    assert result["simulated"] is True
    assert "scanned" in result["simulation"]["serial"]


@pytest.mark.skipif(not PI_HAS_PYTHON, reason="python3 not available")
def test_pi_pipeline_handles_an_infinite_loop_without_hanging() -> None:
    program = "import time\nprint('looping')\nwhile True:\n    time.sleep(1)\n"
    result = verify_pi(program, "pi4", None, ms=3000)
    assert result["simulated"] is True
    assert result["simulation"]["budget_hit"] is True


@pytest.mark.skipif(not PI_HAS_PYTHON, reason="python3 not available")
def test_pi_simulator_does_not_execute_the_program_twice() -> None:
    program = 'print("once")\n\ndef main():\n    print("main ran")\n\nif __name__ == "__main__":\n    main()\n'
    result = verify_pi(program, "pi4", None, ms=2000)
    serial = result["simulation"]["serial"]
    assert serial.count("once") == 1
    assert serial.count("main ran") == 1


def test_pi_uses_the_repo_simulator_asset() -> None:
    """The simulator must be the checked-in asset, not an inline copy."""
    from nanobot.agent.tools.arduino_verify import pi_sim_script

    script = pi_sim_script()
    assert script is not None and script.exists()
    assert "VirtualClock" in script.read_text()
    # Sanity: it is syntactically valid Python.
    proc = subprocess.run(
        [sys.executable or "python3", "-m", "py_compile", str(script)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
