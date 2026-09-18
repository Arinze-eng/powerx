#!/usr/bin/env node
/**
 * powerx headless AVR simulator.
 *
 * Runs an Intel-HEX firmware on an ATmega328P via avr8js, captures USART0
 * serial output, tracks GPIO activity, and applies a scripted stimulus file so
 * that inputs are *driven* rather than assumed.
 *
 * Input pull-up modelling
 * -----------------------
 * avr8js leaves GPIO input values at 0 by default, which makes an
 * INPUT_PULLUP button read LOW (pressed) even when nobody touches it. Real
 * hardware reads HIGH. Every digital pin therefore defaults to HIGH (pulled up)
 * and a scenario pulls a pin LOW to simulate a press.
 *
 * Usage:
 *   node arduino_sim.js <firmware.hex> [--ms 3000] [--expect "TEXT"]
 *        [--scenario scenario.json] [--json out.json]
 *
 * Scenario file:
 *   {
 *     "inputs":    [ { "pin": 4, "at_ms": 5000, "state": "low", "hold_ms": 300 } ],
 *     "serial_in": [ { "at_ms": 3000, "bytes": [1] } ]
 *   }
 */
const fs = require('fs');
const {
  CPU, avrInstruction, AVRIOPort, portBConfig, portCConfig, portDConfig,
  AVRTimer, timer0Config, timer1Config, timer2Config,
  AVRUSART, usart0Config, PinState,
} = require('avr8js');

const CLOCK_HZ = 16e6;
const CYCLES_PER_MS = CLOCK_HZ / 1000;

/* Pin number -> [port, bit] on the ATmega328P (Arduino Uno mapping). */
const PIN_MAP = {
  0: ['D', 0], 1: ['D', 1], 2: ['D', 2], 3: ['D', 3], 4: ['D', 4],
  5: ['D', 5], 6: ['D', 6], 7: ['D', 7], 8: ['B', 0], 9: ['B', 1],
  10: ['B', 2], 11: ['B', 3], 12: ['B', 4], 13: ['B', 5],
  14: ['C', 0], 15: ['C', 1], 16: ['C', 2], 17: ['C', 3],
  18: ['C', 4], 19: ['C', 5],
};

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
      for (let i = 0; i < len; i++) {
        bytes[addr + i] = parseInt(line.substr(9 + i * 2, 2), 16);
      }
      maxAddr = Math.max(maxAddr, addr + len);
    }
  }
  return { bytes, maxAddr };
}

