#!/usr/bin/env bash
# Install a headless MetaTrader 5 (MT5) terminal + Wine + Python bridge inside a
# Linux sandbox (Novita / Daytona / Runloop / Upstash / VPS).
#
# WHY THIS EXISTS
#   The trading feature must not run Wine, MT5, or the MetaTrader5 Python
#   package on the Northflank/Render host. Doing so would install a full amd64
#   Wine prefix plus a GUI terminal on the application server, burn its CPU/RAM,
#   and (worst case) OOM the gateway that serves every user. Instead the whole
#   MT5 stack lives in the user's ephemeral sandbox: Wine + MT5 + the Python
#   `MetaTrader5` bridge module. The host only forwards commands.
#
# WHAT IT INSTALLS (inside the sandbox only)
#   - wine64 / wine32 + wine64 runtime (amd64 emulation is native on x86_64)
#   - xvfb               headless X server so MT5 can start without a display
#   - winbind            Wine needs it for the terminal's named-pipe IPC
#   - mt5 installer      mt5setup.exe (silent /auto install into the Wine prefix)
#   - MetaTrader5        `pip install MetaTrader5` (the official Python bridge)
#   - rpyc-free broker   a small `mt5_cli.py` that talks to the terminal
#
# DESIGN NOTES
#   * Everything is idempotent: rerunning the script reuses the Wine prefix and
#     the downloaded installer instead of re-downloading ~500 MB.
#   * MT5 is launched under a dedicated Xvfb display (:99) and its terminal
#     process is kept warm; login happens through the CLI wrapper, never
#     interactively.
#   * The Wine prefix lives at $HOME/.wine-mt5 so it can be packed into a
#     Novita template later without polluting other Wine apps.
set -euo pipefail

MT5_ROOT="${MT5_ROOT:-$HOME/.mt5}"
WINE_PREFIX="${WINE_PREFIX:-$HOME/.wine-mt5}"
DISPLAY_NUM="${MT5_DISPLAY_NUM:-99}"
MT5_INSTALLER_URL="${MT5_INSTALLER_URL:-https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe}"
# MetaTrader5's PyPI wheels are Windows-only, so the bridge needs a Windows
# python inside the Wine prefix (see section 5 below).
MT5_WINPY_VERSION="${MT5_WINPY_VERSION:-3.11.9}"
# Wine-Gecko release used by the MT5 web installer. Space-separated fallbacks are
# attempted in order; the default matches WineHQ stable's own pairing closely
# enough that the embedded browser works.
MT5_GECKO_VERSION="${MT5_GECKO_VERSION:-2.47.4 2.47.3}"

log() { printf '[mt5-install] %s\n' "$*" >&2; }

# Publish a machine-readable progress marker so the caller can follow a LONG
# install without holding a single (timeout-capped) sandbox command open. The
# Novita tool clamps every command to 900 s while a full Wine+MT5 install can
# take longer, so the install is started detached and polled through this file.
# Format: ``<stage>|<human message>`` — deliberately one line, last write wins.
status() {
  local stage="$1"; shift
  mkdir -p "$MT5_ROOT" 2>/dev/null || true
  printf '%s|%s\n' "$stage" "$*" >"${MT5_ROOT}/install.status" 2>/dev/null || true
  log "$*"
}

is_root() { [ "$(id -u)" -eq 0 ]; }

# Novita's stock "base" template ships ~486 MB of RAM. Wine initialises fine
# there, but MT5's installer plus the terminal it unpacks needs far more and gets
# OOM-killed mid-install — which surfaces as an inexplicable "Killed" line and no
# terminal64.exe. Fail fast with the actual fix instead of burning minutes on a
# doomed install. The Novita execution tool already auto-builds a sized template
# (see nanobot/agent/tools/novita_sandbox.py::_template_sizing), so in a normal
# deployment this check passes; it exists to make a mis-sized sandbox obvious.
MIN_MEMORY_MB="${MT5_MIN_MEMORY_MB:-1800}"

# Any uncaught error is recorded as a terminal stage so a poller sees a definite
# failure instead of waiting forever on a stale "in progress" marker.
_on_error() {
  local rc=$?
  printf 'failed|installer exited with code %s\n' "$rc" >"${MT5_ROOT}/install.status" 2>/dev/null || true
  exit "$rc"
}
trap _on_error ERR

