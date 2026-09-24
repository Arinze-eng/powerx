"""Bridge project sources out of the execution sandbox into a host directory.

Why this exists
---------------
The agent runs shell/exec tools inside a **remote execution sandbox** (Novita
``/workspace``, Daytona ``/home/daytona``, Runloop ``/home/user``, Upstash
``/workspace/home``, or the configurable VPS directory). Host-side tools such as
``build_artifact`` and ``web_dev``, however, only see the **host workspace**
(``ctx.workspace`` / ``~/.nanobot/workspace``).

Those two filesystems are fully isolated per backend, so a host-side tool that
resolves ``source_dir`` against its own workspace finds nothing — the project
the user built inside the sandbox simply is not there. The previous behaviour
surfaced as "the build tool cannot see / cannot copy my files", i.e. a missing
persistence/visibility layer rather than a bug in the builder itself.

This module adds that layer: it stages a directory from whichever backend is
selected into a local directory, so any host-side consumer can read the project
regardless of sandbox provider.

Strategy (works across every backend)
-------------------------------------
1. Ask the selected backend to archive the directory into a single ``.tar.gz``
   (cheap: one remote command, honours sensible excludes so ``node_modules``,
   ``.git`` and build output do not bloat the transfer).
2. Fetch that one archive to the host:
   * backends implementing ``async download(remote, local)`` (vps / upstash /
     daytona / runloop) download it directly;
   * the native Novita SDK path reads it via ``files.read`` in bounded base64
     chunks (the SDK's read returns text, so binary payloads are base64-wrapped).
3. Extract locally and return the staged path.

Every step is best-effort and bounded: an unreachable or unconfigured sandbox
returns ``None`` so callers can fall back to the host workspace instead of
raising.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import posixpath
import re
import shlex
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

#: Directories that must never be shipped: they are either rebuildable locally
#: or enormous, and including them is the main reason a transfer would time out.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git",
    "node_modules",
    ".gradle",
    "build",
    ".dart_tool",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    ".next",
    ".expo",
)

#: Hard ceiling for the compressed archive. Keeps a runaway transfer (a stray
#: dataset, a virtualenv) from exhausting the host disk or the request budget.
DEFAULT_MAX_BYTES = 200 * 1024 * 1024

#: Novita SDK reads are text-based; pull the base64 archive in bounded slices so
#: a large project never materialises as one giant string.
_CHUNK_CHARS = 4 * 1024 * 1024
_REMOTE_ARCHIVE = "/tmp/powerx-stage.tar.gz"


def _exclude_flags(excludes: tuple[str, ...]) -> str:
    return " ".join(f"--exclude={shlex.quote(name)}" for name in excludes)


@dataclass(frozen=True)
class StagedProject:
    """A project extracted from a remote sandbox onto the host.

    ``path`` is the directory holding the sources. ``cleanup_root`` is the
    dedicated temp directory that owns it and must be removed by the caller —
    it is never the user's workspace, so deleting it is always safe.
    """

    path: Path
    cleanup_root: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.cleanup_root, ignore_errors=True)


def _normalise_remote_dir(remote_dir: str | None, root: str) -> str:
    """Resolve a user/agent-supplied path to an absolute in-sandbox directory."""
    raw = (remote_dir or "").strip()
    if not raw or raw in {".", "/"}:
        return root
    if not raw.startswith("/"):
        return posixpath.normpath(posixpath.join(root, raw))
    return posixpath.normpath(raw)


def _safe_member_name(member: tarfile.TarInfo) -> str | None:
    """Return a safe relative member name, or ``None`` if it must be skipped.

    Rejects absolute paths and any traversal component. Note the deliberate use
    of ``posixpath.normpath`` rather than ``str.lstrip("./")`` — ``lstrip``
    strips a *character set*, so it would silently rewrite ``../escape.txt`` to
    ``escape.txt`` and defeat the traversal check.
    """
    raw = (member.name or "").strip()
    if not raw or raw.startswith("/") or raw.startswith("\\"):
        return None
    if re.match(r"^[A-Za-z]:", raw):  # Windows drive letters
        return None
    normalised = posixpath.normpath(raw).replace("\\", "/")
    if normalised in {".", ".."} or normalised.startswith("../") or "/../" in normalised:
        return None
    return normalised


def _extract_archive(archive: Path, dest_root: Path) -> Path:
    """Safely extract a staged archive and return the directory holding sources.

    A single top-level directory in the archive is treated as the project root;
    otherwise everything lands in ``dest_root`` itself. Members that would escape
    ``dest_root`` (absolute paths, traversal, links, device nodes) are skipped
    individually so one hostile entry cannot abort the whole extraction.
    """
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            if member.islnk() or member.issym() or member.isdev():
                # Links and device nodes are never needed for a source push.
                continue
            if not (member.isfile() or member.isdir()):
                continue
            safe_name = _safe_member_name(member)
            if safe_name is None:
                continue
            # Re-point the member at the validated name so the stdlib filter
            # agrees with our own check.
            member.name = safe_name
            try:
                tf.extract(member, dest_root, set_attrs=False)
            except (tarfile.TarError, OSError, ValueError):
                # Skip an individual bad member rather than failing the stage.
                continue

    tops = [p for p in dest_root.iterdir() if p.name not in {".", ".."}]
    if len(tops) == 1 and tops[0].is_dir():
        return tops[0]
    return dest_root


async def _fetch_via_backend_download(
    backend: object,
    remote_archive: str,
    local_archive: Path,
    *,
    timeout: int,
) -> bool:
    """Fetch the archive using a backend's ``download`` API when available."""
    download = getattr(backend, "download", None)
    if download is None or not callable(download):
        return False
    await download(remote_archive, str(local_archive))
    return local_archive.is_file() and local_archive.stat().st_size > 0


