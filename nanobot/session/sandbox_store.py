"""Session state that lives in the execution sandbox, not on the host.

WHY THIS EXISTS
---------------
``JsonlSessionStore`` keeps every session's history on the host's filesystem.
That makes the host *stateful*: replace the container, or run a second instance,
and the conversations go with it. It also means the host owns the durable copy of
a conversation while the sandbox owns the files that conversation is about.

This store inverts that. The sessions directory is mirrored into the execution
sandbox and **the sandbox is the durable copy**; the host keeps a local mirror so
reads stay fast. Then the host can be replaced freely — it rehydrates from the
sandbox on a miss — which is the prerequisite for running the host as a skeleton
and the sandbox as the runtime.

THE SYNC/ASYNC SEAM IS THE REAL CONSTRAINT
------------------------------------------
The ``SessionStore`` protocol is **synchronous** and is called straight from the
agent loop (``loop.py``: ``self.sessions.save(session)``, several call sites), while
the sandbox transport is **asynchronous**. Blocking the event loop on a network
round trip for every save would serialize every session behind one write — worse
than the problem this solves. So:

* a **write is local first**, then pushed by a background thread — ``save()`` never
  touches the network and never blocks the loop;
* a **read is local**; only a MISS pulls that one session from the sandbox, and
  that pull is the one place a caller may wait on the network;
* ``list_sessions`` merges the local list with the sandbox's, once.

The transport runs on its own daemon thread with its own event loop, so a sync
caller never has to be "inside a running loop" (which is exactly the trap
``workspace_bridge.stage_from_sandbox`` documents) and never awaits the caller's.

Enable it with ``NANOBOT_SESSION_STORE=sandbox``. Default off: with the flag
unset nothing here is constructed and persistence is byte-for-byte the old
behaviour.

KNOWN LIMITS
------------
* One shared sandbox. The transport resolves the *ambient* executor, so all
  sessions mirror into the one sandbox that executor names, not one sandbox per
  session. That is deliberate for the first step (get state off the host's single
  volume); per-session sandboxes need the session key to be known before the
  session is loaded, which is a separate problem.
* Pushes are best-effort. A failed push is logged and retried on the next write
  to that session; ``flush()`` reports whether the queue drained.
"""

from __future__ import annotations

import asyncio
import base64
import os
import queue
import shlex
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from nanobot.session.manager import (
    JsonlSessionStore,
    Session,
    SessionInfo,
    SessionMetadataPayload,
    SessionPayload,
)

#: Default directory inside the sandbox that holds the mirrored sessions.
DEFAULT_REMOTE_ROOT = "$HOME/.nanobot/sessions"

#: Bound on how many sessions one rehydrate pass will pull. A first boot against
#: a large history must not turn into an unbounded transfer.
MAX_HYDRATE_FILES = 200

#: Refuse to push a single session file larger than this. A session file is a
#: conversation, not an artifact; anything past this is a bug, not a session.
MAX_PUSH_BYTES = 64 * 1024 * 1024

#: The file APIs cap *text* writes well below what a long conversation reaches
#: (120 KB on most backends), so payloads above this go through the byte API.
_TEXT_WRITE_LIMIT = 100_000


class SandboxTransport(Protocol):
    """The four file operations the store needs from a sandbox."""

    def push(self, local: Path, remote: str) -> bool: ...

    def pull(self, remote: str, local: Path) -> bool: ...

    def remove(self, remote: str) -> bool: ...

    def list_remote(self, remote_dir: str) -> list[str]: ...


