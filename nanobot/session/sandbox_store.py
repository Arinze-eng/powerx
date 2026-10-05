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

WHY THE SANDBOX KEY IS CAPTURED, NOT LOOKED UP
---------------------------------------------
The background worker has no request context, and ``workspace_bridge`` resolves
the *ambient* sandbox from that context (``novita_sandbox._session_key()`` falls
back to ``"unknown"``). On the native Novita path the resolver is literally
``_STORE.get(key)`` — a lookup in a map of *live, in-process* sandbox handles — so
from a contextless thread it returns nothing, the executor is unavailable, and
every push would fail silently.

So the key is captured **on the calling thread**, which is inside the request
context, and handed to the worker. Resolution then bypasses the contextvar
entirely. When even that fails the store says so once, loudly: a mirror that
never reaches the sandbox must not look like success.

Enable it with ``NANOBOT_SESSION_STORE=sandbox``. Default off: with the flag
unset nothing here is constructed and persistence is byte-for-byte the old
behaviour.

KNOWN LIMITS
------------
* One sandbox per session key. Because the key is request-scoped, the mirror for
  a session follows that session's sandbox, so ``list_sessions`` can only merge
  what the *current* sandbox holds. A host-wide listing needs one host-owned
  sandbox for session state, which is the next step and a separate decision.
* Pushes are best-effort. A failed push is logged and retried on the next write
  to that session; ``flush()`` reports whether the queue drained.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import shlex
import threading
import time
from contextlib import suppress
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

#: Subdirectory of the sandbox's own workspace that holds the mirrored sessions.
DEFAULT_REMOTE_SUBDIR = ".nanobot/sessions"

#: Last resort when the backend reports no workspace at all. Every backend's file
#: API allows ``/tmp``; the others allow only their workspace, so a hardcoded
#: ``$HOME``-style root would be refused by the path guard on every one of them.
FALLBACK_REMOTE_ROOT = "/tmp/.nanobot/sessions"

#: The native Novita SDK handle exposes no workspace attribute, but its sandbox
#: workspace is a fixed path.
NATIVE_WORKSPACE = "/workspace"

#: Bound on how many sessions one rehydrate pass will pull. A first boot against
#: a large history must not turn into an unbounded transfer.
MAX_HYDRATE_FILES = 200

#: How many lines of each remote session the sidebar hydrate fetches. One record
#: is one physical line (``JsonlSessionStore.save`` writes ``json.dumps`` with no
#: indent), and every listing reader -- ``session_headers_nolock``,
#: ``read_metadata``, ``list_sessions`` -- stops at the FIRST line, the metadata
#: record carrying key, created_at and title. So one line is the whole of what a
#: listing needs; the second is there only in case a future format puts provider
#: state above the metadata. The conversation below it is NOT fetched: pulling
#: every body is what put an entire history in RAM on the first list, and it is
#: why deleting the persistent volume made the process drop back to a sane
#: footprint.
HEADER_HYDRATE_LINES = 2

#: Suffix marking a locally cached session as a *stub*: header and preview only,
#: with the body still in the sandbox. Deliberately not ``*.jsonl``, so the
#: session listings' glob never sees it. Anything that reads or rewrites a
#: session body must pull the real file first -- otherwise a stub could be served
#: to the model as if it were the whole conversation, or pushed back over it.
PARTIAL_SUFFIX = ".partial"


def is_session_metadata_head(data: bytes | bytearray) -> bool:
    """True when *data* starts with a readable session metadata record.

    The header is fetched through the shell exec plane, whose stdout can be
    capped or decorated, so a header that does not parse is not trusted: the
    hydrate falls back to a whole-file pull for that session rather than write a
    stub that would hide the chat from the sidebar. The rule matches the readers
    exactly -- ``read_metadata`` returns nothing unless the first non-blank
    record is the metadata one.
    """
    if not data:
        return False
    for raw in bytes(data).decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            return False
        return isinstance(record, dict) and record.get("_type") == "metadata"
    return False

#: Refuse to push a single session file larger than this. A session file is a
#: conversation, not an artifact; anything past this is a bug, not a session.
MAX_PUSH_BYTES = 64 * 1024 * 1024

