#!/usr/bin/env bash
# Install the video/audio workshop (ffmpeg + yt-dlp + Pillow/OpenCV + faster-whisper
# + rembg) inside a Linux sandbox (Novita / Daytona / Runloop / VPS).
#
# WHY THIS EXISTS
#   The agent must be able to edit video — cut, crop to vertical, upscale to HD,
#   remove a background, pull shorts out of a long recording, transcribe and burn
#   captions, and download from a link — WITHOUT paying for an API. Running any of
#   that on the application host would be reckless: ffmpeg encodes are CPU-bound
#   and unbounded, the whisper and rembg models are hundreds of MB, and an encode
#   would compete with the gateway that serves every user. So the whole media
#   stack lives in the user's ephemeral sandbox and the host only forwards
#   commands.
#
# WHAT IT INSTALLS (inside the sandbox only)
#   - ffmpeg / ffprobe   static amd64 builds (no root, no shared libs to chase)
#   - yt-dlp             stand-alone binary (its own bundled Python)
#   - Pillow, numpy      stills and frame maths
#   - opencv-python-HEADLESS  face detection for the vertical crop. NOT
#                        `opencv-python`, which links libGL/libX11 and fails to
#                        import on a headless box with a GUI-less libgl error.
#   - faster-whisper     local transcription (CPU int8, or float16 on a GPU box)
#   - rembg[cli]         background removal; u2net weights land in ~/.u2net
#   - DejaVu fonts       so burned captions have a real glyph per codepoint
#
# DESIGN NOTES
#   * Idempotent: rerunning reuses every download and every model weight.
#   * NO root is assumed and none is needed — anything that would need apt is
#     replaced by a self-contained build or a pip wheel. The script never calls
#     sudo and never fails merely because it is not root.
#   * Every heavy step is best-effort in the sense that a missing OPTIONAL piece
#     (rembg, a GPU) does not abort the install: the CLI reports what is actually
#     available via `doctor`, and the actions that need a missing piece refuse
#     with the reason instead of producing a silent bad output.
#   * The final line of this script is a single JSON object, and `install.done` is
#     written last: `media_cli.py status` treats that marker as "the chain exists".
set -euo pipefail

MEDIA_BIN="${MEDIA_BIN:-$HOME/.media/bin}"
CACHE_DIR="${MEDIA_CACHE_DIR:-$HOME/.cache/media_cli}"
MODELS_DIR="${MEDIA_MODELS_DIR:-$HOME/.cache/media_cli/models}"
PY="${PYTHON:-python3}"
#: Where wheels go when the interpreter's own site-packages is not writable.
#:
#: MEASURED 2026-10-04 on Freestyle: its interpreter is a root-owned virtualenv
#: (/opt/freestyle/python), so `--user` is refused outright AND a plain install
#: dies with "[Errno 13] Permission denied: .../site-packages/flatbuffers". Both
#: look identical from the outside — "optional wheel unavailable" — and between
#: them they cost numpy, cv2, faster-whisper, rembg and onnxruntime. `--target`
#: needs no permission on the interpreter at all, so it is the last resort that
#: actually lands. media_cli.py puts this directory on sys.path and PATH itself,
#: which is what makes a --target install usable without a venv activation.
MEDIA_PYLIB="${MEDIA_PYLIB:-$HOME/.media/pylib}"

mkdir -p "${MEDIA_BIN}" "${CACHE_DIR}" "${MODELS_DIR}" "${MEDIA_PYLIB}" 2>/dev/null || true

# Exported once, for the whole script: a --target install is invisible to any
# interpreter that is not told about it, and every step below — warm_models, the
# entry-point shim, the final summary — is an `import`. Children inherit this, so
# it also covers the console scripts we place in MEDIA_BIN.
export PYTHONPATH="${MEDIA_PYLIB}${PYTHONPATH:+:${PYTHONPATH}}"

log() { printf '[media-install] %s\n' "$*" >&2; }

status() {
  local stage="$1"; shift
  printf '%s|%s\n' "$stage" "$*" >"${CACHE_DIR}/install.status" 2>/dev/null || true
  log "$*"
}

