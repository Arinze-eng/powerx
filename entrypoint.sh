#!/bin/sh
dir="$HOME/.nanobot"

# Render deploy path (see render.yaml + render-config.json). Gated on Render's
# automatic RENDER=true env var so local Docker/podman usage is unaffected.
# Initializes the on-disk config from the committed template (wiring secrets via
# ${VAR} env vars, keeping runtime data on the persistent disk) and appends the
# --config flag. Logs each decision so a failed start is diagnosable in Render's
# logs. Privilege dropping is handled below, for every root start (not just here).
if [ "$RENDER" = "true" ]; then
    echo "[entrypoint] Render deploy — starting as $(id)"
    # Recover any missing runtime env vars from Supabase (system_settings). The
    # container only strictly needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY;
    # everything else is restored here so a wiped Render env self-heals. The
    # script emits `export KEY=value` lines which we source in THIS shell so the
    # values reach the nanobot process launched below.
    if [ -f /app/scripts/supabase_env_sync.py ]; then
        sync_file="$dir/supabase-env-$$.sh"
        ( python3 /app/scripts/supabase_env_sync.py --emit-shell > "$sync_file" 2>"$sync_file.err" ) || \
            echo "[entrypoint] warning: supabase env sync failed, continuing with current env"
        if [ -s "$sync_file" ]; then
            # shellcheck disable=SC1090
            . "$sync_file" && echo "[entrypoint] restored runtime env vars from Supabase"
        else
            echo "[entrypoint] no additional env vars needed from Supabase"
        fi
        rm -f "$sync_file" "$sync_file.err"
    fi
    mkdir -p "$dir" || echo "[entrypoint] warning: mkdir $dir failed"
    config="$dir/config.json"
    # Initialize config only when it does not already exist, so WebUI/provider
    # settings edited at runtime survive restarts. The disk persists config.json
    # across deploys; overwriting it every boot would discard those changes.
    if [ ! -f "$config" ]; then
        echo "[entrypoint] initializing $config from render-config.json"
        cp /app/render-config.json "$config" || echo "[entrypoint] warning: cp config failed"
    else
        echo "[entrypoint] existing $config found — leaving it in place"
    fi
    python3 /app/scripts/ensure_render_config.py "$config" || \
        echo "[entrypoint] warning: config migration failed"
    set -- "$@" --config "$config"
fi

# Drop privileges whenever the container starts as root. Render mounts the
# persistent disk root-owned, and a plain `docker run` also defaults to root now,
# so this covers both. Chown the data dir so the non-root user can write it, then
# re-exec as nanobot. Fail closed: if the privilege drop cannot be performed,
# exit rather than run the agent as root.
if [ "$(id -u)" = "0" ]; then
    chown -R nanobot:nanobot "$dir" 2>/dev/null || echo "[entrypoint] warning: chown $dir failed"
    if setpriv --reuid=nanobot --regid=nanobot --init-groups true 2>/dev/null; then
        echo "[entrypoint] dropping privileges to nanobot via setpriv"
        exec setpriv --reuid=nanobot --regid=nanobot --init-groups /app/scripts/nanobot_launcher.sh "$@"
    fi
    echo "[entrypoint] error: started as root but setpriv privilege drop failed — refusing to run as root" >&2
    exit 1
fi

# Already non-root: make sure the data dir is writable before starting.
if [ -d "$dir" ] && [ ! -w "$dir" ]; then
    owner_uid=$(stat -c %u "$dir" 2>/dev/null || stat -f %u "$dir" 2>/dev/null)
    cat >&2 <<EOF
Error: $dir is not writable (owned by UID $owner_uid, running as UID $(id -u)).

Fix (pick one):
  Host:   sudo chown -R 1000:1000 ~/.nanobot
  Docker: docker run --user \$(id -u):\$(id -g) ...
  Podman: podman run --userns=keep-id ...
EOF
    exit 1
fi

exec /app/scripts/nanobot_launcher.sh "$@"
