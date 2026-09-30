#!/usr/bin/env bash
# Install a headless Android runtime inside an execution sandbox.
#
# WHAT THIS BUILDS
# ----------------
# A windowless, KVM-accelerated Android emulator plus `adb`, so the agent can
# install an APK, launch it, drive it and read its screen with no GUI anywhere
# in the loop. That is what "run Android fully, no screen touch" needs: `adb`
# is a complete programmatic control surface (install, launch, input events,
# UI hierarchy dumps, screenshots, file push/pull, logcat).
#
# WHY AN EMULATOR AND NOT WAYDROID
# --------------------------------
# Waydroid was the first choice (much lighter) and it was measured to be
# impossible on this sandbox, not merely awkward: Waydroid's container needs the
# `binder_linux` kernel module, the sandbox runs a provider kernel (6.1.102) with
# no `binder_linux`/`ashmem_linux` modules and no `/lib/modules/<ver>/build`
# headers, so the module cannot be loaded OR compiled. The Android emulator
# carries its own kernel, needs no host module, and the sandbox exposes
# `/dev/kvm` with nested virtualisation working (`kvm-ok` passes), so hardware
# acceleration is available.
#
# Idempotent: every step is skipped when its output already exists, so a second
# run is cheap and the sandbox can be re-entered safely.
set -u

HOME_DIR="${ANDROID_HOME_DIR:-$HOME/.android_box}"
SDK="${ANDROID_SDK_ROOT:-$HOME/android-sdk}"
AVD_NAME="${ANDROID_AVD_NAME:-powerx}"
SYSTEM_IMAGE="${ANDROID_SYSTEM_IMAGE:-system-images;android-30;google_apis;x86_64}"
API_LEVEL="${SYSTEM_IMAGE##*;android-}"
API_LEVEL="${API_LEVEL%%;*}"
CMDLINE_ZIP="${ANDROID_CMDLINE_ZIP:-commandlinetools-linux-11076708_latest.zip}"
CMDLINE_URL="${ANDROID_CMDLINE_URL:-https://dl.google.com/android/repository/$CMDLINE_ZIP}"

mkdir -p "$HOME_DIR" "$SDK/cmdline-tools" "$HOME/.android/avd"
LOG="$HOME_DIR/install.log"
export ANDROID_SDK_ROOT="$SDK" ANDROID_HOME="$SDK"
export ANDROID_AVD_HOME="$HOME/.android/avd"
export PATH="$SDK/cmdline-tools/latest/bin:$SDK/platform-tools:$SDK/emulator:$PATH"
export DEBIAN_FRONTEND=noninteractive

step() { echo "==> $*"; }
ok() { echo "    ok: $*"; }

# The completion marker is cleared up front, before any work: on a re-run the
# previous run's marker is still on disk, and `status` reads only that file -- so
# without this a second install reported "done" ~25 s in, while apt and
# sdkmanager were still working. Measured on the Freestyle VM (2026-09-30): a
# re-run was declared finished at 27.7 s of a job that takes minutes cold.
rm -f "$HOME_DIR/.install.done"
echo "install started $(date -u +%FT%TZ)" > "$HOME_DIR/.install.running"

# --------------------------------------------------------------------------- #
# 1. KVM access
# --------------------------------------------------------------------------- #
# MEASURED FAILURE: the emulator aborts with
#   "x86_64 emulation currently requires hardware acceleration! ...
#    This user doesn't have permissions to use KVM (/dev/kvm)"
# because /dev/kvm is root:kvm 0660 and the sandbox user is not in group kvm.
# `gpasswd -a` alone does NOT help inside one command -- the new group only
# applies to a fresh login, and the emulator is started from this same session,
# so the mode is widened explicitly as well. Doing only one of the two would
# leave this broken for exactly the case that matters (a first boot here).
step "kvm: permissions"
if [ -e /dev/kvm ]; then
  sudo gpasswd -a "$(id -un)" kvm >/dev/null 2>&1 || true
  sudo chmod 666 /dev/kvm 2>/dev/null || true
  ok "/dev/kvm modes now $(stat -c '%A' /dev/kvm 2>/dev/null)"
