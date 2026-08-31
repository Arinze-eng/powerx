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
from uuid import uuid4

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.agent.tools.vps_backend import VPSExecutionBackend
from nanobot.config.paths import get_data_dir, get_workspace_path
from nanobot.utils.helpers import detect_image_mime
from nanobot.utils.tmpfiles import upload_bytes as upload_tmpfile_bytes  # noqa: F401 - referenced by VPS relay-skip tests
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

# A Telegram attachment may be carried through the message bus as a small
# "direct fetch" token instead of a local Render disk path.  When present, the
# active execution backend (Novita sandbox or Linux VPS) downloads the bytes
# straight from api.telegram.org, so the file never transits Render's egress
# (near-zero Render bandwidth).  Format: tgurl::<https-download-url>::<filename>
_TG_URL_PREFIX = "tgurl::"


def _encode_telegram_url_token(download_url: str, filename: str) -> str:
    """Build an opaque token that routes a Telegram file to a direct backend fetch."""
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:120] or "attachment.bin"
    return f"{_TG_URL_PREFIX}{download_url}::{safe_name}"


def _decode_telegram_url_token(token: str) -> tuple[str, str] | None:
    """Return (download_url, safe_name) when *token* is a direct-fetch token."""
    if not token.startswith(_TG_URL_PREFIX):
        return None
    body = token[len(_TG_URL_PREFIX):]
    if "::" not in body:
        return None
    url, safe_name = body.split("::", 1)
    if not url.startswith("https://api.telegram.org/") or not safe_name:
        return None
    return url, safe_name


def _is_telegram_url_token(value: str) -> bool:
    return _decode_telegram_url_token(value) is not None

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
                return (
                    f"File: {Path(path).name}\n"
                    "Tesseract is unavailable on this execution backend; "
                    "an administrator must install tesseract-ocr before image OCR can run."
                )
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

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handles: dict[str, Any] = {}
        self._ids: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        path = os.getenv("NANOBOT_DATA_DIR", "").strip()
        self._index_path = Path(path).expanduser() / "novita_sandboxes.json" if path else Path.home() / ".nanobot" / "novita_sandboxes.json"
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._ids = {str(k): str(v) for k, v in raw.items() if v}
        except (OSError, ValueError):
            pass

    def lock_for(self, key: str) -> asyncio.Lock:
        with self._lock:
            return self._locks.setdefault(key, asyncio.Lock())

    def get(self, key: str) -> Any | None:
        with self._lock:
            return self._handles.get(key)

    def set(self, key: str, sandbox: Any) -> None:
        sandbox_id = str(getattr(sandbox, "sandbox_id", "") or getattr(sandbox, "id", ""))
        with self._lock:
            self._handles[key] = sandbox
            if sandbox_id:
                self._ids[key] = sandbox_id
                try:
                    self._index_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = self._index_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(self._ids, indent=2), encoding="utf-8")
                    tmp.replace(self._index_path)
                except OSError:
                    logger.warning("Could not persist Novita sandbox index")

    def sandbox_id(self, key: str) -> str | None:
        with self._lock:
            return self._ids.get(key)

    def remove(self, key: str) -> None:
        with self._lock:
            self._handles.pop(key, None)
            self._ids.pop(key, None)
            try:
                self._index_path.write_text(json.dumps(self._ids, indent=2), encoding="utf-8")
            except OSError:
                pass


_STORE = _SandboxStore()


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


