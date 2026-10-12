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
from nanobot.remote_turn.dispatcher import (
    DIAGNOSTIC_TAIL_CHARS,
    SandboxTurnDispatcher,
    TurnEventConsumerError,
    _no_final_reason,
)
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


def _host_request(workspace: str | None) -> RemoteTurnRequest:
    return RemoteTurnRequest(
        session_key="telegram:7",
        messages=[{"role": "user", "content": "hi"}],
        credentials=ProviderCredentials(
            provider="deepseek", model="deepseek-chat", api_key="sk-secret"
        ),
        workspace=workspace,
    )


async def _dispatch(monkeypatch, request, root: str | None) -> dict[str, Any]:
    """Dispatch with the sandbox root faked to *root*; return the staged JSON.

    The root is stubbed at ``executor_workspace_root`` because that is the real
    seam. The dispatcher holds a ``RemoteExecutor`` wrapper rather than a backend,
    so an attribute on the double would have tested nothing about the production
    shape.
    """
    seen = _patch(monkeypatch, output=_final())

    async def _root(executor: Any, session_key: str | None = None) -> str | None:
        return root

    monkeypatch.setattr(workspace_bridge, "executor_workspace_root", _root)

    outcome = await _dispatcher(_Executor(name="freestyle")).dispatch(request)
    assert outcome.ok is True
    return json.loads(seen["files"]["nanobot_turn_request.json"])


async def test_the_host_workspace_is_replaced_by_the_sandboxs_own_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # THE production bug this file covers. The host fills `workspace` with its own
    # data directory -- /home/nanobot under the gateway image. Handed verbatim to a
    # freestyle VM that path belongs to a user the worker is not, so composing the
    # agent raised "[Errno 13] Permission denied: '/home/nanobot'" and every turn
    # fell back to the host. The switch looked dead even after the package got
    # installed, because this was the next wall.
    staged = await _dispatch(
        monkeypatch, _host_request("/home/nanobot"), "/home/ubuntu/workspace"
    )

    assert staged["workspace"] == "/home/ubuntu/workspace"
    assert "/home/nanobot" not in json.dumps(staged)


async def test_an_executor_without_a_root_leaves_the_request_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A backend that does not advertise its root is a reason to guess nothing, not
    # a reason to guess wrong: the worker resolves its own default.
    staged = await _dispatch(monkeypatch, _host_request("/srv/somewhere"), None)

    assert staged["workspace"] == "/srv/somewhere"


async def test_a_missing_workspace_still_gets_the_sandbox_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The host sends no workspace here. Pinning it anyway is the point: a turn's
    # files must land in the directory the sandbox's own tools are jailed to, not
    # in a config default those tools will not look at.
    staged = await _dispatch(monkeypatch, _host_request(None), "/home/ubuntu/workspace")

    assert staged["workspace"] == "/home/ubuntu/workspace"


async def test_no_root_and_no_workspace_stays_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing is known about where this box keeps files, so nothing is invented.
    staged = await _dispatch(monkeypatch, _host_request(None), None)

    assert staged["workspace"] is None


async def test_a_failing_root_lookup_does_not_lose_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Resolving a root touches the backend, so it can raise. That must not become a
    # new way to fail a turn: the request goes out as the host wrote it and the
    # worker's own guard decides what to do with the path.
    seen = _patch(monkeypatch, output=_final())

    async def _boom(executor: Any, session_key: str | None = None) -> str | None:
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(workspace_bridge, "executor_workspace_root", _boom)

    outcome = await _dispatcher(_Executor(name="freestyle")).dispatch(
        _host_request("/home/nanobot")
    )

    assert outcome.ok is True
    staged = json.loads(seen["files"]["nanobot_turn_request.json"])
    assert staged["workspace"] == "/home/nanobot"


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