status bootstrap "installer started"

if [ -r /proc/meminfo ]; then
  AVAILABLE_MB=$(awk '/^MemTotal:/ {printf "%d", $2/1024}' /proc/meminfo)
  if [ "${AVAILABLE_MB:-0}" -lt "${MIN_MEMORY_MB}" ]; then
    log "FATAL: sandbox has ${AVAILABLE_MB} MB RAM but MT5 needs >= ${MIN_MEMORY_MB} MB."
    log "The installer and terminal are OOM-killed on the stock ~486 MB template."
    log "Fix: run this in a sized sandbox (NOVITA_SANDBOX_MEMORY_MB=4096, or an"
    log "existing powerx-base-2g-c2 / powerx-base-4g template), then retry."
    status failed "insufficient memory: ${AVAILABLE_MB} MB available, >= ${MIN_MEMORY_MB} MB required"
    printf '{"ok": false, "error": "insufficient memory: %s MB available, %s MB required", "fix": "use a sandbox with >= %s MB (NOVITA_SANDBOX_MEMORY_MB=4096)"}\n' \
      "${AVAILABLE_MB}" "${MIN_MEMORY_MB}" "${MIN_MEMORY_MB}"
    exit 6
  fi
  status bootstrap "sandbox memory: ${AVAILABLE_MB} MB (>= ${MIN_MEMORY_MB} MB required)"
fi

# Passwordless sudo keeps the script working on the sized Novita templates,
# which run as uid 1000 while the stock base image runs as root.
SUDO=""
if ! is_root; then
  if sudo -n true >/dev/null 2>&1; then
    SUDO="sudo -n"
  fi
fi

apt_install() {
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    $SUDO apt-get update -qq >/dev/null 2>&1 || true
    $SUDO apt-get install -y -qq --no-install-recommends "$@" >/dev/null 2>&1
    return $?
  fi
  return 1
}

# --------------------------------------------------------------------------- #
# 1. Base packages
# --------------------------------------------------------------------------- #
# WINE VERSION MATTERS. Debian's packaged Wine 8.0 cannot run the MetaTrader5
# bridge at all: the first broker call aborts with
#   "Call from ... to unimplemented function ucrtbase.dll.crealf"
# Wine 9+ implements crealf (and the other C99 complex-math ucrtbase entry
# points the bridge uses), so we install WineHQ's stable build. Without this the
# terminal installs fine and then *every* IPC call dies, which is a confusing
# failure to debug later.
install_winehq() {
  # Only meaningful on Debian/Ubuntu with passwordless sudo; otherwise fall back
  # to the distro Wine, which is enough for the terminal but not the bridge.
  command -v apt-get >/dev/null 2>&1 || return 1
  $SUDO dpkg --add-architecture i386 >/dev/null 2>&1 || return 1
  $SUDO mkdir -pm755 /etc/apt/keyrings >/dev/null 2>&1 || return 1
  $SUDO wget -q -O /etc/apt/keyrings/winehq-archive.key \
      https://dl.winehq.org/wine-builds/winehq.key >/dev/null 2>&1 || return 1

  # Pick the sources file matching this distro's codename.
  local codename="bookworm"
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    codename="${VERSION_CODENAME:-bookworm}"
  fi
  $SUDO wget -q -NP /etc/apt/sources.list.d/ \
      "https://dl.winehq.org/wine-builds/debian/dists/${codename}/winehq-${codename}.sources" \
      >/dev/null 2>&1 || return 1

  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update -qq >/dev/null 2>&1 || return 1
  $SUDO apt-get install -y -qq --install-recommends winehq-stable >/dev/null 2>&1 || return 1
  return 0
}

WINE_MAJOR=0
if command -v wine >/dev/null 2>&1; then
  WINE_MAJOR=$(wine --version 2>/dev/null | sed 's/[^0-9]*\([0-9]*\).*/\1/' || echo 0)
fi