async def _fetch_via_novita_sdk(
    sandbox: object,
    remote_archive: str,
    local_archive: Path,
    *,
    max_bytes: int,
) -> bool:
    """Fetch the archive from a native Novita sandbox handle.

    ``files.read`` returns text, so the archive is base64-encoded remotely and
    read back in slices to stay within sane memory limits.
    """
    files = getattr(sandbox, "files", None)
    commands = getattr(sandbox, "commands", None)
    if files is None or commands is None:
        return False

    encoded_path = f"{remote_archive}.b64"
    encode_cmd = (
        f"base64 -w0 {shlex.quote(remote_archive)} > {shlex.quote(encoded_path)} "
        f"&& wc -c < {shlex.quote(encoded_path)}"
    )
    result = await asyncio.to_thread(commands.run, encode_cmd, cwd="/", timeout=300)
    size_raw = str(getattr(result, "stdout", "") or "").strip().splitlines()
    try:
        encoded_size = int(size_raw[-1]) if size_raw else 0
    except ValueError:
        encoded_size = 0
    if encoded_size <= 0 or encoded_size > int(max_bytes * 1.5):
        return False

    buffer = io.BytesIO()
    for offset in range(0, encoded_size, _CHUNK_CHARS):
        slice_cmd = (
            f"tail -c +{offset + 1} {shlex.quote(encoded_path)} "
            f"| head -c {_CHUNK_CHARS}"
        )
        chunk = await asyncio.to_thread(commands.run, slice_cmd, cwd="/", timeout=300)
        text = str(getattr(chunk, "stdout", "") or "").strip()
        if not text:
            return False
        try:
            buffer.write(base64.b64decode(text))
        except Exception:
            return False
    data = buffer.getvalue()
    if not data:
        return False
    local_archive.write_bytes(data)
    return True


async def _selected_backend() -> tuple[str, object | None]:
    """Return the configured execution backend name and its config section."""
    try:
        from nanobot.agent.tools.novita_sandbox import NovitaSandboxTool

        return NovitaSandboxTool()._selected_backend()  # noqa: SLF001 - intra-package
    except Exception as exc:  # noqa: BLE001 - absence of a backend is not fatal
        logger.debug("workspace_bridge: backend discovery failed: {}", exc)
        return "novita", None