# Download a URL to a file, retrying on ANY failure.
#
# WHY THIS IS NOT JUST `curl --retry 3`: curl's --retry only retries *transient
# transport* failures (connect timeouts, resets, 5xx). It does NOT retry on an
# HTTP 4xx — and both raw.githubusercontent.com and the yt-dlp CDN intermittently
# answer 404 for a URL that returns 200 moments later. The size floor matters too:
# `curl -f` still leaves a truncated file behind on a dropped connection, and a
# CDN error page is a valid 200, so requiring >= min_bytes rejects both.
fetch_url() {
  local url="$1" dest="$2" min_bytes="${3:-100000}"
  local tries="${MEDIA_DOWNLOAD_TRIES:-4}" delay="${MEDIA_DOWNLOAD_BACKOFF:-4}"
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
      404|403|410)
        log "WARN: ${url} returned HTTP ${code} — retrying once via wget in case it is a CDN blip"
        ;;
      000)
        log "WARN: could not connect to ${url} (attempt ${i}/${tries})"
        ;;
      *)
        log "WARN: ${url} returned HTTP ${code} (attempt ${i}/${tries})"
        ;;
    esac

    if wget -q --timeout=120 --tries=1 -O "${dest}" "${url}" 2>/dev/null; then
      size="$(stat -c %s "${dest}" 2>/dev/null || echo 0)"
      if [ "${size}" -ge "${min_bytes}" ]; then
        return 0
      fi
    fi

    log "WARN: download attempt ${i}/${tries} failed for ${url}"
    rm -f "${dest}"
    if [ "${i}" -lt "${tries}" ]; then sleep "${delay}"; fi
    i=$((i + 1))
  done
  return 1
}

have() { command -v "$1" >/dev/null 2>&1; }

# --------------------------------------------------------------------------- #
# Filters the CLI actually invokes, and why this list has to be CHECKED rather
# than assumed.
#
# MEASURED 2026-10-04: johnvansickle's ffmpeg-release-amd64-static (7.0.2) installs
# cleanly, answers `-version` with a normal release string, and is missing
# `drawtext` — every burned timestamp and burned caption. It was the ONLY filter
# absent there, which is exactly why presence of a binary is the wrong check: the
# failure surfaces far from the install, as
#   watch --timestamps -> "[AVFilterGraph] No such filter: 'drawtext'"
# which reads as a broken feature, not a broken build. `-version` cannot tell the
# two apart; `-filters` can. So we check, and a build missing any of these is
# treated as a failed candidate and the next one is tried.
# --------------------------------------------------------------------------- #
MEDIA_REQUIRED_FILTERS="${MEDIA_REQUIRED_FILTERS:-drawtext subtitles ass overlay scale crop fps setsar unsharp format gblur}"

# Echoes the space-separated filters the installed ffmpeg lacks ("" when it has
# them all). Never fails: an unreadable filter list counts as everything missing,
# so a broken binary falls through to the next candidate instead of being used.
missing_filters() {
  local available="" missing="" f path
  path="$(command -v ffmpeg 2>/dev/null || true)"
  [ -n "${path}" ] || { printf 'ffmpeg'; return 0; }
  available="$(ffmpeg -hide_banner -filters 2>/dev/null | awk '{print $2}')" || available=""
  for f in ${MEDIA_REQUIRED_FILTERS}; do
    printf '%s\n' "${available}" | grep -qx "${f}" || missing="${missing} ${f}"
  done
  printf '%s' "${missing# }"
}

# --------------------------------------------------------------------------- #
# 0. A usable pip, without assuming it exists or that it may write system dirs.
#
# The base images differ: some ship pip, some ship an externally-managed system
# Python that refuses every install with "error: externally-managed-environment",
# and some ship no pip at all. Both cases look like "the install failed" when the
# real fix is a flag or get-pip. Everything below installs into --user, which
# needs no root and no venv, and lands in ~/.local/bin (already on our PATH).
# --------------------------------------------------------------------------- #
PIP_FLAGS="${MEDIA_PIP_FLAGS:-}"

ensure_pip() {
  ${PY} -m pip --version >/dev/null 2>&1 && return 0
  status pip "no pip found; bootstrapping it with get-pip.py"
  if fetch_url "https://bootstrap.pypa.io/get-pip.py" "${CACHE_DIR}/get-pip.py" 100000; then
    ${PY} "${CACHE_DIR}/get-pip.py" --user --break-system-packages >/dev/null 2>&1 || \
      ${PY} "${CACHE_DIR}/get-pip.py" --user >/dev/null 2>&1 || true
  fi
  ${PY} -m pip --version >/dev/null 2>&1
}

