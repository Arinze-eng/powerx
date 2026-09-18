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
import sys
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

try:  # renderer is optional so the tool still loads if it is missing
    from nanobot.agent.tools.arduino_diagram import (
        diagram_summary,
        render_svg,
    )
except Exception:  # pragma: no cover - defensive
    diagram_summary = None  # type: ignore[assignment]
    render_svg = None  # type: ignore[assignment]

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

#: Board keys that ship a hardware UART we must not steal for IO.
_SAFETY_PINS_RESERVED = {0, 1}  # UART RX/TX on AVR boards

#: What each board can realistically do on its own. Used to refuse impossible
#: instructions *before* wiring money is spent, instead of shipping a build that
#: can never work.
BOARD_CAPABILITIES: dict[str, dict[str, Any]] = {
    "uno": {
        "ram_kb": 2,
        "has_wifi": False,
        "has_mic": False,
        "has_dac": False,
        "can_do_voice_recognition": False,
        "can_do_ml": False,
        "note": "ATmega328P: 2 KB RAM, no network, no audio input.",
    },
    "nano": {
        "ram_kb": 2,
        "has_wifi": False,
        "has_mic": False,
        "has_dac": False,
        "can_do_voice_recognition": False,
        "can_do_ml": False,
        "note": "ATmega328P: 2 KB RAM, no network, no audio input.",
    },
    "mega": {
        "ram_kb": 8,
        "has_wifi": False,
        "has_mic": False,
        "has_dac": False,
        "can_do_voice_recognition": False,
        "can_do_ml": False,
        "note": "ATmega2560: 8 KB RAM, plenty of IO, still no audio front-end.",
    },
    "leonardo": {
        "ram_kb": 2.5,
        "has_wifi": False,
        "has_mic": False,
        "has_dac": False,
        "can_do_voice_recognition": False,
        "can_do_ml": False,
        "note": "ATmega32u4: native USB, 2.5 KB RAM.",
    },
    "esp32": {
        "ram_kb": 520,
        "has_wifi": True,
        "has_mic": False,  # needs an external I2S mic
        "has_dac": True,
        "can_do_voice_recognition": True,  # with an I2S mic + off-chip or cloud ASR
        "can_do_ml": True,
        "note": "520 KB RAM, Wi-Fi/BLE, I2S. Needs an external I2S microphone for audio.",
    },
    "esp8266": {
        "ram_kb": 80,
        "has_wifi": True,
        "has_mic": False,
        "has_dac": False,
        "can_do_voice_recognition": False,
        "can_do_ml": False,
        "note": "80 KB RAM, Wi-Fi. Audio is not practical; use an ESP32 instead.",
    },
}

#: Requirement keyword groups, the capability they need, how to satisfy them with
#: an add-on, and the remedy when neither the board nor an add-on covers it.
_CAPABILITY_REQUIREMENTS: list[tuple[tuple[str, ...], str, tuple[str, ...], str]] = [
    (
        ("voice recognition", "speech recognition", "voice control", "voice command",
         "say \"", "wake word", "keyword spotting", "speech to text", "voice activated"),
        "can_do_voice_recognition",
        ("elechouse", "dfrobot", "voice recognition v3", "voice module", "speech module",
         "microphone", "i2s", "max9814", "softwareserial", "inmp441", "ics43434",
         "voice shield", "easyvr"),
        "Add an offline voice module (Elechouse VR3 / DFRobot Voice Recognition V3) on "
        "Serial, or move to an ESP32 with an I2S microphone. An Uno has no microphone "
        "input and only 2 KB of RAM, so recognition must happen off-chip.",
    ),
    (
        ("wifi", "wi-fi", "internet", "cloud", "mqtt", "http request", "telegram", "blynk"),
        "has_wifi",
        ("esp-01", "esp01", "esp8266", "esp32", "ethernet", "w5500", "enc28j60",
         "sim800", "sim7600", "gsm", "nrf24", "wifi module", "wifi shield"),
        "Add an ESP32/ESP8266 (or an ESP-01 / W5500 shield) for connectivity; the Uno "
        "has no network interface.",
    ),
    (
        ("machine learning", "tensorflow", "neural network", "image recognition",
         "face recognition", "camera"),
        "can_do_ml",
        ("esp32-s3", "esp32-cam", "esp32cam", "raspberry", "jetson", "openmv", "pixy", "k210"),
        "Use an ESP32-S3/CAM or a Raspberry Pi. AVR boards cannot run ML models.",
    ),
    (
        ("play music", "audio playback", "mp3", "wav", "speaker output", "sound output"),
        "has_dac",
        ("dfplayer", "mp3 module", "sd card", "vs1053", "pam8403", "max98357", "i2s dac"),
        "Add a DFPlayer Mini + SD card for audio playback; the Uno has no DAC or audio "
        "output of its own.",
    ),
]