else
  # Not fatal any more: the emulator falls back to software emulation (see
  # android_cli.py's accel_mode). It is minutes slower, so say so plainly --
  # a silent absence is what made "the emulator never boots" look like a bug.
  echo "WARNING: /dev/kvm is absent -- no hardware acceleration. The emulator will" >&2
  echo "         fall back to software emulation, which is several times slower." >&2
  echo "         A VM backend that exposes nested virtualisation is recommended." >&2
fi

# --------------------------------------------------------------------------- #
# 2. Host packages
# --------------------------------------------------------------------------- #
# avdmanager is a Java tool, so a JRE is required; unzip/curl fetch the SDK.
step "apt: jre + unzip"
if ! command -v java >/dev/null 2>&1 || ! command -v unzip >/dev/null 2>&1; then
  sudo apt-get update -qq >/dev/null 2>&1 || true
  sudo apt-get install -y -qq unzip curl openjdk-17-jre-headless >/dev/null 2>&1 || true
fi
if command -v java >/dev/null 2>&1; then ok "$(java -version 2>&1 | head -1)"; else
  echo "WARNING: no JRE -- avdmanager will fail; continuing anyway" >&2
fi

# --------------------------------------------------------------------------- #
# 3. Android command-line tools
# --------------------------------------------------------------------------- #
step "sdk: command-line tools"
if [ ! -x "$SDK/cmdline-tools/latest/bin/sdkmanager" ]; then
  curl -fsSL --retry 3 -o /tmp/clt.zip "$CMDLINE_URL" || {
    echo "ERROR: could not download $CMDLINE_URL" >&2; exit 1; }
  rm -rf "$SDK/cmdline-tools/latest" "$SDK/cmdline-tools/cmdline-tools"
  unzip -q -o /tmp/clt.zip -d "$SDK/cmdline-tools" || { echo "ERROR: unzip failed" >&2; exit 1; }
  # The zip always unpacks a top-level cmdline-tools/ directory; sdkmanager
  # insists on finding it under .../latest/bin.
  mv "$SDK/cmdline-tools/cmdline-tools" "$SDK/cmdline-tools/latest" 2>/dev/null || true
  ok "installed to $SDK/cmdline-tools/latest"
else
  ok "already present"
fi

# --------------------------------------------------------------------------- #
# 4. platform-tools + emulator + system image
# --------------------------------------------------------------------------- #
step "sdk: platform-tools, emulator, $SYSTEM_IMAGE"
yes 2>/dev/null | "$SDK/cmdline-tools/latest/bin/sdkmanager" --licenses >/dev/null 2>&1 || true
need_pkgs=""
[ -x "$SDK/platform-tools/adb" ] || need_pkgs="$need_pkgs platform-tools"
[ -x "$SDK/emulator/emulator" ] || need_pkgs="$need_pkgs emulator"
[ -f "$SDK/system-images/android-$API_LEVEL/google_apis/x86_64/system.img" ] || need_pkgs="$need_pkgs $SYSTEM_IMAGE"
if [ -n "$need_pkgs" ]; then
  # shellcheck disable=SC2086
  "$SDK/cmdline-tools/latest/bin/sdkmanager" $need_pkgs 2>&1 | tail -3
else
  ok "already present"
fi
if [ -x "$SDK/platform-tools/adb" ]; then ok "adb present"; else
  echo "ERROR: adb was not installed" >&2; exit 1; fi
if [ -x "$SDK/emulator/emulator" ]; then ok "emulator present"; else
  echo "ERROR: emulator was not installed" >&2; exit 1; fi

