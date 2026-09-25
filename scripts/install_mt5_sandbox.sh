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
# The BROKER's own MT5 installer, not MetaQuotes' generic one.
#
# MEASURED FAILURE (2026-09-22, Runloop devbox, Wine 10.0, MT5 build 6207):
# the generic MetaQuotes terminal CANNOT log in to a broker at all. Its
# ``Config/servers.dat`` contains only
#   "Copyright 2000-2026, MetaQuotes Ltd."  +  "Servers"
# i.e. no server list. MT5 resolves a broker server name (``Exness-MT5Trial9``)
# to a host through ``servers.dat``/``dnsperf.dat``; with no entry the name is
# unresolvable and the terminal silently SKIPS the connection instead of failing.
#
# The symptom is uniquely confusing, which is why this comment is long:
#   * the terminal starts fine, loads our /config: file
#     ("successfully initialized from start config") and reports
#     "launched with C:\mt5cfg\powerx.ini"  -- so everything LOOKS right;
#   * the log contains ZERO ``Network`` lines. A working login writes three
#     ("authorized on <server>", "terminal synchronized ...", "trading has been
#     enabled"). Their absence -- not an error -- is the only tell.
#   * the Python bridge then fails with ``(-10005, 'IPC timeout')``, because the
#     terminal never authorizes and so never exposes an account over IPC. That
#     error points at Wine/IPC and sends you debugging the wrong layer.
#
# A broker-branded installer embeds that broker's server list in servers.dat
# (verified: the Exness build ships a 234 KB servers.dat vs the generic 50 KB,
# and logged in first try). So the terminal a deployment installs MUST match the
# broker it will trade on.
#
# ``MT5_INSTALLER_URL`` stays as the fallback for a generic/MetaQuotes demo
# account (MetaQuotes-Demo resolves itself); ``MT5_BROKER_INSTALLER_URL`` is for
# a real broker. Broker CDN slugs are ``<company>.ltd``-style -- Exness is
# ``exness.technologies.ltd`` -> exness5setup.exe.
#
# The BROKER build is the DEFAULT, not the generic one. A bare install used to
# produce the generic MetaQuotes terminal, which carries no broker server list and
# so can never log in to a real broker -- silently, with zero ``Network`` lines
# and a ``-10005 IPC timeout`` from the bridge that blames Wine. Defaulting to the
# broker build means an install with no arguments is a *working* install. Exness
# is this deployment's broker; override MT5_BROKER_INSTALLER_URL for another one.
#
# INSTALL FOR THE RIGHT BROKER FIRST. ``MT5_BROKER_KEY`` records which build landed
# (read back by mt5_cli.py's preflight), and ``MT5_BROKER_SERVER`` is the server the
# account lives on -- mt5_cli.py turns that into the URL + directory name, so
# ``install --server <server>`` is broker-agnostic and the first install is the
# correct one. Unset, this deployment's default build is installed and a mismatched
# login is refused up front with the install to run instead (never a silent hang).
MT5_INSTALLER_URL="${MT5_INSTALLER_URL:-https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe}"

# A NAMED server with no build to match it is a STOP, not a default.
#
# The defaults above are this deployment's own choice of broker, which is the right
# answer to "install MT5" and the wrong answer to "install MT5 for AXI-Live": it
# lays down a terminal that carries only Exness's servers, cannot resolve that name
# at all, and reports a healthy install while doing it. mt5_cli.py already refuses
# that combination before it gets here (``install --server <unknown>``); this guard
# is the same rule at the layer that actually downloads, so a hand-run script
# cannot slip past it.
# Every read is ``:-``-guarded: this script runs under ``set -u``, and neither
# variable is guaranteed to exist this early -- an unguarded read would kill the
# installer here with "unbound variable" instead of with the reason.
if [ -n "${MT5_BROKER_SERVER:-}" ] \
   && [ -z "${MT5_BROKER_INSTALLER_URL:-}" ] \
   && [ "${MT5_GENERIC_INSTALLER:-0}" != "1" ]; then
  echo "[mt5-install] FATAL: MT5_BROKER_SERVER=${MT5_BROKER_SERVER} names a broker" >&2
  echo "[mt5-install] but MT5_BROKER_INSTALLER_URL is empty, so this run would" >&2
  echo "[mt5-install] install the default build, which cannot resolve that server." >&2
  echo "[mt5-install] Pass MT5_BROKER_INSTALLER_URL (and MT5_BROKER_DIR_NAME) for" >&2
  echo "[mt5-install] that broker, or MT5_GENERIC_INSTALLER=1 for MetaQuotes." >&2
  exit 64
fi
MT5_BROKER_INSTALLER_URL="${MT5_BROKER_INSTALLER_URL:-https://download.mql5.com/cdn/web/exness.technologies.ltd/mt5/exness5setup.exe}"
# Explicit opt-in to the generic MetaQuotes build. Only a MetaQuotes-Demo account
# can use it; a real broker cannot log in on it at all.
if [ "${MT5_GENERIC_INSTALLER:-0}" = "1" ]; then
  MT5_BROKER_INSTALLER_URL=""
fi
# Where the broker's install lands. The branded installer always uses
# "MetaTrader 5 <BRAND>" as the directory name and refuses to overwrite a generic
# install, so the two can coexist and the terminal finder must know both names.
MT5_BROKER_DIR_NAME="${MT5_BROKER_DIR_NAME:-MetaTrader 5 EXNESS}"

# MetaTrader5's PyPI wheels are Windows-only, so the bridge needs a Windows
# python inside the Wine prefix (see section 5 below).
MT5_WINPY_VERSION="${MT5_WINPY_VERSION:-3.11.9}"
# Wine-Gecko release used by the MT5 web installer. Space-separated fallbacks are
# attempted in order; the default matches WineHQ stable's own pairing closely
# enough that the embedded browser works.
MT5_GECKO_VERSION="${MT5_GECKO_VERSION:-2.47.4 2.47.3}"
# The Wine series MT5 requires. Wine 11 trips MetaTrader's anti-debug check.
MT5_WINE_SERIES="${MT5_WINE_SERIES:-10}"

# --------------------------------------------------------------------------- #
# 0. Self-timing
# --------------------------------------------------------------------------- #
# Every line this script logs carries the time elapsed since it started:
#
#   [mt5-install] [+0s]     installer started
#   [mt5-install] [+3s]     sandbox memory: 3931 MB (>= 1800 MB required)
#   [mt5-install] [+186s]   installing WineHQ stable 10 ...
#
# WHY THIS EXISTS, given the install already logs every step it takes: the log
# said WHAT was happening and never HOW LONG, so "the install takes 13 minutes"
# was not decomposable. The 13 minutes could sit in apt, in the Wine prefix boot,
# in the 736 MB MT5 download, or in the first terminal launch that materialises
# the MQL5 library -- and there was no way to tell which without re-running the
# whole thing under an external stopwatch. MEASURED 2026-09-24 on the local
# box: the Wine apt step is 34 s of a 3.5-minute install, so the Wine step is
# NOT the bottleneck and optimizing it would have been wasted work. Two numbers
# per line turn the log into its own profile: the difference between consecutive
# lines is the duration of the step between them, which is what a caller needs in
# order to attack the slow part instead of guessing at it.
#
# ``SECONDS`` is a bash builtin counting whole seconds since THIS shell started.
# The script is executed, never sourced, so that is the start of the install --
# no ``date`` fork per log line (this fires ~40 times) and no dependency on
# ``date +%s.%N``, which busybox and older BSD ``date`` do not support.
_elapsed() {
  local s="${SECONDS:-0}"
  if [ "${s}" -ge 60 ]; then
    printf '+%dm%02ds' "$(( s / 60 ))" "$(( s % 60 ))"
  else
    printf '+%ds' "${s}"
  fi
}

log() { printf '[mt5-install] [%s] %s\n' "$(_elapsed)" "$*" >&2; }