# Supporting tools are installed FIRST and independently of wine. They must not
# be gated on "wine is missing": WineHQ provides wine itself, so installing it
# first used to skip xvfb/winbind entirely (and MT5 then had no display).
apt_install xvfb winbind cabextract p7zip-full ca-certificates curl wget unzip \
            python3-pip fonts-wine || true

# MT5's terminal is a GUI app: without a GL/Vulkan loader it aborts before it can
# even open a window (``err:vulkan:vulkan_init_once Failed to load libvulkan.so.1``
# followed by a hard error). These are cheap and make the terminal startable.
apt_install libgl1 libglu1-mesa libvulkan1 mesa-vulkan-drivers \
            libgnutls30 libasound2 || true

# Reinstall via WineHQ when missing or too old for the bridge (< 9).
if [ "${WINE_MAJOR:-0}" -lt 9 ]; then
  if [ "${WINE_MAJOR:-0}" -eq 0 ]; then
    status wine "installing WineHQ stable (>= 9 required by the MetaTrader5 bridge) ..."
  else
    status wine "wine ${WINE_MAJOR} is too old for the bridge; installing WineHQ stable ..."
  fi
  install_winehq || log "WARN: WineHQ install failed"
fi

if ! command -v wine >/dev/null 2>&1 && ! command -v wine64 >/dev/null 2>&1; then
  status wine "installing distro wine ..."
  apt_install wine64 wine32 wine || apt_install wine || true
fi

if command -v wine >/dev/null 2>&1; then
  WINE_BIN=wine
elif command -v wine64 >/dev/null 2>&1; then
  WINE_BIN=wine64
else
  log "FATAL: wine is not available after install"
  exit 3
fi

# --------------------------------------------------------------------------- #
# 2. Headless display
# --------------------------------------------------------------------------- #
if command -v Xvfb >/dev/null 2>&1; then
  if ! pgrep -f "Xvfb :${DISPLAY_NUM}" >/dev/null 2>&1; then
    status display "starting Xvfb on :${DISPLAY_NUM}"
    nohup Xvfb ":${DISPLAY_NUM}" -screen 0 1280x1024x24 >/dev/null 2>&1 &
    sleep 2
  fi
  export DISPLAY=":${DISPLAY_NUM}"
else
  log "WARN: Xvfb missing; MT5 may fail to start without a display"
fi

# --------------------------------------------------------------------------- #
# 3. Wine prefix
# --------------------------------------------------------------------------- #
export WINEPREFIX="${WINE_PREFIX}"
export WINEDEBUG="${WINEDEBUG:--all}"
export WINEARCH=win64
# THE MOST IMPORTANT LINE IN THIS FILE.
#
# On a headless first boot, Wine tries to offer its Mono (.NET) and Gecko (HTML)
# add-ons through a GUI prompt. Nothing can answer that prompt in a container, so
# ``wineboot`` blocks inside setupapi's ``InstallHinfSection`` and never returns —
# measured at 10+ minutes with no terminal64.exe produced, which looks exactly
# like "the install silently failed". Disabling both DLLs makes wineboot finish
# its one-time prefix work in seconds (~6 s measured on Debian 12 / WineHQ
# stable). This is also why Wine never needed a real display here.
#
# Set unconditionally (not ``:=``): a partially-answered prefix is what breaks
# the install, so honouring a stale caller value would reintroduce the hang.
export WINEDLLOVERRIDES="mscoree,mshtml="

if [ ! -d "${WINE_PREFIX}/drive_c" ]; then
  status wineprefix "initialising wine prefix at ${WINE_PREFIX} (this takes minutes)"
  mkdir -p "${WINE_PREFIX}"
  # wineboot can return non-zero on first run in headless containers and its
  # setupapi phase is slow and occasionally wedges, so it is bounded. A partial
  # prefix is still usable — wineboot finishes the remaining work lazily.
  timeout 900 "$WINE_BIN" wineboot --init >/dev/null 2>&1 || true
  sleep 5
fi

