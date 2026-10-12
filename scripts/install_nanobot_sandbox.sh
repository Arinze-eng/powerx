#!/usr/bin/env bash
# Provision the nanobot package INSIDE an execution sandbox so a whole agent
# turn can run there instead of on the application host (nanobot/remote_turn).
#
# WHY THIS EXISTS
#   `NANOBOT_TURN_HOST=sandbox` moves a turn's model calls and tools into the
#   sandbox, and the dispatcher runs the worker as
#       python3 -m nanobot.remote_turn.worker <request.json>
#   That command needs the nanobot package to exist on the box. It does not:
#   the sandbox images ship a bare Linux userland, and baking the gateway's
#   ~150-package dependency tree into every sandbox template is the memory cost
#   this whole feature exists to remove. So the package is provisioned on demand,
#   by the turn that needs it, the same way `install_media_sandbox.sh` and
#   `install_webdev_sandbox.sh` provision their toolchains -- deliberately not
#   baked into the image (see the NOTE in the Dockerfile).
#
# WHAT IT DOES
#   1. Resolve `main` to a commit SHA through the GitHub API.
#   2. Fast-exit when the box is already provisioned at that SHA. This is the
#      common case after the first turn, and it costs one API call.
#   3. Otherwise download the commit-pinned tarball and lay it out as a source
#      tree (so PYTHONPATH can shadow the installed copy, exactly as the gateway
#      image does with PYTHONPATH=/app).
#   4. Build a private venv and install the package into it.
#   5. Write a readiness marker only after an importable package exists.
#
# DESIGN NOTES
#   * Idempotent: a rerun at the same commit reuses the venv and only re-extracts
#     the source tree, which is the cheap half.
#   * Best-effort: this script is deliberately not `set -e`. The host verifies
#     the outcome itself (the worker either emits a final event or it does not),
#     and a provisioning failure must degrade to "run the turn on the host", not
#     to a wedged dispatch. Exit code 1 with a loud reason is the signal.
#   * No root is assumed and none is needed. Nothing here calls apt or sudo.
#   * Everything chatty goes to stderr: the dispatcher parses the worker's stdout
#     as JSONL, so this script must never print a bare line to that stream.
#
# ENV
#   NANOBOT_SCRIPT_REPO   owner/repo to provision from (default Arinze-eng/powerx)
#   NANOBOT_TURN_REF      ref to resolve when not a commit (default main)
#   NANOBOT_SANDBOX_HOME  install root (default $HOME/.nanobot)
set -u

REPO="${NANOBOT_SCRIPT_REPO:-Arinze-eng/powerx}"
REF="${NANOBOT_TURN_REF:-main}"
ROOT="${NANOBOT_SANDBOX_HOME:-${HOME:-/root}/.nanobot}"
SRC="$ROOT/nanobot-src"
VENV="$ROOT/venv"
STAMP="$SRC/.commit"
MARKER="$ROOT/.worker-ready"

log() { printf '[nanobot-sandbox] %s\n' "$*" >&2; }

# The stamp is what makes the fast path correct rather than merely fast: it is
# the commit the source tree actually came from, so a box left behind by an
# earlier turn cannot keep serving stale code once main moves.
_want=""
if [ -z "$REF" ] || [ "$REF" = "main" ]; then
  _want=$(curl -fsSL --retry 3 "https://api.github.com/repos/$REPO/commits/main" 2>/dev/null \
    | python3 -c "import sys,json;print((json.load(sys.stdin) or {}).get('sha',''))" 2>/dev/null)
fi
[ -n "$_want" ] || _want="$REF"
log "target commit: $_want"

if [ -x "$VENV/bin/python" ] && [ -f "$MARKER" ] && [ -f "$STAMP" ] \
   && [ "$(cat "$STAMP" 2>/dev/null)" = "$_want" ]; then
  log "already provisioned at $_want; nothing to do"
  exit 0
fi

# ---------------------------------------------------------------- python + uv
_py=""
for _c in python3.13 python3.12 python3.11 python3; do
  if command -v "$_c" >/dev/null 2>&1 \
     && "$_c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    _py="$_c"; break
  fi
done

