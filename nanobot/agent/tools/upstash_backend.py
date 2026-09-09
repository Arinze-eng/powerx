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

# Upstash boxes keep their working files under this workspace root.
WORKSPACE = "/workspace/home"

_ALLOWED_RUNTIMES = {
    "python", "node", "golang", "ruby", "rust",
    "python-alpine", "node-alpine", "golang-alpine", "ruby-alpine", "rust-alpine",
}
_ALLOWED_SIZES = {"small", "medium", "large"}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

# Hosts whose URLs fetch_url may download (mirrors the VPS backend policy).
_ALLOWED_FETCH_HOSTS = {"tmpfiles.org", "gofile.io"}


class UpstashError(RuntimeError):
    """Raised when the Upstash Box API rejects a request."""


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
                    raise UpstashError(f"{method} {path} failed with HTTP {resp.status}: {detail}")
                return data
        except aiohttp.ClientError as exc:
            raise UpstashError(f"Upstash Box transport error: {type(exc).__name__}") from None

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

    async def wait_ready(self, session: aiohttp.ClientSession, box_id: str, timeout: int = 120) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            data = await self._request(session, "GET", f"/v2/box/{box_id}", timeout=30)
            status = str(data.get("status") or "").lower()
            if status in {"running", "idle", "ready", "active"}:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise UpstashError(f"Upstash box {box_id} was not ready in time (status={status or 'unknown'})")
            await asyncio.sleep(3)

    async def ensure_box(self, session: aiohttp.ClientSession) -> str:
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
        return box_id

    async def delete_box(self, box_id: str) -> None:
        """Kill the sandbox immediately (used by reset / end-of-task cleanup)."""
        async with aiohttp.ClientSession() as session:
            await self._request(session, "DELETE", f"/v2/box/{box_id}", timeout=60)

    # ------------------------------------------------------------------ exec

    async def _exec(self, session: aiohttp.ClientSession, box_id: str, command: str, timeout: int) -> dict[str, Any]:
        result = await self._request(
            session,
            "POST",
            f"/v2/box/{box_id}/exec",
            body={"command": ["sh", "-c", command]},
            timeout=min(timeout, _MAX_TIMEOUT) + 30,
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
            data = await self._request(
                session, "GET", f"/v2/box/{box_id}/files/read?path={quote(target, safe='')}", timeout=90
            )
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
            raise ValueError("url must be an HTTPS tmpfiles.org or gofile.io URL")
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
            raise UpstashError("could not determine remote file size") from None
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
        quoted = " ".join(shlex.quote(item) for item in cleaned)
        command = (
            "export DEBIAN_FRONTEND=noninteractive; "
            "if command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -y -qq "
            + quoted
            + "; elif command -v apk >/dev/null 2>&1; then apk add --no-cache "
            + quoted
            + "; elif command -v dnf >/dev/null 2>&1; then dnf install -y "
            + quoted
            + "; else echo 'no supported package manager found' >&2; exit 127; fi"
        )
        return await self.run(command, timeout=min(timeout, _MAX_TIMEOUT))

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
