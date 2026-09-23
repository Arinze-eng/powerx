"""Video/audio workshop for the sandbox: cut, crop, upscale, matte, transcribe, caption, download.

WHY THIS TOOL EXISTS (and why it is shaped like this)
----------------------------------------------------
The user wants the agent to *edit video* — watch a clip, cut it, crop it to
vertical, put it in HD, remove a background, pull publishable shorts out of a long
recording, transcribe it, burn captions, and download from a link — with no paid
API and no local tooling. Running any of that on the application host would be
reckless: an ffmpeg encode is CPU-bound and unbounded, the whisper and rembg
models are hundreds of megabytes, and one encode would compete with the gateway
that serves every user.

So this tool is deliberately a *thin forwarder*, exactly like ``mt5_sandbox``. It
never imports ffmpeg, never installs anything locally, and never transcodes on the
host. It resolves the already-configured execution sandbox tool (``novita_sandbox``
— the same plumbing ``arduino_verify`` uses) and runs every media operation inside
it:

    1. bootstrap  fetch media_cli.py + install_media_sandbox.sh into the sandbox
    2. install    ffmpeg, yt-dlp, Pillow/OpenCV, faster-whisper, rembg (detached)
    3. run        ``python3 ~/.media/bin/media_cli.py <action> ...`` in the sandbox
    4. parse      the CLI's single-JSON-object stdout back into the answer

Everything the model needs — durations, resolutions, the contact sheet it can
LOOK at, caption warnings, the per-clip verification probe — comes back as
structured JSON, so the agent can inspect the result, fix the inputs and retry
without a human in the middle.

WHY SOME ACTIONS RUN DETACHED
-----------------------------
The sandbox caps ONE command at 900 s. A real request is routinely longer than
that: a 1080p re-encode, a whisper pass over a 40-minute recording, a per-frame
background matte, a YouTube download. Running those inline means the command is
killed mid-write and the caller receives NO output at all, which reads as "the
tool is broken" rather than "it needed longer". So long actions are launched
detached (stdout to a result file, a sentinel line appended on exit) and reported
through ``action='job'`` — the tool waits a bounded while inline first, so a short
clip still comes back in one call.

Safety model
------------
* Nothing is written to the application host — there is no local fallback path at
  all. With no sandbox configured the tool refuses rather than degrading.
* ``download`` is the only action that reaches the public internet on the user's
  behalf, and it only ever fetches the URL it was given.
* Every artifact is verified by the CLI (probe of duration/resolution/streams)
  before it is reported, so a 0-byte or wrong-shaped file fails here rather than
  three steps later.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext

#: Raw GitHub base for the two sandbox-side scripts. The sandbox has internet
#: access (the Novita tool creates boxes with ``allow_internet_access=True``), so
#: bootstrapping by URL avoids shipping tooling in the image.
#:
#: NOTE: this is only the LAST-RESORT source. The sandbox's egress path caches by
#: URL *path* and ignores both query strings and ``Cache-Control: no-cache``, so a
#: branch-name URL can serve a revision that is several pushes old. See
#: ``bootstrap_command`` for the ordered, verified source list.
_RAW_BASE = os.getenv(
    "MEDIA_SCRIPT_RAW_BASE",
    "https://raw.githubusercontent.com/Arinze-eng/powerx/main/scripts",
)

#: Owner/repo used to resolve ``main`` to a commit SHA before downloading.
_REPO = os.getenv("MEDIA_SCRIPT_REPO", "Arinze-eng/powerx")

#: Version of the sandbox-side CLI this tool requires.
#:
#: MUST be kept equal to ``CLI_VERSION`` in ``scripts/media_cli.py``. The bootstrap
#: refuses a download that does not carry this exact marker, which is what stops a
#: cached/stale revision from being executed silently: instead of debugging code
#: that is no longer running, the caller gets a loud warning and a retry against a
#: different source. Bump BOTH constants together whenever the CLI's contract with
#: this tool changes.
_CLI_VERSION = "1.0.0"

#: Where the CLI, its installer and its detached jobs live inside the sandbox.
_MEDIA_HOME = "$HOME/.media"
_CLI_PATH = f"{_MEDIA_HOME}/bin/media_cli.py"
_INSTALLER_PATH = f"{_MEDIA_HOME}/bin/install_media_sandbox.sh"
_JOBS_DIR = f"{_MEDIA_HOME}/jobs"

#: The sandbox's own ceiling is 900 s per command (``_MAX_TIMEOUT`` in
#: ``novita_sandbox``), so nothing here may exceed it.
_MAX_TIMEOUT = 900

#: Per-action ceilings. These are deliberately generous up to the hard cap: a
#: command killed by the sandbox returns no JSON at all, and a model given an empty
#: failure invents a reason rather than retrying. Long encodes are launched
#: detached instead of being given a larger number.
_TIMEOUTS: dict[str, int] = {
    "install": _MAX_TIMEOUT,
    "status": 300,
    "doctor": 180,
    "probe": 120,
    "frames": 600,
    "watch": _MAX_TIMEOUT,
    "trim": _MAX_TIMEOUT,
    "crop": _MAX_TIMEOUT,
    "scale": _MAX_TIMEOUT,
    "hd": _MAX_TIMEOUT,
    "concat": _MAX_TIMEOUT,
    "audio": 600,
    "transcribe": _MAX_TIMEOUT,
    "captions": _MAX_TIMEOUT,
    "bg": _MAX_TIMEOUT,
    "download": _MAX_TIMEOUT,
    "shorts": _MAX_TIMEOUT,
    "job": 300,
}
_DEFAULT_TIMEOUT = 300

#: Actions that routinely outlive a single command, so they are launched detached
#: and reported through ``job``. Everything NOT in this set is expected to finish
#: inside one command and is run inline.
_LONG_ACTIONS = frozenset(
    {"hd", "scale", "concat", "transcribe", "captions", "bg", "download", "shorts"}
)

#: How long the tool waits inline for a detached action before handing back a
#: ``job`` handle. Short clips therefore still complete in ONE call; only genuinely
#: long work needs the second call, and the caller is told which.
_INLINE_WAIT_SECONDS = int(os.getenv("MEDIA_INLINE_WAIT_SECONDS", "200"))

#: How long ``action='install'`` waits for the chain before reporting progress.
_INSTALL_WAIT_SECONDS = int(os.getenv("MEDIA_INSTALL_WAIT_SECONDS", "840"))

_ALL_ACTIONS = (
    "doctor",
    "install",
    "status",
    "job",
    "probe",
    "frames",
    "watch",
    "trim",
    "crop",
    "scale",
    "hd",
    "concat",
    "audio",
    "transcribe",
    "captions",
    "bg",
    "download",
    "shorts",
)

#: action -> how tool kwargs become the CLI's argv.
#:
#: ``positional`` is rendered in order, ``flags`` maps a tool field to its CLI flag,
#: ``bools`` maps a truthy tool field to a bare flag, and ``list_values`` marks
#: fields whose value is a list (``concat``'s inputs) or a comma-separated string.
#: A data table rather than a chain of ifs: the CLI's flag names are exactly the
#: thing that drifts, and one table is one place to fix it.
_FLAG_SPEC: dict[str, dict[str, Any]] = {
    "probe": {"positional": ("input",), "flags": {}},
    "frames": {"positional": ("input",), "flags": {"count": "--count", "out": "--out"}},
    "watch": {
        "positional": ("input",),
        "flags": {"count": "--count", "columns": "--columns", "width": "--width", "out": "--out"},
        "bools": {"timestamps": "--timestamps"},
    },
    "trim": {
        "positional": ("input",),
        "flags": {"start": "--start", "end": "--end", "duration": "--duration", "out": "--out"},
        "bools": {"fast": "--fast"},
    },
    "crop": {
        "positional": ("input",),
        "flags": {
            "aspect": "--aspect", "width": "--width", "height": "--height", "x": "--x",
            "y": "--y", "anchor": "--anchor", "focus": "--focus", "out": "--out",
        },
    },
    "scale": {
        "positional": ("input",),
        "flags": {
            "height": "--height", "width": "--width", "mode": "--mode", "crf": "--crf",
            "preset": "--preset", "out": "--out",
        },
        "bools": {"allow_downscale": "--allow-downscale"},
    },
    "hd": {
        "positional": ("input",),
        "flags": {
            "height": "--height", "width": "--width", "mode": "--mode", "crf": "--crf",
            "preset": "--preset", "out": "--out",
        },
        "bools": {"allow_downscale": "--allow-downscale"},
    },
    "concat": {
        "positional": (),
        "flags": {"height": "--height", "fps": "--fps", "crf": "--crf", "work_dir": "--work-dir", "out": "--out"},
        "list_values": {"inputs": None},
    },
    "audio": {
        "positional": ("input",),
        "flags": {"format": "--format", "out": "--out"},
        "bools": {"mono": "--mono"},
    },
    "transcribe": {
        "positional": ("input",),
        "flags": {
            "model": "--model", "language": "--language", "format": "--format",
            "max_chars": "--max-chars", "max_lines": "--max-lines", "beam": "--beam",
            "work_audio": "--work-audio", "cache": "--cache", "out": "--out",
        },
        "bools": {"no_vad": "--no-vad"},
    },
    "captions": {
        "positional": ("input",),
        "flags": {
            "srt": "--srt", "style": "--style", "shift": "--shift", "ass_out": "--ass-out",
            "out_prefix": "--out-prefix", "crf": "--crf", "preset": "--preset", "out": "--out",
        },
    },
    "bg": {
        "positional": ("input",),
        "flags": {
            "model": "--model", "foreground_threshold": "--foreground-threshold",
            "background_threshold": "--background-threshold", "erode_size": "--erode-size",
            "background": "--background", "batch": "--batch", "work_dir": "--work-dir",
            "out": "--out",
        },
        "bools": {"alpha_matting": "--alpha-matting"},
    },
    "download": {
        "positional": ("url",),
        "flags": {
            "out": "--out", "quality": "--quality", "cookies": "--cookies",
            "section": "--section", "subtitles": "--subtitles",
        },
        "bools": {"audio_only": "--audio-only", "playlist": "--playlist"},
    },
    "shorts": {
        "positional": ("input",),
        "flags": {
            "count": "--count", "min_seconds": "--min", "max_seconds": "--max",
            "aspect": "--aspect", "anchor": "--anchor", "focus": "--focus",
            "output_height": "--output-height", "caption_style": "--caption-style",
            "transcript": "--transcript", "model": "--model", "language": "--language",
            "crf": "--crf", "preset": "--preset", "out": "--out",
        },
        "bools": {"captions": "--captions", "no_transcribe": "--no-transcribe"},
    },
}

#: Errors the CLI raises when a piece of the chain is absent. Matching on them is
#: how the tool decides to install and retry instead of handing the model a
#: refusal it will interpret as "video editing is unavailable here".
_MISSING_TOOL_MARKERS = (
    "is not installed in this sandbox",
    "not installed",
    "no such file or directory",
    "ffmpeg",
    "yt-dlp",
    "installer not found",
)


def _sandbox_tool(ctx: ToolContext | None) -> Any:
    """Find the configured execution sandbox tool, exactly as mt5_sandbox does.

    Deliberately the same resolution order and the same defensive shape: reusing
    the sandbox tool means media inherits whatever backend the deployment already
    chose (Novita by default) plus its per-session sandbox reuse, sizing and
    lifecycle. This tool therefore adds no new infrastructure.

    The registry is a ToolRegistry, not a dict. Resolve by name first — that is the
    only guaranteed API — then fall back to iterating defensively, because relying
    on iteration alone used to raise TypeError (the registry had no
    ``__iter__``/``values``) and, since this function swallows exceptions, it
    returned None and reached the model as "no sandbox is configured" even when the
    sandbox was fully configured.
    """
    if ctx is None:
        return None
    # Explicit None checks, NOT ``a or b``: an EMPTY registry is a perfectly valid
    # registry, and falling through to the next attribute because it is falsy means
    # resolving ``ctx.tools`` instead — which in a test context is an auto-created
    # Mock that answers every name and is handed back as a "sandbox", turning a
    # clean "no sandbox is configured" refusal into a confusing transport error.
    registry = getattr(ctx, "tool_registry", None)
    if registry is None:
        registry = getattr(ctx, "tools", None)
    if registry is None:
        return None

    for name in ("novita_sandbox", "vps_exec", "runloop_sandbox", "daytona_sandbox"):
        getter = getattr(registry, "get", None)
        if callable(getter):
            try:
                tool = getter(name)
            except Exception:  # pragma: no cover - defensive
                tool = None
            # The NAME must agree with the key it was looked up under. Without this
            # check a permissive stand-in (a plain MagicMock answers any attribute)
            # passes as a sandbox, and the failure surfaces far away as an await
            # TypeError instead of the honest "no sandbox is configured".
            if tool is not None and getattr(tool, "name", None) == name:
                return tool

    try:
        if isinstance(registry, dict):
            items: Any = registry.values()
        elif callable(getattr(registry, "values", None)):
            items = registry.values()
        else:
            items = registry
        for tool in items:
            if getattr(tool, "name", "") in (
                "novita_sandbox", "vps_exec", "runloop_sandbox", "daytona_sandbox"
            ):
                return tool
    except Exception:  # pragma: no cover - defensive
        return None
    return None


def bootstrap_command() -> str:
    """Idempotently fetch the CLI + installer into the sandbox."""
    return _BOOTSTRAP_TEMPLATE.format(
        home=_MEDIA_HOME,
        cli=_CLI_PATH,
        installer=_INSTALLER_PATH,
        repo=_REPO,
        raw_base=_RAW_BASE,
        version=_CLI_VERSION,
    )


#: Bootstrap shell. ``{...}`` placeholders are filled by ``bootstrap_command``.
#:
#: MEASURED FAILURE (2026-09-21, mt5_sandbox — identical plumbing, identical bug):
#:
#: The point of bootstrapping by URL is that a fixed CLI ships without rebuilding
#: the sandbox. That silently stopped being true: the sandbox's egress path caches
#: ``raw.githubusercontent.com`` responses **by path**, so
#: ``.../main/scripts/mt5_cli.py`` kept returning a revision several pushes old.
#: Neither a unique ``?ts=`` query string nor ``Cache-Control: no-cache`` helped
#: (both were measured). The effect was maximally confusing: a fix that was on main,
#: covered by tests and verified from the host still produced the OLD failure live,
#: so the fix looked wrong when it was simply not running.
#:
#: What was measured to work:
#:   * a commit-pinned raw URL (``/<sha>/scripts/...``) — never cached, because that
#:     exact URL had never been requested before, and
#:   * the GitHub API.
#:
#: So: resolve ``main`` to a SHA through the API, download the pinned URL, and
#: VERIFY the result carries the ``CLI_VERSION`` this tool requires. Only if that
#: fails do we fall back to the branch URL — and a version mismatch on every source
#: is reported loudly instead of executing unknown code.
_BOOTSTRAP_TEMPLATE = """\
mkdir -p {home}/bin {home}/jobs
_want='{version}'
_fetch() {{ curl -fsSL --retry 2 "$1" -o "$2" 2>/dev/null && grep -q "CLI_VERSION = [\\"']$_want[\\"']" "$2"; }}
_sha=$(curl -fsSL 'https://api.github.com/repos/{repo}/commits/main' 2>/dev/null \
  | python3 -c "import sys,json;print((json.load(sys.stdin) or {{}}).get('sha',''))" 2>/dev/null)
_ok=''
for _base in "https://raw.githubusercontent.com/{repo}/$_sha/scripts" "{raw_base}"; do
  if _fetch "$_base/media_cli.py" {cli}; then
    _ok=1
    curl -fsSL --retry 2 "$_base/install_media_sandbox.sh" -o {installer} 2>/dev/null
    break
  fi
done
chmod +x {cli} {installer} 2>/dev/null
if [ -z "$_ok" ]; then
  echo "WARNING: could not fetch media_cli.py version $_want (a cached copy of an" >&2
  echo "older revision may be in use). Retry, or set MEDIA_SCRIPT_RAW_BASE." >&2
else
  echo "media_cli.py $_want ready" >&2
fi\
"""


def _sh(value: Any) -> str:
    return shlex.quote(str(value))


def _job_id(action: str, kwargs: dict[str, Any]) -> str:
    """A stable id for one detached invocation.

    Derived from the rendered command, so the SAME request re-uses the same result
    file instead of piling up new ones — which is what makes ``job`` correct
    without the caller having to remember a path.
    """
    payload = json.dumps({"action": action, "kwargs": _job_relevant(kwargs)}, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


#: Fields that describe WHERE a job reports, not WHAT it does. Excluded from the
#: hash so that asking for the result cannot produce a different job id than the
#: launch did.
_JOB_IGNORED_FIELDS = frozenset({"job_id", "wait", "timeout", "background", "action"})


def _job_relevant(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k not in _JOB_IGNORED_FIELDS and v is not None}


def job_paths(job_id: str) -> tuple[str, str]:
    """The result and log files for a job id, inside the sandbox."""
    return f"{_JOBS_DIR}/{job_id}.json", f"{_JOBS_DIR}/{job_id}.log"


def build_cli_command(action: str, kwargs: dict[str, Any]) -> str:
    """Translate tool kwargs into a ``media_cli.py`` invocation.

    Only fields that were actually supplied are rendered: every CLI default is
    already the quality-first choice (CRF 18 preset slow, 1080p HD, 9:16 shorts),
    and passing them back explicitly would freeze today's defaults into the
    command line.
    """
    spec = _FLAG_SPEC.get(action)
    if spec is None:
        return ""

    parts = ["python3", _CLI_PATH, action]

    for field in spec.get("positional", ()):
        value = kwargs.get(field)
        if value in (None, "", []):
            return ""
        parts.append(_sh(value))

    # ``inputs`` has no CLI flag: the CLI takes the pieces as trailing positionals.
    for field in spec.get("list_values", {}):
        value = kwargs.get(field)
        if isinstance(value, str):
            value = [piece.strip() for piece in value.split(",") if piece.strip()]
        if not value:
            return ""
        parts.extend(_sh(piece) for piece in value)

    for field, flag in (spec.get("flags") or {}).items():
        value = kwargs.get(field)
        if value in (None, ""):
            continue
        if isinstance(value, bool):
            # A boolean on a flag field means "the caller wrote it down", so it is
            # rendered as the CLI's bare switch rather than `--flag True`.
            if value:
                parts.append(flag)
            continue
        parts += [flag, _sh(value)]

    for field, flag in (spec.get("bools") or {}).items():
        if kwargs.get(field):
            parts.append(flag)

    return " ".join(parts)


def launch_command(action: str, kwargs: dict[str, Any], job_id: str) -> str:
    """The detached launcher for one long action.

    Written as a small script file in the sandbox rather than a single quoted
    pipeline: the media commands carry filtergraphs full of quotes, colons and
    brackets, and every attempt to nest them inside another shell layer is a
    quoting bug waiting to happen. The script is the artifact; the launcher just
    runs it.

    The trailing ``echo "media_cli exit=$?"`` sentinel is what makes ``done``
    trustworthy: the result file is written by the action itself and can exist in a
    half-flushed state, so its mere presence is not evidence of completion. Reading
    it only after the sentinel is what stops a truncated JSON being reported as a
    finished job.
    """
    result, log = job_paths(job_id)
    script = f"{_JOBS_DIR}/{job_id}.sh"
    command = build_cli_command(action, kwargs)
    lines = [
        f"mkdir -p {_JOBS_DIR}",
        f"rm -f {result} {log}",
        f"cat > {script} <<'MEDIA_JOB_EOF'",
        "#!/usr/bin/env bash",
        f"{command} > {result} 2> {log}",
        f'echo "media_cli exit=$?" >> {log}',
        "MEDIA_JOB_EOF",
        f"chmod +x {script}",
        f"setsid nohup bash {script} >/dev/null 2>&1 < /dev/null &",
        'echo "job launched"',
    ]
    return "\n".join(lines)


def _parse_payload(rendered: str) -> dict[str, Any] | None:
    """Pull the CLI's JSON object out of the sandbox command output.

    The sandbox wrapper appends ``[exit_code=N]`` and may interleave log lines, so
    the first balanced ``{...}`` block is the reliable extraction target. Scanning
    rather than ``json.loads(whole)`` is what keeps a bootstrap warning on stderr
    from turning a good result into "no output".
    """
    text = rendered or ""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escaped = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : idx + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    return None


def _looks_uninstalled(payload: dict[str, Any] | None) -> bool:
    """Whether a failure is really "the chain is not installed yet"."""
    if not isinstance(payload, dict) or payload.get("ok") is not False:
        return False
    haystack = " ".join(
        str(payload.get(key) or "") for key in ("error", "message", "report")
    ).lower()
    if not haystack:
        return False
    strong = ("is not installed in this sandbox", "installer not found", "no such file or directory")
    return any(marker in haystack for marker in strong)


class MediaSandboxTool(Tool):
    """Edit video and audio entirely inside the user's sandbox."""

    config_key = "media_sandbox"
    _scopes = {"core", "subagent"}

    def __init__(self, ctx: ToolContext | None = None) -> None:
        # The ToolContext is retained so the sandbox tool can be resolved at
        # execute() time (the registry is not available during construction).
        self._ctx: ToolContext | None = ctx

    @classmethod
    def create(cls, ctx: ToolContext) -> "MediaSandboxTool":
        """Carry the tool context so the sandbox tool can be resolved later."""
        return cls(ctx)

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        """Always register the tool; availability is decided at execute() time.

        WHY NOT GATE ON THE SANDBOX HERE: ``enabled()`` is evaluated by the loader
        while it iterates the tool classes, i.e. BEFORE the registry is populated.
        ``_sandbox_tool()`` therefore always returns None at this point and the tool
        would be silently dropped from the schema — the model would never see
        ``media_sandbox`` exist and would tell users that video editing is
        unsupported. Advertising the capability is what matters; the tool is
        harmless without a sandbox because it refuses instead of falling back.
        """
        return True

    @property
    def name(self) -> str:
        return "media_sandbox"

    @property
    def description(self) -> str:
        return (
            "Video/audio editing in the user's execution sandbox using local ffmpeg — no "
            "paid API. NEVER runs ffmpeg or an encode on the application host. "
            "MANDATORY FIRST STEP: the first media action in a fresh sandbox MUST begin "
            "with action='install' (fetches ffmpeg, yt-dlp, Pillow/OpenCV, "
            "faster-whisper and rembg). action='install' waits for the chain itself and "
            "returns when it is ready (~3-12 min); if it reports stage='installing' the "
            "install is progressing normally, so poll action='status' yourself — do NOT "
            "tell the user to check back later and do NOT give up and hand them ffmpeg "
            "commands to run locally. "
            "Read the video, do not guess: action='watch' writes ONE contact-sheet PNG "
            "(tiles of N stills, optionally with timestamps) — LOOK at that image before "
            "choosing a crop, a cut point or a short. action='probe' gives "
            "duration/resolution/fps/codecs when you only need numbers. "
            "Every action returns JSON and every written artifact is probed afterwards "
            "(duration, resolution, streams, size), so a good result reports its own "
            "verification and a bad one fails loudly instead of shipping a silent "
            "0-byte file. "
            "Actions: doctor, install, status, job, probe, frames, watch, trim, crop, "
            "scale, hd, concat, audio, transcribe, captions, bg, download, shorts. "
            f"LONG actions ({', '.join(sorted(_LONG_ACTIONS))}) are launched detached: "
            "the tool waits ~200 s inline, and if the work is still running it returns "
            "a job_id — then call action='job' with that job_id (never re-run the same "
            "edit, that would start a second encode). "
            "QUALITY DEFAULTS: video is H.264 CRF 18 preset slow with +faststart, audio "
            "AAC 192k. trim/fast is a stream copy (instant, keyframe-aligned, so its "
            "duration is honestly reported as approximate); omit it for a frame-exact cut. "
            "crop --aspect 9:16 --focus face finds the speaker with OpenCV and keeps them "
            "centred; without a detectable face it falls back to the centre. scale/hd "
            "REFUSE to downscale unless allow_downscale is set, and never invent "
            "resolution: a 720p source cannot become a real 1080x1920, so shorts from it "
            "come out 404x718 (exactly 9:16) and the result says upscaled=false. "
            "Downloading: action='download' takes a YouTube/other link and saves locally "
            "(yt-dlp) — quality 1080/720/480/best, audio_only for the soundtrack, "
            "section for a single range, subtitles to pull the platform's own captions. "
            "Shorts: action='shorts' transcribes (faster-whisper, local) when there is no "
            "sidecar transcript, scores sentence-boundary windows on words-per-second and "
            "hook words, and writes non-overlapping vertical clips with a thumbnail, an "
            ".srt and burned captions plus a manifest.json. Captions: action='transcribe' "
            "writes .srt/.json/.vtt (the transcript is cached beside the media as "
            "<name>.transcript.json and reused), action='captions' burns them in. The "
            "result carries caption_warnings — ffmpeg exits 0 with NO captions drawn when "
            "the font cannot be resolved, so check captions_ok before claiming captions "
            "are in the video. "
            "Background removal: action='bg' takes a still (PNG with alpha) or a video "
            "(per-frame matte). A video without background gives a transparent .webm; "
            "background='RRGGBB' gives an opaque .mp4 with the original audio re-attached. "
            "Outputs live in the sandbox: report the paths, and fetch a file out with the "
            "sandbox tool's own download_url action."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_ALL_ACTIONS)},
                "input": {"type": "string", "description": "Input media path inside the sandbox (a video for trim/crop/scale/hd/watch/shorts, a video or image for bg)."},
                "inputs": {"type": "array", "items": {"type": "string"}, "description": "action=concat: the pieces, in order. They are normalised to one size/fps first, because the concat demuxer mangles mixed sources."},
                "url": {"type": "string", "description": "action=download: the page URL to fetch (e.g. https://youtu.be/...)."},
                "out": {"type": "string", "description": "Output path (file or directory) inside the sandbox. Defaults to a sibling of the input with a sensible name."},
                "quality": {"type": "string", "enum": ["1080", "720", "480", "360", "best", "worst"], "description": "action=download: max height to request (default 1080)."},
                "audio_only": {"type": "boolean", "description": "action=download: fetch only the audio track."},
                "playlist": {"type": "boolean", "description": "action=download: allow a playlist/whole channel. Off by default — one URL means one video."},
                "section": {"type": "string", "description": "action=download: a single range, e.g. '*00:01:00-00:02:30'."},
                "subtitles": {"type": "string", "description": "action=download: also fetch the platform's captions in this language code, e.g. 'en'."},
                "cookies": {"type": "string", "description": "action=download: path to a cookies.txt, for a video that needs a session."},
                "start": {"type": "string", "description": "action=trim: start, as seconds or HH:MM:SS(.mmm)."},
                "end": {"type": "string", "description": "action=trim: absolute end."},
                "duration": {"type": "string", "description": "action=trim: length instead of an absolute end."},
                "fast": {"type": "boolean", "description": "action=trim: stream copy without re-encoding. Instant, but it can only cut on a keyframe, so the result is usually a second or two longer than asked (and the result says so)."},
                "aspect": {"type": "string", "description": "crop/shorts: target aspect, e.g. 9:16, 1:1, 4:5."},
                "focus": {"type": "string", "enum": ["center", "face"], "description": "crop/shorts: 'face' keeps the detected speaker centred. Deliberately best-effort — with no detectable face the crop falls back to the anchor."},
                "anchor": {"type": "string", "enum": ["center", "top", "bottom", "left", "right"], "description": "Which edge the crop is pinned to when there is no face to follow."},
                "x": {"type": "integer", "description": "action=crop: explicit left offset."},
                "y": {"type": "integer", "description": "action=crop: explicit top offset."},
                "width": {"type": "integer", "description": "crop: crop width. scale: target width. watch: tile width in pixels."},
                "height": {"type": "integer", "description": "crop: crop height. scale/hd: target height (hd defaults to 1080)."},
                "mode": {"type": "string", "description": "scale/hd: size preset when no explicit height is given (hd=1080, 2k=1440, 4k=2160)."},
                "crf": {"type": "integer", "description": "x264 quality, lower is better and bigger. Default 18; 20-23 is a smaller file for a draft."},
                "preset": {"type": "string", "description": "x264 speed/compression preset (default slow). Use veryfast for a quick preview."},
                "allow_downscale": {"type": "boolean", "description": "scale/hd: permit shrinking. Off by default, so an accidental 'put it in HD' on a 1080p source is refused instead of quietly destroying detail."},
                "count": {"type": "integer", "description": "frames: how many stills. watch: how many tiles. shorts: how many clips (default 3)."},
                "columns": {"type": "integer", "description": "action=watch: contact-sheet columns (default min(4, count))."},
                "timestamps": {"type": "boolean", "description": "action=watch: stamp each tile with its timestamp, so a cut point can be read straight off the sheet."},
                "fps": {"type": "integer", "description": "action=concat: output frame rate (default 30)."},
                "format": {"type": "string", "description": "audio: mp3/m4a/wav/aac/flac. transcribe: srt/json/vtt/txt."},
                "mono": {"type": "boolean", "description": "action=audio: downmix to one channel."},
                "model": {"type": "string", "description": "transcribe/shorts: whisper model (tiny/base/small/medium/large-v3). 'base' is the default and the practical choice on a CPU sandbox; 'small' is better on accented speech but several times slower."},
                "language": {"type": "string", "description": "transcribe/shorts: language code, or 'auto' to detect."},
                "beam": {"type": "integer", "description": "transcribe: beam size (default 5). Lower is faster and less accurate."},
                "no_vad": {"type": "boolean", "description": "transcribe: skip voice-activity trimming (keep it off VAD when the audio is music-heavy or already tight)."},
                "max_chars": {"type": "integer", "description": "transcribe: wrap subtitles at this many characters per line."},
                "max_lines": {"type": "integer", "description": "transcribe: at most this many lines per subtitle cue."},
                "cache": {"type": "string", "description": "transcribe: where to keep the downloaded whisper model (survives across calls)."},
                "srt": {"type": "string", "description": "captions: the .srt to burn. Defaults to the sidecar <input>.transcript.json's segments."},
                "style": {"type": "string", "enum": ["shorts", "clean", "karaoke"], "description": "captions: caption look. 'shorts' is big and bold and sits above the bottom UI chrome."},
                "shift": {"type": "string", "description": "captions: shift every cue by this much (seconds), for captions that came from a trimmed clip."},
                "ass_out": {"type": "string", "description": "captions: where to write the generated .ass."},
                "out_prefix": {"type": "string", "description": "captions: directory to write the output into."},
                "captions": {"type": "boolean", "description": "action=shorts: burn captions into each clip (requires a transcript)."},
                "caption_style": {"type": "string", "enum": ["shorts", "clean", "karaoke"], "description": "action=shorts: caption look for the burned clips."},
                "min_seconds": {"type": "number", "description": "action=shorts: shortest clip to consider (default 15)."},
                "max_seconds": {"type": "number", "description": "action=shorts: longest clip to consider (default 45)."},
                "output_height": {"type": "integer", "description": "action=shorts: target height (default 1920). Never invents pixels: when the source cannot afford it, the clip keeps the crop's own resolution at exactly the requested aspect and reports upscaled=false."},
                "transcript": {"type": "string", "description": "action=shorts: an existing transcript json to plan from, instead of transcribing."},
                "no_transcribe": {"type": "boolean", "description": "action=shorts: plan only from an existing transcript; refuse rather than transcribe."},
                "background_run": {"type": "boolean", "description": "Force a long action to run detached immediately instead of waiting inline for it."},
                "job_id": {"type": "string", "description": "action=job: the id a previous call returned. Do NOT re-run the edit; poll this instead."},
                "wait": {"type": "integer", "description": "How long to wait inline for a detached job or for the installer, in seconds (capped by the sandbox's own 900 s command ceiling)."},
                "timeout": {"type": "integer", "description": "Command timeout override in seconds (hard-capped at 900 by the sandbox)."},
                "alpha_matting": {"type": "boolean", "description": "action=bg: refine the edges with alpha matting. Cleaner hair/edges, noticeably slower."},
                "foreground_threshold": {"type": "integer", "description": "action=bg: alpha-matting foreground threshold (default 240)."},
                "background_threshold": {"type": "integer", "description": "action=bg: alpha-matting background threshold (default 10)."},
                "erode_size": {"type": "integer", "description": "action=bg: alpha-matting edge erosion in pixels (default 10)."},
                "background": {"type": "string", "description": "action=bg: replace the removed background with this RRGGBB colour and output an opaque .mp4 with the original audio. Omit it for a transparent .webm."},
                "batch": {"type": "integer", "description": "action=bg: frames matted per pass (higher is faster and hungrier)."},
                "work_dir": {"type": "string", "description": "Scratch directory for the per-frame work (bg) or the normalised pieces (concat). Defaults to a temp dir."},
            },
            "required": ["action"],
        }

    # ------------------------------------------------------------------ #
    # sandbox plumbing
    # ------------------------------------------------------------------ #
    async def _wait_for_install(self, sandbox: Any, budget: int) -> dict[str, Any]:
        """Poll the installer to a terminal stage, so the caller need not come back.

        Handing back "installing" and relying on the model to poll is what produced
        stalled runs: a model offered a plausible alternative takes it, and the user
        gets told to install ffmpeg themselves. So the tool does the waiting itself,
        in ONE bounded command that runs inside the sandbox (``status --wait``),
        rather than sleeping between round trips here.
        """
        waited = 0
        payload: dict[str, Any] = {}
        while waited < budget:
            chunk = min(300, budget - waited)
            try:
                rendered = await sandbox.execute(
                    action="run",
                    command=f"{bootstrap_command()} >/dev/null 2>&1 || true; "
                            f"python3 {_CLI_PATH} status --wait {chunk}",
                    timeout=min(_MAX_TIMEOUT, chunk + 60),
                )
            except Exception as exc:  # noqa: BLE001 - transport-level failure
                logger.warning("media_sandbox: installer poll failed ({})", exc)
                return payload or {"ok": False, "stage": "installing"}
            payload = _parse_payload(str(rendered)) or payload or {}
            if payload.get("done") or payload.get("ready"):
                return payload
            if not payload.get("installing") and payload.get("done") is False and waited >= 300:
                return payload
            waited += chunk
        return payload

    async def _provision(self, sandbox: Any, wait: int) -> dict[str, Any]:
        """Kick the detached installer, then wait for a terminal stage."""
        try:
            rendered = await sandbox.execute(
                action="run",
                command=f"{bootstrap_command()} >/dev/null 2>&1 || true; "
                        f"python3 {_CLI_PATH} install",
                timeout=120,
            )
        except Exception as exc:  # noqa: BLE001 - transport-level failure
            return {"ok": False, "error": f"starting the media install failed: {exc}"}
        started = _parse_payload(str(rendered)) or {}
        result = await self._wait_for_install(sandbox, wait)
        result.setdefault("install_started", started.get("started"))
        return result

    async def _run_cli(self, sandbox: Any, command: str, timeout: int) -> str:
        """Run one CLI invocation, always refreshing the CLI first.

        The refresh is why a fixed CLI ships without rebuilding the sandbox. Stdout
        is kept (it carries the action's JSON) and the bootstrap's warning on stderr
        is echoed at the end, so a "no JSON result" failure is interpretable instead
        of mysterious.
        """
        return str(
            await sandbox.execute(
                action="run",
                command=f"{bootstrap_command()} >/dev/null 2>&1 || true; {command}",
                timeout=timeout,
            )
        )

    async def _poll_job(
        self, sandbox: Any, job_id: str, wait: int, *, timeout: int | None = None
    ) -> dict[str, Any]:
        result_path, log_path = job_paths(job_id)
        command = (
            f"python3 {_CLI_PATH} job --result {result_path} --log {log_path} "
            f"--wait {max(0, min(wait, _MAX_TIMEOUT - 60))}"
        )
        rendered = await self._run_cli(
            sandbox, command, timeout=timeout or min(_MAX_TIMEOUT, wait + 60)
        )
        return _parse_payload(rendered) or {"ok": False, "error": "the job poll returned no JSON"}

    # ------------------------------------------------------------------ #
    # execute
    # ------------------------------------------------------------------ #
    async def execute(self, **kwargs: Any) -> ToolResult | str:  # type: ignore[override]
        action = str(kwargs.get("action") or "").strip().lower()
        if action not in _ALL_ACTIONS:
            return ToolResult.error(
                f"Unknown action '{action}'. Valid actions: {', '.join(_ALL_ACTIONS)}"
            )

        # --- validation before anything touches the sandbox ---------------- #
        # Every one of these is a mistake that would otherwise surface as a
        # confusing CLI error AFTER a bootstrap round trip.
        if action in _FLAG_SPEC and action not in ("probe",):
            required = {"input"} if "input" in _FLAG_SPEC[action].get("positional", ()) else set()
            if action == "concat":
                required = {"inputs"}
            if action == "download":
                required = {"url"}
            missing = [field for field in required if kwargs.get(field) in (None, "", [])]
            if missing:
                return ToolResult.error(
                    f"action='{action}' requires {', '.join(repr(m) for m in missing)} "
                    "(an absolute path or URL inside the sandbox)."
                )
        if action == "crop" and not any(
            kwargs.get(field) for field in ("aspect", "width", "height", "x", "y")
        ):
            return ToolResult.error(
                "action='crop' needs a target: 'aspect' (e.g. 9:16), or explicit "
                "width/height/x/y. For a full frame, use action='scale' instead."
            )
        if action == "job" and not kwargs.get("job_id"):
            return ToolResult.error(
                "action='job' requires 'job_id' — the id returned by the call that "
                "launched the long action. Do not re-run the edit."
            )
        if action == "captions" and not (kwargs.get("srt") or kwargs.get("input")):
            return ToolResult.error(
                "action='captions' needs 'input' (the video to burn into) and either "
                "'srt' or a sidecar transcript next to that video."
            )
        if action == "transcribe" and not kwargs.get("input"):
            return ToolResult.error("action='transcribe' requires 'input'.")
        if action == "shorts" and not kwargs.get("input"):
            return ToolResult.error("action='shorts' requires 'input' (the long video).")

        sandbox = _sandbox_tool(getattr(self, "_ctx", None))
        if sandbox is None:
            return ToolResult.error(
                "No execution sandbox is configured. Video editing must run inside a "
                "sandbox (novita/vps/runloop/daytona) — this tool never runs ffmpeg or "
                "an encode on the application host."
            )

        # --- the installer is its own flow --------------------------------- #
        if action == "install":
            budget = int(kwargs.get("wait") or _INSTALL_WAIT_SECONDS)
            result = await self._provision(sandbox, budget)
            if result.get("ready") or result.get("done"):
                return json.dumps({
                    **result,
                    "ok": True,
                    "message": "The media chain is installed in the sandbox. Media actions "
                               "work now.",
                })
            return ToolResult.error(json.dumps({
                "ok": False,
                "stage": result.get("stage") or "installing",
                "failure": "media_install_incomplete",
                "message": (
                    "The media chain is still installing (last stage: "
                    f"{result.get('stage') or 'installing'}). It is progressing normally — "
                    "this is a large download (ffmpeg, whisper and rembg weights)."
                ),
                "next": "Poll media_sandbox(action='status') yourself until ready=true, then "
                        "run the action you wanted. Do NOT ask the user to install anything.",
                "install": result,
            }))

        if action == "status":
            wait = int(kwargs.get("wait") or 0)
            command = f"python3 {_CLI_PATH} status" + (f" --wait {wait}" if wait else "")
            rendered = await self._run_cli(sandbox, command, timeout=min(_MAX_TIMEOUT, wait + 120))
            payload = _parse_payload(rendered)
            if payload is None:
                return ToolResult.error(
                    "The sandbox returned no JSON for status. The sandbox itself may not be "
                    "ready — retry, or check the sandbox tool."
                )
            return json.dumps(payload)

        if action == "doctor":
            rendered = await self._run_cli(sandbox, f"python3 {_CLI_PATH} doctor", timeout=_TIMEOUTS["doctor"])
            payload = _parse_payload(rendered)
            if payload is None:
                return ToolResult.error(
                    "The sandbox returned no JSON for doctor. Start the chain with "
                    "action='install'."
                )
            if not payload.get("ready") or payload.get("report", "").startswith("incomplete"):
                # Auto-provision: a model handed "ffmpeg is missing" will otherwise
                # either give up or hand the user shell commands.
                provision = await self._provision(sandbox, int(kwargs.get("wait") or _INSTALL_WAIT_SECONDS))
                payload["auto_provision"] = provision
                payload["message"] = (
                    "The media chain was incomplete, so the tool started the install itself. "
                    "Poll action='status' until ready=true."
                    if not (provision.get("ready") or provision.get("done"))
                    else "The media chain was incomplete, so the tool installed it. Media "
                         "actions work now."
                )
                payload["ready"] = bool(provision.get("ready") or provision.get("done"))
            return json.dumps(payload)

        if action == "job":
            job_id = str(kwargs["job_id"])
            wait = int(kwargs.get("wait") or 30)
            payload = await self._poll_job(sandbox, job_id, wait)
            if payload.get("job_done"):
                inner = payload.get("result")
                if isinstance(inner, dict) and inner.get("ok") is False:
                    return ToolResult.error(json.dumps(inner))
                return json.dumps(inner if isinstance(inner, dict) else payload)
            return json.dumps({
                **payload,
                "job_id": job_id,
                "message": "Still running. Poll action='job' with the same job_id — do NOT "
                           "re-run the edit, that would start a second encode.",
            })

        # --- a normal media action ----------------------------------------- #
        command = build_cli_command(action, kwargs)
        if not command:
            return ToolResult.error(
                f"action='{action}' is missing a required argument, so no command could be "
                "built. Check 'input' (and 'url' for download, 'inputs' for concat)."
            )

        detached = action in _LONG_ACTIONS and (
            bool(kwargs.get("background_run")) or _INLINE_WAIT_SECONDS <= 0
        )
        if detached:
            job_id = _job_id(action, kwargs)
            try:
                await sandbox.execute(
                    action="run",
                    command=f"{bootstrap_command()} >/dev/null 2>&1 || true; "
                            f"{launch_command(action, kwargs, job_id)}",
                    timeout=180,
                )
            except Exception as exc:  # noqa: BLE001
                return ToolResult.error(f"Could not launch the job: {exc}")
            payload = await self._poll_job(sandbox, job_id, int(kwargs.get("wait") or 30))
            if payload.get("job_done"):
                inner = payload.get("result")
                return json.dumps(inner if isinstance(inner, dict) else payload)
            return json.dumps({**payload, "job_id": job_id})

        timeout = int(kwargs.get("timeout") or _TIMEOUTS.get(action, _DEFAULT_TIMEOUT))
        timeout = max(30, min(timeout, _MAX_TIMEOUT))

        if action in _LONG_ACTIONS:
            # Launch detached, then wait a bounded while inline. A short clip
            # therefore still finishes in ONE call, while a long one hands back a
            # job handle instead of a command that the sandbox kills mid-write.
            job_id = _job_id(action, kwargs)
            try:
                await sandbox.execute(
                    action="run",
                    command=f"{bootstrap_command()} >/dev/null 2>&1 || true; "
                            f"{launch_command(action, kwargs, job_id)}",
                    timeout=180,
                )
            except Exception as exc:  # noqa: BLE001
                return ToolResult.error(f"Could not launch the job: {exc}")

            wait = int(kwargs.get("wait") or _INLINE_WAIT_SECONDS)
            wait = max(0, min(wait, _MAX_TIMEOUT - 60))
            payload = await self._poll_job(sandbox, job_id, wait, timeout=wait + 60)
            if payload.get("job_done"):
                inner = payload.get("result")
                if isinstance(inner, dict) and inner.get("ok") is False:
                    return ToolResult.error(json.dumps(inner))
                return json.dumps(inner if isinstance(inner, dict) else payload)
            return json.dumps({
                **payload,
                "job_id": job_id,
                "message": (
                    f"action='{action}' is still running after {wait}s (a long encode or "
                    "download is normal here). Poll action='job' with this job_id — do NOT "
                    "re-run the edit."
                ),
            })

        try:
            rendered = await self._run_cli(sandbox, command, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - transport-level failure
            return ToolResult.error(
                f"The sandbox did not answer for action='{action}': "
                f"{type(exc).__name__}: {exc}. Retry the action."
            )

        payload = _parse_payload(rendered)
        if payload is None:
            tail = (rendered or "").strip()[-500:]
            return ToolResult.error(
                f"action='{action}' produced no JSON result from the sandbox. Output tail: "
                f"{tail or '(empty)'}. If this mentions a missing tool, run action='install'."
            )

        if payload.get("ok") is False:
            if _looks_uninstalled(payload):
                provision = await self._provision(sandbox, int(kwargs.get("wait") or _INSTALL_WAIT_SECONDS))
                if provision.get("ready") or provision.get("done"):
                    retry = await self._run_cli(sandbox, command, timeout=timeout)
                    retried = _parse_payload(retry)
                    if isinstance(retried, dict) and retried.get("ok") is not False:
                        retried["auto_installed_media_chain"] = True
                        return json.dumps(retried)
                    return ToolResult.error(json.dumps({
                        **(retried or {}),
                        "message": "The media chain is installed now, but the action still "
                                   "failed — this is a real error in the request, not a "
                                   "missing tool.",
                    }))
                return ToolResult.error(json.dumps({
                    **payload,
                    "message": (
                        "The media chain is missing and the install is still running. Poll "
                        "action='status' until ready=true, then retry — do NOT install "
                        "anything by hand and do NOT ask the user to."
                    ),
                    "install": provision,
                }))
            return ToolResult.error(json.dumps(payload))
        return json.dumps(payload)
