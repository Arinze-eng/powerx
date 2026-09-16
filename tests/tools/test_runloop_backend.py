"""Unit tests for the Runloop Devbox execution backend.

These pin the contract the shared sandbox tool depends on:
* config validation and the deterministic per-session devbox name,
* workspace path confinement,
* devbox creation/lookup/resume lifecycle (including a stuck devbox being
  reclaimed instead of wedging every later operation),
* command execution rendering, binary-safe ``write_bytes`` via multipart upload,
* and the task-end keep-alive/teardown behaviour.

The HTTP layer is exercised through a fake ``aiohttp`` session so the suite
never needs a real Runloop account.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from nanobot.agent.tools import runloop_backend
from nanobot.agent.tools.runloop_backend import (
    RunloopError,
    RunloopExecutionBackend,
    RunloopFileNotFound,
    runloop_devbox_name,
    validate_runloop_api_key,
    validate_runloop_api_url,
    validate_runloop_architecture,
    validate_runloop_blueprint,
    validate_runloop_fetch_allow_hosts,
    validate_runloop_keep_alive_seconds,
    validate_runloop_resource_size,
    validate_runloop_snapshot_id,
)


class _Response:
    def __init__(self, status: int, payload: Any = None, raw: bytes | None = None) -> None:
        self.status = status
        self._payload = payload
        self._raw = raw

    async def text(self) -> str:
        if self._payload is None:
            return ""
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload)

    async def read(self) -> bytes:
        if self._raw is not None:
            return self._raw
        return json.dumps(self._payload or {}).encode("utf-8")


class _RequestCtx:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> _Response:
        return self._response

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakeSession:
    """Minimal aiohttp.ClientSession stand-in driven by a route handler."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> _RequestCtx:
        self.calls.append((method, url, kwargs))
        return _RequestCtx(self._handler(method, url, kwargs))


_PATCH: dict[str, Any] = {"handler": None, "session": None}