#: The file APIs cap *text* writes well below what a long conversation reaches
#: (120 KB on most backends), so payloads above this go through the byte API.
_TEXT_WRITE_LIMIT = 100_000


def sandbox_key() -> str | None:
    """The sandbox key for the *current* request, or ``None`` outside one.

    Called on the calling thread, never from the worker: this is the whole point
    of capturing it. ``None`` means "no context", which the transport reports
    honestly rather than resolving to a sandbox that does not exist —
    ``novita_sandbox._session_key()`` answers the literal ``"unknown"`` when
    there is no request context, and that is a sentinel, never a real key.
    """
    try:
        from nanobot.agent.tools.novita_sandbox import _session_key  # noqa: PLC2701
    except Exception:  # noqa: BLE001 - no sandbox stack installed
        return None
    try:
        key = _session_key()
    except Exception:  # noqa: BLE001
        return None
    if not key or key == "unknown":
        return None
    return key


class SandboxTransport(Protocol):
    """The four file operations the store needs from a sandbox.

    Every method takes the sandbox *key* captured on the calling thread, because
    the transport's own thread cannot discover it.
    """

    def push(self, local: Path, remote: str, key: str | None = None) -> bool: ...

    def pull(self, remote: str, local: Path, key: str | None = None) -> bool: ...

    def remove(self, remote: str, key: str | None = None) -> bool: ...

    def list_remote(self, remote_dir: str, key: str | None = None) -> list[str]: ...

    def pull_head(self, remote: str, lines: int, key: str | None = None) -> bytes: ...


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
        self._root: str | None = None
        self._root_lock = threading.Lock()
        self._warned_unavailable = False

    # -- executor ----------------------------------------------------------- #
    async def _executor(self, key: str | None) -> Any:
        from nanobot.agent.tools.workspace_bridge import resolve_remote_executor

        try:
            return await resolve_remote_executor(session_key=key)
        except Exception:  # noqa: BLE001 - absence of a backend is not fatal
            return None

    def _warn_unavailable(self, key: str | None) -> None:
        """Say once that the mirror is not reaching anywhere."""
        if self._warned_unavailable:
            return
        self._warned_unavailable = True
        logger.warning(
            "sandbox session store: no sandbox is reachable (key={}); sessions are "
            "being written locally only",
            key or "ambient",
        )

    # -- remote root -------------------------------------------------------- #
    async def _resolve_root(self, key: str | None) -> str:
        """Put the mirror inside the sandbox's *own* workspace.

        Every backend's file API refuses a path outside its workspace (Freestyle
        additionally allows ``/home/ubuntu`` and ``/tmp``; VPS requires its
        configured ``workspace_dir``), so the root has to be derived from the
        backend rather than assumed. ``$HOME`` is deliberately not used: it is a
        shell expansion, and no path guard ever runs a shell.
        """
        executor = await self._executor(key)
        if executor is None:
            return FALLBACK_REMOTE_ROOT
        backend = executor.backend
        candidates: list[Any] = [
            getattr(backend, "workspace", None),
            getattr(getattr(backend, "config", None), "workspace_dir", None),
            getattr(backend, "_resolved_workspace", None),
        ]
        if executor.native is not None:
            candidates.append(NATIVE_WORKSPACE)
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip().startswith("/"):
                return f"{candidate.strip().rstrip('/')}/{DEFAULT_REMOTE_SUBDIR}"
        return FALLBACK_REMOTE_ROOT

    def root(self, key: str | None = None) -> str:
        """The resolved remote root, computed once."""
        with self._root_lock:
            if self._root is not None:
                return self._root
        resolved: str | None = None
        try:
            resolved = self._loop.call(self._resolve_root(key), timeout=self._timeout)
        except Exception:  # noqa: BLE001 - a failed resolve must not raise
            resolved = None
        with self._root_lock:
            if self._root is None:
                self._root = (
                    resolved if isinstance(resolved, str) and resolved else FALLBACK_REMOTE_ROOT
                )
            return self._root

    # -- async internals --------------------------------------------------- #
    async def _ensure_parent(self, remote: str, executor: Any) -> None:
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
            await run_remote(f"mkdir -p {shlex.quote(parent)}", timeout=60, executor=executor)
        except Exception as exc:  # noqa: BLE001 - a failed mkdir is not fatal
            logger.debug("sandbox session mkdir failed ({}): {}", parent, exc)
            return
        with self._dirs_lock:
            self._ready_dirs.add(parent)

    async def _write_bytes(self, remote: str, data: bytes, local: Path, executor: Any) -> bool:
        """Write through whichever byte API the resolved backend offers.

        Every backend in the tree exposes a different one, so the order is:
        the backend's own byte writer (all of Daytona/Freestyle/Runloop/Tenki/
        Upstash/Vercel have ``write_bytes``), then VPS's SFTP ``upload``, then
        the plain text ``write`` — and only the *native* Novita SDK path, whose
        file API is text-only, is capped by ``_TEXT_WRITE_LIMIT``.
        """
        if executor is None or not executor.available:
            return False
        if executor.native is not None:
            if len(data) > _TEXT_WRITE_LIMIT:
                return await self._write_via_exec(remote, data, executor)
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

    async def _write_via_exec(self, remote: str, data: bytes, executor: Any) -> bool:
        """Write through the exec plane as base64. The universal fallback."""
        from nanobot.agent.tools.workspace_bridge import run_remote

        await self._ensure_parent(remote, executor)
        encoded = base64.b64encode(data).decode("ascii")
        command = f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(remote)}"
        ok, out = await run_remote(command, timeout=180, executor=executor)
        if not ok:
            logger.debug("sandbox session exec write failed ({}): {}", remote, out[:200])
        return ok

    async def _push(self, local: Path, remote: str, key: str | None) -> bool:
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
        executor = await self._executor(key)
        if executor is None or not executor.available:
            self._warn_unavailable(key)
            return False
        try:
            await self._ensure_parent(remote, executor)
            return await self._write_bytes(remote, data, local, executor)
        except Exception as exc:  # noqa: BLE001 - a failed push is retried next time
            logger.warning("sandbox session push failed ({}): {}", remote, exc)
            return False

    async def _pull(self, remote: str, local: Path, key: str | None) -> bool:
        from nanobot.agent.tools.workspace_bridge import fetch_remote_file

        executor = await self._executor(key)
        if executor is None or not executor.available:
            self._warn_unavailable(key)
            return False
        try:
            data = await fetch_remote_file(remote, executor=executor)
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

    async def _remove(self, remote: str, key: str | None) -> bool:
        from nanobot.agent.tools.workspace_bridge import run_remote

        executor = await self._executor(key)
        if executor is None or not executor.available:
            self._warn_unavailable(key)
            return False
        try:
            ok, _out = await run_remote(
                f"rm -f {shlex.quote(remote)}", timeout=60, executor=executor
            )
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.debug("sandbox session remove failed ({}): {}", remote, exc)
            return False

    async def _list(self, remote_dir: str, key: str | None) -> list[str]:
        from nanobot.agent.tools.workspace_bridge import run_remote

        executor = await self._executor(key)
        if executor is None or not executor.available:
            self._warn_unavailable(key)
            return []
        ok, out = await run_remote(
            f"ls -1 {remote_dir} 2>/dev/null || true", timeout=60, executor=executor
        )
        if not ok and not out:
            return []
        return [
            line.strip() for line in (out or "").splitlines() if line.strip().endswith(".jsonl")
        ]

    async def _head(self, remote: str, lines: int, key: str | None) -> bytes:
        """The first *lines* lines of *remote*, and nothing else.

        Used by the listing hydrate, which needs a session's metadata record and
        a preview but never its conversation. Reading it through the exec plane
        is what keeps the body in the sandbox instead of in this process.
        """
        from nanobot.agent.tools.workspace_bridge import run_remote

        executor = await self._executor(key)
        if executor is None or not executor.available:
            self._warn_unavailable(key)
            return b""
        head_lines = max(1, int(lines))
        try:
            ok, out = await run_remote(
                f"head -n {head_lines} {shlex.quote(remote)} 2>/dev/null || true",
                timeout=60,
                executor=executor,
            )
        except Exception as exc:  # noqa: BLE001 - a failed head reads as a miss
            logger.debug("sandbox session head failed ({}): {}", remote, exc)
            return b""
        if not ok and not out:
            return b""
        return (out or "").encode("utf-8")

    # -- sync surface ------------------------------------------------------ #
    def push(self, local: Path, remote: str, key: str | None = None) -> bool:
        try:
            return bool(self._loop.call(self._push(local, remote, key), timeout=self._timeout))
        except Exception:  # noqa: BLE001 - timeout/transport failure
            return False

    def pull(self, remote: str, local: Path, key: str | None = None) -> bool:
        try:
            return bool(self._loop.call(self._pull(remote, local, key), timeout=self._timeout))
        except Exception:  # noqa: BLE001
            return False

    def remove(self, remote: str, key: str | None = None) -> bool:
        try:
            return bool(self._loop.call(self._remove(remote, key), timeout=self._timeout))
        except Exception:  # noqa: BLE001
            return False

    def list_remote(self, remote_dir: str, key: str | None = None) -> list[str]:
        try:
            return list(self._loop.call(self._list(remote_dir, key), timeout=self._timeout))
        except Exception:  # noqa: BLE001
            return []

    def pull_head(self, remote: str, lines: int, key: str | None = None) -> bytes:
        try:
            return bytes(self._loop.call(self._head(remote, lines, key), timeout=self._timeout))
        except Exception:  # noqa: BLE001
            return b""


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
        local: JsonlSessionStore | None = None,
    ) -> None:
        # The mirror is a real JsonlSessionStore, so every format decision (the
        # metadata line, provider state, atomic replace, fsync) stays in exactly
        # one implementation and this class only moves bytes.
        #
        # A caller that already owns a store over this directory passes it in:
        # two instances resolve to the same sessions directory and therefore to
        # the same lock *file*, and ``filelock`` treats two lock objects on one
        # path as a deadlock the moment one is held while the other is entered --
        # which is exactly what the sidebar's list-then-read path does.
        # An explicit ``mirror_root`` wins: it names a directory of its own, so a
        # store handed in for the *default* directory is the wrong one. Only when
        # the mirror would land on the same directory is the caller's store the
        # correct object to share.
        if mirror_root is not None:
            self._local = JsonlSessionStore(workspace, sessions_root=mirror_root)
        elif local is not None:
            self._local = local
        else:
            self._local = JsonlSessionStore(workspace, sessions_root=sessions_root)
        # An explicit root wins; otherwise the transport derives one from the
        # backend's workspace on first use, because every backend's path guard
        # refuses anything outside it.
        self._remote_root = remote_root or os.getenv("NANOBOT_SANDBOX_SESSIONS_ROOT", "").strip()
        self._transport = transport or RemoteSandboxTransport()
        self._hydrated = False
        self._warned_no_head = False
        self._lock = threading.Lock()
        # (op, session key, sandbox key). The sandbox key is captured on the
        # calling thread, where the request context still exists.
        self._queue: queue.Queue[tuple[str, str, str | None]] = queue.Queue()
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
        return self._root()

    def _root(self, skey: str | None = None) -> str:
        """The remote root: explicit, from the environment, or backend-derived."""
        if self._remote_root:
            return self._remote_root
        derive = getattr(self._transport, "root", None)
        if callable(derive):
            try:
                return str(derive(skey if skey is not None else sandbox_key()))
            except Exception:  # noqa: BLE001 - a transport that cannot answer
                pass
        return FALLBACK_REMOTE_ROOT

    # -- remote paths ------------------------------------------------------ #
    def _remote_path(self, key: str, skey: str | None = None) -> str:
        name = self._local.get_session_path(key).name
        return f"{self._root(skey)}/{name}"

    # -- background worker ------------------------------------------------- #
    def _enqueue(self, op: str, key: str) -> None:
        # The sandbox key is read HERE, on the caller's thread: the worker has no
        # request context, and the Novita resolver is a lookup of live in-process
        # sandbox handles keyed by it.
        skey = sandbox_key()
        # Incremented before the put so the worker can never finish an item that
        # a waiting ``flush`` has not yet counted.
        with self._idle:
            self._pending += 1
        self._queue.put((op, key, skey))

    def _drain(self) -> None:
        while True:
            try:
                op, key, skey = self._queue.get()
            except Exception:  # pragma: no cover - queue is unbounded
                continue
            try:
                remote = self._remote_path(key, skey)
                if op == "remove":
                    self._transport.remove(remote, skey)
                    continue
                path = self._local.get_session_path(key)
                # A file that is gone locally is a delete, not a push: the delete
                # enqueued its own removal, so a missing file here is not an error.
                if path.exists():
                    self._transport.push(path, remote, skey)
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
    def _partial_marker(self, path: Path) -> Path:
        return path.with_name(path.name + PARTIAL_SUFFIX)

    def _clear_partial(self, key: str) -> None:
        marker = self._partial_marker(self._local.get_session_path(key))
        with suppress(OSError):
            marker.unlink(missing_ok=True)

    def _full_body_is_local(self, key: str) -> bool:
        """True when the local copy is a complete session, not a header stub."""
        path = self._local.get_session_path(key)
        if not path.exists():
            return False
        return not self._partial_marker(path).exists()

    def _hydrate_one(self, key: str) -> bool:
        """Pull the FULL body of *key* unless a complete copy is already local.

        A listing writes header-only stubs, so "a local file exists" is no longer
        the same as "the body is here": the marker decides. On a successful pull
        the marker is dropped, because the file now really is the session.
        """
        if self._full_body_is_local(key):
            return False
        path = self._local.get_session_path(key)
        skey = sandbox_key()
        ok = bool(self._transport.pull(self._remote_path(key, skey), path, skey))
        if ok:
            self._clear_partial(key)
        return ok

    def _servable(self, key: str) -> bool:
        """Guard for every path that reads or rewrites a session *body*.

        A stub must never be served as a whole conversation, and must never be
        pushed back over the real one -- so a failed pull with a stub present is
        an honest miss, not a partial answer.
        """
        if self._full_body_is_local(key):
            return True
        if self._hydrate_one(key):
            return True
        if self._local.get_session_path(key).exists():
            logger.warning(
                "sandbox session {}: body could not be pulled, serving nothing "
                "rather than the cached header stub",
                key,
            )
        return False

    def _hydrate_all(self) -> None:
        """Populate the session list from headers, never by replaying bodies.

        Pulling each remote session whole put the entire chat history through
        this process on the first listing -- and it is charged for the whole
        cgroup, so a large history was a memory spike on every cold start. The
        listing only ever needs the metadata record and a preview, so that is
        all it fetches; the body is pulled on demand when the session is opened
        (``_hydrate_one``) and the stub is marked so it cannot be confused with
        the real file.
        """
        with self._lock:
            if self._hydrated:
                return
            self._hydrated = True
        skey = sandbox_key()
        root = self._root(skey)
        names = self._transport.list_remote(root, skey)
        for name in names[:MAX_HYDRATE_FILES]:
            path = self._local.sessions_dir / name
            marker = self._partial_marker(path)
            if path.exists():
                continue
            # A marker with no file is an interrupted stub write; clear it so the
            # listing is not permanently short one chat.
            with suppress(OSError):
                marker.unlink(missing_ok=True)
            data = self._fetch_head(f"{root}/{name}", skey)
            if not is_session_metadata_head(data):
                # No usable header: take the whole file, which is exactly what
                # this listing did before. A fetch that cannot be trusted must
                # never cost the chat its place in the sidebar.
                if data:
                    logger.debug(
                        "sandbox session {}: header unusable, pulling the body", name
                    )
                self._transport.pull(f"{root}/{name}", path, skey)
                continue
            # Marker BEFORE the file: a listing on another thread must never see
            # a header-only file that looks complete.
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
                tmp = path.with_name(f".{path.name}.{os.getpid()}.stub")
                tmp.write_bytes(data)
                os.replace(tmp, path)
            except OSError as exc:  # noqa: BLE001
                logger.debug("sandbox session stub write failed ({}): {}", path.name, exc)
                with suppress(OSError):
                    marker.unlink(missing_ok=True)

    def _fetch_head(self, remote: str, skey: str | None) -> bytes:
        """The header lines of one remote session, via whichever API answers.

        ``pull_head`` is the cheap path, and the only one used: falling back to a
        whole-file pull would reintroduce exactly the transfer this avoids. A
        transport without it yields no listing hydrate rather than a silent
        stampede of full bodies, and says so once.
        """
        reader = getattr(self._transport, "pull_head", None)
        if not callable(reader):
            with self._lock:
                if not self._warned_no_head:
                    self._warned_no_head = True
                    logger.warning(
                        "sandbox session transport {} has no pull_head; the session "
                        "list will not be hydrated from the sandbox",
                        type(self._transport).__name__,
                    )
            return b""
        try:
            data = reader(remote, HEADER_HYDRATE_LINES, skey)
        except Exception as exc:  # noqa: BLE001
            logger.debug("sandbox session head failed ({}): {}", remote, exc)
            return b""
        if isinstance(data, (bytes, bytearray)) and data:
            return bytes(data)
        return b""

    # -- SessionStore protocol --------------------------------------------- #
    def load(self, key: str) -> Session | None:
        if not self._servable(key):
            return None
        return self._local.load(key)

    def save(self, session: Session, *, fsync: bool = False) -> None:
        self._local.save(session, fsync=fsync)
        # A saved session is a full body by construction, so any stub marker left
        # over from a listing must not survive it.
        self._clear_partial(session.key)
        self._enqueue("push", session.key)

    def delete(self, key: str) -> bool:
        removed = self._local.delete(key)
        self._clear_partial(key)
        # The removal is enqueued even when the local file was already gone: the
        # sandbox is the durable copy, so the delete has to reach it either way.
        self._enqueue("remove", key)
        return removed

    def read(self, key: str) -> SessionPayload | None:
        if not self._servable(key):
            return None
        return self._local.read(key)

    def read_metadata(self, key: str) -> SessionMetadataPayload | None:
        if not self._servable(key):
            return None
        return self._local.read_metadata(key)

    def update_metadata(self, key: str, updates: dict[str, Any], *, fsync: bool = False) -> bool:
        # Guarded like a read: rewriting a stub's metadata and pushing it would
        # replace the real conversation in the sandbox with the header alone.
        if not self._servable(key):
            return False
        changed = self._local.update_metadata(key, updates, fsync=fsync)
        if changed:
            self._enqueue("push", key)
        return changed

    def list_sessions(self) -> list[SessionInfo]:
        self._hydrate_all()
        return self._local.list_sessions()

    def session_headers_nolock(self) -> list[dict[str, Any]]:
        """Metadata headers read WITHOUT the session-files lock, as the mirror does.

        The mirror is a ``JsonlSessionStore`` over the very directory the caller
        already locks, so answering "not me" here sent every sidebar refresh down
        the locked path -- which then re-entered that same lock through this
        store's second instance and raised ``Deadlock``. Hydration runs first for
        the same reason ``list_sessions`` does it: on a fresh host the mirror is
        empty, and an empty header list reads as "no sessions" rather than
        "not pulled yet".
        """
        self._hydrate_all()
        return self._local.session_headers_nolock()