# --------------------------------------------------------------------------- #
# 4.5 Emulator runtime libraries
# --------------------------------------------------------------------------- #
# MEASURED FAILURE (Tenki, Ubuntu 24.04, kernel 6.18.29, 2026-09-30): on a bare
# image the emulator binary refuses to start at all --
#   emulator: error while loading shared libraries: libX11.so.6
# -- because the launcher links X11 even when it runs -no-window. The installer
# used to bring only a JRE, so `doctor` reported a complete SDK (adb + emulator
# + system image all present) and only the BOOT failed, which reads as "the
# emulator is broken".
#
# A second, quieter failure follows the first: the emulator dlopen()s
# libX11-xcb.so.1 and libpulse.so.0 at runtime, and those never appear in ldd,
# so a half-fixed host dies with a bare segmentation fault. Hence both halves
# below: the explicit dlopen-only set, then a loop over ldd's own "not found"
# list so the install tracks the binary instead of a hand-written list that
# rots. (Measured: with only libx11-6 installed, `-accel-check` passed while a
# real launch segfaulted on libX11-xcb. ldd alone is not the whole answer.)
step "apt: emulator runtime libraries"
EMU_DLOPEN_PKGS="libx11-6 libx11-xcb1 libxext6 libxcb1 libxcb-render0 libxcb-shm0 \
libpulse0 libgl1 libglu1-mesa libglvnd0 libegl1 libgbm1 libdrm2 libnss3 \
libxcomposite1 libxcursor1 libxi6 libxtst6 libxrandr2 libxrender1"
if ! command -v apt-get >/dev/null 2>&1; then
  ok "no apt-get; skipping (the image must already carry the emulator's libs)"
else
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    $EMU_DLOPEN_PKGS >/dev/null 2>&1 || true
  # Ubuntu 24.04 renamed libasound2 -> libasound2t64 and refuses the old name, so
  # try both rather than failing the step on a distro-specific package name.
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    libasound2t64 >/dev/null 2>&1 \
    || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
         libasound2 >/dev/null 2>&1 || true

  if [ -x "$SDK/emulator/emulator" ]; then
    for _round in 1 2 3; do
      missing=$(ldd "$SDK/emulator/emulator" 2>/dev/null | grep 'not found' | awk '{print $1}' | sort -u)
      [ -n "$missing" ] || break
      pkgs=""
      for lib in $missing; do
        case "$lib" in
          libX11.so*) pkg=libx11-6 ;;
          libXext.so*) pkg=libxext6 ;;
          libX11-xcb.so*) pkg=libx11-xcb1 ;;
          libxcb.so*) pkg=libxcb1 ;;
          libxcb-render.so*) pkg=libxcb-render0 ;;
          libxcb-shm.so*) pkg=libxcb-shm0 ;;
          libpulse.so*) pkg=libpulse0 ;;
          libGL.so*) pkg=libgl1 ;;
          libGLU.so*) pkg=libglu1-mesa ;;
          libGLX.so*) pkg=libglvnd0 ;;
          libEGL.so*) pkg=libegl1 ;;
          libgbm.so*) pkg=libgbm1 ;;
          libdrm.so*) pkg=libdrm2 ;;
          libnss3.so) pkg=libnss3 ;;
          libXcomposite.so*) pkg=libxcomposite1 ;;
          libXcursor.so*) pkg=libxcursor1 ;;
          libXi.so*) pkg=libxi6 ;;
          libXtst.so*) pkg=libxtst6 ;;
          libXrandr.so*) pkg=libxrandr2 ;;
          libXrender.so*) pkg=libxrender1 ;;
          libasound.so*) pkg=libasound2t64 ;;
          *) pkg="" ;;
        esac
        [ -n "$pkg" ] && pkgs="$pkgs $pkg"
      done
      if [ -z "$pkgs" ]; then
        echo "WARNING: emulator still misses libraries with no known package:" >&2
        echo "         $missing" >&2
        break
      fi
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
        $pkgs >/dev/null 2>&1 || true
    done
    still=$(ldd "$SDK/emulator/emulator" 2>/dev/null | grep -c 'not found')
    if [ "$still" = "0" ]; then
      ok "emulator's shared libraries are satisfied"
    else
      echo "WARNING: $still emulator libraries are still missing; boot will fail" >&2
    fi
    # The launcher dlopen()s X11 and audio at runtime, which ldd cannot see, so
    # prove the binary actually loads before declaring the SDK usable.
    if timeout 25 "$SDK/emulator/emulator" -version >/dev/null 2>&1; then
      ok "emulator binary loads"
    else
      echo "WARNING: the emulator binary did not load cleanly (it dlopen()s X11 and" >&2
      echo "         audio at runtime); a boot may fail with a segfault." >&2
    fi
  fi
