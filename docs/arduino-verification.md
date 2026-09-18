# Arduino Verification

Give the agent the ability to **prove** an Arduino build works before a user
spends money on parts. No more "this should work" — every answer is backed by a
real compile log and a real simulation transcript.

## What it does

| Stage | Implementation | Proof produced |
|---|---|---|
| **Compile** | `arduino-cli` (`arduino:avr` core) | `Sketch uses N bytes (X%) of program storage space` |
| **Simulate** | Headless ATmega328P emulator (`avr8js`, Node) | USART serial transcript + per-pin GPIO toggle counts |
| **Safety** | Static audit of sketch + diagram | `CRITICAL` / `WARN` / `INFO` findings with fixes |
| **BOM** | Diagram parts → Naira price table | Itemised Kaduna Computer Village quote |

The agent exposes this through the **`arduino_verify`** tool and the
**`arduino-verification`** skill (`nanobot/skills/arduino-verification/SKILL.md`).

## Tool actions

```
arduino_verify action=setup                                    # install toolchain + avr8js
arduino_verify action=compile  code=<ino> board=uno            # → hex path + build log
arduino_verify action=simulate hex=<path> expect="Servo moving" # → serial + pin activity
arduino_verify action=safety   code=<ino> diagram={...}        # → hazard report
arduino_verify action=bom      diagram={...}                   # → Naira BOM
arduino_verify action=build    code=<ino> diagram={...} expect="..."  # full pipeline + verdict
```

`action=build` returns `compiled`, `simulated`, `confidence_pct`, the full
compile log, the serial transcript, safety findings, and the priced BOM.

## Example (verified end-to-end in this repo's sandbox)

Input: a PIR + servo sketch with `expect="Servo"`.

Result:

```json
{
  "compiled": true,
  "simulated": true,
  "confidence_pct": 99,
  "compile": { "flash_bytes": 3308, "flash_pct": 10, "ram_bytes": 265, "ram_pct": 12 },
  "simulation": {
    "serial": "System ready\r\nServo idle\r\nServo idle\r\n...",
    "active_pins": [{ "pin": 9, "toggles": 296 }]
  },
  "bom_total_naira": 28000
}
```

Pin 9 toggling 296 times is the servo PWM carrier being observed on the GPIO
port — concrete evidence the actuator code path actually executes.

## Installing the toolchain

**The toolchain is not baked into the gateway image.** It is installed at
runtime inside the execution sandbox (Novita / VPS / Runloop), because that is
where the work — and the network access to fetch cores — actually lives.

### How the agent installs it (automatic)

The tool exposes `arduino_verify action=setup`, which runs
`scripts/install_arduino_sandbox.sh` inside the sandbox. When the gateway image
has no local toolchain, `action=build` dispatches to the sandbox automatically:

```
1. arduino_verify  →  resolves the novita_sandbox / vps_exec / runloop_sandbox tool
2. setup           →  installs arduino-cli + arduino:avr + avr8js in the sandbox
3. upload          →  writes sketch.ino into the sandbox workspace
4. compile         →  arduino-cli compile --fqbn arduino:avr:uno
5. simulate        →  node arduino_sim.js <hex> --expect "<text>"
6. verdict         →  compiled / simulated / confidence + safety + Naira BOM
```

The returned payload carries `"where": "sandbox"` so callers can see which host
did the work.

### Manual install (once per sandbox)

```bash
bash scripts/install_arduino_sandbox.sh
```

Through the agent:

```
novita_sandbox action=run command="bash /workspace/install_arduino_sandbox.sh"
```

Then export the paths so the tool can find them:

```bash
export ARDUINO_TOOLCHAIN_DIR=/opt/arduino-toolchain
export ARDUINO_SIM_DIR=/opt/arduino-toolchain/sim
export ARDUINO_VERIFY_ENABLED=1
```

Set `INSTALL_ESP32=1` to also pull the (large) ESP32 core. The installer handles
x86_64 and arm64, and falls back to `$HOME/.arduino-toolchain` when `/opt` is
not writable.

### Why not bake it into the image?

- The ESP32/AVR cores plus toolchains add hundreds of MB and a slow download to
  every gateway build — and an intermittent network failure there fails the
  whole image build.
- Sandboxes are disposable: a toolchain installed there costs nothing at rest.
- The tool works locally too (developer laptops, CI) when a toolchain exists.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `ARDUINO_TOOLCHAIN_DIR` | `/opt/arduino-toolchain` | arduino-cli install root |
| `ARDUINO_SIM_DIR` | `<toolchain>/sim` | Node runtime holding `arduino_sim.js` + `avr8js` |
| `ARDUINO_CLI_PATH` | auto | Explicit arduino-cli binary path |
| `ARDUINO_VERIFY_ENABLED` | unset | `1` forces the tool on regardless of toolchain detection |

The tool self-gates: `enabled()` returns `False` when neither arduino-cli nor
node is present, so installs that never touch hardware pay no prompt cost.

## Supported boards

`uno` (default), `nano`, `mega`, `leonardo` — 5 V AVR.
`esp32`, `esp8266` — 3.3 V (compile only; the AVR emulator covers the Uno).

## Safety checks

- 5 V peripherals (HC-SR04, relay, servo, buzzer) on a 3.3 V board → **CRITICAL**, needs a level shifter.
- Digital pins 0/1 (hardware UART) used for IO → **WARN**, move to 2/3.
- `delay()` inside an interrupt handler → **CRITICAL**, use a `volatile` flag.
- Servo/relay/motor present → **WARN**, power from a separate 5 V rail, share ground.
- LED without a series resistor → **WARN**, add 220–330 Ω.

## Notes and limits

- The emulator executes AVR firmware; it models timers, USART, GPIO, and ADC.
  Sensors are **not** physically modelled, so simulated serial output reflects
  the firmware's own logic (sensor values read as 0/LOW unless the sketch
  self-drives them). Treat the simulation as proof the firmware *runs and
  emits* what you expect — not proof a physical sensor is wired correctly.
- Prices are Naira estimates for Kaduna Computer Village and drift with FX and
  stock. Always tell the user to confirm at the market.