"""Run the sandbox tool contract on an administrator-configured Runloop Devbox.

Runloop (https://runloop.ai) provides isolated cloud devboxes with shell +
filesystem APIs. This backend talks to Runloop's REST API directly over HTTPS
(``aiohttp`` only — no extra SDK dependency), mirroring the official
``runloop-api-client``:

Devbox lifecycle (``api_url``, e.g. ``https://api.runloop.ai``):
* ``POST   /v1/devboxes``                  → create a devbox (name, blueprint, snapshot)
* ``GET    /v1/devboxes?name=<name>``      → resolve a devbox by deterministic name
* ``GET    /v1/devboxes/{id}``             → devbox metadata (status, transitions)
* ``POST   /v1/devboxes/{id}/resume``      → resume a suspended devbox (disk preserved)
* ``POST   /v1/devboxes/{id}/keep_alive``  → extend the auto-shutdown deadline
* ``POST   /v1/devboxes/{id}/shutdown``    → permanently shut the devbox down

Execution / files (all scoped to ``/v1/devboxes/{id}``):
* ``POST   /execute_sync``            → body ``{"command": cmd}``
                                        returns ``{"stdout","stderr","exit_status"}``
* ``POST   /read_file_contents``      → body ``{"file_path": p}`` → raw file bytes
* ``POST   /write_file_contents``     → body ``{"file_path": p, "contents": text}``
* ``POST   /upload_file``             → multipart ``file`` + ``path`` (binary-safe)

Lifecycle: user sessions map to a deterministic devbox name so a devbox survives
agent restarts. Explicit release shuts the devbox down; otherwise Runloop's own
``keep_alive_time_seconds`` deadline reaps it. Suspending a devbox preserves its
disk, so a session's workspace survives both agent restarts and platform
redeploys (verified: files, including binaries, round-trip byte-for-byte across
suspend/resume).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import posixpath
import re
import shlex
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import aiohttp
from loguru import logger

_MAX_COMMAND_CHARS = 12_000
_MAX_CONTENT_CHARS = 120_000
_MAX_RESULT_CHARS = 16_000
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
_MAX_TIMEOUT = 900

# Runloop devboxes run as the unprivileged ``user`` account (uid 1000) with
# ``/home/user`` as its home directory, matching ``launch_parameters``.
WORKSPACE = "/home/user"

DEFAULT_API_URL = "https://api.runloop.ai"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

# Resource sizes accepted by ``launch_parameters.resource_size_request``.
RESOURCE_SIZES: tuple[str, ...] = (
    "X_SMALL",
    "SMALL",
    "MEDIUM",
    "LARGE",
    "X_LARGE",
    "XX_LARGE",
)

# Architectures accepted by ``launch_parameters.architecture``.
_ARCHITECTURES = ("x86_64", "arm64")

# Devbox statuses that mean "ready to run commands".
_READY_STATES = frozenset({"running"})

# Statuses that are still on their way up. A devbox in one of these states will
# eventually become ``running``; ``suspended`` additionally accepts an explicit
# resume call to bring the disk back up.
_TRANSIENT_STATES = frozenset(
    {"provisioning", "initializing", "storage_provisioning", "starting", "resuming"}
)
_RESUME_STATES = frozenset({"suspended", "suspending"})
# Nothing here can be revived: a shutdown/failed devbox owns no usable disk and
# must be recreated (Runloop cannot resume a shutdown devbox).
_TERMINAL_STATES = frozenset({"shutdown", "shutting_down", "failure", "failed", "error"})

# Default allowed hosts for ``fetch_url`` (kept in sync with the Daytona backend
# so the two cloud providers behave identically for the agent).
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


class RunloopError(RuntimeError):
    """Any non-recoverable Runloop platform or devbox failure."""


class RunloopFileNotFound(RunloopError):
    """The requested path does not exist in the devbox."""


def _looks_like_missing_file(detail: str) -> bool:
    lowered = detail.lower()
    return "not found" in lowered or "no such file" in lowered or "404" in lowered


def validate_runloop_api_key(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 256 or not re.fullmatch(r"[A-Za-z0-9_\-\.]+", value):
        raise ValueError("Runloop API key format is invalid")
    return value


def validate_runloop_api_url(raw: str) -> str:
    value = str(raw or "").strip().rstrip("/") or DEFAULT_API_URL
    if len(value) > 253:
        raise ValueError("Runloop API URL is too long")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Runloop API URL must be an https URL (for example https://api.runloop.ai)")
    return value


def validate_runloop_resource_size(raw: str) -> str:
    value = str(raw or "").strip().upper() or "SMALL"
    if value not in RESOURCE_SIZES:
        raise ValueError(
            "Runloop resource size must be one of " + ", ".join(RESOURCE_SIZES)
        )
    return value


def validate_runloop_architecture(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return ""
    if value not in _ARCHITECTURES:
        raise ValueError("Runloop architecture must be x86_64 or arm64")
    return value


def validate_runloop_blueprint(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9_\-\./ ]+", value):
        raise ValueError("Runloop blueprint name format is invalid")
    return value


def validate_runloop_snapshot_id(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9_\-\.]+", value):
        raise ValueError("Runloop snapshot ID format is invalid")
    return value


def validate_runloop_keep_alive_seconds(raw: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Runloop keep-alive must be an integer number of seconds") from None
    if not 60 <= value <= 604_800:
        raise ValueError("Runloop keep-alive must be between 60 and 604800 seconds")
    return value


def validate_runloop_fetch_allow_hosts(raw: str) -> str:
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


def runloop_devbox_name(session_key: str) -> str:
    """Deterministic, valid devbox name for a session so devboxes survive restarts."""
    slug = re.sub(r"[^a-z0-9-]", "-", (session_key or "").lower()).strip("-")[:32] or "session"
    digest = hashlib.sha256((session_key or "session").encode("utf-8")).hexdigest()[:10]
    name = f"px-{slug}-{digest}".strip("-").replace("--", "-")
    return name[:48] if _NAME_RE.fullmatch(name) else f"px-{digest}"


def _safe_path(raw: str, root: str = WORKSPACE) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("path is required")
    root_path = PurePosixPath(root).as_posix().rstrip("/") or "/"
    # The LLM frequently asks to "list /" to see the sandbox root. From its
    # perspective that means the sandbox workspace, not the container's real
    # filesystem root. Map a bare "/" to the workspace root so it does not
    # raise. All other paths outside the workspace remain rejected (secure).
    if value == "/":
        return root_path
    candidate = value if value.startswith("/") else f"{root_path}/{value}"
    normalized = posixpath.normpath(candidate)
    if normalized != root_path and not normalized.startswith(root_path + "/"):
        raise ValueError(f"path must remain inside {root_path}")
    return normalized


def _truncate(text: str) -> str:
    return text[-_MAX_RESULT_CHARS:] if len(text) > _MAX_RESULT_CHARS else text


class RunloopExecutionBackend:
    """Async client implementing the shared sandbox contract against Runloop."""

    def __init__(self, config: Any, *, devbox_name: str = "powerx-session") -> None:
        self.config = config
        self.api_url = validate_runloop_api_url(str(getattr(config, "api_url", "") or ""))
        self.api_key = validate_runloop_api_key(str(getattr(config, "api_key", "") or ""))
        self.blueprint = validate_runloop_blueprint(
            str(getattr(config, "blueprint", "") or "")
        )
        self.snapshot_id = validate_runloop_snapshot_id(
            str(getattr(config, "snapshot_id", "") or "")
        )
        self.resource_size = validate_runloop_resource_size(
            str(getattr(config, "resource_size", "") or "SMALL")
        )
        self.architecture = validate_runloop_architecture(
            str(getattr(config, "architecture", "") or "")
        )
        self.keep_alive_seconds = validate_runloop_keep_alive_seconds(
            int(getattr(config, "keep_alive_seconds", 3600) or 3600)
        )
        self.fetch_allow_hosts = validate_runloop_fetch_allow_hosts(
            str(getattr(config, "fetch_allow_hosts", "") or "")
        )
        self._fetch_hosts: set[str] = (
            {h.strip() for h in self.fetch_allow_hosts.split(",") if h.strip()}
            if self.fetch_allow_hosts
            else set(DEFAULT_FETCH_ALLOW_HOSTS)
        )
        self.devbox_name = devbox_name if _NAME_RE.fullmatch(devbox_name) else "powerx-session"
        self.workspace = WORKSPACE
        self.last_devbox_id: str = ""
        # "Perfect sandbox" persistence (mirrors the Upstash/Daytona design):
        # with persistence on, a finished task leaves the devbox alive and its
        # disk intact, so files survive across tasks, agent restarts, and
        # platform redeploys; the devbox is revived with ``resume`` and only its
        # keep-alive deadline ever reaps it. With persistence off, a finished
        # task shuts the devbox down instead.
        self.persist_workspace = bool(getattr(config, "persist_workspace", True))

    # ------------------------------------------------------------------ HTTP

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise RunloopError("Runloop API key is not configured")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    async def _request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        data: Any | None = None,
        timeout: int = 120,
        raw_response: bool = False,
    ) -> Any:
        url = f"{self.api_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(),
                json=body,
                data=data,
                timeout=aiohttp.ClientTimeout(total=timeout + 30),
            ) as resp:
                if raw_response:
                    payload = await resp.read()
                    if resp.status == 404:
                        raise RunloopFileNotFound(f"{path}: file not found")
                    if resp.status >= 400:
                        raise RunloopError(
                            f"{method} {path} failed with HTTP {resp.status}: "
                            f"{payload[:200].decode('utf-8', 'replace')}"
                        )
                    return payload
                text = await resp.text()
                try:
                    decoded = json.loads(text) if text else {}
                except ValueError:
                    decoded = {"raw": text}
                if resp.status == 404:
                    raise RunloopFileNotFound(f"{method} {path}: not found ({text[:200]})")
                if resp.status >= 400:
                    # Runloop returns {"message": "..."} for validation errors and
                    # a plain-text "Endpoint ... not found" for unknown routes.
                    detail = ""
                    if isinstance(decoded, dict):
                        detail = str(decoded.get("message") or decoded.get("error") or "")
                    detail = detail or str(text)[:300]
                    raise RunloopError(f"{method} {path} failed with HTTP {resp.status}: {detail}")
                return decoded
        except aiohttp.ClientError as exc:
            raise RunloopError(f"Runloop transport error: {type(exc).__name__}") from None

    # --------------------------------------------------------------- lifecycle

    def _create_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": self.devbox_name,
            "metadata": {"app": "powerx", "managed-by": "nanobot"},
        }
        launch: dict[str, Any] = {
            "resource_size_request": self.resource_size,
            "keep_alive_time_seconds": self.keep_alive_seconds,
        }
        if self.architecture:
            launch["architecture"] = self.architecture
        body["launch_parameters"] = launch
        # Only one of (snapshot, blueprint) may be supplied; the snapshot wins
        # because it carries the exact disk state an operator baselined.
        if self.snapshot_id:
            body["snapshot_id"] = self.snapshot_id
        elif self.blueprint:
            body["blueprint_name"] = self.blueprint
        return body

    async def find_devbox(self, session: aiohttp.ClientSession) -> dict[str, Any] | None:
        """Resolve the session's devbox by its deterministic name.

        Terminal (shutdown/failed) devboxes are ignored so a fresh one is
        created rather than every operation failing against a dead devbox.
        """
        try:
            raw = await self._request(
                session,
                "GET",
                "/v1/devboxes",
                params={"name": self.devbox_name, "limit": "50"},
                timeout=30,
            )
        except RunloopError:
            return None
        items = raw.get("devboxes") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "") != self.devbox_name:
                continue
            status = str(item.get("status") or "").lower()
            if status in _TERMINAL_STATES:
                continue
            return item
        return None

    async def wait_ready(
        self, session: aiohttp.ClientSession, devbox_id: str, timeout: int = 180
    ) -> dict[str, Any]:
        """Poll until the devbox is ``running``, resuming it once if suspended."""
        deadline = asyncio.get_running_loop().time() + timeout
        resumed_once = False
        while True:
            data: Any = {}
            try:
                data = await self._request(session, "GET", f"/v1/devboxes/{quote(devbox_id, safe='')}", timeout=30)
            except RunloopFileNotFound:
                if asyncio.get_running_loop().time() >= deadline:
                    raise RunloopError(f"Runloop devbox {devbox_id} disappeared while starting")
                await asyncio.sleep(2)
                continue
            if isinstance(data, dict):
                status = str(data.get("status") or "").lower()
                if status in _READY_STATES:
                    return data
                if status in _TERMINAL_STATES:
                    reason = str(data.get("failure_reason") or status)
                    raise RunloopError(f"Runloop devbox entered terminal state: {reason}")
                if status in _RESUME_STATES and not resumed_once:
                    # Suspended devboxes keep their disk; resume revives the
                    # exact filesystem instead of starting from a wiped one.
                    resumed_once = True
                    with suppress(RunloopError):
                        await self._request(
                            session,
                            "POST",
                            f"/v1/devboxes/{quote(devbox_id, safe='')}/resume",
                            body={},
                            timeout=90,
                        )
                    if asyncio.get_running_loop().time() >= deadline:
                        deadline = asyncio.get_running_loop().time() + 90
                    continue
            if asyncio.get_running_loop().time() >= deadline:
                raise RunloopError(f"Runloop devbox {devbox_id} did not become ready in time")
            await asyncio.sleep(2)

    async def ensure_devbox(self, session: aiohttp.ClientSession) -> str:
        """Get or create the session's Runloop devbox."""
        if self.last_devbox_id:
            try:
                data = await self._request(
                    session, "GET", f"/v1/devboxes/{quote(self.last_devbox_id, safe='')}", timeout=30
                )
                if isinstance(data, dict) and str(data.get("status") or "").lower() not in _TERMINAL_STATES:
                    ready = await self.wait_ready(session, self.last_devbox_id, timeout=120)
                    self.last_devbox_id = str(ready.get("id") or self.last_devbox_id)
                    return self.last_devbox_id
            except RunloopError:
                pass
            self.last_devbox_id = ""

        existing = await self.find_devbox(session)
        if existing is not None:
            devbox_id = str(existing.get("id") or self.devbox_name)
            try:
                ready = await self.wait_ready(session, devbox_id, timeout=120)
                self.last_devbox_id = str(ready.get("id") or devbox_id)
                return self.last_devbox_id
            except RunloopError:
                # A pre-existing devbox that will not become ready is unusable and
                # would fail every operation forever. Shut it down so a fresh one
                # is created instead of letting the session stay wedged.
                logger.warning("reclaiming stuck Runloop devbox {} that did not become ready", devbox_id)
                with suppress(Exception):
                    await self._request(
                        session, "POST", f"/v1/devboxes/{quote(devbox_id, safe='')}/shutdown", body={}, timeout=60
                    )
                self.last_devbox_id = ""

        created = await self._request(session, "POST", "/v1/devboxes", body=self._create_body(), timeout=90)
        if not isinstance(created, dict):
            raise RunloopError("Runloop devbox create returned an invalid response")
        devbox_id = str(created.get("id") or "")
        if not devbox_id:
            raise RunloopError("Runloop devbox create returned no devbox id")
        ready = await self.wait_ready(session, devbox_id, timeout=240)
        self.last_devbox_id = str(ready.get("id") or devbox_id)
        return self.last_devbox_id

    async def keep_alive(self, devbox_id: str | None = None) -> None:
        """Extend the devbox auto-shutdown deadline."""
        async with aiohttp.ClientSession() as session:
            target = devbox_id or self.last_devbox_id or self.devbox_name
            await self._request(
                session, "POST", f"/v1/devboxes/{quote(target, safe='')}/keep_alive", body={}, timeout=60
            )

    # ------------------------------------------------------------------ exec

    @staticmethod
    def _render(result: dict[str, Any]) -> str:
        stdout = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        code = result.get("exit_status")
        text = stdout
        if stderr:
            text += f"\n[stderr]\n{stderr}"
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
        async with aiohttp.ClientSession() as session:
            devbox_id = await self.ensure_devbox(session)
            # execute_sync blocks until the command finishes, so the HTTP read
            # window must outlast the caller's timeout (plus provisioning/exec
            # overhead) instead of racing the command to the wire.
            result = await self._request(
                session,
                "POST",
                f"/v1/devboxes/{quote(devbox_id, safe='')}/execute_sync",
                body={"command": command},
                timeout=timeout,
            )
        return self._render(result if isinstance(result, dict) else {"stdout": str(result)})

    async def read(self, path: str) -> str:
        target = _safe_path(path, self.workspace)
        async with aiohttp.ClientSession() as session:
            devbox_id = await self.ensure_devbox(session)
            try:
                raw = await self._request(
                    session,
                    "POST",
                    f"/v1/devboxes/{quote(devbox_id, safe='')}/read_file_contents",
                    body={"file_path": target},
                    timeout=90,
                    raw_response=True,
                )
                if isinstance(raw, (bytes, bytearray)):
                    try:
                        return _truncate(raw.decode("utf-8"))
                    except UnicodeDecodeError:
                        return _truncate(base64.b64encode(raw).decode("ascii"))
            except RunloopFileNotFound:
                return ""
            except RunloopError:
                pass
        # Fallback via base64 exec (e.g. a path the file API rejects).
        encoded = await self.run(f"base64 {shlex.quote(target)}")
        return _truncate(re.sub(r"\n\[(?:stderr|exit_code)=?[^\]]*\]\s*$", "", encoded).strip())

    async def write(self, path: str, content: str) -> None:
        target = _safe_path(path, self.workspace)
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError(f"content exceeds {_MAX_CONTENT_CHARS} characters")
        async with aiohttp.ClientSession() as session:
            devbox_id = await self.ensure_devbox(session)
            await self._request(
                session,
                "POST",
                f"/v1/devboxes/{quote(devbox_id, safe='')}/write_file_contents",
                body={"file_path": target, "contents": content},
                timeout=120,
            )

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = _safe_path(path, self.workspace)
        if len(data) > _MAX_UPLOAD_BYTES:
            raise ValueError("file exceeds 200 MiB")
        async with aiohttp.ClientSession() as session:
            devbox_id = await self.ensure_devbox(session)
            parent = posixpath.dirname(target)
            if parent and parent != "/":
                await self._request(
                    session,
                    "POST",
                    f"/v1/devboxes/{quote(devbox_id, safe='')}/execute_sync",
                    body={"command": f"mkdir -p {shlex.quote(parent)}"},
                    timeout=60,
                )
            form = aiohttp.FormData()
            form.add_field("file", data, filename=posixpath.basename(target) or "upload.bin")
            form.add_field("path", target)
            try:
                await self._request(
                    session,
                    "POST",
                    f"/v1/devboxes/{quote(devbox_id, safe='')}/upload_file",
                    data=form,
                    timeout=300,
                )
            except RunloopError:
                # Fallback: base64 write via exec for payloads the multipart
                # endpoint rejects.
                b64 = base64.b64encode(data).decode("ascii")
                await self._request(
                    session,
                    "POST",
                    f"/v1/devboxes/{quote(devbox_id, safe='')}/execute_sync",
                    body={"command": f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(target)}"},
                    timeout=180,
                )

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
                "Add it to the Runloop fetch_allow_hosts setting."
            )
        dest = _safe_path(dest_path, self.workspace)
        command = (
            f"mkdir -p {shlex.quote(posixpath.dirname(dest))} && "
            f"curl -fsSL --max-time {int(timeout)} -o {shlex.quote(dest)} {shlex.quote(url)} && "
            f"stat -c %s {shlex.quote(dest)}"
        )
        out = await self.run(command, timeout=min(timeout + 30, _MAX_TIMEOUT))
        if "[exit_code=" in out and "[exit_code=0]" not in out:
            raise RunloopError(f"remote fetch failed: {out[:300]}")
        return dest

    async def download(self, remote_path: str, local_path: Any) -> Any:
        """Download a devbox file to a local path."""
        target = _safe_path(remote_path, self.workspace)
        destination = Path(str(local_path)).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with aiohttp.ClientSession() as session:
            devbox_id = await self.ensure_devbox(session)
            try:
                raw = await self._request(
                    session,
                    "POST",
                    f"/v1/devboxes/{quote(devbox_id, safe='')}/read_file_contents",
                    body={"file_path": target},
                    timeout=300,
                    raw_response=True,
                )
                if isinstance(raw, (bytes, bytearray)):
                    if len(raw) > _MAX_DOWNLOAD_BYTES:
                        raise RunloopError(
                            f"file exceeds the {_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MiB download limit"
                        )
                    destination.write_bytes(raw)
                    return destination
            except RunloopFileNotFound:
                raise
            except RunloopError:
                pass
        # Fallback via base64 exec
        encoded = await self.run(f"base64 {shlex.quote(target)}", timeout=180)
        payload = re.sub(r"\n\[(?:stderr|exit_code)[^\]]*\]", "", encoded).strip()
        try:
            raw = base64.b64decode(payload, validate=False)
        except Exception:
            raise RunloopError("failed to decode downloaded artifact") from None
        destination.write_bytes(raw)
        return destination

    async def install_packages(self, packages: list[str], *, timeout: int = 600) -> str:
        cleaned = [item for item in packages if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.:@~=-]{0,127}", item)]
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
        async with aiohttp.ClientSession() as session:
            devbox_id = await self.ensure_devbox(session)
            result = await self._request(
                session,
                "POST",
                f"/v1/devboxes/{quote(devbox_id, safe='')}/execute_sync",
                body={"command": "uname -a"},
                timeout=90,
            )
        exit_code = result.get("exit_status") if isinstance(result, dict) else None
        return {
            "ok": exit_code in (0, None),
            "backend": "runloop",
            "devbox_id": devbox_id,
            "platform": str((result or {}).get("stdout") or "").strip()[:200],
        }

    async def reset(self, devbox_id: str | None = None) -> None:
        """Shut the devbox down permanently (explicit wipe / persistence opt-out)."""
        async with aiohttp.ClientSession() as session:
            target = devbox_id or self.last_devbox_id
            if not target:
                existing = await self.find_devbox(session)
                target = str((existing or {}).get("id") or "")
            if not target:
                return
            with suppress(RunloopError):
                await self._request(
                    session, "POST", f"/v1/devboxes/{quote(target, safe='')}/shutdown", body={}, timeout=90
                )