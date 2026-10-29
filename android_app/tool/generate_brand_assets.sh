#!/usr/bin/env bash
# Regenerate the CDNAI brand assets used by the Android client from the web
# app's own artwork, so the APK and the web UI can never drift apart.
#
# Source of truth: webui/public/brand/*  (nanobot_mark.svg = the orange robot
# mark shown in the web sidebar). Covers the in-app mark only.
#
# Requires ImageMagick (`magick`) with SVG delegate support.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$HERE/.." && pwd)"
REPO_DIR="$(cd "$APP_DIR/.." && pwd)"
WEB_BRAND="$REPO_DIR/webui/public/brand"
RES="$APP_DIR/android/app/src/main/res"

if [ ! -f "$WEB_BRAND/nanobot_mark.svg" ]; then
  echo "web brand assets not found at $WEB_BRAND" >&2
  exit 1
fi

# ---- in-app mark ---------------------------------------------------------
mkdir -p "$APP_DIR/assets/brand"
magick -background none -density 300 "$WEB_BRAND/nanobot_mark.svg" \
  -resize 512x512 -gravity center -background none -extent 512x512 \
  "$APP_DIR/assets/brand/mark.png"
magick -background none -density 300 "$WEB_BRAND/nanobot_mark.svg" \
  -resize 128x128 -gravity center -background none -extent 128x128 \
  "$APP_DIR/assets/brand/mark_128.png"

# Launcher icons are NOT generated here any more: they come from the approved
# app icon via tool/generate_app_icons.sh, so running this script cannot undo them.

echo "brand assets regenerated"
