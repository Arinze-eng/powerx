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

# ---------------------------------------------------------------------------
# Resolve + announce the real cron store, and verify it is on the volume.
#
# This exists because `NANOBOT_PERSISTENT_DISK=true` was set while the cron
# store was still under $HOME/.nanobot/workspace — i.e. the deployment claimed
# to be durable and then skipped the Supabase restore, but the file it was
# trusting was container-local and deleted on every deploy. Silently believing
# an env var is how "cron stopped firing" survived a whole release cycle.
# So: print the resolved path, print whether the volume really backs it, and
# warn loudly when the two disagree.
# ---------------------------------------------------------------------------
# [FIX 2026-10-02] The path is resolved in SHELL, not by starting Python.
#
# This used to call print_cron_store.py, which imports nanobot -- a whole
# interpreter start on the deployment's 0.2-vCPU plan. Measured in the
# production log it cost 17.6 s, and every one of those seconds sat in front of
# the port bind, so it was 17.6 s of the 503 the user saw on every redeploy and
# every container replacement. The resolution below is the same three branches
# `nanobot.config.paths.get_persistent_data_dir` uses (mirrored in
# scripts/migrate_cron_timezone.py:default_store_path). The application's own
# answer is still checked against it -- in the background, after the port is up
# (see the audit near the privilege drop). Boot no longer waits on Python to
# learn one path.
CRON_STORE=""
if [ -n "${POWERX_DATA_DIR:-}" ]; then
    CRON_STORE="$POWERX_DATA_DIR/cron/jobs.json"
elif [ -d /data ] && [ -w /data ]; then
    CRON_STORE="/data/powerx/cron/jobs.json"
else
    CRON_STORE="$dir/persistent/cron/jobs.json"
fi
# print_cron_store.py used to create this directory as a side effect; make sure
# the mount check below sees a real path on a first boot too.
mkdir -p "$(dirname "$CRON_STORE")" 2>/dev/null || true
if [ -n "$CRON_STORE" ]; then
    echo "[entrypoint] cron store: $CRON_STORE"
    CRON_DIR=$(dirname "$CRON_STORE")
    if _data_dir_is_mounted "$CRON_DIR"; then
        echo "[entrypoint] cron store is on a mounted volume — durable across deploys"
    elif [ "$PERSISTENT_DISK" = "true" ]; then
        echo "[entrypoint] WARNING: NANOBOT_PERSISTENT_DISK=true but $CRON_DIR is on the" \
             "container filesystem; cron jobs WILL be lost on redeploy. Mount the volume" \
             "at that path or set POWERX_DATA_DIR to the volume." >&2
    fi
fi

# ---------------------------------------------------------------------------
# One-time cron timezone repair (idempotent, safe on every boot).
#
# Jobs stored before this deployment had a configured zone were stamped with a
# hard-coded "UTC" timezone, or with none at all — and the scheduler then read
# them in the container's zone, which is also UTC. Both fired an hour late for
# an owner at UTC+1. The scheduler no longer does that, but it recomputes each
# job's next run from the zone *stored on the job*, so the existing jobs must be
# rewritten or they stay an hour off forever.
#
# Rewrites only jobs whose zone is missing or a UTC alias; a job that names a
# real zone is left alone. Exits 0 with nothing to do, so it can run every boot.
# ---------------------------------------------------------------------------
if [ -f /app/scripts/migrate_cron_timezone.py ]; then
    _tz_py=""
    for _py in /app/.venv/bin/python3 python3; do
        command -v "$_py" >/dev/null 2>&1 && _tz_py="$_py" && break
    done
    if [ -n "$_tz_py" ]; then
        if [ -n "$CRON_STORE" ]; then
            "$_tz_py" /app/scripts/migrate_cron_timezone.py "$CRON_STORE" || \
                echo "[entrypoint] warning: cron timezone migration failed (continuing)"
        else
            "$_tz_py" /app/scripts/migrate_cron_timezone.py || \
                echo "[entrypoint] warning: cron timezone migration failed (continuing)"
        fi
    fi
fi

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
fi