# Where a wheel should land, decided by asking the interpreter instead of assuming.
#
# MEASURED 2026-10-04 on Freestyle: that image runs its interpreter inside a
# virtualenv (/opt/freestyle/python, VIRTUAL_ENV set), and pip refuses `--user`
# there with "Can not perform a '--user' install. User site-packages are not
# visible in this virtualenv." The installer still exited 0 and still printed a
# summary, so the ONLY visible symptom was `python_deps` reading false for Pillow,
# numpy, cv2, faster-whisper, rembg and onnxruntime at once — six "unavailable
# wheels" that were really one wrong flag. In a virtualenv the target is the venv
# itself (no flag); on a system interpreter it is --user.
pip_target() {
  if ${PY} -c 'import sys; raise SystemExit(0 if sys.prefix == sys.base_prefix else 1)' 2>/dev/null; then
    printf '%s' "--user"
  else
    printf '%s' ""
  fi
}

# Install one or more wheels into the right target, self-healing the two
# environment quirks that actually bite in these images.
#
# MEASURED: Debian 12+/Ubuntu 24 ship an "externally managed" system Python whose
# pip refuses every install with `error: externally-managed-environment`. That
# reads as "the wheel is unavailable" and makes the whole feature look unsupported
# when the real fix is a single flag. Detecting it by *attempting* the install is
# deliberate: probing for the flag up front needs `pip install --dry-run`, which
# older pips do not have, and a version comparison is one more thing to get wrong.
# The retry costs nothing when it is not needed.
pip_install() {
  local out="" target
  target="$(pip_target)"
  if out="$(${PY} -m pip install ${target} ${PIP_FLAGS} --disable-pip-version-check \
      --no-input --no-cache-dir --upgrade "$@" 2>&1)"; then
    printf '%s\n' "${out}" >&2
    return 0
  fi
  case "${out}" in
    *externally-managed*|*"externally managed"*)
      log "system Python is externally managed; retrying with --break-system-packages"
      PIP_FLAGS="--break-system-packages"
      if ${PY} -m pip install ${target} ${PIP_FLAGS} --disable-pip-version-check \
        --no-input --no-cache-dir --upgrade "$@" >&2; then
        return 0
      fi
      ;;
    *"User site-packages are not visible"*|*"perform a '--user' install"*)
      # Belt and braces: if the venv probe above ever misreads an image, retry
      # without the flag rather than losing every Python piece to it.
      log "this interpreter rejects --user; retrying into its own site-packages"
      if ${PY} -m pip install ${PIP_FLAGS} --disable-pip-version-check \
        --no-input --no-cache-dir --upgrade "$@" >&2; then
        return 0
      fi
      ;;
  esac
  # Last resort, and the one that works on a root-owned virtualenv: install into a
  # directory WE own. No interpreter permission is involved, so "Permission denied"
  # stops being a dead end. media_cli.py puts ${MEDIA_PYLIB} on sys.path at import.
  case "${out}" in
    *"Permission denied"*|*"Errno 13"*|*"externally-managed"*)
      log "cannot write the interpreter's site-packages; installing into ${MEDIA_PYLIB}"
      mkdir -p "${MEDIA_PYLIB}" 2>/dev/null || true
      if ${PY} -m pip install --target "${MEDIA_PYLIB}" ${PIP_FLAGS} --disable-pip-version-check \
        --no-input --no-cache-dir --upgrade "$@" >&2; then
        return 0
      fi
      ;;
  esac
  printf '%s\n' "${out}" | tail -5 >&2
  log "WARN: pip install failed for: $*"
  return 1
}

