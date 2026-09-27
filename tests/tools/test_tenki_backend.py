"""Unit tests for the Tenki Sandbox execution backend.

These pin the contract the shared sandbox tool depends on:
* config validation, the deterministic per-session name, and the 1-hour TTL,
* workspace path confinement (a bare ``/`` maps to the workspace, everything
  else outside it is rejected),
* session lifecycle: create with ``sticky=False`` so ``max_duration`` actually
  applies, reattach by id, resolve by name, and reclaim a stuck VM instead of
  wedging the session forever,
* task-end hooks (``keep_alive``/``reset``) resolving an existing VM and never
  creating one,
* command rendering, text/binary round trips with the exec-plane fallback, the
  download guard, and the fetch allow-list.

Tenki's control plane is gRPC, so the backend drives the vendor SDK instead of
speaking REST over ``aiohttp`` the way the Daytona/Runloop/Vercel backends do.
The SDK import seam (``_load_async_client``) is patched with a fake client, so
the suite never needs a real Tenki account.
"""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any

import pytest

from nanobot.agent.tools import tenki_backend
from nanobot.agent.tools.tenki_backend import (
    DEFAULT_API_URL,
    MAX_API_KEYS,
    WORKSPACE,
    TenkiError,
    TenkiExecutionBackend,
    TenkiFileNotFoundError,
    TenkiRotationState,
    is_sdk_available,
    parse_tenki_api_keys,
    tenki_sandbox_name,
    validate_tenki_api_key,
    validate_tenki_api_keys,
    validate_tenki_api_url,
    validate_tenki_cpu_cores,
    validate_tenki_disk_size_gb,
    validate_tenki_fetch_allow_hosts,
    validate_tenki_image,
    validate_tenki_max_duration_seconds,
    validate_tenki_memory_mb,
    validate_tenki_snapshot_id,
    validate_tenki_tag,
)

# --------------------------------------------------------------------- fakes


class _FakeInfo:
    """Stand-in for ``SandboxInfo`` (a plain object, not a method call)."""

    def __init__(self, **overrides: Any) -> None:
        self.id = overrides.pop("id", "sbx_live_1")
        self.name = overrides.pop("name", "powerx-session")
        self.state = overrides.pop("state", "RUNNING")
        self.cpu_cores = overrides.pop("cpu_cores", 2)
        self.memory_mb = overrides.pop("memory_mb", 4096)
        self.disk_size_gb = overrides.pop("disk_size_gb", 10)
        self.timeout_at = overrides.pop(
            "timeout_at", dt.datetime(2026, 9, 27, 5, 0, tzinfo=dt.timezone.utc)
        )
        for key, value in overrides.items():
            setattr(self, key, value)


class _FakeCommandResult:
    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        *,
        timed_out: bool = False,
    ) -> None:
        # ``CommandResult`` exposes ``stdout``/``stderr`` as bytes; the backend
        # must read the ``*_text`` variants, so the fake only provides those.
        self.stdout_text = stdout
        self.stderr_text = stderr
        self.exit_code = exit_code
        self.timed_out = timed_out