function run(hexPath, ms, expect, scenario) {
  const hex = fs.readFileSync(hexPath, 'utf8');
  const { bytes, maxAddr } = parseIntelHex(hex);
  if (maxAddr === 0) throw new Error('Empty or unparsable HEX file');

  const prog = new Uint16Array(0x8000);
  for (let i = 0; i < maxAddr; i += 2) {
    prog[i >> 1] = (bytes[i] | (bytes[i + 1] << 8)) & 0xffff;
  }

  const cpu = new CPU(prog, 0x800);
  const ports = {
    B: new AVRIOPort(cpu, portBConfig),
    C: new AVRIOPort(cpu, portCConfig),
    D: new AVRIOPort(cpu, portDConfig),
  };
  new AVRTimer(cpu, timer0Config);
  new AVRTimer(cpu, timer1Config);
  new AVRTimer(cpu, timer2Config);

  // --- Serial capture (firmware -> host) ---------------------------------- //
  const serial = [];
  const usart = new AVRUSART(cpu, usart0Config, CLOCK_HZ);
  usart.onByteTransmit = (b) => serial.push(String.fromCharCode(b));

  // --- Input pull-up modelling: every pin idles HIGH ---------------------- //
  const pinOwner = {};   // pin -> 'B' | 'C' | 'D'
  const inputPinState = {};
  for (const [pinStr, [portName, bit]] of Object.entries(PIN_MAP)) {
    const pin = Number(pinStr);
    pinOwner[pin] = portName;
    inputPinState[pin] = true;
    ports[portName].setPin(bit, 1);
  }
  const setInput = (pin, high) => {
    const owner = pinOwner[pin];
    if (!owner) return;
    const bit = PIN_MAP[pin][1];
    ports[owner].setPin(bit, high ? 1 : 0);
    inputPinState[pin] = high;
  };

  // --- GPIO activity tracking -------------------------------------------- //
  const activity = {};
  const watched = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13];
  for (const pin of watched) {
    const [portName, bit] = PIN_MAP[pin];
    activity[pin] = { toggles: 0, last: null };
    ports[portName].addListener(() => {
      const st = ports[portName].pinState(bit);
      const high = st === PinState.High || st === PinState.HighPort;
      if (activity[pin].last !== null && activity[pin].last !== high) {
        activity[pin].toggles++;
      }
      activity[pin].last = high;
    });
  }

  // --- Scheduled stimulus ------------------------------------------------- //
  const events = [];
  const scenarioData = scenario || { inputs: [], serial_in: [] };
  for (const ev of scenarioData.inputs || []) {
    const at = Math.max(0, Number(ev.at_ms) || 0);
    const hold = Number(ev.hold_ms) || 0;
    events.push({ at, kind: 'input', pin: Number(ev.pin), high: ev.state !== 'low' });
    if (hold > 0) {
      events.push({ at: at + hold, kind: 'input', pin: Number(ev.pin), high: true });
    }
  }
  for (const ev of scenarioData.serial_in || []) {
    events.push({
      at: Math.max(0, Number(ev.at_ms) || 0),
      kind: 'serial',
      bytes: (ev.bytes || []).map(Number),
    });
  }

  // SoftwareSerial delivers data by bit-banging a GPIO pin, so a software-serial
  // frame must be clocked onto the pin at the right baud rate — writing to the
  // hardware USART would never be seen. Each bit is held for one bit period.
  for (const ev of scenarioData.soft_serial || []) {
    const pin = Number(ev.pin);
    const baud = Number(ev.baud) || 9600;
    const cyclesPerBit = CLOCK_HZ / baud;
    const startMs = Math.max(0, Number(ev.at_ms) || 0);
    const startCycle = startMs * CYCLES_PER_MS;
    const owner = pinOwner[pin];
    if (!owner) continue;
    const bit = PIN_MAP[pin][1];

    // Queue raw pin transitions expressed in absolute cycles.
    const emit = (offsetBits, high) => {
      events.push({
        at: (startCycle + offsetBits * cyclesPerBit) / CYCLES_PER_MS,
        kind: 'pin_edge',
        portName: owner,
        bit,
        high,
      });
    };

    let frameIndex = 0;
    for (const byte of ev.bytes) {
      const b = Number(byte) & 0xff;
      emit(frameIndex + 0, false);            // start bit
      for (let i = 0; i < 8; i++) {
        emit(frameIndex + 1 + i, !!(b & (1 << i)));  // LSB first
      }
      emit(frameIndex + 9, true);             // stop bit
      emit(frameIndex + 10, true);            // idle
      frameIndex += 10;
    }
  }

  events.sort((a, b) => a.at - b.at);

  const applied = [];
  let cursor = 0;

  // --- Execute ------------------------------------------------------------ //
  const totalCycles = Math.min(CYCLES_PER_MS * ms, CLOCK_HZ * 30);
  const started = Date.now();
  for (let i = 1; i < totalCycles; i += 1) {
    const nowMs = cpu.cycles / CYCLES_PER_MS;
    while (cursor < events.length && events[cursor].at <= nowMs) {
      const ev = events[cursor++];
      if (ev.kind === 'input') {
        setInput(ev.pin, ev.high);
        applied.push({ at_ms: Math.round(nowMs), input: ev.pin, state: ev.high ? 'high' : 'low' });
      } else if (ev.kind === 'pin_edge') {
        // Drive a bit-banged serial edge (SoftwareSerial) at exact cycle timing.
        ports[ev.portName].setPin(ev.bit, ev.high ? 1 : 0);
        applied.push({
          at_ms: Math.round(nowMs * 1000) / 1000,
          soft_serial_edge: `${ev.portName}${ev.bit}`,
          state: ev.high ? 'high' : 'low',
        });
      } else {
        for (const b of ev.bytes) usart.writeByte(b, true);
        applied.push({ at_ms: Math.round(nowMs), serial_in: ev.bytes });
      }
    }
    avrInstruction(cpu);
    cpu.tick();
    if (cpu.cycles >= totalCycles) break;
  }

  const text = serial.join('');
  const activePins = Object.entries(activity)
    .map(([pin, a]) => ({ pin: Number(pin), toggles: a.toggles, final: a.last }))
    .filter((p) => p.toggles > 0);

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
    stimulus_applied: applied,
    input_pins_final: inputPinState,
    active_pins: activePins,
  };
}

const args = process.argv.slice(2);
const hexPath = args[0];
if (!hexPath) {
  console.error('usage: arduino_sim.js <hex> [--ms N] [--expect TEXT] [--scenario FILE] [--json OUT]');
  process.exit(2);
}
let ms = 2500;
let expect = null;
let scenarioPath = null;
let outPath = null;
for (let i = 1; i < args.length; i++) {
  if (args[i] === '--ms') ms = parseInt(args[++i], 10);
  else if (args[i] === '--expect') expect = args[++i];
  else if (args[i] === '--scenario') scenarioPath = args[++i];
  else if (args[i] === '--json') outPath = args[++i];
}

let scenario = null;
if (scenarioPath) {
  scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'));
}

const result = run(hexPath, ms, expect, scenario);
const payload = JSON.stringify(result, null, 2);
if (outPath) fs.writeFileSync(outPath, payload);
console.log(payload);