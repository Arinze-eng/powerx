#!/bin/sh
dir="$HOME/.nanobot"

# ---------------------------------------------------------------------------
# Cron persistence: the platform volume, NOT Supabase.
#
# The cron store (workspace/cron/jobs.json) is ordinary on-disk state. On a
# platform that mounts a persistent volume there is nothing to synchronise:
# writing the store to Supabase and reading it back at boot costs egress and
# adds a failure mode (a stale cloud copy can resurrect deleted jobs, and a
# silent sync failure looks exactly like "cron just stopped firing"). So on a
# persistent disk cron is left entirely on the volume and the Supabase cron
# path is not used at all.
#
# Northflank is the persistent-volume platform for this deployment, so it is
# treated as persistent by DEFAULT; set NANOBOT_PERSISTENT_DISK explicitly to
# override in either direction. Render's free tier has no disk, so it keeps the
# legacy Supabase flow.
# ---------------------------------------------------------------------------
# Persistent-disk detection is done by LOOKING, not by trusting an env var:
# Northflank does not reliably export `NORTHFLANK=true`, so a platform check can
# silently miss and drop cron onto the Supabase path even though a real volume is
# mounted. We compare the filesystem device id of the data dir against the
# container root: a different device means a volume is mounted at or above it.
# (Uses stat rather than awk/findmnt so it works in the slim busybox-ish image.)
_fs_device_of() {
    stat -c %d "$1" 2>/dev/null || stat -f %d "$1" 2>/dev/null || echo ""
}

_data_dir_is_mounted() {
    target="$1"
    root_dev=$(_fs_device_of /)
    target_dev=$(_fs_device_of "$target")
    [ -n "$root_dev" ] && [ -n "$target_dev" ] && [ "$root_dev" != "$target_dev" ]
}

PERSISTENT_DISK=false
mkdir -p "$dir" 2>/dev/null || true
if _data_dir_is_mounted "$dir"; then
    PERSISTENT_DISK=true
    echo "[entrypoint] persistent volume detected at $dir — cron jobs stay on disk"
fi
# An explicit operator setting always wins, in either direction.
[ "$NANOBOT_PERSISTENT_DISK" = "true" ] && PERSISTENT_DISK=true
[ "$NANOBOT_PERSISTENT_DISK" = "false" ] && PERSISTENT_DISK=false
if [ "$RENDER" = "true" ] || [ "$NORTHFLANK" = "true" ]; then
    # Keep the legacy flags working as the bootstrap switch too.
    PLATFORM_BOOTSTRAP=true
fi

if [ "$PLATFORM_BOOTSTRAP" = "true" ]; then
    echo "[entrypoint] platform deploy — starting as $(id) (persistent_disk=$PERSISTENT_DISK)"
    # Recover any missing runtime env vars from Supabase (system_settings). The
    # container only strictly needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY;
    # everything else is restored here so a wiped platform env self-heals. This
    # is a single small read at boot (~4 KB) and is part of the approved
    # auth+secrets-only egress budget. The script emits `export KEY=value`
    # lines which we source in THIS shell so the values reach the nanobot
    # process launched below.
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
    # Legacy restore flows (cron jobs + chat history from Supabase). These only
    # make sense when the local disk is ephemeral (Render free tier). With a
    # persistent volume the data is already on disk, so skip entirely unless
    # NANOBOT_SUPABASE_BACKUP=true forces them.
    if [ "$PERSISTENT_DISK" != "true" ] || [ "$NANOBOT_SUPABASE_BACKUP" = "true" ]; then
        # Restore scheduled cron jobs from Supabase so a redeploy does not wipe
        # the user's scheduled reminders / assignments. Runs before the app
        # starts.
        if [ -f /app/scripts/supabase_cron_sync.py ]; then
            NANOBOT_DATA_DIR="$dir" /app/.venv/bin/python3 \
                /app/scripts/supabase_cron_sync.py --restore || \
                echo "[entrypoint] warning: cron restore failed (continuing)"
        fi
        # Restore WebUI chat history (transcripts + session metadata incl.
        # per-user owner tags) from Supabase so a redeploy does not wipe each
        # user's chats. Runs before the app starts so the sidebar is fully
        # populated on boot.
        if [ -f /app/scripts/supabase_chat_sync.py ]; then
            NANOBOT_DATA_DIR="$dir" /app/.venv/bin/python3 \
                /app/scripts/supabase_chat_sync.py --restore || \
                echo "[entrypoint] warning: chat restore failed (continuing)"
        fi
    else
        echo "[entrypoint] persistent disk detected — skipping Supabase cron/chat restore (egress policy)"
    fi
    # [FIX 2026-09-05] Backfill per-user chat ownership. Chat isolation is now
    # fail-closed: an unowned chat is hidden from / denied to every user, so it
    # must never be left anonymously shared. This reconciles unowned legacy
    # chats against their core session metadata (which records the owner) and
    # stamps it. Truly-unowned chats stay hidden, safe, and reclaimable.
    # Local-only operation (no Supabase traffic); always runs.
    if [ -f /app/scripts/backfill_chat_owners.py ]; then
        NANOBOT_DATA_DIR="$dir" /app/.venv/bin/python3 \
            /app/scripts/backfill_chat_owners.py --apply || \
            echo "[entrypoint] warning: chat-owner backfill failed (continuing)"
    fi