class _LoopThread:
    """A daemon thread owning an asyncio loop, for synchronous callers.

    Created once per transport. ``call`` blocks the *calling* thread until the
    coroutine finishes — which is safe because callers are either the store's own
    background worker or a cold-read path, never the agent loop's hot path.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sandbox-session-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def call(self, coro: Any, timeout: float = 120.0) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)


class RemoteSandboxTransport:
    """A transport backed by the configured execution sandbox.

    Reuses ``workspace_bridge``'s resolver and its file primitives rather than
    adding a fourth byte-mover, so per-backend knowledge (Novita's text-only
    ``files.read``, the backend ``download`` path) stays in one place.
    """

    def __init__(self, *, timeout: float = 120.0) -> None:
        self._loop = _LoopThread()
        self._timeout = timeout
        self._ready_dirs: set[str] = set()
        self._dirs_lock = threading.Lock()

    # -- async internals --------------------------------------------------- #
    async def _ensure_parent(self, remote: str) -> None:
        """Best-effort ``mkdir -p`` of *remote*'s parent, once per directory.

        Backends that write through their own file API already create parents;
        the exec plane and the native SDK may not, and one extra command per
        *directory* is cheaper than a failed write.
        """
        parent = remote.rsplit("/", 1)[0] if "/" in remote else ""
        if not parent or parent == "/":
            return
        with self._dirs_lock:
            if parent in self._ready_dirs:
                return
        from nanobot.agent.tools.workspace_bridge import run_remote

        try:
            await run_remote(f"mkdir -p {shlex.quote(parent)}", timeout=60)
        except Exception as exc:  # noqa: BLE001 - a failed mkdir is not fatal
            logger.debug("sandbox session mkdir failed ({}): {}", parent, exc)
            return
        with self._dirs_lock:
            self._ready_dirs.add(parent)

    async def _write_bytes(self, remote: str, data: bytes, local: Path) -> bool:
        """Write through whichever byte API the resolved backend offers.

        Every backend in the tree exposes a different one, so the order is:
        the backend's own byte writer (all of Daytona/Freestyle/Runloop/Tenki/
        Upstash/Vercel have ``write_bytes``), then VPS's SFTP ``upload``, then
        the plain text ``write`` — and only the *native* Novita SDK path, whose
        file API is text-only, is capped by ``_TEXT_WRITE_LIMIT``.
        """
        from nanobot.agent.tools.workspace_bridge import resolve_remote_executor

        executor = await resolve_remote_executor()
        if not executor.available:
            return False
        if executor.native is not None:
            if len(data) > _TEXT_WRITE_LIMIT:
                return await self._write_via_exec(remote, data)
            text = data.decode("utf-8", errors="replace")
            await asyncio.to_thread(executor.native.files.write, remote, text)
            return True
        backend = executor.backend
        if backend is None:
            return False
        writer = getattr(backend, "write_bytes", None)
        if writer is not None:
            await writer(remote, data)
            return True
        uploader = getattr(backend, "upload", None)
        if uploader is not None:
            await uploader(str(local), remote, data)
            return True
        await backend.write(remote, data.decode("utf-8", errors="replace"))  # type: ignore[attr-defined]
        return True

    async def _write_via_exec(self, remote: str, data: bytes) -> bool:
        """Write through the exec plane as base64. The universal fallback."""
        from nanobot.agent.tools.workspace_bridge import run_remote

        await self._ensure_parent(remote)
        encoded = base64.b64encode(data).decode("ascii")
        command = f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(remote)}"
        ok, out = await run_remote(command, timeout=180)
        if not ok:
            logger.debug("sandbox session exec write failed ({}): {}", remote, out[:200])
        return ok

    async def _push(self, local: Path, remote: str) -> bool:
        try:
            data = local.read_bytes()
        except OSError:
            return False
        if len(data) > MAX_PUSH_BYTES:
            logger.warning(
                "sandbox session push skipped: {} is {} bytes (cap {})",
                local.name,
                len(data),
                MAX_PUSH_BYTES,
            )
            return False
        try:
            await self._ensure_parent(remote)
            return await self._write_bytes(remote, data, local)
        except Exception as exc:  # noqa: BLE001 - a failed push is retried next time
            logger.warning("sandbox session push failed ({}): {}", remote, exc)
            return False

    async def _pull(self, remote: str, local: Path) -> bool:
        from nanobot.agent.tools.workspace_bridge import fetch_remote_file

        try:
            data = await fetch_remote_file(remote)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sandbox session pull failed ({}): {}", remote, exc)
            return False
        if not data:
            return False
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_name(f".{local.name}.{os.getpid()}.pull")
            tmp.write_bytes(data)
            os.replace(tmp, local)
            return True
        except OSError:
            return False

    async def _remove(self, remote: str) -> bool:
        from nanobot.agent.tools.workspace_bridge import run_remote

        try:
            ok, _out = await run_remote(f"rm -f {shlex.quote(remote)}", timeout=60)
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.debug("sandbox session remove failed ({}): {}", remote, exc)
            return False

    async def _list(self, remote_dir: str) -> list[str]:
        from nanobot.agent.tools.workspace_bridge import run_remote

        ok, out = await run_remote(f"ls -1 {remote_dir} 2>/dev/null || true", timeout=60)
        if not ok and not out:
            return []
        return [
            line.strip() for line in (out or "").splitlines() if line.strip().endswith(".jsonl")
        ]

    # -- sync surface ------------------------------------------------------ #
    def push(self, local: Path, remote: str) -> bool:
        try:
            return bool(self._loop.call(self._push(local, remote), timeout=self._timeout))
        except Exception:  # noqa: BLE001 - timeout/transport failure
            return False

    def pull(self, remote: str, local: Path) -> bool:
        try:
            return bool(self._loop.call(self._pull(remote, local), timeout=self._timeout))
        except Exception:  # noqa: BLE001
            return False

    def remove(self, remote: str) -> bool:
        try:
            return bool(self._loop.call(self._remove(remote), timeout=self._timeout))
        except Exception:  # noqa: BLE001
            return False

    def list_remote(self, remote_dir: str) -> list[str]:
        try:
            return list(self._loop.call(self._list(remote_dir), timeout=self._timeout))
        except Exception:  # noqa: BLE001
            return []


class SandboxSessionStore:
    """A ``SessionStore`` whose durable copy lives in the execution sandbox.

    Local mirror, sandbox authority. Writes are local then queued; reads are
    local with a one-session pull on a miss.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        sessions_root: Path | None = None,
        remote_root: str | None = None,
        transport: SandboxTransport | None = None,
        mirror_root: Path | None = None,
    ) -> None:
        # The mirror is a real JsonlSessionStore, so every format decision (the
        # metadata line, provider state, atomic replace, fsync) stays in exactly
        # one implementation and this class only moves bytes.
        self._local = JsonlSessionStore(workspace, sessions_root=mirror_root or sessions_root)
        self._remote_root = (
            remote_root
            or os.getenv("NANOBOT_SANDBOX_SESSIONS_ROOT", "").strip()
            or DEFAULT_REMOTE_ROOT
        )
        self._transport = transport or RemoteSandboxTransport()
        self._hydrated = False
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        # Counts enqueued-but-not-finished work. ``queue.empty()`` is not a
        # "done" signal: the worker pops an item before it has pushed it, so an
        # emptiness check lets ``flush`` return while a push is still in flight.
        self._pending = 0
        self._idle = threading.Condition()
        self._worker = threading.Thread(
            target=self._drain, name="sandbox-session-push", daemon=True
        )
        self._worker.start()

    # -- accessors used by tests and callers ------------------------------- #
    @property
    def local(self) -> JsonlSessionStore:
        return self._local

    @property
    def remote_root(self) -> str:
        return self._remote_root

    # -- remote paths ------------------------------------------------------ #
    def _remote_path(self, key: str) -> str:
        name = self._local.get_session_path(key).name
        return f"{self._remote_root}/{name}"

    # -- background worker ------------------------------------------------- #
    def _enqueue(self, op: str, key: str) -> None:
        # Incremented before the put so the worker can never finish an item that
        # a waiting ``flush`` has not yet counted.
        with self._idle:
            self._pending += 1
        self._queue.put((op, key))

    def _drain(self) -> None:
        while True:
            try:
                op, key = self._queue.get()
            except Exception:  # pragma: no cover - queue is unbounded
                continue
            try:
                remote = self._remote_path(key)
                if op == "remove":
                    self._transport.remove(remote)
                    continue
                path = self._local.get_session_path(key)
                # A file that is gone locally is a delete, not a push: the delete
                # enqueued its own removal, so a missing file here is not an error.
                if path.exists():
                    self._transport.push(path, remote)
            except Exception as exc:  # noqa: BLE001 - the worker must never die
                logger.debug("sandbox session push worker: {}", exc)
            finally:
                with self._idle:
                    self._pending -= 1
                    self._idle.notify_all()
                self._queue.task_done()

    def flush(self, timeout: float = 60.0) -> bool:
        """Wait for queued work to finish. For tests and shutdown."""
        deadline = time.monotonic() + timeout
        with self._idle:
            while self._pending > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(remaining)
            return True

    # -- hydrate ----------------------------------------------------------- #
    def _hydrate_one(self, key: str) -> bool:
        path = self._local.get_session_path(key)
        if path.exists():
            return False
        return bool(self._transport.pull(self._remote_path(key), path))

    def _hydrate_all(self) -> None:
        with self._lock:
            if self._hydrated:
                return
            self._hydrated = True
        names = self._transport.list_remote(self._remote_root)
        for name in names[:MAX_HYDRATE_FILES]:
            path = self._local.sessions_dir / name
            if path.exists():
                continue
            self._transport.pull(f"{self._remote_root}/{name}", path)

    # -- SessionStore protocol --------------------------------------------- #
    def load(self, key: str) -> Session | None:
        session = self._local.load(key)
        if session is None and self._hydrate_one(key):
            session = self._local.load(key)
        return session

    def save(self, session: Session, *, fsync: bool = False) -> None:
        self._local.save(session, fsync=fsync)
        self._enqueue("push", session.key)

    def delete(self, key: str) -> bool:
        removed = self._local.delete(key)
        # The removal is enqueued even when the local file was already gone: the
        # sandbox is the durable copy, so the delete has to reach it either way.
        self._enqueue("remove", key)
        return removed

    def read(self, key: str) -> SessionPayload | None:
        payload = self._local.read(key)
        if payload is None and self._hydrate_one(key):
            payload = self._local.read(key)
        return payload

    def read_metadata(self, key: str) -> SessionMetadataPayload | None:
        payload = self._local.read_metadata(key)
        if payload is None and self._hydrate_one(key):
            payload = self._local.read_metadata(key)
        return payload

    def update_metadata(self, key: str, updates: dict[str, Any], *, fsync: bool = False) -> bool:
        changed = self._local.update_metadata(key, updates, fsync=fsync)
        if changed:
            self._enqueue("push", key)
        return changed

    def list_sessions(self) -> list[SessionInfo]:
        self._hydrate_all()
        return self._local.list_sessions()


def store_from_env(workspace: Path, *, sessions_root: Path | None = None) -> Any:
    """Return a ``SandboxSessionStore`` when ``NANOBOT_SESSION_STORE=sandbox``.

    The single decision point, so ``SessionManager`` construction stays unchanged
    and the feature is one environment variable away in either direction.
    """
    if os.getenv("NANOBOT_SESSION_STORE", "").strip().lower() != "sandbox":
        return None
    try:
        return SandboxSessionStore(workspace, sessions_root=sessions_root)
    except Exception as exc:  # noqa: BLE001 - never break boot over this
        logger.warning("sandbox session store unavailable, using local sessions: {}", exc)
        return None


__all__ = [
    "SandboxSessionStore",
    "SandboxTransport",
    "RemoteSandboxTransport",
    "store_from_env",
    "DEFAULT_REMOTE_ROOT",
    "MAX_HYDRATE_FILES",
    "MAX_PUSH_BYTES",
]