# Let the prefix finish initialising before installing anything. Wine 9+ does a
# lot of one-time work on first boot (registry, .NET stubs, prefix rebuild);
# starting the MT5 installer concurrently makes both slower and can push the
# install past its deadline.
#
# This is strictly BEST-EFFORT and must never block: ``wineserver -w`` waits for
# the wineserver to go idle, which on a fresh prefix can take a long time, so it
# is bounded by ``timeout``. A prefix that is still busy is harmless — the MT5
# installer just queues behind it.
status wineprefix "settling wine prefix (best effort) ..."
if command -v wineserver >/dev/null 2>&1; then
  timeout 180 wineserver -w >/dev/null 2>&1 || true
fi

# --------------------------------------------------------------------------- #
# 4. MT5 terminal
# --------------------------------------------------------------------------- #
mkdir -p "${MT5_ROOT}"
INSTALLER="${MT5_ROOT}/mt5setup.exe"
DONE_MARKER="${MT5_ROOT}/.installed"

if [ ! -f "${DONE_MARKER}" ]; then
  if [ ! -s "${INSTALLER}" ]; then
    status download "downloading the MT5 installer ..."
    curl -fsSL --retry 3 --max-time 600 -o "${INSTALLER}" "${MT5_INSTALLER_URL}" \
      || wget -q -O "${INSTALLER}" "${MT5_INSTALLER_URL}" \
      || { status failed "could not download the MT5 installer"; exit 4; }
  fi

  # ------------------------------------------------------------------ #
  # 4a. Wine-Gecko — REQUIRED by mt5setup.exe
  # ------------------------------------------------------------------ #
  # mt5setup.exe is a *web* installer with an embedded browser. Without
  # mshtml/Gecko it aborts with ``fixme:ntdll:NtRaiseHardError`` and produces no
  # terminal64.exe at all — the exact symptom of a "silent" install failure.
  #
  # The catch: wineboot must NOT install Mono/Gecko itself or it wedges in
  # setupapi on a headless box (see section 3). So the DLLs are disabled for the
  # prefix boot, and real Gecko is installed here from the official MSI.
  MSHTML_DLL="${WINE_PREFIX}/drive_c/windows/system32/mshtml.dll"
  if [ ! -s "${MSHTML_DLL}" ]; then
    GECKO_MSI="${MT5_ROOT}/wine-gecko-x86_64.msi"
    if [ ! -s "${GECKO_MSI}" ]; then
      status gecko "downloading Wine-Gecko (needed by the MT5 web installer) ..."
      # Try a small list of versions so one 404 does not fail the whole install.
      for ver in ${MT5_GECKO_VERSION}; do
        curl -fsSL --retry 2 --max-time 600 -o "${GECKO_MSI}" \
          "https://dl.winehq.org/wine/wine-gecko/${ver}/wine-gecko-${ver}-x86_64.msi" \
          && break || rm -f "${GECKO_MSI}"
      done
    fi
    if [ -s "${GECKO_MSI}" ]; then
      status gecko "installing Wine-Gecko into the prefix ..."
      # msiexec runs with mshtml enabled so the MSI's own registration succeeds.
      WINEDLLOVERRIDES="mscoree=" timeout 600 "$WINE_BIN" msiexec /i "${GECKO_MSI}" /qn \
        >/dev/null 2>&1 || log "WARN: Wine-Gecko msiexec returned non-zero"
    else
      log "WARN: could not download Wine-Gecko; the MT5 web installer may abort"
    fi
  fi

  status mt5 "running the silent MT5 install (several minutes) ..."
  # /auto performs an unattended install into the current prefix. mshtml must be
  # ENABLED here (it is disabled only for the prefix boot), and MT5 returns
  # before its files finish landing, so the wait below matters.
  WINEDLLOVERRIDES="mscoree=" "$WINE_BIN" "${INSTALLER}" /auto >/dev/null 2>&1 || true

  # Wine 9+ unpacks the terminal noticeably slower than Wine 8 did, so allow a
  # generous window (10 minutes) before declaring the install failed.
  for _ in $(seq 1 120); do
    if find "${WINE_PREFIX}/drive_c" -iname 'terminal64.exe' 2>/dev/null | grep -q .; then
      break
    fi
    sleep 5
  done

  if find "${WINE_PREFIX}/drive_c" -iname 'terminal64.exe' 2>/dev/null | grep -q .; then
    touch "${DONE_MARKER}"
    status mt5 "MT5 terminal installed"
  else
    status failed "terminal64.exe was not produced; rerun install"
    exit 5
  fi