# Download a URL to a file, retrying on ANY failure.
#
# WHY THIS IS NOT JUST `curl --retry 3`: curl's --retry only retries *transient
# transport* failures (connect timeouts, resets, 5xx). It does NOT retry on an
# HTTP 4xx — and the MQL5 CDN intermittently answers 404 for a URL that returns
# 200 moments later. One such blip therefore aborted the entire install with
# "could not download the MT5 installer", which reads like a network block but is
# pure CDN flakiness (reproduced on both Runloop and Novita, 2026-09-22).
#
# The size floor matters too: curl -f still leaves a truncated file behind on a
# dropped connection, and a CDN error page is a valid 200. Requiring >= min_bytes
# rejects both, so a "successful" download is always a plausible binary.
fetch_url() {
  local url="$1" dest="$2" min_bytes="${3:-1000000}"
  local tries="${MT5_DOWNLOAD_TRIES:-5}" delay="${MT5_DOWNLOAD_BACKOFF:-5}"
  local i=1 size=0 code=""

  while [ "${i}" -le "${tries}" ]; do
    rm -f "${dest}"

    # Primary: curl. Secondary: wget — a different TLS/HTTP stack, so a curl-only
    # failure (stale keep-alive connection, TLS hiccup) can still succeed here.
    #
    # The HTTP code is captured so a DEFINITIVE client error (404/403/410) can be
    # told apart from a transient one. A wrong URL never becomes right, so
    # retrying it just delays the diagnosis; a 5xx or a connection reset very
    # often does clear on the next attempt.
    #
    # `code` is normalised to 3 digits: curl already prints `000` when it cannot
    # connect, so appending another would yield `000000`. Take the LAST 3 chars.
    code="$(curl -sSL --connect-timeout 20 --max-time 600 -o "${dest}" \
      -w '%{http_code}' "${url}" 2>/dev/null)"
    code="${code: -3}"
    [ -n "${code}" ] || code="000"

    case "${code}" in
      2*)
        size="$(stat -c %s "${dest}" 2>/dev/null || echo 0)"
        if [ "${size}" -ge "${min_bytes}" ]; then
          return 0
        fi
        log "WARN: ${url} returned only ${size} bytes (< ${min_bytes}); retrying"
        ;;
      404|403|410)
        # Not retryable: the resource is not there. Surface the URL so a typo (a
        # dropped TLD in a broker slug is the common case) is immediately visible.
        log "FATAL: ${url} returned HTTP ${code} — the URL is wrong, not slow. "\
"Check the broker installer slug; retrying cannot fix a 404."
        rm -f "${dest}"
        return 2
        ;;
      000)
        log "WARN: could not connect to ${url} (attempt ${i}/${tries})"
        ;;
      *)
        log "WARN: ${url} returned HTTP ${code} (attempt ${i}/${tries})"
        ;;
    esac

    if wget -q --timeout=60 --tries=1 -O "${dest}" "${url}" 2>/dev/null; then
      size="$(stat -c %s "${dest}" 2>/dev/null || echo 0)"
      if [ "${size}" -ge "${min_bytes}" ]; then
        return 0
      fi
    fi

    log "WARN: download attempt ${i}/${tries} failed for ${url}"
    rm -f "${dest}"
    if [ "${i}" -lt "${tries}" ]; then
      sleep "${delay}"
    fi
    i=$((i + 1))
  done

  return 1
}

# Bump the prefix if it was built by a Wine that MT5 refuses (>= 11).
# MetaTrader's anti-debug check fires on Wine 11's prefix, and Wine 10 cannot
# read an 11-built prefix, so the only reliable path is a rebuild.
maybe_reset_prefix_for_wine10() {
  local ver_file="${WINE_PREFIX}/.wine-version-built"
  if [ -d "${WINE_PREFIX}/drive_c" ]; then
    local built=""
    [ -r "${ver_file}" ] && built="$(cat "${ver_file}" 2>/dev/null || true)"
    if [ "${built}" != "" ] && [ "${built}" != "${MT5_WINE_SERIES}" ]; then
      status wine "rebuilding the prefix (built with wine ${built}, MT5 needs ${MT5_WINE_SERIES})"
      rm -rf "${WINE_PREFIX}"
    fi
  fi
}

# Run a MT5 binary.
#
# WHY WINEDEBUG IS STRIPPED HERE: Wine sets PEB heap-debug flags whenever
# WINEDEBUG is present in the environment — even WINEDEBUG=-all. MetaTrader reads
# those flags as "a debugger is attached" and refuses to start with
# "A debugger has been found running in your system." Winning combination,
# verified in the sandbox: Wine 10 + WINEDEBUG unset.
run_mt5() {
  env -u WINEDEBUG -u WINEINVALIDATECACHE "$WINE_BIN" "$@"
}

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
# MT5's installer and terminal refuse to run correctly under Wine's default
# "Windows 7" reporting — MetaQuotes has required Windows 10 for years. This is
# set in the registry below, before any MT5 binary is executed.
MT5_WINVER="${MT5_WINVER:-win10}"