# --------------------------------------------------------------------------- #
# 1. ffmpeg / ffprobe.
#
# Static builds are preferred over a distro package for three reasons: no root is
# needed, the binary carries every codec we might be asked for (libx264, libvpx-vp9
# with alpha, aac, ass/subtitles), and the sandbox image cannot be relied on to
# have a package manager that works.
# --------------------------------------------------------------------------- #
install_ffmpeg() {
  # MEDIA_BIN goes on PATH first, so the check below sees a binary a PREVIOUS run
  # left there. Without this the script was only idempotent on a login shell: on a
  # fresh non-login shell it re-downloaded a build it already had. And the check is
  # capability-based, not presence-based, so a rerun also REPAIRS an incomplete
  # build instead of re-accepting it forever.
  export PATH="${MEDIA_BIN}:$PATH"
  if have ffmpeg && have ffprobe && [ -z "$(missing_filters)" ]; then
    log "ffmpeg already present: $(ffmpeg -version 2>/dev/null | head -1)"
    return 0
  fi

  local tmp="${CACHE_DIR}/ffmpeg-dl"
  mkdir -p "${tmp}"

  # (a) johnvansickle static amd64 build — the classic no-dependency tarball.
  local url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
  status ffmpeg "downloading a static ffmpeg build"
  if fetch_url "${url}" "${tmp}/ffmpeg.tar.xz" 1000000; then
    if tar -xJf "${tmp}/ffmpeg.tar.xz" -C "${tmp}" 2>/dev/null; then
      local bin
      bin="$(find "${tmp}" -maxdepth 2 -type f -name ffmpeg -perm -u+x 2>/dev/null | head -1)"
      if [ -n "${bin}" ]; then
        install -m 0755 "${bin}" "${MEDIA_BIN}/ffmpeg"
        install -m 0755 "$(dirname "${bin}")/ffprobe" "${MEDIA_BIN}/ffprobe" 2>/dev/null || true
      fi
    fi
  fi

  local lacking=""
  if [ -x "${MEDIA_BIN}/ffmpeg" ]; then
    lacking="$(missing_filters)"
    if [ -z "${lacking}" ]; then
      log "ffmpeg ready: $(ffmpeg -version 2>/dev/null | head -1)"
      return 0
    fi
    log "WARN: the build that installed is missing filters: ${lacking} — trying a GPL build that has them"
  fi

  # (b) BtbN's GPL builds — a different host, so a johnvansickle outage is survivable.
  # Tried when nothing was installed AND when the build that installed cannot run the
  # filters the CLI needs — the failure mode a `-version` string hides. The previous
  # binary is left in place until this one lands, so a failed attempt cannot leave the
  # sandbox with no ffmpeg at all.
  if [ ! -x "${MEDIA_BIN}/ffmpeg" ] || [ -n "${lacking}" ]; then
    url="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
    status ffmpeg "static build unavailable; trying the BtbN build"
    if fetch_url "${url}" "${tmp}/btbn.tar.xz" 1000000; then
      tar -xJf "${tmp}/btbn.tar.xz" -C "${tmp}" 2>/dev/null || true
      local bin
      bin="$(find "${tmp}" -maxdepth 3 -type f -name ffmpeg -perm -u+x 2>/dev/null | head -1)"
      if [ -n "${bin}" ]; then
        install -m 0755 "${bin}" "${MEDIA_BIN}/ffmpeg"
        install -m 0755 "$(dirname "${bin}")/ffprobe" "${MEDIA_BIN}/ffprobe" 2>/dev/null || true
      fi
    fi
  fi

  # Same check again: if this build closed the gap we are done, and if it did not we
  # still fall through to the wheel below rather than accepting a known-incomplete
  # binary as if it were fine.
  if [ -x "${MEDIA_BIN}/ffmpeg" ] && [ -z "$(missing_filters)" ]; then
    log "ffmpeg ready: $(ffmpeg -version 2>/dev/null | head -1)"
    return 0
  fi

  # (c) Last resort: the imageio-ffmpeg wheel ships a working ffmpeg binary.
  #
  # It is a last resort because that build is stripped down (no libvpx-vp9 alpha,
  # so the transparent background-removal webm is not guaranteed) — but a working
  # ffmpeg with fewer codecs still edits video, and refusing to install anything is
  # strictly worse.
  if [ ! -x "${MEDIA_BIN}/ffmpeg" ] && ensure_pip; then
    status ffmpeg "falling back to the imageio-ffmpeg wheel"
    if pip_install imageio-ffmpeg; then
      local bundled
      bundled="$(${PY} - <<'PY' 2>/dev/null || true
import imageio_ffmpeg
print(imageio_ffmpeg.get_ffmpeg_exe())
PY
)"
      if [ -n "${bundled}" ] && [ -x "${bundled}" ]; then
        ln -sf "${bundled}" "${MEDIA_BIN}/ffmpeg"
        ln -sf "${bundled}" "${MEDIA_BIN}/ffprobe" 2>/dev/null || true
      fi
    fi
  fi

  # A PATH entry for the CURRENT shell too, so the version check below and every
  # later step in this script resolves the binary we just placed.
  export PATH="${MEDIA_BIN}:$PATH"
  if have ffmpeg; then
    local still=""
    still="$(missing_filters)"
    if [ -n "${still}" ]; then
      # Installed but incomplete. Say WHICH filters are gone so the refusal a later
      # action prints names the real cause instead of looking like a broken feature.
      log "WARN: ffmpeg installed but lacks: ${still} — burned captions/timestamps will refuse with a reason"
    else
      log "ffmpeg ready: $(ffmpeg -version 2>/dev/null | head -1)"
    fi
    return 0
  fi
  log "WARN: ffmpeg could not be installed — video actions will refuse with a reason"
  return 1
}