# ---------------------------------------------------------------------------
# [FIX 2026-10-02] Chat-owner backfill runs *alongside* the gateway, not in
# front of it.
#
# It used to run right here, in the foreground, before the gateway process was
# ever exec'd -- and it reconciles every chat on the volume (503 rows on the
# production deployment, several seconds of Python). Until the gateway binds the
# exposed port nothing answers a request, and the platform's edge turns that
# into a 503 the user sees as "the app is down". Boot measured from the
# container log: 13:42:10 entrypoint start, 13:42:28 backfill finished,
# 13:42:40 "WebSocket server listening" -- 30 s of 503, a fifth of it this scan.
#
# The move is safe because chat isolation is fail-CLOSED: an unstamped chat is
# hidden from every user until this stamps it, never shared. Reconciling after
# the port is up therefore exposes nothing; it only means legacy chats appear in
# the sidebar a moment later. Local-only operation, no Supabase traffic, and the
# same behaviour on every boot.
#
# Launched below, after the privilege drop, so it runs as the same unprivileged
# user the gateway does and writes the data dir it was just chowned.
# ---------------------------------------------------------------------------

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

    # [FIX 2026-10-03] Hand the kernel back the page cache this container charged
    # to itself before it has served anything. A fresh container reads its own
    # image into page cache and reaches ~92% of the plan limit at gateway_start
    # with anonymous memory near 40% (1e9cb26) -- the platform replaces a
    # container that crosses the limit, and every replacement is a 503 window
    # while the replacement boots. It was measured again across three failed
    # boots in a row (2026-10-03 05:01-05:03), each killed before the port bound.
    #
    # memory.reclaim is root-owned (mode 0200). Root bypasses the mode bits, so
    # what actually decides whether this works is the mount: a container whose
    # /sys/fs/cgroup is mounted read-only refuses the write even here, and there
    # is no second chance after the privilege drop at the end of this block. The
    # attempt is logged either way -- a silent skip on a platform that refuses it
    # reads as "no cache to reclaim" when the truth is "this lever is closed".
    _reclaim=/sys/fs/cgroup/memory.reclaim
    if [ ! -e "$_reclaim" ]; then
        echo "[entrypoint] page cache reclaim skipped: $_reclaim absent (cgroup v1, or not exposed)"
    else
        _file_bytes=$(awk '/^file /{print $2; exit}' /sys/fs/cgroup/memory.stat 2>/dev/null || true)
        if [ -z "${_file_bytes:-}" ] || ! [ "$_file_bytes" -gt 0 ] 2>/dev/null; then
            echo "[entrypoint] page cache reclaim skipped: memory.stat reports no file bytes"
        # The kernel refuses a request larger than what is reclaimable (EIO), so
        # ask for a fraction and step down once rather than lose the write.
        elif echo "$(( _file_bytes * 3 / 4 ))" > "$_reclaim" 2>/dev/null; then
            echo "[entrypoint] reclaimed page cache: asked $(( _file_bytes / 1048576 * 3 / 4 )) MB back"
        elif echo "$(( _file_bytes / 8 ))" > "$_reclaim" 2>/dev/null; then
            echo "[entrypoint] reclaimed page cache: asked $(( _file_bytes / 1048576 / 8 )) MB back (stepped down)"
        else
            echo "[entrypoint] page cache reclaim skipped: $_reclaim refused the write (read-only mount, or not permitted); continuing" >&2
        fi
    fi
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
    # Chat-owner backfill: backgrounded so it never delays the port binding.
    if [ -f /app/scripts/backfill_chat_owners.py ]; then
        setpriv --reuid=nanobot --regid=nanobot --init-groups env \
            NANOBOT_DATA_DIR="$dir" /app/.venv/bin/python3 \
            /app/scripts/backfill_chat_owners.py --apply &
    fi
    # Cron-store audit: ask nanobot where it will actually keep cron and warn if
    # that disagrees with the shell resolution above. Backgrounded -- the port must
    # never wait on an interpreter start to print a diagnostic.
    if [ -f /app/scripts/print_cron_store.py ]; then
        (
            _app_store=$(/app/.venv/bin/python3 /app/scripts/print_cron_store.py 2>/dev/null || true)
            if [ -n "$_app_store" ] && [ "$_app_store" != "$CRON_STORE" ]; then
                echo "[entrypoint] WARNING: entrypoint resolved cron store $CRON_STORE but" \
                     "nanobot resolves $_app_store -- check POWERX_DATA_DIR" >&2
            fi
        ) &
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

# Chat-owner backfill: backgrounded so it never delays the port binding.
if [ -f /app/scripts/backfill_chat_owners.py ]; then
    NANOBOT_DATA_DIR="$dir" /app/.venv/bin/python3 \
        /app/scripts/backfill_chat_owners.py --apply &
fi

# Cron-store audit: ask nanobot where it will actually keep cron and warn if
# that disagrees with the shell resolution above. Backgrounded -- the port must
# never wait on an interpreter start to print a diagnostic.
if [ -f /app/scripts/print_cron_store.py ]; then
    (
        _app_store=$(/app/.venv/bin/python3 /app/scripts/print_cron_store.py 2>/dev/null || true)
        if [ -n "$_app_store" ] && [ "$_app_store" != "$CRON_STORE" ]; then
            echo "[entrypoint] WARNING: entrypoint resolved cron store $CRON_STORE but" \
                 "nanobot resolves $_app_store -- check POWERX_DATA_DIR" >&2
        fi
    ) &
fi

exec /app/scripts/nanobot_launcher.sh "$@"
