from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import posixpath
import re
import shlex
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.agent.tools.upstash_backend import UpstashError, UpstashExecutionBackend
from nanobot.agent.tools.vps_backend import VPSExecutionBackend
from nanobot.config.paths import get_data_dir, get_workspace_path
from nanobot.utils.gofile import GoFileError, is_gofile_url, request_file, resolve_gofile_download
from nanobot.utils.helpers import detect_image_mime
from nanobot.utils.tmpfiles import upload_bytes as upload_tmpfile_bytes
from nanobot.utils.tmpfiles import upload_path as upload_tmpfile_path

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
_WORKSPACE = "/workspace"
_OCR_DIR = f"{_WORKSPACE}/.nanobot"

#: Automatic Novita sandbox sizing when the admin configured none. The stock
#: "base" image ships ~486 MB which OOM-kills builds/OCR, so we default every
#: spawned sandbox to a 2 GB box. Env (NOVITA_SANDBOX_MEMORY_MB / _CPU_COUNT) or
#: execution.novita_template still override these; see _template_sizing().
DEFAULT_TEMPLATE_CPU = 2
DEFAULT_TEMPLATE_MEMORY_MB = 2048

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

    def lock_for(self, key: str) -> asyncio.Lock:
        with self._lock:
            return self._locks.setdefault(key, asyncio.Lock())

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

# Alias cache for dynamically built Novita templates (desired alias → usable alias).
_TEMPLATE_CACHE: dict[str, str] = {}
_TEMPLATE_BUILD_LOCK = threading.Lock()


