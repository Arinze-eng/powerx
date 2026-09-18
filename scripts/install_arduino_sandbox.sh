#!/usr/bin/env bash
# Install the Arduino verification toolchain inside any Linux sandbox.
#
# Works for Novita Sandboxes, plain VPS hosts (Ubuntu/Debian), and Runloop
# devboxes — anywhere the agent can reach the network. Run it once per
# sandbox (or bake it into the sandbox template) and the `arduino_verify`
# tool can then compile + simulate firmware remotely.
#
#   curl -fsSL <raw-url>/scripts/install_arduino_sandbox.sh | bash
#   # or, through the agent:
#   novita_sandbox action=run command="bash /workspace/install_arduino_sandbox.sh"
#
# Env overrides:
#   ARDUINO_TOOLCHAIN_DIR   default /opt/arduino-toolchain (falls back to $HOME)
#   ARDUINO_CLI_VERSION     default 1.5.1
#   INSTALL_ESP32=1         also install the esp32:esp32 core (large)
set -euo pipefail

DEST="${ARDUINO_TOOLCHAIN_DIR:-/opt/arduino-toolchain}"
CLI_VERSION="${ARDUINO_CLI_VERSION:-1.5.1}"

# Fall back to a writable location when /opt is not writable (non-root shell).
if ! mkdir -p "$DEST" 2>/dev/null; then
  DEST="$HOME/.arduino-toolchain"
  mkdir -p "$DEST"
fi
mkdir -p "$DEST/data" "$DEST/dl" "$DEST/sim"

echo "==> Installing arduino-cli ${CLI_VERSION} into ${DEST}"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) CLI_ARCH="Linux_64bit" ;;
  aarch64|arm64) CLI_ARCH="Linux_ARM64" ;;
  *) echo "Unsupported arch: $ARCH" >&2; exit 1 ;;
esac
curl -fsSL "https://downloads.arduino.cc/arduino-cli/arduino-cli_${CLI_VERSION}_${CLI_ARCH}.tar.gz" \
  | tar -xz -C "$DEST"
"$DEST/arduino-cli" version

export ARDUINO_DIRECTORIES_DATA="$DEST/data"
export ARDUINO_DIRECTORIES_DOWNLOADS="$DEST/dl"

echo "==> Installing arduino:avr core"
"$DEST/arduino-cli" core update-index
"$DEST/arduino-cli" core install arduino:avr

if [ "${INSTALL_ESP32:-0}" = "1" ]; then
  echo "==> Installing esp32:esp32 core"
  "$DEST/arduino-cli" config add board_manager.additional_urls \
    https://espressif.github.io/arduino-esp32/package_esp32_index.json
  "$DEST/arduino-cli" core update-index
  "$DEST/arduino-cli" core install esp32:esp32
fi

echo "==> Installing common libraries"
for lib in Servo "DHT sensor library" RTClib LiquidCrystal LiquidCrystal_I2C; do
  "$DEST/arduino-cli" lib install "$lib" || echo "  (skipped: $lib)"
done

echo "==> Installing the AVR emulator runtime (avr8js)"
if command -v npm >/dev/null 2>&1; then
  cd "$DEST/sim"
  [ -f package.json ] || printf '{"name":"arduino-sim","private":true}' > package.json
  npm install --no-audit --no-fund avr8js@0.20.0
else
  echo "npm not found — install Node.js 18+ so the simulator can run." >&2
fi

chmod -R a+rX "$DEST" 2>/dev/null || true

cat <<EOF

==> Arduino toolchain ready at ${DEST}

Export these for the arduino_verify tool:
  export ARDUINO_TOOLCHAIN_DIR=${DEST}
  export ARDUINO_SIM_DIR=${DEST}/sim
  export ARDUINO_VERIFY_ENABLED=1

Verify:
  ${DEST}/arduino-cli core list
  node -e "require('avr8js'); console.log('avr8js ok')" --prefix ${DEST}/sim 2>/dev/null || \\
    (cd ${DEST}/sim && node -e "require('avr8js'); console.log('avr8js ok')")
EOF