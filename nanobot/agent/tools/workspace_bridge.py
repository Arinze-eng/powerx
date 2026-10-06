"""Bridge project sources out of the execution sandbox into a host directory.

Why this exists
---------------
The agent runs shell/exec tools inside a **remote execution sandbox** (Novita
``/workspace``, Daytona ``/home/daytona``, Runloop ``/home/user``, Tenki
``/home/tenki``, Upstash ``/workspace/home``, or the configurable VPS
directory). Host-side tools such as
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
     daytona / runloop / tenki) download it directly;
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

#: Names never pushed **into** a sandbox. ``DEFAULT_EXCLUDES`` plus the two that
#: only matter in this direction: ``.vercel`` is host-local link state that would
#: pin the sandbox to the wrong project, and a local secrets file has no business
#: travelling to a build machine.
DEFAULT_STAGE_EXCLUDES: tuple[str, ...] = (
    *DEFAULT_EXCLUDES,
    ".vercel",
    ".env.local",
    ".DS_Store",
)

#: Transfer blob for the push direction. Workspace-relative for the same reason
#: as ``_STAGE_ARCHIVE_NAME``: every backend guards its file APIs to the sandbox
#: workspace root, so a ``/tmp`` blob cannot be written at all.
_STAGE_PUSH_BLOB = ".nanobot-push.b64"

#: Base64 characters per ``printf`` when a backend exposes nothing but ``run``.
#: Small enough that one command stays far inside every backend's command limit.
_PUSH_CHUNK_CHARS = 48 * 1024

#: Past either of these, ship the project as one archive instead of one write per
#: file. Chosen so the ordinary agent-authored project (a handful of source files)
#: takes the direct path, and a real repository takes the archive.
_DIRECT_WRITE_MAX_FILES = 120
_DIRECT_WRITE_MAX_BYTES = 4 * 1024 * 1024

#: Transfer archive used by the *backend-driven* staging path. It is deliberately
#: a **workspace-relative** name: every backend guards its file APIs to the
#: sandbox workspace root (``_safe_path`` raises ``path must remain inside the
#: workspace`` for anything outside), so the old hard-coded ``/tmp`` archive
#: could not be downloaded back at all. ``download`` refused it, staging returned
#: ``None``, and the user was told web_dev "cannot locate the project sources" no
#: matter how healthy the sandbox was. ``_REMOTE_ARCHIVE`` above is kept for the
#: native Novita SDK path, whose ``files.read`` is not path-guarded.
_STAGE_ARCHIVE_NAME = ".nanobot-stage.tar.gz"


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


def _remote_archive_path(root: str) -> str:
    """Absolute in-sandbox path of the transfer archive, inside *root*."""
    return posixpath.join(str(root or "/").rstrip("/") or "/", _STAGE_ARCHIVE_NAME)


def _project_basename(remote_dir: str) -> str:
    """The directory name a staged copy has to keep.

    Callers that only read the files do not care. Callers that push the project
    somewhere *named* do: ``web_dev`` links a Vercel project by the staged
    directory's name, so a staged copy that lost the project's name would create
    the deployment under the staging temp directory's name instead.
    """
    base = posixpath.basename(str(remote_dir or "").rstrip("/"))
    return base if base not in {"", ".", "..", "/"} else "project"


def _archive_command(
    remote_dir: str,
    excludes: tuple[str, ...],
    *,
    with_size: bool,
    archive: str = _REMOTE_ARCHIVE,
) -> str:
    """Shell command archiving *remote_dir* with its **own name as the prefix**.

    The previous form archived ``.`` from inside the directory, producing members
    like ``./index.html``. Those extract as loose files directly under the
    staging directory, so ``StagedProject.path`` was named after the temporary
    staging directory rather than the project — and every name derived from it
    (the Vercel project, the env vars hung off it) was wrong.

    Archiving the basename from its parent instead yields ``<name>/…``, which
    :func:`_extract_archive` recognises as a single top-level directory and
    returns as the project root, so the project keeps its own name.
    """
    base = _project_basename(remote_dir)
    parent = posixpath.dirname(str(remote_dir or "").rstrip("/")) or "/"
    # The archive is built in a scratch directory OUTSIDE the tree and only moved
    # into place once tar has exited. It has to be: the archive lives inside the
    # directory being archived whenever the project root is an ancestor of it —
    # always the case for the workspace root, i.e. exactly the "deploy the
    # project I built in the sandbox root" and ``project="/"`` requests.
    #
    # ``--exclude=<archive>`` is *not* enough on its own. Measured on GNU tar
    # 1.35 and on a live Tenki sandbox: the exclusion only suppresses the archive
    # when the file already exists when tar starts. On a clean first run tar
    # creates it mid-walk, notices it growing, and aborts with
    # ``<base>: file changed as we read it`` (exit 1). So staging returned None
    # and ``web_dev`` answered "no sources found to deploy" on a healthy
    # sandbox — intermittently, because the *second* run over the same leftover
    # archive succeeded. Hence both halves of the fix: remove any stale archive
    # up front, and never write the new one inside the tree.
    flags = _exclude_flags((*excludes, _STAGE_ARCHIVE_NAME))
    archive_q = shlex.quote(archive)
    command = (
        f"rm -f {archive_q} && "
        f'scratch=$(mktemp -d "${{TMPDIR:-/tmp}}/powerx-stage.XXXXXX") && '
        f"cd {shlex.quote(parent)} && "
        f'tar czf "$scratch/stage.tar.gz" {flags} {shlex.quote(base)} && '
        f'mv "$scratch/stage.tar.gz" {archive_q} && '
        f'rm -rf "$scratch"'
    )
    if with_size:
        command += f" && wc -c < {archive_q}"
    return command


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


def _extract_into(archive: Path, staging: Path) -> Path:
    """Extract *archive* into a clean subdirectory of *staging* and return it.

    The archive is downloaded *into* ``staging``, so extracting straight into it
    left the project sitting next to ``source.tar.gz`` — two entries, which
    :func:`_extract_archive` reads as "no single top-level directory" and answers
    with the staging directory itself. That is exactly how a staged project lost
    its name (``powerx-stage-…`` instead of the project's own), and with it the
    name every downstream consumer derived — the Vercel project, its env vars,
    its status lookups. A dedicated ``sources`` subdirectory keeps the archive
    out of the way so the project root is decided by the project alone.
    """
    root = staging / "sources"
    root.mkdir(parents=True, exist_ok=True)
    return _extract_archive(archive, root)


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

    Must be called from inside an agent turn: the sandbox it reads is the one
    held by the session in the current request context. A host-side caller has
    no request context, so the session key resolves to ``"unknown"`` and this
    looks at the wrong (usually empty) sandbox — see
    :func:`resolve_remote_executor`, which takes the key explicitly for exactly
    that reason.
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
    if backend_name == "tenki" and backend_config is not None:
        from nanobot.agent.tools.tenki_backend import (
            TenkiExecutionBackend,
            tenki_sandbox_name,
        )

        return TenkiExecutionBackend(backend_config, sandbox_name=tenki_sandbox_name(key))
    if backend_name == "freestyle" and backend_config is not None:
        from nanobot.agent.tools.freestyle_backend import (
            FreestyleExecutionBackend,
            freestyle_sandbox_name,
        )

        return FreestyleExecutionBackend(
            backend_config, sandbox_name=freestyle_sandbox_name(key)
        )
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
    archive = _remote_archive_path(root)
    tar_cmd = _archive_command(remote_dir, excludes, with_size=True, archive=archive)
    output = await backend.run(tar_cmd, timeout=600)  # type: ignore[attr-defined]
    if "exit_code=0" not in output and "[exit_code=" in output and "[exit_code=0]" not in output:
        logger.debug("workspace_bridge: remote archive command failed: {}", output[-400:])
        return None

    fetched = await _fetch_via_backend_download(
        backend, archive, local_archive, timeout=600
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

    await _cleanup_remote(backend, archive)
    return _extract_into(local_archive, staging)


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
    tar_cmd = _archive_command(remote_dir, excludes, with_size=False)
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
    return _extract_into(local_archive, staging)


# --------------------------------------------------------------------------- #
# The push direction: host → sandbox
#
# ``stage_from_sandbox`` above reads a project *out* of the sandbox. This is the
# mirror image, and it exists because the two filesystems are isolated in both
# directions: a project the agent wrote with the ordinary file tools lives on the
# host, while ``web_dev`` (correctly) runs the Vercel CLI inside the sandbox —
# so the sandbox looked at an empty directory and answered "no project sources
# found to deploy" on a project that existed, in full, a few inches away.
#
# It is deliberately built on the two primitives *every* backend has — ``run``
# and a way to place bytes — so one implementation covers Novita, Daytona,
# Runloop, Tenki, Freestyle, Upstash, Vercel and the VPS, rather than one
# special case per provider.
# --------------------------------------------------------------------------- #


def _iter_stage_files(
    source: Path, excludes: tuple[str, ...]
) -> list[tuple[Path, str]]:
    """Every file under *source* as ``(absolute path, relative posix name)``.

    Directory names in *excludes* are pruned during the walk, so a ``node_modules``
    is never even entered — walking a large tree to discard it is how a staging
    step becomes slower than the deploy it feeds. Symlinks are skipped: a link out
    of the tree is a path the sandbox cannot resolve.
    """
    skip = set(excludes)
    entries: list[tuple[Path, str]] = []
    for root, dirs, files in os.walk(source):
        dirs[:] = sorted(name for name in dirs if name not in skip)
        relative = os.path.relpath(root, source)
        for name in sorted(files):
            if name in skip:
                continue
            path = Path(root) / name
            if path.is_symlink() or not path.is_file():
                continue
            rel = (
                name
                if relative in {".", ""}
                else posixpath.join(relative.replace(os.sep, "/"), name)
            )
            entries.append((path, rel))
    return entries


def _tar_bytes(entries: list[tuple[Path, str]]) -> bytes:
    """A gzipped tar of *entries*, named relative to the project root.

    Ownership is normalised and permissions pinned: the archive is extracted by
    whatever user the sandbox runs as, and a mode carried over from the host
    (a ``0600`` secret file, a ``0755`` script) is a surprise nobody asked for.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        for path, rel in entries:
            info = tf.gettarinfo(str(path), arcname=rel)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = 0o644
            with open(path, "rb") as handle:
                tf.addfile(info, handle)
    return buffer.getvalue()


async def _write_remote_text(executor: "RemoteExecutor", path: str, text: str) -> bool:
    """Place *text* at *path* using the backend's own writer.

    Two shapes because Novita's native SDK handle is not a backend: it exposes
    ``files.write`` directly. Anything without either shape answers ``False`` so
    the caller falls through to the archive path, which needs only ``run``.
    """
    if executor.native is not None:
        try:
            await asyncio.to_thread(executor.native.files.write, path, text)
            return True
        except Exception as exc:  # noqa: BLE001 - fall through to the archive path
            logger.debug("workspace_bridge: native write of {} failed: {}", path, exc)
            return False
    writer = getattr(executor.backend, "write", None)
    if not callable(writer):
        return False
    try:
        await writer(path, text)
        return True
    except Exception as exc:  # noqa: BLE001 - fall through to the archive path
        logger.debug("workspace_bridge: backend write of {} failed: {}", path, exc)
        return False


async def _write_blob_without_a_file_api(
    executor: "RemoteExecutor", path: str, payload: str
) -> bool:
    """Place an ASCII *payload* using **only** ``run``.

    The tier that makes this work on a provider nobody has written a file API
    for. The payload travels as base64 chunks rather than a heredoc: base64 is
    byte-exact and immune to every quoting trap a project's own source contains,
    and a chunk is small enough to stay far inside every backend's command limit.
    """
    quoted = shlex.quote(path)
    ok, _out = await run_remote(f"rm -f {quoted}", timeout=60, executor=executor)
    if not ok:
        return False
    for start in range(0, len(payload), _PUSH_CHUNK_CHARS):
        chunk = payload[start : start + _PUSH_CHUNK_CHARS]
        ok, _out = await run_remote(
            f"printf '%s' {shlex.quote(chunk)} >> {quoted}", timeout=120, executor=executor
        )
        if not ok:
            return False
    ok, _out = await run_remote(
        f"base64 -d {quoted} > {quoted}.bin && mv {quoted}.bin {quoted}",
        timeout=120,
        executor=executor,
    )
    return ok


async def _write_remote_bytes(executor: "RemoteExecutor", path: str, data: bytes) -> bool:
    """Place raw *data* at *path*, using ``write_bytes`` when the backend has it."""
    if executor.backend is not None:
        writer = getattr(executor.backend, "write_bytes", None)
        if callable(writer):
            try:
                await writer(path, data)
                return True
            except Exception as exc:  # noqa: BLE001 - fall through to base64
                logger.debug("workspace_bridge: backend write_bytes of {} failed: {}", path, exc)
    payload = base64.b64encode(data).decode("ascii")
    staging = f"{path}.b64"
    if not await _write_remote_text(executor, staging, payload) and not (
        await _write_blob_without_a_file_api(executor, staging, payload)
    ):
        return False
    quoted, staging_q = shlex.quote(path), shlex.quote(staging)
    ok, _out = await run_remote(
        f"base64 -d {staging_q} > {quoted} && rm -f {staging_q}",
        timeout=120,
        executor=executor,
    )
    return ok


async def _push_file_by_file(
    executor: "RemoteExecutor", remote_dir: str, entries: list[tuple[Path, str]]
) -> bool:
    """Write each file straight into the sandbox. No shell, no archive.

    The preferred path: text goes through the backend's own writer, so nothing is
    base64-wrapped and nothing needs ``tar`` to exist in the sandbox.
    """
    for path, rel in entries:
        target = posixpath.join(remote_dir, rel)
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None:
            if not await _write_remote_text(executor, target, text):
                return False
        elif not await _write_remote_bytes(executor, target, data):
            return False
    return True


async def _push_as_archive(
    executor: "RemoteExecutor",
    remote_dir: str,
    entries: list[tuple[Path, str]],
    root: str,
) -> bool:
    """Ship the project as one base64 tar and unpack it with a single command.

    Fewer round trips than file-by-file (which matters for a project with
    hundreds of files) and byte-exact for binaries. The blob is written inside
    the sandbox workspace because every backend's file API refuses a path
    outside its root.
    """
    blob_path = posixpath.join(str(root or "/").rstrip("/") or "/", _STAGE_PUSH_BLOB)
    payload = base64.b64encode(_tar_bytes(entries)).decode("ascii")
    if not await _write_remote_text(executor, blob_path, payload) and not (
        await _write_blob_without_a_file_api(executor, blob_path, payload)
    ):
        return False
    blob = shlex.quote(blob_path)
    target = shlex.quote(remote_dir)
    ok, out = await run_remote(
        f"mkdir -p {target} && "
        f"(base64 -d {blob} 2>/dev/null || base64 --decode {blob}) | tar xzf - -C {target}; "
        f"_rc=$?; rm -f {blob}; exit $_rc",
        timeout=300,
        executor=executor,
    )
    if not ok:
        logger.debug("workspace_bridge: remote unpack failed: {}", (out or "")[-400:])
    return ok


async def stage_to_sandbox(
    source_dir: str | Path,
    remote_dir: str,
    *,
    executor: "RemoteExecutor | None" = None,
    excludes: tuple[str, ...] = DEFAULT_STAGE_EXCLUDES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[bool, str]:
    """Copy a host directory **into** the active execution sandbox.

    Returns ``(ok, detail)`` and never raises: a caller reporting a failed push is
    far more useful than a traceback, and staging is a convenience over the
    project already existing in the sandbox.

    ``remote_dir`` is used verbatim (it must already be an absolute in-sandbox
    path — ``web_dev`` resolves it against the sandbox workspace root).
    """
    source = Path(source_dir)
    if not source.is_dir():
        return False, f"{source} is not a directory on this host, so there is nothing to stage"

    entries = _iter_stage_files(source, excludes)
    if not entries:
        return False, f"{source} holds no files to stage (after excluding {', '.join(excludes)})"

    total = 0
    for path, _rel in entries:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    if total > max_bytes:
        return False, (
            f"{source} is {total} bytes, over the {max_bytes} byte staging ceiling; "
            "stage a subdirectory or raise the limit"
        )

    ex = executor or await resolve_remote_executor()
    if not ex.available:
        return (
            False,
            "no execution sandbox is configured, so there is nothing to stage the files into",
        )

    root = await executor_workspace_root(ex) or posixpath.dirname(remote_dir.rstrip("/")) or "/"
    destination = remote_dir.rstrip("/") or root

    # Which strategy first is chosen up front, not discovered by failing: a
    # project with hundreds of files costs hundreds of round trips file-by-file,
    # and one archive is strictly cheaper for it. Small projects take the direct
    # path because it needs no ``tar`` in the sandbox and no base64 anywhere.
    prefer_archive = len(entries) > _DIRECT_WRITE_MAX_FILES or total > _DIRECT_WRITE_MAX_BYTES
    order = (
        ("archive", _push_as_archive, (destination, entries, root)),
        ("direct write", _push_file_by_file, (destination, entries)),
    ) if prefer_archive else (
        ("direct write", _push_file_by_file, (destination, entries)),
        ("archive", _push_as_archive, (destination, entries, root)),
    )
    for label, push, args in order:
        if await push(ex, *args):  # type: ignore[arg-type]
            return True, (
                f"staged {len(entries)} file(s) into {destination} "
                f"({getattr(ex, 'name', 'sandbox')}, {label})"
            )

    return False, (
        f"could not stage {source} into {destination} on the "
        f"{getattr(ex, 'name', 'sandbox')} sandbox; "
        "the sandbox may be unreachable or its workspace unwritable"
    )


async def write_files_to_sandbox(
    files: "dict[str, str] | list[tuple[str, str]]",
    remote_dir: str,
    *,
    executor: "RemoteExecutor | None" = None,
) -> tuple[bool, str]:
    """Write an in-memory ``{relative path: content}`` set into the sandbox.

    The scaffold path needs exactly this, and it must not go through the host
    filesystem: a scaffold that lands on the host is a project the agent's own
    sandbox tools cannot then edit.
    """
    items = list(files.items()) if isinstance(files, dict) else list(files)
    if not items:
        return False, "no files to write"
    ex = executor or await resolve_remote_executor()
    if not ex.available:
        return False, "no execution sandbox is configured"
    for rel, content in items:
        target = posixpath.join(remote_dir.rstrip("/") or "/", rel.strip("/"))
        if not await _write_remote_text(ex, target, content):
            return False, (
                f"could not write {rel} into {remote_dir} on the "
                f"{getattr(ex, 'name', 'sandbox')} sandbox"
            )
    return True, (
        f"wrote {len(items)} file(s) into {remote_dir} ({getattr(ex, 'name', 'sandbox')})"
    )


def _output_tail(result: object) -> str:
    text = str(getattr(result, "stdout", "") or "")
    err = str(getattr(result, "stderr", "") or "")
    return f"{text}\n{err}"[-400:]


async def _cleanup_remote(backend: object, archive: str | None = None) -> None:
    """Delete the transfer archive from the sandbox. Never raises."""
    target = archive or _REMOTE_ARCHIVE
    try:
        await backend.run(f"rm -f {shlex.quote(target)}", timeout=60)  # type: ignore[attr-defined]
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
        freestyle_backend,
        tenki_backend,
        upstash_backend,
        vercel_backend,
    )

    mapping = {
        "daytona": daytona_backend.WORKSPACE,
        "runloop": runloop_backend.WORKSPACE,
        "tenki": tenki_backend.WORKSPACE,
        "freestyle": freestyle_backend.WORKSPACE,
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


async def executor_workspace_root(
    executor: RemoteExecutor, session_key: str | None = None
) -> str | None:
    """Return the workspace root of an *already resolved* executor.

    A caller holding a handle has no reason to resolve the backend a second time:
    for Novita that second resolution is another lookup of a live in-process
    handle, and it can disagree with the handle the caller is actually about to
    run against. Passing the handle through keeps the two in step.
    """
    if executor.backend is not None:
        _, backend_config = await _selected_backend()
        return _backend_root(executor.name, executor.backend, backend_config)
    return await sandbox_workspace_root()


async def remote_workspace_root(session_key: str | None = None) -> str | None:
    """Return the workspace root of the *resolved* backend instance.

    Differs from :func:`sandbox_workspace_root` only for VPS, where the root is
    configuration-derived and therefore needs a live instance to resolve.
    """
    ex = await resolve_remote_executor(session_key=session_key)
    return await executor_workspace_root(ex, session_key=session_key)


__all__ = [
    "stage_from_sandbox",
    "stage_to_sandbox",
    "write_files_to_sandbox",
    "DEFAULT_STAGE_EXCLUDES",
    "sandbox_workspace_root",
    "remote_workspace_root",
    "resolve_remote_executor",
    "executor_workspace_root",
    "run_remote",
    "fetch_remote_file",
    "RemoteExecutor",
    "StagedProject",
    "DEFAULT_EXCLUDES",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_FILE_BYTES",
]
