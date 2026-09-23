#!/usr/bin/env python3
"""media_cli.py — the video/audio workshop that runs *inside* a sandbox.

This is the tool the agent drives for everything video: watch a clip, cut it,
crop it, put it in HD, pull the background out, get shorts out of a long
recording, transcribe it, burn captions, or download from a link. It is written
for a sandbox with no GPU, no paid API and no sudo: ffmpeg does the pixels,
yt-dlp does the fetching, faster-whisper does the words and rembg does the
matte, all locally.

Design rules that make it trustworthy rather than merely capable:

* **Every action verifies its own output.** Nothing reports success from a zero
  exit code alone: the artifact is probed afterwards (duration, resolution,
  streams, size) and the verified facts go into the JSON result. A step that
  produced a 0-byte or wrong-shaped file fails here, not three steps later.
* **Quality is the default, never a speed/quality coin flip.** Video is encoded
  H.264 CRF 18 preset slow with `+faststart`; audio AAC 192k. Stream copy is
  used only where it is provably safe (``--fast`` on an aligned trim).
* **JSON out, always.** One parseable object on stdout, errors included, so the
  caller never has to scrape prose.
* **Idempotent installs.** Heavy pieces (whisper models, rembg weights) are
  fetched once into a cache dir and reused.

Usage (inside the sandbox):
    media_cli.py doctor
    media_cli.py install                 # detached; poll with `status`
    media_cli.py probe  in.mp4
    media_cli.py watch  in.mp4 --count 12 --out sheet.png
    media_cli.py trim   in.mp4 --start 00:00:05 --end 00:00:12 --out cut.mp4
    media_cli.py crop   in.mp4 --aspect 9:16 --focus face --out vert.mp4
    media_cli.py hd     in.mp4 --height 1080 --out hd.mp4
    media_cli.py bg     in.mp4 --model u2net_human_seg --out cutout.webm
    media_cli.py download 'https://youtu.be/...' --out downloads --quality 1080
    media_cli.py transcribe in.mp4 --model base --format srt --out in.srt
    media_cli.py captions in.mp4 --srt in.srt --style shorts --out capped.mp4
    media_cli.py shorts in.mp4 --count 3 --captions --out shorts/
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Iterable

#: Bumped on every behaviour change. The bootstrap in
#: ``nanobot/agent/tools/media.py`` greps for this exact assignment and refuses
#: to run a sandbox copy that carries a different one, so a stale (CDN-cached)
#: revision can never masquerade as the current one.
CLI_VERSION = "1.0.0"

#: Where heavy downloads live so they survive across calls in one sandbox.
CACHE_DIR = Path(os.getenv("MEDIA_CACHE_DIR", str(Path.home() / ".cache" / "media_cli")))

#: Where the installer drops its binaries. These are prepended to PATH at import
#: time so that BOTH this process (``shutil.which``) and every child it spawns
#: (ffmpeg spawned by yt-dlp, rembg spawned by us) resolve them without the
#: caller having to export anything. The sandbox's non-login shell does not read
#: ~/.profile, so a PATH exported by the installer would otherwise be invisible
#: here — and the failure mode is a confusing "ffmpeg is not installed" while
#: ffmpeg sits two directories away.
MEDIA_BIN = Path(os.getenv("MEDIA_BIN", str(Path.home() / ".media" / "bin")))


def _prepare_path() -> None:
    """Prepend the installer's bin dirs to PATH, once, at import time.

    Prepending unconditionally is deliberate: a directory that does not exist yet
    is harmless on PATH, and re-running this cannot duplicate entries because the
    list is rebuilt from the current value only when something is actually new.
    """
    extra = [str(MEDIA_BIN), str(Path.home() / ".local" / "bin"), str(CACHE_DIR / "bin")]
    current = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    merged = [*extra, *[part for part in current if part not in extra]]
    os.environ["PATH"] = os.pathsep.join(merged)


_prepare_path()

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".ts", ".wmv"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus"}

#: Caption look for shorts: big, bold, high-contrast, safely above the UI chrome
#: that every short platform overlays at the bottom.
_CAPTION_STYLES: dict[str, dict[str, Any]] = {
    "shorts": {
        "font": "DejaVu Sans",
        "size": 64,
        "primary": "&H00FFFFFF",
        "outline": 4,
        "shadow": 0,
        "border": "&H00000000",
        "bold": 1,
        "margin_v": 320,
        "alignment": 2,
        "max_chars": 22,
        "max_lines": 2,
    },
    "clean": {
        "font": "DejaVu Sans",
        "size": 44,
        "primary": "&H00FFFFFF",
        "outline": 3,
        "shadow": 0,
        "border": "&H00000000",
        "bold": 1,
        "margin_v": 60,
        "alignment": 2,
        "max_chars": 42,
        "max_lines": 2,
    },
    "karaoke": {
        "font": "DejaVu Sans",
        "size": 60,
        "primary": "&H0000E5FF",
        "outline": 4,
        "shadow": 0,
        "border": "&H00000000",
        "bold": 1,
        "margin_v": 300,
        "alignment": 2,
        "max_chars": 20,
        "max_lines": 2,
    },
}

_HOOK_WORDS = (
    "how", "why", "what", "secret", "never", "always", "stop", "mistake",
    "truth", "nobody", "everyone", "money", "free", "fast", "best", "worst",
    "warning", "careful", "actually", "really", "here's", "heres", "biggest",
)


# --------------------------------------------------------------------------- #
# process helpers
# --------------------------------------------------------------------------- #
def _which(name: str) -> str | None:
    return shutil.which(name)


def run(
    cmd: list[str],
    *,
    timeout: int = 1800,
    cwd: str | Path | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing both streams, never raising on exit != 0."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        raise MediaError(f"{cmd[0]} is not installed in this sandbox") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{' '.join(cmd[:3])} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise MediaError(_tail(proc.stderr) or f"{cmd[0]} failed (exit {proc.returncode})")
    return proc


class MediaError(RuntimeError):
    """A failure the caller should see as a JSON error, not a traceback."""


def _tail(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, indent=1, default=str)
    sys.stdout.write("\n")


def ok(**payload: Any) -> int:
    body = {"ok": True}
    body.update(payload)
    emit(body)
    return 0


def fail(message: str, **payload: Any) -> int:
    body = {"ok": False, "error": message}
    body.update(payload)
    emit(body)
    return 1


def _require(tool: str) -> str:
    path = _which(tool)
    if not path:
        raise MediaError(
            f"{tool} is not installed. Run: media_cli.py install  (then poll media_cli.py status)"
        )
    return path


def _existing(path: str, *, kinds: str = "any") -> Path:
    p = Path(path).expanduser()
    if not p.is_file():
        raise MediaError(f"input file not found: {p}")
    suffix = p.suffix.lower()
    if kinds == "video" and suffix not in VIDEO_EXT:
        raise MediaError(f"not a video file: {p.name}")
    if kinds == "image" and suffix not in IMAGE_EXT:
        raise MediaError(f"not an image file: {p.name}")
    return p


def _out_path(out: str | None, src: Path, suffix: str, *, default_dir: Path | None = None) -> Path:
    if out:
        p = Path(out).expanduser()
    else:
        base = default_dir or src.parent
        p = base / f"{src.stem}{suffix}"
    if p.suffix == "":
        p = p.with_suffix(suffix)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _seconds(value: str | float | int | None) -> float:
    """Accept ``12``, ``12.5``, ``00:00:12``, ``01:02:03.5``."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    parts = text.split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError as exc:
        raise MediaError(f"cannot read a time from {value!r}") from exc
    total = 0.0
    for part in parts:
        total = total * 60 + part
    return total


def _clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    rest = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{rest:06.3f}"


def _srt_clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:
        millis = 0
        secs += 1
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _ass_clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    rest = seconds % 60
    return f"{hours:d}:{minutes:02d}:{rest:05.2f}"


# --------------------------------------------------------------------------- #
# ffmpeg / ffprobe primitives
# --------------------------------------------------------------------------- #
_ENCODE = [
    "-c:v", "libx264",
    "-preset", "slow",
    "-crf", "18",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac",
    "-b:a", "192k",
    "-movflags", "+faststart",
]


