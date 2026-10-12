"""What runs inside the sandbox: ``python3 -m nanobot.remote_turn.worker <request>``.

It reads the staged request, builds the model client and the tools *locally* --
which is the whole point, because "locally" now means the sandbox, beside the
workspace the turn is about -- runs the turn, and writes JSONL events to stdout.

The turn-running core takes its loop as an argument. That is not decoration: it
is what lets the dispatch path be tested end to end without a sandbox, a network
or an API key, and it keeps the one piece that genuinely cannot be tested
in-process -- reaching a real provider from a real sandbox -- isolated in
:func:`build_loop`.

WHY THE LOOP IS BUILT BY ``AgentLoop.from_config``
--------------------------------------------------
The sandbox must run the *same* agent the host would have run, or a turn would
behave differently depending on where it landed: a different tool set, a
different prompt, a different model default. So :func:`build_loop` does not
assemble a provider and a registry by hand -- it takes the project's own
composition root, with a config carrying the administrator's selection, and
reads ``.provider`` and ``.tools`` off the result. There is exactly one place
that knows how an agent is wired, and this is not a second one.

Output contract
---------------
stdout carries *only* JSONL events. Everything else goes to stderr, because the
host parses stdout and a stray print from a tool or a library would otherwise be
indistinguishable from a turn result. The dispatcher skips non-event lines
anyway, so the contract is enforced twice.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.hook import AgentHook
from nanobot.remote_turn.protocol import (
    RemoteTurnEvent,
    RemoteTurnRequest,
    decode_request,
    encode_event,
)

#: Called for each event as it happens. Awaited, so a slow consumer applies
#: back-pressure rather than buffering a whole turn in memory.
Emit = Callable[[RemoteTurnEvent], Awaitable[None]]

#: Read by ``providers.factory``; see :func:`apply_pool` for why a carried pool
#: turns the sandbox's own last-resort lane off.
ADMIN_LANE_ENV_VAR = "PROVIDER_POOL_ADMIN_LANE"


@dataclass
class LocalLoop:
    """The two things a turn needs: a model client and a tool registry.

    Returned by :func:`build_loop` rather than a whole ``AgentLoop`` so the
    runner is given exactly the surface it uses, and so a test can hand back a
    fake without standing up a loop.
    """

    provider: Any
    tools: Any
    config: Any = None


def apply_pool(request: RemoteTurnRequest) -> int:
    """Reconstitute the administrator's provider pool inside the sandbox.

    The pool is not rebuilt by hand -- that would be a second implementation of
    the rotation, free to drift from the host's. It is *installed* the way the
    admin panel installs it, and the project's own ``make_pool_provider`` finds
    it. Both ends then rotate over the same lanes by construction.

    ``PROVIDER_POOL_PATH`` is set alongside the env var on purpose: ``load_pool``
    prefers the on-disk copy, so a pool file left behind by an earlier turn on a
    long-lived sandbox would otherwise outrank the pool the host just sent.

    The ambient admin lane is switched off at the same time, because the host
    sends its own as one of the entries. ``make_pool_provider`` would otherwise
    append a lane built from *this* sandbox's config -- an endpoint and a key the
    administrator never chose for this turn -- and the sandbox would rotate over
    it. With the switch off the lane set installed here is exactly the lane set
    the host rotates over, which is the whole point of carrying it.

    Returns the number of lanes installed.
    """
    entries = list(request.pool or [])
    if not entries:
        return 0

    from nanobot.provider_pool import POOL_ENV_VAR

    payload = json.dumps({"entries": entries}, separators=(",", ":"), default=str)
    os.environ[POOL_ENV_VAR] = payload
    path = Path(tempfile.gettempdir()) / "nanobot_turn_provider_pool.json"
    try:
        path.write_text(payload, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as exc:  # noqa: PERF203 - the env var alone may still work
        logger.warning("remote turn: could not write the pool file ({}); using env only", exc)
    os.environ["PROVIDER_POOL_PATH"] = str(path)
    os.environ[ADMIN_LANE_ENV_VAR] = "0"
    return len(entries)


def _config_for(request: RemoteTurnRequest) -> Any:
    """Build the config the sandbox will run the turn under.

    Loaded from whatever the sandbox has (env and a config file if the operator
    put one there), then *overridden* with the administrator's selection as the
    host recorded it. The override is the point: the host owns the model choice,
    so a sandbox that happens to carry a stale config of its own must not be able
    to change which model a user's turn runs on.
    """
    from nanobot.config.loader import load_config
    from nanobot.config.schema import ProviderConfig

    config = load_config()
    defaults = config.agents.defaults
    creds = request.credentials

    if request.workspace:
        defaults.workspace = request.workspace

    # A named preset on the sandbox would outrank the fields below
    # (``resolve_preset`` prefers it), so clear it: the host's selection wins.
    defaults.model_preset = None
    if creds.model:
        defaults.model = creds.model
    if creds.provider:
        defaults.provider = creds.provider

    name = (creds.provider or defaults.provider or "auto").strip()
    if name and name != "auto":
        existing = getattr(config.providers, name, None)
        provider_config = existing if isinstance(existing, ProviderConfig) else ProviderConfig()
        if creds.api_key:
            provider_config.api_key = creds.api_key
        if creds.api_base:
            provider_config.api_base = creds.api_base
        if creds.extra_headers:
            provider_config.extra_headers = dict(creds.extra_headers)
        # ``ProvidersConfig`` allows extra fields, so this also works for a
        # custom provider the administrator named themselves.
        setattr(config.providers, name, provider_config)

    for key, value in (request.tools_config or {}).items():
        if hasattr(config.tools, key):
            setattr(config.tools, key, value)

    return config


def build_loop(request: RemoteTurnRequest) -> LocalLoop:
    """Compose the agent inside the sandbox, with the host's selection applied."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.tools.registry import ToolRegistry

    lanes = apply_pool(request)
    if lanes:
        logger.info("remote turn: installed {} provider pool lane(s)", lanes)
    config = _config_for(request)
    loop = AgentLoop.from_config(config, tool_registry=ToolRegistry())
    return LocalLoop(provider=loop.provider, tools=loop.tools, config=config)


