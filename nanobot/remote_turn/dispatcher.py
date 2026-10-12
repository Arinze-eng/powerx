"""The host side: stage a turn into the sandbox, run it, re-emit its events.

This is the piece that makes the host a relay. It does not know which backend the
administrator selected -- it asks ``workspace_bridge`` for the resolved executor,
which is the same resolver every other sandbox tool already uses, so a turn moves
to novita, freestyle, tenki, runloop or vps by changing the admin's selection and
nothing else.

THREE STEPS, NOT ONE
--------------------
Staging, provisioning, running. The middle step is what makes the feature work at
all: the worker is ``python -m nanobot.remote_turn.worker``, which needs the
package on the box, and the sandbox images do not ship it. Provisioning is the
turn's own responsibility (see :mod:`nanobot.remote_turn.bootstrap`) because the
alternative -- baking this dependency tree into every sandbox template -- is the
memory cost the feature exists to remove.

It never raises. Every failure returns a :class:`TurnOutcome` with ``ok=False``,
because the caller's correct response to "the sandbox could not run this turn" is
to run it on the host exactly as before -- not to fail the user's request.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.remote_turn.bootstrap import (
    BOOTSTRAP_TIMEOUT_S,
    bootstrap_enabled,
    provision_command,
    worker_command,
)
from nanobot.remote_turn.protocol import (
    REQUEST_PATH,
    RemoteTurnEvent,
    RemoteTurnRequest,
    decode_event,
    encode_request,
)

#: Where a turn should be run: ``host`` (default) or ``sandbox``.
#:
#: Read live rather than at import, so an operator can move the turns over (or
#: back) without a restart -- the same contract as the other PowerX switches.
TURN_HOST_ENV = "NANOBOT_TURN_HOST"

#: How long a dispatched turn may run before the host gives up on it. The turn's
#: own wall-clock budget is enforced inside the worker; this is the outer bound
#: on the transport, so a wedged sandbox cannot hold a session forever.
DEFAULT_DISPATCH_TIMEOUT_S = 1800


class TurnEventConsumerError(RuntimeError):
    """The caller's own ``on_event`` raised.

    Distinct from every other failure here on purpose. "The sandbox could not run
    this turn" is recoverable -- run it on the host. "The caller's stream consumer
    is broken" is not: it is a code defect, and turning it into a silent host
    fallback would hide it on every single turn while doubling the work.
    """


def sandbox_turns_enabled() -> bool:
    """True when turns should be dispatched to the sandbox rather than run here."""
    raw = os.environ.get(TURN_HOST_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in {"sandbox", "1", "true", "yes", "on", "remote"}


@dataclass
class TurnOutcome:
    """What came back from a dispatched turn.

    ``ok=False`` means "nothing usable came back" and the caller must run the turn
    on the host. ``ok=True`` with an empty ``final_content`` is a real (if empty)
    answer, which is a different thing and must not trigger a host retry.
    """

    ok: bool
    final_content: str = ""
    stop_reason: str = ""
    tools_used: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    #: Token usage the sandbox reported. Carried back because the host is where
    #: usage is metered and billed -- a turn that ran elsewhere must still count.
    usage: dict[str, int] = field(default_factory=dict)
    events: list[RemoteTurnEvent] = field(default_factory=list)
    error: str = ""
    backend: str = ""


async def _sandbox_request(executor: Any, request: RemoteTurnRequest) -> RemoteTurnRequest:
    """Re-point the request's workspace at the *sandbox's* root.

    ``request.workspace`` is filled in on the host, where it is the host's own data
    directory -- ``/home/nanobot`` under the gateway image. That path is not the
    sandbox's: it belongs to a user the worker does not run as, and building the
    agent on it raised ``[Errno 13] Permission denied: '/home/nanobot'``, which
    sent every single turn back to the host. The three "fell back to the host"
    lines in production at 10:05, 10:06 and 10:07 are that bug, and it is why the
    switch looked dead even once the package was installed.

    A path only means something to the machine that opens it, so the sandbox's view
    of its workspace is the only one that can be sent. ``executor_workspace_root``
    is used rather than an attribute read because the dispatcher holds a
    :class:`RemoteExecutor` wrapper, not a backend, and the two shapes -- plus
    Novita and VPS, whose roots are configuration-derived -- resolve differently.
    This also keeps the sandbox's workspace and the workspace its tools are jailed
    to in step, which is the promise in ``RemoteTurnRequest``'s docstring: a turn
    run in the sandbox sees the same files the host's tool calls would have written.

    When the host sent no workspace the root is still applied, because a turn's
    files have to land where the sandbox's own tools look. If the root cannot be
    resolved, nothing is invented: the request goes out unchanged and the worker
    falls back to its own default rather than to the host.
    """
    from nanobot.agent.tools.workspace_bridge import executor_workspace_root

    try:
        root = await executor_workspace_root(executor, session_key=request.session_key)
    except Exception as exc:  # noqa: BLE001 - an unknown root must not lose a turn
        logger.debug("remote turn: workspace root lookup failed: {}", exc)
        return request
    if not root or request.workspace == root:
        return request
    return replace(request, workspace=str(root))


class SandboxTurnDispatcher:
    """Runs one turn in the configured sandbox and streams its events back.

    ``on_event`` is called for every event as it is parsed, so a streaming
    channel can forward deltas as the sandbox produces them instead of waiting
    for the turn to finish. It is awaited, and a raising callback is the one
    failure that is *not* swallowed -- a broken consumer of the stream is a bug
    in the caller, not a sandbox problem.
    """

    def __init__(
        self,
        *,
        timeout_s: int = DEFAULT_DISPATCH_TIMEOUT_S,
        executor_factory: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._executor_factory = executor_factory

    async def _executor(self, session_key: str | None) -> Any:
        if self._executor_factory is not None:
            return await self._executor_factory()
        from nanobot.agent.tools.workspace_bridge import resolve_remote_executor

        # The key is passed explicitly and never left to the ambient lookup: a
        # dispatch happens *before* a turn binds its request context, so the
        # implicit answer would be "unknown" and the turn would be staged into
        # the wrong sandbox -- or none.
        return await resolve_remote_executor(session_key or None)

    async def dispatch(
        self,
        request: RemoteTurnRequest,
        *,
        on_event: Callable[[RemoteTurnEvent], Awaitable[None]] | None = None,
    ) -> TurnOutcome:
        """Run *request* in the sandbox. Never raises."""
        try:
            executor = await self._executor(request.session_key)
        except Exception as exc:  # noqa: BLE001 - absence of a sandbox is not fatal
            return TurnOutcome(ok=False, error=f"could not resolve a sandbox: {exc}")

        if executor is None or not getattr(executor, "available", False):
            return TurnOutcome(ok=False, error="no execution sandbox is configured")

        backend = str(getattr(executor, "name", "") or "")
        try:
            outcome = await asyncio.wait_for(
                self._run(executor, request, on_event),
                timeout=self._timeout_s,
            )
        except TurnEventConsumerError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "Dispatched turn for {} timed out after {}s on {}",
                request.session_key,
                self._timeout_s,
                backend,
            )
            return TurnOutcome(
                ok=False,
                backend=backend,
                error=f"turn timed out in the {backend} sandbox after {self._timeout_s}s",
            )
        except Exception as exc:  # noqa: BLE001 - the host must be able to recover
            logger.warning(
                "Dispatched turn for {} failed on {}: {}",
                request.session_key,
                backend,
                exc,
            )
            return TurnOutcome(ok=False, backend=backend, error=str(exc)[:400])
        outcome.backend = backend
        return outcome

    async def _run(
        self,
        executor: Any,
        request: RemoteTurnRequest,
        on_event: Callable[[RemoteTurnEvent], Awaitable[None]] | None,
    ) -> TurnOutcome:
        from nanobot.agent.tools.workspace_bridge import (
            run_remote,
            write_files_to_sandbox,
        )

        request = await _sandbox_request(executor, request)

        # The worker reads its request from a file, not from argv: a transcript is
        # far too large for a command line, and an argument would land in the
        # process list where anything on the sandbox could read the credentials.
        staged, detail = await write_files_to_sandbox(
            {posixpath.basename(REQUEST_PATH): encode_request(request)},
            posixpath.dirname(REQUEST_PATH),
            executor=executor,
        )
        if not staged:
            return TurnOutcome(ok=False, error=f"could not stage the turn: {detail}")

        # Provision the box before the worker needs the package. Best-effort on
        # purpose: a sandbox left provisioned by an earlier turn must still be
        # able to run this one, so a refresh that fails (GitHub unreachable, a
        # rate limit, no uv) is logged and the worker is tried anyway. A box
        # that genuinely could not be provisioned then fails at the worker step
        # and falls back to the host -- the same outcome as before this existed.
        if bootstrap_enabled():
            provisioned, provision_output = await run_remote(
                provision_command(),
                timeout=BOOTSTRAP_TIMEOUT_S,
                executor=executor,
            )
            if not provisioned:
                logger.warning(
                    "Remote turn: could not provision the {} sandbox; trying the "
                    "worker anyway: {}",
                    getattr(executor, "name", "") or "configured",
                    (provision_output or "").strip()[-300:],
                )

        command = worker_command(REQUEST_PATH)
        ok, output = await run_remote(
            command, timeout=self._timeout_s, executor=executor
        )

        events: list[RemoteTurnEvent] = []
        final: RemoteTurnEvent | None = None
        error_text = ""
        for line in (output or "").splitlines():
            event = decode_event(line)
            if event is None:
                continue
            events.append(event)
            if on_event is not None:
                try:
                    await on_event(event)
                except Exception as exc:  # noqa: BLE001 - re-raised as the distinct type
                    raise TurnEventConsumerError(str(exc)) from exc
            if event.kind == "final":
                final = event
            elif event.kind == "error":
                error_text = event.text or "the sandbox worker reported an error"

        if final is None:
            # No final event: the worker died, was killed, or never started. The
            # caller falls back to the host rather than showing the user half a
            # turn -- which is also why a non-zero exit is not itself the test:
            # a turn whose model errored still ends with a final event.
            return TurnOutcome(
                ok=False,
                events=events,
                error=error_text or f"the sandbox worker produced no final event ({ok=})",
            )

        messages = list(final.data.get("messages") or [])
        if not messages:
            # A final event with no transcript is not a finished turn the host
            # can adopt: persisting it would leave the durable record disagreeing
            # with what the user just read, and the next turn would be built from
            # that disagreement. Fall back and run it here instead.
            return TurnOutcome(
                ok=False,
                events=events,
                error="the sandbox returned a final event with no transcript",
            )

        return TurnOutcome(
            ok=True,
            final_content=final.text,
            stop_reason=str(final.data.get("stop_reason") or ""),
            tools_used=list(final.data.get("tools_used") or []),
            messages=messages,
            usage=dict(final.data.get("usage") or {}),
            events=events,
        )