def probe(path: str | Path) -> dict[str, Any]:
    """ffprobe a file into the handful of facts every other action relies on."""
    ffprobe = _require("ffprobe")
    p = Path(path)
    proc = run([
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(p),
    ], timeout=120)
    if proc.returncode != 0:
        raise MediaError(f"ffprobe could not read {p.name}: {_tail(proc.stderr)}")
    raw = json.loads(proc.stdout or "{}")
    video = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in raw.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = raw.get("format", {}) or {}
    duration = float(fmt.get("duration") or (video or {}).get("duration") or 0.0)
    fps = 0.0
    if video and video.get("avg_frame_rate") and "/" in str(video["avg_frame_rate"]):
        num, _, den = str(video["avg_frame_rate"]).partition("/")
        try:
            fps = float(num) / float(den) if float(den) else 0.0
        except ValueError:
            fps = 0.0
    info: dict[str, Any] = {
        "path": str(p),
        "bytes": p.stat().st_size if p.exists() else 0,
        "duration": round(duration, 3),
        "duration_clock": _clock(duration),
        "has_video": video is not None,
        "has_audio": audio is not None,
    }
    if video:
        info["width"] = int(video.get("width") or 0)
        info["height"] = int(video.get("height") or 0)
        info["fps"] = round(fps, 3)
        info["frames"] = int(video.get("nb_frames") or 0) or round(duration * fps) if fps else 0
        info["video_codec"] = video.get("codec_name")
        info["pix_fmt"] = video.get("pix_fmt")
    if audio:
        info["audio_codec"] = audio.get("codec_name")
        info["audio_rate"] = int(audio.get("sample_rate") or 0)
        info["channels"] = int(audio.get("channels") or 0)
    return info


def _verify(path: Path, *, expect: dict[str, Any] | None = None) -> dict[str, Any]:
    """Probe a just-written artifact and complain if it is not real.

    ``expect`` values mean different things by type, because "verify" needs more
    than equality:

    * ``True``             the key must be present and truthy (a container that
                           claims a duration, a stream that exists)
    * ``{"value", "tolerance"}``  a numeric match within a tolerance. Needed for
                           duration after a stream copy, which can only land on
                           keyframes and so is *legitimately* a second or two off;
                           demanding equality there fails a correct output.
    * ``int``/``str``      exact match (a crop's width, a codec name)

    ORDER MATTERS: ``isinstance(True, int)`` is True in Python, so the bool branch
    has to come first. Getting this backwards made every ``expect={"duration":
    True}`` compare a float against ``True`` and reject a perfectly good file with
    "expected duration=True, got 4.09" — a failure that reads like an encoder bug.
    """
    if not path.exists():
        raise MediaError(f"{path} was not written")
    size = path.stat().st_size
    if size < 1024:
        raise MediaError(f"{path.name} is only {size} bytes — the write did not work")
    if path.suffix.lower() in IMAGE_EXT:
        return {"path": str(path), "bytes": size}
    info = probe(path)
    if not info.get("duration"):
        raise MediaError(f"{path.name} has no duration — the encode is not playable")
    for key, wanted in (expect or {}).items():
        actual = info.get(key)
        if isinstance(wanted, bool) or wanted is None:
            if wanted and not actual:
                raise MediaError(f"{path.name}: missing {key}")
            continue
        if isinstance(wanted, dict):
            target = wanted.get("value")
            tolerance = float(wanted.get("tolerance") or 0.0)
            if actual is None:
                raise MediaError(f"{path.name}: missing {key}")
            try:
                if abs(float(actual) - float(target)) > tolerance:
                    raise MediaError(
                        f"{path.name}: expected {key} about {target} (+/-{tolerance}), got {actual}"
                    )
            except (TypeError, ValueError):
                if actual != target:
                    raise MediaError(f"{path.name}: expected {key}={target}, got {actual}")
            continue
        if isinstance(wanted, (int, float)):
            if actual != wanted:
                raise MediaError(f"{path.name}: expected {key}={wanted}, got {actual}")
            continue
        if actual != wanted:
            raise MediaError(f"{path.name}: expected {key}={wanted}, got {actual}")
    return info


#: libass/ffmpeg messages that mean "the caption was NOT drawn".
#:
#: The dangerous case is a missing font: ffmpeg exits 0 and the output is a
#: perfectly good video with NO captions, so the feature looks broken with no error
#: anywhere — the same class of silent failure as a stale CDN file. These strings
#: are matched against the render's own stderr (which is why the caption encodes run
#: at ``-v warning`` instead of ``-v error``) and surfaced as ``caption_warnings``.
_CAPTION_ALARM_MARKERS = (
    "fontselect",
    "unable to find a suitable font",
    "failed to find any fallback",
    "no usable font",
    "glyph 0x",
)


def _caption_warnings(stderr: str) -> list[str]:
    found: list[str] = []
    for line in (stderr or "").splitlines():
        line = line.strip()
        if line and any(marker in line.lower() for marker in _CAPTION_ALARM_MARKERS):
            found.append(line[:220])
    return found[:8]


def _caption_font() -> str | None:
    """The first caption font that actually exists, in the CLI's search order."""
    for candidate in (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path.home() / ".local/share/fonts/DejaVuSans-Bold.ttf",
        CACHE_DIR / "fonts" / "DejaVuSans-Bold.ttf",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _video_encoder_args(*, crf: int = 18, preset: str = "slow") -> list[str]:
    return [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
    ]


# --------------------------------------------------------------------------- #
# doctor / install
# --------------------------------------------------------------------------- #
def cmd_doctor(args: argparse.Namespace) -> int:
    tools: dict[str, Any] = {}
    for name, version_args in (
        ("ffmpeg", ["-version"]),
        ("ffprobe", ["-version"]),
        ("yt-dlp", ["--version"]),
        ("rembg", ["--version"]),
    ):
        path = _which(name)
        entry: dict[str, Any] = {"present": bool(path), "path": path}
        if path:
            proc = run([path, *version_args], timeout=60)
            entry["version"] = (proc.stdout or proc.stderr).strip().splitlines()[0][:120]
        tools[name] = entry

    python_deps: dict[str, Any] = {}
    for module in ("PIL", "numpy", "faster_whisper", "rembg", "cv2"):
        proc = run([sys.executable, "-c", f"import {module}; print(getattr({module}, '__version__', 'ok'))"], timeout=120)
        python_deps[module] = proc.stdout.strip() if proc.returncode == 0 else None

    fonts = [p for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        str(Path.home() / ".local/share/fonts/DejaVuSans-Bold.ttf"),
        str(CACHE_DIR / "fonts" / "DejaVuSans-Bold.ttf"),
    ) if Path(p).is_file()]

    whisper_models = sorted(p.name for p in CACHE_DIR.glob("whisper-*")) if CACHE_DIR.is_dir() else []
    gpu = Path("/dev/nvidia0").exists()
    ready = bool(_which("ffmpeg") and _which("ffprobe") and _which("yt-dlp"))
    return ok(
        ready=ready,
        gpu=gpu,
        cache_dir=str(CACHE_DIR),
        tools=tools,
        python=python_deps,
        caption_fonts=fonts,
        whisper_models=whisper_models,
        report=(
            "media workshop ready"
            if ready and python_deps.get("faster_whisper") and python_deps.get("PIL")
            else "incomplete: run media_cli.py install, then poll media_cli.py status"
        ),
    )


def _install_marker() -> Path:
    return CACHE_DIR / "install.done"


def _install_log() -> Path:
    return CACHE_DIR / "install.log"


