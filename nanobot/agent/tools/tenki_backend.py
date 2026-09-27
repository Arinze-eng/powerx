"""Run the sandbox tool contract on an administrator-configured Tenki Sandbox.

Tenki (https://tenki.cloud) provides disposable full Linux VMs for coding agents
and untrusted code. Unlike the Daytona/Runloop/Vercel backends (which speak each
vendor's REST API over ``aiohttp``) Tenki's control plane is raw gRPC, so this
backend drives the vendor's own Python SDK instead of re-implementing protobuf
framing by hand. Everything else mirrors the shared sandbox contract exactly.

SDK surface used (``tenki_sandbox.AsyncClient`` / ``AsyncSandbox``):
* ``AsyncClient(auth_token=…, base_url=…)``      → workspace-scoped client
* ``client.create(name=…, cpu_cores=…, memory_mb=…, disk_size_gb=…,
   max_duration=…, sticky=…, tags=…, metadata=…, wait=True)`` → new session
* ``client.get(session_id)``                    → reattach to a live session
* ``client.list(tags=…, include_terminated=False)`` → resolve a session by name
* ``sandbox.wait_ready(timeout)``               → block until ``RUNNING``
* ``sandbox.shell(command, timeout=…)``         → run a command through bash -lc
* ``sandbox.fs.read_text/write_text/write_bytes/read_bytes/stat/mkdir``
* ``sandbox.fs.download(remote, local)``        → stream a file to the host
* ``sandbox.extend(seconds)``                   → push the deadline out
* ``sandbox.close()``                           → terminate (permanent)

Lifecycle: user sessions map to a deterministic session name so a VM survives
agent restarts. ``max_duration`` (seconds) is the TTL — the default is one hour
and it is administrator-configurable — and Tenki's own deadline reaps the VM if
cleanup is ever missed.

Resource limits are enforced by the *workspace*, not by the SDK: this tenant's
ceiling was measured at 4096 MB of RAM, so ``memory_mb`` defaults to 4096 and a
larger value is rejected server-side with
``InvalidStateError: requested resources exceed workspace limits``. Raise the
workspace quota before raising the default.
"""

from __future__ import annotations

import base64
import hashlib
import posixpath
import re
import shlex
from contextlib import asynccontextmanager, suppress
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from loguru import logger

_MAX_COMMAND_CHARS = 12_000
_MAX_CONTENT_CHARS = 120_000
_MAX_RESULT_CHARS = 16_000
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
_MAX_TIMEOUT = 900

# Tenki sessions run as the unprivileged ``tenki`` account (uid 1000) with
# ``/home/tenki`` as its home directory — verified live against the SDK.
WORKSPACE = "/home/tenki"

DEFAULT_API_URL = "https://api.tenki.cloud"

# Session names are used verbatim by the control plane; keep them to a DNS-label
# safe alphabet so name-derived routing never trips a server-side validator.
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")

# Bounds accepted by the SDK's own ``validate_create_resources``. The workspace
# quota (not these numbers) is what actually caps a create.
_MIN_CPU_CORES = 1
_MAX_CPU_CORES = 128
_MIN_MEMORY_MB = 128
_MAX_MEMORY_MB = 524_288
_MIN_DISK_GB = 5
_MAX_DISK_GB = 100

# States from which a session can still be used. Anything else means the VM is
# gone and must be recreated (Tenki has no resume-from-terminated path).
_DEAD_STATES = frozenset({"TERMINATED", "TERMINATING", "FAILED", "ERROR"})
_TERMINAL_STATES = _DEAD_STATES

# Default allowed hosts for ``fetch_url`` (kept in sync with the Daytona/Runloop
# backends so every cloud provider behaves identically for the agent).
DEFAULT_FETCH_ALLOW_HOSTS: tuple[str, ...] = (
    "onlyfiles.com",
    "gofile.io",
    "*.gofile.io",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
    "files.pythonhosted.org",
    "pypi.org",
    "registry.npmjs.org",
    "filebin.net",
    "0x0.st",
    "transfer.sh",
    "bashupload.com",
    "temp.sh",
    "pixeldrain.com",
    "*.pixeldrain.com",
    "catbox.moe",
    "litterbox.catbox.moe",
    "file.io",
    "cdn.jsdelivr.net",
    "unpkg.com",
    "esm.sh",
    "dl.google.com",
    "storage.googleapis.com",
    "registry.yarnpkg.com",
    "nodejs.org",
)


