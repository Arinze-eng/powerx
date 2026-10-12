"""Run a whole agent turn inside the execution sandbox instead of on the host.

WHY
---
The gateway used to do everything: receive the message, build the context, call
the model, execute the tools, stream the answer. The host is therefore sized for
the *heaviest* part of a turn even though the sandbox already owns the workspace,
the files and the processes the turn is about. That is the cost the deployment
pays per user, and it is the reason the host cannot be replaced or scaled without
taking live conversations with it.

This package inverts it. The host keeps what is genuinely host-shaped --
configuration, admin settings, the channel transports, the durable record -- and
*dispatches* the turn to the sandbox the administrator already selected. The
sandbox runs the model calls and the tools, and streams its events back. The host
becomes a relay.

WHAT IS IN HERE
---------------
``protocol``    the wire format: one JSON request in, JSONL events out.
``worker``      what runs inside the sandbox: ``python -m nanobot.remote_turn.worker``.
``dispatcher``  the host side: stage the request, run the worker, re-emit events.

The split is deliberate: the protocol has no imports from the agent, so it can be
tested as plain data, and the dispatcher talks to an *executor* rather than to any
one provider, so it works unchanged for every backend the admin can select.

FAIL-OPEN
---------
Nothing here changes behaviour until ``execution.run_turns_in_sandbox`` is on
(env ``NANOBOT_TURN_HOST=sandbox``). A dispatch that cannot reach a sandbox, or
whose worker dies, reports a failure outcome rather than raising, so the caller
can run the turn on the host exactly as it does today. Moving the turns must
never be able to lose a turn.
"""

from __future__ import annotations

from nanobot.remote_turn.dispatcher import (
    TURN_HOST_ENV,
    SandboxTurnDispatcher,
    TurnEventConsumerError,
    TurnOutcome,
    sandbox_turns_enabled,
)
from nanobot.remote_turn.protocol import (
    PROTOCOL_VERSION,
    RemoteTurnRequest,
    RemoteTurnEvent,
    TurnEventKind,
    decode_event,
    decode_request,
    encode_event,
    encode_request,
)

__all__ = [
    "PROTOCOL_VERSION",
    "RemoteTurnEvent",
    "RemoteTurnRequest",
    "SandboxTurnDispatcher",
    "TURN_HOST_ENV",
    "TurnEventConsumerError",
    "TurnEventKind",
    "TurnOutcome",
    "decode_event",
    "decode_request",
    "encode_event",
    "encode_request",
    "sandbox_turns_enabled",
]