fi

# --------------------------------------------------------------------------- #
# 5. AVD
# --------------------------------------------------------------------------- #
step "avd: $AVD_NAME"
mkdir -p "$ANDROID_AVD_HOME"
if [ ! -f "$ANDROID_AVD_HOME/$AVD_NAME.ini" ]; then
  echo no | "$SDK/cmdline-tools/latest/bin/avdmanager" create avd \
    -n "$AVD_NAME" -k "$SYSTEM_IMAGE" --force 2>&1 | tail -2
else
  ok "already present"
fi
if [ -f "$ANDROID_AVD_HOME/$AVD_NAME.ini" ]; then ok "$AVD_NAME.ini"; else
  echo "ERROR: AVD was not created" >&2; exit 1; fi

# MEASURED (2026-09-30): avdmanager with no --device creates a 320x640 mdpi AVD
# with hw.ramSize=96M and one core. The CLI's -memory/-cores flags override the
# RAM and CPU, but the SCREEN does not come from a flag, so every screenshot and
# every tap landed on a 320x640 mdpi display -- too small for real apps (their
# layouts collapse and some refuse to start) and nothing like the phone the user
# actually has. Pin a normal phone panel here; the CLI's -skin is not used so a
# hand-edited AVD and a fresh one behave the same.
AVD_DIR="$ANDROID_AVD_HOME/$AVD_NAME.avd"
if [ -f "$AVD_DIR/config.ini" ]; then
  set_avd() {
    _key="$1"; _value="$2"
    if grep -q "^$_key=" "$AVD_DIR/config.ini"; then
      sed -i "s|^$_key=.*|$_key=$_value|" "$AVD_DIR/config.ini"
    else
      echo "$_key=$_value" >> "$AVD_DIR/config.ini"
    fi
  }
  set_avd hw.lcd.width "${ANDROID_AVD_WIDTH:-1080}"
  set_avd hw.lcd.height "${ANDROID_AVD_HEIGHT:-1920}"
  set_avd hw.lcd.density "${ANDROID_AVD_DENSITY:-420}"
  set_avd hw.ramSize "${ANDROID_AVD_RAM_MB:-2048}"
  set_avd hw.cpu.ncore "${ANDROID_AVD_CORES:-2}"
  ok "display $(grep '^hw.lcd.width=' "$AVD_DIR/config.ini" | cut -d= -f2)x$(grep '^hw.lcd.height=' "$AVD_DIR/config.ini" | cut -d= -f2) @ $(grep '^hw.lcd.density=' "$AVD_DIR/config.ini" | cut -d= -f2)dpi"
fi

# Hardware acceleration is reported, not assumed: a container without /dev/kvm
# still installs cleanly and boots on the software fallback, and the operator
# needs to see which of the two they got.
ACCEL_NOTE="accel: none (no /dev/kvm -- software emulation, slow)"
if [ -e /dev/kvm ]; then
  ACCEL_NOTE="accel: /dev/kvm present -- hardware acceleration"
fi
rm -f "$HOME_DIR/.install.running"
echo "ANDROID_READY $SYSTEM_IMAGE ($ACCEL_NOTE)" | tee "$HOME_DIR/.install.done"
echo "==> done. Boot it with: android_cli.py boot"
