#!/bin/sh
# Pure polling runtime.
#
# Telegram runs in polling mode, so there is no webhook and no public reverse
# proxy needed. The nanobot gateway runs directly as the foreground process
# (PID 1 replacement). The embedded WebUI/WebSocket channel listens on the
# public port (0.0.0.0:8765) exposed via Render's PORT.
#
# Keeping this as a thin wrapper (instead of calling `nanobot` directly) makes
# it trivial to re-enable a proxy later if a webhook is ever wanted again.

exec nanobot "$@"