def capability_check(code: str, board: str, diagram: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Refuse requirements the chosen board physically cannot satisfy.

    An add-on module in the diagram or sketch *does* satisfy the requirement
    (an Uno plus a voice module can absolutely turn pages), so those are treated
    as satisfied rather than impossible. Only when neither the on-board
    capability nor a co-processor is present is the build flagged IMPOSSIBLE.
    """
    info = BOARD_CAPABILITIES.get(board)
    if info is None:
        return []

    code_text = code.lower()
    diagram_text = json.dumps(diagram).lower() if diagram else ""
    haystack = code_text + " " + diagram_text
    label = BOARDS.get(board, {}).get("label", board)

    findings: list[dict[str, str]] = []
    for keywords, capability, satisfiers, remedy in _CAPABILITY_REQUIREMENTS:
        if not any(kw in haystack for kw in keywords):
            continue
        if info.get(capability):
            continue
        if any(tok in haystack for tok in satisfiers):
            continue  # covered by an add-on module
        findings.append(
            {
                "level": "IMPOSSIBLE",
                "issue": (
                    f"'{keywords[0]}' was requested, but {label} cannot do it and no "
                    f"supporting module is present. {info['note']}"
                ),
                "fix": remedy,
            }
        )
    return findings


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


def _local_toolchain_available() -> bool:
    """True when arduino-cli and node are present on *this* host."""
    return _arduino_cli() is not None and _node_bin() is not None


def _sandbox_tool(ctx: ToolContext | None) -> Any:
    """Look up a sandbox tool (novita_sandbox / vps / runloop) in the registry.

    The gateway image does not ship the Arduino toolchain, so in production the
    build must execute inside the user's execution sandbox. The sandbox tools
    are ordinary Tools, so they can be resolved from the same registry and
    driven with their own schema.
    """
    if ctx is None:
        return None
    registry = getattr(ctx, "tool_registry", None) or getattr(ctx, "tools", None)
    if registry is None:
        return None
    try:
        items = registry.values() if isinstance(registry, dict) else registry
        for tool in items:
            name = getattr(tool, "name", "")
            if name in ("novita_sandbox", "vps_exec", "runloop_sandbox", "daytona_sandbox"):
                return tool
    except Exception:  # pragma: no cover - defensive
        return None
    return None


#: Remote bootstrap: install the toolchain, then compile the uploaded sketch.
SANDBOX_SETUP_CMD = (
    "set -e; mkdir -p ~/.arduino-toolchain; "
    "curl -fsSL {installer_url} -o /tmp/install_arduino_sandbox.sh; "
    "bash /tmp/install_arduino_sandbox.sh"
)


def sandbox_install_command(installer_url: str) -> str:
    """Shell command that provisions the Arduino toolchain inside a sandbox."""
    return SANDBOX_SETUP_CMD.format(installer_url=installer_url)


def verify_in_sandbox(
    sandbox: Any,
    code: str,
    board: str = "uno",
    expect: str | None = None,
    ms: int = _DEFAULT_MS,
    workdir: str = "/tmp/arduino_build",
) -> dict[str, Any]:
    """Run compile + simulate inside a remote sandbox.

    ``sandbox`` is any object exposing ``run(command) -> (exit_code, output)``.
    powerx's sandbox tools expose exactly that shape, so they can be passed
    straight in. Returns the same verdict shape as :func:`verify`.
    """
    info = BOARDS.get(board, BOARDS["uno"])
    runner = getattr(sandbox, "run", None) or getattr(sandbox, "execute", None)
    if runner is None:
        return {"ok": False, "log": "sandbox object does not expose run()/execute()"}

    def sh(cmd: str) -> tuple[int, str]:
        try:
            result = runner(cmd)
        except Exception as exc:  # pragma: no cover - transport level
            return 1, f"[sandbox command failed: {exc}]"
        if isinstance(result, tuple) and len(result) == 2:
            return int(result[0]), str(result[1])
        if isinstance(result, dict):
            return int(result.get("exit_code", 0)), str(result.get("output") or result.get("stdout", ""))
        return 0, str(result)

    # 1. Provision the toolchain (idempotent).
    _, out = sh(
        "ls /opt/arduino-toolchain/arduino-cli >/dev/null 2>&1 && echo READY || "
        "bash -lc 'mkdir -p /opt/arduino-toolchain && "
        "curl -fsSL https://downloads.arduino.cc/arduino-cli/arduino-cli_1.5.1_Linux_64bit.tar.gz "
        "| tar -xz -C /opt/arduino-toolchain'"
    )
    scaffold = (
        "mkdir -p /opt/arduino-toolchain/data /opt/arduino-toolchain/dl /opt/arduino-toolchain/sim "
        + workdir
        + " && "
        "export ARDUINO_DIRECTORIES_DATA=/opt/arduino-toolchain/data "
        "ARDUINO_DIRECTORIES_DOWNLOADS=/opt/arduino-toolchain/dl; "
        "/opt/arduino-toolchain/arduino-cli core update-index >/dev/null 2>&1; "
        "/opt/arduino-toolchain/arduino-cli core install arduino:avr >/dev/null 2>&1; "
        "echo SCAFFOLD_OK"
    )
    _, out2 = sh(scaffold)

    # 2. Upload the sketch and compile.
    sketch_dir = f"{workdir}/sketch"
    sh(f"mkdir -p {sketch_dir} && rm -f {sketch_dir}/*.ino")
    payload = _write_remote_file(sandbox, f"{sketch_dir}/sketch.ino", code)
    if not payload:
        return {"ok": False, "log": "could not upload the sketch into the sandbox"}

    rc, log = sh(
        "export ARDUINO_DIRECTORIES_DATA=/opt/arduino-toolchain/data "
        "ARDUINO_DIRECTORIES_DOWNLOADS=/opt/arduino-toolchain/dl; "
        f"/opt/arduino-toolchain/arduino-cli compile --fqbn {info['fqbn']} "
        f"{sketch_dir} --output-dir {workdir}/build"
    )
    compiled = rc == 0
    size = re.search(r"Sketch uses (\d+) bytes \((\d+)%\)", log)
    sim: dict[str, Any] | None = None

    # 3. Install the emulator runtime and simulate.
    if compiled:
        sh(
            "cd /opt/arduino-toolchain/sim 2>/dev/null || mkdir -p /opt/arduino-toolchain/sim && cd /opt/arduino-toolchain/sim; "
            "printf '{\"name\":\"arduino-sim\",\"private\":true}' > package.json; "
            "npm install --no-audit --no-fund avr8js@0.20.0 >/dev/null 2>&1; echo SIM_READY"
        )
        _write_remote_file(sandbox, "/opt/arduino-toolchain/sim/arduino_sim.js", _SIM_ASSET.read_text(encoding="utf-8"))
        cmd = (
            "cd /opt/arduino-toolchain/sim && node arduino_sim.js "
            f"{workdir}/build/sketch.ino.hex --ms {max(100, min(ms, _MAX_MS))}"
        )
        if expect:
            cmd += f" --expect {shlex_quote(expect)}"
        _, sim_out = sh(cmd)
        start = sim_out.find("{")
        if start >= 0:
            try:
                sim = json.loads(sim_out[start:])
                sim["ok"] = bool(sim.get("serial", "").strip())
            except ValueError:
                sim = {"ok": False, "log": _tail(sim_out, 2000)}

    return {
        "compiled": compiled,
        "simulated": bool(sim and sim.get("ok")),
        "compile": {
            "ok": compiled,
            "board": info["label"],
            "fqbn": info["fqbn"],
            "log": _tail(log, 5000),
            "hex": f"{workdir}/build/sketch.ino.hex" if compiled else "",
            "flash_bytes": int(size.group(1)) if size else None,
            "flash_pct": int(size.group(2)) if size else None,
        },
        "simulation": sim,
        "where": "sandbox",
    }


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def _write_remote_file(sandbox: Any, path: str, content: str) -> bool:
    """Write a file into the sandbox using whichever API it exposes."""
    files = getattr(sandbox, "files", None)
    if files is not None and hasattr(files, "write"):
        try:
            files.write(path, content)
            return True
        except Exception:  # pragma: no cover - fall back to heredoc
            pass
    runner = getattr(sandbox, "run", None) or getattr(sandbox, "execute", None)
    if runner is None:
        return False
    import base64

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    try:
        runner(f"mkdir -p $(dirname {path}) && echo {encoded} | base64 -d > {path}")
        return True
    except Exception:  # pragma: no cover - defensive
        return False


def _pi_python() -> str | None:
    """Locate a Python interpreter capable of running the Pi simulator."""
    return sys.executable or shutil.which("python3")


# --------------------------------------------------------------------------- #
# Raspberry Pi support
# --------------------------------------------------------------------------- #

#: Raspberry Pi board variants and their Naira estimates (Kaduna).
PI_BOARDS: dict[str, dict[str, Any]] = {
    "pi5": {"label": "Raspberry Pi 5 (4GB)", "logic_v": 3.3, "price": 135000, "note": "Latest, fastest"},
    "pi4": {"label": "Raspberry Pi 4 Model B (4GB)", "logic_v": 3.3, "price": 95000, "note": "Best value for most projects"},
    "pi3": {"label": "Raspberry Pi 3 Model B+", "logic_v": 3.3, "price": 65000, "note": "Older but capable"},
    "zero2w": {"label": "Raspberry Pi Zero 2 W", "logic_v": 3.3, "price": 35000, "note": "Tiny, Wi-Fi, low power"},
    "pico": {"label": "Raspberry Pi Pico (RP2040)", "logic_v": 3.3, "price": 9000, "note": "Microcontroller, not a Linux Pi"},
}

#: BCM pin safety facts for the 40-pin header.
PI_RESERVED_PINS = {
    0: "ID_SD (HAT EEPROM) — avoid",
    1: "ID_SC (HAT EEPROM) — avoid",
    2: "SDA1 (I2C) — shared bus",
    3: "SCL1 (I2C) — shared bus",
    14: "TXD (serial console) — used by default",
    15: "RXD (serial console) — used by default",
}
PI_MAX_SAFE_CURRENT_MA = 16  # per GPIO pin, realistically
PI_3V3_RAIL_MA = 300
PI_5V_RAIL_MA = 1000

_PI_SENSITIVE_PINS = (2, 3, 14, 15)

#: Libraries the Pi simulator mocks. Anything else is a real dependency.
PI_MOCKED_LIBS = ("RPi.GPIO", "gpiozero", "smbus", "smbus2", "serial", "time")


def pi_sim_script() -> Path | None:
    """Materialise the Raspberry Pi simulator, or None if the asset is missing."""
    asset = Path(__file__).parent / "arduino_assets" / "pi_sim.py"
    if not asset.exists():
        return None
    _SIM_DIR.mkdir(parents=True, exist_ok=True)
    dest = _SIM_DIR / "pi_sim.py"
    source = asset.read_text(encoding="utf-8")
    if not dest.exists() or dest.read_text(encoding="utf-8") != source:
        dest.write_text(source, encoding="utf-8")
    return dest


def _strip_python_comments(code: str) -> str:
    """Remove comments and string literals so only executable code is scanned.

    Wiring is usually documented in comments ("PIR VCC -> 5V"), and scanning
    those as if they were code produces false hazards — a module *powered* from
    5 V is perfectly fine; only a 5 V *signal* into a GPIO is dangerous.
    """
    code = re.sub(r'"""(?:.|\n)*?"""', " ", code)
    code = re.sub(r"'''(?:.|\n)*?'''", " ", code)
    out_lines = []
    for line in code.splitlines():
        in_str: str | None = None
        kept = []
        i = 0
        while i < len(line):
            ch = line[i]
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
                i += 1
                continue
            if ch in "\"'":
                in_str = ch
                i += 1
                continue
            if ch == "#":
                break
            kept.append(ch)
            i += 1
        out_lines.append("".join(kept))
    return "\n".join(out_lines)


def pi_safety_check(code: str, board: str = "pi4") -> list[dict[str, str]]:
    """Static audit of a Raspberry Pi program for common hardware hazards."""
    findings: list[dict[str, str]] = []
    info = PI_BOARDS.get(board, PI_BOARDS["pi4"])
    executable = _strip_python_comments(code)
    lowered = executable.lower()

    used_pins: set[int] = set()
    for match in re.finditer(r"GPIO\.setup\(\s*(\d+)", executable):
        used_pins.add(int(match.group(1)))
    for match in re.finditer(r"GPIO\.(?:output|input)\(\s*(\d+)", executable):
        used_pins.add(int(match.group(1)))
    for match in re.finditer(r"(?:LED|Button|Buzzer|MotionSensor|Servo|Motor|PWMLED)\(\s*(\d+)", executable):
        used_pins.add(int(match.group(1)))

    clashes = sorted(p for p in used_pins if p in _PI_SENSITIVE_PINS)
    if clashes:
        findings.append(
            {
                "level": "WARN",
                "issue": f"GPIO pins {clashes} are used for application IO.",
                "fix": (
                    "Pins 2/3 are the I2C bus and 14/15 the serial console. Sharing them "
                    "with a sensor usually breaks one or the other — move to spare GPIO."
                ),
            }
        )

    # Peripherals whose *signal* line is 5 V and must not feed a 3.3 V GPIO.
    # Hardware facts live in comments too ("HC-SR04 echo -> GPIO17"), so the full
    # source is scanned here — but a line that only describes the power rail
    # ("VCC -> 5V") is not a signal hazard.
    five_volt_signal = (
        "hc-sr04", "hc_sr04", "hc sr04", "ultrasonic", "l298", "l293", "level shifter",
    )
    signal_words = ("echo", "trig", "signal", "out", "data", "sensor", "gpio", "pin")
    power_only = re.compile(r"vcc|vin|\b5v\b|power|supply|rail", re.IGNORECASE)

    signal_hazard = False
    for line in code.splitlines():
        low = line.lower()
        if not any(tok in low for tok in five_volt_signal):
            continue
        stripped = line.strip().lstrip("#").strip()
        mentions_signal = any(w in low for w in signal_words)
        mentions_power_only = bool(power_only.search(stripped)) and not mentions_signal
        if mentions_power_only:
            continue
        signal_hazard = True
        break

    # A bare peripheral name in code (no wiring comment) is still a signal risk.
    if not signal_hazard and any(tok in lowered for tok in five_volt_signal):
        signal_hazard = True

    if signal_hazard:
        findings.append(
            {
                "level": "CRITICAL",
                "issue": (
                    f"{info['label']} GPIO is 3.3 V only, but a peripheral with a 5 V signal "
                    "line is present."
                ),
                "fix": (
                    "Divide the 5 V signal down (1k/2k) or fit a bidirectional level shifter "
                    "before it reaches a GPIO pin. A 5 V signal will damage the SoC."
                ),
            }
        )
    else:
        findings.append(
            {
                "level": "OK",
                "issue": "No 5 V signal lines detected — all peripherals appear 3.3 V safe.",
                "fix": "Modules may still be *powered* from the 5 V rail; only signals matter.",
            }
        )

    # Current budget.
    if re.search(r"Motor|Servo|relay|solenoid", executable):
        findings.append(
            {
                "level": "WARN",
                "issue": "Motors, servos and relays draw far more current than a GPIO pin can source.",
                "fix": (
                    f"A GPIO pin is safe for roughly {PI_MAX_SAFE_CURRENT_MA} mA. Power loads from a "
                    "separate 5 V supply (>=2 A) and share only the ground."
                ),
            }
        )
    if re.search(r"LED|Buzzer", executable) and not re.search(
        r"220|330|470|resistor|1k", code, re.IGNORECASE
    ):
        findings.append(
            {
                "level": "WARN",
                "issue": "An LED/buzzer is driven without an obvious series resistor.",
                "fix": "Fit 220-330 ohm in series with each LED; a bare LED can damage the pin.",
            }
        )

    # Bus contention.
    if re.search(r"GPIO\.setup\(\s*(?:2|3)\b", executable) or re.search(
        r"(?:LED|Button)\(\s*(?:2|3)\b", executable
    ):
        findings.append(
            {
                "level": "WARN",
                "issue": "An I2C pin (2/3) is claimed as plain GPIO.",
                "fix": "If you use any I2C device (LCD, RTC, IMU), those pins must stay on the bus.",
            }
        )

    if re.search(r"time\.sleep\(\s*(?:[1-9]\d*|[1-9]\d*\.\d+)\s*\)", executable):
        findings.append(
            {
                "level": "INFO",
                "issue": "Long time.sleep() calls detected.",
                "fix": "Fine for simple builds; for multitasking use gpiozero callbacks or asyncio.",
            }
        )

    return findings


def build_pi_bom(diagram: dict[str, Any] | None, board: str = "pi4") -> list[dict[str, Any]]:
    """Naira BOM for a Raspberry Pi build, always including the board + SD card."""
    info = PI_BOARDS.get(board, PI_BOARDS["pi4"])
    bom: list[dict[str, Any]] = [
        {"item": info["label"], "qty": 1, "unit_price": info["price"], "note": info["note"]},
        {"item": "microSD card 32GB (Class 10)", "qty": 1, "unit_price": 6500, "note": "OS + storage"},
        {"item": "5V 3A USB-C power supply", "qty": 1, "unit_price": 5500, "note": "Undervoltage causes weird bugs"},
    ]
    parts, _ = parse_diagram(diagram)
    for part in parts:
        pid = str(part.get("type") or part.get("id") or "")
        if "raspberry" in pid.lower() or "rpi" in pid.lower():
            continue
        name = str(part.get("name") or pid)
        price, note = _price_for(pid, name)
        bom.append({"item": name, "qty": 1, "unit_price": price, "note": note})
    for extra in ("jumper wire", "breadboard"):
        price, note = KADUNA_PRICES[extra]
        bom.append({"item": extra.title(), "qty": 1, "unit_price": price, "note": note})
    return bom


def verify_pi(
    code: str,
    board: str = "pi4",
    diagram: dict[str, Any] | None = None,
    expect: str | None = None,
    ms: int = 5000,
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full Raspberry Pi pipeline: syntax -> simulate -> safety -> BOM -> verdict."""
    board = board.lower() if board else "pi4"
    if board not in PI_BOARDS:
        board = "pi4"

    python = _pi_python()
    result: dict[str, Any] = {
        "platform": "raspberry-pi",
        "board": PI_BOARDS[board]["label"],
        "compiled": False,
        "simulated": False,
        "simulation": None,
        "safety": [],
    }
    if not python:
        result["error"] = "no python interpreter available to run the Pi simulator"
        return result

    script = pi_sim_script()
    if script is None:
        result["error"] = "the Raspberry Pi simulator asset (pi_sim.py) is missing"
        return result
    workdir = Path(tempfile.mkdtemp(prefix="pi_"))
    program = workdir / "program.py"
    program.write_text(code, encoding="utf-8")

    cmd = [python, str(script), str(program), "--ms", str(max(100, min(ms, _MAX_MS)))]
    if expect:
        cmd += ["--expect", expect]
    if scenario:
        scen_path = workdir / "scenario.json"
        scen_path.write_text(json.dumps(scenario), encoding="utf-8")
        cmd += ["--scenario", str(scen_path)]

    rc, out = _run(cmd, timeout=180)
    payload = out.strip()
    start = payload.find("{")
    sim: dict[str, Any] | None = None
    if start >= 0:
        try:
            sim = json.loads(payload[start:])
        except ValueError:
            sim = None

    if sim is None:
        result["compile"] = {"ok": False, "log": _tail(out, 3000)}
        result["safety"] = pi_safety_check(code, board)
        result["bom"] = build_pi_bom(diagram, board)
        result["bom_total_naira"] = sum(i["qty"] * i["unit_price"] for i in result["bom"])
        result["confidence_pct"] = 0
        return result

    syntax_ok = sim.get("syntax_error") is None
    ran = sim.get("ok", False)
    result["compiled"] = syntax_ok
    result["simulated"] = bool(ran)
    result["compile"] = {
        "ok": syntax_ok,
        "log": sim.get("syntax_error") or "syntax OK",
        "runtime_error": sim.get("runtime_error"),
    }
    result["simulation"] = sim
    result["safety"] = pi_safety_check(code, board)
    result["bom"] = build_pi_bom(diagram, board)
    result["bom_total_naira"] = sum(i["qty"] * i["unit_price"] for i in result["bom"])
    result["expect"] = expect

    critical = [f for f in result["safety"] if f["level"] == "CRITICAL"]
    confidence = 0
    if syntax_ok:
        confidence += 50
    if ran:
        confidence += 35
    if not critical:
        confidence += 13
    if expect and sim.get("expect_found"):
        confidence = min(99, confidence + 2)
    if not syntax_ok:
        confidence = 0
    result["confidence_pct"] = confidence
    return result


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


def simulate(
    hex_path: str,
    ms: int = _DEFAULT_MS,
    expect: str | None = None,
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run firmware on the headless AVR emulator and capture serial + pin activity.

    ``scenario`` drives inputs during the run (button presses, serial commands)
    so behaviour is *exercised* rather than assumed:
    ``{"inputs": [{"pin": 4, "at_ms": 1000, "state": "low", "hold_ms": 200}],
       "serial_in": [{"at_ms": 500, "bytes": [1]}]}``
    """
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

    scenario_path: Path | None = None
    if scenario:
        scenario_path = Path(hex_path).parent / "scenario.json"
        scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        cmd += ["--scenario", str(scenario_path)]

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
    if expect is not None:
        data["ok"] = bool(data["ok"]) and bool(data.get("expect_found"))
    return data


def verify(
    code: str,
    board: str = "uno",
    diagram: dict[str, Any] | None = None,
    expect: str | None = None,
    ms: int = _DEFAULT_MS,
    max_tries: int = _MAX_TRIES,
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full pipeline: compile -> simulate -> safety -> BOM -> verdict."""
    board = board.lower() if board else "uno"
    if board not in BOARDS:
        board = "uno"

    compile_result = compile_sketch(code, board)
    sim_result: dict[str, Any] | None = None
    attempts = 1

    while compile_result["ok"] and attempts <= max(1, max_tries):
        sim_result = simulate(compile_result["hex"], ms=ms, expect=expect, scenario=scenario)
        if sim_result.get("ok"):
            break
        # Nothing to auto-fix here (wiring/expectation issue) — report honestly.
        break

    findings = safety_check(code, board, diagram)
    findings = capability_check(code, board, diagram) + findings
    bom = build_bom(diagram, board)
    total = sum(item["qty"] * item["unit_price"] for item in bom)

    critical = [f for f in findings if f["level"] == "CRITICAL"]
    impossible = [f for f in findings if f["level"] == "IMPOSSIBLE"]
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
    if impossible:
        # A board that cannot meet the requirement can never be "safe to buy".
        confidence = min(confidence, 35)

    return {
        "compiled": compiled,
        "simulated": simulated,
        "compile": compile_result,
        "simulation": sim_result,
        "safety": findings,
        "impossible": impossible,
        "bom": bom,
        "bom_total_naira": total,
        "confidence_pct": confidence,
        "expect": expect,
        "scenario": scenario,
        "diagram_summary": diagram_summary(diagram) if diagram_summary else None,
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
            enum=["build", "compile", "simulate", "safety", "bom", "diagram", "setup", "pi"],
        ),
        code=StringSchema("Full Arduino sketch (.ino) source, including every #include"),
        board=StringSchema(
            "Target board key. Arduino: uno (default), nano, mega, leonardo, esp32, esp8266. "
            "Raspberry Pi (action=pi): pi5, pi4 (default), pi3, zero2w, pico.",
        ),
        diagram=ObjectSchema(description="Wokwi/Velxio diagram.json content (parts + connections)"),
        expect=StringSchema("Serial text that must appear for the simulation to pass, e.g. 'Servo moving'"),
        ms=IntegerSchema(description="Milliseconds of firmware execution to simulate", minimum=100, maximum=_MAX_MS),
        hex=StringSchema("Firmware .hex path for the simulate action"),
        scenario=ObjectSchema(
            description=(
                "Optional stimulus to DRIVE inputs during simulation instead of "
                "assuming them: {inputs: [{pin, at_ms, state:'low', hold_ms}], "
                "serial_in: [{at_ms, bytes:[..]}]}"
            )
        ),
        title=StringSchema("Title shown on a rendered diagram (action=diagram)"),
        install_esp32=BooleanSchema(description="Set true during setup to also install the ESP32 core"),
    )
)
class ArduinoVerifyTool(Tool):
    """Compile, simulate, and safety-check Arduino builds before recommending parts."""

    _scopes = {"core", "subagent"}

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        """Available locally *or* wherever an execution sandbox can host it.

        The gateway image does not ship the toolchain, so in production the build
        runs inside the user's sandbox (Novita / VPS / Runloop). Enable whenever
        a local toolchain exists, a sandbox tool is reachable, or the operator
        forces it on with ``ARDUINO_VERIFY_ENABLED=1``.
        """
        if os.getenv("ARDUINO_VERIFY_ENABLED", "").strip() in ("1", "true", "yes"):
            return True
        if _local_toolchain_available():
            return True
        return _sandbox_tool(ctx) is not None

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
            "SUPPORTS RASPBERRY PI TOO: action='pi' runs a Python program against mocked "
            "RPi.GPIO / gpiozero / smbus / serial, so you can observe which pins it drives "
            "and in what order, with a virtual clock so sleeps and infinite loops still "
            "yield a transcript. Use action='pi' for any Raspberry Pi (Python) project and "
            "action='build' for Arduino (.ino) projects. "
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
        scenario = kwargs.get("scenario")
        if isinstance(scenario, str):
            try:
                scenario = json.loads(scenario)
            except ValueError:
                return ToolResult.error("scenario must be valid JSON.")
        if not isinstance(scenario, dict):
            scenario = None
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

            if action == "diagram":
                if render_svg is None:
                    return ToolResult.error("the diagram renderer is not available in this install.")
                if not diagram:
                    return ToolResult.error("diagram (parts + connections) is required for action=diagram.")
                svg = render_svg(
                    diagram,
                    title=str(kwargs.get("title") or "Circuit Diagram"),
                    board=BOARDS.get(board, BOARDS["uno"])["label"],
                )
                return json.dumps(
                    {
                        "format": "svg",
                        "svg": svg,
                        "summary": diagram_summary(diagram) if diagram_summary else None,
                    },
                    indent=2,
                )

            # default: full build
            if not code.strip():
                return ToolResult.error("code is required for the build pipeline.")

            # Raspberry Pi programs are Python: route to the Pi simulator.
            if action == "pi":
                result = await asyncio.to_thread(
                    verify_pi, code, kwargs.get("board") or "pi4", diagram, expect, ms, scenario
                )
                result["where"] = "local"
                return json.dumps(result, indent=2)

            # Prefer the sandbox when no local toolchain ships in this image:
            # hardware builds belong in the execution sandbox that owns the work.
            sandbox = None if _local_toolchain_available() else _sandbox_tool(self._ctx)
            if sandbox is not None:
                remote = await self._build_in_sandbox(sandbox, code, board, expect, ms)
                if remote is not None:
                    findings = safety_check(code, board, diagram)
                    bom = build_bom(diagram, board)
                    critical = [f for f in findings if f["level"] == "CRITICAL"]
                    confidence = 0
                    if remote["compiled"]:
                        confidence += 50
                    if remote["simulated"]:
                        confidence += 35
                    if not critical:
                        confidence += 13
                    remote.update(
                        {
                            "safety": findings,
                            "bom": bom,
                            "bom_total_naira": sum(i["qty"] * i["unit_price"] for i in bom),
                            "confidence_pct": confidence,
                            "expect": expect,
                            "where": "sandbox",
                        }
                    )
                    return json.dumps(remote, indent=2)

            result = await asyncio.to_thread(verify, code, board, diagram, expect, ms, _MAX_TRIES, scenario)
            result["where"] = "local"
            return json.dumps(result, indent=2)
        except ArduinoVerificationError as exc:
            return ToolResult.error(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("arduino_verify failed")
            return ToolResult.error(f"arduino_verify failed: {exc}")

    def __init__(self, ctx: ToolContext | None = None) -> None:
        self._ctx: ToolContext | None = ctx

    @classmethod
    def create(cls, ctx: ToolContext) -> "ArduinoVerifyTool":
        """Carry the tool context so the sandbox tool can be resolved later."""
        return cls(ctx)

    async def _build_in_sandbox(
        self, sandbox_tool: Any, code: str, board: str, expect: str | None, ms: int
    ) -> dict[str, Any] | None:
        """Drive a sandbox tool through the setup -> compile -> simulate loop."""
        try:
            setup = await sandbox_tool.execute(
                action="setup",
                command=sandbox_install_command(
                    "https://raw.githubusercontent.com/Arinze-eng/powerx/main/"
                    "scripts/install_arduino_sandbox.sh"
                ),
            )
            logger.info("arduino_verify: sandbox setup -> {}", str(setup)[:200])

            # The sandbox tools return human-readable output; the compile step
            # re-uses the uploaded installer's environment on subsequent calls.
            scaffold = await sandbox_tool.execute(
                action="run",
                command=(
                    "export ARDUINO_TOOLCHAIN_DIR=/opt/arduino-toolchain "
                    "ARDUINO_SIM_DIR=/opt/arduino-toolchain/sim "
                    "ARDUINO_VERIFY_ENABLED=1; "
                    "/opt/arduino-toolchain/arduino-cli version"
                ),
            )
            if "arduino-cli" not in str(scaffold) and "Version" not in str(scaffold):
                return None
            return {"compiled": False, "simulated": False, "log": str(scaffold)[:1000]}
        except Exception as exc:  # pragma: no cover - transport level
            logger.warning("arduino_verify: sandbox path unavailable ({})", exc)
            return None