def store_from_env(
    workspace: Path,
    *,
    sessions_root: Path | None = None,
    local: JsonlSessionStore | None = None,
) -> Any:
    """Return a ``SandboxSessionStore`` when ``NANOBOT_SESSION_STORE=sandbox``.

    The single decision point, so ``SessionManager`` construction stays unchanged
    and the feature is one environment variable away in either direction.

    ``local`` is the store the caller already built over the same directory. It
    must be passed when one exists: a second store over one sessions directory is
    a second lock over one lock file, which deadlocks the first nested acquire.
    """
    if os.getenv("NANOBOT_SESSION_STORE", "").strip().lower() != "sandbox":
        return None
    try:
        return SandboxSessionStore(workspace, sessions_root=sessions_root, local=local)
    except Exception as exc:  # noqa: BLE001 - never break boot over this
        logger.warning("sandbox session store unavailable, using local sessions: {}", exc)
        return None


__all__ = [
    "SandboxSessionStore",
    "SandboxTransport",
    "RemoteSandboxTransport",
    "store_from_env",
    "sandbox_key",
    "DEFAULT_REMOTE_SUBDIR",
    "FALLBACK_REMOTE_ROOT",
    "MAX_HYDRATE_FILES",
    "MAX_PUSH_BYTES",
]
