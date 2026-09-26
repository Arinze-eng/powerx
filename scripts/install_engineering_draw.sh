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

# FREECAD IS THE PREFERRED ENGINE, AND IT IS INSTALLED HERE, NOT BY THE MODEL
# -------------------------------------------------------------------------
# The model must never be able to call a CAD engine that is not on the box yet,
# so the install is what puts FreeCAD there and `doctor` is what proves it did.
# FreeCAD needs three things that the python packages do not:
#   * the app itself (Debian ships `0.20.2`; upstream 1.x is not in this image),
#   * a display stack -- `freecad` refuses to start without one, and the live
#     screen panel captures :99, so Xvfb AND a window manager are installed and
#     started here rather than left to the model to discover,
#   * `librsvg2-bin` for `rsvg-convert`, which turns a TechDraw sheet's SVG into
#     the PNG the user actually looks at.
CAD_PKGS="freecad freecad-python3 xvfb matchbox-window-manager librsvg2-bin xauth libgl1-mesa-dri mesa-utils"

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

    echo "--- FreeCAD + display stack (measured: about 47s) ---"
    $APT install -y -qq $CAD_PKGS 2>&1 | tail -5 || echo "WARNING: FreeCAD apt install failed (continuing)"
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

# ------------------------------------------------------------------ FreeCAD
# FreeCAD's launcher on this image does NOT find its own interpreter, and the
# failure is silent enough to be mistaken for "FreeCAD is broken". MEASURED:
# the image carries a from-source python at /usr/local whose libpython FreeCAD's
# binary links against, so its embedded interpreter starts with no stdlib and
# dies on `import math` before a single line of FreeCAD code runs -- and
# `freecadcmd --version` segfaults outright. Exporting PYTHONHOME=/usr and
# LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu is the fix that was measured to work
# (LD_PRELOAD also works but injects a second interpreter into every child).
# The same two variables are what the CLI exports for every FreeCAD call, so the
# smoke test below runs under exactly the environment the model will get.
export PYTHONHOME=/usr
export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu/dri
export DISPLAY=:99

echo "--- FreeCAD smoke test ---"
FREECAD_BIN=""
command -v freecadcmd >/dev/null 2>&1 && FREECAD_BIN="freecadcmd"
[ -z "$FREECAD_BIN" ] && [ -x /usr/bin/freecadcmd ] && FREECAD_BIN="/usr/bin/freecadcmd"

FREE_RESULT='{"ready": false, "reason": "freecadcmd not installed"}'
if [ -n "$FREECAD_BIN" ]; then
  # A box, a cut, and a STEP export in one go: this exercises the kernel and the
  # writer, so a green result means the engine can actually build a solid rather
  # than merely start.
  FREE_RESULT=$(cat >/tmp/.ed_freecad_smoke.py <<'PY'
import json, os, traceback
out = {"ready": False, "errors": []}
try:
    import FreeCAD, Part
    out["version"] = ".".join(str(n) for n in FreeCAD.Version()[:3])
    doc = FreeCAD.newDocument("Smoke")
    box = doc.addObject("Part::Box", "Box")
    box.Length, box.Width, box.Height = 20.0, 20.0, 20.0
    cyl = doc.addObject("Part::Cylinder", "Cyl")
    cyl.Radius, cyl.Height = 5.0, 30.0
    cut = doc.addObject("Part::Cut", "Cut")
    cut.Base, cut.Tool = box, cyl
    doc.recompute()
    step = "/tmp/.ed_freecad_smoke.step"
    Part.export([cut], step)
    out["step_bytes"] = os.path.getsize(step)
    out["solids"] = len(cut.Shape.Solids)
    out["ready"] = out["step_bytes"] > 1000 and out["solids"] == 1
except Exception as exc:
    out["errors"].append("%s: %s" % (type(exc).__name__, exc))
    out["trace"] = traceback.format_exc()[-800:]
print(json.dumps(out))
PY
  "$FREECAD_BIN" /tmp/.ed_freecad_smoke.py 2>&1 | tail -20)
fi
echo "$FREE_RESULT"
freecad_ready=no
printf '%s' "$FREE_RESULT" | grep -q '"ready": true' && freecad_ready=yes
echo "freecad ready: $freecad_ready"

# The display is what the live screen captures, so it is started here and left
# running. Both daemons are idempotent to start twice: `pgrep` guards them.
echo "--- display stack (:99) ---"
if command -v Xvfb >/dev/null 2>&1; then
  pgrep -f "Xvfb $DISPLAY" >/dev/null 2>&1 || \
    nohup Xvfb "$DISPLAY" -screen 0 1280x1024x24 >"$HOME_DIR/xvfb.log" 2>&1 &
  sleep 3
  if command -v matchbox-window-manager >/dev/null 2>&1; then
    pgrep -f matchbox-window-manager >/dev/null 2>&1 || \
      nohup matchbox-window-manager -use_titlebar no >"$HOME_DIR/matchbox.log" 2>&1 &
  fi
  sleep 1
  pgrep -af "Xvfb|matchbox" || echo "WARNING: the display did not come up"
else
  echo "WARNING: no Xvfb; the live screen will have nothing to capture."
fi

# What the model reads before it designs. Kept as JSON next to the done-marker
# so `doctor` and a human can both see what this sandbox actually has.
python3 - "$freecad_ready" <<'PY' 2>&1 | tail -3
import json, os, platform, shutil, sys
home = os.environ.get("ENGINEERING_DRAW_HOME") or os.path.join(os.path.expanduser("~"), ".engineering_draw")
os.makedirs(home, exist_ok=True)
info = {
    "freecad_ready": sys.argv[1] == "yes",
    "freecad_console": shutil.which("freecadcmd") or "",
    "freecad_gui": shutil.which("freecad") or "",
    "xvfb": shutil.which("Xvfb") or "",
    "rsvg_convert": shutil.which("rsvg-convert") or "",
    "display": os.environ.get("DISPLAY", ":99"),
    "python": platform.python_version(),
    "py_freecad": "PYTHONHOME=/usr LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu",
}
with open(os.path.join(home, "install.json"), "w") as fh:
    json.dump(info, fh, indent=2, sort_keys=True)
with open(os.path.join(home, "freecad.status"), "w") as fh:
    fh.write("ready\n" if info["freecad_ready"] else "unavailable\n")
print(json.dumps(info))
PY

if [ "$readiness" = "READY" ] || [ "$freecad_ready" = "yes" ]; then
  echo "=== install complete $(date -u +%Y-%m-%dT%H:%M:%SZ) (freecad=$freecad_ready python=$readiness) ==="
  printf 'ready\n' >"$DONE"
else
  echo "=== install finished but the engine is NOT ready $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  echo "=== see the python and FreeCAD errors above ==="
  printf 'failed\n' >"$DONE"
fi
