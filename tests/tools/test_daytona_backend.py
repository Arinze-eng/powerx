from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from nanobot.agent.tools import daytona_backend
from nanobot.agent.tools.daytona_backend import (
    DaytonaError,
    DaytonaExecutionBackend,
    daytona_sandbox_name,
    validate_daytona_api_key,
    validate_daytona_api_url,
    validate_daytona_domain_allow_list,
    validate_daytona_network_allow_list,
    validate_daytona_snapshot,
)
from nanobot.agent.tools.daytona_backend import _safe_path as _dtn_safe_path
from nanobot.config.schema import DaytonaExecutionConfig, ExecutionBackendConfig


def _config(**overrides: Any) -> DaytonaExecutionConfig:
    values: dict[str, Any] = {"api_key": "dtn_test_key"}
    values.update(overrides)
    return DaytonaExecutionConfig(**values)


class _Response:
    def __init__(self, status: int = 200, payload: Any = None, raw: bytes | None = None) -> None:
        self.status = status
        self._payload = payload
        self._raw = raw

    async def text(self) -> str:
        if self._raw is not None:
            return self._raw.decode("utf-8", "replace")
        if self._payload is None:
            return ""
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


class _Calls(list):
    """Call record that can also carry the install helper for handlers."""

    install: Any = None


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    recorded = _Calls()

    def _install(handler: Any) -> None:
        def factory() -> _FakeSession:
            session = _FakeSession(handler)
            session.calls = recorded
            return session

        monkeypatch.setattr(daytona_backend.aiohttp, "ClientSession", factory)

    recorded.install = _install
    return recorded


def test_validators() -> None:
    assert validate_daytona_api_key(" dtn_abc ") == "dtn_abc"
    assert validate_daytona_api_key("") == ""
    with pytest.raises(ValueError):
        validate_daytona_api_key("bad key\nline")
    assert validate_daytona_api_url("https://app.daytona.io/api/") == "https://app.daytona.io/api"
    assert validate_daytona_api_url("").startswith("https://")
    with pytest.raises(ValueError):
        validate_daytona_api_url("ftp://nope")
    assert validate_daytona_snapshot("") == "daytona-small"
    with pytest.raises(ValueError):
        validate_daytona_snapshot("bad snapshot!")
    assert validate_daytona_domain_allow_list("*.pypi.org, example.com") == "*.pypi.org,example.com"
    with pytest.raises(ValueError):
        validate_daytona_domain_allow_list("not a domain!")
    assert validate_daytona_network_allow_list("") == "0.0.0.0/0"
    assert validate_daytona_network_allow_list("10.0.0.0/8,192.168.0.0/16") == "10.0.0.0/8,192.168.0.0/16"
    with pytest.raises(ValueError):
        validate_daytona_network_allow_list("!!!")


def test_sandbox_name_is_deterministic_and_valid() -> None:
    first = daytona_sandbox_name("telegram:12345")
    second = daytona_sandbox_name("telegram:12345")
    assert first == second
    assert first.startswith("px-")
    assert len(first) <= 48
    assert daytona_sandbox_name("") != ""


def test_schema_accepts_daytona_backend() -> None:
    config = ExecutionBackendConfig(backend="daytona")
    assert config.backend == "daytona"
    assert config.daytona.snapshot == "daytona-small"
    assert config.daytona.network_allow_list == "0.0.0.0/0"
    assert config.daytona.ttl_minutes == 60


def test_safe_path_accepts_root_as_workspace() -> None:
    """The LLM calls list with path="/" to see the sandbox root. That must map
    to the /home/daytona workspace instead of raising the "must remain inside"
    ValueError that crashed Daytona sessions on every listing."""
    assert _dtn_safe_path("/") == "/home/daytona"
    assert _dtn_safe_path(" / ") == "/home/daytona"
    assert _dtn_safe_path("/home/daytona") == "/home/daytona"
    assert _dtn_safe_path("notes.md") == "/home/daytona/notes.md"
    with pytest.raises(ValueError):
        _dtn_safe_path("/etc/passwd")
    with pytest.raises(ValueError):
        _dtn_safe_path("/home/other/secret")
    with pytest.raises(ValueError):
        _dtn_safe_path("../escape")
    with pytest.raises(ValueError):
        _dtn_safe_path("")