@tool_parameters(
    tool_parameters_schema(
        required=["action"],
        additional_properties=None,
        action=StringSchema(
            "Operation: run, read, write, upload, list, download_url, or reset",
            enum=["run", "read", "write", "upload", "list", "download_url", "reset"],
        ),
        command=StringSchema("Command to run inside the remote sandbox"),
        path=StringSchema("Sandbox path, relative paths resolve under /workspace"),
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
        if execution is not None and getattr(execution, "backend", "novita") == "vps":
            return bool(getattr(execution.vps, "host", "").strip())
        return bool(os.getenv("NOVITA_API_KEY", "").strip()) and Novita is not None

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls()

    def _selected_backend(self) -> tuple[str, Any | None]:
        execution = self._execution_config()
        if execution is None or getattr(execution, "backend", "novita") != "vps":
            return "novita", None
        return "vps", getattr(execution, "vps", None)

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
            "download generated artifacts, or reset the current user sandbox. "
            "Use this for all coding, tests, builds, package installs, Git, and CI/CD work; "
            "never use the host shell for user work. "
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
            "tool's media parameter when direct attachment delivery is available."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["run", "read", "write", "upload", "install", "list", "download_url", "reset"]},
                "command": {"type": "string"},
                "packages": {"type": "string", "description": "Space-separated Linux distro package names to install in VPS mode."},
                "path": {"type": "string"},
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
                # Skip the tmpfiles.org relay — SFTP upload delivers the same
                # bytes directly, so relay would send every file out of Render
                # twice (once upload, once cron re-pull).
                await backend.upload("telegram", remote_path, raw)
            await backend.write(manifest_path, json.dumps(remote_paths))
            output = await backend.run(
                "env NANOBOT_OCR_ALLOW_INSTALL=0 NANOBOT_OCR_ALLOW_PILLOW_INSTALL=0 "
                "NANOBOT_OCR_TIMEOUT_SECONDS=20 "
                f"python3 {shlex.quote(script_path)} {shlex.quote(manifest_path)}",
                timeout=45,
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

    async def _analyze_telegram_images_vps_from_urls(
        self,
        tokens: list[str],
        *,
        config: Any,
    ) -> str:
        """OCR Telegram images whose bytes were fetched directly from Telegram.

        Each *tokens* entry is a ``tgurl::`` direct-fetch token. The VPS runs
        ``curl`` to pull the bytes from api.telegram.org, so the files never
        transit Render (near-zero Render egress). Local-path images are not
        mixed in here; see ``_analyze_telegram_images_vps`` for those.
        """
        backend = VPSExecutionBackend(config)
        root = str(config.workspace_dir or _WORKSPACE).rstrip("/") or "/workspace"
        ocr_dir = f"{root}/.nanobot"
        remote_paths: list[str] = []
        manifest_path = f"{ocr_dir}/telegram_image_manifest.json"
        script_path = f"{ocr_dir}/telegram_image_ocr.py"
        try:
            img_dir = f"{root}/telegram-images"
            await backend.run(
                f"mkdir -p {shlex.quote(ocr_dir)} {shlex.quote(img_dir)}",
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
            for raw_token in tokens[:_MAX_TELEGRAM_IMAGE_COUNT]:
                decoded = _decode_telegram_url_token(str(raw_token))
                if decoded is None:
                    continue
                download_url, safe_name = decoded
                if not (mimetypes.guess_type(safe_name)[0] or "").startswith("image/"):
                    continue
                suffix = Path(safe_name).suffix.lower() or ".img"
                remote_path = f"{root}/telegram-images/{uuid4().hex}{suffix}"
                remote_paths.append(remote_path)
                await backend.fetch_telegram_url(download_url, remote_path)
            if not remote_paths:
                return "[No readable Telegram images were available to the VPS.]"
            await backend.write(manifest_path, json.dumps(remote_paths))
            output = await backend.run(
                "env NANOBOT_OCR_ALLOW_INSTALL=0 NANOBOT_OCR_ALLOW_PILLOW_INSTALL=0 "
                "NANOBOT_OCR_TIMEOUT_SECONDS=20 "
                f"python3 {shlex.quote(script_path)} {shlex.quote(manifest_path)}",
                timeout=45,
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
            logger.warning("VPS Tesseract OCR (direct-fetch) failed: {}", type(exc).__name__)
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
        selected_backend, vps_config = self._selected_backend()
        if selected_backend == "vps":
            if vps_config is None or not str(vps_config.host or "").strip():
                return "[VPS execution is selected but SSH details are not configured.]"
            local_images: list[tuple[Path, bytes]] = []
            # Direct-fetch tokens let the VPS pull the file from Telegram instead
            # of Render; classify them by filename MIME so they route to image OCR.
            for raw_path in image_paths[:_MAX_TELEGRAM_IMAGE_COUNT]:
                token = _decode_telegram_url_token(str(raw_path)) if _is_telegram_url_token(str(raw_path)) else None
                if token is not None:
                    url, safe_name = token
                    guessed = mimetypes.guess_type(safe_name)[0]
                    if guessed and guessed.startswith("image/"):
                        local_images.append((Path(safe_name), b""))  # placeholder, resolved below
                    continue
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
            # If any entry is a direct-fetch token, fetch every image on the VPS
            # from Telegram and run OCR there, so no bytes transit Render.
            needs_direct_fetch = any(
                _is_telegram_url_token(str(raw)) and _decode_telegram_url_token(str(raw))[0].startswith("https://api.telegram.org/")
                for raw in image_paths[:_MAX_TELEGRAM_IMAGE_COUNT]
            )
            if needs_direct_fetch:
                return await self._analyze_telegram_images_vps_from_urls(
                    image_paths[:_MAX_TELEGRAM_IMAGE_COUNT],
                    config=vps_config,
                )
            return await self._analyze_telegram_images_vps(local_images, config=vps_config)
        if Novita is None:
            return "[Novita Sandbox Tesseract OCR is unavailable in this deployment; the sandbox will install it on first use.]"
        api_key = os.getenv("NOVITA_API_KEY", "").strip()
        if not api_key:
            return "[Novita Sandbox OCR is not configured in this deployment.]"
        if len(image_paths) > _MAX_TELEGRAM_IMAGE_COUNT:
            image_paths = image_paths[:_MAX_TELEGRAM_IMAGE_COUNT]

        # Direct-fetch tokens let the Novita sandbox pull the image from
        # api.telegram.org directly instead of reading local Render bytes.
        token_map: dict[str, tuple[str, str]] = {}
        local_images: list[tuple[Path, bytes]] = []
        for raw_path in image_paths:
            decoded = (_decode_telegram_url_token(str(raw_path))
                       if _is_telegram_url_token(str(raw_path)) else None)
            if decoded is not None:
                url, safe_name = decoded
                if (mimetypes.guess_type(safe_name)[0] or "").startswith("image/"):
                    token_map[str(raw_path)] = (url, safe_name)
                continue
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
        if not local_images and not token_map:
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
                # Direct-fetch entries: curl the bytes straight from Telegram
                # inside the sandbox, so nothing transits Render egress.
                for _raw_token, (url, safe_name) in token_map.items():
                    suffix = Path(safe_name).suffix.lower() or ".img"
                    remote_path = f"{_WORKSPACE}/telegram-images/{uuid4().hex}{suffix}"
                    remote_paths.append(remote_path)
                    await asyncio.to_thread(
                        sandbox.commands.run,
                        f"curl -fsSL --retry 2 --max-time 180 {shlex.quote(url)} -o {shlex.quote(remote_path)} "
                        f"&& test -s {shlex.quote(remote_path)}",
                        cwd=_WORKSPACE,
                        timeout=200,
                        request_timeout=210,
                    )
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

    def _get_or_create(self, key: str) -> Any:
        sandbox = _STORE.get(key)
        if sandbox is not None:
            try:
                if sandbox.is_running():
                    return sandbox
            except Exception:
                pass
        client = self._client()
        sandbox_id = _STORE.sandbox_id(key)
        if sandbox_id:
            try:
                sandbox = client.sandbox.connect(sandbox_id)
                if sandbox.is_running():
                    _STORE.set(key, sandbox)
                    return sandbox
            except Exception:
                _STORE.remove(key)
        sandbox = client.sandbox.create(
            "base",
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
        _STORE.set(key, sandbox)
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
        """Stage confirmed Telegram files into the active VPS workspace.

        This is deliberately a VPS-only pre-step. Novita keeps its established
        model-driven upload flow, while VPS turns confirmed Telegram attachments
        into deterministic remote paths before the model turn is built.

        When a media entry is a ``tgurl::`` direct-fetch token, the VPS fetches
        the bytes straight from api.telegram.org (no Render egress). Local paths
        fall back to a direct SFTP upload of the on-disk bytes.
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
            token = _decode_telegram_url_token(str(raw_path)) if _is_telegram_url_token(str(raw_path)) else None
            if token is not None:
                download_url, safe_name = token
                remote = f"{root}/telegram-attachments/{uuid4().hex}-{safe_name}"
                # The VPS pulls the bytes from api.telegram.org directly, so the
                # file never transits Render (near-zero Render bandwidth).
                await backend.fetch_telegram_url(download_url, remote)
                staged.append((download_url, remote))
                continue
            source = Path(str(raw_path)).expanduser().resolve()
            if not self._local_attachment_allowed(source):
                raise ValueError("Telegram attachment is outside the nanobot media directory")
            if not source.is_file():
                raise FileNotFoundError(source.name)
            if source.stat().st_size > _MAX_UPLOAD_BYTES:
                raise ValueError("Telegram attachment exceeds 200 MiB")
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", source.name)[:120] or "attachment.bin"
            remote = f"{root}/telegram-attachments/{uuid4().hex}-{safe_name}"
            # Skip the tmpfiles.org relay upload — the SFTP upload
            # below delivers the same bytes directly, so the relay
            # was sending every file out of Render twice.
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
                await backend.write(path, content)
                return f"Wrote {len(content)} characters to {path} in the remote VPS workspace."
            if action == "upload":
                source_raw = str(kwargs.get("source") or "")
                source_path = Path(source_raw).expanduser().resolve()
                token = _decode_telegram_url_token(source_raw) if _is_telegram_url_token(source_raw) else None
                if token is not None:
                    # Direct-fetch token: the VPS pulls the bytes from Telegram
                    # itself, so no Render egress for the payload.
                    url, safe_name = token
                    remote_dest = str(path or "").strip()
                    await backend.fetch_telegram_url(url, remote_dest or f"{root}/{safe_name}")
                    return f"Uploaded {safe_name} directly from Telegram to {path} in the remote VPS workspace."
                source = source_path
                if not self._local_attachment_allowed(source):
                    return ToolResult.error("source must be inside the nanobot media/data directory")
                if not source.is_file():
                    return ToolResult.error("source file does not exist")
                if source.stat().st_size > _MAX_UPLOAD_BYTES:
                    return ToolResult.error("source file exceeds 200 MiB")
                # Skip the tmpfiles.org relay — SFTP upload is
                # available and delivers the bytes directly.
                await backend.upload(str(source), path, await asyncio.to_thread(source.read_bytes))
                return f"Uploaded {source.name} to {path} in the remote VPS workspace."
            if action == "list":
                return await backend.list(path)
            if action == "download_url":
                destination = self._artifact_destination(path)
                downloaded = await backend.download(path, destination)
                tmpfile = await upload_tmpfile_path(downloaded)
                return (
                    f"Downloaded remote artifact to local path: {downloaded}. "
                    f"A temporary public download link is also available: {tmpfile['download_url']}. "
                    "Use the message tool with the local path in media for direct attachment "
                    "delivery, or provide the temporary link when a URL is preferred."
                )
            return ToolResult.error("Unknown sandbox action")
        except Exception as exc:
            logger.exception("VPS execution operation failed")
            return ToolResult.error(f"VPS execution error: {type(exc).__name__}: {str(exc)[:500]}")

    async def execute(self, **kwargs: Any) -> ToolResult | str:
        action = str(kwargs.get("action", "")).strip().lower()
        selected_backend, vps_config = self._selected_backend()
        if selected_backend == "vps":
            if vps_config is None or not str(vps_config.host or "").strip():
                return ToolResult.error("VPS execution is selected but SSH details are not configured")
            return await self._execute_vps(action, kwargs, vps_config)
        key = _session_key()
        try:
            if action == "reset":
                async with _STORE.lock_for(key):
                    sandbox = _STORE.get(key)
                    if sandbox is not None:
                        await asyncio.to_thread(sandbox.kill)
                    _STORE.remove(key)
                return "Remote Novita Sandbox reset. A new one will be created for the next operation."
            if action not in {"run", "read", "write", "upload", "list", "download_url"}:
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
                    return str(content)[-_MAX_RESULT_CHARS:]
                if action == "write":
                    content = str(kwargs.get("content") or "")
                    if len(content) > _MAX_CONTENT_CHARS:
                        return ToolResult.error(f"content exceeds {_MAX_CONTENT_CHARS} characters")
                    await asyncio.to_thread(sandbox.files.write, path, content)
                    return f"Wrote {len(content)} characters to {path} in the remote sandbox."
                if action == "upload":
                    source_raw = str(kwargs.get("source") or "")
                    token = _decode_telegram_url_token(source_raw) if _is_telegram_url_token(source_raw) else None
                    if token is not None:
                        # Direct-fetch: the sandbox curls the bytes from Telegram
                        # itself, so nothing transits Render egress.
                        url, safe_name = token
                        await asyncio.to_thread(
                            sandbox.commands.run,
                            f"curl -fsSL --retry 2 --max-time 180 {shlex.quote(url)} -o {shlex.quote(path)} "
                            f"&& test -s {shlex.quote(path)}",
                            cwd=_WORKSPACE,
                            timeout=200,
                            request_timeout=210,
                        )
                        return f"Uploaded {safe_name} directly from Telegram to {path} in the remote sandbox."
                    source = Path(source_raw).expanduser().resolve()
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