@pytest.fixture(autouse=True)
def _patch_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every ClientSession in the module through one shared fake session.

    The backend opens a fresh ``ClientSession`` per operation, so the fake
    returns a single shared instance: every call lands on the same object and
    the test can assert against the full request history.
    """
    _PATCH["handler"] = None
    session = _FakeSession(lambda m, u, k: _PATCH["handler"](m, u, k))
    _PATCH["session"] = session
    monkeypatch.setattr(runloop_backend.aiohttp, "ClientSession", lambda *a, **kw: session)
    return None


def _install(handler: Any) -> _FakeSession:
    """Register the route handler and return the shared session."""
    _PATCH["handler"] = handler
    return _PATCH["session"]


class _Config:
    """Duck-typed stand-in for RunloopExecutionConfig."""

    def __init__(self, **overrides: Any) -> None:
        self.api_key = "ak_test_key"
        self.api_url = "https://api.runloop.ai"
        self.snapshot_id = ""
        self.blueprint = ""
        self.resource_size = "SMALL"
        self.architecture = ""
        self.keep_alive_seconds = 3600
        self.fetch_allow_hosts = ""
        self.persist_workspace = True
        for key, value in overrides.items():
            setattr(self, key, value)


def _running(devbox_id: str = "dbx_test") -> _Response:
    return _Response(200, {"id": devbox_id, "name": "px-test", "status": "running"})


# ---------------------------------------------------------------- validation


def test_validate_api_key_accepts_runloop_shape() -> None:
    assert validate_runloop_api_key("ak_example_test_key") == "ak_example_test_key"
    assert validate_runloop_api_key("") == ""
    with pytest.raises(ValueError):
        validate_runloop_api_key("bad key with spaces")


def test_validate_api_url_requires_https() -> None:
    assert validate_runloop_api_url("") == "https://api.runloop.ai"
    assert validate_runloop_api_url("https://api.runloop.ai/") == "https://api.runloop.ai"
    with pytest.raises(ValueError):
        validate_runloop_api_url("http://api.runloop.ai")


def test_validate_resource_size_and_architecture() -> None:
    assert validate_runloop_resource_size("small") == "SMALL"
    assert validate_runloop_resource_size("") == "SMALL"
    with pytest.raises(ValueError):
        validate_runloop_resource_size("HUGE")
    assert validate_runloop_architecture("") == ""
    assert validate_runloop_architecture("ARM64") == "arm64"
    with pytest.raises(ValueError):
        validate_runloop_architecture("mips")


def test_validate_keep_alive_bounds() -> None:
    assert validate_runloop_keep_alive_seconds(3600) == 3600
    with pytest.raises(ValueError):
        validate_runloop_keep_alive_seconds(10)
    with pytest.raises(ValueError):
        validate_runloop_keep_alive_seconds(999_999)


def test_validate_blueprint_and_snapshot_reject_junk() -> None:
    assert validate_runloop_blueprint("owner/my-blueprint") == "owner/my-blueprint"
    assert validate_runloop_blueprint("") == ""
    with pytest.raises(ValueError):
        validate_runloop_blueprint("bad;rm -rf")
    assert validate_runloop_snapshot_id("snap_abc123") == "snap_abc123"
    with pytest.raises(ValueError):
        validate_runloop_snapshot_id("snap/../etc")


def test_fetch_allow_hosts_validation() -> None:
    assert validate_runloop_fetch_allow_hosts("") == ""
    assert validate_runloop_fetch_allow_hosts("*") == "*"
    assert validate_runloop_fetch_allow_hosts("onlyfiles.com,*.gofile.io") == "onlyfiles.com,*.gofile.io"
    with pytest.raises(ValueError):
        validate_runloop_fetch_allow_hosts("has space.com")


# ------------------------------------------------------------ naming / paths


def test_devbox_name_is_deterministic_and_valid() -> None:
    first = runloop_devbox_name("telegram:7757072055")
    second = runloop_devbox_name("telegram:7757072055")
    other = runloop_devbox_name("webui:persist")
    assert first == second, "the same session must always map to the same devbox"
    assert first != other
    assert first.startswith("px-")
    assert len(first) <= 48
    assert runloop_backend._NAME_RE.fullmatch(first)


def test_safe_path_confines_to_workspace() -> None:
    backend = RunloopExecutionBackend(_Config())
    assert backend.workspace == "/home/user"
    assert runloop_backend._safe_path("/") == "/home/user"
    assert runloop_backend._safe_path("notes.txt") == "/home/user/notes.txt"
    assert runloop_backend._safe_path("/home/user/app/main.py") == "/home/user/app/main.py"
    with pytest.raises(ValueError):
        runloop_backend._safe_path("/etc/passwd")
    with pytest.raises(ValueError):
        runloop_backend._safe_path("../../etc/shadow")


# ----------------------------------------------------------------- lifecycle


def test_ensure_devbox_creates_when_none_exists() -> None:
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "GET" and "/v1/devboxes" in url and "name=" in url:
            return _Response(200, {"devboxes": [], "total_count": 0})
        if method == "POST" and url.endswith("/v1/devboxes"):
            return _Response(200, {"id": "dbx_new", "status": "provisioning"})
        if method == "GET" and "/v1/devboxes/dbx_new" in url:
            return _running("dbx_new")
        return _Response(404, "unexpected route")

    session = _install(handler)
    backend = RunloopExecutionBackend(_Config(), devbox_name="px-test")
    devbox_id = asyncio.run(backend.ensure_devbox(session))
    assert devbox_id == "dbx_new"
    assert backend.last_devbox_id == "dbx_new"
    create_calls = [c for c in session.calls if c[0] == "POST" and c[1].endswith("/v1/devboxes")]
    assert create_calls, "a devbox should have been created"
    body = create_calls[0][2]["json"]
    assert body["name"] == "px-test"
    assert body["launch_parameters"]["resource_size_request"] == "SMALL"
    assert body["launch_parameters"]["keep_alive_time_seconds"] == 3600


def test_create_body_prefers_snapshot_over_blueprint() -> None:
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "GET" and "name=" in url:
            return _Response(200, {"devboxes": []})
        if method == "POST" and url.endswith("/v1/devboxes"):
            return _Response(200, {"id": "dbx_new", "status": "provisioning"})
        return _running("dbx_new")

    session = _install(handler)
    backend = RunloopExecutionBackend(
        _Config(snapshot_id="snap_abc", blueprint="owner/bp", architecture="x86_64", resource_size="LARGE"),
        devbox_name="px-test",
    )
    asyncio.run(backend.ensure_devbox(session))
    body = [c for c in session.calls if c[0] == "POST" and c[1].endswith("/v1/devboxes")][0][2]["json"]
    assert body["snapshot_id"] == "snap_abc"
    assert "blueprint_name" not in body, "snapshot and blueprint are mutually exclusive"
    assert body["launch_parameters"]["architecture"] == "x86_64"
    assert body["launch_parameters"]["resource_size_request"] == "LARGE"


def test_ensure_devbox_resumes_a_suspended_devbox() -> None:
    """A suspended devbox keeps its disk: resume it rather than recreating."""
    resumed: list[str] = []
    state = {"status": "suspended"}

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "POST" and url.endswith("/resume"):
            resumed.append(url)
            state["status"] = "running"
            return _Response(200, {"id": "dbx_susp", "status": "running"})
        if "/v1/devboxes/dbx_susp" in url:
            return _Response(200, {"id": "dbx_susp", "status": state["status"]})
        if method == "POST" and url.endswith("/v1/devboxes"):
            return _Response(200, {"id": "dbx_WRONG", "status": "provisioning"})
        return _Response(200, {"devboxes": [{"id": "dbx_susp", "name": "px-test", "status": state["status"]}]})

    session = _install(handler)
    backend = RunloopExecutionBackend(_Config(), devbox_name="px-test")
    backend.last_devbox_id = "dbx_susp"
    devbox_id = asyncio.run(backend.ensure_devbox(session))
    assert devbox_id == "dbx_susp"
    assert resumed, "a suspended devbox must be resumed to preserve its disk"
    assert not [c for c in session.calls if c[0] == "POST" and c[1].endswith("/v1/devboxes")], (
        "resuming must not create a replacement devbox"
    )


def test_ensure_devbox_ignores_terminal_devbox_and_recreates() -> None:
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "GET" and "name=" in url:
            return _Response(200, {"devboxes": [{"id": "dbx_dead", "name": "px-test", "status": "shutdown"}]})
        if method == "POST" and url.endswith("/v1/devboxes"):
            return _Response(200, {"id": "dbx_fresh", "status": "provisioning"})
        return _running("dbx_fresh")

    session = _install(handler)
    backend = RunloopExecutionBackend(_Config(), devbox_name="px-test")
    assert asyncio.run(backend.ensure_devbox(session)) == "dbx_fresh"


def test_stuck_devbox_is_reclaimed_not_reused() -> None:
    """A devbox that never becomes ready must be shut down so work can continue."""
    shut_down: list[str] = []

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "POST" and url.endswith("/shutdown"):
            shut_down.append(url)
            return _Response(200, {"id": "dbx_stuck", "status": "shutdown"})
        if method == "GET" and "name=" in url and "/v1/devboxes?" in url:
            return _Response(200, {"devboxes": [{"id": "dbx_stuck", "name": "px-test", "status": "provisioning"}]})
        if method == "GET" and "/v1/devboxes/dbx_stuck" in url:
            return _Response(200, {"id": "dbx_stuck", "status": "provisioning"})
        if method == "POST" and url.endswith("/v1/devboxes"):
            return _Response(200, {"id": "dbx_fresh", "status": "provisioning"})
        return _running("dbx_fresh")

    session = _install(handler)
    backend = RunloopExecutionBackend(_Config(), devbox_name="px-test")
    original = runloop_backend.RunloopExecutionBackend.wait_ready

    async def fast_wait(self: Any, sess: Any, devbox_id: str, timeout: int = 180) -> dict[str, Any]:
        if devbox_id == "dbx_stuck":
            raise RunloopError("never ready")
        return {"id": devbox_id, "status": "running"}

    runloop_backend.RunloopExecutionBackend.wait_ready = fast_wait  # type: ignore[assignment]
    try:
        assert asyncio.run(backend.ensure_devbox(session)) == "dbx_fresh"
    finally:
        runloop_backend.RunloopExecutionBackend.wait_ready = original  # type: ignore[assignment]
    assert shut_down, "the stuck devbox must be shut down"


def test_wait_ready_reports_terminal_state() -> None:
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        return _Response(200, {"id": "dbx_bad", "status": "failure", "failure_reason": "quota"})

    session = _install(handler)
    backend = RunloopExecutionBackend(_Config(), devbox_name="px-test")
    with pytest.raises(RunloopError, match="terminal"):
        asyncio.run(backend.wait_ready(session, "dbx_bad", timeout=5))


# -------------------------------------------------------------------- exec


def test_run_renders_stdout_stderr_and_exit_code() -> None:
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if url.endswith("/execute_sync"):
            return _Response(
                200,
                {"devbox_id": "dbx_test", "stdout": "hello\n", "stderr": "warn\n", "exit_status": 3},
            )
        if "name=" in url:
            return _Response(200, {"devboxes": [{"id": "dbx_test", "name": "px-test", "status": "running"}]})
        return _running("dbx_test")

    session = _install(handler)
    backend = RunloopExecutionBackend(_Config(), devbox_name="px-test")
    backend.last_devbox_id = "dbx_test"
    out = asyncio.run(backend.run("echo hello"))
    assert "hello" in out
    assert "warn" in out
    assert "[exit_code=3]" in out


def test_run_rejects_empty_and_oversized_commands() -> None:
    backend = RunloopExecutionBackend(_Config())
    with pytest.raises(ValueError):
        asyncio.run(backend.run("   "))
    with pytest.raises(ValueError):
        asyncio.run(backend.run("x" * 20_000))


def test_run_gives_execute_sync_a_window_longer_than_the_command_timeout() -> None:
    """execute_sync blocks for the whole command: the read window must outlast it."""
    seen: dict[str, Any] = {}

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if url.endswith("/execute_sync"):
            seen["timeout"] = kwargs["timeout"].total
            return _Response(200, {"stdout": "ok", "stderr": "", "exit_status": 0})
        if "name=" in url:
            return _Response(200, {"devboxes": [{"id": "dbx_test", "name": "px-test", "status": "running"}]})
        return _running("dbx_test")

    session = _install(handler)
    backend = RunloopExecutionBackend(_Config(), devbox_name="px-test")
    backend.last_devbox_id = "dbx_test"
    asyncio.run(backend.run("sleep 300", timeout=300))
    assert seen["timeout"] >= 330, "the HTTP window must not race a long-running command"


# ------------------------------------------------------------------- files


def test_read_returns_file_contents_and_empty_when_missing() -> None:
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if url.endswith("/read_file_contents"):
            return _Response(200, raw=b"line one\n")
        if "name=" in url:
            return _Response(200, {"devboxes": [{"id": "dbx_test", "name": "px-test", "status": "running"}]})
        return _running("dbx_test")

    session = _install(handler)
    backend = RunloopExecutionBackend(_Config(), devbox_name="px-test")
    backend.last_devbox_id = "dbx_test"
    assert asyncio.run(backend.read("notes.txt")) == "line one\n"


def test_write_posts_file_contents() -> None:
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if url.endswith("/write_file_contents"):
            return _Response(200, {"exit_status": 0})
        if "name=" in url:
            return _Response(200, {"devboxes": [{"id": "dbx_test", "name": "px-test", "status": "running"}]})
        return _running("dbx_test")

    session = _install(handler)
    backend = RunloopExecutionBackend(_Config(), devbox_name="px-test")
    backend.last_devbox_id = "dbx_test"
    asyncio.run(backend.write("notes.txt", "hello world"))
    call = [c for c in session.calls if c[1].endswith("/write_file_contents")][0]
    assert call[2]["json"]["file_path"] == "/home/user/notes.txt"
    assert call[2]["json"]["contents"] == "hello world"


def test_write_uses_multipart_upload_for_binary_payloads() -> None:
    """Binary data must go through upload_file so bytes are not corrupted."""
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if url.endswith("/upload_file"):
            return _Response(200, {})
        if url.endswith("/execute_sync"):
            return _Response(200, {"stdout": "", "stderr": "", "exit_status": 0})
        if "name=" in url:
            return _Response(200, {"devboxes": [{"id": "dbx_test", "name": "px-test", "status": "running"}]})
        return _running("dbx_test")

    session = _install(handler)
    backend = RunloopExecutionBackend(_Config(), devbox_name="px-test")
    backend.last_devbox_id = "dbx_test"
    payload = bytes(range(256))
    asyncio.run(backend.write_bytes("blob.bin", payload))
    uploads = [c for c in session.calls if c[1].endswith("/upload_file")]
    assert uploads, "binary upload should use the multipart upload endpoint"


def test_write_bytes_rejects_oversized_payload() -> None:
    backend = RunloopExecutionBackend(_Config())
    with pytest.raises(ValueError):
        asyncio.run(backend.write_bytes("big.bin", b"x" * (201 * 1024 * 1024)))


def test_fetch_url_enforces_allow_list() -> None:
    backend = RunloopExecutionBackend(_Config())
    with pytest.raises(ValueError, match="allowed fetch hosts"):
        asyncio.run(backend.fetch_url("https://evil.example.com/x", "/home/user/x"))


def test_fetch_url_default_allow_list_includes_gofile() -> None:
    backend = RunloopExecutionBackend(_Config())
    assert backend._is_host_allowed("gofile.io")
    assert backend._is_host_allowed("store1.gofile.io")
    assert not backend._is_host_allowed("evil.example.com")


# --------------------------------------------------------------- lifecycle end


def test_reset_shuts_the_devbox_down() -> None:
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if url.endswith("/shutdown"):
            return _Response(200, {"id": "dbx_test", "status": "shutdown"})
        return _Response(200, {"devboxes": [{"id": "dbx_test", "name": "px-test", "status": "running"}]})

    session = _install(handler)
    backend = RunloopExecutionBackend(_Config(), devbox_name="px-test")
    asyncio.run(backend.reset("dbx_test"))
    assert [c for c in session.calls if c[1].endswith("/shutdown")]


def test_keep_alive_renews_the_devbox_deadline() -> None:
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if url.endswith("/keep_alive"):
            return _Response(200, {})
        return _Response(200, {})

    session = _install(handler)
    backend = RunloopExecutionBackend(_Config(), devbox_name="px-test")
    asyncio.run(backend.keep_alive("dbx_test"))
    assert [c for c in session.calls if c[1].endswith("/keep_alive")]


def test_test_connection_reports_devbox_details() -> None:
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if url.endswith("/execute_sync"):
            return _Response(200, {"stdout": "Linux dbx_test 6.18\n", "stderr": "", "exit_status": 0})
        if "name=" in url:
            return _Response(200, {"devboxes": [{"id": "dbx_test", "name": "px-test", "status": "running"}]})
        return _running("dbx_test")

    session = _install(handler)
    backend = RunloopExecutionBackend(_Config(), devbox_name="px-test")
    backend.last_devbox_id = "dbx_test"
    result = asyncio.run(backend.test_connection())
    assert result["ok"] is True
    assert result["backend"] == "runloop"
    assert result["devbox_id"] == "dbx_test"


def test_missing_api_key_is_reported() -> None:
    backend = RunloopExecutionBackend(_Config(api_key=""))
    with pytest.raises(RunloopError, match="API key"):
        asyncio.run(backend.run("echo hi"))