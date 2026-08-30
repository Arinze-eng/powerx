#!/bin/sh
# Launch the reverse proxy (public port) in the background, then run the
# nanobot gateway as the foreground process (PID 1 replacement).
#
# The proxy owns Render's single public port ($PORT) and dispatches:
#   /telegram -> PTB Telegram webhook server (127.0.0.1:8081)
#   everything else -> embedded WebUI / WebSocket channel (127.0.0.1:8766)
#
# On Render it is required to make the Telegram webhook publicly reachable.
# Locally (no RENDER), it can be enabled by setting NANOBOT_START_PROXY=1, but
# it defaults to off so plain `nanobot gateway` runs are unaffected.

start_proxy() {
    echo "[launcher] starting reverse proxy on port ${PORT:-8765}..."
    # Keep the proxy alive with a bounded respawn loop so a transient crash in
    # the public listener does not silently drop the Telegram webhook.
    retries=0
    while [ "$retries" -lt 5 ]; do
        python /app/scripts/render_reverse_proxy.py
        status=$?
        retries=$((retries + 1))
        echo "[launcher] reverse proxy exited (status=$status); restart $retries/5"
        sleep 2
    done
    echo "[launcher] reverse proxy gave up after 5 attempts; leaving gateway running"
}

if [ "$RENDER" = "true" ] || [ "$NANOBOT_START_PROXY" = "1" ]; then
    start_proxy &
fi

exec nanobot "$@"