# --------------------------------------------------------------------------- #
# 2. yt-dlp.
#
# The stand-alone `yt-dlp_linux` binary is preferred over pip: it embeds its own
# Python, so it cannot be broken by this image's interpreter version, and it needs
# no pip at all. YouTube's extraction changes constantly, so this is also the piece
# most likely to need a `-U` refresh — noted in the doctor output as the version.
# --------------------------------------------------------------------------- #
install_ytdlp() {
  if have yt-dlp; then
    log "yt-dlp already present: $(yt-dlp --version 2>/dev/null)"
    return 0
  fi
  status ytdlp "downloading the stand-alone yt-dlp binary"
  local url="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux"
  if fetch_url "${url}" "${MEDIA_BIN}/yt-dlp" 1000000; then
    chmod 0755 "${MEDIA_BIN}/yt-dlp" 2>/dev/null || true
  fi
  export PATH="${MEDIA_BIN}:$PATH"
  if have yt-dlp; then
    log "yt-dlp ready: $(yt-dlp --version 2>/dev/null)"
    return 0
  fi
  status ytdlp "stand-alone binary failed; installing yt-dlp through pip"
  if ensure_pip && pip_install yt-dlp; then
    if [ -x "${HOME}/.local/bin/yt-dlp" ]; then
      ln -sf "${HOME}/.local/bin/yt-dlp" "${MEDIA_BIN}/yt-dlp"
    fi
    log "yt-dlp ready (pip): $(yt-dlp --version 2>/dev/null || echo unknown)"
    return 0
  fi
  log "WARN: yt-dlp could not be installed — downloads will refuse with a reason"
  return 1
}

# --------------------------------------------------------------------------- #
# 3. Python pieces: Pillow + numpy (stills and frame maths), OpenCV headless
#    (face detection for the vertical crop), faster-whisper (local ASR) and
#    rembg (background removal).
#
# Each is installed on its own so one unavailable wheel cannot cost us the others.
# --------------------------------------------------------------------------- #
install_python_stack() {
  ensure_pip || {
    log "WARN: no usable pip — Python-dependent actions (bg/transcribe/crop --focus face) will refuse"
    return 1
  }
  local want="Pillow numpy opencv-python-headless"
  local optional="faster-whisper rembg[cli] onnxruntime"

  status python "installing Pillow, numpy and opencv-python-headless"
  # One wheel at a time: `pip install a b c` fails as a unit, so a single
  # unavailable wheel would cost us the other two as well.
  local wheel
  for wheel in ${want}; do
    pip_install ${wheel} || log "WARN: core wheel unavailable: ${wheel}"
  done

  status python "installing onnxruntime, faster-whisper and rembg (these are the big ones)"
  for wheel in ${optional}; do
    pip_install ${wheel} || log "WARN: optional wheel unavailable: ${wheel}"
  done

  # Make sure the console scripts are reachable from a non-login shell.
  local name src
  for name in rembg yt-dlp; do
    for src in "${HOME}/.local/bin/${name}" "${MEDIA_PYLIB}/bin/${name}"; do
      if [ -x "${src}" ]; then
        ln -sf "${src}" "${MEDIA_BIN}/${name}" 2>/dev/null || true
        break
      fi
    done
  done

  # A `--target` install does not reliably write the console script: MEASURED
  # 2026-10-04, numpy's f2py and onnxruntime's test binary landed in
  # ${MEDIA_PYLIB}/bin while rembg's did not — so `action=bg` refused with "rembg
  # is not installed" while the package sat importable one directory away. When the
  # package imports but the entry point is missing, write the two-line shim that
  # calls the same function the console script would have called.
  if [ ! -x "${MEDIA_BIN}/rembg" ] && ${PY} -c 'import rembg.cli' >/dev/null 2>&1; then
    log "no rembg console script was installed; writing the entry-point shim"
    printf '#!/usr/bin/env python3\nfrom rembg.cli import main\nmain()\n' >"${MEDIA_BIN}/rembg" 2>/dev/null || true
    chmod 0755 "${MEDIA_BIN}/rembg" 2>/dev/null || true
  fi

  repair_pyav
  return 0
}