if ! command -v uv >/dev/null 2>&1; then
  log "installing uv"
  curl -fsSL --retry 3 https://astral.sh/uv/install.sh 2>/dev/null | sh >&2 2>&1
  PATH="${HOME:-/root}/.local/bin:$PATH"
  export PATH
fi

# uv can fetch a standalone interpreter, which is the answer for a base image
# that ships nothing newer than the 3.11 floor.
if [ -z "$_py" ] && command -v uv >/dev/null 2>&1; then
  log "no usable python on the box; asking uv for one"
  uv python install 3.12 >&2 2>&1
  _py="$(uv python find 3.12 2>/dev/null || true)"
fi
if [ -z "$_py" ]; then
  log "FAIL: no python >= 3.11 and uv could not provide one"
  exit 1
fi
log "python: $_py"

# ------------------------------------------------------------- source tarball
# codeload serves the archive for a commit; this repo is public, so no token is
# needed. Commit-pinned on purpose: the sandbox's egress path caches
# raw.githubusercontent.com by path, and a branch URL has been measured serving
# a revision several pushes old (see media.py's bootstrap note).
_url="https://codeload.github.com/$REPO/tar.gz/$_want"
_tar="$ROOT/src.tar.gz"
mkdir -p "$ROOT" || { log "FAIL: cannot create $ROOT"; exit 1; }
if ! curl -fsSL --retry 3 "$_url" -o "$_tar" 2>/dev/null || [ ! -s "$_tar" ]; then
  log "FAIL: could not download $_url"
  exit 1
fi
if ! command -v tar >/dev/null 2>&1; then
  log "FAIL: tar is missing in the sandbox"
  exit 1
fi
rm -rf "$SRC"
mkdir -p "$SRC"
if ! tar -xzf "$_tar" -C "$SRC" --strip-components=1 2>/dev/null; then
  log "FAIL: could not extract $_tar"
  exit 1
fi
log "unpacked $_want into $SRC"

# -------------------------------------------------------------------- install
# Deps do not change when only the commit does, so an already-working venv only
# needs the new source tree on PYTHONPATH. That is the difference between a
# cheap refresh and a minutes-long reinstall on every push.
_installed=""
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import nanobot" >/dev/null 2>&1; then
  _installed=1
fi

if [ -z "$_installed" ]; then
  if [ -x "$VENV/bin/python" ]; then
    log "venv exists but the package does not import; reinstalling"
  else
    log "creating the venv at $VENV"
    if command -v uv >/dev/null 2>&1; then
      # --python "$_py" is not decoration: a base image whose default python3 is
      # below the 3.11 floor would otherwise get a venv on the wrong interpreter,
      # and the package installs fine into it but cannot import.
      uv venv --python "$_py" --seed "$VENV" >&2 2>&1 \
        || uv venv --python "$_py" "$VENV" >&2 2>&1
    else
      "$_py" -m venv "$VENV" >&2 2>&1
    fi
  fi
  if [ ! -x "$VENV/bin/python" ]; then
    log "FAIL: could not create a venv at $VENV"
    exit 1
  fi

  log "installing the package (this is the slow part; it is cached after this)"
  # NANOBOT_SKIP_WEBUI_BUILD: the web UI needs the whole Vite/Node toolchain that
  # the gateway image builds in its own stage. The turn worker never serves it.
  if command -v uv >/dev/null 2>&1; then
    NANOBOT_SKIP_WEBUI_BUILD=1 uv pip install --python "$VENV/bin/python" --no-cache "$SRC" >&2 2>&1
  else
    "$VENV/bin/python" -m pip install --upgrade pip >&2 2>&1
    NANOBOT_SKIP_WEBUI_BUILD=1 "$VENV/bin/python" -m pip install --no-cache-dir "$SRC" >&2 2>&1
  fi
fi

# --------------------------------------------------------------------- verify
if ! "$VENV/bin/python" -c "from nanobot.remote_turn.worker import run_turn" >/dev/null 2>&1; then
  log "FAIL: the worker does not import in $VENV"
  exit 1
fi

printf '%s' "$_want" > "$STAMP"
: > "$MARKER"
rm -f "$_tar"
log "ready at $_want"
exit 0