async def test_ensure_sandbox_creates_with_allowlists(calls: Any, monkeypatch: Any) -> None:  # noqa: ANN401
    created_body: dict[str, Any] = {}

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "GET" and "/sandbox/px-test" in url:
            # No sandbox by that name yet -> ensure_sandbox must create one.
            return _Response(status=404, payload={"error": "not found"})
        if method == "POST" and url.endswith("/sandbox"):
            created_body.update(kwargs.get("json") or {})
            return _Response(status=200, payload={"id": "sbx-1"})
        if method == "GET" and "/sandbox/sbx-1" in url:
            return _Response(
                status=200,
                payload={"id": "sbx-1", "name": "px-test", "state": "started", "toolboxProxyUrl": "https://tb.example"},
            )
        return _Response(status=404, payload={"error": "not found"})

    calls.install(handler)
    backend = DaytonaExecutionBackend(_config(domain_allow_list="*.pypi.org,example.com"), sandbox_name="px-test")

    # No snapshot exists yet: stub the restore so this test stays focused on
    # allowlist propagation (restore triggers its own archive-sandbox create).
    async def _no_restore() -> bool:
        return False

    monkeypatch.setattr(backend, "restore_workspace", _no_restore)
    sandbox_id = await backend.ensure_sandbox(_FakeSession(handler))
    assert sandbox_id == "sbx-1"
    assert created_body.get("domainAllowList") == "*.pypi.org,example.com"
    assert created_body.get("ttlMinutes") == 60
    assert "networkAllowList" not in created_body


async def test_run_executes_command_and_renders(calls: Any) -> None:  # noqa: ANN401
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "GET" and "/sandbox/" in url:
            return _Response(status=200, payload={"id": "sbx-2", "state": "started", "toolboxProxyUrl": "https://tb.example"})
        if method == "POST" and url.endswith("/process/execute"):
            return _Response(status=200, payload={"exitCode": 0, "result": "hello from daytona"})
        return _Response(status=404, payload={"error": "not found"})

    calls.install(handler)
    backend = DaytonaExecutionBackend(_config(), sandbox_name="px-run")
    output = await backend.run("echo hello")
    assert "hello from daytona" in output
    assert "[exit_code=" not in output


async def test_run_reports_nonzero_exit(calls: Any) -> None:  # noqa: ANN401
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "GET" and "/sandbox/" in url:
            return _Response(status=200, payload={"id": "sbx-3", "state": "started", "toolboxProxyUrl": "https://tb.example"})
        if method == "POST" and url.endswith("/process/execute"):
            return _Response(status=200, payload={"exitCode": 2, "result": "boom"})
        return _Response(status=404, payload={"error": "not found"})

    calls.install(handler)
    backend = DaytonaExecutionBackend(_config(), sandbox_name="px-fail")
    output = await backend.run("false")
    assert "[exit_code=2]" in output


async def test_read_missing_file_returns_empty(calls: Any) -> None:  # noqa: ANN401
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "GET" and "/sandbox/" in url:
            return _Response(status=200, payload={"id": "sbx-4", "state": "started", "toolboxProxyUrl": "https://tb.example"})
        if method == "GET" and "/files/download" in url:
            return _Response(status=404, raw=b"not found")
        return _Response(status=404, payload={"error": "not found"})

    calls.install(handler)
    backend = DaytonaExecutionBackend(_config(), sandbox_name="px-read")
    assert await backend.read("/home/daytona/absent.txt") == ""


async def test_test_connection_reports_backend(calls: Any) -> None:  # noqa: ANN401
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "GET" and "/sandbox/" in url:
            return _Response(status=200, payload={"id": "sbx-5", "state": "started", "toolboxProxyUrl": "https://tb.example"})
        if method == "POST" and url.endswith("/process/execute"):
            return _Response(status=200, payload={"exitCode": 0, "result": "Linux sandbox"})
        return _Response(status=404, payload={"error": "not found"})

    calls.install(handler)
    backend = DaytonaExecutionBackend(_config(), sandbox_name="px-test")
    result = await backend.test_connection()
    assert result["ok"] is True
    assert result["backend"] == "daytona"
    assert result["sandbox_id"] == "sbx-5"
    assert "Linux" in result["platform"]