def _safe_path(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("path is required")
    if not value.startswith("/"):
        value = posixpath.join(_WORKSPACE, value)
    normalized = posixpath.normpath(value)
    if normalized != _WORKSPACE and not normalized.startswith(_WORKSPACE + "/"):
        raise ValueError("path must remain inside /workspace")
    return normalized


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


@tool_parameters(
    tool_parameters_schema(
        required=["action"],
        additional_properties=None,
        action=StringSchema(
            "Operation: run, read, write, upload, fetch_url, list, download_url, or reset",
            enum=["run", "read", "write", "upload", "fetch_url", "list", "download_url", "reset"],
        ),
        command=StringSchema("Command to run inside the remote sandbox"),
        path=StringSchema("Sandbox path, relative paths resolve under /workspace"),
        url=StringSchema("Remote HTTPS URL to fetch into the sandbox (tmpfiles.org or gofile.io)"),
        content=StringSchema("Text content for write"),
        timeout=IntegerSchema(description="Command timeout in seconds", minimum=1, maximum=_MAX_TIMEOUT),
        source=StringSchema("Local media path to upload into the remote sandbox"),
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
        execution = getattr(ctx, "execution", None)
        backend = getattr(execution, "backend", "novita") if execution is not None else "novita"
        if backend == "vps":
            return bool(getattr(execution.vps, "host", "").strip())
        if backend == "upstash":
            return bool(getattr(execution.upstash, "api_key", "").strip())
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
            "fetch a remote HTTPS file (tmpfiles.org or gofile.io) into the workspace, "
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
            "the local file through tmpfiles.org, then fetch it into the VPS workspace with "
            "curl before using the staged path. If a required Linux command is missing "
            "in VPS mode, use action=install with a space-separated list of distro package "
            "names; installation is noninteractive and uses root or already-configured "
            "passwordless sudo. Never add repositories, remove packages, or put a sudo "
            "password in a command. When a finished file should be returned, call "
            "download_url with its remote workspace path; this downloads the artifact and "
            "also creates a temporary tmpfiles.org link. Use the local path in the message "
            "tool's media parameter when direct attachment delivery is available. "
            "For multi-step work, prefer the sandbox_batch tool so many operations "
            "cost one model call instead of one call per step."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["run", "read", "write", "upload", "fetch_url", "install", "list", "download_url", "reset"]},
                "command": {"type": "string"},
                "packages": {"type": "string", "description": "Space-separated Linux distro package names to install in VPS mode."},
                "path": {"type": "string"},
                "url": {"type": "string", "description": "Remote HTTPS URL to fetch into the sandbox (tmpfiles.org or gofile.io)."},
                "content": {"type": "string"},
                "timeout": {"type": "integer", "minimum": 1, "maximum": _MAX_TIMEOUT},
                "source": {"type": "string"},
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
                await upload_tmpfile_bytes(raw, filename=path.name, content_type=detect_image_mime(raw))
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
        (DEFAULT_TEMPLATE_CPU / DEFAULT_TEMPLATE_MEMORY_MB = 2 vCPU / 2 GB)
        instead of returning None. Returning None previously let sandboxes fall
        back to the stock "base" image (~486 MB), which OOM-kills any nontrivial
        build/OCR. So an admin who loads nothing still gets 2 GB per sandbox;
        explicit config always overrides this default.
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
        # Nothing configured anywhere: apply the automatic 2 GB default rather
        # than dropping to the tiny stock base image.
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
            return _first_existing(["powerx-base-2g-c2", "powerx-base-4g", "base"]) or "base"

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
                fallback = _first_existing([alias, "powerx-base-2g-c2", "powerx-base-4g"]) or "base"
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

        sandbox = _STORE.get(key)
        if sandbox is not None:
            try:
                if sandbox.is_running() and _matches_sizing(_STORE.template_for(key)):
                    return sandbox
            except Exception:
                pass
            # Wrong-sized or dead handle: drop it (and its id) before recreating.
            _STORE.remove(key)
        sandbox_id = _STORE.sandbox_id(key)
        if sandbox_id:
            try:
                sandbox = client.sandbox.connect(sandbox_id)
                if sandbox.is_running() and _matches_sizing(_STORE.template_for(key)):
                    _STORE.set(key, sandbox, template=sandbox_template)
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
        last_error: Exception | None = None
        for attempt in range(6):
            try:
                sandbox.commands.run(
                    f"mkdir -p {_WORKSPACE}",
                    cwd="/",
                    timeout=30,
                    request_timeout=60,
                )
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt < 5:
                    import time
                    time.sleep(3)
        if last_error is not None:
            try:
                sandbox.kill()
            except Exception:
                pass
            raise last_error
        _STORE.set(key, sandbox, template=sandbox_template)
        return sandbox

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
            await upload_tmpfile_path(source)
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
                return await backend.run(command, timeout=timeout, cwd=root)
            if action == "install":
                raw_packages = str(kwargs.get("packages") or "").strip()
                packages = [part for part in re.split(r"[\s,]+", raw_packages) if part]
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
                source = Path(str(kwargs.get("source") or "")).expanduser().resolve()
                if not self._local_attachment_allowed(source):
                    return ToolResult.error("source must be inside the nanobot media/data directory")
                if not source.is_file():
                    return ToolResult.error("source file does not exist")
                if source.stat().st_size > _MAX_UPLOAD_BYTES:
                    return ToolResult.error("source file exceeds 200 MiB")
                await upload_tmpfile_path(source)
                await backend.upload(str(source), path, await asyncio.to_thread(source.read_bytes))
                return f"Uploaded {source.name} via tmpfiles.org to {path} in the remote VPS workspace."
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
                tmpfile = await upload_tmpfile_path(downloaded)
                return (
                    f"Downloaded remote artifact to local path: {downloaded}\n"
                    "A temporary public download link is also available and expires soon:\n"
                    f"{tmpfile['download_url']}\n"
                    "Give the user this link and do NOT paste the file contents into "
                    "your reply. The file may also be attached directly via the "
                    "message tool's media parameter when direct attachment delivery "
                    "is available. Prefer a single clear download link over dumping "
                    "raw text."
                )
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

    async def release_upstash_sandbox(self, session_key: str | None = None) -> None:
        """Kill the session's Upstash box immediately once its task has finished.

        Upstash-only: no-ops for the novita / vps backends so their persistent
        sandboxes are never affected. Best effort: failures are logged, never
        raised to the caller.
        """
        try:
            selected_backend, backend_config = self._selected_backend()
            if selected_backend != "upstash" or backend_config is None:
                return
            key = session_key or _session_key()
            box_id = _UPSTASH_STORE.sandbox_id(key)
            if not box_id:
                return
            backend = self._upstash_backend(backend_config, key)
            with suppress(Exception):
                await backend.reset(box_id)
            _UPSTASH_STORE.remove(key)
        except Exception:
            logger.debug("Could not release Upstash box", exc_info=True)

    async def _execute_upstash(
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
            if action not in {"run", "read", "write", "upload", "fetch_url", "install", "list", "download_url"}:
                return ToolResult.error("Unknown sandbox action")
            async with _UPSTASH_STORE.lock_for(key):
                if action == "run":
                    command = str(kwargs.get("command") or "").strip()
                    if not command:
                        return ToolResult.error("command is required")
                    timeout = max(1, min(int(kwargs.get("timeout") or 120), _MAX_TIMEOUT))
                    output = await backend.run(command, timeout=timeout)
                    if getattr(backend, "last_box_id", ""):
                        _UPSTASH_STORE.set_id(key, backend.last_box_id)
                    return output
                if action == "install":
                    raw_packages = str(kwargs.get("packages") or "").strip()
                    packages = [part for part in re.split(r"[\s,]+", raw_packages) if part]
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
                    source = Path(str(kwargs.get("source") or "")).expanduser().resolve()
                    if not self._local_attachment_allowed(source):
                        return ToolResult.error("source must be inside the nanobot media/data directory")
                    if not source.is_file():
                        return ToolResult.error("source file does not exist")
                    if source.stat().st_size > _MAX_UPLOAD_BYTES:
                        return ToolResult.error("source file exceeds 200 MiB")
                    path = str(kwargs.get("path") or "")
                    await backend.write_bytes(path, await asyncio.to_thread(source.read_bytes))
                    return f"Uploaded {source.name} to {path} in the Upstash workspace."
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
                    if parsed.scheme != "https" or parsed.netloc != "tmpfiles.org":
                        return ToolResult.error("url must be an HTTPS tmpfiles.org or gofile.io URL")
                    dest_path = str(kwargs.get("path") or "").strip()
                    fetched = await backend.fetch_url(url, dest_path, timeout=int(kwargs.get("timeout") or 150))
                    return f"Fetched remote file to {fetched} in the Upstash workspace. Use action=read or run commands to analyze it."
                if action == "list":
                    return await backend.list(str(kwargs.get("path") or ""))
                if action == "download_url":
                    path = str(kwargs.get("path") or "")
                    destination = self._artifact_destination(path)
                    downloaded = await backend.download(path, destination)
                    tmpfile = await upload_tmpfile_path(downloaded)
                    return (
                        f"Downloaded remote artifact to local path: {downloaded}\n"
                        "A temporary public download link is also available and expires soon:\n"
                        f"{tmpfile['download_url']}\n"
                        "Give the user this link and do NOT paste the file contents into "
                        "your reply. The file may also be attached directly via the "
                        "message tool's media parameter when direct attachment delivery "
                        "is available. Prefer a single clear download link over dumping "
                        "raw text."
                    )
            return ToolResult.error("Unknown sandbox action")
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
        if selected_backend == "upstash":
            if backend_config is None or not str(backend_config.api_key or "").strip():
                return ToolResult.error("Upstash Box execution is selected but no API key is configured")
            ctx = current_request_context()
            session_key = (ctx.session_key or f"{ctx.channel}:{ctx.chat_id}") if ctx is not None else _session_key()
            return await self._execute_upstash(action, kwargs, backend_config, session_key)
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
                        command,
                        cwd=_WORKSPACE,
                        timeout=timeout,
                        request_timeout=timeout + 30,
                    )
                    return _output(result)
                path = _safe_path(str(kwargs.get("path") or ""))
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
                    source = Path(str(kwargs.get("source") or "")).expanduser().resolve()
                    allowed_root = Path(os.getenv("NANOBOT_DATA_DIR", str(Path.home() / ".nanobot"))).expanduser().resolve()
                    if allowed_root not in source.parents and source != allowed_root:
                        return ToolResult.error("source must be inside the nanobot media/data directory")
                    if not source.is_file():
                        return ToolResult.error("source file does not exist")
                    if source.stat().st_size > _MAX_UPLOAD_BYTES:
                        return ToolResult.error("source file exceeds 200 MiB")
                    data = await asyncio.to_thread(source.read_bytes)
                    await asyncio.to_thread(sandbox.files.write, path, data)
                    return f"Uploaded {source.name} to {path} in the remote sandbox."
                if action == "fetch_url":
                    url = str(kwargs.get("url") or "").strip()
                    if not url:
                        return ToolResult.error("url is required for fetch_url")
                    parsed_url = urlparse(url)
                    allowed = parsed_url.scheme == "https" and (
                        parsed_url.netloc in {"tmpfiles.org", "gofile.io"}
                        or parsed_url.netloc.endswith(".gofile.io")
                    )
                    if not allowed:
                        return ToolResult.error(
                            "url must be an HTTPS tmpfiles.org or gofile.io URL"
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
                url = await asyncio.to_thread(sandbox.download_url, path, use_signature_expiration=300)
                return f"Signed download URL (expires in 5 minutes): {url}"
        except Exception as exc:
            logger.exception("Novita Sandbox operation failed")
            return ToolResult.error(f"Novita Sandbox error: {type(exc).__name__}: {str(exc)[:500]}")
