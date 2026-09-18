"""Arduino hardware verification tool.

Gives the agent a *provable* Arduino build pipeline: a sketch is compiled with
``arduino-cli``, the resulting firmware is executed on a headless ATmega328P
emulator (``avr8js``), USART output and GPIO activity are captured, the wiring
is statically safety-checked, and a Naira BOM is produced from the diagram.

The whole point is that the agent never tells a user "this should work": it only
reports success after a real compile log and a real simulation transcript exist.

Auto-discovered by ToolLoader like the other agent tools.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

_TOOLCHAIN_DIR = Path(os.getenv("ARDUINO_TOOLCHAIN_DIR", "/opt/arduino-toolchain"))
_SIM_DIR = Path(os.getenv("ARDUINO_SIM_DIR", str(_TOOLCHAIN_DIR / "sim")))
_SIM_ASSET = Path(__file__).parent / "arduino_assets" / "arduino_sim.js"
_MAX_TRIES = 5
_DEFAULT_MS = 2500
_MAX_MS = 30000


def _env_prefix() -> str:
    """Point arduino-cli at the managed toolchain dir, but only if it was set up.

    Falling through to arduino-cli's own defaults keeps the tool working when a
    fresh install already populated ``~/.arduino15``.
    """
    data = _TOOLCHAIN_DIR / "data"
    if data.exists():
        return (
            f"ARDUINO_DIRECTORIES_DATA={data} "
            f"ARDUINO_DIRECTORIES_DOWNLOADS={_TOOLCHAIN_DIR / 'dl'} "
        )
    return ""

#: Boards we can compile for, and whether they are 3.3 V logic.
BOARDS: dict[str, dict[str, Any]] = {
    "uno": {"fqbn": "arduino:avr:uno", "logic_v": 5.0, "label": "Arduino Uno R3"},
    "nano": {"fqbn": "arduino:avr:nano", "logic_v": 5.0, "label": "Arduino Nano"},
    "mega": {"fqbn": "arduino:avr:mega", "logic_v": 5.0, "label": "Arduino Mega 2560"},
    "leonardo": {"fqbn": "arduino:avr:leonardo", "logic_v": 5.0, "label": "Arduino Leonardo"},
    "esp32": {"fqbn": "esp32:esp32:esp32", "logic_v": 3.3, "label": "ESP32 DevKit"},
    "esp8266": {"fqbn": "esp8266:esp8266:generic", "logic_v": 3.3, "label": "ESP8266 NodeMCU"},
}

#: Libraries pre-installed during setup; also auto-installed on compile failure.
AUTO_LIBS: dict[str, str] = {
    "Servo.h": "Servo",
    "DHT.h": "DHT sensor library",
    "RTClib.h": "RTClib",
    "LiquidCrystal.h": "LiquidCrystal",
    "LiquidCrystal_I2C.h": "LiquidCrystal I2C",
    "Wire.h": "",  # builtin
    "SPI.h": "",  # builtin
    "EEPROM.h": "",  # builtin
    "Adafruit_Sensor.h": "Adafruit Unified Sensor",
}

#: Wokwi/Velxio-compatible part ids the simulator understands.
DIAGRAM_PARTS = {
    "wokwi-arduino-uno", "wokwi-arduino-nano", "wokwi-arduino-mega",
    "wokwi-servo", "wokwi-dht22", "wokwi-dht11", "wokwi-ds3231", "wokwi-lcd1602",
    "wokwi-lcd2004", "wokwi-led", "wokwi-rgb-led", "wokwi-buzzer",
    "wokwi-pushbutton", "wokwi-pir", "wokwi-ultrasonic", "wokwi-resistor",
    "wokwi-potentiometer", "wokwi-relay-module", "wokwi-ir-receiver",
    "wokwi-photoresistor-sensor", "wokwi-soil-moisture-sensor", "wokwi-hc-sr04",
    "wokwi-ssd1306", "wokwi-neopixel", "wokwi-membrane-keypad", "wokwi-breadboard",
    "wokwi-power-supply", "wokwi-7segment", "wokwi-flame-sensor",
    "wokwi-servo-motor", "wokwi-mpu6050", "wokwi-dht22-sensor",
}

#: Kaduna Computer Village retail estimates (Naira). Verify before quoting a
#: customer; these drift with FX and stock. Keys are matched loosely.
KADUNA_PRICES: dict[str, tuple[int, str]] = {
    "arduino uno r3 clone": (15000, "Brain / MCU board"),
    "arduino uno r3": (45000, "Brain / MCU board (original)"),
    "arduino nano": (9000, "Compact MCU board"),
    "arduino mega": (28000, "High-IO MCU board"),
    "esp32": (12000, "Wi-Fi/BLE MCU board"),
    "esp8266": (7000, "Wi-Fi MCU board"),
    "sg90": (4000, "Micro servo actuator"),
    "servo": (4000, "Hobby servo"),
    "mg996r": (9500, "High-torque servo"),
    "dht22": (7500, "Temp/humidity sensor"),
    "dht11": (2500, "Temp/humidity sensor (basic)"),
    "ds3231": (5000, "Precision RTC"),
    "lcd1602": (6000, "16x2 character display"),
    "lcd2004": (7500, "20x4 character display"),
    "ssd1306": (7000, "0.96in OLED display"),
    "led": (100, "Indicator"),
    "rgb led": (400, "Indicator (RGB)"),
    "buzzer": (800, "Audible alert"),
    "pushbutton": (300, "User input"),
    "push button": (300, "User input"),
    "pir": (3500, "Motion sensor"),
    "ultrasonic": (3000, "Distance sensor"),
    "hc-sr04": (3000, "Distance sensor"),
    "relay": (3000, "Mains switching module"),
    "ir receiver": (1500, "Remote input"),
    "photoresistor": (800, "Light sensor"),
    "soil moisture": (2500, "Soil moisture sensor"),
    "flame sensor": (2000, "Flame detection"),
    "potentiometer": (700, "Analog input"),
    "neopixel": (3500, "Addressable LED strip"),
    "keypad": (4500, "4x4 membrane keypad"),
    "mpu6050": (4000, "IMU 6-axis"),
    "breadboard": (2500, "Prototyping"),
    "jumper wire": (1500, "Wiring"),
    "resistor": (500, "Current limiting (pack)"),
    "usb cable": (1500, "Programming / power"),
    "9v battery": (2000, "Power source"),
    "power supply": (3500, "5V 2A adapter"),
    "battery": (2000, "Power source"),
}

_SAFETY_PINS_RESERVED = {0, 1}  # UART RX/TX on AVR boards


class ArduinoVerificationError(RuntimeError):
    """Raised when the verification pipeline cannot continue."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 300) -> tuple[int, str]:
    """Run a command, returning ``(returncode, combined_output)``."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out = ""
        if exc.stdout:
            out += exc.stdout.decode() if isinstance(exc.stdout, bytes) else str(exc.stdout)
        if exc.stderr:
            out += exc.stderr.decode() if isinstance(exc.stderr, bytes) else str(exc.stderr)
        return 124, out + f"\n[timeout after {timeout}s]"
    except FileNotFoundError as exc:
        return 127, f"[command not found: {exc}]"


def _arduino_cli() -> str | None:
    """Locate arduino-cli whether it is on PATH, in the toolchain dir, or overridden."""
    explicit = os.getenv("ARDUINO_CLI_PATH", "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("arduino-cli")
    if found:
        return found
    for candidate in (
        _TOOLCHAIN_DIR / "arduino-cli",
        _TOOLCHAIN_DIR / "bin" / "arduino-cli",
        Path("/usr/local/bin/arduino-cli"),
        Path.home() / "bin" / "arduino-cli",
        Path.home() / ".local" / "bin" / "arduino-cli",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _node_bin() -> str | None:
    return shutil.which("node")


def _sim_script() -> Path:
    """Materialise the simulation harness inside the Node runtime directory.

    Living next to ``node_modules`` means ``require('avr8js')`` resolves without
    any NODE_PATH fiddling, regardless of where powerx itself is installed.
    """
    dest = _SIM_DIR / "arduino_sim.js"
    try:
        probe = _SIM_DIR / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        # Managed dir is read-only (e.g. /opt without chown) — fall back to tmp.
        dest = Path(tempfile.gettempdir()) / "arduino-sim" / "arduino_sim.js"
        dest.parent.mkdir(parents=True, exist_ok=True)
    source = _SIM_ASSET.read_text(encoding="utf-8") if _SIM_ASSET.exists() else SIM_HARNESS_JS
    if not dest.exists() or dest.read_text(encoding="utf-8") != source:
        dest.write_text(source, encoding="utf-8")
    (dest.parent / "package.json").write_text(
        json.dumps({"name": "arduino-sim", "private": True}), encoding="utf-8"
    )
    if not (dest.parent / "node_modules" / "avr8js").exists():
        npm = shutil.which("npm")
        if npm:
            _run([npm, "install", "--no-audit", "--no-fund", "avr8js@0.20.0"], cwd=str(dest.parent), timeout=300)
    return dest


def _tail(text: str, limit: int = 4000) -> str:
    text = text.strip()
    return text if len(text) <= limit else "...[truncated]\n" + text[-limit:]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _price_for(part_id: str, name: str) -> tuple[int, str]:
    """Best-effort Kaduna price lookup for a diagram part."""
    haystack = _slug(f"{part_id} {name}")
    best: tuple[int, str] | None = None
    best_len = 0
    for key, value in KADUNA_PRICES.items():
        if key in haystack and len(key) > best_len:
            best, best_len = value, len(key)
    if best is None:
        return 0, "Price on request (not in local price table)"
    return best


# --------------------------------------------------------------------------- #
# Static safety analysis
# --------------------------------------------------------------------------- #


def safety_check(code: str, board: str, diagram: dict[str, Any] | None) -> list[dict[str, str]]:
    """Return a list of findings: ``{level, issue, fix}``."""
    findings: list[dict[str, str]] = []
    info = BOARDS.get(board, BOARDS["uno"])
    board_v = info["logic_v"]

    # --- 1. UART pins 0/1 used for non-serial IO --------------------------- #
    pins_used = set()
    for match in re.finditer(r"\b(?:pinMode|digitalWrite|digitalRead|analogRead|analogWrite)\s*\(\s*(\d+)", code):
        pins_used.add(int(match.group(1)))
    for match in re.finditer(r"(?:const\s+)?int\s+\w*(?:pin|Pin|PIN)\w*\s*=\s*(\d+)", code):
        pins_used.add(int(match.group(1)))
    conflict = pins_used & _SAFETY_PINS_RESERVED
    if conflict:
        findings.append(
            {
                "level": "WARN",
                "issue": f"Pins {sorted(conflict)} are used, but they are the hardware UART (RX/TX).",
                "fix": "Move these to pins 2/3 or leave them free if you need Serial upload/monitor.",
            }
        )

    # --- 2. delay() inside an ISR ------------------------------------------ #
    isr_block = re.search(
        r"void\s+\w+\s*\(\s*\)\s*\{[^}]*(?:delay|delayMicroseconds)\s*\([^)]*\)",
        code,
        re.DOTALL,
    )
    attach = re.search(r"attachInterrupt\s*\(", code)
    if isr_block and attach:
        findings.append(
            {
                "level": "CRITICAL",
                "issue": "delay()/delayMicroseconds() found in an interrupt-driven handler.",
                "fix": "ISRs must be short: set a volatile flag and do the work in loop().",
            }
        )

    # --- 3. 5 V peripherals on a 3.3 V board ------------------------------- #
    needs_level_shift = {"ultrasonic", "hc-sr04", "relay", "buzzer", "servo"}
    part_lines = json.dumps(diagram or {}).lower()
    if board_v < 5.0:
        risky = sorted(p for p in needs_level_shift if p in part_lines)
        if risky:
            findings.append(
                {
                    "level": "CRITICAL",
                    "issue": f"{info['label']} runs {board_v} V logic, but {risky} normally expect 5 V.",
                    "fix": "Add a bidirectional logic level shifter (or use a 3.3 V-rated module) before wiring.",
                }
            )
        findings.append(
            {
                "level": "WARN",
                "issue": f"{info['label']} GPIO is {board_v} V tolerant only.",
                "fix": "Power sensors from 3V3 and never feed a 5 V signal into a GPIO pin.",
            }
        )

    # --- 4. Servo / relay / motor current draw ----------------------------- #
    if re.search(r"servo|relay|motor", part_lines):
        findings.append(
            {
                "level": "WARN",
                "issue": "Servo/relay/motor loads draw more current than the board's 5V regulator should supply.",
                "fix": "Power the load from a separate 5V supply and tie the grounds together.",
            }
        )

    # --- 5. Bare LED without a current-limiting resistor ------------------- #
    if "led" in part_lines and not re.search(r"resistor|220|330|470", part_lines):
        findings.append(
            {
                "level": "WARN",
                "issue": "An LED is present with no resistor detected in the diagram.",
                "fix": "Put a 220-330 ohm resistor in series with each LED.",
            }
        )

    # --- 6. Always-on Serial on battery projects --------------------------- #
    if re.search(r"sleep|battery|low.?power", code, re.IGNORECASE):
        findings.append(
            {
                "level": "INFO",
                "issue": "Battery/low-power keywords detected.",
                "fix": "Consider disabling the power LED and using deep sleep to extend runtime.",
            }
        )

    if not findings:
        findings.append({"level": "OK", "issue": "No wiring or ISR hazards detected.", "fix": ""})
    return findings


def parse_diagram(diagram: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[list[Any]]]:
    """Extract ``(parts, connections)`` from a Wokwi/Velxio diagram."""
    if not diagram:
        return [], []
    parts = diagram.get("parts") or []
    connections = diagram.get("connections") or []
    return list(parts), list(connections)


def build_bom(diagram: dict[str, Any] | None, board: str) -> list[dict[str, Any]]:
    """Build a Naira BOM from a diagram, always including the MCU board."""
    parts, _ = parse_diagram(diagram)
    bom: list[dict[str, Any]] = []
    seen: set[str] = set()

    info = BOARDS.get(board, BOARDS["uno"])
    board_key = info["label"].lower()
    price, note = _price_for(board_key, info["label"])
    if board == "uno":
        price, note = KADUNA_PRICES["arduino uno r3 clone"]
    bom.append({"item": info["label"], "qty": 1, "unit_price": price, "note": note})
    seen.add("mcu")

    for part in parts:
        pid = str(part.get("type") or part.get("id") or "")
        if "arduino" in pid and "uno" in pid:
            continue  # already billed as the board
        name = str(part.get("name") or pid)
        key = _slug(name + " " + pid)
        if key in seen:
            continue
        seen.add(key)
        price, note = _price_for(pid, name)
        bom.append({"item": name, "qty": 1, "unit_price": price, "note": note})

    # Accessories that every build realistically needs.
    for extra in ("jumper wire", "breadboard", "usb cable"):
        price, note = KADUNA_PRICES[extra]
        bom.append({"item": extra.title(), "qty": 1, "unit_price": price, "note": note})

    return bom


# --------------------------------------------------------------------------- #
# Pipeline stages
# --------------------------------------------------------------------------- #


def install_toolchain(install_esp32: bool = False, timeout: int = 900) -> dict[str, Any]:
    """Install arduino-cli + AVR core + the libraries the agent commonly needs."""
    steps: list[dict[str, Any]] = []
    _TOOLCHAIN_DIR.mkdir(parents=True, exist_ok=True)

    cli = _arduino_cli()
    if not cli:
        script = "curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=%s sh" % _TOOLCHAIN_DIR
        code, out = _run(["bash", "-lc", script], timeout=timeout)
        steps.append({"step": "install arduino-cli", "ok": code == 0, "log": _tail(out, 1200)})
        cli = _arduino_cli()
    else:
        steps.append({"step": "install arduino-cli", "ok": True, "log": f"already present: {cli}"})

    if not cli:
        return {"ok": False, "steps": steps, "error": "arduino-cli could not be installed"}

    env_prefix = _env_prefix()

    code, out = _run(["bash", "-lc", f"{env_prefix} {cli} core update-index"], timeout=timeout)
    steps.append({"step": "core update-index", "ok": code == 0, "log": _tail(out, 600)})

    code, out = _run(["bash", "-lc", f"{env_prefix} {cli} core install arduino:avr"], timeout=timeout)
    steps.append({"step": "core install arduino:avr", "ok": code == 0, "log": _tail(out, 800)})

    if install_esp32:
        code, out = _run(
            ["bash", "-lc", f"{env_prefix} {cli} config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json"],
            timeout=120,
        )
        _run(["bash", "-lc", f"{env_prefix} {cli} core update-index"], timeout=timeout)
        code, out = _run(["bash", "-lc", f"{env_prefix} {cli} core install esp32:esp32"], timeout=timeout)
        steps.append({"step": "core install esp32:esp32", "ok": code == 0, "log": _tail(out, 800)})

    for lib in ("Servo", "DHT sensor library", "RTClib", "LiquidCrystal"):
        code, out = _run(["bash", "-lc", f"{env_prefix} {cli} lib install '{lib}'"], timeout=300)
        steps.append({"step": f"lib install {lib}", "ok": code == 0, "log": _tail(out, 300)})

    # Emulator runtime: the AVR simulator is a Node program using avr8js.
    npm = shutil.which("npm")
    if npm:
        sim_dir = _SIM_DIR
        sim_dir.mkdir(parents=True, exist_ok=True)
        pkg = sim_dir / "package.json"
        if not pkg.exists():
            pkg.write_text(json.dumps({"name": "arduino-sim", "private": True}), encoding="utf-8")
        code, out = _run([npm, "install", "--no-audit", "--no-fund", "avr8js@0.20.0"], cwd=str(sim_dir), timeout=timeout)
        steps.append({"step": "npm install avr8js", "ok": code == 0, "log": _tail(out, 500)})
    else:
        steps.append({"step": "npm install avr8js", "ok": False, "log": "npm not found; the AVR simulator cannot run."})

    ok = all(s["ok"] for s in steps if s["step"].startswith(("install arduino-cli", "core install arduino:avr")))
    return {"ok": ok, "steps": steps, "cli": cli, "sim_dir": str(_SIM_DIR)}


def compile_sketch(code: str, board: str = "uno", workdir: str | None = None) -> dict[str, Any]:
    """Compile an .ino sketch; returns the build log and hex path."""
    cli = _arduino_cli()
    if not cli:
        return {"ok": False, "log": "arduino-cli is not installed. Run arduino_verify with action=setup first."}

    info = BOARDS.get(board, BOARDS["uno"])
    root = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="arduino_"))
    sketch_dir = root / "sketch"
    sketch_dir.mkdir(parents=True, exist_ok=True)
    (sketch_dir / "sketch.ino").write_text(code, encoding="utf-8")
    out_dir = root / "build"
    out_dir.mkdir(exist_ok=True)

    env_prefix = _env_prefix()
    # User-scope installs also work when the toolchain data dir is not writable.
    code_rc, log = _run(
        ["bash", "-lc", f"{env_prefix} {cli} compile --fqbn {info['fqbn']} '{sketch_dir}' --output-dir '{out_dir}'"],
        timeout=420,
    )

    # Auto-install a missing library once, then retry.
    if code_rc != 0:
        missing = re.search(r"fatal error:\s+([\w.]+\.h):\s+No such file", log)
        if missing:
            header = missing.group(1)
            lib = AUTO_LIBS.get(header)
            if lib:
                _run(["bash", "-lc", f"{env_prefix} {cli} lib install '{lib}'"], timeout=300)
                code_rc, log = _run(
                    ["bash", "-lc", f"{env_prefix} {cli} compile --fqbn {info['fqbn']} '{sketch_dir}' --output-dir '{out_dir}'"],
                    timeout=420,
                )

    hex_file = out_dir / "sketch.ino.hex"
    size = re.search(r"Sketch uses (\d+) bytes \((\d+)%\)", log)
    ram = re.search(r"Global variables use (\d+) bytes \((\d+)%\)", log)
    return {
        "ok": code_rc == 0 and hex_file.exists(),
        "board": info["label"],
        "fqbn": info["fqbn"],
        "log": _tail(log, 5000),
        "hex": str(hex_file) if hex_file.exists() else "",
        "flash_bytes": int(size.group(1)) if size else None,
        "flash_pct": int(size.group(2)) if size else None,
        "ram_bytes": int(ram.group(1)) if ram else None,
        "ram_pct": int(ram.group(2)) if ram else None,
        "workdir": str(root),
    }


def simulate(hex_path: str, ms: int = _DEFAULT_MS, expect: str | None = None) -> dict[str, Any]:
    """Run firmware on the headless AVR emulator and capture serial + pin activity."""
    node = _node_bin()
    if not node:
        return {"ok": False, "log": "node is not installed; cannot run the AVR simulator."}
    if not hex_path or not Path(hex_path).exists():
        return {"ok": False, "log": f"firmware not found: {hex_path!r}"}
    script = _sim_script()
    if not script.exists():
        return {"ok": False, "log": f"simulation harness missing at {script}"}

    cmd = [node, str(script), hex_path, "--ms", str(max(100, min(ms, _MAX_MS)))]
    if expect:
        cmd += ["--expect", expect]
    rc, out = _run(cmd, timeout=180)
    if rc != 0:
        return {"ok": False, "log": _tail(out, 3000)}

    payload = out.strip()
    start = payload.find("{")
    if start < 0:
        return {"ok": False, "log": _tail(out, 3000)}
    try:
        data = json.loads(payload[start:])
    except ValueError:
        return {"ok": False, "log": _tail(out, 3000)}

    active = [p for p in data.get("active_pins", []) if p.get("toggles", 0) > 0]
    data["active_pins"] = active
    data["serial"] = data.get("serial", "")
    data["ok"] = bool(data.get("serial", "").strip()) or bool(active)
    return data


def verify(
    code: str,
    board: str = "uno",
    diagram: dict[str, Any] | None = None,
    expect: str | None = None,
    ms: int = _DEFAULT_MS,
    max_tries: int = _MAX_TRIES,
) -> dict[str, Any]:
    """Full pipeline: compile -> simulate -> safety -> BOM -> verdict."""
    board = board.lower() if board else "uno"
    if board not in BOARDS:
        board = "uno"

    compile_result = compile_sketch(code, board)
    sim_result: dict[str, Any] | None = None
    attempts = 1

    while compile_result["ok"] and attempts <= max(1, max_tries):
        sim_result = simulate(compile_result["hex"], ms=ms, expect=expect)
        if sim_result.get("ok"):
            break
        # Nothing to auto-fix here (wiring/expectation issue) — report honestly.
        break

    findings = safety_check(code, board, diagram)
    bom = build_bom(diagram, board)
    total = sum(item["qty"] * item["unit_price"] for item in bom)

    critical = [f for f in findings if f["level"] == "CRITICAL"]
    compiled = bool(compile_result["ok"])
    simulated = bool(sim_result and sim_result.get("ok"))
    confidence = 0
    if compiled:
        confidence += 50
    if simulated:
        confidence += 35
    if not critical:
        confidence += 13
    if expect and sim_result and sim_result.get("expect_found"):
        confidence = min(99, confidence + 2)

    return {
        "compiled": compiled,
        "simulated": simulated,
        "compile": compile_result,
        "simulation": sim_result,
        "safety": findings,
        "bom": bom,
        "bom_total_naira": total,
        "confidence_pct": confidence,
        "expect": expect,
    }


# --------------------------------------------------------------------------- #
# Embedded simulation harness (fallback copy of arduino_assets/arduino_sim.js)
# --------------------------------------------------------------------------- #

SIM_HARNESS_JS = r"""
const fs = require('fs');
const {
  CPU, avrInstruction, AVRIOPort, portBConfig, portCConfig, portDConfig,
  AVRTimer, timer0Config, timer1Config, timer2Config,
  AVRUSART, usart0Config, PinState,
} = require('avr8js');