fi

# --------------------------------------------------------------------------- #
# 5. Python bridge
# --------------------------------------------------------------------------- #
# IMPORTANT: the MetaTrader5 PyPI package publishes ONLY win_amd64 wheels (see
# https://pypi.org/pypi/MetaTrader5/json — every file is *-win_amd64.whl). It is
# impossible to `pip install MetaTrader5` into the sandbox's LINUX python; pip
# reports "No matching distribution found". The bridge must therefore run on a
# WINDOWS python living inside this Wine prefix, which is exactly why the task
# calls for MT5 "inside wine".
#
# So we install Windows Python into the prefix, then install MetaTrader5 into
# THAT interpreter. mt5_cli.py detects it and re-executes itself through
# `wine python.exe` for every broker call.
WIN_PY_DIR="${WIN_PY_DIR:-${WINE_PREFIX}/drive_c/Python311}"
WIN_PY="${WIN_PY_DIR}/python.exe"

# IMPORTANT: python.org's *installer* (.exe) silently no-ops under Wine — it
# exits 0 but creates nothing. The **embeddable** zip is a plain archive with no
# installer, so it works reliably in Wine. It ships a ``python3XX._pth`` that
# disables site-packages by default, so we re-enable it before running get-pip.
if [ ! -f "${WIN_PY}" ]; then
  status winpython "installing embeddable Windows Python ${MT5_WINPY_VERSION} ..."
  WINPY_ZIP="${MT5_ROOT}/python-embed.zip"
  if [ ! -s "${WINPY_ZIP}" ]; then
    curl -fsSL --retry 3 --max-time 900 -o "${WINPY_ZIP}" \
      "https://www.python.org/ftp/python/${MT5_WINPY_VERSION}/python-${MT5_WINPY_VERSION}-embed-amd64.zip" \
      || { log "FATAL: could not download embeddable Windows Python"; exit 7; }
  fi
  mkdir -p "${WIN_PY_DIR}"
  if command -v unzip >/dev/null 2>&1; then
    unzip -o -q "${WINPY_ZIP}" -d "${WIN_PY_DIR}" || true
  else
    python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" \
      "${WINPY_ZIP}" "${WIN_PY_DIR}" || true
  fi

  # The embeddable build disables site imports via ``python3XX._pth``. Enable
  # them, otherwise pip's installed packages are invisible to the interpreter.
  PY_TAG=$(printf '%s' "${MT5_WINPY_VERSION}" | awk -F. '{printf "%s%s", $1, $2}')
  PTH_FILE="${WIN_PY_DIR}/python${PY_TAG}._pth"
  if [ -f "${PTH_FILE}" ]; then
    sed -i 's/^#\s*import site/import site/' "${PTH_FILE}" || true
  fi
fi

if [ -f "${WIN_PY}" ]; then
  status bridge "installing pip + the MetaTrader5 bridge inside Wine ..."
  # Bootstrap pip (the embeddable zip has none) then install the bridge.
  if ! "$WINE_BIN" "${WIN_PY}" -m pip --version >/dev/null 2>&1; then
    GETPIP="${MT5_ROOT}/get-pip.py"
    [ -s "${GETPIP}" ] || curl -fsSL --retry 3 -o "${GETPIP}" https://bootstrap.pypa.io/get-pip.py || true
    "$WINE_BIN" "${WIN_PY}" "${GETPIP}" --no-warn-script-location >/dev/null 2>&1 \
      || log "WARN: pip bootstrap failed"
  fi
  "$WINE_BIN" "${WIN_PY}" -m pip install --no-input --disable-pip-version-check \
      MetaTrader5 pandas numpy >/dev/null 2>&1 \
    || log "WARN: MetaTrader5 install inside Wine failed"
else
  log "WARN: Windows Python was not installed; the MT5 bridge is unavailable"
fi

status done "install complete: prefix=${WINE_PREFIX} win_python=${WIN_PY}"
printf '{"ok": true, "stage": "done", "wine_prefix": "%s", "mt5_root": "%s", "windows_python": "%s"}\n' \
  "${WINE_PREFIX}" "${MT5_ROOT}" "${WIN_PY}"