async def test_reset_deletes_sandbox(calls: Any) -> None:  # noqa: ANN401
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "DELETE" and "/sandbox/sbx-9" in url:
            return _Response(status=204)
        return _Response(status=404, payload={"error": "not found"})

    calls.install(handler)
    backend = DaytonaExecutionBackend(_config(), sandbox_name="px-reset")
    await backend.reset("sbx-9")
    delete_calls = [c for c in calls if c[0] == "DELETE"]
    assert delete_calls and "/sandbox/sbx-9" in delete_calls[0][1]


async def test_missing_api_key_raises(calls: Any) -> None:  # noqa: ANN401
    backend = DaytonaExecutionBackend(_config(api_key=""), sandbox_name="px-nokey")
    with pytest.raises(DaytonaError):
        await backend.run("echo nope")


def test_validate_fetch_allow_hosts() -> None:
    from nanobot.agent.tools.daytona_backend import validate_daytona_fetch_allow_hosts

    assert validate_daytona_fetch_allow_hosts("") == ""
    assert validate_daytona_fetch_allow_hosts("*") == "*"
    assert validate_daytona_fetch_allow_hosts("*.pypi.org, example.com") == "*.pypi.org,example.com"
    with pytest.raises(ValueError):
        validate_daytona_fetch_allow_hosts("not a host!")


async def test_ensure_sandbox_sends_default_domain_allow_list(calls: Any) -> None:  # noqa: ANN401
    created_body: dict[str, Any] = {}

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "GET" and "/sandbox/px-default" in url:
            return _Response(status=404, payload={"error": "not found"})
        if method == "POST" and url.endswith("/sandbox"):
            created_body.update(kwargs.get("json") or {})
            return _Response(status=200, payload={"id": "sbx-def"})
        if method == "GET" and "/sandbox/sbx-def" in url:
            return _Response(
                status=200,
                payload={"id": "sbx-def", "state": "started", "toolboxProxyUrl": "https://tb.example"},
            )
        return _Response(status=404, payload={"error": "not found"})

    calls.install(handler)
    backend = DaytonaExecutionBackend(_config(), sandbox_name="px-default")
    await backend.ensure_sandbox(_FakeSession(handler))
    # Default config must send the comprehensive domain allowlist so general
    # internet (registries, AI APIs, GitHub) is reachable out of the box.
    assert created_body.get("domainAllowList") == daytona_backend.DEFAULT_DOMAIN_ALLOW_LIST
    assert "pypi.org" in created_body["domainAllowList"]
    assert "networkAllowList" not in created_body


async def test_ensure_sandbox_wildcard_uses_open_cidr(calls: Any) -> None:  # noqa: ANN401
    created_body: dict[str, Any] = {}

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "GET" and "/sandbox/px-wild" in url:
            return _Response(status=404, payload={"error": "not found"})
        if method == "POST" and url.endswith("/sandbox"):
            created_body.update(kwargs.get("json") or {})
            return _Response(status=200, payload={"id": "sbx-wild"})
        if method == "GET" and "/sandbox/sbx-wild" in url:
            return _Response(
                status=200,
                payload={"id": "sbx-wild", "state": "started", "toolboxProxyUrl": "https://tb.example"},
            )
        return _Response(status=404, payload={"error": "not found"})

    calls.install(handler)
    backend = DaytonaExecutionBackend(_config(domain_allow_list="*"), sandbox_name="px-wild")
    await backend.ensure_sandbox(_FakeSession(handler))
    assert created_body.get("networkAllowList") == "0.0.0.0/0"
    assert "domainAllowList" not in created_body


async def test_fetch_url_respects_configured_hosts(calls: Any) -> None:  # noqa: ANN401
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "GET" and "/sandbox/" in url:
            return _Response(status=200, payload={"id": "sbx-f", "state": "started", "toolboxProxyUrl": "https://tb.example"})
        if method == "POST" and url.endswith("/process/execute"):
            return _Response(status=200, payload={"exitCode": 0, "result": "42"})
        return _Response(status=404, payload={"error": "not found"})

    calls.install(handler)
    backend = DaytonaExecutionBackend(_config(fetch_allow_hosts="example.com,*.example.org"), sandbox_name="px-fetch")
    assert backend._is_host_allowed("example.com") is True
    assert backend._is_host_allowed("sub.example.org") is True
    assert backend._is_host_allowed("example.org") is False
    assert backend._is_host_allowed("evil.com") is False

    wildcard = DaytonaExecutionBackend(_config(fetch_allow_hosts="*"), sandbox_name="px-fetch2")
    assert wildcard._is_host_allowed("anything.example") is True


