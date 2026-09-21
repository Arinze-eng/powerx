"""Run the sandbox tool contract on an administrator-configured Vercel Sandbox.

Vercel Sandbox (https://vercel.com/docs/vercel-sandbox) provides ephemeral,
isolated Linux microVMs for running untrusted or agent-generated code. This
backend talks to Vercel's REST API directly over HTTPS (``aiohttp`` only — no
extra SDK dependency), mirroring the official ``@vercel/sandbox`` client so the
Nanobot host does not need Node.js in the request path.

Sandbox lifecycle (``api_url`` defaults to ``https://api.vercel.com``):
* ``POST   /v1/sandboxes``                  → create a sandbox
* ``GET    /v2/sandboxes``                  → list sandboxes (filter by tag/name)
* ``GET    /v1/sandboxes/{id}``             → sandbox metadata (status, timeout)
* ``PATCH  /v1/sandboxes/{id}``             → extend the sandbox timeout
* ``POST   /v1/sandboxes/{id}/stop``        → stop (destroy) the sandbox

Execution / files (all scoped to ``/v1/sandboxes/{id}``):
* ``POST   /cmd``            → body ``{"command","args","cwd","env"}``
                               returns ``{"stdout","stderr","exitCode"}``
* ``POST   /fs/write``       → body ``{"files":[{"path","content"}]}``
* ``GET    /fs/read``        → ``?path=<p>`` → raw file bytes

Lifecycle: a user session maps to a deterministic sandbox name so a sandbox can
be rediscovered after an agent restart. Because Vercel sandboxes are extremely
cheap to create and are billed by *active CPU* (I/O wait excluded), the default
is ``persist_workspace = False``: a finished task stops its sandbox and the next
operation provisions a fresh one. Operators who want cross-task persistence can
set ``persist_workspace = True`` and the sandbox will instead be left running
until its own timeout reaps it.
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

# Vercel Sandbox runs commands as the unprivileged ``vercel`` user, whose home
# and default working directory is ``/vercel/sandbox``.
WORKSPACE = "/vercel/sandbox"

DEFAULT_API_URL = "https://api.vercel.com"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

# Runtimes accepted by the Vercel Sandbox ``runtime`` field.
RUNTIMES: tuple[str, ...] = ("node22", "node24", "python3.13")

# Sandbox statuses that mean "ready to run commands".
_READY_STATES = frozenset({"running", "ready"})

# Statuses that are still on their way up.
_TRANSIENT_STATES = frozenset({"pending", "creating", "starting", "provisioning"})

# Nothing here can be revived; the sandbox must be recreated.
_TERMINAL_STATES = frozenset({"stopped", "stopping", "failed", "error", "aborted", "deleted"})

# Default allowed hosts for ``fetch_url`` (kept in sync with the Runloop/Daytona
# backends so the three cloud providers behave identically for the agent).
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


class VercelError(RuntimeError):
    """Any non-recoverable Vercel platform or sandbox failure."""


class VercelFileNotFound(VercelError):
    """The requested path does not exist in the sandbox."""


def _looks_like_missing_file(detail: str) -> bool:
    lowered = detail.lower()
    return "not found" in lowered or "no such file" in lowered or "404" in lowered


def validate_vercel_token(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 512 or not re.fullmatch(r"[A-Za-z0-9_\-\.]+", value):
        raise ValueError("Vercel token format is invalid")
    return value


def validate_vercel_api_url(raw: str) -> str:
    value = str(raw or "").strip().rstrip("/") or DEFAULT_API_URL
    if len(value) > 253:
        raise ValueError("Vercel API URL is too long")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Vercel API URL must be an https URL (for example https://api.vercel.com)")
    return value


def validate_vercel_team_id(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9_\-]+", value):
        raise ValueError("Vercel team ID format is invalid")
    return value


def validate_vercel_project_id(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9_\-]+", value):
        raise ValueError("Vercel project ID format is invalid")
    return value


def validate_vercel_runtime(raw: str) -> str:
    value = str(raw or "").strip() or "node22"
    if value not in RUNTIMES:
        raise ValueError("Vercel runtime must be one of " + ", ".join(RUNTIMES))
    return value


def validate_vercel_vcpus(raw: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Vercel vCPUs must be an integer") from None
    if not 1 <= value <= 8:
        raise ValueError("Vercel vCPUs must be between 1 and 8")
    return value


def validate_vercel_timeout_ms(raw: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Vercel timeout must be an integer number of milliseconds") from None
    # Vercel Sandbox caps a single sandbox lifetime at 45 minutes.
    if not 60_000 <= value <= 2_700_000:
        raise ValueError("Vercel timeout must be between 60000 and 2700000 ms (45 minutes)")
    return value


def validate_vercel_fetch_allow_hosts(raw: str) -> str:
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


def vercel_sandbox_name(session_key: str) -> str:
    """Deterministic, valid sandbox name for a session so sandboxes survive restarts."""
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


class VercelExecutionBackend:
    """Async client implementing the shared sandbox contract against Vercel Sandbox."""

    def __init__(self, config: Any, *, sandbox_name: str = "powerx-session") -> None:
        self.config = config
        self.api_url = validate_vercel_api_url(str(getattr(config, "api_url", "") or ""))
        self.token = validate_vercel_token(str(getattr(config, "token", "") or ""))
        self.team_id = validate_vercel_team_id(str(getattr(config, "team_id", "") or ""))
        self.project_id = validate_vercel_project_id(
            str(getattr(config, "project_id", "") or "")
        )
        self.runtime = validate_vercel_runtime(str(getattr(config, "runtime", "") or "node22"))
        self.vcpus = validate_vercel_vcpus(int(getattr(config, "vcpus", 2) or 2))
        self.timeout_ms = validate_vercel_timeout_ms(
            int(getattr(config, "timeout_ms", 300_000) or 300_000)
        )
        self.fetch_allow_hosts = validate_vercel_fetch_allow_hosts(
            str(getattr(config, "fetch_allow_hosts", "") or "")
        )
        self._fetch_hosts: set[str] = (
            {h.strip() for h in self.fetch_allow_hosts.split(",") if h.strip()}
            if self.fetch_allow_hosts
            else set(DEFAULT_FETCH_ALLOW_HOSTS)
        )
        self.sandbox_name = sandbox_name if _NAME_RE.fullmatch(sandbox_name) else "powerx-session"
        self.workspace = WORKSPACE
        self.last_sandbox_id: str = ""
        # Vercel bills by ACTIVE CPU only, so a stopped sandbox costs nothing
        # while the next operation pays a sub-second provision. Persistence is
        # therefore off by default (the opposite of the disk-preserving
        # backends), and only operators who want cross-task files turn it on.
        self.persist_workspace = bool(getattr(config, "persist_workspace", False))

    # ------------------------------------------------------------------ HTTP

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise VercelError("Vercel token is not configured")
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _scoped_params(self, params: dict[str, str] | None = None) -> dict[str, str]:
        merged = dict(params or {})
        if self.team_id:
            merged["teamId"] = self.team_id
        return merged

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
        scoped = self._scoped_params(params)
        if scoped:
            url = f"{url}?{urlencode(scoped)}"
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
                        raise VercelFileNotFound(f"{path}: file not found")
                    if resp.status >= 400:
                        raise VercelError(
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
                    raise VercelFileNotFound(f"{method} {path}: not found ({text[:200]})")
                if resp.status >= 400:
                    # Vercel returns {"error": {"code": ..., "message": ...}}.
                    detail = ""
                    if isinstance(decoded, dict):
                        error = decoded.get("error")
                        if isinstance(error, dict):
                            detail = str(error.get("message") or error.get("code") or "")
                        detail = detail or str(decoded.get("message") or "")
                    detail = detail or str(text)[:300]
                    raise VercelError(f"{method} {path} failed with HTTP {resp.status}: {detail}")
                return decoded
        except aiohttp.ClientError as exc:
            raise VercelError(f"Vercel transport error: {type(exc).__name__}") from None

    # --------------------------------------------------------------- lifecycle

    def _create_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "runtime": self.runtime,
            "resources": {"vcpus": self.vcpus},
            "timeout": self.timeout_ms,
            "tags": {"app": "powerx", "managed-by": "nanobot", "name": self.sandbox_name},
        }
        if self.project_id:
            body["projectId"] = self.project_id
        return body

    async def find_sandbox(self, session: aiohttp.ClientSession) -> dict[str, Any] | None:
        """Resolve the session's sandbox by its deterministic tag name.

        Terminal (stopped/failed) sandboxes are ignored so a fresh one is
        created rather than every operation failing against a dead sandbox.
        """
        try:
            raw = await self._request(
                session,
                "GET",
                "/v2/sandboxes",
                params={"limit": "100", "sortBy": "createdAt", "order": "desc"},
                timeout=30,
            )
        except VercelError:
            return None
        items = raw.get("sandboxes") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, dict):
                continue
            tags = item.get("tags") or {}
            tag_name = str(tags.get("name") or "") if isinstance(tags, dict) else ""
            if tag_name != self.sandbox_name and str(item.get("name") or "") != self.sandbox_name:
                continue
            status = str(item.get("status") or "").lower()
            if status in _TERMINAL_STATES:
                continue
            return item
        return None

    async def wait_ready(
        self, session: aiohttp.ClientSession, sandbox_id: str, timeout: int = 180
    ) -> dict[str, Any]:
        """Poll until the sandbox is ``running``."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            data: Any = {}
            try:
                data = await self._request(
                    session, "GET", f"/v1/sandboxes/{quote(sandbox_id, safe='')}", timeout=30
                )
            except VercelFileNotFound:
                if asyncio.get_running_loop().time() >= deadline:
                    raise VercelError(f"Vercel sandbox {sandbox_id} disappeared while starting")
                await asyncio.sleep(2)
                continue
            if isinstance(data, dict):
                status = str(data.get("status") or "").lower()
                if status in _READY_STATES:
                    return data
                if status in _TERMINAL_STATES:
                    raise VercelError(f"Vercel sandbox entered terminal state: {status}")
            if asyncio.get_running_loop().time() >= deadline:
                raise VercelError(f"Vercel sandbox {sandbox_id} did not become ready in time")
            await asyncio.sleep(2)

    async def ensure_sandbox(self, session: aiohttp.ClientSession) -> str:
        """Get or create the session's Vercel sandbox."""
        if self.last_sandbox_id:
            try:
                data = await self._request(
                    session, "GET", f"/v1/sandboxes/{quote(self.last_sandbox_id, safe='')}", timeout=30
                )
                if isinstance(data, dict) and str(data.get("status") or "").lower() not in _TERMINAL_STATES:
                    ready = await self.wait_ready(session, self.last_sandbox_id, timeout=120)
                    self.last_sandbox_id = str(ready.get("id") or self.last_sandbox_id)
                    return self.last_sandbox_id
            except VercelError:
                pass
            self.last_sandbox_id = ""

        if self.persist_workspace:
            existing = await self.find_sandbox(session)
            if existing is not None:
                sandbox_id = str(existing.get("id") or self.sandbox_name)
                try:
                    ready = await self.wait_ready(session, sandbox_id, timeout=120)
                    self.last_sandbox_id = str(ready.get("id") or sandbox_id)
                    return self.last_sandbox_id
                except VercelError:
                    # A pre-existing sandbox that will not become ready is
                    # unusable and would fail every operation forever. Stop it so
                    # a fresh one is created instead of wedging the session.
                    logger.warning("reclaiming stuck Vercel sandbox {} that did not become ready", sandbox_id)
                    with suppress(Exception):
                        await self._request(
                            session, "POST", f"/v1/sandboxes/{quote(sandbox_id, safe='')}/stop", body={}, timeout=60
                        )
                    self.last_sandbox_id = ""

        created = await self._request(session, "POST", "/v1/sandboxes", body=self._create_body(), timeout=90)
        if not isinstance(created, dict):
            raise VercelError("Vercel sandbox create returned an invalid response")
        sandbox_id = str(created.get("sandbox") or created.get("id") or "")
        if isinstance(created.get("sandbox"), dict):
            sandbox_id = str(created["sandbox"].get("id") or "")
        if not sandbox_id:
            raise VercelError("Vercel sandbox create returned no sandbox id")
        ready = await self.wait_ready(session, sandbox_id, timeout=240)
        self.last_sandbox_id = str(ready.get("id") or sandbox_id)
        return self.last_sandbox_id

    async def keep_alive(self, sandbox_id: str | None = None) -> None:
        """Extend the sandbox timeout window."""
        async with aiohttp.ClientSession() as session:
            target = sandbox_id or self.last_sandbox_id
            if not target:
                return
            await self._request(
                session,
                "PATCH",
                f"/v1/sandboxes/{quote(target, safe='')}",
                body={"timeout": self.timeout_ms},
                timeout=60,
            )

    # ------------------------------------------------------------------ exec

    @staticmethod
    def _render(result: dict[str, Any]) -> str:
        stdout = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        code = result.get("exitCode", result.get("exit_code"))
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
            sandbox_id = await self.ensure_sandbox(session)
            # Vercel's /cmd blocks until the command finishes, so the HTTP read
            # window must outlast the caller's timeout instead of racing the
            # command to the wire.
            result = await self._request(
                session,
                "POST",
                f"/v1/sandboxes/{quote(sandbox_id, safe='')}/cmd",
                body={
                    "command": command,
                    "cwd": self.workspace,
                    "timeout": timeout * 1000,
                },
                timeout=timeout,
            )
            payload = result.get("command") if isinstance(result, dict) else None
            if isinstance(payload, dict):
                return self._render(payload)
        return self._render(result if isinstance(result, dict) else {"stdout": str(result)})

    async def read(self, path: str) -> str:
        target = _safe_path(path, self.workspace)
        async with aiohttp.ClientSession() as session:
            sandbox_id = await self.ensure_sandbox(session)
            try:
                raw = await self._request(
                    session,
                    "GET",
                    f"/v1/sandboxes/{quote(sandbox_id, safe='')}/fs/read",
                    params={"path": target},
                    timeout=90,
                    raw_response=True,
                )
                if isinstance(raw, (bytes, bytearray)):
                    try:
                        return _truncate(raw.decode("utf-8"))
                    except UnicodeDecodeError:
                        return _truncate(base64.b64encode(raw).decode("ascii"))
            except VercelFileNotFound:
                return ""
            except VercelError:
                pass
        # Fallback via base64 exec (e.g. a path the file API rejects).
        encoded = await self.run(f"base64 {shlex.quote(target)}")
        return _truncate(re.sub(r"\n\[(?:stderr|exit_code)=?[^\]]*\]\s*$", "", encoded).strip())

    async def write(self, path: str, content: str) -> None:
        target = _safe_path(path, self.workspace)
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError(f"content exceeds {_MAX_CONTENT_CHARS} characters")
        async with aiohttp.ClientSession() as session:
            sandbox_id = await self.ensure_sandbox(session)
            await self._request(
                session,
                "POST",
                f"/v1/sandboxes/{quote(sandbox_id, safe='')}/fs/write",
                body={"files": [{"path": target, "content": content}]},
                timeout=120,
            )

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = _safe_path(path, self.workspace)
        if len(data) > _MAX_UPLOAD_BYTES:
            raise ValueError("file exceeds 200 MiB")
        parent = posixpath.dirname(target)
        if parent and parent != "/":
            with suppress(VercelError):
                await self.run(f"mkdir -p {shlex.quote(parent)}", timeout=60)
        try:
            await self.write(target, base64.b64encode(data).decode("ascii"))
            # The fs/write API treats content as text, so decode the base64 we
            # just wrote back into the real bytes in place.
            await self.run(
                f"base64 -d {shlex.quote(target)} > {shlex.quote(target)}.bin "
                f"&& mv {shlex.quote(target)}.bin {shlex.quote(target)}",
                timeout=180,
            )
        except VercelError:
            b64 = base64.b64encode(data).decode("ascii")
            await self.run(
                f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(target)}",
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
                "Add it to the Vercel fetch_allow_hosts setting."
            )
        dest = _safe_path(dest_path, self.workspace)
        command = (
            f"mkdir -p {shlex.quote(posixpath.dirname(dest))} && "
            f"curl -fsSL --max-time {int(timeout)} -o {shlex.quote(dest)} {shlex.quote(url)} && "
            f"stat -c %s {shlex.quote(dest)}"
        )
        out = await self.run(command, timeout=min(timeout + 30, _MAX_TIMEOUT))
        if "[exit_code=" in out and "[exit_code=0]" not in out:
            raise VercelError(f"remote fetch failed: {out[:300]}")
        return dest

    async def download(self, remote_path: str, local_path: Any) -> Any:
        """Download a sandbox file to a local path."""
        target = _safe_path(remote_path, self.workspace)
        destination = Path(str(local_path)).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with aiohttp.ClientSession() as session:
            sandbox_id = await self.ensure_sandbox(session)
            try:
                raw = await self._request(
                    session,
                    "GET",
                    f"/v1/sandboxes/{quote(sandbox_id, safe='')}/fs/read",
                    params={"path": target},
                    timeout=300,
                    raw_response=True,
                )
                if isinstance(raw, (bytes, bytearray)):
                    if len(raw) > _MAX_DOWNLOAD_BYTES:
                        raise VercelError(
                            f"file exceeds the {_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MiB download limit"
                        )
                    destination.write_bytes(raw)
                    return destination
            except VercelFileNotFound:
                raise
            except VercelError:
                pass
        # Fallback via base64 exec
        encoded = await self.run(f"base64 {shlex.quote(target)}", timeout=180)
        payload = re.sub(r"\n\[(?:stderr|exit_code)[^\]]*\]", "", encoded).strip()
        try:
            raw = base64.b64decode(payload, validate=False)
        except Exception:
            raise VercelError("failed to decode downloaded artifact") from None
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
            sandbox_id = await self.ensure_sandbox(session)
            result = await self._request(
                session,
                "POST",
                f"/v1/sandboxes/{quote(sandbox_id, safe='')}/cmd",
                body={"command": "uname -a", "cwd": self.workspace},
                timeout=90,
            )
        payload = result.get("command") if isinstance(result, dict) else None
        payload = payload if isinstance(payload, dict) else (result or {})
        exit_code = payload.get("exitCode", payload.get("exit_code"))
        return {
            "ok": exit_code in (0, None),
            "backend": "vercel",
            "sandbox_id": sandbox_id,
            "platform": str(payload.get("stdout") or "").strip()[:200],
        }

    async def reset(self, sandbox_id: str | None = None) -> None:
        """Stop the sandbox permanently (explicit wipe / persistence opt-out)."""
        async with aiohttp.ClientSession() as session:
            target = sandbox_id or self.last_sandbox_id
            if not target:
                return
            with suppress(VercelError):
                await self._request(
                    session, "POST", f"/v1/sandboxes/{quote(target, safe='')}/stop", body={}, timeout=90
                )
            self.last_sandbox_id = ""