# --------------------------------------------------- the fallback reason itself
#
# A silent worker used to be reported as "the sandbox worker produced no final
# event (ok=True)". Production then showed a Freestyle 409 -- the VM had spent its
# whole runtime budget -- sitting in the output that message discarded, and the
# line was still pointing at the worker. These tests hold the reason to what the
# box actually said.


def test_the_reason_quotes_the_sandbox_output_that_was_discarded() -> None:
    conflict = (
        'Freestyle POST /v5/vms/vm-f313a/exec-await returned 409: '
        '{"code":"CONFLICT","message":"has used its total runtime budget of 6600s"}'
    )

    reason = _no_final_reason(
        ok=True, output=conflict, events=[], provision_failed=False
    )

    # The sentence that names the real cause must survive into the log line.
    assert "409" in reason
    assert "runtime budget" in reason
    # ok=True is preserved: it is the fact that makes this interesting.
    assert "ok=True" in reason


def test_the_reason_distinguishes_silence_from_a_backend_answer() -> None:
    # An empty output means the command ran and printed nothing. A non-empty one
    # means something answered -- usually the backend, not the worker -- and those
    # are two different incidents for whoever is on call.
    empty = _no_final_reason(ok=True, output="", events=[], provision_failed=False)
    assert "no output at all" in empty

    answered = _no_final_reason(
        ok=True, output="vm is paused", events=[], provision_failed=False
    )
    assert "vm is paused" in answered


def test_the_reason_says_so_when_provisioning_already_failed() -> None:
    # "the package never installed" and "the worker went quiet on a good box" are
    # fixed by different people; the reason should not make the reader guess.
    reason = _no_final_reason(
        ok=True, output="", events=[], provision_failed=True
    )

    assert "provisioning the sandbox had already failed" in reason


def test_the_reason_stays_a_single_bounded_log_line() -> None:
    # The tail is quoted, not the whole transcript: this goes into a log line and
    # a sandbox can emit megabytes.
    long_output = "y" * 20_000

    reason = _no_final_reason(
        ok=False, output=long_output, events=[], provision_failed=False
    )

    quoted = reason.split(": ", 1)[1]
    assert quoted == "…" + long_output[-DIAGNOSTIC_TAIL_CHARS:]
    assert len(reason) <= DIAGNOSTIC_TAIL_CHARS + 200


async def test_a_silent_sandbox_reports_what_the_backend_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End to end through dispatch(): the reason the host logs is the provider's
    # own words, not a summary of the worker's absence.
    async def _write(files: Any, remote_dir: str, *, executor: Any = None) -> tuple[bool, str]:
        return True, "ok"

    async def _run(command: str, *, timeout: int = 120, executor: Any = None) -> tuple[bool, str]:
        if "-m nanobot.remote_turn.worker" in command:
            return True, "exec-await returned 409: budget spent"
        return True, ""

    monkeypatch.setattr(workspace_bridge, "write_files_to_sandbox", _write)
    monkeypatch.setattr(workspace_bridge, "run_remote", _run)

    outcome = await _dispatcher(_Executor(name="freestyle")).dispatch(_request())

    assert outcome.ok is False
    assert "409" in outcome.error
    assert "budget spent" in outcome.error


async def test_a_provisioning_failure_is_named_in_the_fallback_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _write(files: Any, remote_dir: str, *, executor: Any = None) -> tuple[bool, str]:
        return True, "ok"

    async def _run(command: str, *, timeout: int = 120, executor: Any = None) -> tuple[bool, str]:
        if "-m nanobot.remote_turn.worker" in command:
            return True, ""          # the worker said nothing
        return False, "no uv on this box"   # provisioning failed

    monkeypatch.setattr(workspace_bridge, "write_files_to_sandbox", _write)
    monkeypatch.setattr(workspace_bridge, "run_remote", _run)

    outcome = await _dispatcher(_Executor(name="freestyle")).dispatch(_request())

    assert outcome.ok is False
    assert "provisioning the sandbox had already failed" in outcome.error
