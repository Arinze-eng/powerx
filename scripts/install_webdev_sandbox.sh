#!/usr/bin/env bash
# Install the web-deployment toolchain (Node.js + the Vercel CLI) inside a Linux
# sandbox (Novita / Freestyle / Daytona / Runloop / Tenki / VPS).
#
# WHY THIS EXISTS
#   Deploying a web project means running the Vercel CLI, which is a Node program
#   that resolves a dependency graph, bundles the project and uploads it. The
#   application host is a 512 MB skeleton that also serves every user's gateway
#   turn, so running Node there — or pulling the project out of the sandbox onto
#   the host just to upload it back — is exactly the thing we refuse to do. The
#   project is BUILT in the sandbox, so the CLI that ships it runs in the sandbox
#   too, and the host only forwards the command and reads the URL back.
#
# WHAT IT INSTALLS (inside the sandbox only)
#   - Node.js   the system `node` when it is already present and new enough;
#               otherwise a self-contained official static build, unpacked under
#               $HOME (no root, no apt, nothing system-wide).
#   - vercel    the official Vercel CLI, installed with npm into a private prefix
#               so a global npm tree that needs root is never touched.
#
# DESIGN NOTES
#   * Idempotent: rerunning is a no-op once the ready marker exists, so the
#     bootstrap that runs before every web_dev call costs nothing after the first.
#   * NO root is assumed and none is needed.
#   * `--status` prints one JSON object and exits; it is what the tool reports so
#     the model can see whether the toolchain is present without guessing.
#   * The last line on stdout of `--install` is the same JSON object, and the
#     ready marker is written LAST, so its existence means the chain is complete.
set -euo pipefail

WEBDEV_INSTALLER_VERSION='1.0.0'

WEBDEV_HOME="${WEBDEV_HOME:-$HOME/.webdev}"
NODE_DIR="${WEBDEV_HOME}/node"
BIN_DIR="${WEBDEV_HOME}/bin"
# Pinned so a rerun is deterministic; the CLI itself is version-independent.
NODE_VERSION="${WEBDEV_NODE_VERSION:-22.14.0}"
NODE_TARBALL="node-v${NODE_VERSION}-linux-x64"
NODE_URL="${WEBDEV_NODE_URL:-https://nodejs.org/dist/v${NODE_VERSION}/${NODE_TARBALL}.tar.gz}"
NPM_PREFIX="${WEBDEV_HOME}"
VERCEL_BIN="${WEBDEV_HOME}/node_modules/.bin/vercel"
READY_MARKER="${WEBDEV_HOME}/ready"

log() { printf '[webdev-install] %s\n' "$*" >&2; }

mkdir -p "${BIN_DIR}" "${WEBDEV_HOME}" 2>/dev/null || true

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
# Download a URL to a file, retrying on ANY failure. `curl --retry` only retries
# transient transport errors, not the 4xx that raw.githubusercontent.com and the
# Node CDN intermittently answer for a URL that works a moment later. A size
# floor rejects both a truncated file and a CDN error page that came back 200.
fetch_url() {
  local url="$1" dest="$2" min_bytes="${3:-1000000}"
  local tries="${WEBDEV_DOWNLOAD_TRIES:-4}" delay="${WEBDEV_DOWNLOAD_BACKOFF:-4}"
  local i=1 size=0 code=""

  while [ "${i}" -le "${tries}" ]; do
    rm -f "${dest}"
    code="$(curl -sSL --connect-timeout 20 --max-time 900 -o "${dest}" \
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
      *) log "WARN: ${url} -> HTTP ${code}; retrying in ${delay}s" ;;
    esac
    i=$((i + 1))
    [ "${i}" -le "${tries}" ] && sleep "${delay}"
  done
  return 1
}

node_major() {
  local bin="$1"
  [ -x "${bin}" ] || return 1
  "${bin}" --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/'
}