fi

# Backup-sidecar policy: launch the cron/chat Supabase backup loops ONLY when
# the disk is ephemeral (Render-style). On a persistent volume they would burn
# ~28 MB/day of egress re-uploading chat transcripts every 60 s for zero gain.
LAUNCH_SIDECARS=false
if [ "$NANOBOT_SUPABASE_BACKUP" = "false" ]; then
    :   # explicit kill-switch wins
elif [ "$NANOBOT_SUPABASE_BACKUP" = "true" ]; then
    LAUNCH_SIDECARS=true
elif [ "$PERSISTENT_DISK" != "true" ]; then
    # No persistent disk (Render / local dev): fall back to the historical
    # behaviour and keep Supabase backups running.
    LAUNCH_SIDECARS=true
fi

# Drop privileges whenever the container starts as root. Platforms mount the
# persistent disk root-owned, and a plain `docker run` also defaults to root
# now, so this covers both. Chown the data dir so the non-root user can write
# it, then re-exec as nanobot. Fail closed: if the privilege drop cannot be
# performed, exit rather than run the agent as root.
if [ "$(id -u)" = "0" ]; then
    chown -R nanobot:nanobot "$dir" 2>/dev/null || echo "[entrypoint] warning: chown $dir failed"
    if [ "$LAUNCH_SIDECARS" = "true" ]; then
        # Start the cron-job backup sidecar so newly scheduled reminders /
        # assignments are pushed to Supabase continuously too.
        setpriv --reuid=nanobot --regid=nanobot --init-groups env \
            NANOBOT_DATA_DIR="$dir" /app/.venv/bin/python3 \
            /app/scripts/supabase_cron_sync.py --loop 60 \
            >/dev/null 2>&1 &
        # Start the WebUI chat-history backup sidecar so newly created / edited
        # chats are pushed to Supabase continuously too.
        setpriv --reuid=nanobot --regid=nanobot --init-groups env \
            NANOBOT_DATA_DIR="$dir" /app/.venv/bin/python3 \
            /app/scripts/supabase_chat_sync.py --loop 60 \
            >/dev/null 2>&1 &
    else
        echo "[entrypoint] Supabase backup sidecars disabled (egress policy)"
    fi
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

if [ "$LAUNCH_SIDECARS" = "true" ]; then
    # Start the cron-job backup sidecar (non-root / local dev path).
    if [ -f /app/scripts/supabase_cron_sync.py ]; then
        NANOBOT_DATA_DIR="$dir" /app/.venv/bin/python3 \
            /app/scripts/supabase_cron_sync.py --loop 60 >/dev/null 2>&1 &
    fi
    # Start the WebUI chat-history backup sidecar (non-root / local dev path)
    # so newly created / edited chats are pushed to Supabase continuously too.
    if [ -f /app/scripts/supabase_chat_sync.py ]; then
        NANOBOT_DATA_DIR="$dir" /app/.venv/bin/python3 \
            /app/scripts/supabase_chat_sync.py --loop 60 >/dev/null 2>&1 &
    fi
else
    echo "[entrypoint] Supabase backup sidecars disabled (egress policy)"
fi

exec /app/scripts/nanobot_launcher.sh "$@"