def cmd_install(args: argparse.Namespace) -> int:
    """Kick off the sandbox installer detached (it takes minutes) and return."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log = _install_log()
    installer = _installer_path()
    if args.foreground:
        proc = run(["bash", str(installer)], timeout=3600)
        return ok(returncode=proc.returncode, log=_tail(proc.stdout + proc.stderr, 2000))
    if not installer.is_file():
        raise MediaError(f"installer not found at {installer} — bootstrap it first")
    _install_marker().unlink(missing_ok=True)
    with log.open("w") as handle:
        subprocess.Popen(
            ["bash", str(installer)],
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return ok(
        started=True,
        log=str(log),
        note="poll with: media_cli.py status  (the install takes a few minutes)",
    )


def _installer_path() -> Path:
    here = Path(__file__).resolve().parent
    candidate = here / "install_media_sandbox.sh"
    if candidate.is_file():
        return candidate
    return Path.home() / ".media" / "bin" / "install_media_sandbox.sh"


def _install_finished() -> bool:
    return _install_marker().is_file() or bool(_which("ffmpeg") and _which("ffprobe"))


def cmd_status(args: argparse.Namespace) -> int:
    """Report install progress, optionally WAITING for it.

    ``--wait`` exists because the alternative is the caller sleeping between
    sandbox round trips (or, worse, telling the user to come back later). One
    bounded command that watches the marker is cheaper and cannot be mistaken for
    "nothing happened yet".
    """
    log = _install_log()
    wait = max(0.0, float(getattr(args, "wait", 0.0) or 0.0))
    deadline = time.time() + wait
    while True:
        finished = _install_finished()
        if finished or time.time() >= deadline:
            break
        time.sleep(min(4.0, max(0.25, deadline - time.time())))

    running = run(["bash", "-lc", "pgrep -f install_media_sandbox.sh >/dev/null && echo yes || echo no"], timeout=30)
    tail = ""
    if log.is_file():
        text = log.read_text(errors="replace")
        tail = _tail(text, 2500)
    ready = bool(_which("ffmpeg") and _which("ffprobe"))
    summary: dict[str, Any] = {}
    summary_path = CACHE_DIR / "install.summary.json"
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            summary = {}
    return ok(
        installing=running.stdout.strip() == "yes",
        done=_install_marker().is_file(),
        ready=ready,
        waited=round(wait, 1),
        log=str(log),
        tail=tail,
        summary=summary,
        note=(
            "ready: ffmpeg and ffprobe resolve in this sandbox"
            if ready
            else "not ready yet — poll status again"
        ),
    )


# --------------------------------------------------------------------------- #
# detached jobs
#
# WHY THIS EXISTS: the sandbox caps ONE command at 900 s, but a real request is
# routinely longer than that — a background-removal pass at 30 fps, a whisper
# transcription of a 40-minute recording, a 1080p re-encode, a YouTube download.
# Running those inline means the command is killed mid-write and the caller gets
# NO output at all, which reads as "the tool is broken" rather than "it needed
# longer". So the tool launches the action detached, redirects its stdout to a
# result file, and reports on it through this action:
#
#   done    <- the launcher appended ``media_cli exit=<code>`` to the log
#   result  <- the action's own JSON, verbatim, once the exit line is present
#
# The exit line is what makes ``done`` trustworthy: the result file is written by
# the action itself and can exist in a half-flushed state, so its mere presence is
# not evidence of completion. Reading the file only after the sentinel avoids
# reporting a truncated JSON as a finished job.
# --------------------------------------------------------------------------- #
_JOB_SENTINEL = "media_cli exit="


def cmd_job(args: argparse.Namespace) -> int:
    result = Path(args.result).expanduser()
    log = Path(args.log).expanduser() if args.log else result.with_suffix(".log")
    deadline = time.time() + max(0.0, float(args.wait or 0.0))
    text = ""
    while True:
        text = log.read_text(errors="replace") if log.is_file() else ""
        if _JOB_SENTINEL in text:
            break
        if time.time() >= deadline:
            break
        time.sleep(min(3.0, max(0.25, deadline - time.time())))

    match = re.search(rf"{re.escape(_JOB_SENTINEL)}(\d+)", text)
    exit_code = int(match.group(1)) if match else None
    done = match is not None

    if done and result.is_file():
        raw = result.read_text(errors="replace").strip()
        structured = None
        if raw:
            try:
                structured = json.loads(raw)
            except json.JSONDecodeError:
                structured = None
        if isinstance(structured, dict):
            return ok(job_done=True, exit_code=exit_code, result=structured)
        return ok(job_done=True, exit_code=exit_code, result=None,
                  output_tail=_tail(raw, 1500))

    return ok(
        job_done=False,
        exit_code=exit_code,
        still_running=not done,
        result=str(result),
        log=str(log),
        tail=_tail(text, 1500),
    )


# --------------------------------------------------------------------------- #
# inspect / watch
# --------------------------------------------------------------------------- #
def cmd_probe(args: argparse.Namespace) -> int:
    return ok(**probe(_existing(args.input)))


def cmd_frames(args: argparse.Namespace) -> int:
    """Pull stills out so the agent can actually look at the video."""
    ffmpeg = _require("ffmpeg")
    src = _existing(args.input, kinds="video")
    out = Path(args.out or f"{src.stem}_frames").expanduser()
    out.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    count = max(1, int(args.count or 9))
    duration = info.get("duration") or 0.0
    if duration <= 0:
        raise MediaError("video has no duration; cannot sample frames")
    step = duration / (count + 1)
    written: list[str] = []
    for index in range(1, count + 1):
        target = out / f"frame_{index:02d}.jpg"
        run([
            ffmpeg, "-v", "error", "-ss", f"{step * index:.3f}", "-i", str(src),
            "-frames:v", "1", "-q:v", "2", "-vf", "scale='min(1280,iw)':-2", "-y", str(target),
        ], timeout=300)
        if target.is_file():
            written.append(str(target))
    if not written:
        raise MediaError("no frames were extracted")
    return ok(input=str(src), duration=info["duration"], frames=written)


def cmd_watch(args: argparse.Namespace) -> int:
    """A contact sheet: one image with N stills tiled, so the model can see it."""
    ffmpeg = _require("ffmpeg")
    src = _existing(args.input, kinds="video")
    info = probe(src)
    count = max(1, int(args.count or 12))
    columns = max(1, int(args.columns or min(4, count)))
    rows = math.ceil(count / columns)
    tile_w = max(160, int(args.width or 480))
    out = _out_path(args.out, src, ".png")
    # `fps=` is derived from the real duration so the tiles are evenly spaced
    # regardless of container oddities, and `-frames:v 1` guarantees one image.
    fps = count / info["duration"] if info.get("duration") else 0.1
    vf = (
        f"fps={fps:.6f},scale={tile_w}:-2,tile={columns}x{rows}:padding=6:margin=6:color=black"
    )
    if args.timestamps:
        vf = (
            f"fps={fps:.6f},scale={tile_w}:-2,drawtext="
            "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            "text='%{pts\\:hms}':x=8:y=8:fontsize=20:fontcolor=white:box=1:boxcolor=black@0.6,"
            f"tile={columns}x{rows}:padding=6:margin=6:color=black"
        )
    proc = run([
        ffmpeg, "-v", "error", "-i", str(src), "-vf", vf, "-frames:v", "1", "-y", str(out),
    ], timeout=900)
    if proc.returncode != 0:
        raise MediaError(f"contact sheet failed: {_tail(proc.stderr)}")
    return ok(
        input=str(src), sheet=str(out), tiles=count, columns=columns, rows=rows,
        probe=probe(src), bytes=out.stat().st_size,
    )


# --------------------------------------------------------------------------- #
# edit
# --------------------------------------------------------------------------- #
def cmd_trim(args: argparse.Namespace) -> int:
    src = _existing(args.input, kinds="video")
    info = probe(src)
    start = _seconds(args.start)
    end = _seconds(args.end) if args.end else 0.0
    duration = _seconds(args.duration) if args.duration else 0.0
    if end and end <= start:
        raise MediaError("--end must come after --start")
    if not end and not duration:
        end = info.get("duration") or 0.0
    if duration and not end:
        end = min(start + duration, info.get("duration") or start + duration)
    out = _out_path(args.out, src, ".mp4")
    ffmpeg = _require("ffmpeg")
    if args.fast:
        # Stream copy: instant, but only honest when the start is near a
        # keyframe, so the result is probed and reported rather than assumed.
        cmd = [ffmpeg, "-v", "error", "-ss", f"{start:.3f}", "-i", str(src),
               "-t", f"{max(0.05, end - start):.3f}", "-c", "copy", "-avoid_negative_ts", "make_zero",
               "-movflags", "+faststart", "-y", str(out)]
    else:
        cmd = [ffmpeg, "-v", "error", "-ss", f"{start:.3f}", "-i", str(src),
               "-t", f"{max(0.05, end - start):.3f}", *_video_encoder_args(), "-y", str(out)]
    proc = run(cmd, timeout=1800)
    if proc.returncode != 0:
        raise MediaError(f"trim failed: {_tail(proc.stderr)}")
    # The tolerance is mode-aware, and that is what makes the verification
    # trustworthy: an ENCODE is frame-exact (0.4 s of container slack), while a
    # stream COPY can only cut on a keyframe and is routinely 1-2 s longer than
    # asked. One shared tolerance would either fail correct copies or hide a real
    # encoder fault, so the mode picks it and the actual duration is reported.
    tolerance = 2.0 if args.fast else 0.4
    verified = _verify(
        out,
        expect={
            "width": info.get("width"),
            "height": info.get("height"),
            "duration": {"value": round(end - start, 3), "tolerance": tolerance},
        },
    )
    return ok(input=str(src), output=str(out), start=round(start, 3), end=round(end, 3),
              mode="copy" if args.fast else "encode",
              duration_tolerance=tolerance, output_probe=verified)


def cmd_crop(args: argparse.Namespace) -> int:
    ffmpeg = _require("ffmpeg")
    src = _existing(args.input, kinds="video")
    info = probe(src)
    width, height = int(info.get("width") or 0), int(info.get("height") or 0)
    if not width or not height:
        raise MediaError("input has no video dimensions")
    focus_x: float | None = None
    if args.aspect:
        want_w, _, want_h = args.aspect.partition(":")
        ratio = float(want_w) / float(want_h)
        current = width / height
        if ratio <= current:                     # target is narrower: crop width
            crop_h = height
            crop_w = int(round(height * ratio)) // 2 * 2
        else:                                    # target is wider: crop height
            crop_w = width
            crop_h = int(round(width / ratio)) // 2 * 2
        if args.focus == "face":
            focus_x = _face_center_x(src, width, height)
        x = _anchor_x(args.anchor, width, crop_w, focus_x)
        y = _anchor_y(args.anchor, height, crop_h)
    else:
        crop_w = int(args.width or width)
        crop_h = int(args.height or height)
        if crop_w > width or crop_h > height:
            raise MediaError(f"crop {crop_w}x{crop_h} is larger than the source {width}x{height}")
        x = int(args.x) if args.x is not None else max(0, (width - crop_w) // 2)
        y = int(args.y) if args.y is not None else max(0, (height - crop_h) // 2)
    out = _out_path(args.out, src, ".mp4")
    proc = run([
        ffmpeg, "-v", "error", "-i", str(src),
        "-vf", f"crop={crop_w}:{crop_h}:{x}:{y}", *_video_encoder_args(), "-y", str(out),
    ], timeout=1800)
    if proc.returncode != 0:
        raise MediaError(f"crop failed: {_tail(proc.stderr)}")
    verified = _verify(out)
    if verified.get("width") != crop_w or verified.get("height") != crop_h:
        raise MediaError(
            f"crop verification failed: asked {crop_w}x{crop_h}, got "
            f"{verified.get('width')}x{verified.get('height')}"
        )
    return ok(input=str(src), output=str(out), crop={"w": crop_w, "h": crop_h, "x": x, "y": y},
              source={"width": width, "height": height}, output_probe=verified)


def _anchor_x(anchor: str, width: int, crop_w: int, focus_x: float | None) -> int:
    if focus_x is not None:
        return int(max(0, min(width - crop_w, round(focus_x - crop_w / 2))))
    if anchor in {"left", "west"}:
        return 0
    if anchor in {"right", "east"}:
        return max(0, width - crop_w)
    return max(0, (width - crop_w) // 2)


def _anchor_y(anchor: str, height: int, crop_h: int) -> int:
    if anchor in {"top", "north"}:
        return 0
    if anchor in {"bottom", "south"}:
        return max(0, height - crop_h)
    return max(0, (height - crop_h) // 2)


def _face_center_x(src: Path, width: int, height: int) -> float | None:
    """Where the subject is, so a vertical crop does not cut them in half.

    Uses OpenCV's bundled frontal-face cascade on a few sampled frames and takes
    the median centre. Deliberately best-effort: a talking-head clip needs it,
    a screen recording does not, and a missing cv2 must not fail the crop.
    """
    try:
        import cv2  # noqa: PLC0415
    except Exception:
        return None
    try:
        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        if not cascade_path.is_file():
            return None
        detector = cv2.CascadeClassifier(str(cascade_path))
        if detector.empty():
            return None
        info = probe(src)
        samples = [0.1, 0.3, 0.5, 0.7, 0.9]
        centres: list[float] = []
        import numpy as np  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory(prefix="media_faces_") as tmp:
            for index, fraction in enumerate(samples):
                stamp = max(0.0, (info.get("duration") or 0) * fraction)
                # Write the frame to a FILE, never to stdout.
                #
                # MEASURED HAZARD: piping JPEG bytes through the shared ``run()``
                # helper means decoding binary data as text (``text=True``), which
                # both mangles every byte >= 0x80 on a non-latin1 locale and can
                # raise UnicodeDecodeError inside subprocess. A temp file sidesteps
                # the whole class of bug and reads bytes natively.
                frame_path = Path(tmp) / f"face_{index:02d}.jpg"
                run([
                    _require("ffmpeg"), "-v", "error", "-ss", f"{stamp:.3f}", "-i", str(src),
                    "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", "-y", str(frame_path),
                ], timeout=180)
                if not frame_path.is_file():
                    continue
                try:
                    raw = frame_path.read_bytes()
                except OSError:
                    continue
                if not raw:
                    continue
                frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
                if frame is None:
                    continue
                faces = detector.detectMultiScale(frame, 1.1, 5, minSize=(48, 48))
                if len(faces):
                    scaled = width / frame.shape[1]
                    x_centres = [float(fx + fw / 2) * scaled for fx, _, fw, _ in faces]
                    centres.append(sum(x_centres) / len(x_centres))
        if not centres:
            return None
        centres.sort()
        return centres[len(centres) // 2]
    except Exception:
        return None


def cmd_scale(args: argparse.Namespace) -> int:
    """Resize, and when growing, sharpen — an upscale that is not soft."""
    ffmpeg = _require("ffmpeg")
    src = _existing(args.input, kinds="video")
    info = probe(src)
    src_h = int(info.get("height") or 0)
    target_h = int(args.height) if args.height else 0
    if not target_h and args.width:
        target_w = int(args.width)
        target_h = max(2, round(target_w * (src_h / (info.get("width") or 1))))
    if not target_h:
        presets = {"hd": 1080, "fhd": 1080, "2k": 1440, "4k": 2160}
        target_h = presets.get(str(args.mode).lower(), 1080)
    if src_h and target_h < src_h and not args.allow_downscale:
        raise MediaError(
            f"refusing to shrink {src_h}p to {target_h}p (pass --allow-downscale to force)"
        )
    chain = [f"scale=-2:{target_h}:flags=lanczos"]
    if src_h and target_h > src_h:
        chain.append("unsharp=5:5:0.8:5:5:0.0")
    out = _out_path(args.out, src, ".mp4")
    proc = run([ffmpeg, "-v", "error", "-i", str(src), "-vf", ",".join(chain),
                *_video_encoder_args(crf=args.crf, preset=args.preset), "-y", str(out)], timeout=3600)
    if proc.returncode != 0:
        raise MediaError(f"scale failed: {_tail(proc.stderr)}")
    verified = _verify(out)
    if verified.get("height") != target_h:
        raise MediaError(f"scale verification failed: wanted {target_h}p, got {verified.get('height')}p")
    return ok(input=str(src), output=str(out), upscaled=bool(src_h and target_h > src_h),
              from_height=src_h, to_height=target_h, output_probe=verified)


def cmd_concat(args: argparse.Namespace) -> int:
    """Join clips through the concat demuxer, normalising them first.

    Mixed sources (phone clip + screen recording, 30fps + 60fps, 720p + 4K) are
    the normal case, and the concat *demuxer* silently mangles them. So each
    input is re-encoded to one common shape, then joined — with the shape
    reported so the caller sees what it will get.
    """
    ffmpeg = _require("ffmpeg")
    if len(args.inputs) < 2:
        raise MediaError("concat needs at least two inputs")
    sources = [_existing(i, kinds="video") for i in args.inputs]
    target_h = int(args.height) if args.height else max(
        int(probe(s).get("height") or 0) for s in sources
    )
    if target_h % 2:
        target_h += 1
    work = Path(args.work_dir or (sources[0].parent / ".media_concat"))
    work.mkdir(parents=True, exist_ok=True)
    normalised: list[Path] = []
    for index, source in enumerate(sources):
        piece = work / f"part_{index:03d}.mp4"
        proc = run([
            ffmpeg, "-v", "error", "-i", str(source),
            "-vf", f"scale=-2:{target_h}:flags=lanczos,setsar=1",
            "-r", str(args.fps), *_video_encoder_args(crf=args.crf), "-y", str(piece),
        ], timeout=3600)
        if proc.returncode != 0:
            raise MediaError(f"normalising {source.name} failed: {_tail(proc.stderr)}")
        normalised.append(piece)
    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in normalised))
    out = _out_path(args.out, sources[0], ".mp4")
    proc = run([
        ffmpeg, "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", "-movflags", "+faststart", "-y", str(out),
    ], timeout=1800)
    if proc.returncode != 0:
        raise MediaError(f"concat failed: {_tail(proc.stderr)}")
    verified = _verify(out)
    return ok(outputs=[str(s) for s in sources], output=str(out), height=target_h, fps=args.fps,
              output_probe=verified)


def cmd_extract_audio(args: argparse.Namespace) -> int:
    ffmpeg = _require("ffmpeg")
    src = _existing(args.input)
    suffix = {"mp3": ".mp3", "wav": ".wav", "m4a": ".m4a", "flac": ".flac"}[args.format]
    out = _out_path(args.out, src, suffix)
    codec = {
        "mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
        "wav": ["-c:a", "pcm_s16le", "-ar", "16000"],
        "m4a": ["-c:a", "aac", "-b:a", "192k"],
        "flac": ["-c:a", "flac"],
    }[args.format]
    cmd = [ffmpeg, "-v", "error", "-i", str(src)]
    if args.mono:
        cmd += ["-ac", "1"]
    cmd += ["-vn", *codec, "-y", str(out)]
    proc = run(cmd, timeout=1800)
    if proc.returncode != 0:
        raise MediaError(f"audio extraction failed: {_tail(proc.stderr)}")
    return ok(input=str(src), output=str(out), output_probe=_verify(out))


def cmd_transcribe(args: argparse.Namespace) -> int:
    """Whisper, locally. No API key, no per-minute cost."""
    src = _existing(args.input)
    audio = src
    if src.suffix.lower() in VIDEO_EXT:
        audio = Path(args.work_audio or (CACHE_DIR / f"{src.stem}.16k.wav"))
        audio.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = _require("ffmpeg")
        proc = run([ffmpeg, "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", "-y", str(audio)], timeout=1800)
        if proc.returncode != 0:
            raise MediaError(f"could not extract audio for transcription: {_tail(proc.stderr)}")
    cache = Path(args.cache or (CACHE_DIR / "whisper"))
    cache.mkdir(parents=True, exist_ok=True)
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
    except Exception as exc:
        raise MediaError(
            "faster-whisper is not installed. Run: media_cli.py install"
        ) from exc
    device = "cuda" if Path("/dev/nvidia0").exists() else "cpu"
    compute = "float16" if device == "cuda" else "int8"
    started = time.time()
    model = WhisperModel(
        args.model, device=device, compute_type=compute, download_root=str(cache)
    )
    segments, info = model.transcribe(
        str(audio),
        language=None if args.language in (None, "auto") else args.language,
        vad_filter=not args.no_vad,
        beam_size=int(args.beam),
        word_timestamps=True,
    )
    words: list[dict[str, Any]] = []
    cues: list[dict[str, Any]] = []
    for segment in segments:
        cues.append({
            "start": round(float(segment.start), 3),
            "end": round(float(segment.end), 3),
            "text": (segment.text or "").strip(),
        })
        for word in getattr(segment, "words", None) or []:
            words.append({
                "start": round(float(word.start), 3),
                "end": round(float(word.end), 3),
                "word": (word.word or "").strip(),
                "probability": round(float(getattr(word, "probability", 0.0) or 0.0), 3),
            })
    payload = {
        "language": getattr(info, "language", None),
        "duration": round(float(getattr(info, "duration", 0.0) or 0.0), 3),
        "segments": cues,
        "words": words,
        "model": args.model,
        "elapsed": round(time.time() - started, 2),
    }
    written: list[str] = []
    if args.format in {"json", "all"}:
        out_json = Path(args.out).expanduser() if args.out and args.format == "json" else src.with_suffix(".transcript.json")
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(payload, indent=1))
        written.append(str(out_json))
    if args.format in {"srt", "all"}:
        out_srt = Path(args.out).expanduser() if args.out and args.format == "srt" else src.with_suffix(".srt")
        out_srt.parent.mkdir(parents=True, exist_ok=True)
        out_srt.write_text(build_srt(cues, max_chars=int(args.max_chars), max_lines=int(args.max_lines)))
        written.append(str(out_srt))
    if args.format in {"txt", "all"}:
        out_txt = Path(args.out).expanduser() if args.out and args.format == "txt" else src.with_suffix(".txt")
        out_txt.parent.mkdir(parents=True, exist_ok=True)
        out_txt.write_text("\n".join(cue["text"] for cue in cues if cue["text"]) + "\n")
        written.append(str(out_txt))
    return ok(
        input=str(src), outputs=written, language=payload["language"],
        duration=payload["duration"], segments=len(cues), words=len(words),
        elapsed=payload["elapsed"], model=args.model,
        preview=[cue["text"] for cue in cues[:5]],
    )


def build_srt(cues: Iterable[dict[str, Any]], *, max_chars: int = 42, max_lines: int = 2) -> str:
    """Wrap cues into a readable .srt, splitting long lines the way a human would."""
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        text = " ".join(str(cue.get("text") or "").split())
        if not text:
            continue
        wrapped = textwrap.wrap(text, width=max_chars) or [text]
        lines = wrapped[: max(1, max_lines * 2)]
        blocks.append(
            f"{index}\n{_srt_clock(float(cue['start']))} --> {_srt_clock(float(cue['end']))}\n"
            + "\n".join(lines)
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def cmd_captions(args: argparse.Namespace) -> int:
    """Burn captions in. ffmpeg renders them, so they are in the pixels."""
    ffmpeg = _require("ffmpeg")
    src = _existing(args.input, kinds="video")
    style = _CAPTION_STYLES.get(args.style, _CAPTION_STYLES["shorts"])
    cues: list[dict[str, Any]]
    if args.srt:
        cues = parse_srt(Path(args.srt).expanduser().read_text(errors="replace"))
    else:
        transcript = src.with_suffix(".transcript.json")
        if not transcript.is_file():
            raise MediaError(
                "no --srt and no sidecar transcript. Run: media_cli.py transcribe "
                f"{src.name} --format json"
            )
        data = json.loads(transcript.read_text())
        cues = data.get("segments") or []
    if not cues:
        raise MediaError("no caption cues to burn")
    cues = [cue for cue in cues if cue.get("text")]
    ass = Path(args.ass_out or (src.parent / f"{src.stem}.{args.style}.ass"))
    ass.parent.mkdir(parents=True, exist_ok=True)
    ass.write_text(build_ass(cues, style=style, shift=_seconds(args.shift) if args.shift else 0.0))
    out_prefix = args.out_prefix or str(src.parent)
    out = _out_path(args.out, src, ".mp4", default_dir=Path(out_prefix))
    # Escape for the filtergraph: ':' and '\' inside a filename break subtitles=.
    esc = str(ass).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    proc = run([
        ffmpeg, "-v", "warning", "-i", str(src), "-vf", f"subtitles='{esc}'",
        *_video_encoder_args(crf=args.crf, preset=args.preset), "-y", str(out),
    ], timeout=3600)
    if proc.returncode != 0:
        raise MediaError(f"caption burn failed: {_tail(proc.stderr)}")
    verified = _verify(out)
    warnings = _caption_warnings(proc.stderr)
    return ok(
        input=str(src), output=str(out), ass=str(ass), cues=len(cues), style=args.style,
        font=_caption_font(), caption_warnings=warnings,
        captions_ok=not warnings,
        note=(None if not warnings else
              "ffmpeg warned that the caption font/glyphs could not be resolved, so the "
              "burned captions may be missing even though the video is fine."),
        output_probe=verified,
    )


def parse_srt(text: str) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        timing = next((line for line in lines if "-->" in line), None)
        if not timing:
            continue
        start_text, _, end_text = timing.partition("-->")
        body = " ".join(lines[lines.index(timing) + 1:]).strip()
        cues.append({
            "start": _srt_seconds(start_text),
            "end": _srt_seconds(end_text),
            "text": body,
        })
    return cues


def _srt_seconds(text: str) -> float:
    cleaned = text.strip().replace(",", ".")
    return _seconds(cleaned)


def build_ass(cues: Iterable[dict[str, Any]], *, style: dict[str, Any], shift: float = 0.0) -> str:
    """An .ass script with a readable, high-contrast caption style."""
    max_chars = int(style.get("max_chars") or 40)
    max_lines = int(style.get("max_lines") or 2)
    head = f"""[Script Info]
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{style['font']},{style['size']},{style['primary']},&H000000FF,{style['border']},&H64000000,{style['bold']},0,0,0,100,100,0,0,1,{style['outline']},{style['shadow']},{style['alignment']},60,60,{style['margin_v']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows: list[str] = []
    for cue in cues:
        text = " ".join(str(cue.get("text") or "").split())
        if not text:
            continue
        lines = textwrap.wrap(text, width=max_chars)[:max_lines]
        joined = "\\N".join(lines)
        start = _ass_clock(max(0.0, float(cue["start"]) + shift))
        end = _ass_clock(max(0.0, float(cue["end"]) + shift))
        if end == start:
            continue
        rows.append(f"Dialogue: 0,{start},{end},Cap,,0,0,0,,{joined}")
    return head + "\n".join(rows) + "\n"


# --------------------------------------------------------------------------- #
# background removal
# --------------------------------------------------------------------------- #
def cmd_bg(args: argparse.Namespace) -> int:
    """Pull the subject out, image or video, with alpha kept where it can be."""
    src = _existing(args.input)
    is_video = src.suffix.lower() in VIDEO_EXT
    if not _which("rembg"):
        raise MediaError("rembg is not installed. Run: media_cli.py install")
    model = args.model
    if is_video:
        return _bg_video(src, args, model)
    out = _out_path(args.out, src, ".png")
    cmd = ["rembg", "i", "-m", model]
    if args.alpha_matting:
        cmd += ["-a", "-af", str(args.foreground_threshold), "-ab", str(args.background_threshold),
                "-ae", str(args.erode_size)]
    cmd += [str(src), str(out)]
    proc = run(cmd, timeout=1800)
    if proc.returncode != 0:
        raise MediaError(f"background removal failed: {_tail(proc.stderr)}")
    return ok(input=str(src), output=str(out), model=model, alpha=True,
              bytes=out.stat().st_size)


def _bg_video(src: Path, args: argparse.Namespace, model: str) -> int:
    ffmpeg = _require("ffmpeg")
    info = probe(src)
    fps = float(info.get("fps") or 25.0)
    work = Path(args.work_dir or (CACHE_DIR / f"bg_{src.stem}"))
    frames_dir = work / "in"
    cut_dir = work / "out"
    for d in (frames_dir, cut_dir):
        d.mkdir(parents=True, exist_ok=True)
    # Frame-accurate extraction: alpha video is only convincing if every frame
    # gets the same treatment, so nothing is skipped for speed.
    proc = run([
        ffmpeg, "-v", "error", "-i", str(src),
        "-vf", f"fps={fps}", "-q:v", "2", str(frames_dir / "f_%06d.jpg"),
    ], timeout=3600)
    if proc.returncode != 0:
        raise MediaError(f"frame extraction for matting failed: {_tail(proc.stderr)}")
    frames = sorted(frames_dir.glob("f_*.jpg"))
    if not frames:
        raise MediaError("no frames were extracted for matting")
    batch = max(1, int(args.batch))
    for index in range(0, len(frames), batch):
        chunk = frames[index:index + batch]
        cmd = ["rembg", "p", "-m", model]
        if args.alpha_matting:
            cmd += ["-a", "-af", str(args.foreground_threshold), "-ab", str(args.background_threshold),
                    "-ae", str(args.erode_size)]
        cmd += [str(f) for f in chunk]
        proc = run(cmd, timeout=3600, cwd=str(cut_dir))
        if proc.returncode != 0:
            raise MediaError(f"matting batch {index // batch} failed: {_tail(proc.stderr)}")
    cutouts = sorted(p for p in cut_dir.glob("*.png"))
    if not cutouts:
        raise MediaError("rembg produced no cutout frames")
    if args.background:
        # Compositing onto an opaque colour is what most "green screen" requests
        # actually want, and it survives every platform's player.
        suffix = ".mp4"
        out = _out_path(args.out, src, suffix)
        colour = args.background.lstrip("#")
        proc = run([
            ffmpeg, "-v", "error", "-framerate", f"{fps}", "-i", str(cut_dir / "f_%06d.png"),
            "-f", "lavfi", "-i", f"color=c=0x{colour}:s={info.get('width')}x{info.get('height')}:r={fps}",
            "-filter_complex", "[1:v][0:v]overlay=shortest=1,format=yuv420p",
            "-t", f"{info.get('duration') or 0:.3f}", *_video_encoder_args(), "-y", str(out),
        ], timeout=3600)
        if proc.returncode != 0:
            raise MediaError(f"compositing the cutout failed: {_tail(proc.stderr)}")
        if info.get("has_audio"):
            # Put the original audio back: a silent cutout is a bug, not alpha.
            merged = out.with_name(f"{out.stem}.audio{out.suffix}")
            with_audio = run([
                ffmpeg, "-v", "error", "-i", str(out), "-i", str(src),
                "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy", "-c:a", "aac",
                "-b:a", "192k", "-shortest", "-movflags", "+faststart", "-y", str(merged),
            ], timeout=1800)
            if with_audio.returncode == 0:
                merged.replace(out)
        return ok(input=str(src), output=str(out), frames=len(cutouts), model=model,
                  alpha=False, background=args.background, output_probe=_verify(out))
    out = _out_path(args.out, src, ".webm")
    proc = run([
        ffmpeg, "-v", "error", "-framerate", f"{fps}", "-i", str(cut_dir / "f_%06d.png"),
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-crf", "18", "-b:v", "0",
        "-row-mt", "1", "-auto-alt-ref", "0", "-t", f"{info.get('duration') or 0:.3f}", "-y", str(out),
    ], timeout=7200)
    if proc.returncode != 0:
        raise MediaError(f"alpha encode failed: {_tail(proc.stderr)}")
    return ok(input=str(src), output=str(out), frames=len(cutouts), model=model, alpha=True,
              output_probe=_verify(out))


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
def cmd_download(args: argparse.Namespace) -> int:
    """yt-dlp, the one tool that reliably turns a link into a file."""
    ytdlp = _require("yt-dlp")
    out_dir = Path(args.out or "downloads").expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / "%(title).120B [%(id)s].%(ext)s")
    height = {"best": 0, "1080": 1080, "720": 720, "480": 480}.get(str(args.quality), 1080)
    cmd = [
        ytdlp, "--no-playlist" if not args.playlist else "--yes-playlist",
        "--no-warnings", "--newline", "--no-progress",
        "-f", ("bestvideo[height<=%d]+bestaudio/best[height<=%d]/best" % (height, height)) if height else "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", template,
    ]
    if args.audio_only:
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    if args.cookies:
        cmd += ["--cookies", args.cookies]
    if args.section:
        cmd += ["--download-sections", f"*{args.section}"]
    if args.subtitles:
        cmd += ["--write-auto-subs", "--sub-langs", args.subtitles, "--convert-subs", "srt"]
    cmd.append(args.url)
    proc = run(cmd, timeout=int(args.timeout))
    if proc.returncode != 0:
        raise MediaError(
            f"download failed: {_tail(proc.stderr or proc.stdout)}"
        )
    produced = sorted(
        (p for p in out_dir.iterdir() if p.is_file() and p.stem != ""),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    newest = produced[0] if produced else None
    result: dict[str, Any] = {
        "url": args.url,
        "directory": str(out_dir),
        "files": [str(p) for p in produced[:6]],
    }
    if newest is not None:
        result["output"] = str(newest)
        if newest.suffix.lower() in VIDEO_EXT:
            result["output_probe"] = probe(newest)
    return ok(**result)


# --------------------------------------------------------------------------- #
# shorts
# --------------------------------------------------------------------------- #
def plan_shorts(
    cues: list[dict[str, Any]],
    *,
    count: int = 3,
    min_seconds: float = 15.0,
    max_seconds: float = 45.0,
    total_duration: float = 0.0,
) -> list[dict[str, Any]]:
    """Pick the windows worth publishing, with the reason attached.

    Sentence boundaries and breath pauses do the cutting — a short that starts
    mid-word is the single most obvious sign of an automated edit. Windows are
    then scored on how much is actually said per second, plus whether the window
    opens on a hook word, which is what makes a clip worth watching. Overlaps are
    resolved greedily by score so the returned clips never share footage.
    """
    usable = [
        {"start": float(c["start"]), "end": float(c["end"]), "text": " ".join(str(c.get("text") or "").split())}
        for c in cues
        if str(c.get("text") or "").strip()
    ]
    if not usable:
        return []
    if total_duration <= 0:
        total_duration = usable[-1]["end"]
    max_seconds = min(max_seconds, max(min_seconds + 1.0, total_duration))
    windows: list[dict[str, Any]] = []
    for index in range(len(usable)):
        start = usable[index]["start"]
        text_parts: list[str] = []
        for j in range(index, len(usable)):
            candidate = usable[j]
            span = candidate["end"] - start
            if span > max_seconds:
                break
            text_parts.append(candidate["text"])
            span = candidate["end"] - start
            if span < min_seconds:
                continue
            words = sum(len(part.split()) for part in text_parts)
            density = words / span if span else 0.0
            opening = " ".join(text_parts)[:80].lower()
            hook = sum(1 for word in _HOOK_WORDS if word in opening)
            # A pause after the last cue means the thought ended there.
            gap = 0.0
            if j + 1 < len(usable):
                gap = usable[j + 1]["start"] - candidate["end"]
            if gap > 0.7:
                boundary = 1.0
            elif str(candidate["text"])[-1:] in ".!?":
                boundary = 0.6
            else:
                boundary = 0.0
            score = density + 0.7 * hook + 1.4 * boundary
            windows.append({
                "start": round(start, 3),
                "end": round(candidate["end"], 3),
                "duration": round(span, 3),
                "text": " ".join(text_parts),
                "score": round(score, 3),
                "reason": f"{words} words / {span:.1f}s = {density:.2f} wps, hook={hook}, boundary={boundary}",
            })
    if not windows:
        # No speech long enough: fall back to evenly spaced windows so the caller
        # still gets usable cuts instead of nothing.
        step = min(max_seconds, max(min_seconds, total_duration / max(1, count)))
        for index in range(count):
            start = index * step
            if start >= total_duration:
                break
            windows.append({
                "start": round(start, 3),
                "end": round(min(total_duration, start + step), 3),
                "duration": round(min(total_duration, start + step) - start, 3),
                "text": "",
                "score": 0.0,
                "reason": "even split (no usable transcript)",
            })
    windows.sort(key=lambda w: (-w["score"], w["start"]))
    chosen: list[dict[str, Any]] = []
    for window in windows:
        if len(chosen) >= count:
            break
        if any(window["start"] < c["end"] - 0.25 and c["start"] < window["end"] - 0.25 for c in chosen):
            continue
        chosen.append(window)
    chosen.sort(key=lambda w: w["start"])
    for rank, window in enumerate(chosen, start=1):
        window["rank"] = rank
    return chosen


def cmd_shorts(args: argparse.Namespace) -> int:
    """Long video in, publishable vertical clips out."""
    ffmpeg = _require("ffmpeg")
    src = _existing(args.input, kinds="video")
    info = probe(src)
    out_dir = Path(args.out or f"{src.stem}_shorts").expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = Path(args.transcript).expanduser() if args.transcript else src.with_suffix(".transcript.json")
    cues: list[dict[str, Any]] = []
    if transcript_path.is_file():
        cues = json.loads(transcript_path.read_text()).get("segments") or []
    elif not args.no_transcribe:
        rc = cmd_transcribe(argparse.Namespace(
            input=str(src), model=args.model, language=args.language, format="json",
            out=None, max_chars=42, max_lines=2, no_vad=False, beam=5,
            work_audio=None, cache=None,
        ))
        if rc != 0:
            raise MediaError("transcription failed, so shorts cannot be planned")
        cues = json.loads(transcript_path.read_text()).get("segments") or []
    plan = plan_shorts(
        cues, count=int(args.count), min_seconds=_seconds(args.min),
        max_seconds=_seconds(args.max), total_duration=info.get("duration") or 0.0,
    )
    if not plan:
        raise MediaError("could not find any segment worth publishing")
    if args.captions and not cues:
        raise MediaError("--captions needs a transcript (drop --no-transcribe or pass --transcript)")
    width, height = int(info.get("width") or 0), int(info.get("height") or 0)
    aspect = args.aspect
    want_w, _, want_h = aspect.partition(":")
    ratio = float(want_w) / float(want_h)
    if ratio <= width / height:
        crop_h, crop_w = height, int(round(height * ratio)) // 2 * 2
    else:
        crop_w, crop_h = width, int(round(width / ratio)) // 2 * 2
    focus_x = _face_center_x(src, width, height) if args.focus == "face" else None
    x = _anchor_x(args.anchor, width, crop_w, focus_x)
    y = _anchor_y(args.anchor, height, crop_h)
    # Output size: reach --output-height when the source can afford it, otherwise
    # keep the crop's own resolution and DERIVE the paired dimension from the crop
    # WIDTH so the aspect stays exact.
    #
    # WHY NOT JUST CLAMP THE HEIGHT: forcing both sides onto the even-numbered crop
    # (404x720 for a 9:16 crop of 720p) leaves a 0.5611 aspect where 9:16 is 0.5625,
    # and the platform then letterboxes the clip to fix it. Deriving the height from
    # the width gives exactly 404x718. The rule never invents pixels on the long
    # side: a 720p recording is not silently stretched into a fake 1080x1920.
    target_h = int(args.output_height)
    upscaled = target_h <= crop_h
    if upscaled:
        scale_h = target_h if target_h % 2 == 0 else target_h - 1
        scale_w = int(round(scale_h * ratio)) // 2 * 2
    else:
        scale_w = crop_w if crop_w % 2 == 0 else crop_w - 1
        scale_h = max(2, int(round(scale_w / ratio)) // 2 * 2)
    rendered: list[dict[str, Any]] = []
    for index, window in enumerate(plan, start=1):
        stem = f"short_{index:02d}_{int(window['start'])}s"
        ass_path: Path | None = None
        vf = [f"crop={crop_w}:{crop_h}:{x}:{y}", f"scale={scale_w}:{scale_h}:flags=lanczos", "setsar=1"]
        if args.captions and cues:
            words = [
                cue for cue in cues
                if cue["start"] < window["end"] and cue["end"] > window["start"]
            ]
            shifted = [
                {"start": max(0.0, float(c["start"]) - window["start"]),
                 "end": max(0.0, float(c["end"]) - window["start"]),
                 "text": c["text"]}
                for c in words
            ]
            style = _CAPTION_STYLES.get(args.caption_style, _CAPTION_STYLES["shorts"])
            ass_path = out_dir / f"{stem}.ass"
            ass_path.write_text(build_ass(shifted, style=style))
            esc = str(ass_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
            vf.append(f"subtitles='{esc}'")
        piece = out_dir / f"{stem}.mp4"
        # ``-v warning`` (not ``error``) whenever we burn captions: the missing-font
        # case exits 0 with no captions drawn, and its only tell is a warning line.
        verbose = "warning" if ass_path else "error"
        proc = run([
            ffmpeg, "-v", verbose, "-ss", f"{window['start']:.3f}", "-i", str(src),
            "-t", f"{window['duration']:.3f}", "-vf", ",".join(vf),
            *_video_encoder_args(crf=args.crf, preset=args.preset), "-y", str(piece),
        ], timeout=3600)
        if proc.returncode != 0:
            raise MediaError(f"short {index} failed: {_tail(proc.stderr)}")
        verified = _verify(
            piece,
            expect={"width": scale_w, "height": scale_h,
                    "duration": {"value": round(window["duration"], 3), "tolerance": 0.4}},
        )
        warnings = _caption_warnings(proc.stderr) if ass_path else []
        thumb = out_dir / f"{stem}.jpg"
        run([ffmpeg, "-v", "error", "-ss", "1", "-i", str(piece), "-frames:v", "1", "-q:v", "2",
             "-vf", "scale=480:-2", "-y", str(thumb)], timeout=300)
        srt_path: Path | None = None
        if cues:
            in_window = [c for c in cues if c["start"] < window["end"] and c["end"] > window["start"]]
            srt_path = out_dir / f"{stem}.srt"
            srt_path.write_text(build_srt(
                [{"start": max(0.0, float(c["start"]) - window["start"]),
                  "end": max(0.0, float(c["end"]) - window["start"]),
                  "text": c["text"]} for c in in_window]
            ))
        rendered.append({
            "rank": index,
            "clip": str(piece),
            "thumbnail": str(thumb),
            "captions": str(ass_path) if ass_path else None,
            "captions_ok": (not warnings) if ass_path else None,
            "caption_warnings": warnings,
            "srt": str(srt_path) if srt_path else None,
            "start": window["start"],
            "end": window["end"],
            "duration": window["duration"],
            "score": window["score"],
            "reason": window["reason"],
            "text": window["text"][:400],
            "output_probe": verified,
        })
    manifest = out_dir / "manifest.json"
    manifest.write_text(json.dumps({
        "input": str(src), "aspect": aspect, "crop": {"w": crop_w, "h": crop_h, "x": x, "y": y},
        "focus": args.focus, "focus_x": focus_x,
        "output_size": {"w": scale_w, "h": scale_h}, "upscaled": upscaled,
        "clips": rendered,
    }, indent=1))
    return ok(input=str(src), directory=str(out_dir), count=len(rendered), aspect=aspect,
              output_size={"w": scale_w, "h": scale_h}, upscaled=upscaled,
              manifest=str(manifest), clips=rendered)


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="media_cli.py", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"media_cli {CLI_VERSION}")
    sub = parser.add_subparsers(dest="action", required=True)

    p = sub.add_parser("doctor", help="what is installed, and what is missing")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("install", help="install ffmpeg/yt-dlp/whisper/rembg in this sandbox")
    p.add_argument("--foreground", action="store_true", help="block instead of detaching")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("status", help="is the installer still running, and how far did it get")
    p.add_argument("--wait", type=float, default=0.0,
                   help="watch for this many seconds instead of returning instantly")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("job", help="poll a detached action's result file")
    p.add_argument("--result", required=True, help="the .json file the detached action writes")
    p.add_argument("--log", default=None, help="the .log file the detached action writes")
    p.add_argument("--wait", type=float, default=0.0,
                   help="watch for this many seconds instead of returning instantly")
    p.set_defaults(func=cmd_job)

    p = sub.add_parser("probe", help="duration / resolution / codecs / streams")
    p.add_argument("input")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("frames", help="save N evenly spaced stills to a directory")
    p.add_argument("input")
    p.add_argument("--count", type=int, default=9)
    p.add_argument("--out")
    p.set_defaults(func=cmd_frames)

    p = sub.add_parser("watch", help="one tiled contact-sheet PNG of N stills")
    p.add_argument("input")
    p.add_argument("--count", type=int, default=12)
    p.add_argument("--columns", type=int, default=0)
    p.add_argument("--width", type=int, default=480)
    p.add_argument("--timestamps", action="store_true")
    p.add_argument("--out")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("trim", help="cut a section out")
    p.add_argument("input")
    p.add_argument("--start", default="0")
    p.add_argument("--end")
    p.add_argument("--duration")
    p.add_argument("--fast", action="store_true", help="stream copy (keyframe-safe only)")
    p.add_argument("--out")
    p.set_defaults(func=cmd_trim)

    p = sub.add_parser("crop", help="crop to an aspect ratio or explicit box")
    p.add_argument("input")
    p.add_argument("--aspect", help="e.g. 9:16, 1:1, 16:9")
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--x", type=int)
    p.add_argument("--y", type=int)
    p.add_argument("--anchor", default="center", choices=["center", "top", "bottom", "left", "right"])
    p.add_argument("--focus", default="center", choices=["center", "face"])
    p.add_argument("--out")
    p.set_defaults(func=cmd_crop)

    p = sub.add_parser("scale", help="resize, sharpening when growing (HD/4K)")
    p.add_argument("input")
    p.add_argument("--height", type=int)
    p.add_argument("--width", type=int)
    p.add_argument("--mode", default="hd")
    p.add_argument("--crf", type=int, default=18)
    p.add_argument("--preset", default="slow")
    p.add_argument("--allow-downscale", action="store_true")
    p.add_argument("--out")
    p.set_defaults(func=cmd_scale)

    p = sub.add_parser("hd", help="alias of scale --mode hd")
    p.add_argument("input")
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--crf", type=int, default=18)
    p.add_argument("--preset", default="slow")
    p.add_argument("--allow-downscale", action="store_true")
    p.add_argument("--mode", default="hd")
    p.add_argument("--width", type=int)
    p.add_argument("--out")
    p.set_defaults(func=cmd_scale)

    p = sub.add_parser("concat", help="join clips, normalising them first")
    p.add_argument("inputs", nargs="+")
    p.add_argument("--height", type=int)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--crf", type=int, default=18)
    p.add_argument("--work-dir")
    p.add_argument("--out")
    p.set_defaults(func=cmd_concat)

    p = sub.add_parser("audio", help="extract or transcode the audio track")
    p.add_argument("input")
    p.add_argument("--format", default="m4a", choices=["mp3", "wav", "m4a", "flac"])
    p.add_argument("--mono", action="store_true")
    p.add_argument("--out")
    p.set_defaults(func=cmd_extract_audio)

    p = sub.add_parser("transcribe", help="speech to text/srt locally")
    p.add_argument("input")
    p.add_argument("--model", default="base")
    p.add_argument("--language", default="auto")
    p.add_argument("--format", default="srt", choices=["srt", "json", "txt", "all"])
    p.add_argument("--max-chars", type=int, default=42)
    p.add_argument("--max-lines", type=int, default=2)
    p.add_argument("--beam", type=int, default=5)
    p.add_argument("--no-vad", action="store_true")
    p.add_argument("--work-audio")
    p.add_argument("--cache")
    p.add_argument("--out")
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("captions", help="burn captions from an srt/transcript into the video")
    p.add_argument("input")
    p.add_argument("--srt")
    p.add_argument("--style", default="shorts", choices=sorted(_CAPTION_STYLES))
    p.add_argument("--shift", default="0")
    p.add_argument("--ass-out")
    p.add_argument("--out-prefix")
    p.add_argument("--crf", type=int, default=18)
    p.add_argument("--preset", default="slow")
    p.add_argument("--out")
    p.set_defaults(func=cmd_captions)

    p = sub.add_parser("bg", help="remove the background (image or video)")
    p.add_argument("input")
    p.add_argument("--model", default="u2net")
    p.add_argument("--alpha-matting", action="store_true", help="hair-level edges, slower")
    p.add_argument("--foreground-threshold", type=int, default=240)
    p.add_argument("--background-threshold", type=int, default=10)
    p.add_argument("--erode-size", type=int, default=10)
    p.add_argument("--background", help="composite onto this colour instead of keeping alpha")
    p.add_argument("--batch", type=int, default=8)
    p.add_argument("--work-dir")
    p.add_argument("--out")
    p.set_defaults(func=cmd_bg)

    p = sub.add_parser("download", help="download a video/audio link with yt-dlp")
    p.add_argument("url")
    p.add_argument("--out")
    p.add_argument("--quality", default="1080", choices=["best", "1080", "720", "480"])
    p.add_argument("--audio-only", action="store_true")
    p.add_argument("--playlist", action="store_true")
    p.add_argument("--cookies")
    p.add_argument("--section", help="only this range, e.g. *00:01:00-00:02:00")
    p.add_argument("--subtitles", help="language code, e.g. en")
    p.add_argument("--timeout", type=int, default=3600)
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("shorts", help="cut publishable vertical clips out of a long video")
    p.add_argument("input")
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--min", default="15")
    p.add_argument("--max", default="45")
    p.add_argument("--aspect", default="9:16")
    p.add_argument("--anchor", default="center", choices=["center", "top", "bottom", "left", "right"])
    p.add_argument("--focus", default="face", choices=["center", "face"])
    p.add_argument("--output-height", type=int, default=1920)
    p.add_argument("--captions", action="store_true")
    p.add_argument("--caption-style", default="shorts", choices=sorted(_CAPTION_STYLES))
    p.add_argument("--transcript")
    p.add_argument("--no-transcribe", action="store_true")
    p.add_argument("--model", default="base")
    p.add_argument("--language", default="auto")
    p.add_argument("--crf", type=int, default=18)
    p.add_argument("--preset", default="slow")
    p.add_argument("--out")
    p.set_defaults(func=cmd_shorts)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except MediaError as exc:
        return fail(str(exc))
    except KeyboardInterrupt:  # pragma: no cover
        return fail("interrupted")
    except Exception as exc:  # pragma: no cover - defensive
        return fail(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