# --------------------------------------------------------------------------- #
# Daytona workspace persistence ("perfect sandbox" parity with Upstash)      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_daytona_archive_backend_properties():
    backend = DaytonaExecutionBackend(_config(), sandbox_name="px-user-1")
    archive = backend._archive_backend()
    assert archive.persist_workspace is False
    assert archive.ttl_minutes == 43_200
    assert archive.auto_stop_minutes == 0
    assert archive.sandbox_name.startswith("px-archive-")
    assert archive.sandbox_name != backend.sandbox_name


@pytest.mark.asyncio
async def test_daytona_snapshot_stores_workspace_in_archive(monkeypatch):
    backend = DaytonaExecutionBackend(_config(), sandbox_name="px-user-1")
    archive = backend._archive_backend()
    archive_writes: list[tuple[str, bytes]] = []

    async def fake_ensure(session):
        return "sb-main"

    async def fake_exec(session, cmd, timeout):
        return {"exitCode": 0, "result": ""}

    async def fake_download_bytes(session, path):
        return b"daytona-tarball-bytes"

    async def fake_archive_ensure(session):
        return "sb-archive"

    async def fake_archive_write(path, data):
        archive_writes.append((path, data))
        return None

    monkeypatch.setattr(backend, "ensure_sandbox", fake_ensure)
    monkeypatch.setattr(backend, "_exec", fake_exec)
    monkeypatch.setattr(backend, "_download_bytes", fake_download_bytes)
    monkeypatch.setattr(backend, "_archive_backend", lambda: archive)
    monkeypatch.setattr(archive, "ensure_sandbox", fake_archive_ensure)
    monkeypatch.setattr(archive, "write_bytes", fake_archive_write)

    assert await backend.snapshot_workspace() is True
    assert len(archive_writes) == 1
    path, data = archive_writes[0]
    assert path.endswith("/snapshots/px-user-1.tgz")
    assert data == b"daytona-tarball-bytes"


@pytest.mark.asyncio
async def test_daytona_ensure_restores_snapshot_on_fresh_create(monkeypatch):
    backend = DaytonaExecutionBackend(_config(), sandbox_name="px-user-1")
    restored = {"count": 0}

    async def fake_find(session):
        return None  # brand new sandbox

    async def fake_platform_req(session, method, path, **kw):
        if method == "POST" and path == "/sandbox":
            return {"id": "sb-fresh"}
        return {}

    async def fake_wait(session, sb_id, timeout=180):
        backend._toolbox_url = "https://tb.test/sb-fresh"
        return {"id": "sb-fresh", "state": "started"}

    async def fake_restore():
        restored["count"] += 1
        return True

    monkeypatch.setattr(backend, "find_sandbox", fake_find)
    monkeypatch.setattr(backend, "_platform_request", fake_platform_req)
    monkeypatch.setattr(backend, "wait_ready", fake_wait)
    monkeypatch.setattr(backend, "restore_workspace", fake_restore)

    class _StubSession:
        pass

    sb_id = await backend.ensure_sandbox(_StubSession())
    assert sb_id == "sb-fresh"
    assert restored["count"] == 1


@pytest.mark.asyncio
async def test_daytona_release_snapshots_when_persist_enabled(monkeypatch):
    from nanobot.agent.tools import novita_sandbox as ns

    snapshotted = {"done": False}
    reset_called = {"done": False}

    class _FakeDaytonaBackend:
        persist_workspace = True

        async def snapshot_workspace(self):
            snapshotted["done"] = True
            return True

        async def reset(self, sb_id):
            reset_called["done"] = True

    tool = ns.NovitaSandboxTool()
    monkeypatch.setattr(tool, "_selected_backend", lambda: ("daytona", _config()))
    monkeypatch.setattr(tool, "_daytona_backend", lambda cfg, key: _FakeDaytonaBackend())
    monkeypatch.setattr(ns._DAYTONA_STORE, "sandbox_id", lambda key: "sb-123")

    await tool.release_upstash_sandbox(session_key="test-session")
    # The release snapshot runs as a background task so the turn is never blocked.
    # Yield control to the event loop so the task executes.
    await asyncio.sleep(0.01)
    assert snapshotted["done"] is True
    assert reset_called["done"] is False


