---
name: arduino-verification
description: Build, compile, simulate, and safety-check Arduino projects with the arduino_verify tool before recommending parts.
homepage: https://github.com/davidmonterocrespo24/velxio
metadata: {"nanobot":{"emoji":"🔌","requires":{"bins":["node","arduino-cli"]}}}
---

# Arduino Verification

Never tell a user "this should work". Prove it: **compile → simulate → safety-check → BOM**.
The `arduino_verify` tool does all four. Only report success after a real compile log
and a real simulation transcript exist.

## Tool actions

| Action | What it does |
|---|---|
| `setup` | Install arduino-cli + `arduino:avr` core + common libraries. Pass `install_esp32: true` for ESP32. |
| `compile` | Compile `code` for `board`, return build log + hex path + flash/RAM usage. |
| `simulate` | Execute a `hex` on the headless ATmega328P emulator; return serial transcript + pin activity. |
| `safety` | Static audit of `code` + `diagram` for wiring/ISR hazards. |
| `bom` | Price the diagram's parts in Naira (Kaduna Computer Village estimates). |
| `build` | Full pipeline (`compile` → `simulate` → `safety` → `bom`) with a verdict + confidence %. |

## Required workflow for every build request

1. **Generate three artifacts** before compiling:
   - `sketch.ino` — complete code, every `#include` present.
   - `diagram.json` — Wokwi/Velxio format, `parts` + `connections`.
   - Decide the `expect` string — the serial text that proves the firmware works
     (e.g. `"Servo moving"`, `"Door open"`, `"Motion detected"`).
2. **Run `action=build`** with `code`, `board`, `diagram`, and `expect`.
3. **Read the result honestly**:
   - `compile.ok == false` → read `compile.log`, fix the code, rerun. Max 5 tries.
   - `simulated == false` or `expect_found == false` → the firmware ran but did not
     emit the expected text. Fix the code or wiring, or fix the `expect` string.
4. **Report only after both pass.** Quote the compile log line
   (`Sketch uses N bytes (X%)`) and the actual serial transcript.

## Board keys

`uno` (default, 5 V), `nano`, `mega`, `leonardo`, `esp32` (3.3 V), `esp8266` (3.3 V).

If the user has no board, default to Arduino Uno R3 (`uno`).

## diagram.json format (Wokwi / Velxio)

```json
{
  "version": 1,
  "author": "powerx",
  "editor": "wokwi",
  "parts": [
    { "type": "wokwi-arduino-uno", "id": "uno1", "top": 0, "left": 0, "attrs": {} },
    { "type": "wokwi-servo", "id": "servo1", "top": -80, "left": 150, "attrs": {} },
    { "type": "wokwi-pir", "id": "pir1", "top": 90, "left": 150, "attrs": {} }
  ],
  "connections": [
    [ "uno1:2", "pir1:OUT", "green", [ "v0" ] ],
    [ "uno1:5V", "pir1:VCC", "red", [ "v0" ] ],
    [ "uno1:GND", "pir1:GND", "black", [ "v0" ] ],
    [ "uno1:9", "servo1:PWM", "orange", [ "v0" ] ],
    [ "uno1:5V", "servo1:V+", "red", [ "v0" ] ],
    [ "uno1:GND", "servo1:GND", "black", [ "v0" ] ]
  ]
}
```

Connection rule: `[ "<part>:<pin>", "<part>:<pin>", "<color>", [ "<route>" ] ]`.
Route steps: `"h<n>"` horizontal, `"v<n>"` vertical, `"<part>:<pin>"` diagonal to a pin.

### Valid part types

`wokwi-arduino-uno`, `wokwi-arduino-nano`, `wokwi-arduino-mega`, `wokwi-servo`,
`wokwi-dht22`, `wokwi-dht11`, `wokwi-ds3231`, `wokwi-lcd1602`, `wokwi-lcd2004`,
`wokwi-led`, `wokwi-rgb-led`, `wokwi-buzzer`, `wokwi-pushbutton`, `wokwi-pir`,
`wokwi-ultrasonic`, `wokwi-hc-sr04`, `wokwi-resistor`, `wokwi-potentiometer`,
`wokwi-relay-module`, `wokwi-ir-receiver`, `wokwi-photoresistor-sensor`,
`wokwi-soil-moisture-sensor`, `wokwi-ssd1306`, `wokwi-neopixel`,
`wokwi-membrane-keypad`, `wokwi-breadboard`, `wokwi-power-supply`,
`wokwi-7segment`, `wokwi-flame-sensor`, `wokwi-mpu6050`.

## Final answer template

Only use this shape once compile **and** simulation have passed:

```
✅ COMPILED: Yes — Sketch uses 3308 bytes (10%) of program storage
✅ SIMULATED: Yes — serial showed "System ready" then "Servo idle" x6; pin 9 pulsed 296 times
✅ SAFE TO BUY: Yes 98%
📦 BOM:
 - Arduino Uno R3 (clone) - N15,000 - Brain
 - SG90 Servo - N4,000 - Rotating door latch
 - PIR Motion Sensor - N3,500 - Detects approach
 - Jumper Wires - N1,500 - Wiring
 TOTAL: N24,000
🔌 WIRING:
 - PIR VCC -> Uno 5V, GND -> Uno GND, OUT -> Uno D2
 - Servo orange -> Uno D9, red -> external 5V, brown -> common GND
🔌 WIRING
💻 CODE:
```cpp
...full sketch...
```
📊 DIAGRAM:
```json
...diagram.json...
```
```

## Safety rules enforced by `safety_check`

- **5 V peripheral on a 3.3 V board** (ESP32/ESP8266 + ultrasonic/relay/servo/buzzer)
  → CRITICAL. Add a bidirectional level shifter or use a 3.3 V-rated module.
- **Pins 0/1 used** → WARN. They are hardware UART; move to pins 2/3.
- **`delay()` inside an ISR** → CRITICAL. Set a `volatile` flag, do the work in `loop()`.
- **Servo/relay/motor present** → WARN. Power from a separate 5 V rail, share ground.
- **LED with no resistor** → WARN. Add 220-330 Ω in series.

## Pricing

Prices are Naira estimates for Kaduna Computer Village. They drift with FX and stock —
tell the user to confirm at the market. Sensors priced: SG90 N4,000 · DHT22 N7,500 ·
DS3231 N5,000 · LCD1602 N6,000 · PIR N3,500 · HC-SR04 N3,000 · Relay N3,000 ·
SSD1306 N7,000. The Uno R3 clone is billed at N15,000.

## Sandbox note

The toolchain lives in `/opt/arduino-toolchain` in the sandbox (override with
`ARDUINO_TOOLCHAIN_DIR`). The AVR emulator runs on Node with the `avr8js` package;
if `require('avr8js')` fails, install it once inside the sandbox:
`cd /opt/arduino-toolchain && npm install avr8js@0.20.0`.