# Does the installed PyAV still accept the argument faster-whisper calls it with?
#
# MEASURED 2026-10-04: pip resolves `av` to its NEWEST release, but PyAV REMOVED the
# ``metadata_errors`` argument to ``av.open`` — and faster-whisper 1.2.1 still passes
# it whenever it decodes audio. So the fully-installed state gave
#   transcribe -> TypeError: open() got an unexpected keyword argument 'metadata_errors'
# on av 19.0.1 while `doctor` happily reported faster_whisper 1.2.1 present: the
# whisper model was there, the audio decoder was not. transcribe, captions and
# `shorts --captions` were all dead. Verified fixed by av 18.1.0.
#
# Same rule as the ffmpeg check above: probe the capability the caller actually
# uses and repair it, instead of trusting the resolver's version choice.
av_accepts_metadata_errors() {
  ${PY} - <<'PY' >/dev/null 2>&1
import av
try:
    av.open("/dev/null", metadata_errors="ignore")
except TypeError:
    raise SystemExit(1)
except Exception:
    raise SystemExit(0)
PY
}

repair_pyav() {
  ${PY} -c 'import faster_whisper' >/dev/null 2>&1 || return 0
  av_accepts_metadata_errors && return 0
  log "the installed PyAV rejects metadata_errors, which faster-whisper needs; pinning av<19"
  # Only the copy WE installed is removed, and only from our own target directory —
  # a PyAV that came with the image is left where it is.
  local where=""
  where="$(${PY} -c 'import av, os; print(os.path.dirname(os.path.dirname(av.__file__)))' 2>/dev/null || true)"
  if [ "${where}" = "${MEDIA_PYLIB}" ]; then
    rm -rf "${MEDIA_PYLIB}/av" "${MEDIA_PYLIB}/av.libs" "${MEDIA_PYLIB}"/av-*.dist-info 2>/dev/null || true
  fi
  if pip_install "av<19" && av_accepts_metadata_errors; then
    log "PyAV pinned to a faster-whisper-compatible release: $(${PY} -c 'import av; print(av.__version__)' 2>/dev/null)"
  else
    log "WARN: could not pin a compatible PyAV — transcribe will refuse with a reason"
  fi
}

# --------------------------------------------------------------------------- #
# 4. Caption font.
#
# The burn-in filter needs a real font file: without one ffmpeg's subtitles/ass
# renderer draws nothing, and the output is a perfectly good video with no
# captions — a silent failure that looks like the caption feature is broken.
# --------------------------------------------------------------------------- #
install_fonts() {
  local dir="${HOME}/.local/share/fonts"
  local target="${dir}/DejaVuSans-Bold.ttf"
  if [ -f "${target}" ] || [ -f /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf ]; then
    log "caption font already present"
    return 0
  fi
  mkdir -p "${dir}" "${CACHE_DIR}/fonts" 2>/dev/null || true
  local zip="${CACHE_DIR}/dejavu.zip"
  status fonts "downloading the DejaVu font family for burned captions"
  if fetch_url "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip" "${zip}" 1000000; then
    ${PY} - "${zip}" "${dir}" <<'PY' 2>/dev/null || true
import sys, zipfile, os
zip_path, dest = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zip_path) as zf:
    for member in zf.namelist():
        if member.endswith(("DejaVuSans-Bold.ttf", "DejaVuSans.ttf")) and "/ttf/" in member:
            with zf.open(member) as src, open(os.path.join(dest, os.path.basename(member)), "wb") as out:
                out.write(src.read())