async def stage_from_sandbox(
    source_dir: str | None,
    *,
    staging_root: Path | None = None,
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> "StagedProject | None":
    """Copy a project directory out of the active execution sandbox.

    Returns a :class:`StagedProject` whose ``path`` holds the sources and whose
    ``cleanup_root`` must be deleted by the caller when done, or ``None`` when no
    sandbox is reachable (callers should then fall back to the host workspace).
    Never raises for infrastructure reasons.

    The staging directory is always a **dedicated temp directory** — never the
    caller's workspace — so cleanup can never delete user data.
    """
    backend_name, backend_config = await _selected_backend()
    parent = staging_root or _default_staging_root()
    if not _assert_ephemeral(parent):
        # Never stage onto the durable volume: these archives are throwaway bytes.
        logger.warning(
            "workspace_bridge: staging path {} is on the persistent disk; "
            "using ephemeral temp instead",
            parent,
        )
        parent = Path(tempfile.gettempdir()) / "powerx-staging"
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        parent = Path(tempfile.gettempdir())
    staging = Path(tempfile.mkdtemp(prefix="powerx-stage-", dir=str(parent)))
    local_archive = staging / "source.tar.gz"

    try:
        if backend_name == "novita" and backend_config is None:
            project = await _stage_novita_native(
                source_dir, staging, local_archive, excludes, max_bytes
            )
        else:
            project = await _stage_remote_backend(
                backend_name, backend_config, source_dir, staging, local_archive, excludes, max_bytes
            )
    except Exception as exc:  # noqa: BLE001 - staging is best-effort
        logger.warning("workspace_bridge: staging from {} failed: {}", backend_name, exc)
        shutil.rmtree(staging, ignore_errors=True)
        return None

    if project is None:
        # Nothing usable was produced: remove the scratch dir so a failed transfer
        # does not leave an archive behind. Repeated failures would otherwise
        # accumulate on the temp volume until it filled.
        shutil.rmtree(staging, ignore_errors=True)
        return None

    return StagedProject(path=project, cleanup_root=staging)


def _default_staging_root() -> Path:
    """Return an **ephemeral** scratch root for staged source archives.

    Deliberately NOT on the persistent disk. Staging is transient by definition —
    the archive is downloaded, extracted, consumed, and deleted — so putting it on
    the durable volume would spend customer capacity (a project archive can reach
    the size cap) on bytes that are worthless seconds later. The persistent volume
    is reserved for small text facts.

    Honours ``POWERX_STAGING_DIR`` for operators who want to pin it, then uses the
    system temp dir, which is container-local scratch space. ``_assert_ephemeral``
    verifies the choice rather than trusting it.
    """
    override = (os.environ.get("POWERX_STAGING_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(tempfile.gettempdir()) / "powerx-staging"


def _assert_ephemeral(root: Path) -> bool:
    """Return ``False`` when ``root`` would consume the persistent volume.

    A silent regression here would spend customer disk on throwaway archives, so
    this is checked explicitly instead of assumed. Never raises: staging is best
    effort and the caller falls back to the system temp dir.
    """
    try:
        from nanobot.config.paths import get_persistent_data_dir

        persistent = get_persistent_data_dir().resolve()
    except Exception:  # noqa: BLE001 - cannot resolve, so cannot judge
        return True
    try:
        candidate = root.resolve()
    except OSError:
        return True
    return not (candidate == persistent or persistent in candidate.parents)


def _build_backend(
    backend_name: str,
    backend_config: object | None,
    key: str,
) -> object | None:
    """Instantiate the execution backend for *backend_name*, or ``None``.

    Kept as one function because the constructor signature differs per backend
    (Upstash/Daytona/Runloop/Vercel each need a session-derived resource name,
    VPS does not) and two callers now need the same instance semantics: the
    project stager and the live-screen byte mover.
    """
    if backend_name == "vps":
        from nanobot.agent.tools.vps_backend import VPSExecutionBackend

        return VPSExecutionBackend(backend_config)
    if backend_name == "upstash" and backend_config is not None:
        from nanobot.agent.tools.upstash_backend import (
            UpstashExecutionBackend,
            upstash_box_name,
        )

        return UpstashExecutionBackend(backend_config, box_name=upstash_box_name(key))
    if backend_name == "daytona" and backend_config is not None:
        from nanobot.agent.tools.daytona_backend import (
            DaytonaExecutionBackend,
            daytona_sandbox_name,
        )

        return DaytonaExecutionBackend(backend_config, sandbox_name=daytona_sandbox_name(key))
    if backend_name == "runloop" and backend_config is not None:
        from nanobot.agent.tools.runloop_backend import (
            RunloopExecutionBackend,
            runloop_devbox_name,
        )

        return RunloopExecutionBackend(backend_config, devbox_name=runloop_devbox_name(key))
    if backend_name == "vercel" and backend_config is not None:
        from nanobot.agent.tools.vercel_backend import (
            VercelExecutionBackend,
            vercel_sandbox_name,
        )

        return VercelExecutionBackend(backend_config, sandbox_name=vercel_sandbox_name(key))
    return None


def _backend_root(backend_name: str, backend: object, backend_config: object | None) -> str:
    """Return the workspace root the backend resolves paths against."""
    root = str(getattr(backend, "workspace", "") or "/workspace")
    if backend_name == "vps":
        resolver = getattr(backend, "_configured_workspace", None)
        if callable(resolver):
            try:
                root = str(resolver(backend_config) or root)
            except Exception:  # noqa: BLE001
                pass
    return root


async def _stage_remote_backend(
    backend_name: str,
    backend_config: object | None,
    source_dir: str | None,
    staging: Path,
    local_archive: Path,
    excludes: tuple[str, ...],
    max_bytes: int,
) -> Path | None:
    """Stage from vps / upstash / daytona / runloop backends."""
    from nanobot.agent.tools.novita_sandbox import _session_key  # noqa: PLC2701

    key = _session_key()
    backend = _build_backend(backend_name, backend_config, key)
    if backend is None:
        logger.debug("workspace_bridge: no backend instance for {}", backend_name)
        return None

    root = _backend_root(backend_name, backend, backend_config)

    remote_dir = _normalise_remote_dir(source_dir, root)
    tar_cmd = (
        f"cd {shlex.quote(remote_dir)} && "
        f"tar czf {shlex.quote(_REMOTE_ARCHIVE)} {_exclude_flags(excludes)} . "
        f"&& wc -c < {shlex.quote(_REMOTE_ARCHIVE)}"
    )
    output = await backend.run(tar_cmd, timeout=600)  # type: ignore[attr-defined]
    if "exit_code=0" not in output and "[exit_code=" in output and "[exit_code=0]" not in output:
        logger.debug("workspace_bridge: remote archive command failed: {}", output[-400:])
        return None

    fetched = await _fetch_via_backend_download(
        backend, _REMOTE_ARCHIVE, local_archive, timeout=600
    )
    if not fetched:
        return None
    if local_archive.stat().st_size > max_bytes:
        logger.warning(
            "workspace_bridge: archive {} exceeds {} bytes; refusing to stage",
            local_archive.stat().st_size,
            max_bytes,
        )
        return None

    await _cleanup_remote(backend, None)
    return _extract_archive(local_archive, staging)


async def _stage_novita_native(
    source_dir: str | None,
    staging: Path,
    local_archive: Path,
    excludes: tuple[str, ...],
    max_bytes: int,
) -> Path | None:
    """Stage from the native Novita SDK sandbox handle."""
    from nanobot.agent.tools.novita_sandbox import (  # noqa: PLC2701
        _STORE,
        _WORKSPACE,
        NovitaSandboxTool,
        _session_key,
    )

    tool = NovitaSandboxTool()
    key = _session_key()
    sandbox = _STORE.get(key)
    if sandbox is None:
        # Do not create a sandbox just to read files: only a live session has
        # anything worth staging.
        logger.debug("workspace_bridge: no live Novita sandbox for session")
        return None

    remote_dir = _normalise_remote_dir(source_dir, _WORKSPACE)
    tar_cmd = (
        f"cd {shlex.quote(remote_dir)} && "
        f"tar czf {shlex.quote(_REMOTE_ARCHIVE)} {_exclude_flags(excludes)} ."
    )
    result = await asyncio.to_thread(sandbox.commands.run, tar_cmd, cwd="/", timeout=600)
    code = getattr(result, "exit_code", 0)
    if code not in (0, None):
        logger.debug("workspace_bridge: novita archive failed: {}", _output_tail(result))
        return None

    fetched = await _fetch_via_novita_sdk(
        sandbox, _REMOTE_ARCHIVE, local_archive, max_bytes=max_bytes
    )
    if not fetched:
        return None
    if local_archive.stat().st_size > max_bytes:
        return None

    with_cleanup = (
        f"rm -f {shlex.quote(_REMOTE_ARCHIVE)} {shlex.quote(_REMOTE_ARCHIVE)}.b64"
    )
    try:
        await asyncio.to_thread(sandbox.commands.run, with_cleanup, cwd="/", timeout=60)
    except Exception:  # noqa: BLE001
        pass

    _ = tool  # retained for symmetry/debugging hooks
    return _extract_archive(local_archive, staging)


def _output_tail(result: object) -> str:
    text = str(getattr(result, "stdout", "") or "")
    err = str(getattr(result, "stderr", "") or "")
    return f"{text}\n{err}"[-400:]


async def _cleanup_remote(backend: object, _unused: object | None = None) -> None:
    try:
        await backend.run(f"rm -f {shlex.quote(_REMOTE_ARCHIVE)}", timeout=60)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


async def sandbox_workspace_root() -> str | None:
    """Return the active sandbox workspace root, or ``None`` when unknown.

    Host-side tools use this to explain *where* the agent's files actually live
    when a requested path cannot be found locally.
    """
    backend_name, _config = await _selected_backend()
    if backend_name == "novita":
        from nanobot.agent.tools.novita_sandbox import _WORKSPACE  # noqa: PLC2701

        return _WORKSPACE
    if backend_name == "vps":
        return None  # configurable; resolved at call time
    from nanobot.agent.tools import (  # noqa: PLC2701
        daytona_backend,
        runloop_backend,
        upstash_backend,
        vercel_backend,
    )

    mapping = {
        "daytona": daytona_backend.WORKSPACE,
        "runloop": runloop_backend.WORKSPACE,
        "upstash": upstash_backend.WORKSPACE,
        "vercel": vercel_backend.WORKSPACE,
    }
    return mapping.get(backend_name)


#: Ceiling for a single file-pull out of the sandbox. A GUI frame is tens of
#: kilobytes; this bound exists so a runaway capture cannot pull an unbounded
#: payload into the gateway process, and so a caller can tell "too big" from
#: "transfer failed" instead of hanging on a multi-hundred-megabyte read.
DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024

#: Every backend's ``run`` renders its status as a trailing ``[exit_code=N]``
#: marker (see ``vps_backend._output`` and its siblings). Parse the last one so
#: a command that echoes the marker itself cannot spoof the result.
_EXIT_CODE_RE = re.compile(r"\[exit_code=(-?\d+)\]")


def _exit_code(output: str) -> int | None:
    """Return the trailing exit code a backend's ``run`` reports, or ``None``."""
    matches = _EXIT_CODE_RE.findall(output or "")
    if not matches:
        return None
    try:
        return int(matches[-1])
    except ValueError:
        return None


@dataclass(frozen=True)
class RemoteExecutor:
    """A resolved way to run a command and pull a file from the active sandbox.

    Two shapes exist because Novita's native SDK handle is not a
    :class:`~nanobot.agent.tools.vps_backend.VPSExecutionBackend` — it exposes
    ``commands``/``files`` directly and moves bytes as chunked base64. Callers
    that only need "run this, fetch that" should not have to know which.
    """

    name: str
    backend: object | None = None
    native: object | None = None

    @property
    def available(self) -> bool:
        return self.backend is not None or self.native is not None


async def resolve_remote_executor(session_key: str | None = None) -> RemoteExecutor:
    """Resolve the configured sandbox into a run/fetch handle.

    Never raises: an unconfigured or unreachable backend returns an
    unavailable executor so callers degrade instead of failing a request.

    *session_key* names the sandbox to attach to. Callers outside an agent turn
    — the live-screen pump is the one that exists — have no request context, so
    the implicit :func:`_session_key` lookup would silently answer ``"unknown"``
    and attach to the wrong (or no) sandbox. Such callers must pass the key.
    """
    try:
        backend_name, backend_config = await _selected_backend()
        from nanobot.agent.tools.novita_sandbox import _STORE, _session_key  # noqa: PLC2701

        key = session_key or _session_key()
        if backend_name == "novita" and backend_config is None:
            return RemoteExecutor(name="novita", native=_STORE.get(key))
        return RemoteExecutor(
            name=backend_name, backend=_build_backend(backend_name, backend_config, key)
        )
    except Exception as exc:  # noqa: BLE001 - absence of a backend is not fatal
        logger.debug("workspace_bridge: executor resolution failed: {}", exc)
        return RemoteExecutor(name="unavailable")


async def run_remote(
    command: str,
    *,
    timeout: int = 120,
    executor: RemoteExecutor | None = None,
) -> tuple[bool, str]:
    """Run *command* in the sandbox. Returns ``(ok, output)``; never raises."""
    ex = executor or await resolve_remote_executor()
    try:
        if ex.native is not None:
            result = await asyncio.to_thread(
                ex.native.commands.run, command, cwd="/", timeout=timeout
            )
            code = getattr(result, "exit_code", None)
            text = (
                f"{getattr(result, 'stdout', '') or ''}"
                f"\n{getattr(result, 'stderr', '') or ''}"
            ).strip()
            return (code in (0, None)), text
        if ex.backend is None:
            return False, "no execution backend is configured"
        output = await ex.backend.run(command, timeout=timeout)  # type: ignore[attr-defined]
        return (_exit_code(output) == 0), output
    except Exception as exc:  # noqa: BLE001 - callers degrade, never crash
        logger.debug("workspace_bridge: remote command failed: {}", exc)
        return False, str(exc)[:400]


async def fetch_remote_file(
    remote_path: str,
    *,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
    executor: RemoteExecutor | None = None,
) -> bytes | None:
    """Fetch one file out of the sandbox as bytes, or ``None``.

    Reuses the archive fetch paths rather than adding a third byte-mover, so
    backend knowledge (Novita's text-only ``files.read``, Runloop's base64
    fallback, per-backend ``download``) stays in one place.

    Note the path must be one the backend is willing to read — Runloop's
    ``download`` runs ``_safe_path`` and refuses anything outside its workspace,
    so callers should target the workspace rather than ``/tmp``.
    """
    ex = executor or await resolve_remote_executor()
    if not ex.available:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="powerx-file-") as tmp:
            local = Path(tmp) / "payload.bin"
            if ex.native is not None:
                ok = await _fetch_via_novita_sdk(
                    ex.native, remote_path, local, max_bytes=max_bytes
                )
            else:
                ok = await _fetch_via_backend_download(
                    ex.backend, remote_path, local, timeout=120
                )
            if not ok or not local.is_file():
                return None
            if local.stat().st_size > max_bytes:
                logger.debug(
                    "workspace_bridge: {} exceeds {} bytes; refusing to fetch",
                    remote_path,
                    max_bytes,
                )
                return None
            return local.read_bytes()
    except Exception as exc:  # noqa: BLE001 - a missing frame is not fatal
        logger.debug("workspace_bridge: fetch of {} failed: {}", remote_path, exc)
        return None


async def remote_workspace_root(session_key: str | None = None) -> str | None:
    """Return the workspace root of the *resolved* backend instance.

    Differs from :func:`sandbox_workspace_root` only for VPS, where the root is
    configuration-derived and therefore needs a live instance to resolve.
    """
    ex = await resolve_remote_executor(session_key=session_key)
    if ex.backend is not None:
        _, backend_config = await _selected_backend()
        return _backend_root(ex.name, ex.backend, backend_config)
    return await sandbox_workspace_root()


__all__ = [
    "stage_from_sandbox",
    "sandbox_workspace_root",
    "remote_workspace_root",
    "resolve_remote_executor",
    "run_remote",
    "fetch_remote_file",
    "RemoteExecutor",
    "StagedProject",
    "DEFAULT_EXCLUDES",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_FILE_BYTES",
]
