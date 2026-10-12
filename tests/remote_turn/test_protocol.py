"""The wire format is the one piece that must never be ambiguous.

A request that loses its credential fails a turn; an event line that a stray
``print`` can make unparseable fails a turn; a version the worker misreads fails
a turn in a way that looks like a model problem. All three are tested here as
plain data, with no sandbox, no network and no agent.
"""

from __future__ import annotations

import json

import pytest

from nanobot.remote_turn.protocol import (
    PROTOCOL_VERSION,
    ProviderCredentials,
    RemoteTurnEvent,
    RemoteTurnRequest,
    decode_event,
    decode_request,
    encode_event,
    encode_request,
)


def _request(**overrides: object) -> RemoteTurnRequest:
    payload: dict[str, object] = {
        "session_key": "telegram:42",
        "messages": [{"role": "user", "content": "hi"}],
        "credentials": ProviderCredentials(
            provider="deepseek", model="deepseek-chat", api_key="sk-secret"
        ),
    }
    payload.update(overrides)
    return RemoteTurnRequest(**payload)  # type: ignore[arg-type]


def test_a_request_survives_a_round_trip() -> None:
    request = _request(turn_budget_s=120.0, max_iterations=7, channel="telegram", chat_id="42")

    decoded = decode_request(encode_request(request))

    assert decoded == request
    assert decoded.credentials.api_key == "sk-secret"


def test_the_credential_never_renders_itself() -> None:
    credentials = ProviderCredentials(provider="openai", model="gpt", api_key="sk-live-abc")

    assert "sk-live-abc" not in repr(credentials)
    assert "sk-live-abc" not in str(credentials)


def test_the_credential_is_still_serializable() -> None:
    # Suppressing repr must not be achieved by dropping the value: the sandbox
    # genuinely needs it, so it has to survive to_dict.
    credentials = ProviderCredentials(provider="openai", model="gpt", api_key="sk-live-abc")

    assert credentials.to_dict()["api_key"] == "sk-live-abc"


def test_a_newer_protocol_is_refused() -> None:
    payload = _request().to_dict()
    payload["version"] = PROTOCOL_VERSION + 1

    with pytest.raises(ValueError, match="newer than this worker"):
        decode_request(json.dumps(payload))


def test_a_request_with_no_model_is_refused() -> None:
    payload = _request().to_dict()
    payload["credentials"]["model"] = ""

    with pytest.raises(ValueError, match="no model"):
        decode_request(json.dumps(payload))


def test_a_request_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(ValueError, match="must be a JSON object"):
        decode_request("[1, 2, 3]")


def test_an_event_survives_a_round_trip() -> None:
    event = RemoteTurnEvent(kind="tool", text="exec", data={"phase": "start"})

    assert decode_event(encode_event(event)) == event


def test_stdout_noise_is_skipped_rather_than_fatal() -> None:
    # The worker shares stdout with every tool and library it loads. A line of
    # theirs must not be able to end a turn that is otherwise fine.
    for noise in ("", "   ", "plain log line", "INFO: starting up", "{\"not\": \"an event\"}", "{"):
        assert decode_event(noise) is None


def test_an_unknown_event_kind_degrades_to_a_log() -> None:
    # A worker from the future emitting a kind this host does not know must not
    # crash the parse; it becomes something the host can safely ignore.
    event = decode_event('{"kind": "teleport", "text": "?"}')

    assert event is not None
    assert event.kind == "log"


def test_a_turn_can_carry_a_transcript_verbatim() -> None:
    messages = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "c", "tool_call_id": "1"},
    ]

    decoded = decode_request(encode_request(_request(messages=messages)))

    assert decoded.messages == messages
