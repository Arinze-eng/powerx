from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import posixpath
import re
import shlex
import threading
import time
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse
from uuid import uuid4

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.agent.tools.daytona_backend import DaytonaError, DaytonaExecutionBackend
from nanobot.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.agent.tools.upstash_backend import UpstashError, UpstashExecutionBackend
from nanobot.agent.tools.runloop_backend import RunloopError, RunloopExecutionBackend, runloop_devbox_name
from nanobot.agent.tools.tenki_backend import (
    TenkiError,
    TenkiExecutionBackend,
    tenki_sandbox_name,
)
from nanobot.agent.tools.freestyle_backend import (
    FreestyleError,
    FreestyleExecutionBackend,
    freestyle_sandbox_name,
)
from nanobot.agent.tools.vercel_backend import VercelError, VercelExecutionBackend, vercel_sandbox_name
from nanobot.agent.tools.vps_backend import VPSExecutionBackend
from nanobot.config.paths import get_data_dir, get_workspace_path
from nanobot.utils.file_share import (
    FileShareError,
    artifact_delivery_text,
)
from nanobot.utils.file_share import (
    upload_artifact_bytes as upload_shared_artifact_bytes,
)
from nanobot.utils.file_share import (
    upload_artifact_path as upload_shared_artifact,
)
from nanobot.utils.gofile import GoFileError, is_gofile_url, request_file, resolve_gofile_download
from nanobot.utils.helpers import detect_image_mime
from nanobot.utils.onlyfiles import OnlyFilesError
from nanobot.utils.onlyfiles import upload_bytes as upload_onlyfile_bytes
from nanobot.utils.onlyfiles import upload_path as upload_onlyfile_path

try:
    from novita_sandbox import Novita
except ImportError:  # pragma: no cover - optional dependency is checked by enabled()
    Novita = None  # type: ignore[assignment,misc]

_MAX_COMMAND_CHARS = 12_000
_MAX_CONTENT_CHARS = 120_000
_MAX_RESULT_CHARS = 16_000
_MAX_TIMEOUT = 900
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_MAX_TELEGRAM_IMAGE_BYTES = 12 * 1024 * 1024
_MAX_TELEGRAM_IMAGE_COUNT = 4
_MAX_IMAGE_ANALYSIS_RESULT_CHARS = 16_000
# Upstash task-end release budgets: release_upstash_sandbox is awaited INLINE at
# task end (the agent turn blocks on it), so every step inside it must be
# bounded or a cold/wedged box blocks the finished task's reply for minutes.
_UPSTASH_SNAPSHOT_BUDGET = 150
_UPSTASH_RELEASE_RESET_BUDGET = 90
_DAYTONA_SNAPSHOT_BUDGET = 150
_DAYTONA_RELEASE_RESET_BUDGET = 90
_RUNLOOP_KEEP_ALIVE_BUDGET = 60
_RUNLOOP_RELEASE_RESET_BUDGET = 90
# Tenki sessions land in seconds and their control plane is a persistent gRPC
# channel, so both task-end hooks stay short: a wedged call must never delay the
# finished task's reply.
_TENKI_KEEP_ALIVE_BUDGET = 45
_TENKI_RELEASE_RESET_BUDGET = 90
#: Freestyle resumes a paused VM in about a second (it restores from a
#: snapshot), so the keep-alive and reset budgets are the same shape as Tenki's.
_FREESTYLE_KEEP_ALIVE_BUDGET = 45
_FREESTYLE_RELEASE_RESET_BUDGET = 90
# Vercel Sandbox bills by active CPU only, so the win from stopping a finished
# task's sandbox is smaller than for the always-on backends — the budgets stay
# short so a wedged stop can never delay the finished task's reply.
_VERCEL_KEEP_ALIVE_BUDGET = 45
_VERCEL_RELEASE_RESET_BUDGET = 60
_WORKSPACE = "/workspace"
_OCR_DIR = f"{_WORKSPACE}/.nanobot"
#: GitHub credentials are materialised inside the sandbox as a sourced env file
#: (see _write_sandbox_credentials). Keeping them in a file rather than inline on
#: the command line means the token never lands in tool-call logs or output.
_GIT_CREDS_PATH = f"{_OCR_DIR}/github-env.sh"


def _git_creds_guard(path: str) -> str:
    """A source-prefix that is *safe on a POSIX shell*, not just on bash.

    MEASURED FAILURE (2026-09-29, on a Freestyle VM, reproduced on Debian dash
    and therefore on any Ubuntu guest whose ``/bin/sh`` is dash): the obvious
    ``. <path> 2>/dev/null || true`` is FATAL when the file is absent. ``.`` is
    a POSIX *special* built-in, so a failure in a non-interactive shell exits it
    immediately — before ``|| true`` can run. The guest then answers rc=2 with
    no output at all, for every command, including a bare ``pwd && echo hi``.
    Since the file is only written when a GitHub token is configured, every
    deployment without one had its sandbox reduce to "the AI's commands return
    nothing and exit 2".

    MEASURED FAILURE #2 (same day, same symptom, opposite cause): ``[ -f ]`` is
    the WRONG predicate, because a file that EXISTS but cannot be READ kills the
    shell just as dead. That is exactly what a 0600 file written by another uid
    is, and it is what the deployed service produced — ``-rw------- root root``
    inside a workspace owned by ``ubuntu``. ``.`` against an unreadable file
    aborts a non-interactive dash the same way a missing one does, so every
    command answered rc=2 with no output and the sandbox was unusable from the
    moment credentials were seeded. ``[ -r ]`` covers both cases, and ``sh -n``
    additionally refuses a file whose *contents* are broken (a half-written
    source would otherwise abort the shell too). The trailing ``|| true`` keeps
    the prefix from ever deciding the command's own exit status.

    A skipped source is deliberately not an error: the command runs without git
    credentials, which is strictly better than a sandbox where nothing runs.
    """
    quoted = shlex.quote(path)
    return f"[ -r {quoted} ] && sh -n {quoted} 2>/dev/null && . {quoted} 2>/dev/null || true; "


_GIT_CREDS_SOURCE = _git_creds_guard(_GIT_CREDS_PATH)


def _git_creds_source_for(root: str) -> str:
    """Source-prefix for a credential file under an arbitrary workspace root.

    Each backend has its own workspace root (novita /workspace, upstash
    /workspace/home, vps configurable), so the source line must be built from
    the root actually in use rather than a single hard-coded path.
    """
    return _git_creds_guard(f"{root.rstrip('/')}/.nanobot/github-env.sh")

#: Env vars that may hold a GitHub token, in precedence order. GITHUB_BUILD_TOKEN
#: is the operator-provisioned build account the build_artifact tool uses;
#: GITHUB_TOKEN / GH_TOKEN are the conventional names the `gh` CLI and `git`
#: credential helpers read.
_GITHUB_TOKEN_ENVS = ("GITHUB_BUILD_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


def _github_credentials() -> dict[str, str]:
    """Collect GitHub credentials available to the runtime.

    Returns an empty dict when no token is configured, in which case sandbox
    commands run unauthenticated exactly as before.
    """
    token = ""
    for name in _GITHUB_TOKEN_ENVS:
        candidate = os.environ.get(name, "").strip()
        if candidate:
            token = candidate
            break
    if not token:
        return {}
    creds = {"GITHUB_TOKEN": token, "GH_TOKEN": token}
    owner = os.environ.get("GITHUB_BUILD_OWNER", "").strip()
    if owner:
        creds["GITHUB_BUILD_OWNER"] = owner
    return creds


def _git_identity() -> tuple[str, str]:
    """Resolve the commit identity used inside the sandbox."""
    owner = os.environ.get("GITHUB_BUILD_OWNER", "").strip() or "nanobot"
    email = os.environ.get("GIT_COMMIT_EMAIL", "").strip() or f"{owner}@users.noreply.github.com"
    return owner, email

def _git_creds_script() -> str:
    """Shell script body exporting GitHub credentials inside a sandbox/box.

    Shared by every execution backend (novita / upstash / vps / daytona /
    runloop). The token is written to a file rather than passed inline on the
    command line because commands are logged - an inline token would leak into
    tool-call logs and transcript history.
    """
    creds = _github_credentials()
    if not creds:
        return ""
    owner, email = _git_identity()
    lines = ["# Managed by nanobot - do not edit.", "#!/bin/sh"]
    for name, value in creds.items():
        lines.append(f"export {name}={shlex.quote(value)}")
    lines.append(
        "git config --global credential.helper "
        "'!f() { echo username=x-access-token; echo password=$GITHUB_TOKEN; }; f' "
        "2>/dev/null || true"
    )
    lines.append(f"git config --global user.name {shlex.quote(owner)} 2>/dev/null || true")
    lines.append(f"git config --global user.email {shlex.quote(email)} 2>/dev/null || true")
    return "\n".join(lines) + "\n"

#: Automatic Novita sandbox sizing when the admin configured none. The stock
#: "base" image ships ~486 MB which OOM-kills builds/OCR, so we default every
#: spawned sandbox to 4 GB. 2 GB was not enough for the documented heavy task —
#: the Wine + MetaTrader 5 stack (MT5 docs/mt5-sandbox-login.md) needs ~4 GB:
#: a 2 GB box is OOM-killed mid-prefix-build or when the terminal plus the
#: MetaTrader5 bridge load numpy, which users saw as "the MT5 install hangs /
#: the trade never happens". Env (NOVITA_SANDBOX_MEMORY_MB / _CPU_COUNT) or
#: execution.novita_template still override these; see _template_sizing(). The
#: admin UI default and the verified MT5 template (`powerx-base-4g-c2`) are
#: both 4 GB, so this keeps the code path in step with the documentation.
DEFAULT_TEMPLATE_CPU = 2
DEFAULT_TEMPLATE_MEMORY_MB = 4096

_TELEGRAM_IMAGE_SCRIPT = r'''import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
except ImportError:
    Image = ImageEnhance = ImageFilter = ImageOps = None
    if os.getenv("NANOBOT_OCR_ALLOW_PILLOW_INSTALL") == "1":
        import subprocess as install_subprocess
        try:
            install_subprocess.run(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "Pillow"],
                stdout=install_subprocess.DEVNULL,
                stderr=install_subprocess.DEVNULL,
                check=True,
                timeout=45,
            )
            from PIL import Image, ImageEnhance, ImageFilter, ImageOps
        except Exception:
            Image = ImageEnhance = ImageFilter = ImageOps = None


def fail(message):
    print(json.dumps({"error": message}, ensure_ascii=False))
    raise SystemExit(1)


def prepare_image(path, output_path):
    if Image is None:
        return False
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        # Upscaling and contrast normalization improve OCR for Telegram previews
        # without changing the original uploaded file.
        scale = max(1, min(4, 1800 // max(image.width, image.height)))
        if scale > 1:
            image = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS)
        image = ImageEnhance.Contrast(image).enhance(1.35)
        image = image.filter(ImageFilter.SHARPEN)
        image.save(output_path, format="PNG", optimize=True)
    return True


def install_tesseract():
    if shutil.which("tesseract") is not None:
        return True
    # VPS execution must never perform an implicit apt-get/sudo operation.
    # Novita opts in explicitly when it runs this script so its existing
    # first-use installation behavior remains unchanged.
    if os.getenv("NANOBOT_OCR_ALLOW_INSTALL") != "1":
        return False
    commands = [
        ["apt-get", "update", "-qq"],
        ["apt-get", "install", "-y", "-qq", "tesseract-ocr", "tesseract-ocr-eng"],
    ]
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
            if result.returncode != 0:
                sudo_command = ["sudo", "-n", *command]
                result = subprocess.run(sudo_command, capture_output=True, text=True, check=False, timeout=120)
        except OSError:
            return False
        if result.returncode != 0:
            return False
    return shutil.which("tesseract") is not None


TESSERACT_BINARY = shutil.which("tesseract") or (install_tesseract() and shutil.which("tesseract"))


def _tesseract_environment():
    """Add bundled Tesseract library directories to the child process loader path."""
    environment = os.environ.copy()
    if not TESSERACT_BINARY:
        return environment
    binary = Path(TESSERACT_BINARY)
    library_dirs = []
    for parent in (binary.parent, *binary.parents):
        for candidate in (parent / "lib", parent / "usr" / "lib", parent / "usr" / "lib" / "x86_64-linux-gnu"):
            if candidate.is_dir():
                library_dirs.append(str(candidate))
    current = environment.get("LD_LIBRARY_PATH", "")
    entries = list(dict.fromkeys(library_dirs + ([current] if current else [])))
    if entries:
        environment["LD_LIBRARY_PATH"] = ":".join(entries)
    return environment


def run_tesseract(image_path, psm):
    if not TESSERACT_BINARY:
        return "", []
    command = [TESSERACT_BINARY, str(image_path), "stdout", "--oem", "3", "--psm", str(psm), "tsv"]
    try:
        timeout = max(5, min(int(os.getenv("NANOBOT_OCR_TIMEOUT_SECONDS", "90")), 90))
    except ValueError:
        timeout = 90
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=_tesseract_environment(),
    )
    if result.returncode != 0:
        return "", []
    words = []
    for line in result.stdout.splitlines()[1:]:
        columns = line.split("\t")
        if len(columns) < 12:
            continue
        text = columns[11].strip()
        if not text:
            continue
        try:
            confidence = float(columns[10])
        except ValueError:
            confidence = -1.0
        words.append((text, confidence))
    if not words:
        return "", []
    text = " ".join(word for word, _ in words)
    confidence = sum(conf for _, conf in words if conf >= 0) / max(1, sum(1 for _, conf in words if conf >= 0))
    return text, [(confidence, len(words))]


def describe_image(path):
    """Best-effort Pillow metadata line for an image (used when OCR is absent).

    Returns "" if Pillow is unavailable or the file can't be opened, so callers
    treat it as optional enrichment rather than a hard dependency.
    """
    if Image is None:
        return ""
    try:
        with Image.open(path) as image:
            width, height = image.size
            mode = getattr(image, "mode", "?")
            fmt = getattr(image, "format", "?") or Path(path).suffix.lstrip(".").upper()
        return f"Image metadata: {fmt} {width}x{height}, mode {mode} (no text extracted)."
    except Exception:
        return ""


def read_image(path, temp_dir):
    try:
        prepared = Path(temp_dir) / (Path(path).stem + "_prepared.png")
        prepared_path = prepared if prepare_image(path, prepared) else Path(path)
        candidates = []
        for psm in (6, 11, 3):
            text, scores = run_tesseract(prepared_path, psm)
            if text:
                confidence, word_count = scores[0]
                candidates.append((confidence, word_count, text, psm))
        if not candidates:
            if not TESSERACT_BINARY:
                detail = (
                    f"File: {Path(path).name}\n"
                    "Tesseract is unavailable on this execution backend; "
                    "an administrator must install tesseract-ocr before image OCR can run."
                )
                meta = describe_image(path)
                return f"{detail}\n{meta}" if meta else detail
            return f"File: {Path(path).name}\nTesseract detected no readable text."
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        confidence, word_count, text, psm = candidates[0]
        return (
            f"File: {Path(path).name}\n"
            f"Tesseract OCR confidence: {confidence:.1f}%\n"
            f"Tesseract page mode: {psm}; words: {word_count}\n"
            f"Recognized text:\n{text}"
        )
    except Exception as exc:
        return f"File: {Path(path).name}\nTesseract could not read this image: {type(exc).__name__}."


if len(sys.argv) != 2:
    fail("invalid image-analysis arguments")
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        image_paths = json.load(handle)
except Exception as exc:
    fail(f"could not read image manifest: {type(exc).__name__}")
if not isinstance(image_paths, list) or not image_paths:
    fail("no image paths supplied")

with __import__("tempfile").TemporaryDirectory() as temp_dir:
    content = "\n\n".join(read_image(str(path), temp_dir) for path in image_paths if path)
if not content:
    fail("no OCR results produced")
print(json.dumps({"content": content}, ensure_ascii=False))
'''


class SandboxBusyError(RuntimeError):
    """Raised when a per-session sandbox lock cannot be acquired in time."""


class _BoundedSectionLock:
    """`async with` adapter that bounds *acquiring* the underlying asyncio.Lock.

    The raw lock is fine once held; the danger is a stuck holder (a hung HTTP
    call, an OCR run that outlived its budget) making every later sandbox call
    for the same session queue forever with no error and no output — to the
    user that reads as "the AI hangs". Acquisition is now capped; on timeout we
    raise SandboxBusyError so the model gets an actionable message instead.
    """

    __slots__ = ("_lock", "_timeout")

    def __init__(self, lock: asyncio.Lock, timeout: float) -> None:
        self._lock = lock
        self._timeout = timeout

    async def __aenter__(self) -> None:
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=self._timeout)
        except (asyncio.TimeoutError, TimeoutError):
            raise SandboxBusyError(
                "another sandbox operation for this session is still running"
            ) from None

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._lock.release()


class _SandboxStore:
    """In-memory handles with a small disk index so sessions can resume after a restart."""

    def __init__(self, index_path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._handles: dict[str, Any] = {}
        self._ids: dict[str, str] = {}
        # session key -> template alias that sandbox was created from. Lets us
        # detect a sizing change (old base box vs new 2 GB target) and rebuild
        # instead of reusing an undersized running instance forever.
        self._templates: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        if index_path is not None:
            self._index_path = index_path
        else:
            path = os.getenv("NANOBOT_DATA_DIR", "").strip()
            self._index_path = Path(path).expanduser() / "novita_sandboxes.json" if path else Path.home() / ".nanobot" / "novita_sandboxes.json"
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if "ids" in raw or "templates" in raw:
                    ids_raw = raw.get("ids") or {}
                    tmpl_raw = raw.get("templates") or {}
                    if isinstance(ids_raw, dict):
                        self._ids = {str(k): str(v) for k, v in ids_raw.items() if v}
                    if isinstance(tmpl_raw, dict):
                        self._templates = {str(k): str(v) for k, v in tmpl_raw.items() if v}
                else:
                    # Legacy format: the whole file WAS the id map. Rebuild under
                    # the new schema on next write; treat every entry as unknown
                    # template so it gets recreated once at the new sizing.
                    self._ids = {str(k): str(v) for k, v in raw.items() if v}
        except (OSError, ValueError):
            pass

    def lock_for(self, key: str, *, timeout: float = 960.0) -> "_BoundedSectionLock":
        with self._lock:
            lock = self._locks.setdefault(key, asyncio.Lock())
        return _BoundedSectionLock(lock, timeout)

    def get(self, key: str) -> Any | None:
        with self._lock:
            return self._handles.get(key)

    def set(self, key: str, sandbox: Any, *, template: str | None = None) -> None:
        sandbox_id = str(getattr(sandbox, "sandbox_id", "") or getattr(sandbox, "id", ""))
        with self._lock:
            self._handles[key] = sandbox
            if sandbox_id:
                self._ids[key] = sandbox_id
                if template:
                    # Remember which template this sandbox came from so a later
                    # sizing change (e.g. base -> 2 GB) forces a rebuild instead
                    # of silently reusing an undersized running box.
                    self._templates[key] = template
                try:
                    self._index_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = self._index_path.with_suffix(".tmp")
                    payload = {"ids": self._ids, "templates": self._templates}
                    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    tmp.replace(self._index_path)
                except OSError:
                    logger.warning("Could not persist Novita sandbox index")

    def sandbox_id(self, key: str) -> str | None:
        with self._lock:
            return self._ids.get(key)

    def template_for(self, key: str) -> str | None:
        """Template alias the persisted sandbox for *key* was created from."""
        with self._lock:
            return self._templates.get(key)

    def remove(self, key: str) -> None:
        with self._lock:
            self._handles.pop(key, None)
            self._ids.pop(key, None)
            self._templates.pop(key, None)
            try:
                payload = {"ids": self._ids, "templates": self._templates}
                self._index_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            except OSError:
                pass


_STORE = _SandboxStore()


class _UpstashBoxStore(_SandboxStore):
    """Disk-indexed session → Upstash box id map (no in-process handles needed)."""

    def __init__(self) -> None:
        super().__init__()
        path = os.getenv("NANOBOT_DATA_DIR", "").strip()
        base = Path(path).expanduser() if path else Path.home() / ".nanobot"
        # Point the inherited persistence at a dedicated index file.
        self._index_path = base / "upstash_boxes.json"
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._ids = {str(k): str(v) for k, v in raw.items() if v}
        except (OSError, ValueError):
            pass

    def set_id(self, key: str, box_id: str) -> None:
        """Persist a session → box id mapping without a live handle."""
        with self._lock:
            self._ids[key] = str(box_id)
            try:
                self._index_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._index_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._ids, indent=2), encoding="utf-8")
                tmp.replace(self._index_path)
            except OSError:
                logger.warning("Could not persist Upstash box index")


_UPSTASH_STORE = _UpstashBoxStore()


class _DaytonaSandboxStore(_SandboxStore):
    """Disk-indexed session → Daytona sandbox id map (no in-process handles needed)."""

    def __init__(self) -> None:
        super().__init__()
        path = os.getenv("NANOBOT_DATA_DIR", "").strip()
        base = Path(path).expanduser() if path else Path.home() / ".nanobot"
        # Point the inherited persistence at a dedicated index file.
        self._index_path = base / "daytona_sandboxes.json"
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._ids = {str(k): str(v) for k, v in raw.items() if v}
        except (OSError, ValueError):
            pass

    def set_id(self, key: str, sandbox_id: str) -> None:
        """Persist a session → sandbox id mapping without a live handle."""
        with self._lock:
            self._ids[key] = str(sandbox_id)
            try:
                self._index_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._index_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._ids, indent=2), encoding="utf-8")
                tmp.replace(self._index_path)
            except OSError:
                logger.warning("Could not persist Daytona sandbox index")


_DAYTONA_STORE = _DaytonaSandboxStore()


class _RunloopDevboxStore(_SandboxStore):
    """Disk-indexed session → Runloop devbox id map (no in-process handles needed)."""

    def __init__(self) -> None:
        super().__init__()
        path = os.getenv("NANOBOT_DATA_DIR", "").strip()
        base = Path(path).expanduser() if path else Path.home() / ".nanobot"
        # Point the inherited persistence at a dedicated index file.
        self._index_path = base / "runloop_devboxes.json"
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._ids = {str(k): str(v) for k, v in raw.items() if v}
        except (OSError, ValueError):
            pass

    def set_id(self, key: str, devbox_id: str) -> None:
        """Persist a session → devbox id mapping without a live handle."""
        with self._lock:
            self._ids[key] = str(devbox_id)
            try:
                self._index_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._index_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._ids, indent=2), encoding="utf-8")
                tmp.replace(self._index_path)
            except OSError:
                logger.warning("Could not persist Runloop devbox index")


_RUNLOOP_STORE = _RunloopDevboxStore()


class _VercelSandboxStore(_SandboxStore):
    """Disk-indexed session → Vercel sandbox id map (no in-process handles needed)."""

    def __init__(self) -> None:
        super().__init__()
        path = os.getenv("NANOBOT_DATA_DIR", "").strip()
        base = Path(path).expanduser() if path else Path.home() / ".nanobot"
        # Point the inherited persistence at a dedicated index file.
        self._index_path = base / "vercel_sandboxes.json"
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._ids = {str(k): str(v) for k, v in raw.items() if v}
        except (OSError, ValueError):
            pass

    def set_id(self, key: str, sandbox_id: str) -> None:
        """Persist a session → sandbox id mapping without a live handle."""
        with self._lock:
            self._ids[key] = str(sandbox_id)
            try:
                self._index_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._index_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._ids, indent=2), encoding="utf-8")
                tmp.replace(self._index_path)
            except OSError:
                logger.warning("Could not persist Vercel sandbox index")


_VERCEL_STORE = _VercelSandboxStore()