class _EventHook(AgentHook):
    """Turn runner lifecycle into wire events.

    Text the model produces is streamed by the runner's own ``progress_callback``
    (that is what it carries: assistant deltas, not tool names), so this hook
    covers the other half -- what the agent is *doing*. Without it the host would
    see the answer arrive with no sign that any tool ran until the turn ended,
    which is exactly the frozen-label problem the host's own hooks exist to fix.

    It inherits ``AgentHook`` rather than duck-typing the two methods this file
    happens to use. It did not, and the runner's ``on_error`` call -- which fires
    on every failed provider call -- raised ``AttributeError`` instead of
    reporting the failure: a bad API key, a rate limit or a 5xx turned into a
    worker crash rather than a clean error event. Inheriting the base makes the
    runner's full contract satisfied by construction, so adding a hook call
    upstream cannot silently brick the worker again.
    """

    def __init__(self, emit: Emit) -> None:
        super().__init__()
        self._emit = emit

    def wants_streaming(self) -> bool:
        return False

    async def on_error(self, context: Any) -> None:
        """Report a failed run as an error event instead of crashing the worker.

        The dispatcher falls back to the host on an ``error`` event, which is the
        right outcome for a provider failure: the turn is not lost, it is simply
        run where it would have run before this feature existed.
        """
        error = str(getattr(context, "error", "") or "").strip()
        exception = getattr(context, "exception", None)
        if not error and exception is not None:
            error = f"the turn failed: {type(exception).__name__}: {exception}"
        await self._emit(
            RemoteTurnEvent(kind="error", text=error or "the turn failed")
        )

    async def before_execute_tools(self, context: Any) -> None:
        for call in getattr(context, "tool_calls", None) or []:
            name = str(getattr(call, "name", "") or "")
            if name:
                await self._emit(
                    RemoteTurnEvent(
                        kind="tool",
                        text=name,
                        data={"phase": "start", "name": name},
                    )
                )

    async def after_execute_tool(self, context: Any, tool_call: Any, tool: Any, params: Any, result: Any) -> None:
        name = str(getattr(tool_call, "name", "") or getattr(tool, "name", "") or "")
        await self._emit(
            RemoteTurnEvent(
                kind="tool",
                text=name,
                data={"phase": "end", "name": name},
            )
        )


