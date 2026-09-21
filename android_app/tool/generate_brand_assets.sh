#!/usr/bin/env bash
# Regenerate the CDNAI brand assets used by the Android client from the web
# app's own artwork, so the APK and the web UI can never drift apart.
#
# Source of truth: webui/public/brand/*  (nanobot_mark.svg = the orange robot
# mark shown in the web sidebar; nanobot_icon_512.png = the installable icon).
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

# ---- launcher icons ------------------------------------------------------
# Paper-white tile (the web canvas) with the robot inside the 66% safe zone,
# matching how the mark reads in a browser tab.
tile() { # size, out
  local size="$1" out="$2"
  magick -size "${size}x${size}" xc:"#FDFDFC" \
    \( -background none -density 300 "$WEB_BRAND/nanobot_mark.svg" \
       -resize "$((size * 66 / 100))x$((size * 66 / 100))" \) \
    -gravity center -composite "$out"
}
round_tile() { # size, out
  local size="$1" out="$2"
  magick -size "${size}x${size}" xc:none -fill "#FDFDFC" \
    -draw "circle $((size / 2)),$((size / 2)) $((size / 2)),0" \
    \( -background none -density 300 "$WEB_BRAND/nanobot_mark.svg" \
       -resize "$((size * 62 / 100))x$((size * 62 / 100))" \) \
    -gravity center -composite "$out"
}
foreground() { # size, out  (transparent, artwork inside the safe zone)
  local size="$1" out="$2"
  magick -background none -density 300 "$WEB_BRAND/nanobot_mark.svg" \
    -resize "$((size * 60 / 100))x$((size * 60 / 100))" \
    -gravity center -background none -extent "${size}x${size}" "$out"
}

declare -A DPI=( [mdpi]=48 [hdpi]=72 [xhdpi]=96 [xxhdpi]=144 [xxxhdpi]=192 )
for dpi in "${!DPI[@]}"; do
  size="${DPI[$dpi]}"
  mkdir -p "$RES/mipmap-$dpi"
  tile "$size" "$RES/mipmap-$dpi/ic_launcher.png"
  round_tile "$size" "$RES/mipmap-$dpi/ic_launcher_round.png"
  foreground "$((size * 108 / 48))" "$RES/mipmap-$dpi/ic_launcher_foreground.png"
done

echo "brand assets regenerated"
