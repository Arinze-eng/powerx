#!/usr/bin/env bash
# Install the engineering drawing engine's dependencies inside the execution
# sandbox. Idempotent: safe to run twice, and it exits 0 when the engine already
# works so the tool never burns a sandbox call re-installing.
#
# WHY THIS IS A SCRIPT AND NOT A PIP LINE IN THE TOOL
# ---------------------------------------------------
# The install takes minutes (build123d pulls OpenCASCADE, hundreds of MB), which
# is longer than any single sandbox command should be held open. So the tool
# starts this detached, writes progress to a log and a done-marker, and the model
# polls `status` itself. That is the same contract `install_mt5_sandbox.sh` uses.
#
# THE RUNTIME IS NOT ASSUMED
# --------------------------
# A Novita `secure` sandbox hands back uid 1000, NOT root (measured: `id` ->
# uid=1000(user) groups=...,27(sudo)). So `apt-get install` fails with a lock
# error, and the OCP wheel then fails to import for want of libGL and friends.
# Two consequences, both handled below:
#   * apt is attempted through `sudo -n` and its failure is a WARNING, never a
#     failed install -- but the install is only called a success once the imports
#     actually work, so a silent "installed but unusable" cannot be reported.
#   * a venv is not used: the sandbox's system python is what every other CLI in
#     this repo runs under.
set -uo pipefail

HOME_DIR="${ENGINEERING_DRAW_HOME:-$HOME/.engineering_draw}"
BIN_DIR="$HOME_DIR/bin"
LOG="$HOME_DIR/install.log"
DONE="$HOME_DIR/.install.done"

mkdir -p "$BIN_DIR"
exec >"$LOG" 2>&1
rm -f "$DONE"

echo "=== engineering_draw install $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "user: $(id -un) ($(id -u)); python: $(python3 --version 2>&1)"

PKGS="libgl1 libglu1-mesa libxrender1 libxext6 libsm6 libxkbcommon0 libfontconfig1 libgomp1 libxcursor1 libxfixes3 libxi6"

echo "--- system libraries for OpenCASCADE (best effort) ---"
if command -v apt-get >/dev/null 2>&1; then
  if [ "$(id -u)" = "0" ]; then
    APT="apt-get"
  elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    APT="sudo -n env DEBIAN_FRONTEND=noninteractive apt-get"
  else
    APT=""
  fi
  if [ -n "$APT" ]; then
    # `apt-get update` FIRST, and it is not optional. MEASURED FAILURE: a Novita
    # secure sandbox ships an image whose apt lists were never populated, so
    # `apt-get install libgl1` answers "E: Unable to locate package libgl1" even
    # though the package exists upstream. Installing without updating looks like
    # "the library is unavailable on this image" when the truth is "the index was
    # empty", which sent the whole install down the wrong path once already.
    #
    # Non-interactive and best-effort. A failure here is NOT fatal: the manylinux
    # wheels build123d and ezdxf ship are self-contained, and the import check
    # below is what actually decides whether this worked.
    $APT update -qq 2>&1 | tail -3 || echo "WARNING: apt update failed (continuing)"
    $APT install -y -qq $PKGS 2>&1 | tail -5 || echo "WARNING: apt install failed (continuing)"
  else
    echo "WARNING: no root or passwordless sudo; skipping system libraries."
    echo "The import check below decides whether that mattered."
  fi
else
  echo "WARNING: no apt-get on this image; skipping system libraries."
fi

echo "--- python packages ---"
pip install --no-input --disable-pip-version-check --upgrade \
  build123d ezdxf matplotlib 2>&1 | tail -8
PIP_STATUS=${PIPESTATUS[0]}
echo "pip exit: $PIP_STATUS"

# The imports are the acceptance test, not pip's exit code. build123d importing
# means the OCP shared objects loaded, which is exactly what the apt step above
# was for.
echo "--- verify ---"
VERIFY=$(python3 - <<'PY' 2>&1
import json, sys
found = {}
for name in ("build123d", "ezdxf", "matplotlib"):
    try:
        module = __import__(name)
        found[name] = getattr(module, "__version__", "unknown")
    except Exception as exc:
        found[name] = f"MISSING ({type(exc).__name__}: {exc})"
print(json.dumps(found))
print("READY" if not any(str(v).startswith("MISSING") for v in found.values()) else "NOT_READY")
PY
)
echo "$VERIFY"
readiness=$(printf '%s' "$VERIFY" | tail -1)

if [ "$readiness" = "READY" ]; then
  echo "=== install complete $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  printf 'ready\n' >"$DONE"
else
  echo "=== install finished but the engine is NOT ready $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  echo "=== see the python error above ==="
  printf 'failed\n' >"$DONE"
fi
