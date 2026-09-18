"""Wiring-diagram renderer for the Arduino verification tool.

Turns a Wokwi/Velxio ``diagram.json`` (parts + connections) into a readable SVG
circuit diagram: labelled component blocks, colour-coded point-to-point wiring,
and a legend. Pure Python — no browser, no Chromium, no network, so it cannot
fail inside a disposable sandbox.

The goal is clarity for someone holding real jumper wires, not photorealism.
"""

from __future__ import annotations

import html
from typing import Any

# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #

_WIRE_COLORS = {
    "red": "#d92b2b",
    "black": "#222222",
    "green": "#1f9d3d",
    "orange": "#e8801a",
    "yellow": "#e0b400",
    "blue": "#1f6fd0",
    "purple": "#8a3fc0",
    "white": "#9aa0a6",
    "gray": "#6b7280",
    "grey": "#6b7280",
    "brown": "#7a4a1d",
}

#: Human labels + accent colour per part family.
_PART_STYLE = {
    "arduino-uno": ("Arduino Uno R3", "#0b7285", "#e3f6f9"),
    "arduino-nano": ("Arduino Nano", "#0b7285", "#e3f6f9"),
    "arduino-mega": ("Arduino Mega", "#0b7285", "#e3f6f9"),
    "servo": ("Servo (SG90)", "#b8860b", "#fff6dc"),
    "dht22": ("DHT22 Sensor", "#0a7d4b", "#e4f7ee"),
    "dht11": ("DHT11 Sensor", "#0a7d4b", "#e4f7ee"),
    "ds3231": ("DS3231 RTC", "#5b4bb7", "#eeebfd"),
    "lcd1602": ("LCD 16x2", "#166534", "#e7f6ec"),
    "lcd2004": ("LCD 20x4", "#166534", "#e7f6ec"),
    "ssd1306": ("OLED SSD1306", "#166534", "#e7f6ec"),
    "led": ("LED", "#c92a2a", "#ffe8e8"),
    "rgb-led": ("RGB LED", "#c92a2a", "#ffe8e8"),
    "buzzer": ("Piezo Buzzer", "#a1446b", "#fdeaf2"),
    "pushbutton": ("Pushbutton", "#1f6fd0", "#e8f1ff"),
    "pir": ("PIR Motion", "#b45309", "#fdf1e3"),
    "ultrasonic": ("HC-SR04 Ultrasonic", "#0e7490", "#e4f5f9"),
    "hc-sr04": ("HC-SR04 Ultrasonic", "#0e7490", "#e4f5f9"),
    "resistor": ("Resistor", "#4b5563", "#f1f3f5"),
    "potentiometer": ("Potentiometer", "#4b5563", "#f1f3f5"),
    "relay": ("Relay Module", "#7c2d12", "#fdece3"),
    "ir-receiver": ("IR Receiver", "#6d28d9", "#f0ebfe"),
    "photoresistor": ("Photoresistor", "#4b5563", "#f1f3f5"),
    "soil-moisture-sensor": ("Soil Moisture", "#166534", "#e7f6ec"),
    "flame-sensor": ("Flame Sensor", "#b91c1c", "#fee9e9"),
    "neopixel": ("NeoPixel Strip", "#7e22ce", "#f5ecfe"),
    "membrane-keypad": ("Membrane Keypad", "#334155", "#eef2f7"),
    "mpu6050": ("MPU6050 IMU", "#4338ca", "#ecebfd"),
    "breadboard": ("Breadboard", "#8a6d3b", "#f6f0e2"),
    "power-supply": ("5V Power Supply", "#b91c1c", "#ffe9e9"),
    "7segment": ("7-Segment Display", "#c92a2a", "#ffe8e8"),
}

_FALLBACK_STYLE = ("Module", "#374151", "#f3f4f6")

#: Preferred connection order so power rails read first, then signals.
_KIND_ORDER = {"power": 0, "ground": 1, "signal": 2}

_GROUND_TOKENS = ("gnd", "ground", "-", "v-")
_POWER_TOKENS = ("5v", "5v0", "vcc", "v+", "3v3", "3.3v", "vin", "vdd")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _family(part_type: str) -> str:
    """Map a Wokwi part type to a presentation family key."""
    key = part_type.replace("wokwi-", "").lower()
    if key in _PART_STYLE:
        return key
    for candidate in _PART_STYLE:
        if candidate in key:
            return candidate
    return ""


def _style_for(part_type: str) -> tuple[str, str, str]:
    fam = _family(part_type)
    return _PART_STYLE.get(fam, _FALLBACK_STYLE)


def _kind(pin_a: str, pin_b: str) -> str:
    joined = f"{pin_a} {pin_b}".lower()
    if any(tok in joined for tok in _GROUND_TOKENS):
        return "ground"
    if any(tok in joined for tok in _POWER_TOKENS):
        return "power"
    return "signal"


def _pin_label(ref: str) -> str:
    """``uno1:GND`` -> ``GND``."""
    return ref.split(":", 1)[1] if ":" in ref else ref


def _part_id(ref: str) -> str:
    return ref.split(":", 1)[0]


def esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


# --------------------------------------------------------------------------- #
# Renderer
# --------------------------------------------------------------------------- #


def render_svg(
    diagram: dict[str, Any] | None,
    title: str = "Circuit Diagram",
    board: str = "Arduino Uno R3",
) -> str:
    """Render a diagram.json into a standalone SVG string.

    Components are laid out in a grid; every connection becomes a routed,
    colour-coded wire with pin labels at both ends. Works with ``None`` (yields a
    placeholder card) so it can always be attached to a report.
    """
    parts = list((diagram or {}).get("parts") or [])
    connections = list((diagram or {}).get("connections") or [])

    # Index parts, synthesising an entry for any referenced-but-undeclared part.
    index: dict[str, dict[str, Any]] = {}
    for part in parts:
        pid = str(part.get("id") or "")
        if pid:
            index[pid] = part
    for conn in connections:
        for ref in (conn[0], conn[1]) if len(conn) >= 2 else ():
            pid = _part_id(str(ref))
            if pid and pid not in index:
                index[pid] = {
                    "id": pid,
                    "type": "wokwi-arduino-uno" if pid.startswith("uno") else "unknown",
                }

    # ---- Layout ---------------------------------------------------------- #
    box_w, box_h = 230, 104
    gap_x, gap_y = 74, 76
    margin_x, margin_top = 56, 150
    cols = max(1, min(3, len(index)))

    positions: dict[str, tuple[int, int]] = {}
    for i, pid in enumerate(index):
        row, col = divmod(i, cols)
        positions[pid] = (margin_x + col * (box_w + gap_x), margin_top + row * (box_h + gap_y))

    rows = (len(index) + cols - 1) // cols or 1
    content_w = cols * box_w + (cols - 1) * gap_x
    width = max(860, content_w + margin_x * 2)

    # Route wires through a channel below the last component row.
    wire_zone_top = margin_top + rows * (box_h + gap_y) - gap_y + 46
    wire_zone_h = max(120, len(connections) * 26 + 70)
    height = wire_zone_top + wire_zone_h + 210

    order = {"power": 0, "ground": 1, "signal": 2}
    kind_of: dict[int, str] = {}
    for i, conn in enumerate(connections):
        if len(conn) >= 2:
            kind_of[i] = _kind(str(conn[0]), str(conn[1]))
    seq = sorted(range(len(connections)), key=lambda i: (order.get(kind_of.get(i, "signal"), 3), i))

    svg: list[str] = []
    svg.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Segoe UI, Roboto, Helvetica, Arial, sans-serif">'
    )
    svg.append(f'<rect width="{width}" height="{height}" fill="#fbfcfd"/>')
    svg.append(
        f'<text x="{margin_x}" y="56" font-size="27" font-weight="700" fill="#111827">{esc(title)}</text>'
    )
    svg.append(
        f'<text x="{margin_x}" y="84" font-size="14" fill="#6b7280">Board: {esc(board)} '
        f'&#160;|&#160; {len(index)} components &#160;|&#160; {len(connections)} connections</text>'
    )

    # ---- Components ------------------------------------------------------ #
    for pid, part in index.items():
        x, y = positions[pid]
        ptype = str(part.get("type") or "unknown")
        label, accent, fill = _style_for(ptype)
        name = str(part.get("name") or "") or label

        svg.append(
            f'<g><rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" rx="12" '
            f'fill="{fill}" stroke="{accent}" stroke-width="2.2"/>'
        )
        svg.append(f'<rect x="{x}" y="{y}" width="{box_w}" height="30" rx="12" fill="{accent}"/>')
        svg.append(f'<rect x="{x}" y="{y + 18}" width="{box_w}" height="12" fill="{accent}"/>')
        svg.append(
            f'<text x="{x + 14}" y="{y + 21}" font-size="14.5" font-weight="700" '
            f'fill="#ffffff">{esc(name)}</text>'
        )
        svg.append(
            f'<text x="{x + 14}" y="{y + 54}" font-size="12.5" fill="#374151">{esc(ptype)}</text>'
        )
        svg.append(
            f'<text x="{x + 14}" y="{y + 74}" font-size="12" fill="#6b7280">id: {esc(pid)}</text>'
        )
        # Pin chips for this part's connections.
        pins = []
        for conn in connections:
            if len(conn) < 2:
                continue
            if _part_id(str(conn[0])) == pid:
                pins.append(_pin_label(str(conn[0])))
            elif _part_id(str(conn[1])) == pid:
                pins.append(_pin_label(str(conn[1])))
        unique_pins = list(dict.fromkeys(pins))
        for j, pin in enumerate(unique_pins[:4]):
            cx = x + 16 + (j % 2) * 108
            cy = y + 88
            svg.append(
                f'<rect x="{cx}" y="{cy - 11}" width="96" height="18" rx="6" '
                f'fill="#ffffff" stroke="{accent}" stroke-width="1.2"/>'
            )
            svg.append(
                f'<text x="{cx + 48}" y="{cy + 3}" font-size="11.5" text-anchor="middle" '
                f'fill="#1f2937" font-family="ui-monospace, Menlo, monospace">{esc(pin)}</text>'
            )
        svg.append("</g>")

    # ---- Wires ----------------------------------------------------------- #
    lane_h = 26
    for lane, ci in enumerate(seq):
        conn = connections[ci]
        if len(conn) < 2:
            continue
        ref_a, ref_b = str(conn[0]), str(conn[1])
        color_key = str(conn[2]).lower() if len(conn) > 2 else "black"
        color = _WIRE_COLORS.get(color_key, "#6b7280")

        pa, pb = _part_id(ref_a), _part_id(ref_b)
        if pa not in positions or pb not in positions:
            continue

        ax, ay = positions[pa]
        bx, by = positions[pb]
        lane_y = wire_zone_top + 34 + lane * lane_h

        start_x = ax + box_w / 2 + (0 if pa == pb else (30 if positions[pa][1] == positions[pb][1] else 0))
        start_y = ay + box_h
        end_x = bx + box_w / 2
        end_y = by + box_h

        # From A, down into the lane; across; up into B.
        path = (
            f"M {start_x:.0f} {start_y:.0f} "
            f"L {start_x:.0f} {lane_y:.0f} "
            f"L {end_x:.0f} {lane_y:.0f} "
            f"L {end_x:.0f} {end_y:.0f}"
        )
        if pa == pb:
            path = (
                f"M {start_x:.0f} {start_y:.0f} L {start_x + 46:.0f} {lane_y:.0f} "
                f"L {end_x + 46:.0f} {lane_y:.0f} L {end_x:.0f} {end_y:.0f}"
            )

        svg.append(
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.6" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        # Junction dots.
        for jx, jy in ((start_x, start_y), (end_x, end_y)):
            svg.append(f'<circle cx="{jx:.0f}" cy="{jy:.0f}" r="4" fill="{color}"/>')

        mid = min(start_x, end_x) + abs(end_x - start_x) / 2
        badge = f"{_pin_label(ref_a)} → {_pin_label(ref_b)}"
        text_w = max(96, len(badge) * 6.6 + 18)
        bx0 = mid - text_w / 2
        svg.append(
            f'<g><rect x="{bx0:.0f}" y="{lane_y - 12:.0f}" width="{text_w:.0f}" height="22" rx="7" '
            f'fill="#ffffff" stroke="{color}" stroke-width="1.3"/>'
        )
        svg.append(
            f'<text x="{mid:.0f}" y="{lane_y + 3:.0f}" font-size="11.5" text-anchor="middle" '
            f'fill="#111827">{esc(badge)}</text></g>'
        )

    # ---- Legend ---------------------------------------------------------- #
    legend_y = height - 178
    svg.append(
        f'<rect x="{margin_x}" y="{legend_y}" width="{width - margin_x * 2}" height="150" rx="12" '
        f'fill="#ffffff" stroke="#e5e7eb" stroke-width="1.5"/>'
    )
    svg.append(
        f'<text x="{margin_x + 20}" y="{legend_y + 30}" font-size="14.5" font-weight="700" '
        f'fill="#111827">Wire legend</text>'
    )
    legend = [
        ("#d92b2b", "Power (5V / VCC)"),
        ("#222222", "Ground (GND)"),
        ("#1f9d3d", "Digital signal"),
        ("#e8801a", "PWM signal"),
        ("#1f6fd0", "Input / button"),
        ("#8a3fc0", "Data / bus"),
    ]
    for i, (col, text) in enumerate(legend):
        lx = margin_x + 24 + (i % 3) * 250
        ly = legend_y + 62 + (i // 3) * 30
        svg.append(f'<line x1="{lx}" y1="{ly}" x2="{lx + 34}" y2="{ly}" stroke="{col}" stroke-width="3.4"/>')
        svg.append(f'<text x="{lx + 44}" y="{ly + 4}" font-size="12.5" fill="#374151">{esc(text)}</text>')
    svg.append(
        f'<text x="{margin_x + 24}" y="{legend_y + 132}" font-size="12" fill="#6b7280">'
        f'Route every wire on the real build, then verify continuity before applying power. '
        f'Generated by powerx arduino_verify.</text>'
    )

    svg.append("</svg>")
    return "\n".join(svg)


def render_png_payload(svg: str) -> dict[str, str]:
    """Describe the SVG for embedding in JSON tool output."""
    return {"format": "svg", "svg": svg}


def diagram_summary(diagram: dict[str, Any] | None) -> dict[str, Any]:
    """Compact structural summary, useful for logs and safety checks."""
    parts, connections = [], []
    if diagram:
        parts = list(diagram.get("parts") or [])
        connections = list(diagram.get("connections") or [])
    kinds = {"power": 0, "ground": 0, "signal": 0}
    for conn in connections:
        if len(conn) >= 2:
            kinds[_kind(str(conn[0]), str(conn[1]))] += 1
    return {
        "part_count": len(parts),
        "connection_count": len(connections),
        "connection_kinds": kinds,
        "parts": [str(p.get("type") or "") for p in parts],
    }