class TenkiError(RuntimeError):
    """Any non-recoverable Tenki platform or session failure."""


class TenkiFileNotFoundError(TenkiError):
    """The requested path does not exist in the session."""


def _load_async_client() -> Any:
    """Import the Tenki SDK lazily so the tool stays importable without it."""
    try:
        from tenki import AsyncClient  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on the deployment image
        raise TenkiError(
            "The Tenki SDK is not installed. Install it with `pip install tenki` "
            "in the nanobot environment, then retry."
        ) from exc
    return AsyncClient


def is_sdk_available() -> bool:
    """True when the Tenki SDK can be imported (used by the admin test path)."""
    try:
        _load_async_client()
    except TenkiError:
        return False
    return True


def validate_tenki_api_key(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 256 or not re.fullmatch(r"[A-Za-z0-9_\-\.]+", value):
        raise ValueError("Tenki API key format is invalid")
    if not value.startswith("tk_"):
        raise ValueError("Tenki API key must start with tk_")
    return value


def validate_tenki_api_url(raw: str) -> str:
    value = str(raw or "").strip().rstrip("/") or DEFAULT_API_URL
    if len(value) > 253:
        raise ValueError("Tenki API URL is too long")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(
            "Tenki API URL must be an https URL (for example https://api.tenki.cloud)"
        )
    return value


def _validate_int(raw: Any, *, field: str, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Tenki {field} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"Tenki {field} must be between {minimum} and {maximum}")
    return value


def validate_tenki_cpu_cores(raw: Any) -> int:
    return _validate_int(
        raw, field="CPU cores", minimum=_MIN_CPU_CORES, maximum=_MAX_CPU_CORES
    )


def validate_tenki_memory_mb(raw: Any) -> int:
    value = _validate_int(
        raw, field="memory (MB)", minimum=_MIN_MEMORY_MB, maximum=_MAX_MEMORY_MB
    )
    if value % 2 != 0:
        raise ValueError("Tenki memory (MB) must be an even number of megabytes")
    return value


def validate_tenki_disk_size_gb(raw: Any) -> int:
    """0 means "let Tenki pick the source's default disk"; otherwise 5-100 GB."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Tenki disk size must be an integer number of GB") from None
    if value == 0:
        return 0
    if not _MIN_DISK_GB <= value <= _MAX_DISK_GB:
        raise ValueError(
            f"Tenki disk size must be 0 (provider default) or between "
            f"{_MIN_DISK_GB} and {_MAX_DISK_GB} GB"
        )
    return value


def validate_tenki_max_duration_seconds(raw: Any) -> int:
    """"The session TTL in seconds. One hour by default."""
    return _validate_int(
        raw, field="max duration (seconds)", minimum=60, maximum=604_800
    )


def validate_tenki_image(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 512 or not re.fullmatch(r"[A-Za-z0-9_\-\./:@]+", value):
        raise ValueError("Tenki image reference format is invalid")
    return value


def validate_tenki_snapshot_id(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9_\-\.]+", value):
        raise ValueError("Tenki snapshot ID format is invalid")
    return value


def validate_tenki_tag(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return ""
    if len(value) > 64 or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value):
        raise ValueError("Tenki tag format is invalid")
    return value


def validate_tenki_fetch_allow_hosts(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value == "*":
        return "*"
    parts = [p.strip() for p in value.split(",") if p.strip()]
    for p in parts:
        if p != "*" and not re.fullmatch(r"(\*\.)?[a-zA-Z0-9][-a-zA-Z0-9.]*[a-zA-Z0-9]", p):
            raise ValueError(f"Invalid host in fetch allowlist: {p!r}")
    return ",".join(parts)


def tenki_sandbox_name(session_key: str) -> str:
    """Deterministic, valid session name for a session so VMs survive restarts."""
    slug = re.sub(r"[^a-z0-9-]", "-", (session_key or "").lower()).strip("-")[:32] or "session"
    digest = hashlib.sha256((session_key or "session").encode("utf-8")).hexdigest()[:10]
    name = f"px-{slug}-{digest}".strip("-")
    while "--" in name:
        name = name.replace("--", "-")
    return name[:48] if _NAME_RE.fullmatch(name) else f"px-{digest}"


def _safe_path(raw: str, root: str = WORKSPACE) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("path is required")
    root_path = PurePosixPath(root).as_posix().rstrip("/") or "/"
    # The LLM frequently asks to "list /" to see the sandbox root. From its
    # perspective that means the sandbox workspace, not the VM's real filesystem
    # root. Map a bare "/" to the workspace root so it does not raise. All other
    # paths outside the workspace remain rejected (secure).
    if value == "/":
        return root_path
    candidate = value if value.startswith("/") else f"{root_path}/{value}"
    normalized = posixpath.normpath(candidate)
    if normalized != root_path and not normalized.startswith(root_path + "/"):
        raise ValueError(f"path must remain inside {root_path}")
    return normalized


def _truncate(text: str) -> str:
    return text[-_MAX_RESULT_CHARS:] if len(text) > _MAX_RESULT_CHARS else text


def _looks_like_missing_file(detail: str) -> bool:
    lowered = detail.lower()
    return "not found" in lowered or "no such file" in lowered or "does not exist" in lowered


class TenkiExecutionBackend:
    """Async client implementing the shared sandbox contract against Tenki."""

    def __init__(self, config: Any, *, sandbox_name: str = "powerx-session") -> None:
        self.config = config
        self.api_url = validate_tenki_api_url(str(getattr(config, "api_url", "") or ""))
        self.api_key = validate_tenki_api_key(str(getattr(config, "api_key", "") or ""))
        self.image = validate_tenki_image(str(getattr(config, "image", "") or ""))
        self.snapshot_id = validate_tenki_snapshot_id(
            str(getattr(config, "snapshot_id", "") or "")
        )
        self.cpu_cores = validate_tenki_cpu_cores(
            getattr(config, "cpu_cores", 2) or 2
        )
        self.memory_mb = validate_tenki_memory_mb(
            getattr(config, "memory_mb", 4096) or 4096
        )
        self.disk_size_gb = validate_tenki_disk_size_gb(
            getattr(config, "disk_size_gb", 0) or 0
        )
        # The session TTL. One hour by default and administrator-configurable;
        # Tenki's own deadline is the backstop that reaps an abandoned VM.
        self.max_duration_seconds = validate_tenki_max_duration_seconds(
            getattr(config, "max_duration_seconds", 3600) or 3600
        )
        self.tag = validate_tenki_tag(str(getattr(config, "tag", "") or "powerx")) or "powerx"
        self.fetch_allow_hosts = validate_tenki_fetch_allow_hosts(
            str(getattr(config, "fetch_allow_hosts", "") or "")
        )
        self._fetch_hosts: set[str] = (
            {h.strip() for h in self.fetch_allow_hosts.split(",") if h.strip()}
            if self.fetch_allow_hosts
            else set(DEFAULT_FETCH_ALLOW_HOSTS)
        )
        self.sandbox_name = (
            sandbox_name if _NAME_RE.fullmatch(sandbox_name) else "powerx-session"
        )
        self.workspace = WORKSPACE
        self.last_session_id: str = ""
        # "Perfect sandbox" persistence (mirrors the Upstash/Daytona/Runloop
        # design): with persistence on, a finished task leaves the VM alive and
        # its disk intact, so files survive across tasks, agent restarts and
        # platform redeploys; the VM is reattached by name and only its max
        # duration ever reaps it. With persistence off, a finished task
        # terminates the VM instead.
        self.persist_workspace = bool(getattr(config, "persist_workspace", True))

    # ------------------------------------------------------------- lifecycle

    def _new_client(self) -> Any:
        client_cls = _load_async_client()
        if not self.api_key:
            raise TenkiError("Tenki API key is not configured")
        try:
            return client_cls(auth_token=self.api_key, base_url=self.api_url)
        except Exception as exc:  # noqa: BLE001 - SDK raises its own error types
            raise TenkiError(f"could not create the Tenki client: {_detail(exc)}") from None

    @staticmethod
    def _state(sandbox: Any) -> str:
        try:
            info = sandbox.info
        except Exception:  # noqa: BLE001
            return ""
        return str(getattr(info, "state", "") or getattr(sandbox, "state", "") or "").upper()

    def _usable(self, sandbox: Any) -> bool:
        return self._state(sandbox) not in _DEAD_STATES

    async def _ready(self, sandbox: Any, *, timeout: float = 240) -> Any:
        """Block until the session is ``RUNNING`` (a fresh VM can take a while)."""
        if self._state(sandbox) == "RUNNING":
            return sandbox
        try:
            await sandbox.wait_ready(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - SDK raises WaitReadyFailedError et al.
            if self._state(sandbox) in _DEAD_STATES:
                raise TenkiError(
                    f"Tenki session {sandbox.id} entered terminal state: {_detail(exc)}"
                ) from None
            raise TenkiError(
                f"Tenki session {sandbox.id} did not become ready: {_detail(exc)}"
            ) from None
        return sandbox

    async def _find_session(self, client: Any) -> Any | None:
        """Resolve this session's VM by its deterministic name + tag."""
        for kwargs in ({"tags": [self.tag]}, {}):
            try:
                sessions = await client.list(include_terminated=False, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Tenki list failed: {}", _detail(exc))
                continue
            for item in sessions or []:
                try:
                    if str(item.name or "") != self.sandbox_name:
                        continue
                except Exception:  # noqa: BLE001
                    continue
                if self._usable(item):
                    return item
        return None

    async def _attach_only(self, client: Any, session_id: str | None = None) -> Any | None:
        """Resolve an ALREADY EXISTING session without ever creating one.

        Task-end lifecycle hooks (keep-alive, reset) must never spin up a fresh
        VM just to touch it, so they resolve rather than ensure.
        """
        target = session_id or self.last_session_id
        if target:
            with suppress(Exception):
                sandbox = await client.get(target)
                if sandbox is not None and self._usable(sandbox):
                    return sandbox
        return await self._find_session(client)

    async def _ensure_session(self, client: Any) -> Any:
        """Get or create this session's Tenki Sandbox."""
        if self.last_session_id:
            sandbox: Any | None = None
            try:
                sandbox = await client.get(self.last_session_id)
            except Exception as exc:  # noqa: BLE001 - a stale id is expected
                logger.debug("Tenki get({}) failed: {}", self.last_session_id, _detail(exc))
            if sandbox is not None and self._usable(sandbox):
                await self._ready(sandbox, timeout=180)
                self.last_session_id = str(sandbox.id)
                return sandbox
            self.last_session_id = ""

        existing = await self._find_session(client)
        if existing is not None:
            try:
                await self._ready(existing, timeout=180)
                self.last_session_id = str(existing.id)
                return existing
            except TenkiError:
                # A pre-existing VM that will not come up would fail every
                # operation forever; terminate it so a fresh one is created
                # instead of leaving the session wedged.
                logger.warning(
                    "reclaiming stuck Tenki session {} that did not become ready",
                    getattr(existing, "id", self.sandbox_name),
                )
                with suppress(Exception):
                    await existing.close()

        sandbox = await self._create_session(client)
        self.last_session_id = str(sandbox.id)
        return sandbox

    async def _create_session(self, client: Any) -> Any:
        kwargs: dict[str, Any] = {
            "name": self.sandbox_name,
            "cpu_cores": self.cpu_cores,
            "memory_mb": self.memory_mb,
            # ``sticky`` must be False for ``max_duration`` to be honoured:
            # sticky takes precedence over the duration and would make the VM
            # immortal, so the TTL would silently stop applying.
            "sticky": False,
            "max_duration": self.max_duration_seconds,
            "tags": [self.tag],
            "metadata": {"app": "powerx", "managed-by": "nanobot"},
            "wait": True,
        }
        if self.disk_size_gb:
            kwargs["disk_size_gb"] = self.disk_size_gb
        # Only one base image source may be supplied: an operator snapshot (exact
        # baselined state) wins over a raw image reference.
        if self.snapshot_id:
            kwargs["snapshot_id"] = self.snapshot_id
        elif self.image:
            kwargs["image"] = self.image
        try:
            return await client.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            message = _detail(exc)
            if "exceed workspace limits" in message.lower():
                raise TenkiError(
                    f"Tenki rejected the requested resources ({self.cpu_cores} vCPU / "
                    f"{self.memory_mb} MB): {message}. Lower the Tenki CPU/RAM settings "
                    "in the admin panel or raise the workspace quota."
                ) from None
            raise TenkiError(f"could not create a Tenki session: {message}") from None

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        """One operation = one SDK client, so the gRPC channel always matches
        the running event loop and a wedged channel can never be reused."""
        client = self._new_client()
        try:
            yield await self._ensure_session(client)
        finally:
            with suppress(Exception):
                await client.close()

    async def describe(self, session_id: str | None = None) -> dict[str, Any]:
        """Return live session facts (used by the admin Test button and tests)."""
        async with self._session() as sandbox:
            info = getattr(sandbox, "info", None)
            return {
                "session_id": str(sandbox.id),
                "name": str(getattr(info, "name", "") or ""),
                "state": self._state(sandbox),
                "cpu_cores": getattr(info, "cpu_cores", None),
                "memory_mb": getattr(info, "memory_mb", None),
                "disk_size_gb": getattr(info, "disk_size_gb", None),
                "timeout_at": getattr(info, "timeout_at", None),
                "workspace": self.workspace,
            }

    async def keep_alive(self, session_id: str | None = None) -> None:
        """Push the session deadline out by one full TTL window.

        Never creates a VM: with persistence on this runs at task end, where
        spinning up a fresh session would be pure waste. A missing or already
        dead session is a silent no-op.
        """
        client = self._new_client()
        try:
            sandbox = await self._attach_only(client, session_id)
            if sandbox is None:
                return
            await sandbox.extend(self.max_duration_seconds)
            self.last_session_id = str(sandbox.id)
        finally:
            with suppress(Exception):
                await client.close()

    # ------------------------------------------------------------------ exec

    @staticmethod
    def _render(result: Any) -> str:
        stdout = str(getattr(result, "stdout_text", "") or "")
        stderr = str(getattr(result, "stderr_text", "") or "")
        code = getattr(result, "exit_code", None)
        text = stdout
        if stderr:
            text += f"\n[stderr]\n{stderr}"
        if getattr(result, "timed_out", False):
            text += "\n[timed_out=true]"
        if code is not None and int(code) != 0:
            text += f"\n[exit_code={code}]"
        return _truncate(text) or "(no output)"

    async def run(self, command: str, *, timeout: int = 120) -> str:
        command = str(command or "").strip()
        if not command:
            raise ValueError("command is required")
        if len(command) > _MAX_COMMAND_CHARS:
            raise ValueError(f"command exceeds {_MAX_COMMAND_CHARS} characters")
        timeout = max(1, min(int(timeout), _MAX_TIMEOUT))
        async with self._session() as sandbox:
            try:
                result = await sandbox.shell(command, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                raise TenkiError(_detail(exc)) from None
        return self._render(result)

    async def read(self, path: str) -> str:
        target = _safe_path(path, self.workspace)
        async with self._session() as sandbox:
            try:
                raw = await sandbox.fs.read_bytes(target)
            except Exception as exc:  # noqa: BLE001
                if _looks_like_missing_file(_detail(exc)):
                    return ""
                # Fall back to a shell read for paths the file API declines.
                try:
                    return await self._read_via_exec(sandbox, target)
                except Exception:  # noqa: BLE001
                    return ""
        if raw is None:
            return ""
        try:
            return _truncate(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return _truncate(base64.b64encode(raw).decode("ascii"))

    async def _read_via_exec(self, sandbox: Any, target: str) -> str:
        result = await sandbox.shell(f"base64 {shlex.quote(target)}", timeout=90)
        payload = _strip_trailers(str(getattr(result, "stdout_text", "") or ""))
        try:
            raw = base64.b64decode(payload, validate=False)
        except Exception:  # noqa: BLE001
            return payload
        try:
            return _truncate(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return _truncate(payload)

    async def write(self, path: str, content: str) -> None:
        target = _safe_path(path, self.workspace)
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError(f"content exceeds {_MAX_CONTENT_CHARS} characters")
        async with self._session() as sandbox:
            parent = posixpath.dirname(target)
            if parent and parent != "/":
                with suppress(Exception):
                    await sandbox.fs.mkdir(parent, recursive=True)
            try:
                await sandbox.fs.write_text(target, content)
            except Exception as exc:  # noqa: BLE001
                raise TenkiError(_detail(exc)) from None

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = _safe_path(path, self.workspace)
        if len(data) > _MAX_UPLOAD_BYTES:
            raise ValueError("file exceeds 200 MiB")
        async with self._session() as sandbox:
            parent = posixpath.dirname(target)
            if parent and parent != "/":
                with suppress(Exception):
                    await sandbox.fs.mkdir(parent, recursive=True)
            try:
                await sandbox.fs.write_bytes(target, data)
            except Exception as exc:  # noqa: BLE001
                # Fallback: base64 through the exec plane for payloads the file
                # API rejects.
                b64 = base64.b64encode(data).decode("ascii")
                result = await sandbox.shell(
                    f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(target)}",
                    timeout=180,
                )
                if int(getattr(result, "exit_code", 0) or 0) != 0:
                    raise TenkiError(_detail(exc)) from None

    async def list(self, path: str) -> str:
        target = _safe_path(path or self.workspace, self.workspace)
        listing_root = target if target == self.workspace else (posixpath.dirname(target) or self.workspace)
        command = f"find {shlex.quote(listing_root)} -maxdepth 2 -printf '%y %p\\n' 2>/dev/null | head -200"
        return await self.run(command, timeout=60)

    def _is_host_allowed(self, host: str) -> bool:
        if "*" in self._fetch_hosts:
            return True
        if host in self._fetch_hosts:
            return True
        return any(
            host.endswith("." + pattern[2:])
            for pattern in self._fetch_hosts
            if pattern.startswith("*.")
        )

    async def fetch_url(self, url: str, dest_path: str, *, timeout: int = 150) -> str:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        allowed = parsed.scheme in ("https", "http") and self._is_host_allowed(host)
        if not allowed:
            raise ValueError(
                f"URL host {host!r} is not in the allowed fetch hosts list. "
                "Add it to the Tenki fetch_allow_hosts setting."
            )
        dest = _safe_path(dest_path, self.workspace)
        command = (
            f"mkdir -p {shlex.quote(posixpath.dirname(dest))} && "
            f"curl -fsSL --max-time {int(timeout)} -o {shlex.quote(dest)} {shlex.quote(url)} && "
            f"stat -c %s {shlex.quote(dest)}"
        )
        out = await self.run(command, timeout=min(timeout + 30, _MAX_TIMEOUT))
        if "[exit_code=" in out and "[exit_code=0]" not in out:
            raise TenkiError(f"remote fetch failed: {out[:300]}")
        return dest

    async def download(self, remote_path: str, local_path: Any) -> Any:
        """Download a session file to a local path."""
        target = _safe_path(remote_path, self.workspace)
        destination = Path(str(local_path)).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with self._session() as sandbox:
            # The stat is best-effort (a missing file is reported by the
            # download itself), but the size guard must NOT be swallowed: keep
            # it outside the suppress so an oversized file fails loudly.
            size = 0
            with suppress(Exception):
                stat = await sandbox.fs.stat(target)
                size = int(getattr(stat, "size", 0) or 0)
            if size > _MAX_DOWNLOAD_BYTES:
                raise TenkiError(
                    f"file exceeds the {_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MiB download limit"
                )
            try:
                await sandbox.fs.download(target, str(destination))
                return destination
            except Exception as exc:  # noqa: BLE001
                if _looks_like_missing_file(_detail(exc)):
                    raise TenkiFileNotFoundError(_detail(exc)) from None
            # Fallback via base64 exec.
            payload = await self._read_via_exec(sandbox, target)
        try:
            destination.write_bytes(base64.b64decode(payload, validate=False))
        except Exception:  # noqa: BLE001
            raise TenkiError("failed to decode downloaded artifact") from None
        return destination

    async def install_packages(self, packages: list[str], *, timeout: int = 600) -> str:
        cleaned = [
            item
            for item in packages
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.:@~=-]{0,127}", item)
        ]
        if not cleaned:
            raise ValueError("no valid package names supplied")
        quoted = " ".join(shlex.quote(item) for item in cleaned)
        command = (
            "export DEBIAN_FRONTEND=noninteractive; "
            "if command -v sudo >/dev/null 2>&1; then SUDO='sudo -n'; else SUDO=''; fi; "
            "if command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "
            + quoted
            + "; elif command -v apk >/dev/null 2>&1; then $SUDO apk add --no-cache "
            + quoted
            + "; elif command -v dnf >/dev/null 2>&1; then $SUDO dnf install -y "
            + quoted
            + "; else echo 'no supported package manager found' >&2; exit 127; fi"
        )
        return await self.run(command, timeout=min(timeout, _MAX_TIMEOUT))

    async def test_connection(self) -> dict[str, Any]:
        async with self._session() as sandbox:
            result = await sandbox.shell("uname -a", timeout=90)
            info = getattr(sandbox, "info", None)
            timeout_at = getattr(info, "timeout_at", None)
            session_id = str(sandbox.id)
            state = self._state(sandbox)
        return {
            "ok": int(getattr(result, "exit_code", 0) or 0) == 0,
            "backend": "tenki",
            "session_id": session_id,
            "platform": str(getattr(result, "stdout_text", "") or "").strip()[:200],
            "state": state,
            "cpu_cores": getattr(info, "cpu_cores", None),
            "memory_mb": getattr(info, "memory_mb", None),
            # ``SandboxInfo`` exposes the deadline, not the configured TTL, so the
            # landing TTL is verified through ``timeout_at``.
            "timeout_at": timeout_at.isoformat() if hasattr(timeout_at, "isoformat") else str(timeout_at or ""),
        }

    async def reset(self, session_id: str | None = None) -> None:
        """Terminate the session permanently (explicit wipe / persistence opt-out).

        Only an existing VM is terminated — reset must never create one.
        """
        client = self._new_client()
        try:
            sandbox = await self._attach_only(client, session_id)
            if sandbox is not None:
                with suppress(Exception):
                    await sandbox.close()
            self.last_session_id = ""
        finally:
            with suppress(Exception):
                await client.close()


def _detail(exc: BaseException) -> str:
    """One-line, always-non-empty description of an SDK error."""
    text = " ".join(str(exc).split())
    return text[:400] or type(exc).__name__


def _strip_trailers(text: str) -> str:
    return re.sub(r"\n\[(?:stderr|exit_code)=\d+\]\s*$", "", text).strip()


__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_FETCH_ALLOW_HOSTS",
    "WORKSPACE",
    "TenkiError",
    "TenkiExecutionBackend",
    "TenkiFileNotFoundError",
    "is_sdk_available",
    "tenki_sandbox_name",
    "validate_tenki_api_key",
    "validate_tenki_api_url",
    "validate_tenki_cpu_cores",
    "validate_tenki_disk_size_gb",
    "validate_tenki_fetch_allow_hosts",
    "validate_tenki_image",
    "validate_tenki_max_duration_seconds",
    "validate_tenki_memory_mb",
    "validate_tenki_snapshot_id",
    "validate_tenki_tag",
]