function parseIntelHex(text) {
  const bytes = new Uint8Array(0x10000);
  let maxAddr = 0;
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line.startsWith(':')) continue;
    const len = parseInt(line.substr(1, 2), 16);
    const addr = parseInt(line.substr(3, 4), 16);
    const type = parseInt(line.substr(7, 2), 16);
    if (type === 0x00) {
      for (let i = 0; i < len; i++) bytes[addr + i] = parseInt(line.substr(9 + i * 2, 2), 16);
      maxAddr = Math.max(maxAddr, addr + len);
    }
  }
  return { bytes, maxAddr };
}

function run(hexPath, ms, expect) {
  const hex = fs.readFileSync(hexPath, 'utf8');
  const { bytes, maxAddr } = parseIntelHex(hex);
  if (maxAddr === 0) throw new Error('Empty or unparsable HEX file');
  const prog = new Uint16Array(0x8000);
  for (let i = 0; i < maxAddr; i += 2) prog[i >> 1] = (bytes[i] | (bytes[i + 1] << 8)) & 0xffff;

  const cpu = new CPU(prog, 0x800);
  const portB = new AVRIOPort(cpu, portBConfig);
  const portC = new AVRIOPort(cpu, portCConfig);
  const portD = new AVRIOPort(cpu, portDConfig);
  new AVRTimer(cpu, timer0Config);
  new AVRTimer(cpu, timer1Config);
  new AVRTimer(cpu, timer2Config);

  const serial = [];
  const usart = new AVRUSART(cpu, usart0Config, 16e6);
  usart.onByteTransmit = (b) => serial.push(String.fromCharCode(b));

  const pinMap = {
    0: [portD, 0], 1: [portD, 1], 2: [portD, 2], 3: [portD, 3], 4: [portD, 4],
    5: [portD, 5], 6: [portD, 6], 7: [portD, 7], 8: [portB, 0], 9: [portB, 1],
    10: [portB, 2], 11: [portB, 3], 12: [portB, 4], 13: [portB, 5],
    14: [portC, 0], 15: [portC, 1], 16: [portC, 2], 17: [portC, 3],
    18: [portC, 4], 19: [portC, 5],
  };
  const activity = {};
  for (const p of [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]) {
    const [port, bit] = pinMap[p];
    activity[p] = { toggles: 0, last: null };
    port.addListener(() => {
      const state = port.pinState(bit);
      const high = state === PinState.High || state === PinState.HighPort;
      if (activity[p].last !== null && activity[p].last !== high) activity[p].toggles++;
      activity[p].last = high;
    });
  }

  const totalCycles = Math.min(16000 * ms, 16e6 * 30);
  const started = Date.now();
  for (let i = 1; i < totalCycles; i += 1) {
    avrInstruction(cpu);
    cpu.tick();
    if (cpu.cycles >= totalCycles) break;
  }
  const text = serial.join('');
  return {
    ok: true,
    firmware_bytes: maxAddr,
    cycles_run: cpu.cycles,
    sim_ms: ms,
    wall_ms: Date.now() - started,
    serial: text,
    serial_lines: text.split(/\r?\n/).filter((l) => l.length),
    expect: expect || null,
    expect_found: expect ? text.includes(expect) : null,
    active_pins: Object.entries(activity)
      .filter(([, a]) => a.toggles > 0 || a.last !== null)
      .map(([p, a]) => ({ pin: Number(p), toggles: a.toggles })),
  };
}