# Any uncaught error is recorded as a terminal stage so a poller sees a definite
# failure instead of waiting forever on a stale "in progress" marker.
_on_error() {
  # ORDER MATTERS, and only the first two lines may come first: ``$?`` is the
  # status that tripped the trap and ``$BASH_COMMAND`` is the command that was
  # running when it did, and BASH_COMMAND reflects whatever command is executing
  # *now* -- so anything that runs before these reads overwrites the evidence.
  local rc=$? cmd="${BASH_COMMAND}"
  local line="${BASH_LINENO[0]}"

  # WHERE it died, not just that it did.
  #
  # MEASURED 2026-09-24: a fresh install into an empty prefix failed at +2m52s
  # with ``installer exited with code 1`` and NOTHING else -- the exit code, the
  # status file and the log's last line ("running the silent MT5 install") all
  # agreed that something had gone wrong and none of them could say what. A
  # 3-minute install had to be diagnosed by re-running it under ``set -x``
  # because the trap threw away the two things bash hands it for free. MT5's own
  # output went to mt5setup.log, the prefix was intact, and the terminal
  # directory held MetaEditor64.exe and metatester64.exe but no terminal64.exe --
  # every one of those facts is consistent with a stall, a kill, and an
  # unrelated command returning 1.
  #
  # The elapsed time belongs on the failure too: a box that died at +00:12s and a
  # box that died at +12m30s have entirely different causes (a missing dependency
  # versus a step that timed out), and the exit code alone does not tell them
  # apart.
  printf 'failed|installer exited with code %s after %s (line %s: %s)\n' \
    "$rc" "$(_elapsed)" "${line}" "${cmd}" >"${MT5_ROOT}/install.status" 2>/dev/null || true
  log "installer exited with code ${rc} after $(_elapsed) at line ${line}: ${cmd}"
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

#: Refresh the apt indexes AT MOST ONCE per install.
#
# MEASURED 2026-09-24: a fresh install calls ``apt_install`` four times -- the
# Wine stack, the base tools, the GL/Vulkan loaders and the window manager -- and
# every call ran its own ``apt-get update``. That is four full index refreshes
# against the same mirrors, and every one of them is requests this install then
# has to wait for: the boxes this runs on are sandboxes, where the cost is
# round-trip latency, not bandwidth. Downloading and parsing nothing new four
# times is time taken straight off an install that is already minutes long.
#
# Once is enough. Nothing in this script needs an index fresher than the one the
# install already fetched. ``install_winehq`` is the one deliberate exception and
# passes ``force``: it ADDS a repository, so its index genuinely does not exist
# yet and reusing the cached one would leave the Wine packages unfindable.
#
# The return value is apt-get's own, so a caller that treats a failed refresh as
# fatal still can (``install_winehq``), while a caller that can proceed on a
# cached index still does (``apt_install``).
_APT_REFRESHED=0
apt_refresh() {
  if [ "${1:-}" != "force" ] && [ "${_APT_REFRESHED}" = "1" ]; then
    return 0
  fi
  _APT_REFRESHED=1
  $SUDO apt-get update -qq >/dev/null 2>&1
}

apt_install() {
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt_refresh || true
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
# points the bridge uses), so we install WineHQ's Wine 10 build. The upper bound
# matters just as much as the lower one — see the Wine 11 note below.
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
  # FORCED, not cached: the WineHQ sources file was just added, so its index does
  # not exist yet and the cached one cannot name any wine package. This is also
  # the refresh every later ``apt_install`` reuses, which is why the base tools,
  # the GL/Vulkan loaders and the window manager no longer fetch their own.
  apt_refresh force || return 1

  # WHY WINE 10 AND NOT THE LATEST:
  #
  # Wine 11 sets PEB heap-debug flags that MetaTrader reads as "a debugger is
  # attached". mt5setup.exe then refuses to run at all and pops a modal dialog
  # reading "A debugger has been found running in your system. Please, unload it
  # from memory and restart your program." — captured from a screenshot inside
  # the sandbox. The install stops before making a single network request, so it
  # looks like a silent hang (0% CPU, no terminal64.exe) rather than an error.
  #
  # Wine 10.0 is not affected. Measured in the sandbox: with Wine 11 the install
  # never produced a terminal; with Wine 10.0 pinned, terminal64.exe and
  # MetaEditor64.exe appeared in ~30 seconds.
  local pin
  pin="$(resolve_wine10_version)"
  if [ -z "${pin}" ]; then
    log "WARN: no Wine 10 build found in the repo; MT5 may refuse to install"
    return 1
  fi
  log "pinning Wine ${pin}"
  # ALL FOUR packages must be pinned together. Pinning only the winehq-stable
  # metapackage leaves wine-stable/amd64/i386 at 11.0, so `wine --version` still
  # reports 11.0 and MT5's anti-debug check still fires. Verified in the sandbox:
  # the 4-package form downgrades cleanly and reports wine-10.0.
  if $SUDO apt-get install -y -qq --allow-downgrades --install-recommends \
      "winehq-stable=${pin}" "wine-stable=${pin}" \
      "wine-stable-amd64=${pin}" "wine-stable-i386=${pin}" >/dev/null 2>&1; then
    return 0
  fi
  # Fall back to the metapackage alone (some distros do not ship the split
  # packages), then give up rather than silently installing Wine 11 — which
  # would reintroduce exactly the anti-debug failure this function prevents.
  $SUDO apt-get install -y -qq --allow-downgrades --install-recommends \
      "winehq-stable=${pin}" >/dev/null 2>&1 && return 0
  return 1
}

# Discover the Wine 10 apt version for THIS distro.
#
# The version string embeds the distro codename (Debian: "10.0.0.0~bookworm-1",
# Ubuntu: "10.0.0.0~jammy-1"), so a hardcoded pin silently fails to match on any
# other distro — and because it then falls back to the default candidate the box
# would quietly end up on Wine 11, reintroducing MT5's anti-debug abort. Ask apt
# for the actual 10.x candidate instead, and only fall back to a computed guess.
resolve_wine10_version() {
  # Explicit override wins (lets an operator pin a different build).
  if [ -n "${MT5_WINE_VERSION:-}" ]; then
    printf '%s' "${MT5_WINE_VERSION}"
    return 0
  fi

  local found
  found="$(apt-cache madison winehq-stable 2>/dev/null \
    | awk -F'|' '{gsub(/^[ \t]+|[ \t]+$/, "", $2); print $2}' \
    | grep -E '^10\.' | head -1)"
  if [ -n "${found}" ]; then
    printf '%s' "${found}"
    return 0
  fi

  # apt has no candidate (repo not reachable / already removed): compute the
  # conventional Debian-style version from this distro's own codename.
  local codename="bookworm"
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    codename="${VERSION_CODENAME:-bookworm}"
  fi
  printf '10.0.0.0~%s-1' "${codename}"
}

# Report the Wine major version from whichever launcher actually runs.
#
# ``wine --version`` is used first for parity with the rest of the file, but on a
# kernel without IA32 support that binary cannot execute at all (see the
# WINE_BIN selection below), so wine64 is consulted as a fallback. Without this
# the version read as 0 and the script concluded Wine was missing even when a
# perfectly good WineHQ 10 was installed — sending it into a pointless
# reinstall.
wine_major() {
  local out major bin
  for bin in wine wine64; do
    command -v "$bin" >/dev/null 2>&1 || continue
    out=$(env -u WINEDEBUG timeout 60 "$bin" --version 2>/dev/null) || continue
    major=$(printf '%s' "$out" | sed 's/[^0-9]*\([0-9]*\).*/\1/')
    if [ -n "$major" ] && [ "$major" != "0" ]; then
      printf '%s' "$major"
      return 0
    fi
  done
  printf '0'
}

WINE_MAJOR=$(wine_major)

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

# Wine 10 specifically is required — see install_winehq() above. Wine 11 trips
# MetaTrader's anti-debug check and nothing installs. A Wine 11 prefix is also not
# readable by Wine 10, so an existing too-new prefix is rebuilt below.
if [ "${WINE_MAJOR:-0}" -lt 9 ] || [ "${WINE_MAJOR:-0}" -ge 11 ]; then
  if [ "${WINE_MAJOR:-0}" -eq 0 ]; then
    status wine "installing WineHQ stable 10 (required by the MT5 installer) ..."
  else
    status wine "wine ${WINE_MAJOR} is unusable for MT5; installing WineHQ 10 ..."
  fi
  install_winehq || log "WARN: WineHQ install failed"
  # apt may have swapped the binaries; re-read the version so the prefix rebuild
  # below (and the doctor report) sees the version actually in place.
  WINE_MAJOR=$(wine_major)
  if [ "${WINE_MAJOR:-0}" -ge 11 ]; then
    log "WARN: wine is still ${WINE_MAJOR} after the WineHQ install; MT5 may refuse to run"
  fi
fi

# A prefix built by a different Wine series is not reliably readable. Discard a
# Wine-11 prefix so Wine 10 builds a clean one (this is what makes terminal64.exe
# appear — a stale 11-built prefix keeps tripping the anti-debug check).
maybe_reset_prefix_for_wine10

if ! command -v wine >/dev/null 2>&1 && ! command -v wine64 >/dev/null 2>&1; then
  status wine "installing distro wine ..."
  apt_install wine64 wine32 wine || apt_install wine || true
fi

# Pick a Wine launcher that actually EXECUTES on this kernel.
#
# MEASURED FAILURE (2026-09-22, Runloop devbox, Debian 12, WineHQ 10.0):
# ``/usr/bin/wine`` is a 32-bit ELF. This kernel has no IA32 emulation
# (``/proc/sys/abi/ldt16`` is absent and ``ia32`` is missing from
# /proc/cpuinfo), so the 32-bit loader itself fails:
#
#   /usr/bin/wine: cannot execute binary file: Exec format error
#   /lib/ld-linux.so.2: cannot execute binary file: Exec format error
#
# ``command -v wine`` happily returned 0 for that dead binary, so the script
# selected it, ``wineboot`` died immediately, and the ERR trap reported
# ``failed|installer exited with code 2`` with the log's last line still reading
# "initialising wine prefix ..." — a misdiagnosis that looks like an OOM or a
# hung prefix rather than a broken launcher.
#
# So probe the candidate instead of trusting ``which``. A working ``wine``
# (i.e. every normal host) passes on the first try and is used unchanged;
# otherwise fall through to 64-bit wine64, which is sufficient because
# mt5setup.exe, the embeddable Windows python and the MetaTrader5 win_amd64
# wheels are all 64-bit (mt5setup.exe's PE header reports machine 0x8664).
_wine_works() {
  local bin="$1"
  command -v "$bin" >/dev/null 2>&1 || return 1
  env -u WINEDEBUG timeout 60 "$bin" --version >/dev/null 2>&1
}

WINE_BIN=""
for _cand in wine wine64; do
  if _wine_works "$_cand"; then
    WINE_BIN="$_cand"
    break
  fi
done

if [ -z "${WINE_BIN}" ]; then
  # Last resort: name a real binary so the doctor report shows something, and say
  # plainly that it does not execute.
  if command -v wine64 >/dev/null 2>&1; then
    WINE_BIN=wine64
  elif command -v wine >/dev/null 2>&1; then
    WINE_BIN=wine
  else
    log "FATAL: wine is not available after install"
    exit 3
  fi
  log "WARN: ${WINE_BIN} is present but does not execute on this kernel"
fi
if [ "${WINE_BIN}" != "wine" ]; then
  log "using ${WINE_BIN} (the 'wine' launcher does not run here)"
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
# 2b. winbindd — required for the terminal's IPC
# --------------------------------------------------------------------------- #
# The package alone is NOT enough. Wine's named-pipe implementation for the
# MetaTrader terminal goes through winbind, and installing ``winbind`` only drops
# the binaries on disk — nothing starts the daemon. Without it the terminal still
# boots and logs in, but the Python bridge cannot attach:
#
#   mt5.initialize() -> False (-10005, 'IPC timeout')
#
# which reads as a broken bridge and sends you debugging IPC/Wine instead of a
# missing daemon. Verified 2026-09-22: starting winbindd made the bridge attach
# immediately on the same running terminal.
#
# Best-effort by design: it must never fail an otherwise-good install, and on a
# host where winbindd is already up this is a no-op.
if command -v winbindd >/dev/null 2>&1 || [ -x /usr/sbin/winbindd ]; then
  if ! pgrep -x winbindd >/dev/null 2>&1; then
    status winbind "starting winbindd (required for MT5 terminal IPC)"
    # A minimal smb.conf keeps winbindd from refusing to start on a box that has
    # never been joined to a domain.
    if [ ! -s /etc/samba/smb.conf ]; then
      $SUDO mkdir -p /etc/samba 2>/dev/null || true
      printf '[global]\n\tworkgroup = WORKGROUP\n\tsecurity = guest\n\twinbind use default domain = yes\n' \
        | $SUDO tee /etc/samba/smb.conf >/dev/null 2>&1 || true
    fi
    $SUDO /usr/sbin/winbindd -D >/dev/null 2>&1 || \
      $SUDO winbindd -D >/dev/null 2>&1 || true
    sleep 2
    if pgrep -x winbindd >/dev/null 2>&1; then
      log "winbindd running (MT5 IPC available)"
    else
      log "WARN: winbindd did not start; the MT5 bridge may report IPC timeout"
    fi
  fi
fi

# --------------------------------------------------------------------------- #
# 3. Wine prefix
# --------------------------------------------------------------------------- #
export WINEPREFIX="${WINE_PREFIX}"
export WINEARCH=win64
# NOTE: WINEDEBUG is intentionally NOT set anywhere in this script.
#
# Wine raises PEB heap-debug flags for a process whenever WINEDEBUG exists in its
# environment — even the seemingly harmless WINEDEBUG=-all. MetaTrader inspects
# those flags and, finding them, refuses to run: "A debugger has been found
# running in your system." Setting WINEDEBUG to anything therefore breaks the
# install. MT5 binaries are launched through run_mt5(), which strips it.
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

# --------------------------------------------------------------------------- #
# 3a. A window manager for the virtual display
# --------------------------------------------------------------------------- #
# Xvfb alone provides no window manager. Without one, Wine's dialogs are not
# properly mapped/adopted by the X server, and MT5's installer windows can hang
# unmapped. A tiny WM makes the display behave like a real desktop.
if ! pgrep -x matchbox-window-manager >/dev/null 2>&1; then
  apt_install matchbox-window-manager >/dev/null 2>&1 || true
  if command -v matchbox-window-manager >/dev/null 2>&1; then
    status display "starting a window manager on :${DISPLAY_NUM}"
    nohup matchbox-window-manager -use_titlebar no >/dev/null 2>&1 &
    sleep 2
  fi
fi

# --------------------------------------------------------------------------- #
# 3b. Report Windows 10
# --------------------------------------------------------------------------- #
if [ ! -d "${WINE_PREFIX}/drive_c" ]; then
  status wineprefix "initialising wine prefix at ${WINE_PREFIX} (this takes minutes)"
  mkdir -p "${WINE_PREFIX}"
  # wineboot can return non-zero on first run in headless containers and its
  # setupapi phase is slow and occasionally wedges, so it is bounded. A partial
  # prefix is still usable — wineboot finishes the remaining work lazily.
  timeout 900 "$WINE_BIN" wineboot --init >/dev/null 2>&1 || true
  sleep 5
  # Stamp the series that built this prefix so a later version change (e.g. an
  # image upgrade to Wine 11) triggers an automatic rebuild instead of a
  # mysterious anti-debug failure.
  printf '%s' "${MT5_WINE_SERIES}" >"${WINE_PREFIX}/.wine-version-built" 2>/dev/null || true
fi

# Wine defaults to reporting itself as Windows 7. MetaQuotes has required
# Windows 10 for years and mt5setup.exe refuses to proceed under the older
# version string — it raises a hard error and shows a modal dialog that nothing
# can dismiss, which is exactly the "0% CPU, nothing produced" hang. This must
# be corrected before ANY MT5 binary runs, i.e. only after the prefix exists.
# GUARDED, and this is not defensive noise: MEASURED 2026-09-24 on a fresh prefix,
# this one line killed an install outright. Under ``set -e`` a bare
# ``var=$(command)`` assignment inherits the command's exit status, and this
# pipeline exits non-zero whenever ``reg query`` cannot answer -- which a fresh
# prefix does while wineboot is still settling it. The install aborted at +28s
# with ``installer exited with code 53``, the log's last line still reading
# "initialising wine prefix", and the prefix was perfectly usable: the ONE
# question ``reg query`` could not answer yet was "does this prefix already
# report Windows 10". An empty answer is not an error here -- it just means the
# registry writes below are needed, which is exactly what the ``!=`` test does
# with it.
winver_current=$(timeout 60 "$WINE_BIN" reg query \
  'HKLM\Software\Microsoft\Windows NT\CurrentVersion' /v CurrentVersion 2>/dev/null \
  | tr -d '\r' | awk '/CurrentVersion/{print $3}' || true)
if [ "${winver_current}" != "10.0" ]; then
  status winecfg "configuring the prefix to report Windows 10"
  timeout 120 "$WINE_BIN" reg add \
    'HKLM\Software\Microsoft\Windows NT\CurrentVersion' /v CurrentVersion /t REG_SZ \
    /d 10.0 /f >/dev/null 2>&1 || true
  timeout 120 "$WINE_BIN" reg add \
    'HKLM\Software\Microsoft\Windows NT\CurrentVersion' /v CurrentBuildNumber /t REG_SZ \
    /d 19045 /f >/dev/null 2>&1 || true
fi

# MT5 links against the MSVC runtime; without it the installer aborts.
if ! ls "${WINE_PREFIX}/drive_c/windows/system32/msvcp140.dll" >/dev/null 2>&1; then
  status vcrun "installing the MSVC runtime (vcrun2022) ..."
  apt_install winetricks >/dev/null 2>&1 || true
  if command -v winetricks >/dev/null 2>&1; then
    timeout 600 winetricks -q --force vcrun2022 >/dev/null 2>&1 || \
      log "WARN: vcrun2022 install failed; continuing"
  fi
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
# Install the BROKER's terminal when one is configured, else the generic build.
#
# See the MT5_BROKER_INSTALLER_URL note at the top of this file: the generic
# MetaQuotes terminal carries no broker server list, cannot resolve a broker
# server name, and therefore never authorizes — silently, with no Network log
# line and an IPC timeout from the bridge. A broker-branded installer embeds
# that broker's servers and logs in first try.
mkdir -p "${MT5_ROOT}"
DONE_MARKER="${MT5_ROOT}/.installed"

if [ -n "${MT5_BROKER_INSTALLER_URL}" ]; then
  INSTALLER="${MT5_ROOT}/broker_setup.exe"
  # Distinct marker: switching broker (or to generic) must re-run the install
  # rather than short-circuit on a marker written by a different terminal.
  DONE_MARKER="${MT5_ROOT}/.installed.broker"
  TERM_DISPLAY_NAME="${MT5_BROKER_DIR_NAME}"
else
  INSTALLER="${MT5_ROOT}/mt5setup.exe"
  TERM_DISPLAY_NAME="MetaTrader 5"
fi

# WHAT THE MARKER MEANS: "the terminal for THIS url is installed", not "some
# terminal is installed". Builds coexist in one prefix (a branded installer refuses
# to overwrite another broker's), so a marker that only says "done" short-circuits a
# legitimate broker switch and leaves the OLD terminal in place -- which then cannot
# resolve the new broker's server names and fails silently at login.
_RESOLVED_URL="${MT5_BROKER_INSTALLER_URL:-${MT5_INSTALLER_URL}}"
_PREV_URL="$(cat "${MT5_ROOT}/.installed.url" 2>/dev/null || true)"
# AND THE RECORD MUST BE BACKED BY THE DISK.
#
# MEASURED FAILURE (2026-09-25, live): the marker + URL pair was written by a run that
# reported ``done`` and installed nothing, so the NEXT install short-circuited on it --
# skipping the download, the installer and the directory entirely -- and the script's
# own last line still printed ``status done "install complete"``. A record of an
# install that failed is not evidence that the next one may be skipped.
#
# The directory the install RECORDED is the one to check, and it is also adopted as
# this run's display name so the MQL5 step and the closing check use the directory
# that actually holds the terminal.
_PREV_DIR_NAME="$(cat "${MT5_ROOT}/.installed.dir_name" 2>/dev/null || true)"
_PREV_TERMINAL_PRESENT=0
if [ -n "${_PREV_DIR_NAME}" ] \
   && [ -f "${WINE_PREFIX}/drive_c/Program Files/${_PREV_DIR_NAME}/terminal64.exe" ]; then
  _PREV_TERMINAL_PRESENT=1
fi
# The requested build's own directory counts too: a repeat install of a REGISTERED
# broker passes that broker's real directory name, which is how this stays a no-op
# for the common case.
_PREDICTED_TERMINAL="${WINE_PREFIX}/drive_c/Program Files/${MT5_BROKER_DIR_NAME}/terminal64.exe"
if [ -f "${DONE_MARKER}" ] && [ "${_PREV_URL}" = "${_RESOLVED_URL}" ] \
   && { [ "${_PREV_TERMINAL_PRESENT}" -eq 1 ] || [ -f "${_PREDICTED_TERMINAL}" ]; }; then
  _TERMINAL_ALREADY_INSTALLED=1
  if [ "${_PREV_TERMINAL_PRESENT}" -eq 1 ]; then
    TERM_DISPLAY_NAME="${_PREV_DIR_NAME}"
  fi
else
  _TERMINAL_ALREADY_INSTALLED=0
  # A different broker's installer is on disk under the same filename; keeping it
  # would install the wrong terminal while reporting success.
  if [ -n "${_PREV_URL}" ] && [ "${_PREV_URL}" != "${_RESOLVED_URL}" ]; then
    rm -f "${INSTALLER}"
  fi
fi

if [ "${_TERMINAL_ALREADY_INSTALLED}" -eq 0 ]; then
  if [ ! -s "${INSTALLER}" ]; then
    status download "downloading the MT5 installer ..."
    _dl_url="${MT5_BROKER_INSTALLER_URL:-${MT5_INSTALLER_URL}}"
    # MT5's installer is ~5 MB (branded) to ~24 MB (generic); require a plausible
    # size so a truncated/error-page "200" is not mistaken for the real thing.
    _dl_rc=0
    fetch_url "${_dl_url}" "${INSTALLER}" 1000000 || _dl_rc=$?
    case "${_dl_rc}" in
      0) : ;;
      2) status failed "broker installer URL is wrong (404): ${_dl_url} — check the broker slug (e.g. exness.technologies.ltd, not exness.technologies)"; exit 4 ;;
      *) status failed "could not download the MT5 installer from ${_dl_url}"; exit 4 ;;
    esac
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
        fetch_url "https://dl.winehq.org/wine/wine-gecko/${ver}/wine-gecko-${ver}-x86_64.msi" \
          "${GECKO_MSI}" 1000000 && break || rm -f "${GECKO_MSI}"
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
  # ENABLED here (it is disabled only for the prefix boot).
  #
  # The installer is BOUNDED on purpose. mt5setup.exe is a web installer: when it
  # cannot reach its download backend it opens a small dialog and simply sits
  # there — measured at 0% CPU for 20+ minutes with no terminal64.exe. Without a
  # timeout the whole install would hang forever and the poller would never see a
  # terminal state. Bounding it converts "hangs indefinitely" into a fast,
  # diagnosable failure with the installer's own output attached.
  MT5_SETUP_TIMEOUT="${MT5_SETUP_TIMEOUT:-900}"
  MT5_SETUP_LOG="${MT5_ROOT}/mt5setup.log"

  # ------------------------------------------------------------------ #
  # Stalled-download watchdog (the Novita hang)
  # ------------------------------------------------------------------ #
  # MEASURED (2026-09-22, Novita, Wine 10.0): bounding the installer is not
  # enough, because MT5's web installer does not FAIL -- it STOPS. It picks ONE
  # download mirror and, when that mirror completes the TCP handshake but never
  # moves data, Wine's downloader blocks in a plain ``read`` forever: 0% CPU,
  # zero bytes written into the prefix, and one ESTABLISHED socket with empty
  # queues in both directions. Measured wedged for 20 minutes; the only thing that
  # ended it was MT5_SETUP_TIMEOUT, after which the old code then waited ANOTHER
  # 300 s of unpack grace for a terminal that was never coming.
  #
  # This is NOT a DNS problem, and DNS fixes do not move it. Measured on that box:
  #   * the MQL5 CDN name resolves IPv6-first (``2a01:f680:21:1::85``) and the
  #     sandbox has no IPv6 egress at all; pinning the CDN names to an edge that
  #     answers in 0.7 s (``45.94.185.85``) in the prefix's own hosts file changed
  #     NOTHING -- the installer went straight back to the same wedged peer;
  #   * the wedged peer was ``148.113.1.241:443``, an address that appears in no
  #     DNS answer we could find, i.e. the mirror list is embedded in the
  #     installer and reached as a literal IP;
  #   * ``ip route add blackhole 148.113.1.241/32`` -- making that one peer
  #     unreachable -- let the SAME installer finish the entire 736 MB terminal in
  #     ~40 s, because it falls through to a mirror that works.
  # The sandbox's own network was never at fault: ``curl`` fetched the 5 MB
  # installer from the CDN in 0.68 s, three times running, while the installer sat
  # at zero bytes.
  #
  # So: watch for progress (prefix growth OR CPU time) and, when the installer
  # stalls, blackhole the peer it is wedged on and try again. Each attempt is then
  # bounded by MT5_STALL_SECONDS instead of by MT5_SETUP_TIMEOUT, and a stalled
  # attempt no longer collects the unpack grace -- an installer that was killed
  # having written nothing has failed NOW, and 300 more seconds cannot change it.
  #
  # INERT ON A HEALTHY BACKEND. Runloop's install completes in ~4 minutes and
  # never goes MT5_STALL_SECONDS without progress, so none of this runs there: the
  # first attempt is byte-for-byte the command that already ships.
  MT5_STALL_SECONDS="${MT5_STALL_SECONDS:-120}"
  MT5_INSTALL_ATTEMPTS="${MT5_INSTALL_ATTEMPTS:-3}"
  # Wine 9+ unpacks the terminal noticeably slower than Wine 8 did, so an
  # installer that exited on its own still gets a generous window -- but the window
  # is driven by GROWTH now, so it ends when the tree stops landing.
  MT5_UNPACK_GRACE="${MT5_UNPACK_GRACE:-300}"

  # Every process the installer spawned: the ``timeout`` wrapper, wine's
  # start.exe, and the ``main`` process that actually owns the sockets. Tracked by
  # DESCENT from our own background pid, deliberately NOT with
  # ``pgrep -f broker_setup.exe``: a name-based match also hits any shell whose
  # *command line* contains that name, and killing this script's own shell
  # mid-install is a distinctly unpleasant way to fail (reproduced while building
  # this fix).
  _descendants() {
    local frontier="$1" all="" next pid
    while [ -n "${frontier}" ]; do
      next=""
      for pid in ${frontier}; do
        next="${next} $(pgrep -P "${pid}" 2>/dev/null | tr '\n' ' ')"
      done
      all="${all} ${next}"
      frontier="${next}"
    done
    printf '%s' "${all}"
  }

  # CPU jiffies burned by the installer tree -- the companion signal to prefix
  # growth, so a download that is genuinely working is never mistaken for a stall.
  _installer_cpu() {
    local total=0 pid v
    for pid in $1; do
      [ -r "/proc/${pid}/stat" ] || continue
      v="$(awk '{print $14 + $15}' "/proc/${pid}/stat" 2>/dev/null || true)"
      [ -n "${v}" ] && total=$(( total + v ))
    done
    printf '%s' "${total}"
  }

  # Remote peers the installer tree holds a connection to, port stripped. The
  # sandbox's own service addresses are excluded so the platform's control
  # connection can never be blackholed.
  _installer_peers() {
    local pid out=""
    for pid in $1; do
      out="${out}$(ss -tnpH 2>/dev/null | grep -F "pid=${pid}," | awk '{print $5}' || true)"$'\n'
    done
    printf '%s\n' "${out}" \
      | sed 's/:[0-9]*$//' \
      | grep -E '^[0-9a-fA-F.:]+$' \
      | grep -vE '^(127\.|169\.254\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|192\.0\.2\.|::1$|fe80:)' \
      | sort -u
  }

  # Make one wedged peer unreachable so the installer falls through to another
  # mirror. Sandbox-local and ephemeral, and deliberately NOT undone: the peer is
  # by definition one this sandbox cannot download from.
  _blackhole_peer() {
    [ -n "$1" ] || return 1
    if ip route add blackhole "$1" 2>/dev/null; then
      log "blackholed the stalled download peer $1"
      return 0
    fi
    if sudo -n ip route add blackhole "$1" 2>/dev/null; then
      log "blackholed the stalled download peer $1 (sudo)"
      return 0
    fi
    return 1
  }

  # Wait for the terminal in the DIRECTORY THIS INSTALLER TARGETS, not just any
  # terminal64.exe anywhere in the prefix: the generic and branded builds coexist
  # ("MetaTrader 5" vs "MetaTrader 5 EXNESS"), so a bare ``find`` would match the
  # OTHER build's binary, conclude success, and hand the bridge a terminal that
  # cannot reach the broker — reintroducing exactly the silent no-authorization
  # failure this change fixes.
  #
  # MEASURED FAILURE (2026-09-25, live Novita box): the fallback DID fall back to a
  # bare ``find``, reintroducing exactly the failure the paragraph above warns
  # about. On a box that already carried Deriv's terminal, ``install --server
  # AXI-Live`` reported ``done`` and wrote AXI's URL into ``.installed.url`` while
  # only Deriv's terminal existed: the FIRST poll matched the PRE-EXISTING
  # terminal, so the AXI installer was reaped ~0 s after it started and the script
  # advanced to its own "MT5 terminal installed" line. The URL was right; the
  # reported outcome was not, and the next command found a terminal that could not
  # resolve the server.
  #
  # So "installed" now means a terminal THIS INSTALL produced -- one that was not on
  # disk before it ran -- or the directory the caller named. A terminal that
  # predates the install is evidence of nothing.
  _want_dir="${WINE_PREFIX}/drive_c/Program Files/${TERM_DISPLAY_NAME}"

  # WHICH terminals existed BEFORE the installer ran. Read back rather than assumed:
  # the installer's own choice of directory is the only reliable answer, which is
  # why the record it writes at the end is ``.installed.dir_name``.
  _TERMINALS_BEFORE="${MT5_ROOT}/.terminals.before"
  _list_terminals() {
    find "${WINE_PREFIX}/drive_c" -maxdepth 3 -iname 'terminal64.exe' 2>/dev/null | sort
  }
  _list_terminals > "${_TERMINALS_BEFORE}" 2>/dev/null || : > "${_TERMINALS_BEFORE}"

  # Terminals that appeared AFTER the snapshot: exactly the ones this install made.
  _new_terminals() {
    if [ ! -s "${_TERMINALS_BEFORE}" ]; then
      _list_terminals
      return 0
    fi
    _list_terminals | comm -13 "${_TERMINALS_BEFORE}" - 2>/dev/null || true
  }

  # Brand tokens mined out of the installer URL, so the directory the installer
  # ACTUALLY used can still be recognised when the name we were given is wrong.
  #
  # A discovery-derived install has no registry entry, so MT5_BROKER_DIR_NAME is a
  # GUESS ("MetaTrader 5 AXI" for slug ``axicorp.financial.services``); Exness's own
  # installer ignores the brand outright and lays down "MetaTrader 5 Terminal". No
  # caller-supplied name can be trusted to predict the directory.
  _brand_tokens() {
    printf '%s' "${_RESOLVED_URL}" \
      | sed -E 's#^[A-Za-z][A-Za-z0-9+.-]*://##; s#[?#].*$##' \
      | tr '/._-' '\n' \
      | tr 'A-Z' 'a-z' \
      | sed -E 's/[0-9]+setup$//; s/setup$//; s/^[0-9]+//' \
      | grep -E '^[a-z][a-z0-9]{2,}$' \
      | grep -vxE 'http|https|www|com|net|org|cdn|web|mql5|metaquotes|setup|exe|mt4|mt5|download|downloads|terminal|platform|trade|trading|group|ltd|limited|inc|llc|financial|services|technology|technologies|broker|brokers|global|markets|capital|online|invest|investing'
  }

  # "MetaTrader 5 AXI" -> "axi"; the bare generic build -> "".
  _dir_brand() {
    printf '%s' "$(basename "$1")" \
      | tr 'A-Z' 'a-z' \
      | sed -E 's/^metatrader[ _-]*5[ _-]*//' \
      | tr -d ' _-'
  }

  # Does this directory name carry the brand this installer is for? Consulted ONLY
  # for a directory that ALREADY existed -- the reinstall/update case, where an
  # installer refreshes the terminal it does not duplicate.
  _dir_is_brand() {
    _db="$(_dir_brand "$1")"
    [ -n "${_db}" ] || return 1
    for _tok in $(_brand_tokens); do
      case "${_db}" in
        "${_tok}"*) return 0 ;;
      esac
      case "${_tok}" in
        "${_db}"*) return 0 ;;
      esac
    done
    return 1
  }

  # The directory this install can actually claim, in order of strength:
  #   1. the directory the caller named, if a terminal is there;
  #   2. a directory that did not exist before this install ran;
  #   3. a directory whose name carries this installer's brand.
  _installed_dir() {
    if [ -f "${_want_dir}/terminal64.exe" ]; then
      printf '%s' "${_want_dir}"
      return 0
    fi
    _cand="$(_new_terminals | head -n 1)"
    if [ -n "${_cand}" ]; then
      printf '%s' "$(dirname "${_cand}")"
      return 0
    fi
    for _d in "${WINE_PREFIX}/drive_c/Program Files"/*/; do
      [ -f "${_d}terminal64.exe" ] || continue
      if _dir_is_brand "${_d}"; then
        printf '%s' "${_d%/}"
        return 0
      fi
    done
    return 1
  }

  # Predicate for the poll loops: only the two signals that mean "this install has
  # produced its terminal". The brand fallback is deliberately NOT here -- a
  # pre-existing branded terminal would end the loop ~0 s in and reap an install
  # that is still working. It is consulted once, after the loop.
  _have_terminal() {
    [ -f "${_want_dir}/terminal64.exe" ] && return 0
    [ -n "$(_new_terminals | head -n 1)" ]
  }

  # Run the installer in the BACKGROUND and poll for the terminal WHILE it works.
  #
  # WHY NOT BLOCK FIRST, POLL AFTER: blocking on the installer and only then
  # looking for terminal64.exe means a sandbox slow enough to exceed
  # MT5_SETUP_TIMEOUT gets the installer killed mid-write, the poll finds nothing,
  # and the script reports "terminal64.exe was not produced" for an install that
  # was actually progressing normally. Reproduced 2026-09-22: the identical
  # command, re-run manually, succeeded in ~4 minutes. Polling concurrently turns
  # that false failure into a success while still bounding a genuine hang.
  _term_found=0
  _attempt=1
  _last_attempt_log="${MT5_SETUP_LOG}"
  while [ "${_attempt}" -le "${MT5_INSTALL_ATTEMPTS}" ]; do
    # One log per attempt: the stalling attempt's own output is the evidence that
    # it stalled, so a retry must not overwrite it.
    if [ "${_attempt}" -eq 1 ]; then
      _last_attempt_log="${MT5_SETUP_LOG}"
    else
      _last_attempt_log="${MT5_ROOT}/mt5setup.attempt${_attempt}.log"
      status mt5 "retrying the MT5 install (attempt ${_attempt}/${MT5_INSTALL_ATTEMPTS}) ..."
    fi

    WINEDLLOVERRIDES="mscoree=" timeout "${MT5_SETUP_TIMEOUT}" \
      env -u WINEDEBUG "$WINE_BIN" "${INSTALLER}" /auto >>"${_last_attempt_log}" 2>&1 &
    _installer_pid=$!
    _pids="${_installer_pid}"
    _stall_deadline=$(( $(date +%s) + MT5_STALL_SECONDS ))
    _last_size=$(du -sk "${WINE_PREFIX}" 2>/dev/null | awk '{print $1}')
    _last_cpu=0
    _stalled=0

    while :; do
      if _have_terminal; then
        _term_found=1
        break
      fi
      if ! kill -0 "${_installer_pid}" 2>/dev/null; then
        # The installer returned on its own; the unpack window below decides.
        break
      fi
      _pids="${_installer_pid} $(_descendants "${_installer_pid}")"
      _size=$(du -sk "${WINE_PREFIX}" 2>/dev/null | awk '{print $1}')
      _cpu="$(_installer_cpu "${_pids}")"
      if [ "${_size:-0}" -gt "$(( ${_last_size:-0} + 256 ))" ] \
        || [ "${_cpu:-0}" -gt "$(( ${_last_cpu:-0} + 20 ))" ]; then
        _last_size="${_size:-0}"
        _last_cpu="${_cpu:-0}"
        _stall_deadline=$(( $(date +%s) + MT5_STALL_SECONDS ))
      fi
      if [ "$(date +%s)" -ge "${_stall_deadline}" ]; then
        _stalled=1
        _peers="$(_installer_peers "${_pids}")"
        log "MT5 installer moved no data and burned no CPU for ${MT5_STALL_SECONDS}s (peers: $(printf '%s' "${_peers}" | tr '\n' ' '))"
        _blackholed=0
        for _peer in ${_peers}; do
          if _blackhole_peer "${_peer}"; then
            _blackholed=$(( _blackholed + 1 ))
          fi
        done
        for _p in ${_pids}; do kill "${_p}" 2>/dev/null || true; done
        if [ "${_blackholed}" -eq 0 ]; then
          # Nothing to blackhole means the next attempt would wedge on the same
          # peer for another MT5_STALL_SECONDS. Fail now instead of spending it.
          log "WARN: could not identify or blackhole the stalled peer; not retrying"
          MT5_INSTALL_ATTEMPTS="${_attempt}"
        fi
        break
      fi
      sleep 10
    done

    # Reap the installer tree so nothing lingers past this attempt. If we broke
    # out because the terminal appeared, give it a short grace to exit on its own
    # -- it has done its job.
    _reap_deadline=$(( $(date +%s) + 60 ))
    while kill -0 "${_installer_pid}" 2>/dev/null && [ "$(date +%s)" -lt "${_reap_deadline}" ]; do
      if _have_terminal; then
        _term_found=1
        break
      fi
      sleep 5
    done
    kill "${_installer_pid}" 2>/dev/null || true
    for _p in ${_pids}; do kill "${_p}" 2>/dev/null || true; done
    wait "${_installer_pid}" 2>/dev/null || true
    if [ "${_term_found}" -eq 1 ]; then
      break
    fi

    # The installer is gone and there is no terminal. An installer that exited on
    # its own still gets the generous unpack window; one that was KILLED (by this
    # watchdog, or by its own MT5_SETUP_TIMEOUT) gets none at all.
    if [ "${_stalled}" -eq 0 ]; then
      _unpack_deadline=$(( $(date +%s) + MT5_UNPACK_GRACE ))
      _last_size=$(du -sk "${WINE_PREFIX}" 2>/dev/null | awk '{print $1}')
      while [ "$(date +%s)" -lt "${_unpack_deadline}" ]; do
        if _have_terminal; then
          _term_found=1
          break
        fi
        _size=$(du -sk "${WINE_PREFIX}" 2>/dev/null | awk '{print $1}')
        if [ "${_size:-0}" -gt "$(( ${_last_size:-0} + 256 ))" ]; then
          _last_size="${_size:-0}"
          _unpack_deadline=$(( $(date +%s) + MT5_UNPACK_GRACE ))
        fi
        sleep 5
      done
      if [ "${_term_found}" -eq 1 ]; then
        break
      fi
    fi

    _attempt=$(( _attempt + 1 ))
  done

  _INSTALLED_DIR="$(_installed_dir || true)"
  if [ -n "${_INSTALLED_DIR}" ]; then
    touch "${DONE_MARKER}"
    printf '%s' "${_RESOLVED_URL}" > "${MT5_ROOT}/.installed.url"
    # WHICH build landed. Read by mt5_cli.py's preflight, which refuses a login whose
    # server this build cannot resolve instead of letting it hang on the IPC timeout.
    printf '%s' "${MT5_BROKER_KEY:-unknown}" > "${MT5_ROOT}/.broker_key"
    # WHERE it landed, read back from the disk instead of assumed.
    #
    # MT5_BROKER_DIR_NAME is a prediction: Exness's installer writes "MetaTrader 5
    # Terminal" (not "MetaTrader 5 EXNESS") and a discovery-derived broker has no
    # verified name at all. Everything downstream that uses the name -- the MQL5
    # standard-library materialisation below, and mt5_cli.py's terminal finder on
    # every later call -- therefore looked in a directory the terminal is not in, so
    # a correct install reported a missing MQL5 tree and a terminal that "was never
    # installed".
    #
    # so the truth is read off the disk and recorded once, here.
    _INSTALLED_DIR_NAME="$(basename "${_INSTALLED_DIR}")"
    TERM_DISPLAY_NAME="${_INSTALLED_DIR_NAME}"
    printf '%s' "${_INSTALLED_DIR_NAME}" > "${MT5_ROOT}/.installed.dir_name"
    status mt5 "MT5 terminal installed (${_INSTALLED_DIR_NAME})"
    if [ "${_INSTALLED_DIR_NAME}" != "${MT5_BROKER_DIR_NAME}" ]; then
      log "the installer used '${_INSTALLED_DIR_NAME}', not the predicted '${MT5_BROKER_DIR_NAME}'; recorded in .installed.dir_name"
    fi
  else
    # Surface the installer's own output so the failure is actionable instead of
    # looking like a silent no-op -- AND land it in install.log.
    #
    # MEASURED (2026-09-22): a box whose install failed with
    # ``failed|installer exited with code 126`` had NO install.log at all, because
    # this branch only ever wrote to stderr. From outside the sandbox the only way
    # to read stderr is to have been the caller, so a box that had already finished
    # and been idled could not be diagnosed at all. install.log is the one file
    # that survives every path out of this script, so the installer's tail belongs
    # in it.
    {
      printf '[mt5-install] installer output (%s, last 2000 bytes):\n' "${_last_attempt_log}"
      tr -d '\000' <"${_last_attempt_log}" 2>/dev/null | tail -c 2000
      printf '\n'
    } >>"${MT5_ROOT}/install.log" 2>/dev/null || true
    # TWO different failures used to share one message. "terminal64.exe was not
    # produced" is right for an installer that failed outright; it is wrong, and
    # actively misleading, when a terminal that PREDATES this install is sitting on
    # disk -- the reader concludes the download broke, when the real answer is that
    # this prefix already carried a terminal and the installer did not add one.
    _PRE_EXISTING_TERMINAL="$(head -n 1 "${_TERMINALS_BEFORE}" 2>/dev/null || true)"
    if [ -n "${_PRE_EXISTING_TERMINAL}" ]; then
      _FAILURE="no_new_terminal"
      _REASON="the installer produced no new terminal; a terminal that predates this install is all that is on disk (${_PRE_EXISTING_TERMINAL})"
      _REMEDY="this Wine prefix already carried ${_PRE_EXISTING_TERMINAL} and the installer did not add a second terminal beside it: install into a fresh sandbox, or clear the prefix and install once, for the broker you want"
    else
      _FAILURE="terminal_not_produced"
      _REASON="terminal64.exe was not produced"
      _REMEDY="read the installer log tail in the payload; a wedged download peer and a 404 installer are the two common causes"
    fi
    {
      printf '{"ok": false, "stage": "failed", "failure": "%s", "error": "%s",\n' "${_FAILURE}" "${_REASON}"
      printf ' "remedy": "%s", "existing_terminal": "%s",\n' "${_REMEDY}" "${_PRE_EXISTING_TERMINAL}"
      printf ' "attempts": %s, "installer_log": "' "${_attempt}"
      tr -d '\000' <"${_last_attempt_log}" 2>/dev/null | tail -c 900 | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n' ' '
      printf '"}\n'
    } >&2
    status failed "${_REASON} after ${_attempt} attempt(s) (installer log: ${_last_attempt_log})"
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
    fetch_url "https://www.python.org/ftp/python/${MT5_WINPY_VERSION}/python-${MT5_WINPY_VERSION}-embed-amd64.zip" \
      "${WINPY_ZIP}" 5000000 \
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
    [ -s "${GETPIP}" ] || fetch_url "https://bootstrap.pypa.io/get-pip.py" "${GETPIP}" 100000 || true
    "$WINE_BIN" "${WIN_PY}" "${GETPIP}" --no-warn-script-location >/dev/null 2>&1 \
      || log "WARN: pip bootstrap failed"
  fi
  # numpy MUST be pinned below 2.
  #
  # Wine 10's *builtin* ucrtbase.dll does not implement the C99 complex-math
  # entry points (crealf/cimagf/...). numpy 2.x calls crealf from its compiled
  # _multiarray_umath during import, so `import MetaTrader5` — which imports
  # numpy — died with
  #   wine: Call from ... to unimplemented function ucrtbase.dll.crealf, aborting
  # Measured in the sandbox: `import numpy` alone (2.4.6) aborts; after
  # `pip install "numpy<2"` the bridge imports cleanly
  # (`BRIDGE_IMPORT_OK 5.0.6180 1.26.4`).
  #
  # Installing the genuine Microsoft ucrtbase via `winetricks vcrun2022` does NOT
  # fix it — verified: vcrun2022 exits 0, ucrtbase.dll is byte-identical before
  # and after, and the abort still fires. Do not re-try that route.
  #
  # This is why every MT5 bridge call used to hang until the sandbox command
  # timeout: Wine reacts to an unimplemented function by starting winedbg, which
  # on a headless box waits forever for nobody. The registry key below makes such
  # a call abort immediately instead of hanging, so a broken bridge reports an
  # error rather than silently burning 900 seconds.
  timeout 120 "$WINE_BIN" reg add 'HKCU\Software\Wine\WineDbg' \
      /v ShowCrashDialog /t REG_DWORD /d 0 /f >/dev/null 2>&1 || true
  "$WINE_BIN" "${WIN_PY}" -m pip install --no-input --disable-pip-version-check \
      "numpy<2" MetaTrader5 pandas >/dev/null 2>&1 \
    || log "WARN: MetaTrader5 install inside Wine failed"
else
  log "WARN: Windows Python was not installed; the MT5 bridge is unavailable"
fi

# ---------------------------------------------------------------------------
# Materialise the MQL5 folder.
#
# MT5 ships the standard library (<Trade/Trade.mqh>, <MovingAverages.mqh>, ...)
# *inside* terminal64.exe and only unpacks it into <install>/MQL5 on the first
# terminal launch. A silent install never launches the terminal, so the tree is
# absent and every compile of a realistic EA dies with
#   "cannot open source file, Include\Trade\Trade.mqh  not found"
# which reads exactly like a bug in the user's .mq5. Launch the terminal once,
# let it build the tree, then stop it. Best-effort: a failure here must not
# mark the whole install failed, since the chain is otherwise usable.
# ---------------------------------------------------------------------------
MT5_DIR="${WINE_PREFIX}/drive_c/Program Files/${TERM_DISPLAY_NAME}"
if [ -d "${MT5_DIR}" ] && [ ! -d "${MT5_DIR}/MQL5/Include" ]; then
  # MUST be a `status` write, not just a `log` line: this phase is the single
  # longest step left (up to MT5_LAUNCH_TIMEOUT, 600 s by default) and the old
  # marker stayed on `bridge`, so an agent polling `status` saw a 10-minute-old
  # stage and reasonably concluded the installer had wedged — which is exactly
  # the misdiagnosis recorded in this file's own history.
  status mql5stdlib "materialising MQL5 standard library (first terminal launch)"
  set +e
  # On a cold prefix the terminal's very first start can take several minutes
  # (it unpacks the MQL5 tree and probes the network). 180 s was not enough and
  # routinely tripped the timeout.
  #
  # MEASURED FAILURE (2026-09-21, real Novita sandbox): the old form here was
  # ``timeout "${MT5_LAUNCH_TIMEOUT:-600}" wine terminal64.exe``, which HOLDS THE
  # TERMINAL ALIVE for up to ten minutes (this run was still downloading an
  # ``mt5onnx64`` LiveUpdate payload 80 s in). ``status`` flips to
  # ``done`` the moment the tree exists, so the agent calls ``start`` while that
  # credential-less terminal is still running -- and the old ``cmd_start`` skipped
  # its own launch whenever *any* terminal was up, silently discarding the
  # login/password/server it had just been given. MT5 then never authorized, and
  # the agent was told to "pass login/password/server" that it HAD passed.
  #
  # So: launch it in the background, wait only for the tree, and kill it before
  # reporting success. ``start`` is the only command that may own a live terminal.
  nohup "$WINE_BIN" "${MT5_DIR}/terminal64.exe" >/dev/null 2>&1 &
  launch_deadline=$(( $(date +%s) + ${MT5_LAUNCH_TIMEOUT:-600} ))
  while [ ! -d "${MT5_DIR}/MQL5/Include" ] && [ "$(date +%s)" -lt "${launch_deadline}" ]; do
    sleep 5
  done
  # Give the unpack a beat to finish writing, then reclaim the terminal.
  sleep 5
  # Kill the terminal WITHOUT pattern-matching command lines.
  #
  # `pkill -f terminal64` is the classic self-kill: `-f` also matches (a) the
  # shell running this script and (b) `wineserver`, whose argv contains the
  # prefix path — either takes down the session mid-install.
  # `pkill -x terminal64.exe` is the opposite failure: it matches nothing,
  # because Wine starts the exe through its loader so the kernel comm is `main`
  # or `start.exe`, so the terminal would simply keep running.
  #
  # So resolve the PIDs from /proc the same way scripts/mt5_cli.py does: a
  # process counts only when one of its argv fields IS the terminal path.
  python3 - <<'PYEOF' || true
import os, signal, sys, time

def terminal_pids():
    found = set()
    me = os.getpid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        entries = []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                args = [a.decode("utf-8", "replace") for a in fh.read().split(b"\x00") if a]
        except OSError:
            continue
        if any(a.lower().endswith("terminal64.exe") for a in args):
            found.add(pid)
    return found

for pid in terminal_pids():
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass

# `stage=done` must mean NO terminal is running: a leftover credential-less
# terminal is exactly what made `start` silently drop the user's credentials.
for _ in range(10):
    if not terminal_pids():
        break
    time.sleep(1)
left = terminal_pids()
if left:
    print(f"WARN: terminal still running after kill: {sorted(left)}", file=sys.stderr)
PYEOF
  set -e
  if [ -d "${MT5_DIR}/MQL5/Include" ]; then
    log "MQL5 standard library ready ($(find "${MT5_DIR}/MQL5/Include" -name '*.mqh' | wc -l) headers)"
  else
    log "WARN: MQL5/Include still missing; compiles using <Trade/...> will need #include paths supplied"
  fi
fi

# ---------------------------------------------------------------------------
# Mirror the standard library into the terminal DATA tree.
#
# MetaEditor resolves ``#include <Trade/Trade.mqh>`` against the MQL5 data
# directory that owns the SOURCE file, NOT against ``--include``. Sources are
# conventionally written to the data tree
#   <prefix>/drive_c/users/<u>/AppData/Roaming/MetaQuotes/Terminal/Common/MQL5/Experts/
# and MetaEditor then looks for the sibling
#   .../Common/MQL5/Include/Trade/Trade.mqh
# which the installer ships EMPTY. The result is
#   error 106: file '...\Common\MQL5\Include\Trade\Trade.mqh' not found
# even though the library exists under ``Program Files/MetaTrader 5/MQL5/Include``
# and even when ``--include`` points straight at it. That error reads like a bug
# in the user's .mq5, so agents edited working code instead of fixing the tree.
# Keeping both trees populated makes a plain ``#include <Trade/...>`` compile with
# no extra flags. Best-effort: never fail an otherwise-good install over it.
# ---------------------------------------------------------------------------
if [ -d "${MT5_DIR}/MQL5/Include" ]; then
  status mql5mirror "mirroring the MQL5 standard library into the data tree"
  COMMON_MQL5="${WINE_PREFIX}/drive_c/users/${USER:-user}/AppData/Roaming/MetaQuotes/Terminal/Common/MQL5"
  case "${WINE_PREFIX}" in
    *users/*) : ;;  # already namespaced; keep default
  esac
  mkdir -p "${COMMON_MQL5}/Include"
  # ``-n`` so a broker-provided header is never clobbered by the stock one.
  cp -rn "${MT5_DIR}/MQL5/Include/." "${COMMON_MQL5}/Include/" 2>/dev/null || true
  _hdr=$(find "${COMMON_MQL5}/Include" -name '*.mqh' 2>/dev/null | wc -l)
  if [ "${_hdr}" -gt 0 ]; then
    log "MQL5 data-tree include ready (${_hdr} headers at ${COMMON_MQL5}/Include)"
  else
    log "WARN: could not mirror MQL5 standard library into the data tree"
  fi
fi

# THE LAST LINE HAS TO BE TRUE.
#
# ``status done`` was printed unconditionally, so every route out of the terminal
# section reported success -- including the one that SKIPPED the terminal because of a
# marker left by a run that had installed nothing. Measured live 2026-09-25: this line
# reported a successful AXI install over a prefix whose only terminal was Deriv's, and
# it is what ``status`` then showed an agent for the rest of the box's life.
#
# The gate is the terminal in the directory this run identified -- either one it just
# installed and read back off the disk, or the one the record it trusted names.
if [ ! -f "${WINE_PREFIX}/drive_c/Program Files/${TERM_DISPLAY_NAME}/terminal64.exe" ]; then
  status failed "install finished with no terminal in '${TERM_DISPLAY_NAME}'"
  printf '{"ok": false, "stage": "failed", "failure": "terminal_not_produced",\n' >&2
  printf ' "error": "no terminal64.exe in %s after the install"}\n' \
    "Program Files/${TERM_DISPLAY_NAME}" >&2
  exit 5
fi
status done "install complete: prefix=${WINE_PREFIX} win_python=${WIN_PY}"
printf '{"ok": true, "stage": "done", "wine_prefix": "%s", "mt5_root": "%s", "windows_python": "%s"}\n' \
  "${WINE_PREFIX}" "${MT5_ROOT}" "${WIN_PY}"