class _TenkiSessionStore(_SandboxStore):
    """Session → Tenki session id map on disk, plus the key-rotation state.

    The lane each session lives in is stored beside its session id because the
    two must agree: rotating is only safe for a session whose workspace is
    known, and a session whose lane was forgotten would be looked for in a
    workspace that has never seen its files. The round-robin cursor and any
    parked lanes live here too, so a restart resumes the same order instead of
    stampeding one workspace again.

    This class is also the rotation implementation the backend is handed: it
    satisfies the same ``next_lane`` / ``park`` / ``parked`` / ``clear``
    interface as ``TenkiRotationState`` (the in-memory default), with absolute
    epoch deadlines instead of monotonic offsets because the process that reads
    the state back is not the one that wrote it.

    The file is written in one shape (``sessions`` / ``lanes`` / ``cursor`` /
    ``parked``) and read back in either that shape or the older flat
    session → id map, so an existing deployment upgrades on first write.
    """

    #: Cooldown for a lane that failed in a way saying "this workspace cannot
    #: serve us" — quota exhausted, key rejected. Short enough that a workspace
    #: which frees up is retried soon; long enough to stop hammering it.
    PARK_SECONDS = 900.0

    #: File the store persists to. Overridden per provider so two rotating
    #: backends never share one index (a shared file would send Tenki's lane
    #: pins to Freestyle's accounts and vice versa).
    INDEX_NAME = "tenki_sessions.json"

    def __init__(self) -> None:
        super().__init__()
        path = os.getenv("NANOBOT_DATA_DIR", "").strip()
        base = Path(path).expanduser() if path else Path.home() / ".nanobot"
        # Point the inherited persistence at a dedicated index file.
        self._index_path = base / self.INDEX_NAME
        self._lanes: dict[str, int] = {}
        self._cursor = 0
        self._parked: dict[int, float] = {}
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict):
            # ``ids``/``templates`` is the shape the inherited ``set``/``remove``
            # write. It is recognised here so a file an older build left in that
            # form still reloads as sessions instead of being mistaken for the
            # legacy flat map and scrambled.
            if (
                "sessions" in raw
                or "lanes" in raw
                or "cursor" in raw
                or ("ids" in raw and "templates" in raw)
            ):
                sessions = raw.get("sessions") or raw.get("ids") or {}
                if isinstance(sessions, dict):
                    self._ids = {str(k): str(v) for k, v in sessions.items() if v}
                lanes = raw.get("lanes") or {}
                if isinstance(lanes, dict):
                    for session_key, lane in lanes.items():
                        try:
                            self._lanes[str(session_key)] = int(lane)
                        except (TypeError, ValueError):
                            continue
                try:
                    self._cursor = int(raw.get("cursor") or 0)
                except (TypeError, ValueError):
                    self._cursor = 0
                parked = raw.get("parked") or {}
                if isinstance(parked, dict):
                    for lane, until in parked.items():
                        try:
                            self._parked[int(lane)] = float(until)
                        except (TypeError, ValueError):
                            continue
            else:
                # Legacy format: the file WAS the flat session → id map. Keep the
                # sessions and rebuild under the new schema on the next write.
                self._ids = {str(k): str(v) for k, v in raw.items() if v}

    def _save(self) -> None:
        """Write the whole state atomically. The caller holds ``self._lock``."""
        try:
            self._index_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._index_path.with_suffix(".tmp")
            payload = {
                "sessions": self._ids,
                "lanes": dict(self._lanes),
                "cursor": self._cursor,
                "parked": {str(lane): until for lane, until in self._parked.items()},
            }
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._index_path)
        except OSError:
            logger.warning("Could not persist Tenki session index")

    def set_id(self, key: str, session_id: str) -> None:
        """Persist a session → Tenki session id mapping without a live handle."""
        with self._lock:
            self._ids[key] = str(session_id)
            self._save()

    def remove(self, key: str) -> None:
        """Drop a session entirely: its handle, its id, and its lane pin.

        Overridden because the inherited version persists the Novita-shaped
        payload, which the loader above would have to mistake for a legacy map —
        losing every session id, lane, cursor and cooldown in the file. Dropping
        the lane matters as much as dropping the id: a session that was reset
        must not leave a stale pin behind that drags every later session back to
        a workspace it no longer occupies.
        """
        with self._lock:
            self._handles.pop(key, None)
            self._ids.pop(key, None)
            self._templates.pop(key, None)
            self._lanes.pop(key, None)
            self._save()

    # ------------------------------------------------- key rotation (lanes)

    def lane(self, key: str) -> int | None:
        """The lane a session's VM lives in, or ``None`` when it has none yet."""
        with self._lock:
            lane = self._lanes.get(key)
        return lane if isinstance(lane, int) and lane >= 0 else None

    def set_lane(self, key: str, lane: int) -> None:
        """Pin a session to the lane its workspace was created in."""
        with self._lock:
            self._lanes[key] = int(lane)
            self._save()

    def forget(self, key: str) -> None:
        """Drop a session's lane pin so its next session picks a fresh one."""
        with self._lock:
            self._lanes.pop(key, None)
            self._save()

    def _expire_parked(self) -> None:
        now = time.time()
        self._parked = {lane: until for lane, until in self._parked.items() if until > now}

    def next_lane(self, count: int, *, skip: set[int] | None = None) -> int:
        """Round-robin the next lane for a NEW session, parked lanes last.

        Mirrors ``TenkiRotationState.next_lane`` deliberately: when every lane
        is parked a full order is still returned, because a stale cooldown must
        never become "no lane was tried at all".
        """
        with self._lock:
            self._expire_parked()
            if count <= 1:
                return 0
            blocked = set(skip or set()) | set(self._parked)
            start = self._cursor % count
            self._cursor = (self._cursor + 1) % count
            order = [(start + i) % count for i in range(count)]
            healthy = [lane for lane in order if lane not in blocked]
            lane = (healthy + order)[0]
            self._save()
            return lane

    def park(self, lane: int, *, seconds: float = PARK_SECONDS) -> None:
        """Stop offering *lane* first for a while after it failed on us."""
        with self._lock:
            self._parked[int(lane)] = time.time() + float(seconds)
            self._save()

    def parked(self) -> set[int]:
        with self._lock:
            self._expire_parked()
            return set(self._parked)

    def clear(self) -> None:
        with self._lock:
            self._parked.clear()
            self._save()


_TENKI_STORE = _TenkiSessionStore()


class _FreestyleSessionStore(_TenkiSessionStore):
    """Tenki's session→id / lane-pin store, pointed at its own index file.

    The rotation contract is identical (``next_lane`` / ``park`` / ``parked`` /
    ``lane`` / ``set_lane`` / ``clear``), so it is inherited rather than
    reimplemented. The one Freestyle-specific fact is that these pins describe
    *Freestyle accounts*, which must never share Tenki's index file.
    """

    INDEX_NAME = "freestyle_sessions.json"


_FREESTYLE_STORE = _FreestyleSessionStore()

# Alias cache for dynamically built Novita templates (desired alias → usable alias).
_TEMPLATE_CACHE: dict[str, str] = {}
_TEMPLATE_BUILD_LOCK = threading.Lock()


def _safe_path(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("path is required")
    # The LLM frequently asks to "list /" to see the sandbox root. From its
    # perspective that means the sandbox workspace, not the container's real
    # filesystem root. Map a bare "/" to the workspace root so it does not
    # raise. All other paths outside the workspace remain rejected (secure).
    if value == "/":
        return _WORKSPACE
    if not value.startswith("/"):
        value = posixpath.join(_WORKSPACE, value)
    normalized = posixpath.normpath(value)
    if normalized != _WORKSPACE and not normalized.startswith(_WORKSPACE + "/"):
        raise ValueError("path must remain inside /workspace")
    return normalized


_UPLOAD_SUBDIR = "uploads"

#: Appended to every successful ``upload`` result. The turn that reported a
#: user attachment as unreachable had just watched its own staging call die (see
#: ``_stage_upload``); with nothing in the sandbox it concluded the media
#: directory was off limits. The attachment was never unreachable — both
#: Cloudinary entry points read a local path straight off this host.
_UPLOAD_NOTE = (
    " The local file remains readable on this host, so generate_image "
    "(reference_images) and cloudinary_video_edit (source) can take its path "
    "directly — an edit does not need this staging step."
)


def _upload_destination(source: Path, requested: Any, *, root: str = _WORKSPACE) -> str:
    """Where an ``upload`` of *source* lands inside the remote sandbox.

    A model that sends the attachment as ``source`` and omits ``path`` used to
    reach the backend with an empty destination, where the path guard raised
    ``ValueError: path is required``. An omitted destination now means
    ``<root>/uploads/<filename>``.
    """
    target = str(requested or "").strip()
    if target:
        return target
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", source.name)[:120] or "upload.bin"
    return f"{root.rstrip('/')}/{_UPLOAD_SUBDIR}/{safe_name}"


def _upload_failure(source: Path, path: str, label: str, exc: BaseException) -> str:
    """The message the model gets when staging a local file fails.

    It must say the local file itself is fine, because the failure mode this
    replaces ended with the model telling the user the media directory could not
    be reached at all.
    """
    return (
        f"could not upload {source.name} into {label} ({path}): "
        f"{type(exc).__name__}: {str(exc)[:300]}. "
        f"{source} is a readable local file on this host — local tools and the "
        "Cloudinary tools can use it without staging it in a sandbox."
    )


def _session_key() -> str:
    ctx = current_request_context()
    if ctx is None:
        return "unknown"
    return ctx.session_key or f"{ctx.channel}:{ctx.chat_id}"


def _output(result: Any) -> str:
    stdout = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    code = getattr(result, "exit_code", getattr(result, "exitCode", None))
    text = stdout
    if stderr:
        text += f"\n[stderr]\n{stderr}"
    if code is not None:
        text += f"\n[exit_code={code}]"
    return text[-_MAX_RESULT_CHARS:] or "(no output)"


async def _install_tesseract_resilient(backend: Any) -> bool:
    """Best-effort tesseract install inside an Upstash Box; never raises.

    The previous approach ran a single combined ``apt-get install -y tesseract-ocr
    tesseract-ocr-eng`` — on Alpine (``*-alpine`` runtimes) the English-data
    package does not exist under that name (it ships in the base package), so the
    whole command exited non-zero and OCR hard-failed even though tesseract could
    have installed fine. Here we try each candidate package set independently and
    stop as soon as the binary appears, tolerating partial failures. Returns True
    when tesseract ends up on PATH.
    """

    def _probe_ok(rendered: str) -> bool:
        # run() renders exit codes as "[exit_code=N]"; treat only 0/None as ready.
        import re as _re

        match = _re.search(r"\[exit_code=(-?\d+)\]", rendered)
        return match is None or match.group(1) == "0"

    # Ordered candidate groups; first that yields a working binary wins. Debian
    # apt names first (default python runtime), then Alpine/busybox variants.
    candidates = [
        ["tesseract-ocr", "tesseract-ocr-eng"],
        ["tesseract-ocr"],
        ["tesseract"],
    ]
    for group in candidates:
        try:
            await backend.install_packages(group, timeout=600)
        except Exception as exc:  # noqa: BLE001 - one bad group must not abort others
            logger.debug("Upstash tesseract install attempt {} failed: {}", group, type(exc).__name__)
            continue
        try:
            probe = await backend.run(
                "command -v tesseract >/dev/null 2>&1 && printf READY || printf MISSING",
                timeout=30,
            )
        except Exception:
            continue
        if "READY" in probe:
            return True
    return False


def _freestyle_key_configured(config: Any | None) -> bool:
    """True when Freestyle has a usable key, in EITHER stored form.

    The same rule as Tenki and for the same reason: rotation is configured with
    the plural lane list, which deliberately leaves the legacy single key empty,
    so a check that read only ``api_key`` would refuse a deployment whose keys
    were configured correctly.
    """
    return _tenki_key_configured(config)


def _tenki_key_configured(config: Any | None) -> bool:
    """True when Tenki has a usable key, in EITHER stored form.

    Rotation is configured with the plural ``api_keys`` list, which leaves the
    legacy single ``api_key`` empty on purpose: the backend resolves the list and
    never reads the singular field. Checking only ``api_key`` therefore refused a
    deployment whose keys were configured correctly as rotation lanes, and made
    ``enabled`` decline to offer the tool at all. The admin Test button has always
    accepted either form, which is what let the two disagree.
    """
    if config is None:
        return False
    if [key for key in (getattr(config, "api_keys", None) or []) if str(key).strip()]:
        return True
    return bool(str(getattr(config, "api_key", "") or "").strip())


#: Tokens that turn up in an ``install`` payload but are never package names.
#: A model that reaches for ``action=install`` habitually sends the packages
#: under ``command`` (the key ``run`` takes), and often sends a whole
#: ``sudo apt-get install -y nmap`` line rather than a bare name, so both the
#: key and the contents have to be read leniently.
_INSTALL_NOISE_TOKENS = frozenset(
    {
        "sudo", "apt", "apt-get", "aptitude", "apk", "dnf", "yum", "zypper",
        "pacman", "install", "add", "update", "upgrade", "reinstall", "sh",
        "bash", "-c", "&&", "||", ";", "|", "&", ">", ">>", "<", "export",
        "env", "command", "which", "run", "yes",
        "-y", "--yes", "-q", "-qq", "--no-cache", "--no-install-recommends",
    }
)

_PACKAGE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.:@~=-]{0,127}")

#: Package names never contain shell punctuation, so splitting on it recovers
#: the names from a pasted one-liner ("nmap;rm -rf /", "nmap&&curl") instead of
#: discarding the token wholesale.
_PACKAGE_SPLIT_RE = re.compile(r"[\s,;|&<>()]+")

#: Returned instead of raising when an ``install`` call carries no usable
#: package names. The model has to be told which argument to use; the
#: ``ValueError`` the backends raise for an empty list only produced an
#: opaque "VM operation failed".
_INSTALL_NEEDS_PACKAGES = (
    'The install action needs the package names in the "packages" argument, for '
    'example {"action": "install", "packages": "nmap curl"}. "command" belongs to '
    "action=run. Do not repeat the same payload unchanged: send packages, or fold "
    "the install into a run command."
)


def _clean_install_package_names(raw: Any) -> list[str]:
    """Reduce whatever the model sent to plausible distro package names."""
    if isinstance(raw, (list, tuple, set)):
        text = " ".join(str(item) for item in raw)
    else:
        text = str(raw or "")
    tokens = _PACKAGE_SPLIT_RE.split(text)
    names: list[str] = []
    for token in tokens:
        token = token.strip().strip("'\"")
        if not token or token.startswith("-"):
            continue
        if token.lower() in _INSTALL_NOISE_TOKENS:
            continue
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*=.*", token):  # DEBIAN_FRONTEND=noninteractive
            continue
        if not _PACKAGE_NAME_RE.fullmatch(token):
            continue
        if token not in names:
            names.append(token)
    return names


def _install_packages_from_kwargs(kwargs: dict[str, Any]) -> list[str]:
    """Package names for ``action=install``, read from ``packages`` or ``command``.

    MEASURED FAILURE (2026-09-29, from this deployment's Northflank log): the
    model called ``novita_sandbox({"action": "install", "command": "nmap"})``.
    Only ``packages`` was read, so the list arrived empty, the backend's
    ``install_packages`` raised ``ValueError: no valid package names supplied``,
    and the exception escaped this method to the generic tool handler. The model
    was shown "Freestyle VM operation failed" plus a traceback instead of an
    explanation, the tool call failed, and the task could not continue. Accept
    either key so a call shaped like ``run`` still does what it plainly means.
    """
    names = _clean_install_package_names(kwargs.get("packages"))
    if names:
        return names
    return _clean_install_package_names(kwargs.get("command"))


async def _publish_signed_artifact(
    signed_url: str, *, filename: str, timeout: int = 300
) -> dict[str, Any]:
    """Fetch a short-lived sandbox download URL and republish it permanently.

    The Novita sandbox's own ``download_url`` is valid for only a few minutes, so
    it cannot be handed to a user — tapping it later returns nothing. The bytes
    are pulled here while the signature is still live and pushed to the same
    artifact hosts every other backend uses (onlyfiles for small files, catbox
    above the onlyfiles ceiling), which yields the permanent, tap-to-download
    link the delivery text is written around.

    Raises :class:`FileShareError` when the sandbox refuses the download or no
    host accepts the bytes, and :class:`OnlyFilesError` from the upload path.
    """
    import aiohttp

    if not signed_url:
        raise FileShareError("sandbox returned no download URL")
    try:
        timeout_obj = aiohttp.ClientTimeout(total=max(30, min(int(timeout), 900)))
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.get(signed_url, allow_redirects=True) as response:
                if response.status < 200 or response.status >= 300:
                    raise FileShareError(f"sandbox download failed with HTTP {response.status}")
                data = await response.read()
    except aiohttp.ClientError as exc:
        raise FileShareError(f"sandbox download request failed: {type(exc).__name__}") from None
    if not data:
        raise FileShareError("sandbox returned an empty file")
    return await upload_shared_artifact_bytes(data, filename=filename)


