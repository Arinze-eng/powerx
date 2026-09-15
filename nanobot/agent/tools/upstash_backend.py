"""Run the sandbox tool contract on an administrator-configured Upstash Box.

Upstash Box (https://upstash.com/docs/box) provides isolated cloud containers
with shell + filesystem APIs.  This backend talks to the documented REST API
directly over HTTPS (``aiohttp`` only — no extra SDK dependency), mirroring the
request shapes used by the official ``upstash-box`` Python SDK:

* ``POST   {base}/v2/box``                       → create a box (runtime/size/name)
* ``GET    {base}/v2/box/{id}``                  → box metadata (status)
* ``POST   {base}/v2/box/{id}/exec``             → body ``{"command": ["sh", "-c", cmd]}``
                                                    returns ``{exit_code, output, error}``
* ``GET    {base}/v2/box/{id}/files/read?path=`` → ``{path, content}`` (or raw bytes w/ encoding=base64)
* ``POST   {base}/v2/box/{id}/files/write``      → body ``{"path", "content", "encoding"}``
* ``DELETE {base}/v2/box/{id}``                  → permanently kill the box

Sandbox lifecycle: every user session maps to one box.  The box is created with
an account-side TTL so it is reaped automatically even if cleanup is missed, and
``reset`` / end-of-task handling deletes it immediately ("kill sandbox
automatically for users after task done").
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import posixpath
import re
import shlex
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp

_MAX_COMMAND_CHARS = 12_000
_MAX_CONTENT_CHARS = 120_000
_MAX_RESULT_CHARS = 16_000
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
_MAX_TIMEOUT = 900

# Upstash's synchronous exec endpoint is capped at 5 minutes server-side and
# accepts no timeout parameter. Anything we send must therefore finish well
# inside that window: we wrap commands with coreutils `timeout` (mirroring the
# official SDK) and hard-cap the exec budget below the server-side limit. A
# command killed by the wrapper exits 124, which callers surface verbatim.
_MAX_SYNC_EXEC_TIMEOUT = 270

# Upstash boxes keep their working files under this workspace root.
WORKSPACE = "/workspace/home"

_ALLOWED_RUNTIMES = {
    "python", "node", "golang", "ruby", "rust",
    "python-alpine", "node-alpine", "golang-alpine", "ruby-alpine", "rust-alpine",
}
_ALLOWED_SIZES = {"small", "medium", "large"}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

# Hosts whose URLs fetch_url may download (mirrors the VPS backend policy).
_ALLOWED_FETCH_HOSTS = {"onlyfiles.com", "gofile.io"}


class UpstashError(RuntimeError):
    """Raised when the Upstash Box API rejects a request."""


class UpstashFileNotFound(UpstashError):
    """Raised when a remote file does not exist.

    Upstash's ``files/read`` endpoint answers *missing* files with an opaque
    ``HTTP 500 {"error": "Failed to read file"}`` rather than a clean 404, so we
    translate that shape into a distinct, catchable error. Callers that merely
    want to know "is there content here?" can treat it as empty instead of a hard
    failure — this is what previously surfaced to users as "could not write".
    """


# Substrings Upstash uses when a read fails because the target is absent.
_MISSING_FILE_HINTS = (
    "failed to read file",
    "no such file",
    "not found",
    "does not exist",
)


def _looks_like_missing_file(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(hint in lowered for hint in _MISSING_FILE_HINTS)


def validate_upstash_api_key(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 256 or any(ord(char) < 0x21 or ord(char) == 0x7F for char in value):
        raise ValueError("Upstash API key must be a single-line token of at most 256 characters")
    return value


def validate_upstash_base_url(raw: str) -> str:
    value = str(raw or "").strip().rstrip("/") or "https://us-east-1.box.upstash.com"
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or "/" in parsed.path.strip("/"):
        raise ValueError("Upstash base URL must be an HTTPS origin such as https://us-east-1.box.upstash.com")
    return value


def validate_upstash_runtime(raw: str) -> str:
    value = str(raw or "python").strip().lower()
    if value not in _ALLOWED_RUNTIMES:
        raise ValueError(f"Upstash runtime must be one of: {', '.join(sorted(_ALLOWED_RUNTIMES))}")
    return value


def validate_upstash_size(raw: str) -> str:
    value = str(raw or "small").strip().lower()
    if value not in _ALLOWED_SIZES:
        raise ValueError(f"Upstash size must be one of: {', '.join(sorted(_ALLOWED_SIZES))}")
    return value


def upstash_box_name(session_key: str) -> str:
    """Deterministic, valid box name for a session so boxes survive restarts.

    Uses a stable SHA-256 digest (not Python's salted ``hash()``) so the same
    session maps to the same box across processes and deployments.
    """
    slug = re.sub(r"[^a-z0-9-]", "-", session_key.lower()).strip("-")[:32] or "session"
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:10]
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


class UpstashExecutionBackend:
    """Async client implementing the shared sandbox contract against one Upstash box."""

    def __init__(self, config: Any, *, box_name: str = "powerx-session") -> None:
        self.config = config
        self.base_url = validate_upstash_base_url(str(getattr(config, "base_url", "") or ""))
        self.api_key = validate_upstash_api_key(str(getattr(config, "api_key", "") or ""))
        self.runtime = validate_upstash_runtime(str(getattr(config, "runtime", "") or "python"))
        self.size = validate_upstash_size(str(getattr(config, "size", "") or "small"))
        self.ttl_s = max(60, min(int(getattr(config, "ttl_s", 3600) or 3600), 86_400))
        self.box_name = box_name if _NAME_RE.fullmatch(box_name) else "powerx-session"
        self.workspace = WORKSPACE
        # Box id resolved by the most recent ensure_box() call (for callers that
        # want to persist the mapping between operations).
        self.last_box_id: str = ""
        # "Perfect box" persistence: when True (the default) a finished task
        # must NOT destroy the box. Files written/read through the box persist
        # for the box lifetime, and the workspace is snapshotted into a
        # dedicated archive box so a fresh box (after expiry/recreation) is
        # restored with the session's files. Admins can opt out via
        # upstashPersistWorkspace in execution settings.
        self.persist_workspace = bool(getattr(config, "persist_workspace", True))

    # ------------------------------------------------------------------ HTTP

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise UpstashError("Upstash API key is not configured")
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: int = 120,
        raw_response: bool = False,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(),
                json=body,
                timeout=aiohttp.ClientTimeout(total=timeout + 30),
            ) as resp:
                if raw_response:
                    payload_bytes = await resp.read()
                    if resp.status >= 400:
                        raise UpstashError(
                            f"{method} {path} failed with HTTP {resp.status}: "
                            f"{payload_bytes[:200].decode('utf-8', 'replace')}"
                        )
                    return payload_bytes
                text = await resp.text()
                try:
                    data = json.loads(text) if text else {}
                except ValueError:
                    data = {"raw": text}
                if resp.status >= 400:
                    detail = str(data.get("error") or data.get("message") or text)[:300]
                    if resp.status in (401, 403):
                        # A rejected credential is NOT an endpoint problem. Say so
                        # explicitly: the classic failure mode here was the admin
                        # rotating the API key and every request then reading as
                        # "the endpoint has changed or is not correct".
                        raise UpstashError(
                            f"Upstash rejected the configured API key (HTTP {resp.status}) for {method} {path}: "
                            f"{detail}. The Upstash endpoint itself is unchanged; the admin-configured "
                            "Upstash API key is invalid, expired, or was just rotated. Save the new key "
                            "in Admin -> Execution settings and retry."
                        )
                    # Upstash reports *missing* files as an opaque HTTP 500; turn
                    # that specific shape into a typed, recoverable error so reads
                    # of not-yet-written paths don't blow up the whole operation.
                    if resp.status == 500 and _looks_like_missing_file(detail):
                        raise UpstashFileNotFound(
                            f"{method} {path}: file not found ({detail})"
                        )
                    raise UpstashError(f"{method} {path} failed with HTTP {resp.status}: {detail}")
                return data
        except aiohttp.ClientError as exc:
            raise UpstashError(f"Upstash Box transport error: {type(exc).__name__}") from None
        except (asyncio.TimeoutError, TimeoutError) as exc:
            # A wall-clock timeout on ANY Upstash call must surface as a typed
            # UpstashError (never a bare asyncio.TimeoutError) so callers retry
            # coherently instead of seeing a foreign exception type.
            raise UpstashError(
                f"Upstash Box request timed out (budget={timeout + 30}s)"
            ) from None

    # --------------------------------------------------------------- lifecycle

    async def find_box(self, session: aiohttp.ClientSession) -> dict[str, Any] | None:
        """Return metadata for a live (non-deleted) box carrying our name, if any."""
        data = await self._request(session, "GET", "/v2/box", timeout=30)
        boxes = data.get("boxes") if isinstance(data, dict) else None
        if not isinstance(boxes, list):
            boxes = data if isinstance(data, list) else []
        for box in boxes:
            if not isinstance(box, dict):
                continue
            if str(box.get("name") or "") != self.box_name:
                continue
            status = str(box.get("status") or "").lower()
            if status in {"deleted", "deleting", "error"}:
                continue
            return box
        return None

    # Upstash keeps a box's filesystem when it stops/expires (idle timeout);
    # restarting the existing box preserves it. Recreating from a template
    # wipes the workspace, so restart must always be tried first.
    _RESUMABLE_STATUSES = {"stopped", "paused", "suspended", "expired", "sleeping"}

    async def _restart_box(self, session: aiohttp.ClientSession, box_id: str) -> None:
        """Best-effort restart of a stopped box, preserving its filesystem."""
        last_exc: Exception | None = None
        for path in (f"/v2/box/{box_id}/restart", f"/v2/box/{box_id}/start"):
            try:
                await self._request(session, "POST", path, timeout=90)
                return
            except UpstashError as exc:
                detail = str(exc)
                if "404" in detail or "409" in detail or "already" in detail.lower():
                    continue
                last_exc = exc
        if last_exc is not None:
            raise last_exc

    async def wait_ready(self, session: aiohttp.ClientSession, box_id: str, timeout: int = 120) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        restarted = False
        while True:
            data = await self._request(session, "GET", f"/v2/box/{box_id}", timeout=30)
            status = str(data.get("status") or "").lower()
            if status in {"running", "idle", "ready", "active"}:
                return
            if status in self._RESUMABLE_STATUSES and not restarted:
                restarted = True
                await self._restart_box(session, box_id)
                if asyncio.get_running_loop().time() >= deadline:
                    deadline = asyncio.get_running_loop().time() + 90
                continue
            if asyncio.get_running_loop().time() >= deadline:
                raise UpstashError(f"Upstash box {box_id} was not ready in time (status={status or 'unknown'})")
            await asyncio.sleep(1)

    async def ensure_box(self, session: aiohttp.ClientSession) -> str:
        # Fast path: verify the exact box we already track (the caller seeds
        # last_box_id from the persisted store) instead of listing every box in
        # the account, which is slow on busy accounts and the main startup cost
        # after a box was killed.
        if self.last_box_id:
            box_id = self.last_box_id
            try:
                data = await self._request(session, "GET", f"/v2/box/{box_id}", timeout=30)
                status = str(data.get("status") or "").lower()
                if status not in {"deleted", "deleting", "error"}:
                    await self.wait_ready(session, box_id, timeout=90)
                    return box_id
            except UpstashError:
                pass  # stale id — fall through to the list/create path
            self.last_box_id = ""
        existing = await self.find_box(session)
        if existing is not None:
            box_id = str(existing.get("id") or "")
            if box_id:
                await self.wait_ready(session, box_id, timeout=90)
                self.last_box_id = box_id
                return box_id
        body: dict[str, Any] = {
            "runtime": self.runtime,
            "size": self.size,
            "name": self.box_name,
        }
        # Best-effort server-side backstop: even if our own cleanup is missed
        # (process crash, forgotten reset), ask Upstash to reap the box once it
        # expires. The API accepts unknown fields silently on some revisions.
        body["ttl"] = self.ttl_s
        created = await self._request(session, "POST", "/v2/box", body=body, timeout=90)
        box_id = str(created.get("id") or "")
        if not box_id:
            raise UpstashError("Upstash Box create returned no box id")
        await self.wait_ready(session, box_id, timeout=120)
        self.last_box_id = box_id
        if self.persist_workspace:
            # A brand-new box is empty. Restore the last workspace snapshot so a
            # session's files survive box expiry/recreation instead of every
            # restart starting from a wiped workspace.
            try:
                await self.restore_workspace()
            except Exception:
                pass  # best-effort: a missing/unreadable snapshot is not fatal
        return box_id

    async def delete_box(self, box_id: str) -> None:
        """Kill the sandbox immediately (used by reset / explicit-wipe cleanup)."""
        async with aiohttp.ClientSession() as session:
            await self._request(session, "DELETE", f"/v2/box/{box_id}", timeout=60)

    # ---------------------------------------------------------------- persist

    _SNAPSHOT_STAGED = ".px-snapshot.tgz"
    _RESTORE_STAGED = ".px-restore.tgz"
    _SNAPSHOT_MAX_BYTES = 150 * 1024 * 1024

    def _archive_box_name(self) -> str:
        """Dedicated long-lived box that stores workspace snapshots."""
        digest = hashlib.sha256(f"archive:{self.box_name}".encode("utf-8")).hexdigest()[:10]
        return f"px-archive-{digest}"

    def _archive_backend(self) -> "UpstashExecutionBackend":
        archive = UpstashExecutionBackend(self.config, box_name=self._archive_box_name())
        # Snapshots must outlive the ephemeral session boxes, so the archive
        # box uses the longest TTL the API accepts. It must never snapshot
        # itself (that would recurse forever).
        archive.ttl_s = 86_400
        archive.persist_workspace = False
        return archive

    async def _read_box_bytes(
        self,
        session: aiohttp.ClientSession,
        box_id: str,
        path: str,
        *,
        timeout: int = 120,
    ) -> bytes | None:
        """Read one binary file from a box via the base64 files/read encoding."""
        target = _safe_path(path, self.workspace)
        data = await self._request(
            session,
            "GET",
            f"/v2/box/{box_id}/files/read?path={quote(target, safe='')}&encoding=base64",
            timeout=timeout,
        )
        if isinstance(data, dict) and data.get("content"):
            try:
                return base64.b64decode(str(data["content"]), validate=False)
            except Exception:
                return None
        return None

    async def snapshot_workspace(self) -> bool:
        """Tar the workspace and store the archive in the dedicated archive box.

        Called instead of box deletion when persistence is enabled, so writes
        and reads made during a task survive task end, agent restarts, and even
        box recreation (the snapshot is restored when a fresh box is created).
        Returns True when a snapshot was stored.
        """
        if not self.persist_workspace:
            return False
        staged = f"{self.workspace}/{self._SNAPSHOT_STAGED}"
        async with aiohttp.ClientSession() as session:
            box_id = await self.ensure_box(session)
            await self._exec(
                session,
                box_id,
                f"rm -f {shlex.quote(staged)} && tar czf {shlex.quote(staged)} "
                f"-C {shlex.quote(self.workspace)} "
                f"--exclude=./{self._SNAPSHOT_STAGED} --exclude=./{self._RESTORE_STAGED} . "
                "2>/dev/null || true",
                120,
            )
            data = await self._read_box_bytes(session, box_id, staged, timeout=90)
            if not data or len(data) > self._SNAPSHOT_MAX_BYTES:
                return False
            archive = self._archive_backend()
            await archive.ensure_box(session)
            await archive.write_bytes(f"{archive.workspace}/snapshots/{self.box_name}.tgz", data)
        return True

    async def restore_workspace(self) -> bool:
        """Restore the last workspace snapshot from the archive box (best effort)."""
        if not self.persist_workspace:
            return False
        try:
            archive = self._archive_backend()
            async with aiohttp.ClientSession() as session:
                # Fast skip: if the archive box does not exist yet, no snapshot
                # was ever stored. Return WITHOUT creating one — creating a box
                # here just to learn there is nothing to restore added minutes
                # to every cold start, which users experienced as "the AI hangs
                # in the sandbox" on the first command after box recreation.
                existing_archive = await archive.find_box(session)
                if existing_archive is None:
                    return False
                archive_id = str(existing_archive.get("id") or "")
                if not archive_id:
                    return False
                data = await archive._read_box_bytes(
                    session,
                    archive_id,
                    f"{archive.workspace}/snapshots/{self.box_name}.tgz",
                    timeout=90,
                )
                if not data or len(data) > self._SNAPSHOT_MAX_BYTES:
                    return False
                box_id = await self.ensure_box(session)
                staged = f"{self.workspace}/{self._RESTORE_STAGED}"
                await self.write_bytes(staged, data)
                await self._exec(
                    session,
                    box_id,
                    f"tar xzf {shlex.quote(staged)} -C {shlex.quote(self.workspace)} "
                    f"2>/dev/null || true; rm -f {shlex.quote(staged)}",
                    180,
                )
        except UpstashError:
            return False
        return True

    # ------------------------------------------------------------------ exec

    async def _exec(self, session: aiohttp.ClientSession, box_id: str, command: str, timeout: int) -> dict[str, Any]:
        # Cap the effective budget below Upstash's 5-minute server-side cap and
        # enforce it INSIDE the box with coreutils `timeout` (the sync endpoint
        # has no timeout parameter). This is what keeps a runaway or never-
        # exiting command from holding the HTTP request (and the AI's turn)
        # open for many minutes: the shell wrapper kills it at the budget and
        # the endpoint returns promptly with exit code 124.
        bounded = max(1, min(int(timeout), _MAX_SYNC_EXEC_TIMEOUT))
        wrapped = f"timeout {bounded} sh -c {shlex.quote(command)}"
        result = await self._request(
            session,
            "POST",
            f"/v2/box/{box_id}/exec",
            body={"command": ["sh", "-c", wrapped]},
            timeout=bounded + 30,
        )
        return result if isinstance(result, dict) else {"output": str(result)}

    @staticmethod
    def _render(result: dict[str, Any]) -> str:
        stdout = str(result.get("output") or "")
        stderr = str(result.get("error") or "")
        code = result.get("exit_code")
        text = stdout
        if stderr:
            text += f"\n[stderr]\n{stderr}"
        if code is not None:
            text += f"\n[exit_code={code}]"
        return _truncate(text) or "(no output)"

    async def run(self, command: str, *, timeout: int = 120) -> str:
        command = str(command or "").strip()
        if not command:
            raise ValueError("command is required")
        if len(command) > _MAX_COMMAND_CHARS:
            raise ValueError(f"command exceeds {_MAX_COMMAND_CHARS} characters")
        async with aiohttp.ClientSession() as session:
            box_id = await self.ensure_box(session)
            result = await self._exec(session, box_id, command, timeout)
        return self._render(result)

    async def read(self, path: str) -> str:
        target = _safe_path(path, self.workspace)
        async with aiohttp.ClientSession() as session:
            box_id = await self.ensure_box(session)
            try:
                data = await self._request(
                    session, "GET", f"/v2/box/{box_id}/files/read?path={quote(target, safe='')}", timeout=90
                )
            except UpstashFileNotFound:
                # A read of a path that was never written is not an error worth
                # surfacing to the model as a failure — treat it as empty content
                # so callers (and write-then-read flows) proceed normally.
                return ""
        if isinstance(data, dict) and "content" in data:
            return _truncate(str(data.get("content") or ""))
        # Not text-decodable server-side: fall back to a base64 dump via exec.
        b64 = await self.run(f"base64 {shlex.quote(target)}")
        return _truncate(b64)

    async def write(self, path: str, content: str) -> None:
        target = _safe_path(path, self.workspace)
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError(f"content exceeds {_MAX_CONTENT_CHARS} characters")
        parent = posixpath.dirname(target)
        # Two attempts: a cold/expired box can reject the first call with a
        # transient 5xx or transport error; re-ensuring (which may create a fresh
        # box) and retrying once recovers those without bothering the user.
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                async with aiohttp.ClientSession() as session:
                    box_id = await self.ensure_box(session)
                    if parent and parent != "/":
                        await self._exec(session, box_id, f"mkdir -p {shlex.quote(parent)}", 60)
                    await self._request(
                        session,
                        "POST",
                        f"/v2/box/{box_id}/files/write",
                        body={"path": target, "content": content},
                        timeout=90,
                    )
                # Verify the write actually landed; Upstash occasionally reports
                # success while the box is mid-restart, which previously surfaced
                # to users as a silent "could not write".
                probe = await self.read(target)
                if probe == "" and content != "":
                    raise UpstashError("write verification failed (empty read-back)")
                return
            except UpstashFileNotFound:
                return  # empty write to an absent path is fine
            except UpstashError as exc:
                last_error = exc
                # Force ensure_box to re-resolve/create on the next attempt.
                self.last_box_id = ""
                await asyncio.sleep(1.5)
        assert last_error is not None
        raise last_error

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = _safe_path(path, self.workspace)
        if len(data) > _MAX_UPLOAD_BYTES:
            raise ValueError("file exceeds 200 MiB")
        parent = posixpath.dirname(target)
        encoded = base64.b64encode(data).decode("ascii")
        async with aiohttp.ClientSession() as session:
            box_id = await self.ensure_box(session)
            if parent and parent != "/":
                await self._exec(session, box_id, f"mkdir -p {shlex.quote(parent)}", 60)
            await self._request(
                session,
                "POST",
                f"/v2/box/{box_id}/files/write",
                body={"path": target, "content": encoded, "encoding": "base64"},
                timeout=180,
            )

    async def list(self, path: str) -> str:
        target = _safe_path(path or self.workspace, self.workspace)
        listing_root = target if target == self.workspace else (posixpath.dirname(target) or self.workspace)
        command = f"find {shlex.quote(listing_root)} -maxdepth 2 -printf '%y %p\\n' 2>/dev/null | head -200"
        return await self.run(command, timeout=60)

    async def fetch_url(self, url: str, dest_path: str, *, timeout: int = 150) -> str:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        allowed = parsed.scheme == "https" and (host in _ALLOWED_FETCH_HOSTS or host.endswith(".gofile.io"))
        if not allowed:
            raise ValueError("url must be an HTTPS onlyfiles.com or gofile.io URL")
        dest = _safe_path(dest_path, self.workspace)
        command = (
            f"mkdir -p {shlex.quote(posixpath.dirname(dest))} && "
            f"curl -fsSL --max-time {int(timeout)} -o {shlex.quote(dest)} {shlex.quote(url)} && "
            f"stat -c %s {shlex.quote(dest)}"
        )
        out = await self.run(command, timeout=min(timeout + 30, _MAX_TIMEOUT))
        if "[exit_code=" in out and "exit_code=0" not in out:
            raise UpstashError(f"remote fetch failed: {out[:300]}")
        return dest

    async def download(self, remote_path: str, local_path: Any) -> Any:
        """Download a box file to a local Path via base64 through exec."""
        from pathlib import Path

        target = _safe_path(remote_path, self.workspace)
        probe = await self.run(f"stat -c %s {shlex.quote(target)}", timeout=30)
        size_text = probe.splitlines()[0].strip() if probe else ""
        try:
            size = int(size_text)
        except ValueError:
            # A cold box can answer the first stat with an empty/failed result;
            # re-ensure the box (recreating if it expired) and try once more
            # before declaring failure. This is what used to read as
            # "could not determine remote file size".
            self.last_box_id = ""
            await asyncio.sleep(1.5)
            probe = await self.run(f"stat -c %s {shlex.quote(target)}", timeout=30)
            size_text = probe.splitlines()[0].strip() if probe else ""
            try:
                size = int(size_text)
            except ValueError:
                raise UpstashFileNotFound(
                    f"remote file not found or unreadable: {target}"
                ) from None
        if size > _MAX_DOWNLOAD_BYTES:
            raise ValueError("remote artifact exceeds 50 MiB")
        encoded = await self.run(f"base64 {shlex.quote(target)}", timeout=120)
        payload = re.sub(r"\n\[exit_code=\d+\]\s*$", "", encoded).strip()
        try:
            raw = base64.b64decode(payload, validate=False)
        except Exception:
            raise UpstashError("failed to decode downloaded artifact") from None
        destination = Path(str(local_path)).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        return destination

    async def install_packages(self, packages: list[str], *, timeout: int = 600) -> str:
        cleaned = [item for item in packages if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.:@~=-]{0,127}", item)]
        if not cleaned:
            raise ValueError("no valid package names supplied")

        def _manager_clause(quoted: str) -> str:
            return (
                "if command -v apt-get >/dev/null 2>&1; then apt-get install -y -qq "
                + quoted
                + "; elif command -v apk >/dev/null 2>&1; then apk add --no-cache "
                + quoted
                + "; elif command -v dnf >/dev/null 2>&1; then dnf install -y "
                + quoted
                + "; else echo 'no supported package manager found' >&2; exit 127; fi"
            )

        # The sync exec endpoint is capped at 5 minutes server-side, so one
        # giant "apt-get update && apt-get install a b c ..." command could be
        # killed mid-flight and leave the caller hanging on a partial install.
        # Instead: refresh the index once (bounded), then install each package
        # as its own bounded exec and stop at the first real failure. Every
        # request stays far below the cap, which also keeps long installs from
        # reading as an infinite hang to the model.
        per_call = _MAX_SYNC_EXEC_TIMEOUT
        update_cmd = (
            "export DEBIAN_FRONTEND=noninteractive; "
            "if command -v apt-get >/dev/null 2>&1; then apt-get update -qq; "
            "elif command -v apk >/dev/null 2>&1; then true; "
            "elif command -v dnf >/dev/null 2>&1; then true; "
            "fi"
        )
        chunks: list[str] = []
        for item in cleaned:
            chunks.append(_manager_clause(shlex.quote(item)))
        outputs: list[str] = []
        update_out = await self.run(update_cmd, timeout=per_call)
        if "[exit_code=" in update_out and "exit_code=0" not in update_out:
            return f"Upstash Box package installation result:\n[index refresh]\n{update_out}"
        for index, clause in enumerate(chunks):
            out = await self.run(clause, timeout=per_call)
            outputs.append(f"[{cleaned[index]}]\n{out}")
            if "[exit_code=" in out and "exit_code=0" not in out:
                outputs.append("[remaining packages skipped because an install failed]")
                break
        return f"Upstash Box package installation result:\n" + "\n".join(outputs)

    async def test_connection(self) -> dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            box_id = await self.ensure_box(session)
            result = await self._exec(session, box_id, "uname -a", 60)
        return {
            "ok": result.get("exit_code") in (0, None),
            "backend": "upstash",
            "box_id": box_id,
            "platform": str(result.get("output") or "").strip()[:200],
        }

    async def reset(self, box_id: str | None = None) -> None:
        """Delete the given box (or our named box) — kills the sandbox now."""
        async with aiohttp.ClientSession() as session:
            target = box_id or ""
            if not target:
                existing = await self.find_box(session)
                target = str((existing or {}).get("id") or "")
            if target:
                await self._request(session, "DELETE", f"/v2/box/{target}", timeout=60)