# Resolve a usable `node` binary: an existing system node first (fastest, and it
# is what a well-provisioned image already ships), then our private build.
resolve_node() {
  local sys major
  sys="$(command -v node 2>/dev/null || true)"
  if [ -n "${sys}" ]; then
    major="$(node_major "${sys}" || echo 0)"
    if [ "${major:-0}" -ge 18 ] 2>/dev/null; then
      printf '%s' "${sys}"
      return 0
    fi
  fi
  if [ -x "${NODE_DIR}/bin/node" ]; then
    major="$(node_major "${NODE_DIR}/bin/node" || echo 0)"
    if [ "${major:-0}" -ge 18 ] 2>/dev/null; then
      printf '%s' "${NODE_DIR}/bin/node"
      return 0
    fi
  fi
  return 1
}

install_node() {
  if resolve_node >/dev/null 2>&1; then
    return 0
  fi
  log "no usable node found; fetching ${NODE_TARBALL}"
  mkdir -p "${NODE_DIR}"
  local tmp="${WEBDEV_HOME}/${NODE_TARBALL}.tar.gz"
  if ! fetch_url "${NODE_URL}" "${tmp}" 20000000; then
    log "ERROR: could not download Node.js from ${NODE_URL}"
    return 1
  fi
  tar -xzf "${tmp}" -C "${NODE_DIR}" --strip-components=1
  rm -f "${tmp}"
  log "node installed: $("${NODE_DIR}/bin/node" --version 2>/dev/null)"
  return 0
}

install_vercel() {
  local node npm
  node="$(resolve_node 2>/dev/null || true)"
  if [ -z "${node}" ]; then
    log "ERROR: no node available to run npm"
    return 1
  fi
  npm="${NODE_DIR}/bin/npm"
  if [ ! -x "${npm}" ]; then
    # System node: its npm is a sibling of the binary on PATH.
    npm="$(command -v npm 2>/dev/null || true)"
  fi
  if [ -z "${npm}" ]; then
    log "ERROR: no npm available next to node at ${node}"
    return 1
  fi
  if [ -x "${VERCEL_BIN}" ]; then
    log "vercel already installed: $("${VERCEL_BIN}" --version 2>/dev/null || echo unknown)"
    return 0
  fi
  log "installing the Vercel CLI with npm into ${NPM_PREFIX}"
  export PATH="$(dirname "${node}"):${PATH}"
  if ! "${npm}" install --prefix "${NPM_PREFIX}" --no-audit --no-fund --loglevel=error vercel >/dev/null 2>&1; then
    log "ERROR: npm install of vercel failed"
    return 1
  fi
  return 0
}

# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
report() {
  local node vercel
  node="$(resolve_node 2>/dev/null || true)"
  vercel=""
  if [ -x "${VERCEL_BIN}" ]; then
    vercel="$("${VERCEL_BIN}" --version 2>/dev/null | head -n1 || true)"
  fi
  python3 - "$node" "$vercel" "$READY_MARKER" <<'PY' 2>/dev/null || printf '{"ready":false}\n'
import json, os, sys
node, vercel, marker = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({
    "installer_version": "1.0.0",
    "node": node or None,
    "vercel": vercel or None,
    "vercel_bin": os.path.expanduser("~/.webdev/node_modules/.bin/vercel"),
    "ready": bool(node and vercel),
    "marker": marker if os.path.exists(marker) else None,
}))
PY
}

# --------------------------------------------------------------------------- #
# entry
# --------------------------------------------------------------------------- #
mode="${1:---install}"
case "${mode}" in
  --status)
    report
    exit 0
    ;;
  --version)
    printf '%s\n' "${WEBDEV_INSTALLER_VERSION}"
    exit 0
    ;;
esac

# Fast path: already provisioned and the marker agrees.
if [ -f "${READY_MARKER}" ] && [ -x "${VERCEL_BIN}" ]; then
  report
  exit 0
fi

install_node || true
install_vercel || true

# The marker is written BEFORE the report is built, so the JSON `--install`
# prints and the state a later `--status` sees can never disagree.
if [ -x "${VERCEL_BIN}" ]; then
  printf 'done %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"${READY_MARKER}" 2>/dev/null || true
else
  rm -f "${READY_MARKER}" 2>/dev/null || true
fi

summary="$(report)"
printf '%s\n' "${summary}" >"${WEBDEV_HOME}/install.summary.json" 2>/dev/null || true
printf '%s\n' "${summary}"