@tool_parameters(
    tool_parameters_schema(
        required=["action"],
        additional_properties=None,
        action=StringSchema(
            "Operation: run, read, write, upload, fetch_url, install, list, download_url, apk_toolchain, apk_decompile, apk_build, or reset",
            enum=["run", "read", "write", "upload", "fetch_url", "install", "list", "download_url",
                  "apk_toolchain", "apk_decompile", "apk_build", "reset"],
        ),
        command=StringSchema(
            "The shell command to execute, for action=run. action=install ignores it — "
            "put the package names in packages there."
        ),
        packages=StringSchema(
            "Required for action=install: bare distro package names, space- or "
            'comma-separated, e.g. "nmap curl jq". Not a shell command.'
        ),
        path=StringSchema("Sandbox path, relative paths resolve under /workspace"),
        url=StringSchema("Remote HTTPS URL to fetch into the sandbox (onlyfiles.com or gofile.io)"),
        content=StringSchema("Text content for write"),
        timeout=IntegerSchema(description="Command timeout in seconds", minimum=1, maximum=_MAX_TIMEOUT),
        source=StringSchema("Local media path to upload into the remote sandbox"),
        apk_path=StringSchema("APK path inside the sandbox workspace (apk_decompile)"),
        src=StringSchema("Decompiled APK source dir to rebuild (apk_build)"),
        out=StringSchema("Output APK path (apk_build)"),
    )
)
class NovitaSandboxTool(Tool):
    """Execute agent work in an isolated Novita Sandbox instead of the Render host."""

    config_key = "novita_sandbox"

    @staticmethod
    def _execution_config() -> Any:
        try:
            from nanobot.config.loader import load_config
            from nanobot.config.paths import get_config_path
            from nanobot.execution_env import apply_render_execution_env

            return apply_render_execution_env(load_config(get_config_path())).execution
        except Exception:
            return None

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        # Prefer the live on-disk config (with the durable env overlay applied)
        # over the possibly stale ctx snapshot, so a backend the admin saved
        # moments ago is offered without waiting for a process restart.
        execution = cls._execution_config()
        if execution is None:
            execution = getattr(ctx, "execution", None)
        backend = getattr(execution, "backend", "novita") if execution is not None else "novita"
        if backend == "vps":
            return bool(getattr(execution.vps, "host", "").strip())
        if backend == "upstash":
            return bool(getattr(execution.upstash, "api_key", "").strip())
        if backend == "daytona":
            return bool(getattr(execution.daytona, "api_key", "").strip())
        if backend == "runloop":
            return bool(getattr(execution.runloop, "api_key", "").strip())
        if backend == "tenki":
            return _tenki_key_configured(getattr(execution, "tenki", None))
        if backend == "freestyle":
            return _freestyle_key_configured(getattr(execution, "freestyle", None))
        if backend == "vercel":
            return bool(getattr(execution.vercel, "token", "").strip())
        return bool(os.getenv("NOVITA_API_KEY", "").strip()) and Novita is not None

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls()

    def _selected_backend(self) -> tuple[str, Any | None]:
        execution = self._execution_config()
        if execution is None:
            return "novita", None
        backend = getattr(execution, "backend", "novita")
        if backend == "vps":
            return "vps", getattr(execution, "vps", None)
        if backend == "upstash":
            return "upstash", getattr(execution, "upstash", None)
        if backend == "daytona":
            return "daytona", getattr(execution, "daytona", None)
        if backend == "runloop":
            return "runloop", getattr(execution, "runloop", None)
        if backend == "tenki":
            return "tenki", getattr(execution, "tenki", None)
        if backend == "freestyle":
            return "freestyle", getattr(execution, "freestyle", None)
        if backend == "vercel":
            return "vercel", getattr(execution, "vercel", None)
        return "novita", None

    def backend_name(self) -> str:
        """Return the active execution backend label without exposing credentials."""
        return self._selected_backend()[0]

    @property
    def name(self) -> str:
        return "novita_sandbox"

    @property
    def description(self) -> str:
        return (
            "Use the configured isolated execution backend for coding and operations. "
            "Run shell commands, inspect or write project files, list a workspace, "
            "fetch a remote HTTPS file (onlyfiles.com or gofile.io) into the workspace, "
            "download generated artifacts, or reset the current user sandbox. "
            "When the task is finished and no further work is expected in this session, "
            "call action=reset so the sandbox is killed automatically for the user. "
            "Use this for all coding, tests, builds, package installs, Git, CI/CD work, "
            "and POLLING / WATCHING / MONITORING tasks; "
            "never use the host shell for user work. "
            "For any repeated or 'watch/poll/monitor/keep an eye on' request, run your "
            "polling loop INSIDE the sandbox exactly like watching a GitHub Actions "
            "workflow: write a small script (or use a shell loop with sleep) into the "
            "sandbox workspace, run it with action=run over a bounded duration, and "
            "inspect its output/log to report progress and completion. Do NOT try to "
            "start a background watch on the host — host-side polling is disabled. "
            "When a user sends a gofile.io URL (for example https://gofile.io/d/<code>), "
            "use action=fetch_url with that exact URL as the url argument to pull the "
            "uploaded file into the execution workspace. GoFile share links are resolved "
            "automatically here (a guest token is created and the real direct-download "
            "link is fetched), so gofile.io links always work and do NOT require the user "
            "to attach the file or provide any API token. After fetching, inspect or "
            "process the downloaded file with action=read or run commands. "
            "When the user message contains an [Attachment: local path], use the upload "
            "action first with that exact source path and a safe destination under /workspace "
            "before running or reading the uploaded file remotely. In VPS mode, upload "
            "the local file through onlyfiles.com, then fetch it into the VPS workspace with "
            "curl before using the staged path. If a required Linux command is missing "
            "(nmap, ffmpeg, a compiler, ...), use action=install and put the distro "
            "package names in the packages argument — space- or comma-separated, bare "
            "names such as \"nmap curl jq\". action=install reads packages, NOT command "
            "(command is the key action=run takes); installation is noninteractive and "
            "uses root or already-configured passwordless sudo. Never add repositories, "
            "remove packages, or put a sudo "
            "password in a command. When a finished file should be returned to the user, "
            "ALWAYS call download_url with its remote workspace path; this downloads the "
            "artifact and publishes a public link automatically — files under ~100 MB go to "
            "onlyfiles.com, larger files (up to ~200 MB) go to catbox.moe. The published "
            "link is permanent and third-party (an onlyfiles.com page URL), so give the user "
            "exactly that link and never substitute another one: not a gateway /f/ link on "
            "this deployment's own host, not a signed or preview URL, not a raw transfer "
            "token. Use the local path in the message tool's media parameter when direct "
            "attachment delivery is available. "
            "For multi-step work, prefer the run_plan tool so many operations "
            "cost one model call instead of one call per step."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """SHADOWED — this is not the schema the model is given.

        ``@tool_parameters`` runs after the class body and rebinds
        ``cls.parameters``, so this property never executes; only the
        ``tool_parameters_schema(...)`` call above reaches a provider. It was
        the reason ``packages`` was missing from the live schema for months
        while appearing to be declared here: a parameter added only to this
        dict changes nothing. Kept as a mirror of the live schema so the two do
        not disagree; edit the decorator first.
        """
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["run", "read", "write", "upload", "fetch_url", "install", "list", "download_url", "apk_toolchain", "apk_decompile", "apk_build", "reset"]},
                "command": {"type": "string", "description": "The shell command to execute, for action=run. action=install ignores it — use packages there."},
                "packages": {"type": "string", "description": "Required for action=install: bare distro package names, space- or comma-separated (e.g. \"nmap curl jq\"). Not a shell command."},
                "path": {"type": "string"},
                "url": {"type": "string", "description": "Remote HTTPS URL to fetch into the sandbox (onlyfiles.com or gofile.io)."},
                "content": {"type": "string"},
                "timeout": {"type": "integer", "minimum": 1, "maximum": _MAX_TIMEOUT},
                "source": {"type": "string"},
                "apk_path": {"type": "string", "description": "APK path inside the sandbox workspace (apk_decompile)."},
                "src": {"type": "string", "description": "Decompiled APK source dir to rebuild (apk_build)."},
                "out": {"type": "string", "description": "Output APK path (apk_build)."},
            },
            "required": ["action"],
        }

    def _client(self) -> Any:
        if Novita is None:
            raise RuntimeError("Novita Sandbox SDK is not installed")
        return Novita(api_key=os.environ["NOVITA_API_KEY"])

    async def _analyze_telegram_images_vps(
        self,
        image_paths: list[tuple[Path, bytes]],
        *,
        config: Any,
    ) -> str:
        backend = VPSExecutionBackend(config)
        root = str(config.workspace_dir or _WORKSPACE).rstrip("/") or "/workspace"
        ocr_dir = f"{root}/.nanobot"
        remote_paths: list[str] = []
        manifest_path = f"{ocr_dir}/telegram_image_manifest.json"
        script_path = f"{ocr_dir}/telegram_image_ocr.py"
        try:
            await backend.run(
                f"mkdir -p {shlex.quote(ocr_dir)} {shlex.quote(f'{root}/telegram-images')}",
                timeout=30,
                cwd=root,
            )
            tesseract_probe = await backend.run(
                "if command -v tesseract >/dev/null 2>&1; then printf READY; "
                "else printf MISSING; fi",
                timeout=20,
                cwd=root,
            )
            if "READY" not in tesseract_probe:
                await backend.install_packages(
                    ["tesseract-ocr", "tesseract-ocr-eng"],
                    timeout=600,
                )
                tesseract_probe = await backend.run(
                    "if command -v tesseract >/dev/null 2>&1; then printf READY; "
                    "else printf MISSING; fi",
                    timeout=20,
                    cwd=root,
                )
                if "READY" not in tesseract_probe:
                    raise RuntimeError(
                        "Tesseract was not available after the VPS package installation"
                    )
            await backend.write(script_path, _TELEGRAM_IMAGE_SCRIPT)
            for path, raw in image_paths:
                suffix = path.suffix.lower() if path.suffix else ".img"
                remote_path = f"{root}/telegram-images/{uuid4().hex}{suffix}"
                remote_paths.append(remote_path)
                await upload_onlyfile_bytes(raw, filename=path.name, content_type=detect_image_mime(raw))
                await backend.upload("telegram", remote_path, raw)
            await backend.write(manifest_path, json.dumps(remote_paths))
            output = await backend.run(
                "env NANOBOT_OCR_ALLOW_INSTALL=1 NANOBOT_OCR_ALLOW_PILLOW_INSTALL=1 "
                "NANOBOT_OCR_TIMEOUT_SECONDS=90 "
                f"python3 {shlex.quote(script_path)} {shlex.quote(manifest_path)}",
                timeout=180,
                cwd=root,
            )
            stdout = output.split("\n[stderr]", 1)[0].strip()
            parsed: Any | None = None
            try:
                parsed = json.loads(stdout)
            except (TypeError, ValueError):
                for line in reversed(stdout.splitlines()):
                    candidate = line.strip()
                    if not candidate.startswith("{"):
                        continue
                    try:
                        parsed = json.loads(candidate)
                        break
                    except ValueError:
                        continue
            if not isinstance(parsed, dict) or not str(parsed.get("content") or "").strip():
                logger.warning("VPS returned no usable Tesseract OCR result")
                return "[VPS Tesseract OCR returned no readable result.]"
            return str(parsed["content"]).strip()[:_MAX_IMAGE_ANALYSIS_RESULT_CHARS]
        except Exception as exc:
            logger.warning("VPS Tesseract OCR failed: {}", type(exc).__name__)
            return "[VPS Tesseract OCR failed.]"
        finally:
            if remote_paths:
                with suppress(Exception):
                    await backend.run(
                        "rm -f " + " ".join(shlex.quote(path) for path in remote_paths)
                        + f" {shlex.quote(manifest_path)} {shlex.quote(script_path)}",
                        timeout=30,
                        cwd=root,
                    )

    async def _analyze_telegram_images_daytona(
        self,
        image_paths: list[tuple[Path, bytes]],
        *,
        config: Any,
        session_key: str,
        _retry_on_failure: bool = True,
    ) -> str:
        """Tesseract OCR for Telegram images inside a Daytona sandbox.

        Mirrors the Upstash path: installs (tesseract + Pillow) are allowed inside
        the sandbox, Tesseract gets the same generous 90s timeout, and a failure
        retries once against a fresh sandbox before degrading gracefully.
        """
        from nanobot.agent.tools.daytona_backend import daytona_sandbox_name

        backend = DaytonaExecutionBackend(config, sandbox_name=daytona_sandbox_name(session_key or "telegram"))
        # Reuse the persisted sandbox id (if any) so ensure_sandbox verifies that
        # exact sandbox instead of re-resolving it by name on each OCR run.
        stored_id = _DAYTONA_STORE.sandbox_id(session_key or "telegram")
        if stored_id:
            backend.last_sandbox_id = stored_id
        root = backend.workspace
        ocr_dir = f"{root}/.nanobot"
        remote_paths: list[str] = []
        manifest_path = f"{ocr_dir}/telegram_image_manifest.json"
        script_path = f"{ocr_dir}/telegram_image_ocr.py"
        try:
            await backend.run(f"mkdir -p {shlex.quote(ocr_dir)} {shlex.quote(f'{root}/telegram-images')}", timeout=60)
            # Tesseract is optional: the OCR script degrades to Pillow-based
            # extraction when it is present but tesseract is not, so we must NOT
            # hard-fail just because the binary could not be installed. Install
            # attempts are made best-effort and per-package-group so one missing
            # name (e.g. tesseract-ocr-eng on Alpine, where English data ships in
            # the base package) does not abort the whole install the way a single
            # combined "apt/apk add a b c" would.
            probe = await backend.run(
                "if command -v tesseract >/dev/null 2>&1; then printf READY; else printf MISSING; fi",
                timeout=30,
            )
            if "READY" not in probe:
                await _install_tesseract_resilient(backend)
                probe = await backend.run(
                    "if command -v tesseract >/dev/null 2>&1; then printf READY; else printf MISSING; fi",
                    timeout=30,
                )
                if "READY" not in probe:
                    logger.warning(
                        "Daytona sandbox: tesseract unavailable after install attempts; "
                        "falling back to Pillow-only image analysis"
                    )
            await backend.write(script_path, _TELEGRAM_IMAGE_SCRIPT)
            for path, raw in image_paths:
                suffix = path.suffix.lower() if path.suffix else ".img"
                remote_path = f"{root}/telegram-images/{uuid4().hex}{suffix}"
                remote_paths.append(remote_path)
                await backend.write_bytes(remote_path, raw)
            await backend.write(manifest_path, json.dumps(remote_paths))
            output = await backend.run(
                "env NANOBOT_OCR_ALLOW_INSTALL=1 NANOBOT_OCR_ALLOW_PILLOW_INSTALL=1 "
                "NANOBOT_OCR_TIMEOUT_SECONDS=90 "
                f"python3 {shlex.quote(script_path)} {shlex.quote(manifest_path)}",
                timeout=180,
            )
            stdout = output.split("\n[stderr]", 1)[0].strip()
            parsed: Any | None = None
            try:
                parsed = json.loads(stdout)
            except (TypeError, ValueError):
                for line in reversed(stdout.splitlines()):
                    candidate = line.strip()
                    if not candidate.startswith("{"):
                        continue
                    try:
                        parsed = json.loads(candidate)
                        break
                    except ValueError:
                        continue
            if not isinstance(parsed, dict) or not str(parsed.get("content") or "").strip():
                logger.warning("Daytona sandbox returned no usable Tesseract OCR result")
                return "[Daytona sandbox Tesseract OCR returned no readable result.]"
            return str(parsed["content"]).strip()[:_MAX_IMAGE_ANALYSIS_RESULT_CHARS]
        except Exception as exc:
            logger.warning("Daytona sandbox Tesseract OCR failed: {}", type(exc).__name__)
            if _retry_on_failure:
                sandbox_id = _DAYTONA_STORE.sandbox_id(session_key or "telegram")
                with suppress(Exception):
                    await backend.reset(sandbox_id)
                _DAYTONA_STORE.remove(session_key or "telegram")
                return await self._analyze_telegram_images_daytona(
                    image_paths,
                    config=config,
                    session_key=session_key,
                    _retry_on_failure=False,
                )
            return "[Daytona sandbox Tesseract OCR failed.]"

    async def _analyze_telegram_images_runloop(
        self,
        image_paths: list[tuple[Path, bytes]],
        *,
        config: Any,
        session_key: str,
        _retry_on_failure: bool = True,
    ) -> str:
        """Tesseract OCR for Telegram images inside a Runloop Devbox.

        Mirrors the Daytona/Upstash path: installs (tesseract + Pillow) are allowed
        inside the devbox, Tesseract gets the same generous 90s timeout, and a
        failure retries once against a fresh devbox before degrading gracefully.
        """
        backend = RunloopExecutionBackend(config, devbox_name=runloop_devbox_name(session_key or "telegram"))
        # Reuse the persisted devbox id (if any) so ensure_devbox verifies that
        # exact devbox instead of re-resolving it by name on each OCR run.
        stored_id = _RUNLOOP_STORE.sandbox_id(session_key or "telegram")
        if stored_id:
            backend.last_devbox_id = stored_id
        root = backend.workspace
        ocr_dir = f"{root}/.nanobot"
        remote_paths: list[str] = []
        manifest_path = f"{ocr_dir}/telegram_image_manifest.json"
        script_path = f"{ocr_dir}/telegram_image_ocr.py"
        devbox_reset = False
        try:
            await backend.run(f"mkdir -p {shlex.quote(ocr_dir)} {shlex.quote(f'{root}/telegram-images')}", timeout=60)
            # Tesseract is optional: the OCR script degrades to Pillow-based
            # extraction when it is present but tesseract is not, so we must NOT
            # hard-fail just because the binary could not be installed. Install
            # attempts are made best-effort and per-package-group so one missing
            # name (e.g. tesseract-ocr-eng on Alpine) does not abort the whole
            # install the way a single combined "apt/apk add a b c" would.
            probe = await backend.run(
                "if command -v tesseract >/dev/null 2>&1; then printf READY; else printf MISSING; fi",
                timeout=30,
            )
            if "READY" not in probe:
                await _install_tesseract_resilient(backend)
                probe = await backend.run(
                    "if command -v tesseract >/dev/null 2>&1; then printf READY; else printf MISSING; fi",
                    timeout=30,
                )
                if "READY" not in probe:
                    logger.warning(
                        "Runloop Devbox: tesseract unavailable after install attempts; "
                        "falling back to Pillow-only image analysis"
                    )
            await backend.write(script_path, _TELEGRAM_IMAGE_SCRIPT)
            for path, raw in image_paths:
                suffix = path.suffix.lower() if path.suffix else ".img"
                remote_path = f"{root}/telegram-images/{uuid4().hex}{suffix}"
                remote_paths.append(remote_path)
                await backend.write_bytes(remote_path, raw)
            await backend.write(manifest_path, json.dumps(remote_paths))
            output = await backend.run(
                "env NANOBOT_OCR_ALLOW_INSTALL=1 NANOBOT_OCR_ALLOW_PILLOW_INSTALL=1 "
                "NANOBOT_OCR_TIMEOUT_SECONDS=90 "
                f"python3 {shlex.quote(script_path)} {shlex.quote(manifest_path)}",
                timeout=180,
            )
            stdout = output.split("\n[stderr]", 1)[0].strip()
            parsed: Any | None = None
            try:
                parsed = json.loads(stdout)
            except (TypeError, ValueError):
                for line in reversed(stdout.splitlines()):
                    candidate = line.strip()
                    if not candidate.startswith("{"):
                        continue
                    try:
                        parsed = json.loads(candidate)
                        break
                    except ValueError:
                        continue
            if not isinstance(parsed, dict) or not str(parsed.get("content") or "").strip():
                logger.warning("Runloop Devbox returned no usable Tesseract OCR result")
                return "[Runloop Devbox Tesseract OCR returned no readable result.]"
            return str(parsed["content"]).strip()[:_MAX_IMAGE_ANALYSIS_RESULT_CHARS]
        except Exception as exc:
            logger.warning("Runloop Devbox Tesseract OCR failed: {}", type(exc).__name__)
            if _retry_on_failure:
                devbox_reset = True
                devbox_id = _RUNLOOP_STORE.sandbox_id(session_key or "telegram")
                with suppress(Exception):
                    await backend.reset(devbox_id)
                _RUNLOOP_STORE.remove(session_key or "telegram")
                return await self._analyze_telegram_images_runloop(
                    image_paths,
                    config=config,
                    session_key=session_key,
                    _retry_on_failure=False,
                )
            return "[Runloop Devbox Tesseract OCR failed.]"
        finally:
            if remote_paths and not devbox_reset:
                with suppress(Exception):
                    await backend.run(
                        "rm -f " + " ".join(shlex.quote(path) for path in remote_paths)
                        + f" {shlex.quote(manifest_path)} {shlex.quote(script_path)}",
                        timeout=30,
                    )

    async def _analyze_telegram_images_tenki(
        self,
        image_paths: list[tuple[Path, bytes]],
        *,
        config: Any,
        session_key: str,
        _retry_on_failure: bool = True,
    ) -> str:
        """Tesseract OCR for Telegram images inside a Tenki Sandbox.

        Mirrors the Daytona/Runloop path: installs (tesseract + Pillow) are allowed
        inside the VM, Tesseract gets the same generous 90s timeout, and a failure
        retries once against a fresh session before degrading gracefully.
        """
        backend = TenkiExecutionBackend(
            config, sandbox_name=tenki_sandbox_name(session_key or "telegram")
        )
        # Reuse the persisted session id (if any) so the backend reattaches to
        # that exact VM instead of re-resolving it by name on each OCR run.
        stored_id = _TENKI_STORE.sandbox_id(session_key or "telegram")
        if stored_id:
            backend.last_session_id = stored_id
        root = backend.workspace
        ocr_dir = f"{root}/.nanobot"
        remote_paths: list[str] = []
        manifest_path = f"{ocr_dir}/telegram_image_manifest.json"
        script_path = f"{ocr_dir}/telegram_image_ocr.py"
        session_reset = False
        try:
            await backend.run(
                f"mkdir -p {shlex.quote(ocr_dir)} {shlex.quote(f'{root}/telegram-images')}",
                timeout=60,
            )
            # Tesseract is optional: the OCR script degrades to Pillow-based
            # extraction when it is present but tesseract is not, so we must NOT
            # hard-fail just because the binary could not be installed. Install
            # attempts are made best-effort and per-package-group so one missing
            # name (e.g. tesseract-ocr-eng on Alpine) does not abort the whole
            # install the way a single combined "apt/apk add a b c" would.
            probe = await backend.run(
                "if command -v tesseract >/dev/null 2>&1; then printf READY; else printf MISSING; fi",
                timeout=30,
            )
            if "READY" not in probe:
                await _install_tesseract_resilient(backend)
                probe = await backend.run(
                    "if command -v tesseract >/dev/null 2>&1; then printf READY; else printf MISSING; fi",
                    timeout=30,
                )
                if "READY" not in probe:
                    logger.warning(
                        "Tenki Sandbox: tesseract unavailable after install attempts; "
                        "falling back to Pillow-only image analysis"
                    )
            await backend.write(script_path, _TELEGRAM_IMAGE_SCRIPT)
            for path, raw in image_paths:
                suffix = path.suffix.lower() if path.suffix else ".img"
                remote_path = f"{root}/telegram-images/{uuid4().hex}{suffix}"
                remote_paths.append(remote_path)
                await backend.write_bytes(remote_path, raw)
            await backend.write(manifest_path, json.dumps(remote_paths))
            output = await backend.run(
                "env NANOBOT_OCR_ALLOW_INSTALL=1 NANOBOT_OCR_ALLOW_PILLOW_INSTALL=1 "
                "NANOBOT_OCR_TIMEOUT_SECONDS=90 "
                f"python3 {shlex.quote(script_path)} {shlex.quote(manifest_path)}",
                timeout=180,
            )
            stdout = output.split("\n[stderr]", 1)[0].strip()
            parsed: Any | None = None
            try:
                parsed = json.loads(stdout)
            except (TypeError, ValueError):
                for line in reversed(stdout.splitlines()):
                    candidate = line.strip()
                    if not candidate.startswith("{"):
                        continue
                    try:
                        parsed = json.loads(candidate)
                        break
                    except ValueError:
                        continue
            if not isinstance(parsed, dict) or not str(parsed.get("content") or "").strip():
                logger.warning("Tenki Sandbox returned no usable Tesseract OCR result")
                return "[Tenki Sandbox Tesseract OCR returned no readable result.]"
            return str(parsed["content"]).strip()[:_MAX_IMAGE_ANALYSIS_RESULT_CHARS]
        except Exception as exc:
            logger.warning("Tenki Sandbox Tesseract OCR failed: {}", type(exc).__name__)
            if _retry_on_failure:
                session_reset = True
                session_id = _TENKI_STORE.sandbox_id(session_key or "telegram")
                with suppress(Exception):
                    await backend.reset(session_id)
                _TENKI_STORE.remove(session_key or "telegram")
                return await self._analyze_telegram_images_tenki(
                    image_paths,
                    config=config,
                    session_key=session_key,
                    _retry_on_failure=False,
                )
            return "[Tenki Sandbox Tesseract OCR failed.]"
        finally:
            if remote_paths and not session_reset:
                with suppress(Exception):
                    await backend.run(
                        "rm -f " + " ".join(shlex.quote(path) for path in remote_paths)
                        + f" {shlex.quote(manifest_path)} {shlex.quote(script_path)}",
                        timeout=30,
                    )

    async def _analyze_telegram_images_freestyle(
        self,
        image_paths: list[tuple[Path, bytes]],
        *,
        config: Any,
        session_key: str,
        _retry_on_failure: bool = True,
    ) -> str:
        """Tesseract OCR for Telegram images inside a Freestyle VM.

        Mirrors the Daytona/Runloop path: installs (tesseract + Pillow) are allowed
        inside the VM, Tesseract gets the same generous 90s timeout, and a failure
        retries once against a fresh session before degrading gracefully.
        """
        backend = FreestyleExecutionBackend(
            config, sandbox_name=freestyle_sandbox_name(session_key or "telegram")
        )
        # Reuse the persisted session id (if any) so the backend reattaches to
        # that exact VM instead of re-resolving it by name on each OCR run.
        stored_id = _FREESTYLE_STORE.sandbox_id(session_key or "telegram")
        if stored_id:
            backend.last_session_id = stored_id
        root = backend.workspace
        ocr_dir = f"{root}/.nanobot"
        remote_paths: list[str] = []
        manifest_path = f"{ocr_dir}/telegram_image_manifest.json"
        script_path = f"{ocr_dir}/telegram_image_ocr.py"
        session_reset = False
        try:
            await backend.run(
                f"mkdir -p {shlex.quote(ocr_dir)} {shlex.quote(f'{root}/telegram-images')}",
                timeout=60,
            )
            # Tesseract is optional: the OCR script degrades to Pillow-based
            # extraction when it is present but tesseract is not, so we must NOT
            # hard-fail just because the binary could not be installed. Install
            # attempts are made best-effort and per-package-group so one missing
            # name (e.g. tesseract-ocr-eng on Alpine) does not abort the whole
            # install the way a single combined "apt/apk add a b c" would.
            probe = await backend.run(
                "if command -v tesseract >/dev/null 2>&1; then printf READY; else printf MISSING; fi",
                timeout=30,
            )
            if "READY" not in probe:
                await _install_tesseract_resilient(backend)
                probe = await backend.run(
                    "if command -v tesseract >/dev/null 2>&1; then printf READY; else printf MISSING; fi",
                    timeout=30,
                )
                if "READY" not in probe:
                    logger.warning(
                        "Freestyle VM: tesseract unavailable after install attempts; "
                        "falling back to Pillow-only image analysis"
                    )
            await backend.write(script_path, _TELEGRAM_IMAGE_SCRIPT)
            for path, raw in image_paths:
                suffix = path.suffix.lower() if path.suffix else ".img"
                remote_path = f"{root}/telegram-images/{uuid4().hex}{suffix}"
                remote_paths.append(remote_path)
                await backend.write_bytes(remote_path, raw)
            await backend.write(manifest_path, json.dumps(remote_paths))
            output = await backend.run(
                "env NANOBOT_OCR_ALLOW_INSTALL=1 NANOBOT_OCR_ALLOW_PILLOW_INSTALL=1 "
                "NANOBOT_OCR_TIMEOUT_SECONDS=90 "
                f"python3 {shlex.quote(script_path)} {shlex.quote(manifest_path)}",
                timeout=180,
            )
            stdout = output.split("\n[stderr]", 1)[0].strip()
            parsed: Any | None = None
            try:
                parsed = json.loads(stdout)
            except (TypeError, ValueError):
                for line in reversed(stdout.splitlines()):
                    candidate = line.strip()
                    if not candidate.startswith("{"):
                        continue
                    try:
                        parsed = json.loads(candidate)
                        break
                    except ValueError:
                        continue
            if not isinstance(parsed, dict) or not str(parsed.get("content") or "").strip():
                logger.warning("Freestyle VM returned no usable Tesseract OCR result")
                return "[Freestyle VM Tesseract OCR returned no readable result.]"
            return str(parsed["content"]).strip()[:_MAX_IMAGE_ANALYSIS_RESULT_CHARS]
        except Exception as exc:
            logger.warning("Freestyle VM Tesseract OCR failed: {}", type(exc).__name__)
            if _retry_on_failure:
                session_reset = True
                session_id = _FREESTYLE_STORE.sandbox_id(session_key or "telegram")
                with suppress(Exception):
                    await backend.reset(session_id)
                _FREESTYLE_STORE.remove(session_key or "telegram")
                return await self._analyze_telegram_images_freestyle(
                    image_paths,
                    config=config,
                    session_key=session_key,
                    _retry_on_failure=False,
                )
            return "[Freestyle VM Tesseract OCR failed.]"
        finally:
            if remote_paths and not session_reset:
                with suppress(Exception):
                    await backend.run(
                        "rm -f " + " ".join(shlex.quote(path) for path in remote_paths)
                        + f" {shlex.quote(manifest_path)} {shlex.quote(script_path)}",
                        timeout=30,
                    )

    async def _analyze_telegram_images_upstash(
        self,
        image_paths: list[tuple[Path, bytes]],
        *,
        config: Any,
        session_key: str,
        _retry_on_failure: bool = True,
    ) -> str:
        """Tesseract OCR for Telegram images inside an Upstash Box.

        Mirrors the Novita path: installs (tesseract + Pillow) are allowed inside
        the box, Tesseract gets the same generous 90s timeout, and a failure
        retries once against a fresh box before degrading gracefully.
        """
        from nanobot.agent.tools.upstash_backend import upstash_box_name

        backend = UpstashExecutionBackend(config, box_name=upstash_box_name(session_key or "telegram"))
        # Reuse the persisted box id (if any) so ensure_box verifies that exact
        # box instead of listing every box in the account on each OCR run.
        stored_id = _UPSTASH_STORE.sandbox_id(session_key or "telegram")
        if stored_id:
            backend.last_box_id = stored_id
        root = backend.workspace
        ocr_dir = f"{root}/.nanobot"
        remote_paths: list[str] = []
        manifest_path = f"{ocr_dir}/telegram_image_manifest.json"
        script_path = f"{ocr_dir}/telegram_image_ocr.py"
        box_reset = False
        try:
            await backend.run(f"mkdir -p {shlex.quote(ocr_dir)} {shlex.quote(f'{root}/telegram-images')}", timeout=60)
            # Tesseract is optional: the OCR script degrades to Pillow-based
            # extraction when it is present but tesseract is not, so we must NOT
            # hard-fail just because the binary could not be installed. Install
            # attempts are made best-effort and per-package-group so one missing
            # name (e.g. tesseract-ocr-eng on Alpine, where English data ships in
            # the base package) does not abort the whole install the way a single
            # combined "apt/apk add a b c" would.
            probe = await backend.run(
                "if command -v tesseract >/dev/null 2>&1; then printf READY; else printf MISSING; fi",
                timeout=30,
            )
            if "READY" not in probe:
                await _install_tesseract_resilient(backend)
                probe = await backend.run(
                    "if command -v tesseract >/dev/null 2>&1; then printf READY; else printf MISSING; fi",
                    timeout=30,
                )
                if "READY" not in probe:
                    logger.warning(
                        "Upstash Box: tesseract unavailable after install attempts; "
                        "falling back to Pillow-only image analysis"
                    )
            await backend.write(script_path, _TELEGRAM_IMAGE_SCRIPT)
            for path, raw in image_paths:
                suffix = path.suffix.lower() if path.suffix else ".img"
                remote_path = f"{root}/telegram-images/{uuid4().hex}{suffix}"
                remote_paths.append(remote_path)
                await backend.write_bytes(remote_path, raw)
            await backend.write(manifest_path, json.dumps(remote_paths))
            output = await backend.run(
                "env NANOBOT_OCR_ALLOW_INSTALL=1 NANOBOT_OCR_ALLOW_PILLOW_INSTALL=1 "
                "NANOBOT_OCR_TIMEOUT_SECONDS=90 "
                f"python3 {shlex.quote(script_path)} {shlex.quote(manifest_path)}",
                timeout=180,
            )
            stdout = output.split("\n[stderr]", 1)[0].strip()
            parsed: Any | None = None
            try:
                parsed = json.loads(stdout)
            except (TypeError, ValueError):
                for line in reversed(stdout.splitlines()):
                    candidate = line.strip()
                    if not candidate.startswith("{"):
                        continue
                    try:
                        parsed = json.loads(candidate)
                        break
                    except ValueError:
                        continue
            if not isinstance(parsed, dict) or not str(parsed.get("content") or "").strip():
                logger.warning("Upstash Box returned no usable Tesseract OCR result")
                return "[Upstash Box Tesseract OCR returned no readable result.]"
            return str(parsed["content"]).strip()[:_MAX_IMAGE_ANALYSIS_RESULT_CHARS]
        except Exception as exc:
            logger.warning("Upstash Box Tesseract OCR failed: {}", type(exc).__name__)
            if _retry_on_failure:
                box_reset = True
                box_id = _UPSTASH_STORE.sandbox_id(session_key or "telegram")
                with suppress(Exception):
                    await backend.reset(box_id)
                _UPSTASH_STORE.remove(session_key or "telegram")
                return await self._analyze_telegram_images_upstash(
                    image_paths,
                    config=config,
                    session_key=session_key,
                    _retry_on_failure=False,
                )
            return "[Upstash Box Tesseract OCR failed.]"
        finally:
            if remote_paths and not box_reset:
                with suppress(Exception):
                    await backend.run(
                        "rm -f " + " ".join(shlex.quote(path) for path in remote_paths)
                        + f" {shlex.quote(manifest_path)} {shlex.quote(script_path)}",
                        timeout=30,
                    )

    async def _analyze_telegram_images_vercel(
        self,
        image_paths: list[tuple[Path, bytes]],
        *,
        config: Any,
        session_key: str,
        _retry_on_failure: bool = True,
    ) -> str:
        """Tesseract OCR for Telegram images inside a Vercel Sandbox.

        Mirrors the Upstash path, with one difference worth stating plainly: a
        Vercel Sandbox runs as the unprivileged ``vercel`` user in a stock
        runtime, so the package install below is best-effort and normally cannot
        succeed — there is no sudo to drive and no package manager the sandbox
        user may use. That is not a failure of this path: the OCR script degrades
        to a Pillow-only reading and says so, which is a useful answer.

        What was *not* acceptable was the previous behaviour. A Vercel deployment
        had no branch here at all, so every image fell through to the Novita path
        — running OCR in the wrong sandbox when a Novita key happened to be
        configured, and refusing outright when it was not. That is the reported
        "OCR doesn't work on Vercel".
        """
        backend = self._vercel_backend(config, session_key or "telegram")
        root = backend.workspace
        ocr_dir = f"{root}/.nanobot"
        remote_paths: list[str] = []
        manifest_path = f"{ocr_dir}/telegram_image_manifest.json"
        script_path = f"{ocr_dir}/telegram_image_ocr.py"
        sandbox_reset = False
        try:
            await backend.run(
                f"mkdir -p {shlex.quote(ocr_dir)} {shlex.quote(f'{root}/telegram-images')}",
                timeout=60,
            )
            probe = await backend.run(
                "if command -v tesseract >/dev/null 2>&1; then printf READY; else printf MISSING; fi",
                timeout=30,
            )
            if "READY" not in probe:
                # Reuse the shared resilient installer: it tries each candidate
                # package group independently and never raises, which matters
                # here because a Vercel Sandbox is an unprivileged stock
                # runtime — `apt-get` usually cannot run at all, and a single
                # combined install would hard-fail the whole OCR pass. The
                # script then degrades to a Pillow-only reading and reports
                # that honestly instead of claiming OCR ran.
                installed = await _install_tesseract_resilient(backend)
                if not installed:
                    logger.info(
                        "Vercel Sandbox: tesseract could not be installed; the OCR script "
                        "will report the Pillow-only reading instead"
                    )
            await backend.write(script_path, _TELEGRAM_IMAGE_SCRIPT)
            for path, raw in image_paths:
                suffix = path.suffix.lower() if path.suffix else ".img"
                remote_path = f"{root}/telegram-images/{uuid4().hex}{suffix}"
                remote_paths.append(remote_path)
                await backend.write_bytes(remote_path, raw)
            await backend.write(manifest_path, json.dumps(remote_paths))
            output = await backend.run(
                "env NANOBOT_OCR_ALLOW_INSTALL=1 NANOBOT_OCR_ALLOW_PILLOW_INSTALL=1 "
                "NANOBOT_OCR_TIMEOUT_SECONDS=90 "
                f"python3 {shlex.quote(script_path)} {shlex.quote(manifest_path)}",
                timeout=180,
            )
            stdout = output.split("\n[stderr]", 1)[0].strip()
            parsed: Any | None = None
            try:
                parsed = json.loads(stdout)
            except (TypeError, ValueError):
                for line in reversed(stdout.splitlines()):
                    candidate = line.strip()
                    if not candidate.startswith("{"):
                        continue
                    try:
                        parsed = json.loads(candidate)
                        break
                    except ValueError:
                        continue
            if not isinstance(parsed, dict) or not str(parsed.get("content") or "").strip():
                logger.warning("Vercel Sandbox returned no usable Tesseract OCR result")
                return "[Vercel Sandbox OCR returned no readable result.]"
            return str(parsed["content"]).strip()[:_MAX_IMAGE_ANALYSIS_RESULT_CHARS]
        except Exception as exc:
            logger.warning("Vercel Sandbox OCR failed: {}", type(exc).__name__)
            if _retry_on_failure:
                sandbox_reset = True
                with suppress(Exception):
                    await backend.reset(_VERCEL_STORE.sandbox_id(session_key or "telegram"))
                _VERCEL_STORE.remove(session_key or "telegram")
                return await self._analyze_telegram_images_vercel(
                    image_paths,
                    config=config,
                    session_key=session_key,
                    _retry_on_failure=False,
                )
            return "[Vercel Sandbox Tesseract OCR failed.]"
        finally:
            if remote_paths and not sandbox_reset:
                with suppress(Exception):
                    await backend.run(
                        "rm -f " + " ".join(shlex.quote(path) for path in remote_paths)
                        + f" {shlex.quote(manifest_path)} {shlex.quote(script_path)}",
                        timeout=30,
                    )

    async def analyze_telegram_images(
        self,
        image_paths: list[str],
        user_prompt: str,
        *,
        session_key: str,
        _retry_on_failure: bool = True,
    ) -> str:
        """Read Telegram images with Tesseract OCR inside the selected backend.

        Pillow is used only to verify, orient, upscale, and normalize each image.
        Tesseract performs the text recognition remotely; no image-generation
        endpoint or configured chat-model vision request is used.
        """
        if not image_paths:
            return ""
        selected_backend, backend_config = self._selected_backend()
        if selected_backend == "runloop":
            if backend_config is None or not str(backend_config.api_key or "").strip():
                return "[Runloop execution is selected but no API key is configured.]"
            runloop_images: list[tuple[Path, bytes]] = []
            for raw_path in image_paths[:_MAX_TELEGRAM_IMAGE_COUNT]:
                path = Path(raw_path).expanduser().resolve()
                try:
                    raw = path.read_bytes()
                except OSError:
                    continue
                if not raw or len(raw) > _MAX_TELEGRAM_IMAGE_BYTES:
                    continue
                mime = detect_image_mime(raw) or mimetypes.guess_type(str(path))[0]
                if mime and mime.startswith("image/"):
                    runloop_images.append((path, raw))
            if not runloop_images:
                return "[No readable Telegram images were available to the Runloop Devbox.]"
            return await self._analyze_telegram_images_runloop(
                runloop_images, config=backend_config, session_key=session_key
            )
        if selected_backend == "freestyle":
            if not _freestyle_key_configured(backend_config):
                return "[Freestyle execution is selected but no API key is configured.]"
            freestyle_images: list[tuple[Path, bytes]] = []
            for raw_path in image_paths[:_MAX_TELEGRAM_IMAGE_COUNT]:
                path = Path(raw_path).expanduser().resolve()
                try:
                    raw = path.read_bytes()
                except OSError:
                    continue
                if not raw or len(raw) > _MAX_TELEGRAM_IMAGE_BYTES:
                    continue
                mime = detect_image_mime(raw) or mimetypes.guess_type(str(path))[0]
                if mime and mime.startswith("image/"):
                    freestyle_images.append((path, raw))
            if not freestyle_images:
                return "[No readable Telegram images were available to the Freestyle VM.]"
            return await self._analyze_telegram_images_freestyle(
                freestyle_images, config=backend_config, session_key=session_key
            )
        if selected_backend == "tenki":
            if not _tenki_key_configured(backend_config):
                return "[Tenki execution is selected but no API key is configured.]"
            tenki_images: list[tuple[Path, bytes]] = []
            for raw_path in image_paths[:_MAX_TELEGRAM_IMAGE_COUNT]:
                path = Path(raw_path).expanduser().resolve()
                try:
                    raw = path.read_bytes()
                except OSError:
                    continue
                if not raw or len(raw) > _MAX_TELEGRAM_IMAGE_BYTES:
                    continue
                mime = detect_image_mime(raw) or mimetypes.guess_type(str(path))[0]
                if mime and mime.startswith("image/"):
                    tenki_images.append((path, raw))
            if not tenki_images:
                return "[No readable Telegram images were available to the Tenki Sandbox.]"
            return await self._analyze_telegram_images_tenki(
                tenki_images, config=backend_config, session_key=session_key
            )
        if selected_backend == "daytona":
            if backend_config is None or not str(backend_config.api_key or "").strip():
                return "[Daytona execution is selected but no API key is configured.]"
            daytona_images: list[tuple[Path, bytes]] = []
            for raw_path in image_paths[:_MAX_TELEGRAM_IMAGE_COUNT]:
                path = Path(raw_path).expanduser().resolve()
                try:
                    raw = path.read_bytes()
                except OSError:
                    continue
                if not raw or len(raw) > _MAX_TELEGRAM_IMAGE_BYTES:
                    continue
                mime = detect_image_mime(raw) or mimetypes.guess_type(str(path))[0]
                if mime and mime.startswith("image/"):
                    daytona_images.append((path, raw))
            if not daytona_images:
                return "[No readable Telegram images were available to Daytona.]"
            return await self._analyze_telegram_images_daytona(
                daytona_images, config=backend_config, session_key=session_key
            )
        if selected_backend == "upstash":
            if backend_config is None or not str(backend_config.api_key or "").strip():
                return "[Upstash Box execution is selected but no API key is configured.]"
            upstash_images: list[tuple[Path, bytes]] = []
            for raw_path in image_paths[:_MAX_TELEGRAM_IMAGE_COUNT]:
                path = Path(raw_path).expanduser().resolve()
                try:
                    raw = path.read_bytes()
                except OSError:
                    continue
                if not raw or len(raw) > _MAX_TELEGRAM_IMAGE_BYTES:
                    continue
                mime = detect_image_mime(raw) or mimetypes.guess_type(str(path))[0]
                if mime and mime.startswith("image/"):
                    upstash_images.append((path, raw))
            if not upstash_images:
                return "[No readable Telegram images were available to Upstash Box.]"
            return await self._analyze_telegram_images_upstash(
                upstash_images, config=backend_config, session_key=session_key
            )
        if selected_backend == "vercel":
            if backend_config is None or not str(backend_config.token or "").strip():
                return "[Vercel execution is selected but no token is configured.]"
            vercel_images: list[tuple[Path, bytes]] = []
            for raw_path in image_paths[:_MAX_TELEGRAM_IMAGE_COUNT]:
                path = Path(raw_path).expanduser().resolve()
                try:
                    raw = path.read_bytes()
                except OSError:
                    continue
                if not raw or len(raw) > _MAX_TELEGRAM_IMAGE_BYTES:
                    continue
                mime = detect_image_mime(raw) or mimetypes.guess_type(str(path))[0]
                if mime and mime.startswith("image/"):
                    vercel_images.append((path, raw))
            if not vercel_images:
                return "[No readable Telegram images were available to the Vercel Sandbox.]"
            return await self._analyze_telegram_images_vercel(
                vercel_images, config=backend_config, session_key=session_key
            )
        if selected_backend == "vps":
            if backend_config is None or not str(backend_config.host or "").strip():
                return "[VPS execution is selected but SSH details are not configured.]"
            local_images: list[tuple[Path, bytes]] = []
            for raw_path in image_paths[:_MAX_TELEGRAM_IMAGE_COUNT]:
                path = Path(raw_path).expanduser().resolve()
                try:
                    raw = path.read_bytes()
                except OSError:
                    continue
                if not raw or len(raw) > _MAX_TELEGRAM_IMAGE_BYTES:
                    continue
                mime = detect_image_mime(raw) or mimetypes.guess_type(str(path))[0]
                if mime and mime.startswith("image/"):
                    local_images.append((path, raw))
            if not local_images:
                return "[No readable Telegram images were available to the VPS.]"
            return await self._analyze_telegram_images_vps(local_images, config=backend_config)
        if Novita is None:
            return "[Novita Sandbox Tesseract OCR is unavailable in this deployment; the sandbox will install it on first use.]"
        api_key = os.getenv("NOVITA_API_KEY", "").strip()
        if not api_key:
            return "[Novita Sandbox OCR is not configured in this deployment.]"
        if len(image_paths) > _MAX_TELEGRAM_IMAGE_COUNT:
            image_paths = image_paths[:_MAX_TELEGRAM_IMAGE_COUNT]

        local_images: list[tuple[Path, bytes]] = []
        for raw_path in image_paths:
            path = Path(raw_path).expanduser().resolve()
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if not raw or len(raw) > _MAX_TELEGRAM_IMAGE_BYTES:
                continue
            mime = detect_image_mime(raw) or mimetypes.guess_type(str(path))[0]
            if not mime or not mime.startswith("image/"):
                continue
            local_images.append((path, raw))
        if not local_images:
            return "[No readable Telegram images were available to Novita Sandbox.]"

        key = session_key or "telegram:unknown"
        remote_paths: list[str] = []
        manifest_path = f"{_OCR_DIR}/telegram_image_manifest.json"
        script_path = f"{_OCR_DIR}/telegram_image_ocr.py"
        sandbox: Any | None = None
        try:
            async with _STORE.lock_for(key):
                sandbox = await asyncio.to_thread(self._get_or_create, key)
                await asyncio.to_thread(
                    sandbox.commands.run,
                    f"mkdir -p {shlex.quote(_OCR_DIR)} {shlex.quote(f'{_WORKSPACE}/telegram-images')}",
                    cwd="/",
                    timeout=30,
                    request_timeout=60,
                )
                await asyncio.to_thread(sandbox.files.write, script_path, _TELEGRAM_IMAGE_SCRIPT)
                for path, raw in local_images:
                    suffix = path.suffix.lower() if path.suffix else ".img"
                    remote_path = f"{_WORKSPACE}/telegram-images/{uuid4().hex}{suffix}"
                    remote_paths.append(remote_path)
                    await asyncio.to_thread(sandbox.files.write, remote_path, raw)
                await asyncio.to_thread(
                    sandbox.files.write,
                    manifest_path,
                    json.dumps(remote_paths),
                )
                command = (
                    "env NANOBOT_OCR_ALLOW_INSTALL=1 NANOBOT_OCR_ALLOW_PILLOW_INSTALL=1 "
                    "NANOBOT_OCR_TIMEOUT_SECONDS=90 "
                    f"python3 {shlex.quote(script_path)} {shlex.quote(manifest_path)}"
                )
                result = await asyncio.to_thread(
                    sandbox.commands.run,
                    command,
                    cwd=_WORKSPACE,
                    timeout=180,
                    request_timeout=210,
                )
                exit_code = getattr(result, "exit_code", getattr(result, "exitCode", 1))
                stdout = str(getattr(result, "stdout", "") or "").strip()
                if exit_code not in (None, 0):
                    logger.warning("Novita Sandbox Tesseract OCR exited with code {}", exit_code)
                    return "[Novita Sandbox Tesseract OCR failed.]"
                try:
                    parsed = json.loads(stdout)
                except (TypeError, ValueError):
                    parsed = None
                    for line in reversed(stdout.splitlines()):
                        candidate = line.strip()
                        if not candidate.startswith("{"):
                            continue
                        try:
                            parsed = json.loads(candidate)
                            break
                        except ValueError:
                            continue
                    if parsed is None:
                        logger.warning("Novita Sandbox returned a non-JSON OCR response")
                        return "[Novita Sandbox returned an unusable Tesseract OCR result.]"
                if not isinstance(parsed, dict) or not str(parsed.get("content") or "").strip():
                    logger.warning("Novita Sandbox returned no OCR content")
                    return "[Novita Sandbox returned no Tesseract OCR result.]"
                return str(parsed["content"]).strip()[:_MAX_IMAGE_ANALYSIS_RESULT_CHARS]
        except Exception as exc:
            logger.warning("Novita Sandbox Tesseract OCR failed: {}", type(exc).__name__)
            if _retry_on_failure:
                _STORE.remove(key)
                if sandbox is not None:
                    with suppress(Exception):
                        await asyncio.to_thread(sandbox.kill)
                return await self.analyze_telegram_images(
                    image_paths,
                    user_prompt,
                    session_key=key,
                    _retry_on_failure=False,
                )
            return "[Novita Sandbox Tesseract OCR failed.]"
        finally:
            if remote_paths and sandbox is not None:
                try:
                    cleanup = "rm -f " + " ".join(shlex.quote(path) for path in remote_paths)
                    cleanup += f" {shlex.quote(manifest_path)} {shlex.quote(script_path)}"
                    await asyncio.to_thread(
                        sandbox.commands.run,
                        cleanup,
                        cwd=_WORKSPACE,
                        timeout=30,
                        request_timeout=60,
                    )
                except Exception:
                    logger.debug("Novita Sandbox Tesseract OCR cleanup failed")

    @staticmethod
    def _template_sizing() -> tuple[int, int] | None:
        """Desired (cpu_count, memory_mb) for Novita sandboxes.

        Priority: NOVITA_SANDBOX_CPU_COUNT + NOVITA_SANDBOX_MEMORY_MB env vars
        (deployment-level), then the admin-saved execution.novita_template
        config. When NEITHER is set we now default to a sane sized box
        (DEFAULT_TEMPLATE_CPU / DEFAULT_TEMPLATE_MEMORY_MB = 2 vCPU / 4 GB)
        instead of returning None. Returning None previously let sandboxes fall
        back to the stock "base" image (~486 MB), which OOM-kills any nontrivial
        build/OCR; 2 GB then OOM-killed the Wine + MetaTrader 5 stack. So an
        admin who loads nothing still gets 4 GB per sandbox; explicit config
        always overrides this default.
        """
        cpu = memory = None
        try:
            raw_cpu = os.getenv("NOVITA_SANDBOX_CPU_COUNT", "").strip()
            if raw_cpu:
                cpu = int(raw_cpu)
        except ValueError:
            cpu = None
        try:
            raw_memory = os.getenv("NOVITA_SANDBOX_MEMORY_MB", "").strip()
            if raw_memory:
                memory = int(raw_memory)
        except ValueError:
            memory = None
        if cpu is None or memory is None:
            execution = NovitaSandboxTool._execution_config()
            template = getattr(execution, "novita_template", None) if execution is not None else None
            if template is not None:
                if cpu is None:
                    cpu = int(getattr(template, "cpu_count", 2) or 2)
                if memory is None:
                    memory = int(getattr(template, "memory_mb", 0) or 0)
        # Nothing configured anywhere: apply the automatic 4 GB default rather
        # than dropping to the tiny stock base image (or the 2 GB that killed
        # the Wine + MT5 install).
        if not memory:
            memory = DEFAULT_TEMPLATE_MEMORY_MB
        if not cpu:
            cpu = DEFAULT_TEMPLATE_CPU
        cpu = max(1, min(int(cpu), 8))
        memory = max(512, min(int(memory), 65_536))
        return cpu, memory

    @staticmethod
    def _desired_alias(sizing: tuple[int, int]) -> str:
        cpu, memory = sizing
        prefix = "powerx-base"
        try:
            execution = NovitaSandboxTool._execution_config()
            template = getattr(execution, "novita_template", None) if execution is not None else None
            configured = str(getattr(template, "alias_prefix", "") or "").strip()
            if configured:
                prefix = re.sub(r"[^A-Za-z0-9_-]", "-", configured)[:24] or "powerx-base"
        except Exception:
            pass
        return f"{prefix}-{max(1, memory // 1024)}g-c{cpu}"

    def _resolve_template(self, client: Any) -> str:
        """Pick the Novita template alias honouring the configured RAM/CPU.

        Explicit NOVITA_SANDBOX_TEMPLATE always wins (back-compat). Otherwise,
        when sizing is configured, reuse an existing template alias, build a
        custom one once (cached by Upstash-style name), and fall back to the
        legacy default whenever anything goes wrong so task flow never breaks.
        """
        explicit = os.getenv("NOVITA_SANDBOX_TEMPLATE", "").strip()
        if explicit:
            return explicit

        def _template_exists(template_alias: str) -> bool:
            """True when the alias exists on this Novita account (never raises)."""
            try:
                return bool(client.template.alias_exists(template_alias))
            except Exception:
                return False

        # Sizing is now ALWAYS resolved (defaults to 2 GB), so the normal path
        # builds/uses a sized template. We still keep a graceful fallback chain
        # for accounts where building a custom template is impossible: prefer any
        # existing powerx sized alias, then the stock "base" image, so task flow
        # never breaks — but we log loudly because "base" is ~486 MB.
        def _first_existing(candidates: list[str]) -> str | None:
            for candidate in candidates:
                if _template_exists(candidate):
                    return candidate
            return None

        sizing = self._template_sizing()
        if sizing is None:  # defensive: _template_sizing no longer returns None
            return (
                _first_existing(
                    ["powerx-base-4g-c2", "powerx-base-2g-c2", "powerx-base-4g", "base"]
                )
                or "base"
            )

        alias = self._desired_alias(sizing)
        cached = _TEMPLATE_CACHE.get(alias)
        if cached:
            return cached
        with _TEMPLATE_BUILD_LOCK:
            cached = _TEMPLATE_CACHE.get(alias)
            if cached:
                return cached
            if _template_exists(alias):
                _TEMPLATE_CACHE[alias] = alias
                return alias
            cpu, memory = sizing
            try:
                # Clone the built-in "base" image with the requested CPU/RAM so
                # every sandbox spawned from this template gets exactly that RAM
                # (same approach as scripts/build_novita_template.py).
                build_info = client.template.build(
                    client.template.from_template("base"),
                    alias=alias,
                    cpu_count=cpu,
                    memory_mb=memory,
                )
                built_alias = str(getattr(build_info, "alias", "") or alias)
                logger.info(
                    "Built Novita sandbox template '{}' ({} vCPU / {} MB)", built_alias, cpu, memory
                )
                _TEMPLATE_CACHE[alias] = built_alias
                return built_alias
            except Exception as exc:
                # Building failed. Prefer an already-published sized template over
                # the tiny stock base image; only use "base" as a last resort.
                fallback = (
                    _first_existing(
                        [alias, "powerx-base-4g-c2", "powerx-base-2g-c2", "powerx-base-4g"]
                    )
                    or "base"
                )
                logger.warning(
                    "Could not build Novita template '{}' ({}); falling back to '{}'. "
                    "If this is 'base', sandboxes run at ~486 MB — publish a 2 GB "
                    "template or set NOVITA_SANDBOX_TEMPLATE.",
                    alias,
                    type(exc).__name__,
                    fallback,
                )
                _TEMPLATE_CACHE[alias] = fallback
                return fallback

    def _ensure_workspace(self, sandbox: Any) -> None:
        """Make sure ``_WORKSPACE`` exists before anything runs with it as cwd.

        The Novita "base" template ships no ``/workspace``: the root listing is
        ``/root``, ``/code``, ``/home`` and so on. Passing a non-existent
        directory as ``cwd`` is a hard error, not a fallback::

            InvalidArgumentException: cwd '/workspace' does not exist

        Command execution, file listing and downloads all pass
        ``cwd=_WORKSPACE``, so a sandbox without it fails every operation until
        something else happens to create the directory (the Telegram-OCR path
        does, as a side effect of ``mkdir -p /workspace/.nanobot`` — which is why
        this presented as an intermittent failure).

        [FIX 2026-09-17] Custom sized templates run as the unprivileged ``user``
        (uid 1000) rather than ``root``. ``/`` is owned by root with ``0755``,
        so a plain ``mkdir -p /workspace`` fails outright::

            mkdir: cannot create directory '/workspace': Permission denied

        The stock ``base`` image happens to run as root, which is why this only
        surfaced once the 2 GB/4 GB sized templates were built. Escalate through
        passwordless ``sudo -n`` (the user is in the sudo group) to create the
        directory AND hand it to the invoking user, otherwise later writes —
        uploads, downloads, OCR — still fail on a root-owned workspace.

        This is idempotent and must run on *every* path that hands a sandbox
        back, not just fresh creation: a box resumed from ``pause`` or
        reconnected by id can also be missing the directory.
        """
        quoted = shlex.quote(_WORKSPACE)
        # 1) plain mkdir covers root-style images; 2) sudo -n covers uid-1000
        # images; 3) chown flips a root-owned workspace to the invoking user so
        # subsequent unprivileged writes succeed. `||` keeps it a single
        # idempotent command, and `sudo -n` fails fast instead of hanging on a
        # password prompt when passwordless sudo is unavailable.
        prep_cmd = (
            f"mkdir -p {quoted} 2>/dev/null"
            f" || sudo -n mkdir -p {quoted}"
            f" || exit 1; "
            f"if [ ! -w {quoted} ]; then sudo -n chown -R \"$(id -u):$(id -g)\" {quoted}; fi; "
            f"chmod u+rwx {quoted} 2>/dev/null; "
            f"[ -d {quoted} ] && [ -w {quoted} ]"
        )
        last_error: Exception | None = None
        for attempt in range(6):
            try:
                sandbox.commands.run(
                    prep_cmd,
                    cwd="/",
                    timeout=30,
                    request_timeout=60,
                )
                return
            except Exception as exc:  # noqa: BLE001 - retried below, then re-raised
                last_error = exc
                if attempt < 5:
                    import time

                    time.sleep(3)
        # A sandbox we cannot prepare is unusable; surface the cause rather than
        # letting the caller fail later with a confusing cwd error.
        raise last_error if last_error is not None else RuntimeError("workspace prepare failed")

    def _prepare_sandbox(self, sandbox: Any) -> None:
        """Ensure the workspace exists AND GitHub credentials are materialised.

        Sandbox commands run in an isolated container that does NOT inherit the
        backend's environment, so ``git``/``gh``/``curl`` had no credentials even
        though GITHUB_BUILD_TOKEN is set on the host. That made every
        repo-create/clone/push attempt inside the sandbox fail with an
        authentication error while the host-side build_artifact tool worked fine.

        Must run on every path that hands a sandbox back (create AND reconnect),
        because a resumed box can have lost the workspace and the env file.
        """
        self._ensure_workspace(sandbox)
        self._write_sandbox_credentials(sandbox)

    def _write_sandbox_credentials(self, sandbox: Any) -> None:
        """Write GitHub credentials into the sandbox as a sourced env file.

        Deliberately NOT injected inline on the command line: commands are
        logged, so a literal ``GITHUB_TOKEN=ghp_...`` prefix would leak the
        token into tool-call logs and transcript history. A 0600 file sourced by
        each command keeps the secret out of logs while still making it
        available to git/gh/curl.
        """
        creds = _github_credentials()
        if not creds:
            # No token configured: leave the box unauthenticated (previous
            # behaviour) rather than writing an empty file that looks like
            # working auth.
            return
        owner, email = _git_identity()
        lines = ["# Managed by nanobot - do not edit.", "#!/bin/sh"]
        for name, value in creds.items():
            lines.append(f"export {name}={shlex.quote(value)}")
        # Neutralise any pre-existing credential helper (e.g. a stale gh login)
        # and instead store the token for github.com so git push/pull works
        # non-interactively. `|| true` keeps this a no-op when git is absent.
        lines.append(
            "git config --global credential.helper "
            "'!f() { echo username=x-access-token; echo password=$GITHUB_TOKEN; }; f' "
            "2>/dev/null || true"
        )
        lines.append(f"git config --global user.name {shlex.quote(owner)} 2>/dev/null || true")
        lines.append(f"git config --global user.email {shlex.quote(email)} 2>/dev/null || true")
        script = "\n".join(lines) + "\n"
        try:
            sandbox.files.write(_GIT_CREDS_PATH, script)
            # 0600: only the sandbox user may read the token.
            sandbox.commands.run(
                f"chmod 600 {shlex.quote(_GIT_CREDS_PATH)}",
                cwd="/",
                timeout=30,
                request_timeout=60,
            )
        except Exception as exc:  # noqa: BLE001 - credentials are best-effort
            logger.warning("could not seed GitHub credentials into sandbox: {}", exc)

    def _get_or_create(self, key: str) -> Any:
        client = self._client()
        # Resolve the target template up front so we can tell whether an existing
        # (in-memory or persisted) sandbox matches the CURRENT sizing. Before
        # this change a stale base-template box (~486 MB) was reused forever even
        # after the 2 GB default shipped, because reconnect never checked which
        # template it came from. A mismatch now forces a fresh create.
        sandbox_template = self._resolve_template(client)

        def _matches_sizing(stored_template: str | None) -> bool:
            # Unknown provenance (legacy index / pre-fix handle): assume stale so
            # it gets recreated once at the correct size, then tracked properly.
            return stored_template == sandbox_template

        def _try_resume(sandbox: Any) -> bool:
            # A timed-out box is PAUSED (lifecycle on_timeout=pause), not dead:
            # its filesystem is intact on Novita's side. Try to resume it and
            # wait for it to come back before ever falling through to create,
            # which would start from a fresh template and wipe the workspace.
            import time as _time

            try:
                if sandbox.is_running():
                    return True
            except Exception:
                return False
            resume = getattr(sandbox, "resume", None)
            if callable(resume):
                try:
                    resume()
                except Exception:
                    pass
            deadline = _time.time() + 90
            while _time.time() < deadline:
                try:
                    if sandbox.is_running():
                        return True
                except Exception:
                    return False
                _time.sleep(3)
            return False

        sandbox = _STORE.get(key)
        if sandbox is not None:
            try:
                if _matches_sizing(_STORE.template_for(key)) and _try_resume(sandbox):
                    self._prepare_sandbox(sandbox)
                    return sandbox
            except Exception:
                pass
            # Wrong-sized or dead handle: drop it (and its id) before recreating.
            _STORE.remove(key)
        sandbox_id = _STORE.sandbox_id(key)
        if sandbox_id:
            try:
                sandbox = client.sandbox.connect(sandbox_id)
                if _matches_sizing(_STORE.template_for(key)) and _try_resume(sandbox):
                    _STORE.set(key, sandbox, template=sandbox_template)
                    self._prepare_sandbox(sandbox)
                    return sandbox
                # Connected but undersized/unknown template: don't reuse it.
                _STORE.remove(key)
            except Exception:
                _STORE.remove(key)
        sandbox = client.sandbox.create(
            sandbox_template,
            timeout=min(int(os.getenv("NOVITA_SANDBOX_TIMEOUT", "3600")), 86_400),
            secure=True,
            allow_internet_access=True,
            lifecycle={"on_timeout": "pause", "auto_resume": True},
        )
        try:
            self._prepare_sandbox(sandbox)
        except Exception:
            # A freshly created box we cannot prepare is unusable; kill it so a
            # broken sandbox is not left running (and billing) on Novita.
            try:
                sandbox.kill()
            except Exception:
                pass
            raise
        _STORE.set(key, sandbox, template=sandbox_template)
        return sandbox

    async def _prepare_upload(
        self, kwargs: dict[str, Any], *, label: str
    ) -> tuple[Path, str] | ToolResult:
        """Validate an ``upload`` without writing it — used by the VPS branch."""

        async def _noop(_target: str, _data: bytes) -> None:
            return None

        return await self._stage_upload(kwargs, _noop, label=label)

    async def _stage_upload(
        self,
        kwargs: dict[str, Any],
        write: Callable[[str, bytes], Awaitable[Any]],
        *,
        label: str,
    ) -> tuple[Path, str] | ToolResult:
        """Validate a local ``source`` and write it into the active sandbox.

        Returns ``(source, destination)`` on success, or a ``ToolResult.error``
        carrying a usable message. It never raises: an uncaught exception here
        escapes the tool, leaves the file out of the sandbox, and is what the
        model then narrates as "the tools cannot reach the media directory".
        """
        raw = str(kwargs.get("source") or "").strip()
        if not raw:
            return ToolResult.error(
                "upload needs 'source': the local media path to send. "
                "Pass source=<path on this host>, and path=<destination inside "
                "the sandbox> only if you need somewhere other than "
                f"{_WORKSPACE}/{_UPLOAD_SUBDIR}/<filename>."
            )
        source = Path(raw).expanduser().resolve()
        if not self._local_attachment_allowed(source):
            return ToolResult.error(
                f"source must be inside the nanobot media/data directory (got {source}). "
                "An attachment the user sent is already there — use the path exactly as given."
            )
        if not source.is_file():
            return ToolResult.error(f"source file does not exist: {source}")
        if source.stat().st_size > _MAX_UPLOAD_BYTES:
            return ToolResult.error("source file exceeds 200 MiB")
        path = _upload_destination(source, kwargs.get("path"))
        try:
            await write(path, await asyncio.to_thread(source.read_bytes))
        except Exception as exc:  # noqa: BLE001 - report it, never crash the turn
            return ToolResult.error(_upload_failure(source, path, label, exc))
        return source, path

    @staticmethod
    def _local_attachment_allowed(source: Path) -> bool:
        """Accept Telegram files under the active config data directory.

        Telegram downloads use ``get_media_dir('telegram')``, which is derived
        from the active config path. The old check trusted only
        ``NANOBOT_DATA_DIR`` and could reject valid files when those two sources
        differed in a deployed process.
        """
        roots: list[Path] = [get_data_dir().expanduser().resolve()]
        configured = os.getenv("NANOBOT_DATA_DIR", "").strip()
        if configured:
            roots.append(Path(configured).expanduser().resolve())
        roots.append((Path.home() / ".nanobot").expanduser().resolve())
        return any(source == root or root in source.parents for root in roots)

    async def stage_telegram_attachments(
        self,
        media_paths: list[str],
        *,
        session_key: str,
    ) -> list[tuple[str, str]]:
        """Upload confirmed Telegram files to the active VPS workspace.

        This is deliberately a VPS-only pre-step. Novita keeps its established
        model-driven upload flow, while VPS turns confirmed local attachments
        into deterministic remote paths before the model turn is built.
        """
        selected_backend, config = self._selected_backend()
        if selected_backend != "vps":
            return []
        if config is None or not str(config.host or "").strip():
            raise RuntimeError("VPS execution is selected but SSH details are not configured")
        backend = VPSExecutionBackend(config)
        root = str(config.workspace_dir or _WORKSPACE).rstrip("/") or _WORKSPACE
        staged: list[tuple[str, str]] = []
        for raw_path in media_paths:
            source = Path(str(raw_path)).expanduser().resolve()
            if not self._local_attachment_allowed(source):
                raise ValueError("Telegram attachment is outside the nanobot media directory")
            if not source.is_file():
                raise FileNotFoundError(source.name)
            if source.stat().st_size > _MAX_UPLOAD_BYTES:
                raise ValueError("Telegram attachment exceeds 200 MiB")
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", source.name)[:120] or "attachment.bin"
            remote = f"{root}/telegram-attachments/{uuid4().hex}-{safe_name}"
            await upload_onlyfile_path(source)
            await backend.upload("telegram", remote, await asyncio.to_thread(source.read_bytes))
            staged.append((str(source), remote))
        return staged

    @staticmethod
    def _artifact_destination(remote_path: str) -> Path:
        ctx = current_request_context()
        workspace = (
            Path(ctx.workspace).expanduser().resolve()
            if ctx is not None and ctx.workspace is not None
            else get_workspace_path().expanduser().resolve()
        )
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(remote_path).name)[:120] or "artifact.bin"
        return workspace / ".nanobot" / "telegram-artifacts" / f"{uuid4().hex}-{safe_name}"

    async def _execute_vps(self, action: str, kwargs: dict[str, Any], config: Any) -> ToolResult | str:
        backend = VPSExecutionBackend(config)
        root = str(config.workspace_dir or _WORKSPACE)
        try:
            if action == "reset":
                return await backend.reset()
            if action == "run":
                command = str(kwargs.get("command") or "").strip()
                timeout = max(1, min(int(kwargs.get("timeout") or 120), _MAX_TIMEOUT))
                # Seed once per session, then source the credential file so git,
                # gh and curl authenticate inside the VPS (it does not inherit
                # the backend environment).
                if not getattr(backend, "_nb_creds_seeded", False):
                    await self._seed_git_credentials(backend, root)
                    backend._nb_creds_seeded = True
                return await backend.run(_git_creds_source_for(root) + command, timeout=timeout, cwd=root)
            if action == "install":
                packages = _install_packages_from_kwargs(kwargs)
                if not packages:
                    return ToolResult.error(_INSTALL_NEEDS_PACKAGES)
                timeout = max(30, min(int(kwargs.get("timeout") or 600), _MAX_TIMEOUT))
                result = await backend.install_packages(packages, timeout=timeout)
                return f"VPS package installation result:\n{result}"
            path = str(kwargs.get("path") or "")
            if action == "read":
                return await backend.read(path)
            if action == "write":
                content = str(kwargs.get("content") or "")
                if len(content) > _MAX_CONTENT_CHARS:
                    return ToolResult.error(
                        f"content exceeds {_MAX_CONTENT_CHARS} characters. Do NOT retry with the same payload: "
                        "instead split the file into sequential write ops (first op writes the head, "
                        'then {"action":"run","command":"cat >> \\"<path>\\" << \'PX_EOF\'\\n...\\nPX_EOF"} '
                        "appends each following chunk; use a unique heredoc marker)."
                    )
                await backend.write(path, content)
                return f"Wrote {len(content)} characters to {path} in the remote VPS workspace."
            if action == "upload":
                prepared = await self._prepare_upload(kwargs, label="the remote VPS workspace")
                if isinstance(prepared, ToolResult):
                    return prepared
                source, path = prepared
                try:
                    shared = await upload_onlyfile_path(source)
                    await backend.upload(str(source), path, await asyncio.to_thread(source.read_bytes))
                except Exception as exc:  # noqa: BLE001 - report it, never crash the turn
                    return ToolResult.error(_upload_failure(source, path, "the remote VPS workspace", exc))
                link = str(shared.get("gateway_url") or shared.get("url") or "").strip()
                return (
                    f"Uploaded {source.name} via onlyfiles.com to {path} in the remote VPS workspace."
                    + (f" Public link: {link}." if link else "")
                    + _UPLOAD_NOTE
                )
            if action == "fetch_url":
                url = str(kwargs.get("url") or "").strip()
                if not url:
                    return ToolResult.error("url is required for fetch_url")
                dest_path = path or f"{_WORKSPACE}/{re.sub(r'[^A-Za-z0-9._-]', '_', urlparse(url).path.rstrip('/').split('/')[-1] or 'download.bin')}"
                fetched = await backend.fetch_url(url, dest_path or _WORKSPACE, timeout=int(kwargs.get("timeout") or 150))
                return f"Fetched remote file to {fetched} in the remote VPS workspace. Use action=read or run commands to analyze it."
            if action == "list":
                return await backend.list(path)
            if action == "download_url":
                destination = self._artifact_destination(path)
                downloaded = await backend.download(path, destination)
                try:
                    shared = await upload_shared_artifact(downloaded)
                except (FileShareError, OnlyFilesError) as exc:
                    return ToolResult.error(f"Could not publish artifact link: {str(exc)[:200]}")
                return artifact_delivery_text(shared, downloaded)
            return ToolResult.error("Unknown sandbox action")
        except Exception as exc:
            logger.exception("VPS execution operation failed")
            return ToolResult.error(f"VPS execution error: {type(exc).__name__}: {str(exc)[:500]}")

    def _upstash_backend(self, config: Any, key: str) -> UpstashExecutionBackend:
        from nanobot.agent.tools.upstash_backend import upstash_box_name

        backend = UpstashExecutionBackend(config, box_name=upstash_box_name(key))
        # Seed the persisted box id (if any) so ensure_box verifies that exact
        # box directly instead of listing every box in the account on each op.
        stored_id = _UPSTASH_STORE.sandbox_id(key)
        if stored_id:
            backend.last_box_id = stored_id
        return backend

    def _daytona_backend(self, config: Any, key: str) -> DaytonaExecutionBackend:
        from nanobot.agent.tools.daytona_backend import daytona_sandbox_name

        backend = DaytonaExecutionBackend(config, sandbox_name=daytona_sandbox_name(key))
        # Seed the persisted sandbox id (if any) so ensure_sandbox verifies that
        # exact sandbox directly instead of re-resolving it by name each op.
        stored_id = _DAYTONA_STORE.sandbox_id(key)
        if stored_id:
            backend.last_sandbox_id = stored_id
        return backend


    async def _seed_git_credentials(self, backend: Any, root: str) -> None:
        """Write GitHub credentials into a remote backend's workspace.

        Every execution backend is an isolated container that does NOT inherit
        the backend process environment, so git/gh/curl ran unauthenticated even
        though GITHUB_BUILD_TOKEN is configured on the host. This seeds the same
        credential file the Novita path uses, for backends whose run() cannot
        prefix a source line. Best-effort: a failure must not break the action.

        MEASURED FAILURE (2026-09-29): seeding used to end at ``chmod``, which
        assumed the file it had just written was the file the shell would read.
        It was not. A credential file that is unreadable to the workspace user
        (observed as ``-rw------- root root`` inside an ``ubuntu``-owned
        workspace) is fatal to the *whole sandbox*: the source-prefix on every
        command aborts the shell, so the repair command was itself behind the
        fatal prefix and could never run. The write is therefore followed by a
        verification, and the permissions are *forced* before anything is
        assumed:

        * readability is verified (``[ -r ]``), never assumed, so a file that
          cannot be read is detected on the spot instead of on the next command;
        * ownership is repaired with one passwordless ``sudo -n chown`` when the
          guest allows it, and ``chmod 600`` is re-applied after it;
        * failing that, the file is *removed*. A credential file nobody can read
          authenticates nothing — it only breaks every command — whereas its
          absence is a case the guard tolerates by design (git runs
          unauthenticated, which is strictly better than a dead sandbox).
        """
        script = _git_creds_script()
        if not script:
            return
        path = f"{root.rstrip('/')}/.nanobot/github-env.sh"
        try:
            parent = path.rsplit("/", 1)[0]
            await backend.run(f"mkdir -p {shlex.quote(parent)}", timeout=60)
            await backend.write(path, script)
            quoted = shlex.quote(path)
            verify = (
                f"chmod 600 {quoted} 2>/dev/null; "
                f'if [ ! -r {quoted} ]; then sudo -n chown "$(id -u):$(id -g)" {quoted} 2>/dev/null; fi; '
                f"chmod 600 {quoted} 2>/dev/null; "
                f"if [ -r {quoted} ]; then echo nanobot-git-creds-ok; "
                f"else rm -f {quoted} 2>/dev/null; echo nanobot-git-creds-unreadable; fi"
            )
            outcome = await backend.run(verify, timeout=60)
            if "nanobot-git-creds-ok" not in str(outcome):
                logger.warning(
                    "GitHub credentials were not readable in the sandbox; removed {} so "
                    "commands still run (git will be unauthenticated): {}",
                    path,
                    str(outcome)[-200:],
                )
        except Exception as exc:  # noqa: BLE001 - credentials are best-effort
            logger.warning("could not seed git credentials into backend: {}", exc)

    def _runloop_backend(self, config: Any, key: str) -> RunloopExecutionBackend:
        backend = RunloopExecutionBackend(config, devbox_name=runloop_devbox_name(key))
        # Seed the persisted devbox id (if any) so ensure_devbox verifies that
        # exact devbox directly instead of re-resolving it by name each op.
        stored_id = _RUNLOOP_STORE.sandbox_id(key)
        if stored_id:
            backend.last_devbox_id = stored_id
        return backend

    def _tenki_backend(self, config: Any, key: str) -> TenkiExecutionBackend:
        backend = TenkiExecutionBackend(
            config,
            sandbox_name=tenki_sandbox_name(key),
            # The lane this session's disk lives in, recorded when its VM was
            # first created. Seeding it PINS every later operation to that
            # workspace: Tenki gives a session the same name in every workspace,
            # so rotating a live session would look for its files somewhere they
            # have never been and silently build a fresh, empty VM instead.
            lane_index=_TENKI_STORE.lane(key),
            # A brand-new session has no lane yet, so the round-robin picks one
            # from the persisted cursor (and records the choice back).
            rotation=_TENKI_STORE,
            on_lane_pinned=lambda lane, _key=key: _TENKI_STORE.set_lane(_key, lane),
        )
        # Seed the persisted session id (if any) so the backend reattaches to
        # that exact VM instead of re-resolving it by name on each operation.
        stored_id = _TENKI_STORE.sandbox_id(key)
        if stored_id:
            backend.last_session_id = stored_id
        return backend

    def _freestyle_backend(self, config: Any, key: str) -> FreestyleExecutionBackend:
        backend = FreestyleExecutionBackend(
            config,
            sandbox_name=freestyle_sandbox_name(key),
            # The lane this session's VM lives in, recorded when it was first
            # created. Seeding it PINS every later operation to that account:
            # the VM slug is unique per account, so rotating a live session
            # would build a second, empty VM somewhere its files are not.
            lane_index=_FREESTYLE_STORE.lane(key),
            # A brand-new session has no lane yet, so the round-robin picks one
            # from the persisted cursor (and records the choice back).
            rotation=_FREESTYLE_STORE,
            on_lane_pinned=lambda lane, _key=key: _FREESTYLE_STORE.set_lane(_key, lane),
        )
        # Seed the persisted VM id (if any) so the backend addresses that exact
        # VM instead of re-resolving it by slug on each operation.
        stored_id = _FREESTYLE_STORE.sandbox_id(key)
        if stored_id:
            backend.last_session_id = stored_id
        return backend

    def _vercel_backend(self, config: Any, key: str) -> VercelExecutionBackend:
        backend = VercelExecutionBackend(config, sandbox_name=vercel_sandbox_name(key))
        # Seed the persisted sandbox id (if any) so ensure_sandbox verifies that
        # exact sandbox directly instead of re-resolving it by name each op.
        stored_id = _VERCEL_STORE.sandbox_id(key)
        if stored_id:
            backend.last_sandbox_id = stored_id
        return backend

    async def release_upstash_sandbox(self, session_key: str | None = None) -> None:
        """Kill the session's ephemeral sandbox (Daytona / Upstash) once its task has finished.

        Daytona and Upstash sandboxes are billed while they exist, so a finished
        task must not leave one running. No-ops for the novita / vps backends so
        their persistent sandboxes are never affected. Best effort: failures are
        logged, never raised to the caller.
        """
        try:
            selected_backend, backend_config = self._selected_backend()
            if selected_backend == "daytona" and backend_config is not None:
                key = session_key or _session_key()
                sandbox_id = _DAYTONA_STORE.sandbox_id(key)
                if not sandbox_id:
                    return
                backend = self._daytona_backend(backend_config, key)
                if getattr(backend, "persist_workspace", True):
                    # "Perfect sandbox" persistence: a finished task must not
                    # wipe the user's workspace. Snapshot the workspace in the
                    # background and leave the session sandbox to its
                    # TTL/auto-stop, so writes/reads keep their state across
                    # tasks, restarts, and sandbox recreation. The task-end
                    # reply is not blocked by the snapshot.
                    async def _bg_daytona_snapshot() -> None:
                        try:
                            await asyncio.wait_for(
                                backend.snapshot_workspace(), timeout=_DAYTONA_SNAPSHOT_BUDGET
                            )
                        except Exception:
                            logger.debug("Background Daytona snapshot failed", exc_info=True)

                    asyncio.get_running_loop().create_task(_bg_daytona_snapshot())
                    return
                with suppress(Exception):
                    await asyncio.wait_for(
                        backend.reset(sandbox_id), timeout=_DAYTONA_RELEASE_RESET_BUDGET
                    )
                _DAYTONA_STORE.remove(key)
                return
            if selected_backend == "runloop" and backend_config is not None:
                key = session_key or _session_key()
                devbox_id = _RUNLOOP_STORE.sandbox_id(key)
                if not devbox_id:
                    return
                backend = self._runloop_backend(backend_config, key)
                if getattr(backend, "persist_workspace", True):
                    # "Perfect sandbox" persistence: a finished task must not
                    # wipe the user's workspace. Renew the devbox keep-alive so it
                    # survives until the next task (and across agent restarts),
                    # then leave its disk intact. Runloop's own deadline is the
                    # final backstop if no further work arrives.
                    async def _bg_runloop_keep_alive() -> None:
                        try:
                            await asyncio.wait_for(
                                backend.keep_alive(devbox_id), timeout=_RUNLOOP_KEEP_ALIVE_BUDGET
                            )
                        except Exception:
                            logger.debug("Background Runloop keep-alive failed", exc_info=True)

                    asyncio.get_running_loop().create_task(_bg_runloop_keep_alive())
                    return
                with suppress(Exception):
                    await asyncio.wait_for(
                        backend.reset(devbox_id), timeout=_RUNLOOP_RELEASE_RESET_BUDGET
                    )
                _RUNLOOP_STORE.remove(key)
                return
            if selected_backend == "freestyle" and backend_config is not None:
                key = session_key or _session_key()
                session_id = _FREESTYLE_STORE.sandbox_id(key)
                if not session_id:
                    return
                backend = self._freestyle_backend(backend_config, key)
                if getattr(backend, "persist_workspace", True):
                    # Persistence: a finished task must not wipe the user's
                    # workspace. Freestyle pauses an idle VM but never deletes
                    # it, so keeping it alive means leaving its disk intact; the
                    # provider's own total-run budget remains the backstop.
                    async def _bg_freestyle_keep_alive() -> None:
                        try:
                            await asyncio.wait_for(
                                backend.keep_alive(session_id), timeout=_FREESTYLE_KEEP_ALIVE_BUDGET
                            )
                        except Exception:
                            logger.debug("Background Freestyle keep-alive failed", exc_info=True)

                    asyncio.get_running_loop().create_task(_bg_freestyle_keep_alive())
                    return
                with suppress(Exception):
                    await asyncio.wait_for(
                        backend.reset(session_id), timeout=_FREESTYLE_RELEASE_RESET_BUDGET
                    )
                _FREESTYLE_STORE.remove(key)
                return
            if selected_backend == "tenki" and backend_config is not None:
                key = session_key or _session_key()
                session_id = _TENKI_STORE.sandbox_id(key)
                if not session_id:
                    return
                backend = self._tenki_backend(backend_config, key)
                if getattr(backend, "persist_workspace", True):
                    # "Perfect sandbox" persistence: a finished task must not
                    # wipe the user's workspace. Renew the session deadline so
                    # the VM survives until the next task (and across agent
                    # restarts), then leave its disk intact. Tenki's own max
                    # duration remains the final backstop if no work arrives.
                    async def _bg_tenki_keep_alive() -> None:
                        try:
                            await asyncio.wait_for(
                                backend.keep_alive(session_id), timeout=_TENKI_KEEP_ALIVE_BUDGET
                            )
                        except Exception:
                            logger.debug("Background Tenki keep-alive failed", exc_info=True)

                    asyncio.get_running_loop().create_task(_bg_tenki_keep_alive())
                    return
                with suppress(Exception):
                    await asyncio.wait_for(
                        backend.reset(session_id), timeout=_TENKI_RELEASE_RESET_BUDGET
                    )
                _TENKI_STORE.remove(key)
                return
            if selected_backend != "upstash" or backend_config is None:
                if selected_backend == "vercel" and backend_config is not None:
                    key = session_key or _session_key()
                    sandbox_id = _VERCEL_STORE.sandbox_id(key)
                    if not sandbox_id:
                        return
                    backend = self._vercel_backend(backend_config, key)
                    if getattr(backend, "persist_workspace", False):
                        # Persistence opted in: keep the sandbox alive for the
                        # next task by renewing its timeout window; its own
                        # deadline remains the final backstop.
                        async def _bg_vercel_keep_alive() -> None:
                            try:
                                await asyncio.wait_for(
                                    backend.keep_alive(sandbox_id), timeout=_VERCEL_KEEP_ALIVE_BUDGET
                                )
                            except Exception:
                                logger.debug("Background Vercel keep-alive failed", exc_info=True)

                        asyncio.get_running_loop().create_task(_bg_vercel_keep_alive())
                        return
                    # Default: Vercel bills only while the sandbox is alive and
                    # recreating one costs a sub-second provision, so a finished
                    # task stops the sandbox instead of leaving it running.
                    with suppress(Exception):
                        await asyncio.wait_for(
                            backend.reset(sandbox_id), timeout=_VERCEL_RELEASE_RESET_BUDGET
                        )
                    _VERCEL_STORE.remove(key)
                    return
                return
            key = session_key or _session_key()
            box_id = _UPSTASH_STORE.sandbox_id(key)
            if not box_id:
                return
            backend = self._upstash_backend(backend_config, key)
            if getattr(backend, "persist_workspace", True):
                # "Perfect box" persistence: a finished task must not wipe the
                # user's workspace. Snapshot the box into the archive box and
                # leave it running so writes/reads keep their state across
                # tasks, restarts, and box recreation.
                #
                # HARD WATCHDOG: this coroutine is awaited INLINE at task end
                # (the agent turn blocks on it), and snapshot_workspace's worst
                # case (cold-box wait + tar + staged read + archive box create
                # + upload) could previously block the turn for many minutes on
                # a cold or wedged box - the "task finished but the Upstash
                # sandbox hangs" report. Two defenses:
                #   1. The snapshot runs as a DETACHED background task, so the
                #      finished task's reply is never delayed by it.
                #   2. A hard wait_for budget (150s) still bounds the detached
                #      task so a wedged snapshot cannot linger forever.
                # The snapshot is best-effort: a missed one leaves the box (and
                # its workspace) intact and the next task reuses it via the
                # stored box id.
                async def _bg_snapshot() -> None:
                    try:
                        await asyncio.wait_for(
                            backend.snapshot_workspace(), timeout=_UPSTASH_SNAPSHOT_BUDGET
                        )
                    except Exception:
                        logger.debug("Background Upstash snapshot failed", exc_info=True)

                asyncio.get_running_loop().create_task(_bg_snapshot())
                return
            with suppress(Exception):
                await asyncio.wait_for(
                    backend.reset(box_id), timeout=_UPSTASH_RELEASE_RESET_BUDGET
                )
            _UPSTASH_STORE.remove(key)
        except Exception:
            logger.debug("Could not release sandbox", exc_info=True)

    async def _execute_daytona(
        self, action: str, kwargs: dict[str, Any], config: Any, session_key: str
    ) -> ToolResult | str:
        key = session_key or "unknown"
        backend = self._daytona_backend(config, key)
        try:
            if action == "reset":
                # Kill the user's sandbox immediately; a fresh sandbox is created
                # on the next operation. The stored id (if any) is deleted even
                # if the remote lookup fails, so nothing lingers.
                sandbox_id = _DAYTONA_STORE.sandbox_id(key)
                with suppress(Exception):
                    await backend.reset(sandbox_id)
                _DAYTONA_STORE.remove(key)
                return "Daytona sandbox reset. A new sandbox will be created for the next operation."
            if action not in {"run", "read", "write", "upload", "fetch_url", "install", "list", "download_url",
                              "apk_toolchain", "apk_decompile", "apk_build"}:
                return ToolResult.error("Unknown sandbox action")
            async with _DAYTONA_STORE.lock_for(key):
                if action == "apk_toolchain":
                    return await self._apk_toolchain(backend)
                if action == "apk_decompile":
                    return await self._apk_decompile(backend, kwargs)
                if action == "apk_build":
                    return await self._apk_build(backend, kwargs)
                if action == "run":
                    command = str(kwargs.get("command") or "").strip()
                    if not command:
                        return ToolResult.error("command is required")
                    timeout = max(1, min(int(kwargs.get("timeout") or 120), _MAX_TIMEOUT))
                    # First command of the session seeds the credential file so
                    # git/gh/curl authenticate (the box does not inherit the
                    # backend env). Then source it for every command.
                    if not getattr(backend, "_nb_creds_seeded", False):
                        await self._seed_git_credentials(backend, backend.workspace)
                        backend._nb_creds_seeded = True
                    output = await backend.run(_git_creds_source_for(backend.workspace) + command, timeout=timeout)
                    if getattr(backend, "last_sandbox_id", ""):
                        _DAYTONA_STORE.set_id(key, backend.last_sandbox_id)
                    return output
                if action == "install":
                    packages = _install_packages_from_kwargs(kwargs)
                    if not packages:
                        return ToolResult.error(_INSTALL_NEEDS_PACKAGES)
                    timeout = max(30, min(int(kwargs.get("timeout") or 600), _MAX_TIMEOUT))
                    result = await backend.install_packages(packages, timeout=timeout)
                    return f"Daytona sandbox package installation result:\n{result}"
                if action == "read":
                    return await backend.read(str(kwargs.get("path") or ""))
                if action == "write":
                    content = str(kwargs.get("content") or "")
                    if len(content) > _MAX_CONTENT_CHARS:
                        return ToolResult.error(
                            f"content exceeds {_MAX_CONTENT_CHARS} characters. Do NOT retry with the same payload: "
                            "instead split the file into sequential write ops (first op writes the head, "
                            'then {"action":"run","command":"cat >> \\"<path>\\" << \'PX_EOF\'\\n...\\nPX_EOF"} '
                            "appends each following chunk; use a unique heredoc marker)."
                        )
                    path = str(kwargs.get("path") or "")
                    await backend.write(path, content)
                    if getattr(backend, "last_sandbox_id", ""):
                        _DAYTONA_STORE.set_id(key, backend.last_sandbox_id)
                    return f"Wrote {len(content)} characters to {path} in the Daytona workspace."
                if action == "upload":
                    staged = await self._stage_upload(
                        kwargs,
                        lambda target, data: backend.write_bytes(target, data),
                        label="the Daytona workspace",
                    )
                    if isinstance(staged, ToolResult):
                        return staged
                    source, path = staged
                    if getattr(backend, "last_sandbox_id", ""):
                        _DAYTONA_STORE.set_id(key, backend.last_sandbox_id)
                    return f"Uploaded {source.name} to {path} in the Daytona workspace.{_UPLOAD_NOTE}"
                if action == "fetch_url":
                    url = str(kwargs.get("url") or "").strip()
                    if not url:
                        return ToolResult.error("url is required for fetch_url")
                    parsed = urlparse(url)
                    if is_gofile_url(url):
                        try:
                            resolved = await resolve_gofile_download(url, timeout_seconds=int(kwargs.get("timeout") or 150))
                        except GoFileError as exc:
                            return ToolResult.error(f"could not resolve gofile.io link: {exc}")
                        item = resolved[0]
                        real_name = re.sub(r"[^A-Za-z0-9._-]", "_", str(item.get("name") or "gofile_file")) or "gofile_file"
                        try:
                            data = await request_file(item, timeout_seconds=int(kwargs.get("timeout") or 150))
                        except GoFileError as exc:
                            return ToolResult.error(f"could not download gofile.io file: {exc}")
                        dest = str(kwargs.get("path") or "").strip() or f"{real_name}"
                        await backend.write_bytes(dest, data)
                        if getattr(backend, "last_sandbox_id", ""):
                            _DAYTONA_STORE.set_id(key, backend.last_sandbox_id)
                        return f"Fetched remote file to {dest} in the Daytona workspace. Use action=read or run commands to analyze it."
                    if parsed.scheme != "https" or parsed.netloc != "onlyfiles.com":
                        return ToolResult.error("url must be an HTTPS onlyfiles.com or gofile.io URL")
                    dest_path = str(kwargs.get("path") or "").strip()
                    fetched = await backend.fetch_url(url, dest_path, timeout=int(kwargs.get("timeout") or 150))
                    if getattr(backend, "last_sandbox_id", ""):
                        _DAYTONA_STORE.set_id(key, backend.last_sandbox_id)
                    return f"Fetched remote file to {fetched} in the Daytona workspace. Use action=read or run commands to analyze it."
                if action == "list":
                    return await backend.list(str(kwargs.get("path") or ""))
                if action == "download_url":
                    path = str(kwargs.get("path") or "")
                    destination = self._artifact_destination(path)
                    downloaded = await backend.download(path, destination)
                    if getattr(backend, "last_sandbox_id", ""):
                        _DAYTONA_STORE.set_id(key, backend.last_sandbox_id)
                    try:
                        shared = await upload_shared_artifact(downloaded)
                    except (FileShareError, OnlyFilesError) as exc:
                        return ToolResult.error(f"Could not publish artifact link: {str(exc)[:200]}")
                    return artifact_delivery_text(shared, downloaded)
            return ToolResult.error("Unknown sandbox action")
        except SandboxBusyError:
            return ToolResult.error(
                "A previous sandbox operation for this session is still running and did not finish in time. "
                "Wait a moment, then either retry the same step or reset the sandbox first."
            )
        except DaytonaError as exc:
            logger.warning("Daytona sandbox operation failed: {}", str(exc)[:300])
            return ToolResult.error(f"Daytona sandbox error: {str(exc)[:500]}")
        except Exception as exc:
            logger.exception("Daytona sandbox operation failed")
            return ToolResult.error(f"Daytona sandbox error: {type(exc).__name__}: {str(exc)[:500]}")

    @staticmethod
    def _runloop_action_budget(action: str, kwargs: dict[str, Any]) -> int:
        # Hard watchdog: whatever the underlying slow path (cold devbox,
        # provisioning, a wedged HTTP request), the AI's turn must never block
        # indefinitely. ``run``/``install``/``fetch_url`` track the caller's own
        # timeout plus margin; fixed budgets cover the rest.
        if action in {"run", "install", "fetch_url"}:
            try:
                requested = int(kwargs.get("timeout") or 0)
            except (TypeError, ValueError):
                requested = 0
            default = 600 if action == "install" else 150
            return max(300, min(max(requested, default), _MAX_TIMEOUT)) + 180
        return {
            "reset": 120,
            "read": 240,
            "write": 300,
            "upload": 420,
            "list": 180,
            "download_url": 480,
            "apk_toolchain": 900,
            "apk_decompile": 780,
            "apk_build": 780,
        }.get(action, 300)

    async def _execute_vercel(
        self, action: str, kwargs: dict[str, Any], config: Any, session_key: str
    ) -> ToolResult | str:
        """Run the shared sandbox action contract on a Vercel Sandbox."""
        budget = self._vercel_action_budget(action, kwargs)
        try:
            return await asyncio.wait_for(
                self._execute_vercel_inner(action, kwargs, config, session_key), timeout=budget
            )
        except asyncio.TimeoutError:
            return ToolResult.error(
                "The Vercel Sandbox operation did not finish in time. Wait a moment, then "
                "either retry the same step or reset the sandbox first."
            )

    @staticmethod
    def _vercel_action_budget(action: str, kwargs: dict[str, Any]) -> int:
        # Hard watchdog: whatever the underlying slow path (cold sandbox,
        # provisioning, a wedged HTTP request), the AI's turn must never block
        # indefinitely. ``run``/``install``/``fetch_url`` track the caller's own
        # timeout plus margin; fixed budgets cover the rest. Vercel provisions
        # in roughly a second, so the fixed budgets are tighter than Runloop's.
        if action in {"run", "install", "fetch_url"}:
            try:
                requested = int(kwargs.get("timeout") or 0)
            except (TypeError, ValueError):
                requested = 0
            default = 600 if action == "install" else 150
            return max(300, min(max(requested, default), _MAX_TIMEOUT)) + 120
        return {
            "reset": 120,
            "read": 180,
            "write": 300,
            "upload": 420,
            "list": 150,
            "download_url": 420,
        }.get(action, 240)

    async def _execute_vercel_inner(
        self, action: str, kwargs: dict[str, Any], config: Any, session_key: str
    ) -> ToolResult | str:
        key = session_key or "unknown"
        backend = self._vercel_backend(config, key)
        try:
            if action == "reset":
                # Stop the user's sandbox immediately; a fresh sandbox is created
                # on the next operation. The stored id is cleared even if the
                # remote call fails, so nothing lingers.
                sandbox_id = _VERCEL_STORE.sandbox_id(key)
                with suppress(Exception):
                    await backend.reset(sandbox_id)
                _VERCEL_STORE.remove(key)
                return "Vercel Sandbox reset. A new sandbox will be created for the next operation."
            if action not in {"run", "read", "write", "upload", "fetch_url", "install", "list", "download_url"}:
                return ToolResult.error("Unknown sandbox action")
            async with _VERCEL_STORE.lock_for(key):
                if action == "run":
                    command = str(kwargs.get("command") or "").strip()
                    if not command:
                        return ToolResult.error("command is required")
                    timeout = max(1, min(int(kwargs.get("timeout") or 120), _MAX_TIMEOUT))
                    # Seed once per session, then source the credential file so
                    # git/gh/curl authenticate (the sandbox does not inherit the
                    # backend environment).
                    if not getattr(backend, "_nb_creds_seeded", False):
                        await self._seed_git_credentials(backend, backend.workspace)
                        backend._nb_creds_seeded = True
                    output = await backend.run(_git_creds_source_for(backend.workspace) + command, timeout=timeout)
                    if getattr(backend, "last_sandbox_id", ""):
                        _VERCEL_STORE.set_id(key, backend.last_sandbox_id)
                    return output
                if action == "install":
                    packages = _install_packages_from_kwargs(kwargs)
                    if not packages:
                        return ToolResult.error(_INSTALL_NEEDS_PACKAGES)
                    timeout = max(30, min(int(kwargs.get("timeout") or 600), _MAX_TIMEOUT))
                    result = await backend.install_packages(packages, timeout=timeout)
                    if getattr(backend, "last_sandbox_id", ""):
                        _VERCEL_STORE.set_id(key, backend.last_sandbox_id)
                    return f"Vercel Sandbox package installation result:\n{result}"
                if action == "read":
                    return await backend.read(str(kwargs.get("path") or ""))
                if action == "write":
                    content = str(kwargs.get("content") or "")
                    if len(content) > _MAX_CONTENT_CHARS:
                        return ToolResult.error(
                            f"content exceeds {_MAX_CONTENT_CHARS} characters. Do NOT retry with the same payload: "
                            "instead split the file into sequential write ops (first op writes the head, "
                            'then {"action":"run","command":"cat >> \\"<path>\\" << \'PX_EOF\'\\n...\\nPX_EOF"} '
                            "appends each following chunk; use a unique heredoc marker)."
                        )
                    path = str(kwargs.get("path") or "")
                    await backend.write(path, content)
                    if getattr(backend, "last_sandbox_id", ""):
                        _VERCEL_STORE.set_id(key, backend.last_sandbox_id)
                    return f"Wrote {len(content)} characters to {path} in the Vercel workspace."
                if action == "upload":
                    staged = await self._stage_upload(
                        kwargs,
                        lambda target, data: backend.write_bytes(target, data),
                        label="the Vercel workspace",
                    )
                    if isinstance(staged, ToolResult):
                        return staged
                    source, path = staged
                    if getattr(backend, "last_sandbox_id", ""):
                        _VERCEL_STORE.set_id(key, backend.last_sandbox_id)
                    return f"Uploaded {source.name} to {path} in the Vercel workspace.{_UPLOAD_NOTE}"
                if action == "fetch_url":
                    url = str(kwargs.get("url") or "").strip()
                    if not url:
                        return ToolResult.error("url is required for fetch_url")
                    parsed = urlparse(url)
                    if is_gofile_url(url):
                        try:
                            resolved = await resolve_gofile_download(url, timeout_seconds=int(kwargs.get("timeout") or 150))
                        except GoFileError as exc:
                            return ToolResult.error(f"could not resolve gofile.io link: {exc}")
                        item = resolved[0]
                        real_name = re.sub(r"[^A-Za-z0-9._-]", "_", str(item.get("name") or "gofile_file")) or "gofile_file"
                        try:
                            data = await request_file(item, timeout_seconds=int(kwargs.get("timeout") or 150))
                        except GoFileError as exc:
                            return ToolResult.error(f"could not download gofile.io file: {exc}")
                        dest = str(kwargs.get("path") or "").strip() or f"{real_name}"
                        await backend.write_bytes(dest, data)
                        if getattr(backend, "last_sandbox_id", ""):
                            _VERCEL_STORE.set_id(key, backend.last_sandbox_id)
                        return f"Fetched remote file to {dest} in the Vercel workspace. Use action=read or run commands to analyze it."
                    if parsed.scheme != "https" or parsed.netloc not in {"onlyfiles.com", "gofile.io"}:
                        if not backend._is_host_allowed((parsed.netloc or "").lower()):
                            return ToolResult.error(
                                "url must be an HTTPS onlyfiles.com / gofile.io URL, or a host on the "
                                "Vercel fetch_allow_hosts list"
                            )
                    dest_path = str(kwargs.get("path") or "").strip()
                    fetched = await backend.fetch_url(url, dest_path, timeout=int(kwargs.get("timeout") or 150))
                    if getattr(backend, "last_sandbox_id", ""):
                        _VERCEL_STORE.set_id(key, backend.last_sandbox_id)
                    return f"Fetched remote file to {fetched} in the Vercel workspace. Use action=read or run commands to analyze it."
                if action == "list":
                    return await backend.list(str(kwargs.get("path") or ""))
                if action == "download_url":
                    path = str(kwargs.get("path") or "")
                    destination = self._artifact_destination(path)
                    downloaded = await backend.download(path, destination)
                    if getattr(backend, "last_sandbox_id", ""):
                        _VERCEL_STORE.set_id(key, backend.last_sandbox_id)
                    try:
                        shared = await upload_shared_artifact(downloaded)
                    except (FileShareError, OnlyFilesError) as exc:
                        return ToolResult.error(f"Could not publish artifact link: {str(exc)[:200]}")
                    return artifact_delivery_text(shared, downloaded)
            return ToolResult.error("Unknown sandbox action")
        except SandboxBusyError:
            return ToolResult.error(
                "A previous Vercel Sandbox operation for this session is still running and did not finish in time. "
                "Wait a moment, then either retry the same step or reset the sandbox first."
            )
        except VercelError as exc:
            logger.warning("Vercel Sandbox operation failed: {}", str(exc)[:300])
            return ToolResult.error(f"Vercel Sandbox error: {str(exc)[:500]}")
        except Exception as exc:
            logger.exception("Vercel Sandbox operation failed")
            return ToolResult.error(f"Vercel Sandbox error: {type(exc).__name__}: {str(exc)[:500]}")

    async def _execute_runloop(
        self, action: str, kwargs: dict[str, Any], config: Any, session_key: str
    ) -> ToolResult | str:
        """Run the shared sandbox action contract on a Runloop Devbox."""
        budget = self._runloop_action_budget(action, kwargs)
        try:
            return await asyncio.wait_for(
                self._execute_runloop_inner(action, kwargs, config, session_key), timeout=budget
            )
        except asyncio.TimeoutError:
            return ToolResult.error(
                "The Runloop Devbox operation did not finish in time. Wait a moment, then "
                "either retry the same step or reset the sandbox first."
            )

    async def _execute_runloop_inner(
        self, action: str, kwargs: dict[str, Any], config: Any, session_key: str
    ) -> ToolResult | str:
        key = session_key or "unknown"
        backend = self._runloop_backend(config, key)
        try:
            if action == "reset":
                # Shut the user's devbox down immediately; a fresh devbox is
                # created on the next operation. The stored id is cleared even
                # if the remote call fails, so nothing lingers.
                devbox_id = _RUNLOOP_STORE.sandbox_id(key)
                with suppress(Exception):
                    await backend.reset(devbox_id)
                _RUNLOOP_STORE.remove(key)
                return "Runloop Devbox reset. A new devbox will be created for the next operation."
            if action not in {"run", "read", "write", "upload", "fetch_url", "install", "list", "download_url",
                              "apk_toolchain", "apk_decompile", "apk_build"}:
                return ToolResult.error("Unknown sandbox action")
            async with _RUNLOOP_STORE.lock_for(key):
                if action == "apk_toolchain":
                    return await self._apk_toolchain(backend)
                if action == "apk_decompile":
                    return await self._apk_decompile(backend, kwargs)
                if action == "apk_build":
                    return await self._apk_build(backend, kwargs)
                if action == "run":
                    command = str(kwargs.get("command") or "").strip()
                    if not command:
                        return ToolResult.error("command is required")
                    timeout = max(1, min(int(kwargs.get("timeout") or 120), _MAX_TIMEOUT))
                    # Seed once per session, then source the credential file so
                    # git/gh/curl authenticate (the Devbox does not inherit the
                    # backend environment).
                    if not getattr(backend, "_nb_creds_seeded", False):
                        await self._seed_git_credentials(backend, backend.workspace)
                        backend._nb_creds_seeded = True
                    output = await backend.run(_git_creds_source_for(backend.workspace) + command, timeout=timeout)
                    if getattr(backend, "last_devbox_id", ""):
                        _RUNLOOP_STORE.set_id(key, backend.last_devbox_id)
                    return output
                if action == "install":
                    packages = _install_packages_from_kwargs(kwargs)
                    if not packages:
                        return ToolResult.error(_INSTALL_NEEDS_PACKAGES)
                    timeout = max(30, min(int(kwargs.get("timeout") or 600), _MAX_TIMEOUT))
                    result = await backend.install_packages(packages, timeout=timeout)
                    return f"Runloop Devbox package installation result:\n{result}"
                if action == "read":
                    return await backend.read(str(kwargs.get("path") or ""))
                if action == "write":
                    content = str(kwargs.get("content") or "")
                    if len(content) > _MAX_CONTENT_CHARS:
                        return ToolResult.error(
                            f"content exceeds {_MAX_CONTENT_CHARS} characters. Do NOT retry with the same payload: "
                            "instead split the file into sequential write ops (first op writes the head, "
                            'then {"action":"run","command":"cat >> \\"<path>\\" << \'PX_EOF\'\\n...\\nPX_EOF"} '
                            "appends each following chunk; use a unique heredoc marker)."
                        )
                    path = str(kwargs.get("path") or "")
                    await backend.write(path, content)
                    if getattr(backend, "last_devbox_id", ""):
                        _RUNLOOP_STORE.set_id(key, backend.last_devbox_id)
                    return f"Wrote {len(content)} characters to {path} in the Runloop workspace."
                if action == "upload":
                    staged = await self._stage_upload(
                        kwargs,
                        lambda target, data: backend.write_bytes(target, data),
                        label="the Runloop workspace",
                    )
                    if isinstance(staged, ToolResult):
                        return staged
                    source, path = staged
                    if getattr(backend, "last_devbox_id", ""):
                        _RUNLOOP_STORE.set_id(key, backend.last_devbox_id)
                    return f"Uploaded {source.name} to {path} in the Runloop workspace.{_UPLOAD_NOTE}"
                if action == "fetch_url":
                    url = str(kwargs.get("url") or "").strip()
                    if not url:
                        return ToolResult.error("url is required for fetch_url")
                    parsed = urlparse(url)
                    if is_gofile_url(url):
                        try:
                            resolved = await resolve_gofile_download(url, timeout_seconds=int(kwargs.get("timeout") or 150))
                        except GoFileError as exc:
                            return ToolResult.error(f"could not resolve gofile.io link: {exc}")
                        item = resolved[0]
                        real_name = re.sub(r"[^A-Za-z0-9._-]", "_", str(item.get("name") or "gofile_file")) or "gofile_file"
                        try:
                            data = await request_file(item, timeout_seconds=int(kwargs.get("timeout") or 150))
                        except GoFileError as exc:
                            return ToolResult.error(f"could not download gofile.io file: {exc}")
                        dest = str(kwargs.get("path") or "").strip() or f"{real_name}"
                        await backend.write_bytes(dest, data)
                        if getattr(backend, "last_devbox_id", ""):
                            _RUNLOOP_STORE.set_id(key, backend.last_devbox_id)
                        return f"Fetched remote file to {dest} in the Runloop workspace. Use action=read or run commands to analyze it."
                    if parsed.scheme != "https" or parsed.netloc != "onlyfiles.com":
                        return ToolResult.error("url must be an HTTPS onlyfiles.com or gofile.io URL")
                    dest_path = str(kwargs.get("path") or "").strip()
                    fetched = await backend.fetch_url(url, dest_path, timeout=int(kwargs.get("timeout") or 150))
                    if getattr(backend, "last_devbox_id", ""):
                        _RUNLOOP_STORE.set_id(key, backend.last_devbox_id)
                    return f"Fetched remote file to {fetched} in the Runloop workspace. Use action=read or run commands to analyze it."
                if action == "list":
                    return await backend.list(str(kwargs.get("path") or ""))
                if action == "download_url":
                    path = str(kwargs.get("path") or "")
                    destination = self._artifact_destination(path)
                    downloaded = await backend.download(path, destination)
                    if getattr(backend, "last_devbox_id", ""):
                        _RUNLOOP_STORE.set_id(key, backend.last_devbox_id)
                    try:
                        shared = await upload_shared_artifact(downloaded)
                    except (FileShareError, OnlyFilesError) as exc:
                        return ToolResult.error(f"Could not publish artifact link: {str(exc)[:200]}")
                    return artifact_delivery_text(shared, downloaded)
            return ToolResult.error("Unknown sandbox action")
        except SandboxBusyError:
            return ToolResult.error(
                "A previous Runloop Devbox operation for this session is still running and did not finish in time. "
                "Wait a moment, then either retry the same step or reset the sandbox first."
            )
        except RunloopError as exc:
            logger.warning("Runloop Devbox operation failed: {}", str(exc)[:300])
            return ToolResult.error(f"Runloop Devbox error: {str(exc)[:500]}")
        except Exception as exc:
            logger.exception("Runloop Devbox operation failed")
            return ToolResult.error(f"Runloop Devbox error: {type(exc).__name__}: {str(exc)[:500]}")

    async def _execute_tenki(
        self, action: str, kwargs: dict[str, Any], config: Any, session_key: str
    ) -> ToolResult | str:
        """Run the shared sandbox action contract on a Tenki Sandbox."""
        budget = self._tenki_action_budget(action, kwargs)
        try:
            return await asyncio.wait_for(
                self._execute_tenki_inner(action, kwargs, config, session_key), timeout=budget
            )
        except asyncio.TimeoutError:
            return ToolResult.error(
                "The Tenki Sandbox operation did not finish in time. Wait a moment, then "
                "either retry the same step or reset the sandbox first."
            )

    @staticmethod
    def _tenki_action_budget(action: str, kwargs: dict[str, Any]) -> int:
        # Hard watchdog: whatever the underlying slow path (cold VM, image pull,
        # provisioning), the AI's turn must never block indefinitely.
        # ``run``/``install``/``fetch_url`` track the caller's own timeout plus
        # margin; fixed budgets cover the rest. A Tenki VM lands in seconds, so
        # the fixed budgets stay close to the Vercel ones.
        if action in {"run", "install", "fetch_url"}:
            try:
                requested = int(kwargs.get("timeout") or 0)
            except (TypeError, ValueError):
                requested = 0
            default = 600 if action == "install" else 150
            return max(300, min(max(requested, default), _MAX_TIMEOUT)) + 180
        return {
            "reset": 120,
            "read": 240,
            "write": 300,
            "upload": 420,
            "list": 180,
            "download_url": 480,
            "apk_toolchain": 900,
            "apk_decompile": 780,
            "apk_build": 780,
        }.get(action, 300)

    async def _execute_tenki_inner(
        self, action: str, kwargs: dict[str, Any], config: Any, session_key: str
    ) -> ToolResult | str:
        key = session_key or "unknown"
        backend = self._tenki_backend(config, key)
        try:
            if action == "reset":
                # Terminate the user's VM immediately; a fresh one is created on
                # the next operation. The stored id is cleared even if the remote
                # call fails, so nothing lingers.
                session_id = _TENKI_STORE.sandbox_id(key)
                with suppress(Exception):
                    await backend.reset(session_id)
                _TENKI_STORE.remove(key)
                return "Tenki Sandbox reset. A new session will be created for the next operation."
            if action not in {"run", "read", "write", "upload", "fetch_url", "install", "list", "download_url",
                              "apk_toolchain", "apk_decompile", "apk_build"}:
                return ToolResult.error("Unknown sandbox action")
            async with _TENKI_STORE.lock_for(key):
                if action == "apk_toolchain":
                    return await self._apk_toolchain(backend)
                if action == "apk_decompile":
                    return await self._apk_decompile(backend, kwargs)
                if action == "apk_build":
                    return await self._apk_build(backend, kwargs)
                if action == "run":
                    command = str(kwargs.get("command") or "").strip()
                    if not command:
                        return ToolResult.error("command is required")
                    timeout = max(1, min(int(kwargs.get("timeout") or 120), _MAX_TIMEOUT))
                    # Seed once per session, then source the credential file so
                    # git/gh/curl authenticate (the VM does not inherit the
                    # backend environment).
                    if not getattr(backend, "_nb_creds_seeded", False):
                        await self._seed_git_credentials(backend, backend.workspace)
                        backend._nb_creds_seeded = True
                    output = await backend.run(_git_creds_source_for(backend.workspace) + command, timeout=timeout)
                    if getattr(backend, "last_session_id", ""):
                        _TENKI_STORE.set_id(key, backend.last_session_id)
                    return output
                if action == "install":
                    packages = _install_packages_from_kwargs(kwargs)
                    if not packages:
                        return ToolResult.error(_INSTALL_NEEDS_PACKAGES)
                    timeout = max(30, min(int(kwargs.get("timeout") or 600), _MAX_TIMEOUT))
                    result = await backend.install_packages(packages, timeout=timeout)
                    return f"Tenki Sandbox package installation result:\n{result}"
                if action == "read":
                    return await backend.read(str(kwargs.get("path") or ""))
                if action == "write":
                    content = str(kwargs.get("content") or "")
                    if len(content) > _MAX_CONTENT_CHARS:
                        return ToolResult.error(
                            f"content exceeds {_MAX_CONTENT_CHARS} characters. Do NOT retry with the same payload: "
                            "instead split the file into sequential write ops (first op writes the head, "
                            'then {"action":"run","command":"cat >> \\"<path>\\" << \'PX_EOF\'\\n...\\nPX_EOF"} '
                            "appends each following chunk; use a unique heredoc marker)."
                        )
                    path = str(kwargs.get("path") or "")
                    await backend.write(path, content)
                    if getattr(backend, "last_session_id", ""):
                        _TENKI_STORE.set_id(key, backend.last_session_id)
                    return f"Wrote {len(content)} characters to {path} in the Tenki workspace."
                if action == "upload":
                    staged = await self._stage_upload(
                        kwargs,
                        lambda target, data: backend.write_bytes(target, data),
                        label="the Tenki workspace",
                    )
                    if isinstance(staged, ToolResult):
                        return staged
                    source, path = staged
                    if getattr(backend, "last_session_id", ""):
                        _TENKI_STORE.set_id(key, backend.last_session_id)
                    return f"Uploaded {source.name} to {path} in the Tenki workspace.{_UPLOAD_NOTE}"
                if action == "fetch_url":
                    url = str(kwargs.get("url") or "").strip()
                    if not url:
                        return ToolResult.error("url is required for fetch_url")
                    parsed = urlparse(url)
                    if is_gofile_url(url):
                        try:
                            resolved = await resolve_gofile_download(url, timeout_seconds=int(kwargs.get("timeout") or 150))
                        except GoFileError as exc:
                            return ToolResult.error(f"could not resolve gofile.io link: {exc}")
                        item = resolved[0]
                        real_name = re.sub(r"[^A-Za-z0-9._-]", "_", str(item.get("name") or "gofile_file")) or "gofile_file"
                        try:
                            data = await request_file(item, timeout_seconds=int(kwargs.get("timeout") or 150))
                        except GoFileError as exc:
                            return ToolResult.error(f"could not download gofile.io file: {exc}")
                        dest = str(kwargs.get("path") or "").strip() or f"{real_name}"
                        await backend.write_bytes(dest, data)
                        if getattr(backend, "last_session_id", ""):
                            _TENKI_STORE.set_id(key, backend.last_session_id)
                        return f"Fetched remote file to {dest} in the Tenki workspace. Use action=read or run commands to analyze it."
                    if parsed.scheme != "https" or parsed.netloc != "onlyfiles.com":
                        return ToolResult.error("url must be an HTTPS onlyfiles.com or gofile.io URL")
                    dest_path = str(kwargs.get("path") or "").strip()
                    fetched = await backend.fetch_url(url, dest_path, timeout=int(kwargs.get("timeout") or 150))
                    if getattr(backend, "last_session_id", ""):
                        _TENKI_STORE.set_id(key, backend.last_session_id)
                    return f"Fetched remote file to {fetched} in the Tenki workspace. Use action=read or run commands to analyze it."
                if action == "list":
                    return await backend.list(str(kwargs.get("path") or ""))
                if action == "download_url":
                    path = str(kwargs.get("path") or "")
                    destination = self._artifact_destination(path)
                    downloaded = await backend.download(path, destination)
                    if getattr(backend, "last_session_id", ""):
                        _TENKI_STORE.set_id(key, backend.last_session_id)
                    try:
                        shared = await upload_shared_artifact(downloaded)
                    except (FileShareError, OnlyFilesError) as exc:
                        return ToolResult.error(f"Could not publish artifact link: {str(exc)[:200]}")
                    return artifact_delivery_text(shared, downloaded)
            return ToolResult.error("Unknown sandbox action")
        except SandboxBusyError:
            return ToolResult.error(
                "A previous Tenki Sandbox operation for this session is still running and did not finish in time. "
                "Wait a moment, then either retry the same step or reset the sandbox first."
            )
        except TenkiError as exc:
            logger.warning("Tenki Sandbox operation failed: {}", str(exc)[:300])
            return ToolResult.error(f"Tenki Sandbox error: {str(exc)[:500]}")
        except Exception as exc:
            logger.exception("Tenki Sandbox operation failed")
            return ToolResult.error(f"Tenki Sandbox error: {type(exc).__name__}: {str(exc)[:500]}")

    async def _execute_freestyle(
        self, action: str, kwargs: dict[str, Any], config: Any, session_key: str
    ) -> ToolResult | str:
        """Run the shared sandbox action contract on a Freestyle VM.

        NOT a staticmethod: it is dispatched as ``self._execute_freestyle(action,
        kwargs, config, session_key)`` and uses ``self`` for both the nested
        ``_execute_freestyle_inner`` call and the action budget. Decorating it
        bound the first positional argument to ``self`` and left ``session_key``
        unfilled, so EVERY Freestyle action — run, read, write, list, reset —
        raised ``TypeError: _execute_freestyle() missing 1 required positional
        argument: 'session_key'`` before reaching the VM. That is what made the
        sandbox unusable the moment an administrator selected Freestyle.
        """
        budget = self._freestyle_action_budget(action, kwargs)
        try:
            return await asyncio.wait_for(
                self._execute_freestyle_inner(action, kwargs, config, session_key), timeout=budget
            )
        except asyncio.TimeoutError:
            return ToolResult.error(
                "The Freestyle VM operation did not finish in time. Wait a moment, then "
                "either retry the same step or reset the sandbox first."
            )

    @staticmethod
    def _freestyle_action_budget(action: str, kwargs: dict[str, Any]) -> int:
        # Hard watchdog: whatever the underlying slow path (cold VM, image pull,
        # provisioning), the AI's turn must never block indefinitely.
        # ``run``/``install``/``fetch_url`` track the caller's own timeout plus
        # margin; fixed budgets cover the rest. A Tenki VM lands in seconds, so
        # the fixed budgets stay close to the Vercel ones.
        if action in {"run", "install", "fetch_url"}:
            try:
                requested = int(kwargs.get("timeout") or 0)
            except (TypeError, ValueError):
                requested = 0
            default = 600 if action == "install" else 150
            return max(300, min(max(requested, default), _MAX_TIMEOUT)) + 180
        return {
            "reset": 120,
            "read": 240,
            "write": 300,
            "upload": 420,
            "list": 180,
            "download_url": 480,
            "apk_toolchain": 900,
            "apk_decompile": 780,
            "apk_build": 780,
        }.get(action, 300)

    async def _execute_freestyle_inner(
        self, action: str, kwargs: dict[str, Any], config: Any, session_key: str
    ) -> ToolResult | str:
        key = session_key or "unknown"
        backend = self._freestyle_backend(config, key)
        try:
            if action == "reset":
                # Terminate the user's VM immediately; a fresh one is created on
                # the next operation. The stored id is cleared even if the remote
                # call fails, so nothing lingers.
                session_id = _FREESTYLE_STORE.sandbox_id(key)
                with suppress(Exception):
                    await backend.reset(session_id)
                _FREESTYLE_STORE.remove(key)
                return "Freestyle VM reset. A new session will be created for the next operation."
            if action not in {"run", "read", "write", "upload", "fetch_url", "install", "list", "download_url",
                              "apk_toolchain", "apk_decompile", "apk_build"}:
                return ToolResult.error("Unknown sandbox action")
            async with _FREESTYLE_STORE.lock_for(key):
                if action == "apk_toolchain":
                    return await self._apk_toolchain(backend)
                if action == "apk_decompile":
                    return await self._apk_decompile(backend, kwargs)
                if action == "apk_build":
                    return await self._apk_build(backend, kwargs)
                if action == "run":
                    command = str(kwargs.get("command") or "").strip()
                    if not command:
                        return ToolResult.error("command is required")
                    timeout = max(1, min(int(kwargs.get("timeout") or 120), _MAX_TIMEOUT))
                    # Seed once per session, then source the credential file so
                    # git/gh/curl authenticate (the VM does not inherit the
                    # backend environment).
                    if not getattr(backend, "_nb_creds_seeded", False):
                        await self._seed_git_credentials(backend, backend.workspace)
                        backend._nb_creds_seeded = True
                    output = await backend.run(_git_creds_source_for(backend.workspace) + command, timeout=timeout)
                    if getattr(backend, "last_session_id", ""):
                        _FREESTYLE_STORE.set_id(key, backend.last_session_id)
                    return output
                if action == "install":
                    packages = _install_packages_from_kwargs(kwargs)
                    if not packages:
                        return ToolResult.error(_INSTALL_NEEDS_PACKAGES)
                    timeout = max(30, min(int(kwargs.get("timeout") or 600), _MAX_TIMEOUT))
                    result = await backend.install_packages(packages, timeout=timeout)
                    return f"Freestyle VM package installation result:\n{result}"
                if action == "read":
                    return await backend.read(str(kwargs.get("path") or ""))
                if action == "write":
                    content = str(kwargs.get("content") or "")
                    if len(content) > _MAX_CONTENT_CHARS:
                        return ToolResult.error(
                            f"content exceeds {_MAX_CONTENT_CHARS} characters. Do NOT retry with the same payload: "
                            "instead split the file into sequential write ops (first op writes the head, "
                            'then {"action":"run","command":"cat >> \\"<path>\\" << \'PX_EOF\'\\n...\\nPX_EOF"} '
                            "appends each following chunk; use a unique heredoc marker)."
                        )
                    path = str(kwargs.get("path") or "")
                    await backend.write(path, content)
                    if getattr(backend, "last_session_id", ""):
                        _FREESTYLE_STORE.set_id(key, backend.last_session_id)
                    return f"Wrote {len(content)} characters to {path} in the Freestyle VM."
                if action == "upload":
                    staged = await self._stage_upload(
                        kwargs,
                        lambda target, data: backend.write_bytes(target, data),
                        label="the Freestyle VM",
                    )
                    if isinstance(staged, ToolResult):
                        return staged
                    source, path = staged
                    if getattr(backend, "last_session_id", ""):
                        _FREESTYLE_STORE.set_id(key, backend.last_session_id)
                    return f"Uploaded {source.name} to {path} in the Freestyle VM.{_UPLOAD_NOTE}"
                if action == "fetch_url":
                    url = str(kwargs.get("url") or "").strip()
                    if not url:
                        return ToolResult.error("url is required for fetch_url")
                    parsed = urlparse(url)
                    if is_gofile_url(url):
                        try:
                            resolved = await resolve_gofile_download(url, timeout_seconds=int(kwargs.get("timeout") or 150))
                        except GoFileError as exc:
                            return ToolResult.error(f"could not resolve gofile.io link: {exc}")
                        item = resolved[0]
                        real_name = re.sub(r"[^A-Za-z0-9._-]", "_", str(item.get("name") or "gofile_file")) or "gofile_file"
                        try:
                            data = await request_file(item, timeout_seconds=int(kwargs.get("timeout") or 150))
                        except GoFileError as exc:
                            return ToolResult.error(f"could not download gofile.io file: {exc}")
                        dest = str(kwargs.get("path") or "").strip() or f"{real_name}"
                        await backend.write_bytes(dest, data)
                        if getattr(backend, "last_session_id", ""):
                            _FREESTYLE_STORE.set_id(key, backend.last_session_id)
                        return f"Fetched remote file to {dest} in the Freestyle VM. Use action=read or run commands to analyze it."
                    if parsed.scheme != "https" or parsed.netloc != "onlyfiles.com":
                        return ToolResult.error("url must be an HTTPS onlyfiles.com or gofile.io URL")
                    dest_path = str(kwargs.get("path") or "").strip()
                    fetched = await backend.fetch_url(url, dest_path, timeout=int(kwargs.get("timeout") or 150))
                    if getattr(backend, "last_session_id", ""):
                        _FREESTYLE_STORE.set_id(key, backend.last_session_id)
                    return f"Fetched remote file to {fetched} in the Freestyle VM. Use action=read or run commands to analyze it."
                if action == "list":
                    return await backend.list(str(kwargs.get("path") or ""))
                if action == "download_url":
                    path = str(kwargs.get("path") or "")
                    destination = self._artifact_destination(path)
                    downloaded = await backend.download(path, destination)
                    if getattr(backend, "last_session_id", ""):
                        _FREESTYLE_STORE.set_id(key, backend.last_session_id)
                    try:
                        shared = await upload_shared_artifact(downloaded)
                    except (FileShareError, OnlyFilesError) as exc:
                        return ToolResult.error(f"Could not publish artifact link: {str(exc)[:200]}")
                    return artifact_delivery_text(shared, downloaded)
            return ToolResult.error("Unknown sandbox action")
        except SandboxBusyError:
            return ToolResult.error(
                "A previous Freestyle VM operation for this session is still running and did not finish in time. "
                "Wait a moment, then either retry the same step or reset the sandbox first."
            )
        except FreestyleError as exc:
            logger.warning("Freestyle VM operation failed: {}", str(exc)[:300])
            return ToolResult.error(f"Freestyle VM error: {str(exc)[:500]}")
        except Exception as exc:
            logger.exception("Freestyle VM operation failed")
            return ToolResult.error(f"Freestyle VM error: {type(exc).__name__}: {str(exc)[:500]}")

    @staticmethod
    def _upstash_action_budget(action: str, kwargs: dict[str, Any]) -> int:
        # Hard watchdog: whatever the underlying slow path (cold box, snapshot
        # restore, a wedged HTTP request), the AI's turn must never block
        # indefinitely — that is exactly what users reported as "the AI hangs
        # in the Upstash sandbox". Every action is bounded: run/install get the
        # caller's timeout plus margin; fixed budgets cover the rest.
        if action in {"run", "install", "fetch_url"}:
            try:
                requested = int(kwargs.get("timeout") or 0)
            except (TypeError, ValueError):
                requested = 0
            default = 600 if action == "install" else 150
            return max(300, min(max(requested, default), _MAX_TIMEOUT)) + 150
        return {
            "reset": 90,
            "read": 180,
            "write": 330,
            "upload": 360,
            "list": 120,
            "download_url": 480,
            "apk_toolchain": 900,
            "apk_decompile": 780,
            "apk_build": 780,
        }.get(action, 240)

    # ---------------------------------------------------------------- apk
    # In-sandbox APK assemble/disassemble (user-space):
    # decompile an EXISTING binary -> patch smali/resources -> rebuild -> sign.
    # Building apk/deb/EXE from PROJECT SOURCE prefers GitHub Actions via the
    # build_artifact tool, and falls back to this same user-space toolchain when
    # that path is unavailable — the JDK + build-tools installed here are the
    # first half of that fallback. A build from source is never refused.

    _APK_TOOLS_DIR = "$HOME/.powerx-tools"
    _APK_KEYSTORE_PASS = "powerx123"

    def _apk_env_prefix(self) -> str:
        root = self._APK_TOOLS_DIR
        return (
            f'export JAVA_HOME={root}/jdk; '
            f'[ -x "$JAVA_HOME/bin/java" ] && export PATH="$JAVA_HOME/bin:$PATH"; '
            f'BT=$(ls -d {root}/build-tools/android-* 2>/dev/null | head -1); '
            f'[ -n "$BT" ] && export PATH="$BT:$PATH"; '
        )

    async def _apk_run(self, backend: Any, step: str, command: str, timeout: int) -> str:
        out = await backend.run(command, timeout=timeout)
        if "[exit_code=" in out and "[exit_code=0]" not in out:
            raise RuntimeError(f"APK {step} failed:\n{out[-1500:]}")
        return out

    async def _apk_toolchain(self, backend: Any) -> str:
        """Install the user-space APK toolchain (JDK 17 + apktool + build-tools + keystore)."""
        root = self._APK_TOOLS_DIR
        ksp = self._APK_KEYSTORE_PASS
        await self._apk_run(backend, "prepare", f"mkdir -p {root}/jdk {root}/build-tools", 30)
        probe = await backend.run(
            'if command -v java >/dev/null 2>&1 || [ -x "$HOME/.powerx-tools/jdk/bin/java" ]; '
            "then printf READY; else printf MISSING; fi",
            timeout=30,
        )
        if "READY" not in probe:
            await self._apk_run(
                backend,
                "install JDK",
                f'curl -fL --max-time 230 -o /tmp/jre.tgz '
                f'"https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jre/hotspot/normal/eclipse" '
                f'&& tar xzf /tmp/jre.tgz -C {root}/jdk --strip-components=1 && echo JDK_INSTALLED',
                240,
            )
        await self._apk_run(
            backend,
            "fetch apktool",
            f'if [ ! -s {root}/apktool.jar ]; then curl -fL --max-time 200 -o {root}/apktool.jar '
            f'https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar; fi; '
            f'ls -lh {root}/apktool.jar',
            240,
        )
        await self._apk_run(
            backend,
            "fetch build-tools",
            f'if ! ls {root}/build-tools/android-*/apksigner >/dev/null 2>&1; then '
            f'curl -fL --max-time 230 -o /tmp/bt.zip https://dl.google.com/android/repository/build-tools_r34-linux.zip '
            f'&& cd {root}/build-tools && (unzip -q /tmp/bt.zip 2>/dev/null || python3 -c "import zipfile; '
            f'zipfile.ZipFile(\'/tmp/bt.zip\').extractall(\'.\')"); fi; '
            f'ls -d {root}/build-tools/android-*',
            240,
        )
        await self._apk_run(
            backend,
            "keystore",
            f'KS={root}/px.keystore; if [ ! -f "$KS" ]; then '
            f'({root}/jdk/bin/keytool -genkeypair -keystore "$KS" -alias powerx -keyalg RSA -keysize 2048 '
            f'-validity 10000 -storepass {ksp} -keypass {ksp} -dname "CN=PowerX" 2>/dev/null '
            f'|| keytool -genkeypair -keystore "$KS" -alias powerx -keyalg RSA -keysize 2048 -validity 10000 '
            f'-storepass {ksp} -keypass {ksp} -dname "CN=PowerX"); fi; ls -lh "$KS"',
            60,
        )
        ver = await self._apk_run(
            backend,
            "verify",
            f'{self._apk_env_prefix()} java -jar {root}/apktool.jar --version 2>&1 | tail -1; '
            f'apksigner --version 2>&1 | tail -1',
            60,
        )
        return "APK toolchain ready in the sandbox ($HOME/.powerx-tools: JDK 17, apktool, Android build-tools, px.keystore).\n" + ver

    async def _apk_decompile(self, backend: Any, kwargs: dict[str, Any]) -> str:
        """Decompile an existing APK into smali + resources (apktool d)."""
        root = self._APK_TOOLS_DIR
        apk = str(kwargs.get("apk_path") or "app.apk").strip()
        out = str(kwargs.get("out") or "").strip() or (re.sub(r"\.apk$", "", apk) + ".out")
        guard = f'[ -s {root}/apktool.jar ] || {{ echo APK_TOOLCHAIN_MISSING_RUN_apk_toolchain_FIRST; exit 7; }}; '
        result = await self._apk_run(
            backend,
            "decompile",
            f'{self._apk_env_prefix()} {guard} java -jar {root}/apktool.jar d -f '
            f'-o {shlex.quote(out)} {shlex.quote(apk)}',
            240,
        )
        listing = await self._apk_run(backend, "list decompiled tree", f'ls {shlex.quote(out)} | head -12', 30)
        return f"Decompiled {apk} -> {out}\n{result}\n{listing}"

    async def _apk_build(self, backend: Any, kwargs: dict[str, Any]) -> str:
        """Rebuild + sign a patched decompiled tree into an installable APK."""
        root = self._APK_TOOLS_DIR
        src = str(kwargs.get("src") or "app.out").strip()
        out = str(kwargs.get("out") or "app-rebuilt.apk").strip()
        ksp = self._APK_KEYSTORE_PASS
        await self._apk_run(
            backend, "build",
            f'{self._apk_env_prefix()} [ -s {root}/apktool.jar ] || {{ echo APK_TOOLCHAIN_MISSING_RUN_apk_toolchain_FIRST; exit 7; }}; '
            f'java -jar {root}/apktool.jar b -f {shlex.quote(src)} -o /tmp/px-unsigned.apk',
            240,
        )
        await self._apk_run(backend, "align", f'{self._apk_env_prefix()} zipalign -f 4 /tmp/px-unsigned.apk /tmp/px-aligned.apk', 60)
        await self._apk_run(
            backend,
            "sign",
            f'{self._apk_env_prefix()} apksigner sign --ks {root}/px.keystore --ks-pass pass:{ksp} '
            f'--out {shlex.quote(out)} /tmp/px-aligned.apk && apksigner verify {shlex.quote(out)}',
            60,
        )
        listing = await self._apk_run(backend, "verify", f'ls -lh {shlex.quote(out)}', 30)
        return f"Rebuilt and signed APK: {out}\n{listing}"

    async def _execute_upstash(
        self, action: str, kwargs: dict[str, Any], config: Any, session_key: str
    ) -> ToolResult | str:
        budget = self._upstash_action_budget(action, kwargs)
        try:
            return await asyncio.wait_for(
                self._execute_upstash_inner(action, kwargs, config, session_key), timeout=budget
            )
        except asyncio.TimeoutError:
            logger.warning("Upstash sandbox action {} exceeded its {}s budget", action, budget)
            return ToolResult.error(
                f"Upstash Box operation timed out after {budget}s (action={action}). The box may be cold or the "
                "previous operation wedged. Retry once — a warm box usually answers immediately — or reset the "
                "sandbox with action=reset and retry if it keeps happening."
            )

    async def _execute_upstash_inner(
        self, action: str, kwargs: dict[str, Any], config: Any, session_key: str
    ) -> ToolResult | str:
        key = session_key or "unknown"
        backend = self._upstash_backend(config, key)
        try:
            if action == "reset":
                # Kill the user's sandbox immediately; a fresh box is created on
                # the next operation. The stored id (if any) is deleted even if
                # the remote lookup fails, so nothing lingers.
                box_id = _UPSTASH_STORE.sandbox_id(key)
                with suppress(Exception):
                    await backend.reset(box_id)
                _UPSTASH_STORE.remove(key)
                return "Upstash Box reset. A new sandbox will be created for the next operation."
            if action not in {"run", "read", "write", "upload", "fetch_url", "install", "list", "download_url",
                              "apk_toolchain", "apk_decompile", "apk_build"}:
                return ToolResult.error("Unknown sandbox action")
            async with _UPSTASH_STORE.lock_for(key):
                if action == "apk_toolchain":
                    return await self._apk_toolchain(backend)
                if action == "apk_decompile":
                    return await self._apk_decompile(backend, kwargs)
                if action == "apk_build":
                    return await self._apk_build(backend, kwargs)
                if action == "run":
                    command = str(kwargs.get("command") or "").strip()
                    if not command:
                        return ToolResult.error("command is required")
                    timeout = max(1, min(int(kwargs.get("timeout") or 120), _MAX_TIMEOUT))
                    # Seed once per session, then source the credential file so
                    # git/gh/curl authenticate (the Box does not inherit the
                    # backend environment).
                    if not getattr(backend, "_nb_creds_seeded", False):
                        await self._seed_git_credentials(backend, backend.workspace)
                        backend._nb_creds_seeded = True
                    output = await backend.run(_git_creds_source_for(backend.workspace) + command, timeout=timeout)
                    if getattr(backend, "last_box_id", ""):
                        _UPSTASH_STORE.set_id(key, backend.last_box_id)
                    return output
                if action == "install":
                    packages = _install_packages_from_kwargs(kwargs)
                    if not packages:
                        return ToolResult.error(_INSTALL_NEEDS_PACKAGES)
                    timeout = max(30, min(int(kwargs.get("timeout") or 600), _MAX_TIMEOUT))
                    result = await backend.install_packages(packages, timeout=timeout)
                    return f"Upstash Box package installation result:\n{result}"
                if action == "read":
                    return await backend.read(str(kwargs.get("path") or ""))
                if action == "write":
                    content = str(kwargs.get("content") or "")
                    if len(content) > _MAX_CONTENT_CHARS:
                        return ToolResult.error(
                            f"content exceeds {_MAX_CONTENT_CHARS} characters. Do NOT retry with the same payload: "
                            "instead split the file into sequential write ops (first op writes the head, "
                            'then {"action":"run","command":"cat >> \\"<path>\\" << \'PX_EOF\'\\n...\\nPX_EOF"} '
                            "appends each following chunk; use a unique heredoc marker)."
                        )
                    path = str(kwargs.get("path") or "")
                    await backend.write(path, content)
                    return f"Wrote {len(content)} characters to {path} in the Upstash workspace."
                if action == "upload":
                    staged = await self._stage_upload(
                        kwargs,
                        lambda target, data: backend.write_bytes(target, data),
                        label="the Upstash workspace",
                    )
                    if isinstance(staged, ToolResult):
                        return staged
                    source, path = staged
                    return f"Uploaded {source.name} to {path} in the Upstash workspace.{_UPLOAD_NOTE}"
                if action == "fetch_url":
                    url = str(kwargs.get("url") or "").strip()
                    if not url:
                        return ToolResult.error("url is required for fetch_url")
                    parsed = urlparse(url)
                    if is_gofile_url(url):
                        try:
                            resolved = await resolve_gofile_download(url, timeout_seconds=int(kwargs.get("timeout") or 150))
                        except GoFileError as exc:
                            return ToolResult.error(f"could not resolve gofile.io link: {exc}")
                        item = resolved[0]
                        real_name = re.sub(r"[^A-Za-z0-9._-]", "_", str(item.get("name") or "gofile_file")) or "gofile_file"
                        try:
                            data = await request_file(item, timeout_seconds=int(kwargs.get("timeout") or 150))
                        except GoFileError as exc:
                            return ToolResult.error(f"could not download gofile.io file: {exc}")
                        dest = str(kwargs.get("path") or "").strip() or f"{real_name}"
                        await backend.write_bytes(dest, data)
                        return f"Fetched remote file to {dest} in the Upstash workspace. Use action=read or run commands to analyze it."
                    if parsed.scheme != "https" or parsed.netloc != "onlyfiles.com":
                        return ToolResult.error("url must be an HTTPS onlyfiles.com or gofile.io URL")
                    dest_path = str(kwargs.get("path") or "").strip()
                    fetched = await backend.fetch_url(url, dest_path, timeout=int(kwargs.get("timeout") or 150))
                    return f"Fetched remote file to {fetched} in the Upstash workspace. Use action=read or run commands to analyze it."
                if action == "list":
                    return await backend.list(str(kwargs.get("path") or ""))
                if action == "download_url":
                    path = str(kwargs.get("path") or "")
                    destination = self._artifact_destination(path)
                    downloaded = await backend.download(path, destination)
                    try:
                        shared = await upload_shared_artifact(downloaded)
                    except (FileShareError, OnlyFilesError) as exc:
                        return ToolResult.error(f"Could not publish artifact link: {str(exc)[:200]}")
                    return artifact_delivery_text(shared, downloaded)
            return ToolResult.error("Unknown sandbox action")
        except SandboxBusyError:
            return ToolResult.error(
                "A previous Upstash Box operation for this session is still running and did not finish in time. "
                "Wait a moment, then either retry the same step or reset the sandbox first."
            )
        except UpstashError as exc:
            logger.warning("Upstash Box operation failed: {}", str(exc)[:300])
            return ToolResult.error(f"Upstash Box error: {str(exc)[:500]}")
        except Exception as exc:
            logger.exception("Upstash Box operation failed")
            return ToolResult.error(f"Upstash Box error: {type(exc).__name__}: {str(exc)[:500]}")

    async def execute(self, **kwargs: Any) -> ToolResult | str:
        action = str(kwargs.get("action", "")).strip().lower()
        selected_backend, backend_config = self._selected_backend()
        if selected_backend == "vps":
            if backend_config is None or not str(backend_config.host or "").strip():
                return ToolResult.error("VPS execution is selected but SSH details are not configured")
            return await self._execute_vps(action, kwargs, backend_config)
        if selected_backend == "daytona":
            if backend_config is None or not str(backend_config.api_key or "").strip():
                return ToolResult.error("Daytona execution is selected but no API key is configured")
            ctx = current_request_context()
            session_key = (ctx.session_key or f"{ctx.channel}:{ctx.chat_id}") if ctx is not None else _session_key()
            return await self._execute_daytona(action, kwargs, backend_config, session_key)
        if selected_backend == "upstash":
            if backend_config is None or not str(backend_config.api_key or "").strip():
                return ToolResult.error("Upstash Box execution is selected but no API key is configured")
            ctx = current_request_context()
            session_key = (ctx.session_key or f"{ctx.channel}:{ctx.chat_id}") if ctx is not None else _session_key()
            return await self._execute_upstash(action, kwargs, backend_config, session_key)
        if selected_backend == "runloop":
            if backend_config is None or not str(backend_config.api_key or "").strip():
                return ToolResult.error("Runloop execution is selected but no API key is configured")
            ctx = current_request_context()
            session_key = (ctx.session_key or f"{ctx.channel}:{ctx.chat_id}") if ctx is not None else _session_key()
            return await self._execute_runloop(action, kwargs, backend_config, session_key)
        if selected_backend == "tenki":
            if not _tenki_key_configured(backend_config):
                return ToolResult.error("Tenki execution is selected but no API key is configured")
            ctx = current_request_context()
            session_key = (ctx.session_key or f"{ctx.channel}:{ctx.chat_id}") if ctx is not None else _session_key()
            return await self._execute_tenki(action, kwargs, backend_config, session_key)
        if selected_backend == "freestyle":
            if not _freestyle_key_configured(backend_config):
                return ToolResult.error("Freestyle execution is selected but no API key is configured")
            ctx = current_request_context()
            session_key = (ctx.session_key or f"{ctx.channel}:{ctx.chat_id}") if ctx is not None else _session_key()
            return await self._execute_freestyle(action, kwargs, backend_config, session_key)
        if selected_backend == "vercel":
            if backend_config is None or not str(backend_config.token or "").strip():
                return ToolResult.error("Vercel execution is selected but no token is configured")
            ctx = current_request_context()
            session_key = (ctx.session_key or f"{ctx.channel}:{ctx.chat_id}") if ctx is not None else _session_key()
            return await self._execute_vercel(action, kwargs, backend_config, session_key)
        key = _session_key()
        try:
            if action == "reset":
                async with _STORE.lock_for(key):
                    sandbox = _STORE.get(key)
                    if sandbox is not None:
                        await asyncio.to_thread(sandbox.kill)
                    _STORE.remove(key)
                return "Remote Novita Sandbox reset. A new one will be created for the next operation."
            if action not in {"run", "read", "write", "upload", "fetch_url", "list", "download_url"}:
                return ToolResult.error("Unknown sandbox action")
            async with _STORE.lock_for(key):
                sandbox = await asyncio.to_thread(self._get_or_create, key)
                if action == "run":
                    command = str(kwargs.get("command") or "").strip()
                    if not command:
                        return ToolResult.error("command is required")
                    if len(command) > _MAX_COMMAND_CHARS:
                        return ToolResult.error(f"command exceeds {_MAX_COMMAND_CHARS} characters")
                    timeout = max(1, min(int(kwargs.get("timeout") or 120), _MAX_TIMEOUT))
                    result = await asyncio.to_thread(
                        sandbox.commands.run,
                        # Source the credential file so git/gh/curl authenticate
                        # inside the sandbox. Failure is tolerated (`|| true`), so
                        # boxes without a configured token behave as before.
                        _GIT_CREDS_SOURCE + command,
                        cwd=_WORKSPACE,
                        timeout=timeout,
                        request_timeout=timeout + 30,
                    )
                    return _output(result)
                # `upload` and `fetch_url` derive their own destination, so they
                # must not run through the required-path guard: an omitted `path`
                # used to raise `ValueError: path is required` here, before the
                # branch below could default it.
                if action in {"upload", "fetch_url"}:
                    path = ""
                else:
                    try:
                        path = _safe_path(str(kwargs.get("path") or ""))
                    except ValueError as exc:
                        return ToolResult.error(f"{action} needs a valid 'path': {exc}")
                if action == "read":
                    content = await asyncio.to_thread(sandbox.files.read, path)
                    text = str(content)
                    if len(text) > _MAX_RESULT_CHARS:
                        # Never silently drop the beginning of a large file —
                        # models assume they saw everything and write broken
                        # edits. Keep the tail (most relevant for logs) but
                        # tell the model exactly what is missing and how to
                        # inspect the rest cheaply.
                        return (
                            f"[read truncated: file is {len(text)} characters; "
                            f"showing only the LAST {_MAX_RESULT_CHARS}. Use "
                            "action=run with head/sed -n/grep to read earlier "
                            "sections]\n" + text[-_MAX_RESULT_CHARS:]
                        )
                    return text or "(empty file)"
                if action == "write":
                    content = str(kwargs.get("content") or "")
                    if len(content) > _MAX_CONTENT_CHARS:
                        return ToolResult.error(
                            f"content exceeds {_MAX_CONTENT_CHARS} characters. Do NOT retry with the same payload: "
                            "instead split the file into sequential write ops (first op writes the head, "
                            'then {"action":"run","command":"cat >> \\"<path>\\" << \'PX_EOF\'\\n...\\nPX_EOF"} '
                            "appends each following chunk; use a unique heredoc marker)."
                        )
                    await asyncio.to_thread(sandbox.files.write, path, content)
                    return f"Wrote {len(content)} characters to {path} in the remote sandbox."
                if action == "upload":
                    staged = await self._stage_upload(
                        kwargs,
                        lambda target, data: asyncio.to_thread(sandbox.files.write, target, data),
                        label="the remote Novita sandbox",
                    )
                    if isinstance(staged, ToolResult):
                        return staged
                    source, path = staged
                    return f"Uploaded {source.name} to {path} in the remote sandbox.{_UPLOAD_NOTE}"
                if action == "fetch_url":
                    url = str(kwargs.get("url") or "").strip()
                    if not url:
                        return ToolResult.error("url is required for fetch_url")
                    parsed_url = urlparse(url)
                    allowed = parsed_url.scheme == "https" and (
                        parsed_url.netloc in {"onlyfiles.com", "gofile.io"}
                        or parsed_url.netloc.endswith(".gofile.io")
                    )
                    if not allowed:
                        return ToolResult.error(
                            "url must be an HTTPS onlyfiles.com or gofile.io URL"
                        )
                    dest = _safe_path(str(kwargs.get("path") or "") or (
                        f"{_WORKSPACE}/{re.sub(r'[^A-Za-z0-9._-]', '_', parsed_url.path.rstrip('/').split('/')[-1] or 'download.bin')}"
                    ))
                    timeout = max(30, min(int(kwargs.get("timeout") or 150), _MAX_TIMEOUT))
                    # Download on the host, then write the bytes into the sandbox.
                    # Using the host avoids depending on the sandbox image shipping curl.
                    if is_gofile_url(url):
                        # gofile.io ``/d/<code>`` pages are HTML landing pages; resolve
                        # the share through the GoFile API to get the real direct link
                        # and file name so we never write an HTML shell to disk. The
                        # resolved descriptor carries the guest token that must be sent
                        # (as the accountToken cookie + Range header) to get the file.
                        try:
                            resolved = await resolve_gofile_download(
                                url, timeout_seconds=timeout
                            )
                        except GoFileError as exc:
                            return ToolResult.error(
                                f"could not resolve gofile.io link: {exc}"
                            )
                        item = resolved[0]
                        real_name = re.sub(
                            r"[^A-Za-z0-9._-]",
                            "_",
                            str(item.get("name") or "gofile_file"),
                        ) or "gofile_file"
                        dest = _safe_path(str(kwargs.get("path") or "") or (
                            f"{_WORKSPACE}/{real_name}"
                        ))
                        try:
                            data = await request_file(item, timeout_seconds=timeout)
                        except GoFileError as exc:
                            return ToolResult.error(
                                f"could not download gofile.io file: {exc}"
                            )
                    else:
                        download_url = url
                        import aiohttp
                        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                            async with session.get(download_url, allow_redirects=True) as resp:
                                if resp.status < 200 or resp.status >= 300:
                                    return ToolResult.error(f"remote fetch failed with HTTP {resp.status}")
                                content_type = resp.headers.get("Content-Type", "")
                                if "text/html" in content_type.lower():
                                    return ToolResult.error(
                                        "remote URL resolved to an HTML page rather than a direct binary file"
                                    )
                                data = await resp.read()
                    await asyncio.to_thread(sandbox.files.write, dest, data)
                    return (
                        f"Fetched remote file to {dest} in the remote sandbox "
                        f"({len(data)} bytes). Use action=read or run commands to analyze it."
                    )
                if action == "list":
                    result = await asyncio.to_thread(
                        sandbox.commands.run,
                        f"find {posixpath.dirname(path) if path != _WORKSPACE else _WORKSPACE} -maxdepth 2 -printf '%y %p\\n' | head -200",
                        cwd=_WORKSPACE,
                        timeout=30,
                        request_timeout=60,
                    )
                    return _output(result)
                signed = await asyncio.to_thread(
                    sandbox.download_url, path, use_signature_expiration=300
                )
                # Publish through onlyfiles instead of handing back the sandbox's
                # own signed URL. That URL expires in FIVE MINUTES, so a user who
                # taps it a moment later gets nothing — the same "the link is not
                # working" report as the missing gateway link. The bytes are
                # fetched here while the signature is still valid and republished
                # permanently, which is the contract every other backend already
                # delivers (and what the user asked for: when the LLM gives files,
                # it uses onlyfiles).
                try:
                    shared = await _publish_signed_artifact(
                        str(signed), filename=Path(path).name or "artifact.bin"
                    )
                except (FileShareError, OnlyFilesError) as exc:
                    return ToolResult.error(f"Could not publish artifact link: {str(exc)[:200]}")
                return artifact_delivery_text(shared, path)
        except Exception as exc:
            logger.exception("Novita Sandbox operation failed")
            return ToolResult.error(f"Novita Sandbox error: {type(exc).__name__}: {str(exc)[:500]}")
