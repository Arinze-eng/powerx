"""The dispatcher's one job is to never lose a turn.

Every failure it can meet -- no sandbox, a dead worker, a wedged worker, a result
that came home incomplete -- has to come back as ``ok=False`` so the caller runs
the turn on the host. The tests below are written against that contract rather
than against the happy path, because the happy path is the one case where getting
it wrong is invisible until it is a user's lost message.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from nanobot.agent.tools import workspace_bridge
from nanobot.remote_turn.dispatcher import SandboxTurnDispatcher, TurnEventConsumerError
from nanobot.remote_turn.protocol import (
    REQUEST_PATH,
    ProviderCredentials,
    RemoteTurnEvent,
    RemoteTurnRequest,
    encode_event,
)


class _Executor:
    def __init__(self, name: str = "novita", available: bool = True) -> None:
        self.name = name
        self._available = available

    @property
    def available(self) -> bool:
        return self._available


def _request() -> RemoteTurnRequest:
    return RemoteTurnRequest(
        session_key="telegram:7",
        messages=[{"role": "user", "content": "hi"}],
        credentials=ProviderCredentials(
            provider="deepseek", model="deepseek-chat", api_key="sk-secret"
        ),
    )


def _final(text: str = "done", messages: list[dict[str, Any]] | None = None) -> str:
    return encode_event(
        RemoteTurnEvent(
            kind="final",
            text=text,
            data={
                "stop_reason": "completed",
                "tools_used": ["exec"],
                "usage": {"input_tokens": 3, "output_tokens": 4},
                "messages": messages if messages is not None else [{"role": "assistant", "content": text}],
            },
        )
    )


def _patch(monkeypatch: pytest.MonkeyPatch, *, output: str, staged: bool = True) -> dict[str, Any]:
    """Stub the two remote calls a dispatch makes: provision, then the worker.

    ``seen["commands"]`` is every command in order; ``seen["command"]`` stays the
    worker's alone, because that is the one most tests care about.
    """
    seen: dict[str, Any] = {"commands": []}

    async def _write(files: Any, remote_dir: str, *, executor: Any = None) -> tuple[bool, str]:
        seen["files"] = dict(files)
        seen["remote_dir"] = remote_dir
        seen["executor"] = executor
        return (staged, "ok" if staged else "nope")

    async def _run(command: str, *, timeout: int = 120, executor: Any = None) -> tuple[bool, str]:
        seen["commands"].append(command)
        seen["timeout"] = timeout
        # Only the worker produces the turn's output; the provisioning step
        # reports success without emitting events.
        if "-m nanobot.remote_turn.worker" in command:
            seen["command"] = command
            return True, output
        return True, ""

    monkeypatch.setattr(workspace_bridge, "write_files_to_sandbox", _write)
    monkeypatch.setattr(workspace_bridge, "run_remote", _run)
    return seen


def _dispatcher(executor: Any) -> SandboxTurnDispatcher:
    async def _factory() -> Any:
        return executor

    return SandboxTurnDispatcher(timeout_s=30, executor_factory=_factory)


async def test_a_complete_turn_is_adopted(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch(monkeypatch, output=_final())

    outcome = await _dispatcher(_Executor()).dispatch(_request())

    assert outcome.ok is True
    assert outcome.final_content == "done"
    assert outcome.stop_reason == "completed"
    assert outcome.tools_used == ["exec"]
    assert outcome.usage == {"input_tokens": 3, "output_tokens": 4}
    assert outcome.backend == "novita"
    assert outcome.messages == [{"role": "assistant", "content": "done"}]
    assert "-m nanobot.remote_turn.worker" in seen["command"]


async def test_the_credentials_travel_in_a_file_not_in_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    # argv lands in the sandbox's process list, where anything on the box can
    # read it. The transcript is also far too large for a command line.
    seen = _patch(monkeypatch, output=_final())

    await _dispatcher(_Executor()).dispatch(_request())

    assert seen["remote_dir"] == "/tmp"
    assert list(seen["files"]) == [REQUEST_PATH.rsplit("/", 1)[-1]]
    staged = json.loads(seen["files"][REQUEST_PATH.rsplit("/", 1)[-1]])
    assert staged["credentials"]["api_key"] == "sk-secret"
    assert "sk-secret" not in seen["command"]
    assert "hi" not in seen["command"]


async def test_no_sandbox_means_the_host_runs_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, output=_final())

    outcome = await _dispatcher(_Executor(available=False)).dispatch(_request())

    assert outcome.ok is False
    assert "no execution sandbox" in outcome.error


async def test_a_resolution_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, output=_final())

    async def _boom() -> Any:
        raise RuntimeError("no backend configured")

    outcome = await SandboxTurnDispatcher(executor_factory=_boom).dispatch(_request())

    assert outcome.ok is False
    assert "no backend configured" in outcome.error


async def test_staging_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, output=_final(), staged=False)

    outcome = await _dispatcher(_Executor()).dispatch(_request())

    assert outcome.ok is False
    assert "could not stage" in outcome.error


async def test_a_stream_with_no_final_event_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # A worker that died mid-turn leaves a transcript with no end marker. Half a
    # turn must never be shown as a whole one.
    _patch(
        monkeypatch,
        output="\n".join(
            [
                encode_event(RemoteTurnEvent(kind="delta", text="hel")),
                encode_event(RemoteTurnEvent(kind="tool", text="exec")),
            ]
        ),
    )

    outcome = await _dispatcher(_Executor()).dispatch(_request())

    assert outcome.ok is False
    assert "no final event" in outcome.error


async def test_a_non_zero_exit_is_not_itself_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # A turn whose model errored still ends with a final event, and that answer
    # is real: re-running it on the host would bill the user twice for one turn.
    async def _write(files: Any, remote_dir: str, *, executor: Any = None) -> tuple[bool, str]:
        return True, "ok"

    async def _run(command: str, *, timeout: int = 120, executor: Any = None) -> tuple[bool, str]:
        return False, _final("I could not reach the model.")

    monkeypatch.setattr(workspace_bridge, "write_files_to_sandbox", _write)
    monkeypatch.setattr(workspace_bridge, "run_remote", _run)

    outcome = await _dispatcher(_Executor()).dispatch(_request())

    assert outcome.ok is True
    assert outcome.final_content == "I could not reach the model."


async def test_an_empty_answer_is_still_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``ok=True`` with empty text is a finished turn that said nothing. Treating
    # it as a failure would re-run the whole turn on the host.
    _patch(monkeypatch, output=_final(text=""))

    outcome = await _dispatcher(_Executor()).dispatch(_request())

    assert outcome.ok is True
    assert outcome.final_content == ""


async def test_a_final_event_with_no_transcript_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # Adopting it would persist a turn the session cannot remember, and the next
    # turn would be built from a transcript that disagrees with the screen.
    _patch(
        monkeypatch,
        output=encode_event(
            RemoteTurnEvent(kind="final", text="done", data={"stop_reason": "completed"})
        ),
    )

    outcome = await _dispatcher(_Executor()).dispatch(_request())

    assert outcome.ok is False
    assert "no transcript" in outcome.error


async def test_an_error_event_reports_why(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        output="\n".join(
            [
                encode_event(RemoteTurnEvent(kind="log", text="starting")),
                encode_event(RemoteTurnEvent(kind="error", text="no api key")),
            ]
        ),
    )

    outcome = await _dispatcher(_Executor()).dispatch(_request())

    assert outcome.ok is False
    assert outcome.error == "no api key"


async def test_noise_on_stdout_does_not_break_a_good_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, output="\n".join(["INFO loading", _final(), "trailing garbage"]))

    outcome = await _dispatcher(_Executor()).dispatch(_request())

    assert outcome.ok is True
    assert outcome.final_content == "done"


async def test_events_are_forwarded_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        output="\n".join(
            [
                encode_event(RemoteTurnEvent(kind="delta", text="a")),
                encode_event(RemoteTurnEvent(kind="tool", text="exec")),
                encode_event(RemoteTurnEvent(kind="delta", text="b")),
                _final(),
            ]
        ),
    )
    forwarded: list[tuple[str, str]] = []

    async def _on_event(event: RemoteTurnEvent) -> None:
        forwarded.append((event.kind, event.text))

    outcome = await _dispatcher(_Executor()).dispatch(_request(), on_event=_on_event)

    assert outcome.ok is True
    assert forwarded == [("delta", "a"), ("tool", "exec"), ("delta", "b"), ("final", "done")]


async def test_a_wedged_worker_is_abandoned(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _write(files: Any, remote_dir: str, *, executor: Any = None) -> tuple[bool, str]:
        return True, "ok"

    async def _run(command: str, *, timeout: int = 120, executor: Any = None) -> tuple[bool, str]:
        await asyncio.sleep(30)
        return True, ""

    monkeypatch.setattr(workspace_bridge, "write_files_to_sandbox", _write)
    monkeypatch.setattr(workspace_bridge, "run_remote", _run)

    async def _factory() -> Any:
        return _Executor()

    outcome = await SandboxTurnDispatcher(timeout_s=0, executor_factory=_factory).dispatch(_request())

    assert outcome.ok is False
    assert "timed out" in outcome.error


async def test_a_backend_exception_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _write(files: Any, remote_dir: str, *, executor: Any = None) -> tuple[bool, str]:
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(workspace_bridge, "write_files_to_sandbox", _write)

    outcome = await _dispatcher(_Executor()).dispatch(_request())

    assert outcome.ok is False
    assert "backend exploded" in outcome.error


async def test_a_broken_event_consumer_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The one failure that must surface: a caller whose stream consumer is broken
    # is a bug in the caller, not a sandbox problem, and hiding it would turn a
    # code defect into a silent fallback on every turn.
    _patch(monkeypatch, output=_final())

    async def _boom(event: RemoteTurnEvent) -> None:
        raise ValueError("consumer is broken")

    with pytest.raises(TurnEventConsumerError, match="consumer is broken"):
        await _dispatcher(_Executor()).dispatch(_request(), on_event=_boom)