class _FakeFS:
    """Stand-in for ``SandboxFS`` — note there is no ``write``/``read``."""

    def __init__(self, sandbox: "_FakeSandbox") -> None:
        self._sandbox = sandbox
        self.files: dict[str, bytes] = {}
        self.mkdirs: list[tuple[str, bool]] = []
        self.downloads: list[tuple[str, str]] = []
        self.fail_writes = False

    async def mkdir(self, path: str, *, recursive: bool = False) -> None:
        self.mkdirs.append((path, recursive))

    async def write_text(self, path: str, content: str) -> None:
        if self.fail_writes:
            raise RuntimeError("boom")
        self.files[path] = content.encode("utf-8")

    async def write_bytes(self, path: str, data: bytes) -> None:
        if self.fail_writes:
            raise RuntimeError("file API declined the payload")
        self.files[path] = bytes(data)

    async def read_bytes(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(f"no such file or directory: {path}")
        return self.files[path]

    async def stat(self, path: str) -> Any:
        if path not in self.files:
            raise FileNotFoundError(f"no such file or directory: {path}")
        size = len(self.files[path])

        class _Stat:
            pass

        stat = _Stat()
        stat.size = size
        return stat

    async def download(self, remote: str, local: str) -> None:
        self.downloads.append((remote, local))
        if remote not in self.files:
            raise FileNotFoundError(f"no such file or directory: {remote}")
        with open(local, "wb") as handle:
            handle.write(self.files[remote])


class _FakeSandbox:
    def __init__(self, **info_overrides: Any) -> None:
        self.id = info_overrides.get("id", "sbx_live_1")
        self.name = info_overrides.get("name", "powerx-session")
        self.info = _FakeInfo(**info_overrides)
        self.fs = _FakeFS(self)
        self.shells: list[tuple[str, int | None]] = []
        self.extended: list[int] = []
        self.closed = False
        self.wait_calls: list[float | None] = []
        self.shell_handler: Any = None
        self.ready_error: Exception | None = None

    @property
    def state(self) -> str:
        return str(self.info.state)

    async def wait_ready(self, timeout: float | None = None) -> Any:
        self.wait_calls.append(timeout)
        if self.ready_error is not None:
            raise self.ready_error
        self.info.state = "RUNNING"
        return self

    async def shell(self, command: str, timeout: int | None = None) -> Any:
        self.shells.append((command, timeout))
        if self.shell_handler is not None:
            return self.shell_handler(command)
        return _FakeCommandResult(stdout="ok")

    async def extend(self, seconds: int) -> None:
        self.extended.append(seconds)

    async def close(self) -> None:
        self.closed = True
        self.info.state = "TERMINATED"


class _FakeAsyncClient:
    """Stand-in for ``tenki.AsyncClient`` routed through one handler."""

    instances: list["_FakeAsyncClient"] = []

    def __init__(self, *, auth_token: str = "", base_url: str = "", **_: Any) -> None:
        self.auth_token = auth_token
        self.base_url = base_url
        self.closed = False
        _FakeAsyncClient.instances.append(self)

    async def create(self, **kwargs: Any) -> Any:
        return await _PATCH["handler"]("create", kwargs)

    async def get(self, session_id: str) -> Any:
        return await _PATCH["handler"]("get", {"session_id": session_id})

    async def list(self, **kwargs: Any) -> Any:
        return await _PATCH["handler"]("list", kwargs)

    async def close(self) -> None:
        self.closed = True


_PATCH: dict[str, Any] = {"handler": None}


@pytest.fixture(autouse=True)
def _patch_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every SDK client through the test's route handler."""
    _PATCH["handler"] = None
    _FakeAsyncClient.instances = []
    monkeypatch.setattr(tenki_backend, "_load_async_client", lambda: _FakeAsyncClient)
    return None


def _install(handler: Any) -> None:
    _PATCH["handler"] = handler
    return None


def _sdk_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode() -> Any:
        raise TenkiError("The Tenki SDK is not installed.")

    monkeypatch.setattr(tenki_backend, "_load_async_client", _explode)
    return None


class _Config:
    """Duck-typed stand-in for ``TenkiExecutionConfig``."""

    def __init__(self, **overrides: Any) -> None:
        self.api_key = "tk_test_key"
        self.api_url = DEFAULT_API_URL
        self.snapshot_id = ""
        self.image = ""
        self.cpu_cores = 2
        self.memory_mb = 4096
        self.disk_size_gb = 0
        self.max_duration_seconds = 3600
        self.tag = "powerx"
        self.fetch_allow_hosts = ""
        self.persist_workspace = True
        for key, value in overrides.items():
            setattr(self, key, value)


def _backend(**overrides: Any) -> TenkiExecutionBackend:
    return TenkiExecutionBackend(_Config(**overrides), sandbox_name="px-test-abc123")


# ---------------------------------------------------------------- validators


def test_validate_api_key_accepts_tenki_shape() -> None:
    assert validate_tenki_api_key("tk_live_abc-123") == "tk_live_abc-123"
    assert validate_tenki_api_key("") == ""
    with pytest.raises(ValueError):
        validate_tenki_api_key("sk_wrong_prefix")
    with pytest.raises(ValueError):
        validate_tenki_api_key("tk_bad key!")


def test_validate_api_url_requires_https() -> None:
    assert validate_tenki_api_url("") == DEFAULT_API_URL
    assert validate_tenki_api_url("https://api.tenki.cloud/") == "https://api.tenki.cloud"
    with pytest.raises(ValueError):
        validate_tenki_api_url("http://api.tenki.cloud")
    with pytest.raises(ValueError):
        validate_tenki_api_url("api.tenki.cloud")


def test_validate_cpu_and_memory_bounds() -> None:
    assert validate_tenki_cpu_cores(2) == 2
    assert validate_tenki_memory_mb(4096) == 4096
    with pytest.raises(ValueError):
        validate_tenki_cpu_cores(0)
    with pytest.raises(ValueError):
        validate_tenki_memory_mb(4097)  # odd megabyte count
    with pytest.raises(ValueError):
        validate_tenki_memory_mb(64)


def test_validate_disk_size_allows_provider_default() -> None:
    assert validate_tenki_disk_size_gb(0) == 0
    assert validate_tenki_disk_size_gb(20) == 20
    with pytest.raises(ValueError):
        validate_tenki_disk_size_gb(3)


def test_validate_max_duration_is_the_ttl_in_seconds() -> None:
    assert validate_tenki_max_duration_seconds(3600) == 3600
    assert validate_tenki_max_duration_seconds(7200) == 7200
    with pytest.raises(ValueError):
        validate_tenki_max_duration_seconds(30)
    with pytest.raises(ValueError):
        validate_tenki_max_duration_seconds(604_801)


def test_validate_image_snapshot_and_tag_reject_junk() -> None:
    assert validate_tenki_image("ubuntu:24.04") == "ubuntu:24.04"
    assert validate_tenki_snapshot_id("snap_abc-1") == "snap_abc-1"
    assert validate_tenki_tag("PowerX") == "powerx"  # normalised
    with pytest.raises(ValueError):
        validate_tenki_image("bad image!")
    with pytest.raises(ValueError):
        validate_tenki_fetch_allow_hosts("good.com, bad host!")


def test_fetch_allow_hosts_wildcard_and_empty() -> None:
    assert validate_tenki_fetch_allow_hosts("") == ""
    assert validate_tenki_fetch_allow_hosts("*") == "*"
    assert validate_tenki_fetch_allow_hosts("a.com, b.com") == "a.com,b.com"


# ------------------------------------------------------------------- naming


def test_sandbox_name_is_deterministic_and_valid() -> None:
    first = tenki_sandbox_name("telegram:12345")
    assert first == tenki_sandbox_name("telegram:12345")
    assert first.startswith("px-")
    assert len(first) <= 48
    assert "--" not in first
    # Different session keys must never collide onto one VM.
    assert tenki_sandbox_name("telegram:12345") != tenki_sandbox_name("telegram:12346")


def test_sandbox_name_handles_hostile_input() -> None:
    name = tenki_sandbox_name("../../etc/passwd; rm -rf /")
    assert name.startswith("px-")
    assert "/" not in name and " " not in name and ";" not in name
    assert tenki_sandbox_name("") == tenki_sandbox_name("")


def test_safe_path_confines_to_workspace() -> None:
    assert tenki_backend._safe_path("notes/a.txt") == f"{WORKSPACE}/notes/a.txt"
    assert tenki_backend._safe_path(f"{WORKSPACE}/notes/a.txt") == f"{WORKSPACE}/notes/a.txt"
    # A bare "/" means the workspace, from the agent's point of view.
    assert tenki_backend._safe_path("/") == WORKSPACE
    assert tenki_backend._safe_path(WORKSPACE) == WORKSPACE
    # An absolute path outside the workspace is rejected — same semantics as
    # the Daytona/Runloop/Vercel/Upstash backends.
    with pytest.raises(ValueError):
        tenki_backend._safe_path("/notes/a.txt")
    with pytest.raises(ValueError):
        tenki_backend._safe_path("/etc/passwd")
    with pytest.raises(ValueError):
        tenki_backend._safe_path("../../etc/shadow")


def test_sdk_available_reflects_the_import_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    assert is_sdk_available() is True  # patched seam resolves
    _sdk_unavailable(monkeypatch)
    assert is_sdk_available() is False


# ---------------------------------------------------------------- lifecycle


def test_create_body_carries_the_ttl_and_non_sticky() -> None:
    captured: dict[str, Any] = {}

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            captured.update(payload)
            return _FakeSandbox(id="sbx_new", name="px-test-abc123")
        raise AssertionError(op)

    _install(handler)
    import asyncio

    backend = _backend(max_duration_seconds=5400, memory_mb=2048, cpu_cores=4)
    asyncio.run(backend.run("true"))

    assert captured["name"] == "px-test-abc123"
    assert captured["max_duration"] == 5400
    # sticky must be False or Tenki ignores max_duration and the VM is immortal.
    assert captured["sticky"] is False
    assert captured["memory_mb"] == 2048
    assert captured["cpu_cores"] == 4
    assert captured["tags"] == ["powerx"]
    assert captured["metadata"] == {"app": "powerx", "managed-by": "nanobot"}
    assert captured["wait"] is True
    assert "disk_size_gb" not in captured  # 0 means "provider default"


def test_create_sends_disk_size_only_when_set() -> None:
    captured: dict[str, Any] = {}

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            captured.update(payload)
            return _FakeSandbox(id="sbx_new")
        raise AssertionError(op)

    _install(handler)
    import asyncio

    asyncio.run(_backend(disk_size_gb=25).run("true"))
    assert captured["disk_size_gb"] == 25


def test_snapshot_wins_over_image() -> None:
    captured: dict[str, Any] = {}

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            captured.update(payload)
            return _FakeSandbox(id="sbx_new")
        raise AssertionError(op)

    _install(handler)
    import asyncio

    asyncio.run(_backend(snapshot_id="snap_1", image="ubuntu:24.04").run("true"))
    assert captured["snapshot_id"] == "snap_1"
    assert "image" not in captured


def test_workspace_quota_rejection_is_actionable() -> None:
    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            raise RuntimeError(
                "requested resources exceed workspace limits: memory_mb required 8192 allowed 4096"
            )
        raise AssertionError(op)

    _install(handler)
    import asyncio

    with pytest.raises(TenkiError) as err:
        asyncio.run(_backend(memory_mb=8192).run("true"))
    message = str(err.value)
    assert "exceed workspace limits" in message
    assert "admin panel" in message


def test_existing_session_is_reattached_by_id() -> None:
    sandbox = _FakeSandbox(id="sbx_existing", name="px-test-abc123", state="RUNNING")
    seen: list[str] = []

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "get":
            seen.append(payload["session_id"])
            return sandbox
        raise AssertionError(f"{op} should not be called")

    _install(handler)
    import asyncio

    backend = _backend()
    backend.last_session_id = "sbx_existing"
    asyncio.run(backend.run("echo hi"))
    assert seen == ["sbx_existing"]
    assert sandbox.shells  # the command ran on the reattached VM


def test_dead_session_id_is_discarded_and_resolved_by_name() -> None:
    replacement = _FakeSandbox(id="sbx_by_name", name="px-test-abc123")

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "get":
            raise RuntimeError("session not found")
        if op == "list":
            return [replacement]
        raise AssertionError(op)

    _install(handler)
    import asyncio

    backend = _backend()
    backend.last_session_id = "sbx_gone"
    asyncio.run(backend.run("echo hi"))
    assert backend.last_session_id == "sbx_by_name"


def test_dead_listed_session_is_skipped_and_recreated() -> None:
    dead = _FakeSandbox(id="sbx_dead", name="px-test-abc123", state="TERMINATED")
    created: list[str] = []

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return [dead]
        if op == "create":
            created.append("yes")
            return _FakeSandbox(id="sbx_fresh", name="px-test-abc123")
        raise AssertionError(op)

    _install(handler)
    import asyncio

    backend = _backend()
    asyncio.run(backend.run("echo hi"))
    assert created == ["yes"]
    assert backend.last_session_id == "sbx_fresh"


def test_stuck_session_is_reclaimed_not_reused() -> None:
    """A VM that will never be ready must not wedge every later operation."""
    stuck = _FakeSandbox(id="sbx_stuck", name="px-test-abc123", state="STARTING")
    stuck.ready_error = TimeoutError("not ready")

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return [stuck]
        if op == "create":
            return _FakeSandbox(id="sbx_after_reclaim")
        raise AssertionError(op)

    _install(handler)
    import asyncio

    backend = _backend()
    asyncio.run(backend.run("echo hi"))
    assert stuck.closed is True
    assert backend.last_session_id == "sbx_after_reclaim"


def test_ready_reports_terminal_state_loudly() -> None:
    """A VM that died on startup must be named as terminal, not merely slow."""
    import asyncio

    failed = _FakeSandbox(id="sbx_failed", name="px-test-abc123", state="FAILED")
    failed.ready_error = RuntimeError("failed to start")

    with pytest.raises(TenkiError) as err:
        asyncio.run(_backend()._ready(failed))
    assert "terminal state" in str(err.value)


# ------------------------------------------------------- task-end lifecycle


def test_keep_alive_never_creates_a_session() -> None:
    sandbox = _FakeSandbox(id="sbx_keep", name="px-test-abc123")

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "get":
            return sandbox
        if op == "list":
            return []
        if op == "create":
            raise AssertionError("keep_alive must never create a VM")
        raise AssertionError(op)

    _install(handler)
    import asyncio

    backend = _backend(max_duration_seconds=3600)
    backend.last_session_id = "sbx_keep"
    asyncio.run(backend.keep_alive())
    assert sandbox.extended == [3600]
    assert backend.last_session_id == "sbx_keep"


def test_keep_alive_without_a_session_is_a_silent_noop() -> None:
    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            raise AssertionError("keep_alive must never create a VM")
        raise AssertionError(op)

    _install(handler)
    import asyncio

    asyncio.run(_backend().keep_alive())  # must not raise


def test_reset_terminates_an_existing_session_only() -> None:
    sandbox = _FakeSandbox(id="sbx_reset", name="px-test-abc123")

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "get":
            return sandbox
        if op == "create":
            raise AssertionError("reset must never create a VM")
        raise AssertionError(op)

    _install(handler)
    import asyncio

    backend = _backend()
    backend.last_session_id = "sbx_reset"
    asyncio.run(backend.reset())
    assert sandbox.closed is True
    assert backend.last_session_id == ""


def test_reset_without_a_session_does_not_create_one() -> None:
    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            raise AssertionError("reset must never create a VM")
        raise AssertionError(op)

    _install(handler)
    import asyncio

    asyncio.run(_backend().reset())


# --------------------------------------------------------------------- exec


def test_run_renders_stdout_stderr_and_exit_code() -> None:
    sandbox = _FakeSandbox(id="sbx_run", name="px-test-abc123")
    sandbox.shell_handler = lambda cmd: _FakeCommandResult(
        stdout="hello\n", stderr="a warning\n", exit_code=3
    )

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    out = asyncio.run(_backend().run("echo hello"))
    assert "hello" in out
    assert "[stderr]" in out and "a warning" in out
    assert "[exit_code=3]" in out


def test_run_reports_timeouts() -> None:
    sandbox = _FakeSandbox(id="sbx_to", name="px-test-abc123")
    sandbox.shell_handler = lambda cmd: _FakeCommandResult(
        stdout="partial", timed_out=True
    )

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    assert "[timed_out=true]" in asyncio.run(_backend().run("sleep 999"))


def test_run_rejects_empty_and_oversized_commands() -> None:
    import asyncio

    backend = _backend()
    with pytest.raises(ValueError):
        asyncio.run(backend.run("   "))
    with pytest.raises(ValueError):
        asyncio.run(backend.run("x" * 12_001))


def test_run_clamps_the_timeout() -> None:
    sandbox = _FakeSandbox(id="sbx_clamp", name="px-test-abc123")

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    asyncio.run(_backend().run("sleep 1", timeout=99_999))
    assert sandbox.shells[0][1] == 900  # _MAX_TIMEOUT


def test_read_returns_file_contents() -> None:
    sandbox = _FakeSandbox(id="sbx_read", name="px-test-abc123")
    sandbox.fs.files[f"{WORKSPACE}/notes.txt"] = b"content here"

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    assert asyncio.run(_backend().read("notes.txt")) == "content here"


def test_read_missing_file_returns_empty_string() -> None:
    sandbox = _FakeSandbox(id="sbx_missing", name="px-test-abc123")

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    assert asyncio.run(_backend().read("nope.txt")) == ""


def test_read_binary_falls_back_to_base64() -> None:
    sandbox = _FakeSandbox(id="sbx_bin", name="px-test-abc123")
    sandbox.fs.files[f"{WORKSPACE}/logo.png"] = b"\x89PNG\xff\xfe"

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    out = asyncio.run(_backend().read("logo.png"))
    assert out == base64.b64encode(b"\x89PNG\xff\xfe").decode("ascii")


def test_read_rejects_paths_outside_the_workspace() -> None:
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(_backend().read("/etc/passwd"))


def test_write_creates_the_parent_directory_and_writes_text() -> None:
    sandbox = _FakeSandbox(id="sbx_write", name="px-test-abc123")

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    asyncio.run(_backend().write("deep/nested/file.txt", "payload"))
    assert (f"{WORKSPACE}/deep/nested", True) in sandbox.fs.mkdirs
    assert sandbox.fs.files[f"{WORKSPACE}/deep/nested/file.txt"] == b"payload"


def test_write_rejects_oversized_content() -> None:
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(_backend().write("big.txt", "x" * 120_001))


def test_write_bytes_uses_the_file_api() -> None:
    sandbox = _FakeSandbox(id="sbx_wb", name="px-test-abc123")

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    payload = bytes(range(256))
    asyncio.run(_backend().write_bytes("blob.bin", payload))
    assert sandbox.fs.files[f"{WORKSPACE}/blob.bin"] == payload


def test_write_bytes_falls_back_to_the_exec_plane() -> None:
    sandbox = _FakeSandbox(id="sbx_wbf", name="px-test-abc123")
    sandbox.fs.fail_writes = True

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    asyncio.run(_backend().write_bytes("blob.bin", b"abc"))
    command = sandbox.shells[-1][0]
    assert "base64 -d" in command
    assert base64.b64encode(b"abc").decode("ascii") in command


def test_write_bytes_rejects_oversized_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    # Shrink the cap rather than allocating 200 MiB inside the suite.
    monkeypatch.setattr(tenki_backend, "_MAX_UPLOAD_BYTES", 8)
    with pytest.raises(ValueError):
        asyncio.run(_backend().write_bytes("huge.bin", b"x" * 9))


def test_install_packages_builds_the_apt_command() -> None:
    sandbox = _FakeSandbox(id="sbx_apt", name="px-test-abc123")

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    asyncio.run(_backend().install_packages(["tesseract-ocr", "libgl1"]))
    command = sandbox.shells[0][0]
    assert "apt-get install" in command
    assert "tesseract-ocr" in command and "libgl1" in command
    with pytest.raises(ValueError):
        asyncio.run(_backend().install_packages(["bad pkg!"]))


def test_fetch_url_enforces_the_allow_list() -> None:
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(_backend().fetch_url("https://evil.example.com/x", "out.bin"))


def test_fetch_url_default_allow_list_includes_gofile() -> None:
    sandbox = _FakeSandbox(id="sbx_fetch", name="px-test-abc123")
    sandbox.shell_handler = lambda cmd: _FakeCommandResult(stdout="1234\n")

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    backend = _backend()
    assert backend._is_host_allowed("gofile.io")
    assert backend._is_host_allowed("store.gofile.io")  # "*.gofile.io"
    assert not backend._is_host_allowed("evil.example.com")
    dest = asyncio.run(backend.fetch_url("https://gofile.io/d/abc", "dl/out.bin"))
    assert dest == f"{WORKSPACE}/dl/out.bin"


def test_download_reports_a_missing_file() -> None:
    sandbox = _FakeSandbox(id="sbx_dl", name="px-test-abc123")
    sandbox.fs.fail_writes = True  # so the base64 fallback also finds nothing

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    with pytest.raises(TenkiFileNotFoundError):
        asyncio.run(_backend().download("nope.bin", "/tmp/px-tenki-nope.bin"))


def test_download_rejects_oversized_files(tmp_path: Any) -> None:
    sandbox = _FakeSandbox(id="sbx_dlbig", name="px-test-abc123")
    sandbox.fs.files[f"{WORKSPACE}/huge.bin"] = b"x" * 10
    # Force the size guard to trip without allocating 50 MiB.
    original = sandbox.fs.stat

    async def oversized(path: str) -> Any:
        stat = await original(path)
        stat.size = 50 * 1024 * 1024 + 1
        return stat

    sandbox.fs.stat = oversized

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    with pytest.raises(TenkiError):
        asyncio.run(_backend().download("huge.bin", tmp_path / "huge.bin"))


def test_download_streams_a_file_to_the_host(tmp_path: Any) -> None:
    sandbox = _FakeSandbox(id="sbx_dl_ok", name="px-test-abc123")
    sandbox.fs.files[f"{WORKSPACE}/art.bin"] = b"\x00\x01\x02binary"

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    target = tmp_path / "art.bin"
    result = asyncio.run(_backend().download("art.bin", target))
    assert target.read_bytes() == b"\x00\x01\x02binary"
    assert str(result) == str(target)


def test_list_runs_find_inside_the_workspace() -> None:
    sandbox = _FakeSandbox(id="sbx_list", name="px-test-abc123")
    sandbox.shell_handler = lambda cmd: _FakeCommandResult(stdout="d /home/tenki/notes\n")

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    out = asyncio.run(_backend().list(""))
    assert "notes" in out
    assert "find" in sandbox.shells[-1][0]


# --------------------------------------------------------------- diagnostics


def test_test_connection_reports_session_details() -> None:
    sandbox = _FakeSandbox(
        id="sbx_diag",
        name="powerx-connection-test",
        state="RUNNING",
        cpu_cores=2,
        memory_mb=4096,
    )
    sandbox.shell_handler = lambda cmd: _FakeCommandResult(stdout="Linux px 6.18.29 x86_64\n")

    async def handler(op: str, payload: dict[str, Any]) -> Any:
        if op == "list":
            return []
        if op == "create":
            return sandbox
        raise AssertionError(op)

    _install(handler)
    import asyncio

    info = asyncio.run(_backend().test_connection())
    assert info["ok"] is True
    assert info["backend"] == "tenki"
    assert info["session_id"] == "sbx_diag"
    assert info["state"] == "RUNNING"
    assert info["memory_mb"] == 4096
    assert info["cpu_cores"] == 2
    assert "6.18.29" in info["platform"]
    # The TTL is proven through the deadline, not a max_duration field.
    assert info["timeout_at"].startswith("2026-09-27T05:00")


def test_missing_api_key_is_reported() -> None:
    import asyncio

    backend = _backend(api_key="")
    with pytest.raises(TenkiError) as err:
        asyncio.run(backend.test_connection())
    assert "API key is not configured" in str(err.value)


def test_missing_sdk_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    _sdk_unavailable(monkeypatch)
    with pytest.raises(TenkiError) as err:
        asyncio.run(_backend().run("true"))
    assert "SDK is not installed" in str(err.value)


# ------------------------------------------------- API key rotation (lanes)
#
# Each ``tk_`` key authenticates into its OWN Tenki workspace and every
# workspace carries its own session quota (measured live at five active
# sessions), so several keys are extra *capacity*. The invariants below are what
# keep that capacity from costing correctness: a session that already exists is
# pinned to the workspace holding its disk, only a brand-new session rotates,
# and only a failure that says "this lane cannot serve us" is allowed to move.


class _LaneWorkspace:
    """One fake Tenki workspace: its own sessions, so lanes stay distinct."""

    def __init__(self, workspace_id: str, *, limit: int = 5) -> None:
        self.workspace_id = workspace_id
        self.limit = limit
        self.active = 0
        self.sessions: dict[str, Any] = {}
        self.created = 0
        self.fail: str | None = None


class _FakeWorkspaceEntry:
    def __init__(self, workspace_id: str) -> None:
        self.id = workspace_id
        self.name = "My Workspace"


class _FakeIdentity:
    """Stand-in for ``Identity`` returned by ``who_am_i()``."""

    def __init__(self, workspace_id: str) -> None:
        self.owner_id = workspace_id
        self.owner_type = "WORKSPACE"
        self.workspaces = (_FakeWorkspaceEntry(workspace_id),)


class _FakeUsage:
    """Stand-in for one ``WorkspaceUsageLimit`` from ``get_usage()``."""

    def __init__(self, key: str, current: int = 0, maximum: int = 5) -> None:
        self.key = key
        self.label = key
        self.unit = "count"
        self.current = current
        self.max = maximum


class _LaneClient:
    """AsyncClient stand-in that answers from the workspace its key unlocks.

    The whole point of a lane is that one key cannot see another key's
    workspace, so the fake enforces exactly that: a client only ever returns its
    own lane's sessions. That is what makes "a session was adopted in lane 1"
    and "a second VM was built in lane 0" distinguishable in a test.
    """

    registry: dict[str, _LaneWorkspace] = {}
    calls: list[tuple[str, str]] = []
    # Every client ever built, so a test can assert when one gets closed: a
    # sandbox handle is only usable while the client that produced it is alive.
    instances: list["_LaneClient"] = []

    def __init__(self, *, auth_token: str = "", base_url: str = "", **_: Any) -> None:
        self.auth_token = auth_token
        self.base_url = base_url
        self.closed = False
        self.workspace = _LaneClient.registry[auth_token]
        _LaneClient.instances.append(self)

    async def who_am_i(self) -> Any:
        return _FakeIdentity(self.workspace.workspace_id)

    async def get_usage(self) -> Any:
        return [_FakeUsage("max_concurrent_jobs", self.workspace.active, self.workspace.limit)]

    async def create(self, **kwargs: Any) -> Any:
        _LaneClient.calls.append((self.auth_token, "create"))
        if self.workspace.fail:
            raise RuntimeError(self.workspace.fail)
        self.workspace.created += 1
        self.workspace.active += 1
        sandbox = _FakeSandbox(
            id=f"sbx-{self.workspace.workspace_id}-{self.workspace.created}",
            # ``_find_session`` reattaches by the sandbox's own ``name``, so the
            # fake must carry it exactly as the real SDK object does.
            name=str(kwargs.get("name") or ""),
        )
        self.workspace.sessions[sandbox.id] = sandbox
        return sandbox

    async def get(self, session_id: str) -> Any:
        _LaneClient.calls.append((self.auth_token, "get"))
        return self.workspace.sessions.get(session_id)

    async def list(self, **kwargs: Any) -> Any:
        _LaneClient.calls.append((self.auth_token, "list"))
        return list(self.workspace.sessions.values())

    async def close(self) -> None:
        self.closed = True


def _lane_env(
    monkeypatch: pytest.MonkeyPatch, count: int = 2
) -> tuple[list[str], list[_LaneWorkspace]]:
    """Independent fake workspaces behind their own keys."""
    keys = [f"tk_lane_{chr(ord('a') + index)}" for index in range(count)]
    workspaces = [_LaneWorkspace(f"ws-{index}") for index in range(count)]
    _LaneClient.registry = dict(zip(keys, workspaces))
    _LaneClient.calls = []
    _LaneClient.instances = []
    monkeypatch.setattr(tenki_backend, "_load_async_client", lambda: _LaneClient)
    return keys, workspaces


def _lane_backend(
    keys: list[str],
    *,
    name: str = "px-test-abc123",
    rotation: Any = None,
    lane_index: int | None = None,
    pinned: Any = None,
    **config_overrides: Any,
) -> TenkiExecutionBackend:
    return TenkiExecutionBackend(
        _Config(api_keys=list(keys), **config_overrides),
        sandbox_name=name,
        lane_index=lane_index,
        rotation=rotation,
        on_lane_pinned=pinned,
    )


def test_parse_api_keys_splits_dedupes_and_keeps_order() -> None:
    assert parse_tenki_api_keys("tk_a,tk_b") == ["tk_a", "tk_b"]
    assert parse_tenki_api_keys("  tk_a ; tk_b \n tk_c  ") == ["tk_a", "tk_b", "tk_c"]
    assert parse_tenki_api_keys('"tk_a",tk_b') == ["tk_a", "tk_b"]
    assert parse_tenki_api_keys(["tk_a", "tk_a", "tk_b"]) == ["tk_a", "tk_b"]
    assert parse_tenki_api_keys(None) == []
    assert parse_tenki_api_keys("") == []


def test_validate_api_keys_enforces_the_lane_cap_and_key_shape() -> None:
    assert validate_tenki_api_keys("tk_a, tk_b") == ["tk_a", "tk_b"]
    with pytest.raises(ValueError):
        validate_tenki_api_keys(",".join(f"tk_{i}" for i in range(MAX_API_KEYS + 1)))
    with pytest.raises(ValueError):
        validate_tenki_api_keys("tk_a,not_a_key")


def test_rotation_walks_lanes_in_order_then_steps_over_parked_ones() -> None:
    state = TenkiRotationState()
    assert [state.next_lane(3) for _ in range(6)] == [0, 1, 2, 0, 1, 2]
    state.park(1)
    assert state.parked() == {1}
    assert [state.next_lane(3) for _ in range(4)] == [0, 2, 2, 0]
    state.clear()
    assert state.parked() == set()


def test_parking_every_lane_still_offers_them_all() -> None:
    state = TenkiRotationState()
    for lane in range(3):
        state.park(lane)
    # A stale cooldown must never become "no lane was tried at all".
    assert sorted({state.next_lane(3) for _ in range(3)}) == [0, 1, 2]


def test_plural_keys_win_over_the_legacy_single_key() -> None:
    backend = _lane_backend(["tk_lane_a", "tk_lane_b"], api_key="tk_legacy")
    assert backend.api_keys == ["tk_lane_a", "tk_lane_b"]
    # Anything reading the singular attribute still sees a usable primary.
    assert backend.api_key == "tk_lane_a"


def test_a_single_key_is_a_one_lane_rotation() -> None:
    backend = _backend(api_key="tk_only")
    assert backend.api_keys == ["tk_only"]
    assert backend.lane_index is None


def test_new_sessions_are_created_on_successive_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    keys, workspaces = _lane_env(monkeypatch)
    rotation = TenkiRotationState()
    pinned: list[int] = []
    first = _lane_backend(keys, name="px-session-one", rotation=rotation, pinned=pinned.append)
    asyncio.run(first.describe())
    second = _lane_backend(keys, name="px-session-two", rotation=rotation)
    asyncio.run(second.describe())
    assert (first.lane_index, second.lane_index) == (0, 1)
    # One VM per lane: rotation really did buy a second workspace.
    assert [workspace.created for workspace in workspaces] == [1, 1]
    # The lane was reported so the caller can persist it.
    assert pinned == [0]


def test_a_new_backend_adopts_the_lane_that_holds_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    keys, workspaces = _lane_env(monkeypatch)
    rotation = TenkiRotationState()
    first = _lane_backend(keys, name="px-session-one", rotation=rotation)
    asyncio.run(first.describe())
    assert first.lane_index == 0
    # The next process has no recorded lane and a cursor that would hand out
    # lane 1 — it must still find the session in lane 0 rather than build a
    # second, empty VM somewhere the user's files are not.
    second = _lane_backend(keys, name="px-session-one", rotation=rotation)
    asyncio.run(second.describe())
    assert second.lane_index == 0
    assert [workspace.created for workspace in workspaces] == [1, 0]


def test_a_pinned_session_never_rotates(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    keys, workspaces = _lane_env(monkeypatch)
    backend = _lane_backend(
        keys, name="px-session-one", lane_index=1, rotation=TenkiRotationState()
    )
    asyncio.run(backend.describe())
    assert backend.lane_index == 1
    assert [workspace.created for workspace in workspaces] == [0, 1]
    # A later operation resolves in lane 1 again instead of looking wider.
    asyncio.run(backend.run("true"))
    assert [workspace.created for workspace in workspaces] == [0, 1]
    assert keys[0] not in [key for key, op in _LaneClient.calls if op == "create"]


def test_a_full_lane_rotates_to_the_next_one_and_parks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    keys, workspaces = _lane_env(monkeypatch)
    workspaces[0].fail = (
        "requested resources exceed workspace limits: "
        "max_concurrent_jobs required 1 allowed 5"
    )
    rotation = TenkiRotationState()
    backend = _lane_backend(keys, name="px-session-one", rotation=rotation)
    asyncio.run(backend.describe())
    assert backend.lane_index == 1
    assert [workspace.created for workspace in workspaces] == [0, 1]
    assert rotation.parked() == {0}
    # A later new session does not go straight back to the lane that refused us.
    later = _lane_backend(keys, name="px-session-two", rotation=rotation)
    asyncio.run(later.describe())
    assert later.lane_index == 1


def test_a_rejected_key_parks_that_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    keys, workspaces = _lane_env(monkeypatch)
    workspaces[0].fail = "unauthenticated: invalid api key"
    rotation = TenkiRotationState()
    backend = _lane_backend(keys, name="px-session-one", rotation=rotation)
    asyncio.run(backend.describe())
    assert backend.lane_index == 1
    assert rotation.parked() == {0}


def test_a_broken_request_does_not_rotate(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    keys, workspaces = _lane_env(monkeypatch)
    workspaces[0].fail = "could not resolve the base image manifest"
    backend = _lane_backend(keys, name="px-session-one", rotation=TenkiRotationState())
    with pytest.raises(TenkiError) as err:
        asyncio.run(backend.describe())
    assert "base image manifest" in str(err.value)
    # Cycling keys would have hidden a real bug behind a retry that happened to
    # be less picky, so no other lane may have been touched.
    assert [workspace.created for workspace in workspaces] == [0, 0]
    assert [key for key, op in _LaneClient.calls if op == "create"] == [keys[0]]


def test_a_pinned_lane_reports_a_full_workspace_instead_of_moving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    keys, workspaces = _lane_env(monkeypatch)
    workspaces[1].fail = "workspace limit reached: max_concurrent_jobs"
    backend = _lane_backend(keys, name="px-session-one", lane_index=1)
    with pytest.raises(TenkiError) as err:
        asyncio.run(backend.describe())
    assert "pinned" in str(err.value)
    # Moving the session to lane 0 would have lost every file written so far.
    assert [workspace.created for workspace in workspaces] == [0, 0]


def test_when_every_lane_refuses_the_last_reason_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    keys, workspaces = _lane_env(monkeypatch)
    for workspace in workspaces:
        workspace.fail = "workspace limit reached"
    backend = _lane_backend(keys, name="px-session-one", rotation=TenkiRotationState())
    with pytest.raises(TenkiError) as err:
        asyncio.run(backend.describe())
    assert "no Tenki lane could accept a new session" in str(err.value)
    assert "tried 2 of 2" in str(err.value)


def test_describe_lanes_reports_each_workspace_and_its_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    keys, workspaces = _lane_env(monkeypatch)
    workspaces[1].active = 3
    rows = asyncio.run(_lane_backend(keys).describe_lanes())
    assert [row["lane"] for row in rows] == [0, 1]
    assert [row["workspace_id"] for row in rows] == ["ws-0", "ws-1"]
    assert [row["active_sessions"] for row in rows] == [0, 3]
    assert [row["session_limit"] for row in rows] == [5, 5]


def test_one_unusable_lane_does_not_hide_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    keys, _ = _lane_env(monkeypatch)
    _LaneClient.registry.pop(keys[1])
    rows = asyncio.run(_lane_backend(keys).describe_lanes())
    assert [row["lane"] for row in rows] == [0, 1]
    assert rows[0]["workspace_id"] == "ws-0"
    assert "error" in rows[1]


def test_describe_reports_the_lane_the_session_lives_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    keys, _ = _lane_env(monkeypatch)
    info = asyncio.run(_lane_backend(keys, name="px-session-one").describe())
    assert info["lane_index"] == 0
    assert info["lane_count"] == 2
    assert info["workspace_id"] == "ws-0"


def test_an_operation_keeps_its_owning_client_open_until_the_body_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the lane rewrite closed the client before the body ran.

    A sandbox handle is only usable while the client that produced it is alive,
    so closing early made every real write fail with "Channel is closed." The
    fake sandbox cannot reproduce that — its methods need no channel — but the
    ownership rule can be asserted directly: the client that resolved the
    session must still be OPEN while the operation body runs, and closed once
    the operation is over.
    """
    import asyncio

    keys, _ = _lane_env(monkeypatch)
    backend = _lane_backend(keys, name="px-session-one", rotation=TenkiRotationState())

    async def operation() -> tuple[bool, bool]:
        async with backend._session() as sandbox:
            open_under_body = [
                client for client in _LaneClient.instances if not client.closed
            ]
            assert open_under_body, "the operation body ran with no open client"
            await sandbox.fs.write_text("/home/tenki/probe.txt", "written under the body")
        return bool(open_under_body), all(c.closed for c in _LaneClient.instances)

    held_open, closed_after = asyncio.run(operation())
    assert held_open
    assert closed_after


def test_reset_closes_the_client_it_attached_with(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    keys, workspaces = _lane_env(monkeypatch)
    backend = _lane_backend(keys, name="px-session-one", rotation=TenkiRotationState())
    asyncio.run(backend.describe())
    session_id = backend.last_session_id
    assert session_id

    _LaneClient.instances = []
    asyncio.run(backend.reset(session_id))

    assert backend.last_session_id == ""
    assert workspaces[0].sessions[session_id].closed
    assert _LaneClient.instances, "reset never built a client"
    assert all(client.closed for client in _LaneClient.instances)