PY
  fi
  # Mirror into the CLI's cache dir too: the CLI looks in three places, and this is
  # the one that is guaranteed writable.
  for f in DejaVuSans-Bold.ttf DejaVuSans.ttf; do
    [ -f "${dir}/${f}" ] && cp -f "${dir}/${f}" "${CACHE_DIR}/fonts/${f}" 2>/dev/null || true
  done
  if [ -f "${dir}/DejaVuSans-Bold.ttf" ]; then
    log "caption font ready at ${dir}/DejaVuSans-Bold.ttf"
  else
    log "WARN: no caption font — burned captions will be skipped rather than silently blank"
  fi
  return 0
}

# --------------------------------------------------------------------------- #
# 5. Warm the models, so the FIRST real request is not the one that pays for the
#    download. Both are best-effort: the CLI fetches them lazily anyway.
# --------------------------------------------------------------------------- #
warm_models() {
  status models "pre-fetching the whisper and rembg weights (best-effort)"
  # rembg: instantiating the session downloads u2net.onnx into ~/.u2net.
  if ${PY} -c "import rembg" >/dev/null 2>&1; then
    ${PY} - <<'PY' >/dev/null 2>&1 || true
from rembg import new_session
new_session("u2net")
new_session("u2net_human_seg")
PY
  fi
  # faster-whisper: the model files come from Hugging Face on first use.
  if ${PY} -c "import faster_whisper" >/dev/null 2>&1; then
    ${PY} - <<'PY' >/dev/null 2>&1 || true
import os
from faster_whisper import WhisperModel
model = os.getenv("MEDIA_WHISPER_WARM_MODEL", "tiny")
WhisperModel(model, device="cpu", compute_type="int8")
PY
  fi
  return 0
}

# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
status start "installing the media workshop into ${MEDIA_BIN}"

install_ffmpeg || true
install_ytdlp || true
install_python_stack || true
install_fonts || true
warm_models || true

export PATH="${MEDIA_BIN}:${HOME}/.local/bin:${MEDIA_PYLIB}/bin:$PATH"

# A machine-readable summary, built from what is ACTUALLY present rather than from
# what we tried to install — this is what makes the JSON worth parsing.
summary="$(${PY} - <<'PY' 2>/dev/null || echo '{}'
import json, shutil, subprocess

def version(cmd, *args):
    path = shutil.which(cmd)
    if not path:
        return None
    try:
        out = subprocess.run([path, *args], capture_output=True, text=True, timeout=30)
        text = (out.stdout or out.stderr or "").strip().splitlines()
        return text[0][:120] if text else "ok"
    except Exception:
        return "ok"

# Which of the filters the CLI actually invokes this build can run. Reported so a
# build that installed but cannot burn captions is visible here instead of only at
# the first `watch --timestamps`, where it looks like a broken feature.
def ffmpeg_filters():
    path = shutil.which("ffmpeg")
    if not path:
        return {"available": False, "missing": []}
    try:
        out = subprocess.run([path, "-hide_banner", "-filters"], capture_output=True,
                             text=True, timeout=60)
        have = {parts[1] for parts in (line.split() for line in out.stdout.splitlines()) if len(parts) > 1}
    except Exception:
        return {"available": False, "missing": []}
    required = "drawtext subtitles ass overlay scale crop fps setsar unsharp format gblur".split()
    return {"available": True, "missing": [f for f in required if f not in have]}

deps = {}
for module in ("PIL", "numpy", "cv2", "faster_whisper", "rembg"):
    try:
        __import__(module)
        deps[module] = True
    except Exception:
        deps[module] = False

print(json.dumps({
    "ffmpeg": shutil.which("ffmpeg"),
    "ffprobe": shutil.which("ffprobe"),
    "yt_dlp": shutil.which("yt-dlp"),
    "ffmpeg_version": version("ffmpeg", "-version"),
    "yt_dlp_version": version("yt-dlp", "--version"),
    "ffmpeg_filters": ffmpeg_filters(),
    "python_deps": deps,
}))
PY
)"

# install.done is written LAST: it is the marker `media_cli.py status` reads as
# "the chain exists", so it must never appear before the chain does.
printf '%s\n' "${summary}" >"${CACHE_DIR}/install.summary.json" 2>/dev/null || true
printf 'done %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"${CACHE_DIR}/install.done" 2>/dev/null || true

status done "install complete"

# One JSON object on stdout, matching the MT5 installer's contract.
printf '%s\n' "${summary}"
