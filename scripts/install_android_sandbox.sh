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
  echo "WARNING: /dev/kvm is absent -- Android will not boot without hardware" >&2
  echo "acceleration. The sandbox must expose nested virtualisation." >&2
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

echo "ANDROID_READY $SYSTEM_IMAGE" | tee "$HOME_DIR/.install.done"
echo "==> done. Boot it with: android_cli.py boot"