async def run_turn(
    request: RemoteTurnRequest,
    *,
    emit: Emit,
    loop_factory: Callable[[RemoteTurnRequest], Any] | None = None,
) -> RemoteTurnEvent:
    """Run *request* to completion, emitting events. Returns the final event.

    Never raises: a turn that cannot start, or that dies mid-way, is reported as
    an ``error`` event so the host can fall back to running it itself. A turn
    that ran and produced nothing is a ``final`` event with empty text -- a
    different thing, and not a reason to re-run the work.
    """
    from nanobot.agent.runner import AgentRunner, AgentRunSpec
    from nanobot.utils.llm_runtime import LLMRuntime

    try:
        loop = (loop_factory or build_loop)(request)
        provider = loop.provider
        tools = loop.tools
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        logger.exception("remote turn: could not build the turn")
        final = RemoteTurnEvent(kind="error", text=f"could not build the turn: {exc}")
        await emit(final)
        return final

    # ``capture`` rather than a bare constructor: it derives the model's true
    # context window, which is what makes compaction fire on a small-window
    # model. A sandbox that guessed 200k would let a long turn grow unbounded.
    runtime = LLMRuntime.capture(
        provider,
        request.credentials.model,
        context_window_tokens=0,
    )

    async def on_progress(content: str) -> None:
        if content:
            await emit(RemoteTurnEvent(kind="delta", text=content))

    workspace = Path(request.workspace) if request.workspace else None

    spec = AgentRunSpec(
        initial_messages=list(request.messages),
        tools=tools,
        runtime=runtime,
        max_iterations=request.max_iterations or 0,
        max_tool_result_chars=request.max_tool_result_chars or 0,
        workspace=workspace,
        session_key=request.session_key or None,
        progress_callback=on_progress,
        hook=_EventHook(emit),  # type: ignore[arg-type]
        # The turn budget is enforced in here, on the clock that matters: the
        # sandbox's, where the work is actually happening. The host's outer
        # timeout is only the transport bound.
        turn_budget_s=request.turn_budget_s or None,
    )

    try:
        result = await AgentRunner().run(spec)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        logger.exception("remote turn: the run failed")
        final = RemoteTurnEvent(kind="error", text=f"the turn failed: {exc}")
        await emit(final)
        return final

    final = RemoteTurnEvent(
        kind="final",
        text=result.final_content or "",
        data={
            "stop_reason": result.stop_reason,
            "tools_used": list(result.tools_used),
            "usage": dict(result.usage or {}),
            # The transcript the host will persist. It travels back with the
            # result because the host is still the system of record: a turn whose
            # messages never came home would be a turn the user can see and the
            # session cannot remember, and the next turn would start from a
            # transcript that disagrees with the screen.
            "messages": list(result.messages or []),
        },
    )
    await emit(final)
    return final


async def _amain(request_path: str) -> int:
    with open(request_path, "r", encoding="utf-8") as handle:
        request = decode_request(handle.read())

    async def emit(event: RemoteTurnEvent) -> None:
        sys.stdout.write(encode_event(event) + "\n")
        sys.stdout.flush()

    final = await run_turn(request, emit=emit)
    return 0 if final.kind == "final" else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python3 -m nanobot.remote_turn.worker <request.json>", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_amain(args[0]))
    except Exception as exc:  # noqa: BLE001 - the host falls back on a non-zero exit
        # Last-resort error event: a request that cannot even be parsed still has
        # to tell the host why, or the host retries the same broken payload.
        try:
            sys.stdout.write(
                encode_event(
                    RemoteTurnEvent(kind="error", text=f"worker failed to start: {exc}")
                )
                + "\n"
            )
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        print(f"worker failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
