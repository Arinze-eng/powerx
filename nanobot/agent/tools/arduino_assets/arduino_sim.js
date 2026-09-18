#!/usr/bin/env node
/**
 * Velxio headless AVR simulator core.
 * Runs an Intel-HEX firmware on an ATmega328P via avr8js, captures USART0
 * serial output, and reports GPIO state + activity for each digital pin.
 *
 * Usage: node arduino_sim.js <firmware.hex> [--ms 2000] [--expect "text"] [--json out.json]
 */
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
      for (let i = 0; i < len; i++) {
        const b = parseInt(line.substr(9 + i * 2, 2), 16);
        bytes[addr + i] = b;
      }
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
  for (let i = 0; i < maxAddr; i += 2) {
    prog[i >> 1] = (bytes[i] | (bytes[i + 1] << 8)) & 0xffff;
  }
  const cpu = new CPU(prog, 0x800);
  const portB = new AVRIOPort(cpu, portBConfig);
  const portC = new AVRIOPort(cpu, portCConfig);
  const portD = new AVRIOPort(cpu, portDConfig);
  const timer0 = new AVRTimer(cpu, timer0Config);
  const timer1 = new AVRTimer(cpu, timer1Config);
  const timer2 = new AVRTimer(cpu, timer2Config);

  const serial = [];
  const usart = new AVRUSART(cpu, usart0Config, 16e6);
  usart.onByteTransmit = (b) => serial.push(String.fromCharCode(b));

  // Pin activity tracking (Arduino pin numbers -> ports/direction).
  const pinMap = {
    // Uno digital pins; (port, bit)
    0: [portD, 0], 1: [portD, 1], 2: [portD, 2], 3: [portD, 3], 4: [portD, 4],
    5: [portD, 5], 6: [portD, 6], 7: [portD, 7], 8: [portB, 0], 9: [portB, 1],
    10: [portB, 2], 11: [portB, 3], 12: [portB, 4], 13: [portB, 5],
    14: [portC, 0], 15: [portC, 1], 16: [portC, 2], 17: [portC, 3],
    18: [portC, 4], 19: [portC, 5],
  };
  const activity = {};
  const watched = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13];
  for (const p of watched) {
    const [port, bit] = pinMap[p];
    activity[p] = { toggles: 0, highMs: 0, last: null };
    port.addListener(() => {
      const state = port.pinState(bit);
      const high = state === PinState.High || state === PinState.HighPort;
      if (activity[p].last !== null && activity[p].last !== high) activity[p].toggles++;
      activity[p].last = high;
    });
  }

  const cyclesPerMs = 16000; // 16 MHz
  const totalCycles = Math.min(cyclesPerMs * ms, 16e6 * 30);
  const started = Date.now();
  for (let i = 1; i < totalCycles; i += 1) {
    avrInstruction(cpu);
    cpu.tick();
    if (cpu.cycles >= totalCycles) break;
  }
  const elapsed = Date.now() - started;

  const text = serial.join('');
  const pass = expect ? text.includes(expect) : text.trim().length > 0;
  return {
    ok: true,
    firmware: hexPath,
    firmware_bytes: maxAddr,
    cycles_run: cpu.cycles,
    sim_ms: ms,
    wall_ms: elapsed,
    serial: text,
    serial_lines: text.split(/\r?\n/).filter((l) => l.length),
    expect: expect || null,
    expect_found: expect ? text.includes(expect) : null,
    pin_activity: activity,
    active_pins: Object.entries(activity).filter(([, a]) => a.toggles > 0 || a.last !== null).map(([p, a]) => ({ pin: Number(p), toggles: a.toggles })),
  };
}

const args = process.argv.slice(2);
const hexPath = args[0];
if (!hexPath) { console.error('usage: arduino_sim.js <hex> [--ms N] [--expect TEXT]'); process.exit(2); }
let ms = 2000, expect = null, out = null;
for (let i = 1; i < args.length; i++) {
  if (args[i] === '--ms') ms = parseInt(args[++i], 10);
  else if (args[i] === '--expect') expect = args[++i];
  else if (args[i] === '--json') out = args[++i];
}
const result = run(hexPath, ms, expect);
const payload = JSON.stringify(result, null, 2);
if (out) fs.writeFileSync(out, payload);
console.log(payload);