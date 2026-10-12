"""The wire format for a dispatched turn: one JSON request in, JSONL events out.

Deliberately free of any import from ``nanobot.agent``. The request is data -- a
transcript, a model selection, the credentials the sandbox needs to reach that
model, the tool configuration and the turn's limits -- so it can be validated,
logged without loading an agent, and tested as plain JSON.

WHO HOLDS THE SECRETS
---------------------
The sandbox makes the model calls, so it must hold the model credential. That is
inherent to moving the turn, not an accident of this format, and it is why the
request carries ``ProviderCredentials`` as its own object: the credential is the
one field that must never be logged, echoed into an event, or written into the
durable record, and giving it a name means every serializer can treat it
specially. ``repr`` is suppressed for the same reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

#: Bumped when a change would make an older worker misread a newer request.
#: The dispatcher refuses to send a request a worker has already rejected.
PROTOCOL_VERSION = 1

#: Where the dispatcher stages the request inside the sandbox.
#:
#: Directly under ``/tmp`` on purpose: the staging helpers write a file, they do
#: not create a directory, and a missing parent would turn "the sandbox is fine"
#: into "the turn could not be staged" for no reason.
REQUEST_PATH = "/tmp/nanobot_turn_request.json"

#: The sandbox command the dispatcher runs. ``python3`` rather than ``python``:
#: the backends' base images ship the former.
WORKER_COMMAND = "python3 -m nanobot.remote_turn.worker {request_path}"

TurnEventKind = Literal["delta", "tool", "final", "error", "log"]


@dataclass(frozen=True)
class ProviderCredentials:
    """Everything the sandbox needs to reach the model the admin selected.

    ``api_key`` is never rendered: a credential that reaches a log line, an
    exception message or a checkpoint is a credential that has leaked.
    """

    provider: str
    model: str
    api_key: str = field(default="", repr=False)
    api_base: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "api_key": self.api_key,
            "api_base": self.api_base,
            "extra_headers": dict(self.extra_headers),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProviderCredentials":
        return cls(
            provider=str(payload.get("provider") or ""),
            model=str(payload.get("model") or ""),
            api_key=str(payload.get("api_key") or ""),
            api_base=payload.get("api_base") or None,
            extra_headers=dict(payload.get("extra_headers") or {}),
        )


@dataclass(frozen=True)
class RemoteTurnRequest:
    """One turn, packaged for a sandbox to run.

    *session_key* travels with it because the sandbox is per-session: the worker
    uses it to attach to the same workspace the host's tool calls would have
    used, so a turn that runs in the sandbox sees the same files as the turn
    before it.
    """

    session_key: str
    messages: list[dict[str, Any]]
    credentials: ProviderCredentials
    tools_config: dict[str, Any] = field(default_factory=dict)
    workspace: str | None = None
    max_iterations: int = 0
    max_tool_result_chars: int = 0
    turn_budget_s: float = 0.0
    channel: str = ""
    chat_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "session_key": self.session_key,
            "messages": self.messages,
            "credentials": self.credentials.to_dict(),
            "tools_config": self.tools_config,
            "workspace": self.workspace,
            "max_iterations": self.max_iterations,
            "max_tool_result_chars": self.max_tool_result_chars,
            "turn_budget_s": self.turn_budget_s,
            "channel": self.channel,
            "chat_id": self.chat_id,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), default=str)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RemoteTurnRequest":
        credentials = payload.get("credentials") or {}
        return cls(
            session_key=str(payload.get("session_key") or ""),
            messages=list(payload.get("messages") or []),
            credentials=ProviderCredentials.from_dict(credentials),
            tools_config=dict(payload.get("tools_config") or {}),
            workspace=payload.get("workspace") or None,
            max_iterations=int(payload.get("max_iterations") or 0),
            max_tool_result_chars=int(payload.get("max_tool_result_chars") or 0),
            turn_budget_s=float(payload.get("turn_budget_s") or 0.0),
            channel=str(payload.get("channel") or ""),
            chat_id=str(payload.get("chat_id") or ""),
            metadata=dict(payload.get("metadata") or {}),
            version=int(payload.get("version") or PROTOCOL_VERSION),
        )

    @classmethod
    def from_json(cls, raw: str) -> "RemoteTurnRequest":
        return cls.from_dict(json.loads(raw))


@dataclass(frozen=True)
class RemoteTurnEvent:
    """One line of the worker's output.

    ``delta``  the model is streaming text the user should see as it arrives.
    ``tool``   a tool started or finished; ``data`` carries the tool event.
    ``final``  the turn ended; ``text`` is the answer and ``data`` the summary
               (stop reason, tools used, usage).
    ``error``  the turn could not run; the caller should fall back to the host.
    ``log``    progress with no user-facing meaning.
    """

    kind: TurnEventKind
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "data": self.data}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), default=str)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RemoteTurnEvent":
        kind = str(payload.get("kind") or "log")
        if kind not in {"delta", "tool", "final", "error", "log"}:
            kind = "log"
        return cls(
            kind=kind,  # type: ignore[arg-type]
            text=str(payload.get("text") or ""),
            data=dict(payload.get("data") or {}),
        )

    @classmethod
    def from_json(cls, raw: str) -> "RemoteTurnEvent":
        return cls.from_dict(json.loads(raw))


def encode_request(request: RemoteTurnRequest) -> str:
    """Serialize a request for staging into the sandbox."""
    return request.to_json()


def decode_request(raw: str) -> RemoteTurnRequest:
    """Parse a staged request. Raises ``ValueError`` on a payload that is not a
    request object -- the worker must fail loudly rather than run a half-read
    turn."""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("turn request must be a JSON object")
    request = RemoteTurnRequest.from_dict(payload)
    if request.version > PROTOCOL_VERSION:
        raise ValueError(
            f"turn request protocol v{request.version} is newer than this worker "
            f"(v{PROTOCOL_VERSION}); refusing to run a turn it may misread"
        )
    if not request.credentials.model:
        raise ValueError("turn request carries no model to run")
    return request


def encode_event(event: RemoteTurnEvent) -> str:
    """One JSONL line. Emitted with a trailing newline by the worker so a
    partially flushed stream still ends on an event boundary."""
    return event.to_json()


def decode_event(raw: str) -> RemoteTurnEvent | None:
    """Parse one output line, or ``None`` for a line that is not an event.

    The worker's stdout also carries whatever the tools or the model library
    print, so a line that is not JSON is skipped rather than treated as an
    error: noise must not be able to fail a turn that is otherwise fine.
    """
    line = raw.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict) or "kind" not in payload:
        return None
    return RemoteTurnEvent.from_dict(payload)
