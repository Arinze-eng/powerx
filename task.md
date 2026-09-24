# Task — Live GUI screen panel (tier 2: frames over the existing WebSocket)

User decisions (2026-09-24):
- **Tier 2** — frames pushed over the existing WebSocket. View-only (no input).
- **Capture service host-side** — "decide for me, most robust across Novita/Runloop/VPS"
  → decided: host-side daemon in the gateway process. Reasoning below.
- **Generic** — any GUI app in the sandbox, NOT MT5-only.

## Why host-side, not in-sandbox

The gateway process is the SAME process as the agent loop
(`nanobot/cli/gateway_runtime.py` builds `AgentLoop` at :431 and `ChannelManager` at :725),
so it already holds the sandbox handles (`_STORE`, `_RUNLOOP_STORE`, ...).

A detached in-sandbox loop (the `guard` pattern) would have to survive Novita's
TTL'd sandbox and would need its own re-attach logic per backend. Host-side is one
implementation for every backend, and it dies with the process instead of leaking
a watcher into a sandbox that is about to be recycled.

## Measured facts this design rests on (verified on the live box, 2026-09-24)

| Fact | Value | Consequence |
|---|---|---|
| 1080p PNG of the live MT5 terminal | **85 KB** | wire format = PNG |
| Same frame as JPEG q60 | 272 KB | **do not use JPEG for UI content** |
| 720p PNG (downscaled) | 293 KB | **never downscale** — interpolation noise defeats deflate |
| ffmpeg x11grab cost | 181 ms/frame | spawn-per-frame caps at ~5 fps; acceptable at 1–2 fps |
| Idle desktop pixel change over 4 s | 1.26 % | frame dedup is the big win |
| Static screen → byte-identical capture | **confirmed** (ffmpeg AND ImageMagick) | byte-compare is a valid change detector |
| `xdpyinfo`, `x11vnc`, `xdotool` | MISSING in sandbox | must not be required; `ffmpeg` + `import` present |
| `matchbox-window-manager` | present | WM available if tier 3 ever needs it |

## Files

1. `nanobot/agent/tools/workspace_bridge.py`
   - add public `fetch_remote_file(remote_path, *, max_bytes) -> bytes | None`
   - reuses the two existing fetch paths (`download()` for vps/upstash/daytona/runloop/vercel,
     chunked base64 `files.read` for native Novita). One place knows how to move bytes.

2. `nanobot/webui/screen_stream.py`  **NEW**
   - `ScreenGeometry`, `ScreenFrame`
   - `ScreenSource` protocol; `SandboxScreenSource` (remote), `LocalScreenSource` (tests/host)
   - `ScreenStream` — pump loop, dedup by byte compare, subscriber fan-out
   - `ScreenStreamManager` — per-session streams, refcounted start/stop

3. `nanobot/channels/websocket/runtime.py`
   - inbound envelope `screen_subscribe` / `screen_unsubscribe`
   - push `screen_frame` events (JSON text, base64 payload — client already parses
     JSON envelopes; no binary-protocol work, and base64 costs 33 % on an 85 KB frame)
   - stop streams on connection cleanup

4. WebUI
   - `webui/src/lib/nanobot-client.ts` — `screen_frame` event → subscribers
   - new live-view component + sidebar entry ("a separate section they can tap to view")

## Non-goals (explicitly, this round)

- No input / interaction (that is tier 3 — needs a duplex byte channel on the gateway)
- No public artifact host (`file_share.py` → catbox/onlyfiles) — would publish a live
  broker terminal to a public URL. Disqualified on privacy grounds.
- No tile-diff yet (measured opportunity, phase 2)
- Not an `mt5_sandbox` action — the panel is generic

## Status

- [ ] workspace_bridge: fetch_remote_file
- [ ] screen_stream.py
- [ ] runtime wiring
- [ ] webui client + panel + sidebar
- [ ] tests
- [ ] verify live, commit, push
