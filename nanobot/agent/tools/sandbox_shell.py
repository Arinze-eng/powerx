"""Run a shell command in the execution sandbox instead of on the container.

The container is a skeleton: 0.2 vCPU and a 488 MiB cgroup ceiling that the
process already sits at 78-90% of. ``ExecTool`` was the one lane that ignored
that -- it spawns every command with ``asyncio.create_subprocess_exec`` against
the gateway itself, so a compile, an install or a build competes with the agent
loop for the same two tenths of a core, and whatever page cache it leaves behind
is charged to the cgroup that decides whether the container survives.

The backend-agnostic primitive to move it already exists in
:mod:`nanobot.agent.tools.workspace_bridge`: :func:`~.resolve_remote_executor`
resolves the configured backend into a run/fetch handle without caring whether it
is Novita's native SDK object or a ``VPSExecutionBackend``, and
:func:`~.run_remote` runs through either. This module is the thin policy layer on
top: when to offload, where the command should run, and what to refuse.

Three things it deliberately does NOT do.

* **It is off unless asked.** ``NANOBOT_SHELL_SANDBOX`` gates the whole path, so
  the default behaviour of every existing turn is unchanged and the switch can be
  flipped after the production logs have been read.
* **It never silently runs somewhere else.** A command whose meaning depends on a
  local process -- a ``yield_time_ms`` session, which returns a ``session_id`` to
  poll and write to, or a command already wrapped for local ``bwrap`` -- is
  refused with an explanation rather than quietly executed on the host. A caller
  that cannot tell where its command ran cannot trust the result.
* **It never guesses a directory.** The sandbox has its own filesystem; the host
  workspace is not mounted into it. A command whose working directory cannot be
  mapped faithfully onto the sandbox workspace is refused, because running it in
  ``/`` instead would produce a plausible wrong answer.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.context import current_request_session_key
from nanobot.agent.tools.workspace_bridge import (
    executor_workspace_root,
    resolve_remote_executor,
    run_remote,
)

__all__ = [
    "SHELL_SANDBOX_ENV",
    "SandboxShellError",
    "map_remote_cwd",
    "run_in_sandbox",
    "sandbox_shell_enabled",
]

#: Opt-in switch for the whole path. Unset or false means every command keeps
#: running on the container, exactly as it did before this module existed.
SHELL_SANDBOX_ENV = "NANOBOT_SHELL_SANDBOX"

#: Default hard timeout for a remote command. Matches the tool's own default.
DEFAULT_REMOTE_TIMEOUT_S = 60

_TRUTHY = frozenset({"1", "true", "yes", "on", "sandbox", "enabled"})


class SandboxShellError(RuntimeError):
    """The command cannot be run in the sandbox, with the reason attached.

    ``degradable`` separates the two cases a caller must treat differently. An
    unreachable sandbox is a *transport* problem: the command still has a correct
    home on the host, so the caller falls back. An unmappable working directory is
    a *meaning* problem: the command would produce a plausible wrong answer
    somewhere else, so the caller must refuse it and say so.
    """

    def __init__(self, message: str, *, degradable: bool) -> None:
        super().__init__(message)
        self.degradable = degradable


def sandbox_shell_enabled() -> bool:
    """Whether the sandbox path is switched on for this process."""
    return os.getenv(SHELL_SANDBOX_ENV, "").strip().lower() in _TRUTHY


def map_remote_cwd(
    host_cwd: str, workspace_root: str | None, remote_root: str | None
) -> str | None:
    """Map a host working directory onto the sandbox's own workspace, or ``None``.

    The mapping is relative to the workspace root, because that is the only
    anchor the two filesystems share: the sandbox has its own root and the host
    workspace is not mounted into it. A path inside the workspace becomes
    ``<remote_root>/<relative>``; anything else -- an absolute host path outside
    the workspace, a different volume -- returns ``None`` so the caller refuses
    instead of running the command somewhere arbitrary.
    """
    if not remote_root or not workspace_root:
        return None
    host_root = Path(workspace_root).expanduser().resolve(strict=False)
    target = Path(host_cwd).expanduser().resolve(strict=False)
    try:
        relative = target.relative_to(host_root)
    except ValueError:
        return None
    base = remote_root.rstrip("/")
    if str(relative) in ("", "."):
        return base
    return f"{base}/{relative.as_posix()}"


def _refusal(reason: str, *, degradable: bool) -> SandboxShellError:
    return SandboxShellError(
        f"{reason} With {SHELL_SANDBOX_ENV} set, shell commands run in the sandbox, "
        "so this command was refused rather than silently run on the host.",
        degradable=degradable,
    )


async def run_in_sandbox(
    command: str,
    *,
    host_cwd: str | None = None,
    workspace_root: str | None = None,
    timeout: int | None = None,
    session_key: str | None = None,
    executor: Any = None,
) -> tuple[bool, str]:
    """Run *command* in the execution sandbox. Returns ``(ok, output)``.

    Raises :class:`SandboxShellError` when there is no reachable sandbox or
    the working directory cannot be mapped. The caller is expected to fall back to
    the local path on an unreachable sandbox -- that is the documented degradation
    -- and to surface the reason to the model when the directory cannot be mapped,
    because that one is a real answer about where the command would have run.
    """
    ex = executor or await resolve_remote_executor(session_key=session_key)
    if not getattr(ex, "available", False):
        raise _refusal("No execution sandbox is reachable for this session.", degradable=True)

    # Asked of the handle we are about to run against, not re-resolved: a second
    # resolution can name a different sandbox than the one the command lands in.
    remote_root = await executor_workspace_root(ex, session_key=session_key)
    remote_cwd: str | None = None
    if host_cwd:
        remote_cwd = map_remote_cwd(host_cwd, workspace_root, remote_root)
        if remote_cwd is None:
            raise _refusal(
                f"The working directory {host_cwd!r} is not inside the workspace "
                f"({workspace_root!r}) and has no equivalent in the sandbox.",
                degradable=False,
            )

    # ``cd`` rather than a backend-specific working-directory argument: no
    # backend's ``run`` takes one, and this keeps the call shape identical across
    # all of them.
    wrapped = f"cd {shlex.quote(remote_cwd)} && {command}" if remote_cwd else command
    ok, output = await run_remote(
        wrapped,
        timeout=timeout or DEFAULT_REMOTE_TIMEOUT_S,
        executor=ex,
    )
    logger.debug(
        "sandbox_shell: ran in {} cwd={} ok={}",
        getattr(ex, "name", "?"),
        remote_cwd or "-",
        ok,
    )
    return ok, output


def calling_session_key() -> str | None:
    """The session key of the caller, read on the calling thread.

    Read here rather than inside the transport because the resolver is a lookup of
    live in-process handles keyed by this value, and a worker thread has no
    request context to read it from.
    """
    key = current_request_session_key()
    return key or None
