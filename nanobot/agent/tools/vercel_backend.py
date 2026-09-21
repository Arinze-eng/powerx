"""Run the sandbox tool contract on an administrator-configured Vercel Sandbox.

Vercel Sandbox (https://vercel.com/docs/vercel-sandbox) provides ephemeral,
isolated Linux microVMs for agent-generated code. This backend talks to Vercel's
REST API directly over HTTPS (``aiohttp`` only — no extra SDK dependency),
mirroring what the official ``@vercel/sandbox`` client does so the Nanobot host
does not need Node.js in the request path.

The endpoint shape was verified against the live API (see the notes on each
call). The important, non-obvious parts:

* ``POST /v2/sandboxes`` **requires** ``projectId`` — without it the API answers
  ``400 bad_request: missing required property projectId``. One of ``runtime``
  or ``image`` is also required. ``tags`` is accepted on ``/v2`` but rejected
  with ``should NOT have additional property tags`` on ``/v1``, and ``name`` is
  accepted on ``/v2`` (this makes the sandbox *named* and therefore findable
  again by name). Named sandboxes are ``persistent`` by default.
* Commands and file operations are scoped to a **session id**
  (``session.currentSessionId`` from the create response), not the sandbox name:
  ``POST /v2/sandboxes/sessions/{sessionId}/cmd``.
* ``cmd`` takes the executable in ``command`` and the arguments as a separate
  ``args`` array — it does **not** run a shell string. Shell syntax (``&&``,
  pipes, redirection) therefore has to go through ``/bin/sh -c``. The ``POST``
  response returns immediately with ``exitCode: null`` and the command keeps
  running; stdout/stderr are only available from the command's log stream
  (``GET .../cmd/{cmdId}/logs``), which emits newline-delimited
  ``{"data": ..., "stream": "stdout"|"stderr"}`` records. ``exitCode`` in the
  status payload stays ``null`` even after completion, so the exit status is
  captured by appending a sentinel ``echo`` to the shell command instead.
* ``fs/read`` returns the **raw file bytes** (HTTP 200) for an existing path and
  ``404 not_found`` otherwise — it is not JSON.
* ``fs/write`` expects a **gzip-compressed tar** body
  (``Content-Type: application/gzip``) whose member paths are relative to the
  sandbox home (``foo/bar.txt`` → ``/vercel/sandbox/foo/bar.txt``). A JSON body
  or a multipart upload is rejected (``415``/``InvalidContentType``).
* ``GET /v2/sandboxes`` (list) answers ``400`` on some plans, so this backend
  never lists — the deterministic ``name`` lets it look a sandbox up directly.

Lifecycle: a user session maps to a deterministic sandbox name so a sandbox can
be rediscovered after an agent restart. Because Vercel bills by *active CPU*
(I/O wait excluded) and provisions in about a second, the default is
``persist_workspace = False``: a finished task stops its sandbox and the next
operation provisions a fresh one. Operators who want cross-task persistence can
set ``persist_workspace = True`` and the sandbox is instead left running until
its own timeout reaps it.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import posixpath
import re
import shlex
import tarfile
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
# and default working directory is ``/vercel/sandbox`` (confirmed via the
# ``cwd`` field of the create response).
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

# The cmd log stream is newline-delimited JSON. The exit status is not reported
# reliably, so every shell command ends with this sentinel which we parse back.
_EXIT_SENTINEL = "__PX_EXIT__"
_EXIT_MARKER = f'echo "{_EXIT_SENTINEL}$?"'
_EXIT_RE = re.compile(rf"{_EXIT_SENTINEL}(\d+)")

# Default allowed hosts for ``fetch_url`` (kept in sync with the Runloop and
# Daytona backends so the three cloud providers behave identically for the agent).
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
    # Named sandboxes cap out well above a single command's runtime; Vercel
    # documents 45 minutes as the ceiling for this field.
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
    name = re.sub(r"-+", "-", f"px-{slug}-{digest}").strip("-")
    return name if _NAME_RE.fullmatch(name) else f"px-{digest}"


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


def _relative_member(path: str, root: str = WORKSPACE) -> str:
    """Path relative to the sandbox home, as the fs/write tar requires."""
    absolute = _safe_path(path, root)
    if absolute == root:
        return "."
    return absolute[len(root) + 1 :]


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
            int(getattr(config, "timeout_ms", 1_800_000) or 1_800_000)
        )
        self.fetch_allow_hosts = validate_vercel_fetch_allow_hosts(
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
        # Holds the sandbox *name* (the API's addressable key for a named
        # sandbox); sessions are resolved from it on demand.
        self.last_sandbox_id: str = ""
        self.last_session_id: str = ""
        # Lazily discovered project id (see ``_resolve_project_id``).
        self._resolved_project_id: str = ""
        # Vercel bills by ACTIVE CPU only, so a stopped sandbox costs nothing
        # while the next operation pays a sub-second provision. Persistence is
        # therefore off by default (the opposite of the disk-preserving
        # backends), and only operators who want cross-task files turn it on.
        self.persist_workspace = bool(getattr(config, "persist_workspace", False))

    # ------------------------------------------------------------------ HTTP

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        if not self.token:
            raise VercelError("Vercel token is not configured")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

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
        content_type: str | None = "application/json",
        timeout: int = 120,
        raw_response: bool = False,
        allow_404: bool = False,
    ) -> Any:
        url = f"{self.api_url}{path}"
        scoped = self._scoped_params(params)
        if scoped:
            url = f"{url}?{urlencode(scoped)}"
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(content_type if (body is not None or data is not None) else None),
                json=body,
                data=data,
                timeout=aiohttp.ClientTimeout(total=timeout + 60),
            ) as resp:
                payload = await resp.read()
                if allow_404 and resp.status == 404:
                    return None
                if raw_response:
                    if resp.status == 404:
                        raise VercelFileNotFound(f"{path}: file not found")
                    if resp.status >= 400:
                        raise VercelError(
                            f"{method} {path} failed with HTTP {resp.status}: "
                            f"{payload[:200].decode('utf-8', 'replace')}"
                        )
                    return payload
                text = payload.decode("utf-8", "replace")
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
                    detail = detail or text[:300]
                    raise VercelError(f"{method} {path} failed with HTTP {resp.status}: {detail}")
                return decoded
        except aiohttp.ClientError as exc:
            raise VercelError(f"Vercel transport error: {type(exc).__name__}") from None

    # --------------------------------------------------------------- lifecycle

    def _create_body(self) -> dict[str, Any]:
        """Body for ``POST /v2/sandboxes``.

        ``projectId`` is required by the API, and a named sandbox (``name``) is
        what lets us find it again later; ``tags`` mirrors the name for
        operator visibility. Note ``tags`` is only valid on ``/v2``.
        """
        body: dict[str, Any] = {
            "name": self.sandbox_name,
            "runtime": self.runtime,
            "resources": {"vcpus": self.vcpus},
            "timeout": self.timeout_ms,
            # Vercel caps a sandbox's lifetime; keeping the sandbox alive until
            # the task-end release path stops it means a long build cannot be
            # guillotined mid-run.
            "tags": {"app": "powerx", "managed-by": "nanobot", "name": self.sandbox_name},
        }
        if self.project_id:
            body["projectId"] = self.project_id
        return body

    async def _get_sandbox(
        self, session: aiohttp.ClientSession, name: str
    ) -> dict[str, Any] | None:
        """Fetch a named sandbox, or ``None`` if it is gone."""
        try:
            payload = await self._request(
                session,
                "GET",
                f"/v2/sandboxes/{quote(name, safe='')}",
                params={"projectId": self.project_id} if self.project_id else None,
                timeout=30,
                allow_404=True,
            )
        except VercelError:
            return None
        if isinstance(payload, dict):
            sandbox = payload.get("sandbox")
            if isinstance(sandbox, dict):
                return sandbox
        return None

    def _remember(self, sandbox: dict[str, Any]) -> str:
        name = str(sandbox.get("name") or self.sandbox_name)
        session_id = str(sandbox.get("currentSessionId") or "")
        self.last_sandbox_id = name
        if session_id:
            self.last_session_id = session_id
        return name

    async def wait_ready(
        self, session: aiohttp.ClientSession, name: str, timeout: int = 180
    ) -> dict[str, Any]:
        """Poll until the named sandbox is ``running``."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            data = await self._get_sandbox(session, name)
            if isinstance(data, dict):
                status = str(data.get("status") or "").lower()
                if status in _READY_STATES:
                    return data
                if status in _TERMINAL_STATES:
                    raise VercelError(f"Vercel sandbox entered terminal state: {status}")
            if asyncio.get_running_loop().time() >= deadline:
                raise VercelError(f"Vercel sandbox {name} did not become ready in time")
            await asyncio.sleep(2)

    async def _resolve_project_id(self, session: aiohttp.ClientSession) -> str:
        """Return the project to create sandboxes in.

        ``POST /v2/sandboxes`` requires ``projectId``, but operators usually only
        hold a token. When no project is configured we discover one: an explicit
        ``vercel-sandbox-default-project`` if the team has it (Vercel's own
        default for sandbox work), otherwise the first project the token can
        see. The choice is cached for the process.
        """
        if self.project_id:
            return self.project_id
        if self._resolved_project_id:
            return self._resolved_project_id
        payload = await self._request(
            session, "GET", "/v9/projects", params={"limit": "100"}, timeout=45
        )
        projects = payload.get("projects") if isinstance(payload, dict) else None
        if not isinstance(projects, list) or not projects:
            raise VercelError(
                "Vercel Sandbox requires a project ID and none could be discovered. "
                "Set the Vercel project ID in the admin execution settings "
                "(Vercel -> projectId)."
            )
        chosen = ""
        for item in projects:
            if isinstance(item, dict) and str(item.get("name") or "") == "vercel-sandbox-default-project":
                chosen = str(item.get("id") or "")
                break
        if not chosen:
            chosen = str((projects[0] or {}).get("id") or "")
        if not chosen:
            raise VercelError("Vercel returned projects without ids; set the project ID manually")
        logger.info("Vercel Sandbox using discovered project {}", chosen)
        self._resolved_project_id = chosen
        return chosen

    async def ensure_sandbox(self, session: aiohttp.ClientSession) -> str:
        """Get or create the session's Vercel sandbox; returns the sandbox name."""
        # Reuse the seeded/persisted sandbox when it is still alive.
        candidate = self.last_sandbox_id or self.sandbox_name
        existing = await self._get_sandbox(session, candidate)
        if isinstance(existing, dict):
            status = str(existing.get("status") or "").lower()
            if status not in _TERMINAL_STATES:
                ready = await self.wait_ready(session, candidate, timeout=120)
                return self._remember(ready)
            logger.warning(
                "reclaiming terminal Vercel sandbox {} (status={}) before creating a new one",
                candidate,
                status,
            )

        project_id = await self._resolve_project_id(session)
        self.project_id = project_id

        created = await self._request(
            session, "POST", "/v2/sandboxes", body=self._create_body(), timeout=90
        )
        sandbox = created.get("sandbox") if isinstance(created, dict) else None
        if not isinstance(sandbox, dict):
            raise VercelError("Vercel sandbox create returned an invalid response")
        name = self._remember(sandbox)
        ready = await self.wait_ready(session, name, timeout=240)
        return self._remember(ready)

    async def _session_id(self, session: aiohttp.ClientSession) -> tuple[str, str]:
        """Resolve (sandbox name, current session id).

        Command and file endpoints are session-scoped, and the session id can
        change when a sandbox is (re)started, so it is always re-read rather
        than cached across calls.
        """
        name = await self.ensure_sandbox(session)
        data = await self._get_sandbox(session, name)
        session_id = str((data or {}).get("currentSessionId") or "")
        if not session_id:
            raise VercelError(f"Vercel sandbox {name} has no active session")
        self.last_session_id = session_id
        return name, session_id

    async def keep_alive(self, sandbox_id: str | None = None) -> None:
        """Extend the sandbox timeout window."""
        async with aiohttp.ClientSession() as session:
            target = sandbox_id or self.last_sandbox_id or self.sandbox_name
            await self._request(
                session,
                "PATCH",
                f"/v2/sandboxes/{quote(target, safe='')}",
                body={"timeout": self.timeout_ms},
                params={"projectId": self.project_id} if self.project_id else None,
                timeout=60,
            )

    # ------------------------------------------------------------------ exec

    async def _run_shell(
        self, session: aiohttp.ClientSession, session_id: str, command: str, timeout: int
    ) -> tuple[str, int | None]:
        """Run ``command`` through /bin/sh and return (combined output, exit code).

        ``cmd`` does not accept a shell string: the program goes in ``command``
        and its argv in ``args``, so shell syntax is only available via
        ``/bin/sh -c``. Output is not in the POST response — it is streamed from
        the command's log endpoint as newline-delimited JSON. Exit status is not
        reported reliably either, hence the trailing sentinel echo.
        """
        wrapped = f"{command}\n{_EXIT_MARKER}"
        started = await self._request(
            session,
            "POST",
            f"/v2/sandboxes/sessions/{quote(session_id, safe='')}/cmd",
            body={"command": "/bin/sh", "args": ["-c", wrapped], "cwd": self.workspace},
            timeout=60,
        )
        command_info = started.get("command") if isinstance(started, dict) else None
        cmd_id = str((command_info or {}).get("id") or "")
        if not cmd_id:
            raise VercelError("Vercel command did not return an id")

        log_path = f"/v2/sandboxes/sessions/{quote(session_id, safe='')}/cmd/{quote(cmd_id, safe='')}/logs"
        chunks: list[str] = []
        exit_code: int | None = None
        try:
            async with session.get(
                f"{self.api_url}{log_path}",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=timeout + 60),
            ) as resp:
                if resp.status >= 400:
                    detail = (await resp.read())[:200].decode("utf-8", "replace")
                    raise VercelError(f"Vercel command log stream failed: HTTP {resp.status} {detail}")
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        chunks.append(line)
                        continue
                    if not isinstance(record, dict):
                        continue
                    text = str(record.get("data") or "")
                    if record.get("stream") == "stderr":
                        chunks.append(f"[stderr] {text}")
                    else:
                        chunks.append(text)
                    match = _EXIT_RE.search(text)
                    if match:
                        exit_code = int(match.group(1))
        except asyncio.TimeoutError:
            raise VercelError(f"Vercel command exceeded its {timeout}s budget") from None

        output = "".join(chunks)
        output = _EXIT_RE.sub("", output).strip()
        return output, exit_code

    @staticmethod
    def _render(output: str, exit_code: int | None) -> str:
        text = output
        if exit_code is not None and exit_code != 0:
            text = f"{text}\n[exit_code={exit_code}]"
        return _truncate(text) or "(no output)"

    async def run(self, command: str, *, timeout: int = 120) -> str:
        command = str(command or "").strip()
        if not command:
            raise ValueError("command is required")
        if len(command) > _MAX_COMMAND_CHARS:
            raise ValueError(f"command exceeds {_MAX_COMMAND_CHARS} characters")
        timeout = max(1, min(int(timeout), _MAX_TIMEOUT))
        async with aiohttp.ClientSession() as session:
            _name, session_id = await self._session_id(session)
            output, exit_code = await self._run_shell(session, session_id, command, timeout)
        return self._render(output, exit_code)

    async def read(self, path: str) -> str:
        target = _safe_path(path, self.workspace)
        async with aiohttp.ClientSession() as session:
            _name, session_id = await self._session_id(session)
            # fs/read returns the raw bytes of the file (it is not JSON).
            raw = await self._request(
                session,
                "POST",
                f"/v2/sandboxes/sessions/{quote(session_id, safe='')}/fs/read",
                body={"path": target},
                timeout=90,
                raw_response=True,
                allow_404=True,
            )
            if raw is None:
                return ""
            if isinstance(raw, (bytes, bytearray)):
                try:
                    return _truncate(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    raise VercelError(
                        "file is not valid UTF-8 text; use action=download_url to fetch it"
                    ) from None
        return ""

    def _tar_for(self, entries: list[tuple[str, bytes]]) -> bytes:
        """Build the gzip tar body that fs/write expects (home-relative paths)."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            seen_dirs: set[str] = set()
            for member, payload in entries:
                parent = posixpath.dirname(member)
                if parent and parent != "." and parent not in seen_dirs:
                    # Add directory entries so nested writes create their parents.
                    info = tarfile.TarInfo(parent)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    archive.addfile(info)
                    seen_dirs.add(parent)
                info = tarfile.TarInfo(member)
                info.size = len(payload)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(payload))
        return buffer.getvalue()

    async def _write_archive(self, entries: list[tuple[str, bytes]]) -> None:
        payload = self._tar_for(entries)
        async with aiohttp.ClientSession() as session:
            _name, session_id = await self._session_id(session)
            await self._request(
                session,
                "POST",
                f"/v2/sandboxes/sessions/{quote(session_id, safe='')}/fs/write",
                data=payload,
                content_type="application/gzip",
                timeout=300,
            )

    async def write(self, path: str, content: str) -> None:
        target = _safe_path(path, self.workspace)
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError(f"content exceeds {_MAX_CONTENT_CHARS} characters")
        await self._write_archive([(_relative_member(target), content.encode("utf-8"))])

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = _safe_path(path, self.workspace)
        if len(data) > _MAX_UPLOAD_BYTES:
            raise ValueError("file exceeds 200 MiB")
        await self._write_archive([(_relative_member(target), bytes(data))])

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
            _name, session_id = await self._session_id(session)
            raw = await self._request(
                session,
                "POST",
                f"/v2/sandboxes/sessions/{quote(session_id, safe='')}/fs/read",
                body={"path": target},
                timeout=300,
                raw_response=True,
            )
        if not isinstance(raw, (bytes, bytearray)):
            raise VercelError("Vercel did not return file bytes")
        if len(raw) > _MAX_DOWNLOAD_BYTES:
            raise VercelError(
                f"file exceeds the {_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MiB download limit"
            )
        destination.write_bytes(bytes(raw))
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
        async with aiohttp.ClientSession() as session:
            name, session_id = await self._session_id(session)
            output, exit_code = await self._run_shell(
                session, session_id, "uname -a", timeout=90
            )
        return {
            "ok": exit_code in (0, None),
            "backend": "vercel",
            "sandbox_id": name,
            "session_id": session_id,
            "platform": output.strip()[:200],
        }

    async def reset(self, sandbox_id: str | None = None) -> None:
        """Stop (delete) the named sandbox permanently."""
        async with aiohttp.ClientSession() as session:
            target = sandbox_id or self.last_sandbox_id or self.sandbox_name
            if not target:
                return
            with suppress(VercelError):
                await self._request(
                    session,
                    "DELETE",
                    f"/v2/sandboxes/{quote(target, safe='')}",
                    params={"projectId": self.project_id} if self.project_id else None,
                    timeout=90,
                )
            self.last_sandbox_id = ""
            self.last_session_id = ""