@pytest.mark.asyncio
async def test_daytona_error_prefers_message_over_generic_error():
    """Verify _platform_request surfaces the descriptive message rather than Bad Request."""
    backend = DaytonaExecutionBackend(_config(), sandbox_name="px-msg-test")
    session = _FakeSession(
        handler=lambda method, url, kwargs: _Response(
            status=400,
            payload={
                "statusCode": 400,
                "error": "Bad Request",
                "message": "Total disk limit exceeded. Maximum allowed: 30GiB.",
            },
        )
    )
    with pytest.raises(DaytonaError) as exc_info:
        await backend._platform_request(session, "POST", "/sandbox", body={})
    assert "Total disk limit exceeded" in str(exc_info.value)
    assert "Bad Request" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_ensure_sandbox_reclaims_and_retries_on_disk_limit():
    """When POST /sandbox fails with disk limit exceeded (400), reap stopped sandboxes and retry."""
    backend = DaytonaExecutionBackend(_config(), sandbox_name="px-disk-test")
    deleted: list[str] = []
    created: list[dict] = []
    attempt = {"count": 0}

    async def fake_find(session):
        return None

    async def fake_wait(session, sb_id, timeout=180):
        backend._toolbox_url = "https://tb.test/sb-retried"
        return {"id": "sb-retried", "state": "started"}

    async def fake_platform_req(session, method, path, **kw):
        if method == "GET" and path == f"/sandbox/{backend.sandbox_name}":
            return None
        if method == "POST" and path == "/sandbox":
            attempt["count"] += 1
            if attempt["count"] == 1:
                raise DaytonaError("POST /sandbox failed with HTTP 400: Total disk limit exceeded. Maximum allowed: 30GiB.")
            created.append(kw.get("body", {}))
            return {"id": "sb-retried"}
        if method == "GET" and path == "/sandbox":
            # List of sandboxes in organization
            return [
                {"id": "sb-old-1", "name": "px-old-session-1", "state": "stopped", "disk": 10},
                {"id": "sb-old-2", "name": "px-old-session-2", "state": "stopped", "disk": 10},
                {"id": "sb-current", "name": "px-disk-test", "state": "creating", "disk": 3},
            ]
        if method == "DELETE":
            deleted.append(path)
            return {"ok": True}
        return {}

    monkeypatch_find = fake_find
    backend.find_sandbox = monkeypatch_find
    backend._platform_request = fake_platform_req
    backend.wait_ready = fake_wait
    backend.restore_workspace = lambda: None

    class _Stub:
        pass

    sb_id = await backend.ensure_sandbox(_Stub())
    assert sb_id == "sb-retried"
    assert attempt["count"] == 2
    # Two stopped px-* sandboxes must have been deleted to free space
    assert any("sb-old-1" in d or "px-old-session-1" in d for d in deleted)
    assert any("sb-old-2" in d or "px-old-session-2" in d for d in deleted)
    assert not any("px-disk-test" in d for d in deleted)


@pytest.mark.asyncio
async def test_find_sandbox_deletes_terminal_state():
    """A sandbox in terminal state (deleted/failed/error) must be cleaned up to free disk and name."""
    backend = DaytonaExecutionBackend(_config(), sandbox_name="px-terminal-test")
    deleted: list[str] = []

    async def fake_platform_req(session, method, path, **kw):
        if method == "GET":
            return {"id": "sb-failed-123", "name": "px-terminal-test", "state": "error"}
        if method == "DELETE":
            deleted.append(path)
            return {"ok": True}
        return {}

    backend._platform_request = fake_platform_req

    class _Stub:
        pass

    result = await backend.find_sandbox(_Stub())
    assert result is None
    assert any("sb-failed-123" in d for d in deleted)

