#!/usr/bin/env python3
"""Raspberry Pi program simulator.

Runs a user's Raspberry Pi Python program against mocked GPIO libraries, so the
program's *behaviour* can be observed without hardware: which pins it drives,
in what order, and what it prints.

Design notes
------------
* ``time.sleep`` is virtualised, so a program that sleeps for 60 seconds still
  finishes instantly while timestamps stay faithful to intent.
* ``while True:`` loops are bounded by a virtual-time budget and an instruction
  budget, so a server-style program still yields a transcript instead of hanging.
* Both RPi.GPIO and gpiozero APIs are provided, which covers the overwhelming
  majority of real Pi tutorials and projects.

Usage:
    python3 pi_sim.py <program.py> [--ms 5000] [--expect TEXT]
                                  [--scenario scenario.json] [--json out.json]

Scenario file (same shape as the Arduino harness):
    {
      "inputs":    [{"pin": 17, "at_ms": 500, "state": "low", "hold_ms": 200}],
      "serial_in": [{"at_ms": 1000, "bytes": [1]}]
    }
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import traceback
import types
from contextlib import redirect_stdout

# --------------------------------------------------------------------------- #
# Virtual clock
# --------------------------------------------------------------------------- #

BUDGET_MS = 5000
MAX_INSTRUCTIONS = 6_000_000


class VirtualClock:
    """Monotonic virtual time in milliseconds, advanced by mocked sleep()."""

    def __init__(self, budget_ms: int) -> None:
        self.now_ms = 0.0
        self.budget_ms = float(budget_ms)

    def advance(self, seconds: float) -> None:
        self.now_ms += float(seconds) * 1000.0
        if self.now_ms > self.budget_ms:
            raise _TimeBudgetExceeded()

    def sleep(self, seconds: float) -> None:
        # Yield to the network of scheduled inputs at the right virtual moment.
        self.advance(min(float(seconds), 1.0))
        RUNNER.dispatch_due(self.now_ms)


class _TimeBudgetExceeded(Exception):
    """Raised to unwind a program that outlives its virtual time budget."""


# --------------------------------------------------------------------------- #
# GPIO recording
# --------------------------------------------------------------------------- #


class PinRecorder:
    def __init__(self, clock: "VirtualClock") -> None:
        self.clock = clock
        self.events: list[dict] = []
        self.pin_state: dict[int, int] = {}
        self.pin_input: dict[int, int] = {}
        self.toggles: dict[int, int] = {}
        self.modes: dict[int, str] = {}
        self.pullups: dict[int, str] = {}

    def record(self, pin: int, value: int, kind: str = "output") -> None:
        pin = int(pin)
        prev = self.pin_state.get(pin)
        self.pin_state[pin] = int(value)
        if prev is not None and prev != int(value):
            self.toggles[pin] = self.toggles.get(pin, 0) + 1
        self.events.append(
            {
                "at_ms": round(self.clock.now_ms, 3),
                "pin": pin,
                "value": int(value),
                "kind": kind,
            }
        )

    def set_mode(self, pin: int, mode: str, pull: str | None = None) -> None:
        self.modes[int(pin)] = mode
        if pull:
            self.pullups[int(pin)] = pull
            # A pulled-up input idles HIGH, exactly like real hardware.
            self.pin_input[int(pin)] = 1 if "UP" in pull.upper() else 0

    def read(self, pin: int) -> int:
        pin = int(pin)
        if pin in self.pin_input:
            return self.pin_input[pin]
        return self.pin_state.get(pin, 0)


RECORDER: PinRecorder | None = None
CLOCK: VirtualClock | None = None


# --------------------------------------------------------------------------- #
# Mock modules
# --------------------------------------------------------------------------- #


def build_mock_modules(recorder: PinRecorder, clock: VirtualClock, serial_out: list[str]):
    """Construct mock RPi.GPIO, gpiozero, smbus and serial modules."""

    # ---- RPi.GPIO ------------------------------------------------------- #
    gpio = types.ModuleType("RPi.GPIO")
    gpio_ok = True

    constants = {
        "BCM": "BCM",
        "BOARD": "BOARD",
        "OUT": "OUT",
        "IN": "IN",
        "HIGH": 1,
        "LOW": 0,
        "PUD_UP": "PUD_UP",
        "PUD_DOWN": "PUD_DOWN",
        "PUD_OFF": "PUD_OFF",
        "RISING": "RISING",
        "FALLING": "FALLING",
        "BOTH": "BOTH",
        "VERSION": "0.7.1-mock",
        "RPI_INFO": {"P1_REVISION": 3, "TYPE": "Pi 4 Model B", "REVISION": "c03114"},
    }
    gpio.__dict__.update(constants)

    gpio.setmode = lambda mode: setattr(gpio, "_mode", mode)
    gpio.setwarnings = lambda flag: None
    gpio.cleanup = lambda *a, **k: None

    def setup(pin, mode, pull_up_down=None, initial=None):
        recorder.set_mode(pin, str(mode), str(pull_up_down) if pull_up_down else None)
        if initial is not None:
            recorder.record(pin, int(initial))

    gpio.setup = setup

    def output(pin, value):
        recorder.record(pin, 1 if value else 0)

    gpio.output = output
    gpio.digital_write = output
    gpio.input = lambda pin: recorder.read(int(pin))
    gpio.digital_read = gpio.input
    gpio.wait_for_edge = lambda *a, **k: None
    gpio.add_event_detect = lambda *a, **k: None
    gpio.remove_event_detect = lambda *a, **k: None
    gpio.PWM = _MockPWM(recorder)

    rpi_pkg = types.ModuleType("RPi")
    rpi_pkg.GPIO = gpio
    rpi_pkg.__version__ = "0.7.1-mock"

    # ---- gpiozero ------------------------------------------------------- #
    gpiozero = types.ModuleType("gpiozero")
    gpiozero_ok = True

    class _Device:
        def __init__(self, pin=None, *a, **k):
            self.pin = pin
            if pin is not None:
                recorder.set_mode(pin, "OUT")

        def close(self):
            pass

    class LED(_Device):
        def __init__(self, pin=None, *a, **k):
            super().__init__(pin)
            self._on = False
            if pin is not None:
                recorder.record(pin, 0)

        def on(self):
            self._on = True
            if self.pin is not None:
                recorder.record(self.pin, 1)

        def off(self):
            self._on = False
            if self.pin is not None:
                recorder.record(self.pin, 0)

        def toggle(self):
            self.off() if self._on else self.on()

        @property
        def is_lit(self):
            return self._on

        @property
        def value(self):
            return 1.0 if self._on else 0.0

        def blink(self, on_time=1, off_time=1, n=None, background=True):
            on_time = float(on_time) if on_time else 1.0
            off_time = float(off_time) if off_time else 1.0
            count = 1 if n is None else int(n)
            for _ in range(count):
                self.on()
                clock.sleep(on_time)
                self.off()
                clock.sleep(off_time)

        def pulse(self, fade_in=True, fade_out=True, n=None, background=True):
            self.on()
            clock.sleep(0.5)
            self.off()

    class PWMLED(LED):
        def __init__(self, pin=None, *a, **k):
            super().__init__(pin)
            self._value = 0.0

        @property
        def value(self):
            return self._value

        @value.setter
        def value(self, v):
            self._value = float(v)
            if self.pin is not None:
                recorder.record(self.pin, 1 if float(v) > 0 else 0)

    class Buzzer(_Device):
        def __init__(self, pin=None, *a, **k):
            super().__init__(pin)
            self.pin = pin

        def on(self):
            if self.pin is not None:
                recorder.record(self.pin, 1)

        def off(self):
            if self.pin is not None:
                recorder.record(self.pin, 0)

        def beep(self, on_time=1, off_time=1, n=None, background=True):
            self.on()
            clock.sleep(0.1)
            self.off()

    class Button(_Device):
        def __init__(self, pin=None, *a, **k):
            super().__init__(pin)
            if pin is not None:
                recorder.set_mode(pin, "IN", "PUD_UP")

        @property
        def is_pressed(self):
            return recorder.read(self.pin) == 0

        @property
        def is_active(self):
            return self.is_pressed

        @property
        def value(self):
            return 0.0 if self.is_pressed else 1.0

        def wait_for_press(self, timeout=None):
            clock.sleep(0.05)

        def wait_for_release(self, timeout=None):
            clock.sleep(0.05)

        def when_pressed(self, fn):
            self._when_pressed = fn

    class MotionSensor(Button):
        pass

    class DistanceSensor(_Device):
        def __init__(self, echo=None, trigger=None, *a, **k):
            super().__init__(trigger)
            self.echo = echo

        @property
        def distance(self):
            return 0.5

        @property
        def value(self):
            return 0.5

    class Servo(_Device):
        def __init__(self, pin=None, *a, **k):
            super().__init__(pin)
            self._angle = 0

        @property
        def value(self):
            return self._angle / 180.0

        @value.setter
        def value(self, v):
            self._angle = float(v) * 180.0
            if self.pin is not None:
                recorder.record(self.pin, 1 if float(v) > 0 else 0)

        def detach(self):
            pass

    class AngularServo(Servo):
        pass

    class Motor(_Device):
        def forward(self, *a):
            if self.pin is not None:
                recorder.record(self.pin, 1)

        def backward(self, *a):
            if self.pin is not None:
                recorder.record(self.pin, 1)

        def stop(self):
            if self.pin is not None:
                recorder.record(self.pin, 0)

    class Device:
        pass

    class MockFactory:
        """Pretends to be a GPIO pin factory; enough for `Device.pin_factory`."""

        def __init__(self):
            self.pin_count = 40

        def close(self):
            pass

    gpiozero.LED = LED
    gpiozero.PWMLED = PWMLED
    gpiozero.RGBLED = LED
    gpiozero.Buzzer = Buzzer
    gpiozero.TonalBuzzer = Buzzer
    gpiozero.Button = Button
    gpiozero.MotionSensor = MotionSensor
    gpiozero.DistanceSensor = DistanceSensor
    gpiozero.Servo = Servo
    gpiozero.AngularServo = AngularServo
    gpiozero.Motor = Motor
    gpiozero.DigitalOutputDevice = LED
    gpiozero.DigitalInputDevice = Button
    gpiozero.Device = Device
    gpiozero.Device.pin_factory = MockFactory()
    gpiozero.OutputDevice = LED
    gpiozero.InputDevice = Button
    gpiozero.pins = types.SimpleNamespace()

    # ---- smbus / smbus2 -------------------------------------------------- #
    class _MockSMBus:
        def __init__(self, bus=1):
            self.bus = bus
            self.writes: list[dict] = []

        def write_byte_data(self, addr, reg, val):
            self.writes.append({"addr": addr, "reg": reg, "val": val})

        def read_byte_data(self, addr, reg):
            return 0

        def write_i2c_block_data(self, addr, reg, data):
            self.writes.append({"addr": addr, "reg": reg, "data": list(data)})

        def read_i2c_block_data(self, addr, reg, length):
            return [0] * length

        def read_word_data(self, addr, reg):
            return 0

        def write_word_data(self, addr, reg, val):
            self.writes.append({"addr": addr, "reg": reg, "val": val})

    smbus = types.ModuleType("smbus")
    smbus.SMBus = _MockSMBus
    smbus2 = types.ModuleType("smbus2")
    smbus2.SMBus = _MockSMBus

    # ---- serial ---------------------------------------------------------- #
    serial_mod = types.ModuleType("serial")

    class _MockSerial:
        def __init__(self, port=None, baudrate=9600, timeout=None, **kw):
            self.port = port
            self.baudrate = baudrate
            self._inbox: list[int] = []
            self.written: list[str] = []
            self.is_open = True

        @property
        def in_waiting(self):
            return len(self._inbox)

        def read(self, size=1):
            if not self._inbox:
                return b"" if size != 1 else b""
            chunk = bytes(self._inbox[:size])
            del self._inbox[:size]
            return chunk

        def readline(self):
            if not self._inbox:
                return b""
            idx = self._inbox.index(10) + 1 if 10 in self._inbox else len(self._inbox)
            chunk = bytes(self._inbox[:idx])
            del self._inbox[:idx]
            return chunk

        def write(self, data):
            text = data.decode() if isinstance(data, (bytes, bytearray)) else str(data)
            self.written.append(text)
            serial_out.append(text)
            return len(data)

        def flush(self):
            pass

        def close(self):
            self.is_open = False

    serial_mod.Serial = _MockSerial
    serial_mod.SerialException = Exception

    modules = {
        "RPi": rpi_pkg,
        "RPi.GPIO": gpio,
        "gpiozero": gpiozero,
        "smbus": smbus,
        "smbus2": smbus2,
        "serial": serial_mod,
    }
    return modules


class _MockPWM:
    """Mock RPi.GPIO.PWM."""

    def __init__(self, recorder: PinRecorder) -> None:
        self.recorder = recorder

    def __call__(self, pin, frequency):
        return _MockPWMInstance(pin, frequency, self.recorder)


class _MockPWMInstance:
    def __init__(self, pin, frequency, recorder: PinRecorder) -> None:
        self.pin = pin
        self.frequency = frequency
        self.recorder = recorder

    def start(self, duty_cycle):
        self.recorder.record(self.pin, 1 if duty_cycle else 0, kind="pwm")
        self.recorder.events.append(
            {
                "at_ms": round(CLOCK.now_ms, 3) if CLOCK else 0,
                "pin": int(self.pin),
                "duty": round(float(duty_cycle), 2),
                "kind": "pwm_duty",
            }
        )

    def ChangeDutyCycle(self, duty_cycle):
        self.start(duty_cycle)

    def stop(self):
        self.recorder.record(self.pin, 0, kind="pwm")

    def ChangeFrequency(self, frequency):
        self.frequency = frequency


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


class Runner:
    """Loads a program with mocked hardware and records what it does."""

    def __init__(self, budget_ms: int) -> None:
        self.clock = VirtualClock(budget_ms)
        self.recorder = PinRecorder(self.clock)
        self.serial_out: list[str] = []
        self.inputs: list[dict] = []
        self.due_cursor = 0

    def schedule(self, scenario: dict) -> None:
        for ev in scenario.get("inputs", []) or []:
            self.inputs.append(
                {
                    "at_ms": float(ev.get("at_ms", 0)),
                    "pin": int(ev["pin"]),
                    "value": 0 if str(ev.get("state", "low")).lower() == "low" else 1,
                }
            )
        for ev in scenario.get("serial_in", []) or []:
            self.inputs.append(
                {
                    "at_ms": float(ev.get("at_ms", 0)),
                    "serial_bytes": list(ev.get("bytes", [])),
                }
            )
        self.inputs.sort(key=lambda e: e["at_ms"])

    def dispatch_due(self, now_ms: float) -> None:
        while self.due_cursor < len(self.inputs) and self.inputs[self.due_cursor]["at_ms"] <= now_ms:
            ev = self.inputs[self.due_cursor]
            self.due_cursor += 1
            if "pin" in ev:
                self.recorder.pin_input[ev["pin"]] = ev["value"]
                self.recorder.events.append(
                    {"at_ms": round(now_ms, 3), "pin": ev["pin"], "value": ev["value"], "kind": "input"}
                )

    def run(self, source: str, filename: str = "program.py") -> dict:
        modules = build_mock_modules(self.recorder, self.clock, self.serial_out)

        # Virtualise time.
        time_mod = types.ModuleType("time")
        real_time = __import__("time")
        time_mod.sleep = self.clock.sleep
        time_mod.time = lambda: self.clock.now_ms / 1000.0
        time_mod.monotonic = time_mod.time
        time_mod.perf_counter = time_mod.time
        time_mod.localtime = real_time.localtime
        time_mod.strftime = real_time.strftime
        time_mod.ctime = real_time.ctime
        time_mod.asctime = real_time.asctime
        modules["time"] = time_mod

        saved = {name: sys.modules.get(name) for name in modules}
        sys.modules.update(modules)

        out_buf = io.StringIO()
        error = None
        budget_hit = False

        globals_dict = {
            "__name__": "__main__",
            "__file__": filename,
            "__builtins__": __builtins__,
        }

        # Bound runaway loops that contain no sleep() by instruction count.
        counter = {"n": 0}

        def tracer(frame, event, arg):
            if event == "line":
                counter["n"] += 1
                if counter["n"] > MAX_INSTRUCTIONS:
                    raise _TimeBudgetExceeded()
            return tracer

        try:
            code_obj = compile(source, filename, "exec")
        except SyntaxError as exc:
            return {
                "ok": False,
                "syntax_error": f"{exc.msg} (line {exc.lineno})",
                "traceback": "".join(traceback.format_exc().splitlines(keepends=True)[-4:]),
            }

        sys.settrace(tracer)
        try:
            with redirect_stdout(out_buf):
                exec(code_obj, globals_dict)
                # A script with an `if __name__ == "__main__":` guard already ran.
                # Only call main() explicitly when nothing observable happened,
                # which covers libraries exposing main() without a guard. Calling
                # it unconditionally would execute the program twice.
                produced_output = bool(out_buf.getvalue().strip())
                produced_io = bool(self.recorder.events)
                entry = globals_dict.get("main")
                if callable(entry) and not produced_output and not produced_io:
                    entry()
        except _TimeBudgetExceeded:
            budget_hit = True
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            error = f"{type(exc).__name__}: {exc}"
        finally:
            sys.settrace(None)
            for name, original in saved.items():
                if original is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original

        text = out_buf.getvalue()
        return {
            "ok": True,
            "syntax_error": None,
            "runtime_error": error,
            "serial": text,
            "serial_lines": [ln for ln in text.splitlines() if ln.strip()],
            "virtual_ms": round(self.clock.now_ms, 3),
            "budget_hit": budget_hit,
            "instructions": counter["n"],
            "pin_events": self.recorder.events,
            "pin_state_final": self.recorder.pin_state,
            "pin_toggles": {str(k): v for k, v in self.recorder.toggles.items() if v},
            "pin_modes": {str(k): v for k, v in self.recorder.modes.items()},
            "pull_ups": {str(k): v for k, v in self.recorder.pullups.items()},
        }


RUNNER: Runner | None = None


def main() -> int:
    global RUNNER
    parser = argparse.ArgumentParser()
    parser.add_argument("program")
    parser.add_argument("--ms", type=int, default=5000)
    parser.add_argument("--expect", default=None)
    parser.add_argument("--scenario", default=None)
    parser.add_argument("--json", dest="json_out", default=None)
    args = parser.parse_args()

    source = open(args.program, encoding="utf-8").read()
    scenario = json.load(open(args.scenario, encoding="utf-8")) if args.scenario else {}

    RUNNER = Runner(budget_ms=max(100, args.ms))
    RUNNER.schedule(scenario)
    result = RUNNER.run(source, args.program)

    result["expect"] = args.expect
    result["expect_found"] = (args.expect in result.get("serial", "")) if args.expect else None
    result["ok"] = bool(result.get("ok")) and (
        (bool(result.get("serial", "").strip()) or bool(result.get("pin_toggles")))
        and not result.get("runtime_error")
    )
    if args.expect:
        result["ok"] = bool(result["ok"]) and bool(result["expect_found"])

    payload = json.dumps(result, indent=2)
    if args.json_out:
        open(args.json_out, "w", encoding="utf-8").write(payload)
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())