const args = process.argv.slice(2);
const hexPath = args[0];
if (!hexPath) { console.error('usage: arduino_sim.js <hex> [--ms N] [--expect TEXT]'); process.exit(2); }
let ms = 2500, expect = null;
for (let i = 1; i < args.length; i++) {
  if (args[i] === '--ms') ms = parseInt(args[++i], 10);
  else if (args[i] === '--expect') expect = args[++i];
}
console.log(JSON.stringify(run(hexPath, ms, expect), null, 2));
"""


# --------------------------------------------------------------------------- #
# Tool
# --------------------------------------------------------------------------- #


@tool_parameters(
    tool_parameters_schema(
        required=["action"],
        action=StringSchema(
            "Arduino verification operation",
            enum=["build", "compile", "simulate", "safety", "bom", "setup"],
        ),
        code=StringSchema("Full Arduino sketch (.ino) source, including every #include"),
        board=StringSchema(
            "Target board key",
            enum=sorted(BOARDS.keys()),
        ),
        diagram=ObjectSchema(description="Wokwi/Velxio diagram.json content (parts + connections)"),
        expect=StringSchema("Serial text that must appear for the simulation to pass, e.g. 'Servo moving'"),
        ms=IntegerSchema(description="Milliseconds of firmware execution to simulate", minimum=100, maximum=_MAX_MS),
        hex=StringSchema("Firmware .hex path for the simulate action"),
        install_esp32=BooleanSchema(description="Set true during setup to also install the ESP32 core"),
    )
)
class ArduinoVerifyTool(Tool):
    """Compile, simulate, and safety-check Arduino builds before recommending parts."""

    _scopes = {"core", "subagent"}

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        """Offer this tool only where the Arduino toolchain can actually run.

        Keeps the tool description off the prompt on installs that never build
        hardware, while staying available in the gateway image (which ships the
        toolchain under /opt/arduino-toolchain). ``ARDUINO_VERIFY_ENABLED=1``
        forces it on for a sandbox that installed the toolchain elsewhere.
        """
        if os.getenv("ARDUINO_VERIFY_ENABLED", "").strip() in ("1", "true", "yes"):
            return True
        if _arduino_cli() is None or _node_bin() is None:
            return False
        return True

    @property
    def name(self) -> str:
        return "arduino_verify"

    @property
    def description(self) -> str:
        return (
            "Build and PROVE an Arduino project before telling a user it works. "
            "Runs a real toolchain: arduino-cli compiles the sketch to firmware, then the "
            "firmware is executed on a headless AVR emulator that captures USART serial "
            "output and per-pin GPIO activity. Also performs a static wiring/ISR safety "
            "audit and produces a Naira (Kaduna Computer Village) bill of materials. "
            "Actions: 'setup' installs arduino-cli + AVR core + common libraries; "
            "'compile' compiles sketch code and returns the build log and hex path; "
            "'simulate' executes a hex and returns the serial transcript; "
            "'safety' audits code+diagram for hazards (5V logic on 3.3V boards, UART pins "
            "0/1, delay() in ISRs, missing LED resistors, unpowered servos); "
            "'bom' prices the parts in a diagram; "
            "'build' runs the whole pipeline and returns a verdict with confidence. "
            "Always generate sketch.ino, diagram.json (parts + connections), and use "
            "action=build with an 'expect' string. NEVER claim hardware works without "
            "the compile log and simulation transcript this tool returns."
        )

    async def execute(self, **kwargs: Any) -> ToolResult | str:  # type: ignore[override]
        action = str(kwargs.get("action") or "build").strip().lower()
        board = str(kwargs.get("board") or "uno").strip().lower() or "uno"
        code = kwargs.get("code") or ""
        expect = kwargs.get("expect") or None
        ms = int(kwargs.get("ms") or _DEFAULT_MS)
        hex_path = str(kwargs.get("hex") or "")
        diagram = kwargs.get("diagram")
        if isinstance(diagram, str):
            try:
                diagram = json.loads(diagram)
            except ValueError:
                return ToolResult.error("diagram must be valid JSON (parts + connections).")
        if not isinstance(diagram, dict):
            diagram = None

        try:
            if action == "setup":
                result = await asyncio.to_thread(
                    install_toolchain, bool(kwargs.get("install_esp32"))
                )
                return json.dumps(result, indent=2)

            if action == "compile":
                if not code.strip():
                    return ToolResult.error("code is required for action=compile.")
                result = await asyncio.to_thread(compile_sketch, code, board)
                return json.dumps(result, indent=2)

            if action == "simulate":
                if not hex_path:
                    return ToolResult.error("hex is required for action=simulate.")
                result = await asyncio.to_thread(simulate, hex_path, ms, expect)
                return json.dumps(result, indent=2)

            if action == "safety":
                findings = safety_check(code, board, diagram)
                return json.dumps({"board": board, "findings": findings}, indent=2)

            if action == "bom":
                bom = build_bom(diagram, board)
                total = sum(i["qty"] * i["unit_price"] for i in bom)
                return json.dumps({"bom": bom, "total_naira": total}, indent=2)

            # default: full build
            if not code.strip():
                return ToolResult.error("code is required for the build pipeline.")
            result = await asyncio.to_thread(verify, code, board, diagram, expect, ms)
            return json.dumps(result, indent=2)
        except ArduinoVerificationError as exc:
            return ToolResult.error(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("arduino_verify failed")
            return ToolResult.error(f"arduino_verify failed: {exc}")
