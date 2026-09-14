"""Unit tests for the Upstash Box execution backend and admin wiring."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nanobot.agent.tools.upstash_backend import (
    UpstashExecutionBackend,
    _safe_path,
    upstash_box_name,
    validate_upstash_api_key,
    validate_upstash_base_url,
    validate_upstash_runtime,
    validate_upstash_size,
)


def _config(**overrides):
    values = {
        "api_key": "box_test",
        "base_url": "https://us-east-1.box.upstash.com",
        "runtime": "python",
        "size": "small",
        "ttl_s": 3600,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_validators_reject_bad_values():
    with pytest.raises(ValueError):
        validate_upstash_api_key("bad key\nwith newline")
    with pytest.raises(ValueError):
        validate_upstash_base_url("http://insecure.example.com")
    with pytest.raises(ValueError):
        validate_upstash_runtime("brainfuck")
    with pytest.raises(ValueError):
        validate_upstash_size("huge")
    assert validate_upstash_api_key("") == ""
    assert validate_upstash_base_url("") == "https://us-east-1.box.upstash.com"


def test_box_names_are_stable_and_valid():
    first = upstash_box_name("telegram:12345")
    second = upstash_box_name("telegram:12345")
    assert first == second
    assert first.startswith("px-telegram-")
    assert upstash_box_name("webui:abc") != first


def test_safe_path_confinement():
    assert _safe_path("notes.md") == "/workspace/home/notes.md"
    assert _safe_path("/workspace/home/a/b.txt") == "/workspace/home/a/b.txt"
    with pytest.raises(ValueError):
        _safe_path("/etc/passwd")
    with pytest.raises(ValueError):
        _safe_path("../escape")


def test_backend_reads_config_fields():
    backend = UpstashExecutionBackend(_config(size="large", ttl_s=120), box_name="px-test-1")
    assert backend.size == "large"
    assert backend.ttl_s == 120
    assert backend.box_name == "px-test-1"
    # Invalid names fall back instead of raising at construction time.
    assert UpstashExecutionBackend(_config(), box_name="BAD NAME!!").box_name == "powerx-session"


@pytest.mark.parametrize(
    ("backend_value", "expected"),
    [("novita", "novita"), ("vps", "vps"), ("upstash", "upstash")],
)
def test_selected_backend_routing(monkeypatch, backend_value, expected):
    from nanobot.agent.tools import novita_sandbox as ns

    cfg = SimpleNamespace(
        backend=backend_value,
        vps=SimpleNamespace(host="1.2.3.4"),
        upstash=_config(),
        novita_template=None,
    )
    monkeypatch.setattr(ns.NovitaSandboxTool, "_execution_config", staticmethod(lambda: cfg))
    tool = ns.NovitaSandboxTool()
    name, config = tool._selected_backend()
    assert name == expected


@pytest.mark.asyncio
async def test_execute_reports_missing_upstash_key(monkeypatch):
    from nanobot.agent.tools import novita_sandbox as ns

    cfg = SimpleNamespace(
        backend="upstash",
        vps=SimpleNamespace(host=""),
        upstash=_config(api_key=""),
        novita_template=None,
    )
    monkeypatch.setattr(ns.NovitaSandboxTool, "_execution_config", staticmethod(lambda: cfg))
    result = await ns.NovitaSandboxTool().execute(action="run", command="echo hi")
    assert "Upstash" in str(result) and "API key" in str(result)


def test_admin_settings_expose_upstash_shape():
    from nanobot.config.schema import Config

    cfg = Config()
    assert cfg.execution.backend == "novita"
    assert cfg.execution.upstash.runtime == "python"
    assert cfg.execution.upstash.size == "small"
    assert cfg.execution.novita_template.memory_mb == 4096
    # repr must not leak the API key
    cfg.execution.upstash.api_key = "box_secret"
    assert "box_secret" not in repr(cfg.execution.upstash)


def test_execution_env_overlay_for_upstash(monkeypatch):
    from nanobot.config.schema import Config
    from nanobot.execution_env import apply_render_execution_env

    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "upstash")
    monkeypatch.setenv("UPSTASH_BOX_API_KEY", "box_env_key")
    monkeypatch.setenv("NANOBOT_UPSTASH_SIZE", "medium")
    monkeypatch.setenv("NANOBOT_UPSTASH_TTL", "7200")
    config = apply_render_execution_env(Config())
    assert config.execution.backend == "upstash"
    assert config.execution.upstash.api_key == "box_env_key"
    assert config.execution.upstash.size == "medium"
    assert config.execution.upstash.ttl_s == 7200


def test_admin_save_roundtrip_upstash(tmp_path, monkeypatch):
    import nanobot.admin_registry as admin_registry

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)
    payload = {
        "backend": "upstash",
        "upstashApiKey": "box_saved_key",
        "upstashRuntime": "node",
        "upstashSize": "large",
        "upstashTtlSeconds": 900,
        "novitaCpuCount": 4,
        "novitaMemoryMb": 8192,
    }
    response = admin_registry._save_execution_settings(payload, refresh_runtime_config=None)
    body = json.loads(bytes(response.body).decode())
    assert body["ok"] is True
    assert body["backend"] == "upstash"
    assert body["upstash"]["size"] == "large"
    assert body["upstash"]["apiKeyConfigured"] is True
    assert body["novitaTemplate"] == {"cpu_count": 4, "memory_mb": 8192}
    # Secrets are never echoed back.
    assert "box_saved_key" not in bytes(response.body).decode()


def test_admin_page_contains_upstash_controls():
    import nanobot.admin_registry as admin_registry

    section = admin_registry._execution_admin_section()
    for marker in (
        "Upstash Box",
        "upstashApiKey",
        "upstashSize",
        "upstashTtl",
        "novitaMemory",
        "admin.execution.save",
    ):
        assert marker in section


def test_execution_section_loads_via_get_fetch_not_socket():
    """The execution section must load settings via a plain GET fetch.

    The admin WebSocket client (window.nanobotAdminRequest) only handles
    allowlisted mutation actions (save/test). Routing the initial read through
    the socket as 'admin.execution.get' would return "unknown WebUI mutation
    action", so the section reads settings with a GET fetch to
    /api/admin/execution-settings (the same pattern the provider section uses
    for /api/admin/provider-settings). Save/test still go through the socket.
    """
    import nanobot.admin_registry as admin_registry

    section = admin_registry._execution_admin_section()
    # No local helper / no synchronous load call.
    assert "const adminRequest=" not in section
    assert "void load().catch(" not in section
    # Load uses a GET fetch, never the mutation socket.
    assert "fetch('/api/admin/execution-settings'" in section
    assert "admin.execution.get" not in section
    # Save/test still use the shared window client, deferred until it exists.
    assert "window.nanobotAdminRequest('admin.execution.save'" in section
    assert "window.nanobotAdminRequest('admin.execution.test'" in section
    assert "__execReady" in section
    assert "typeof window.nanobotAdminRequest==='function'" in section
    assert "adminRequest('admin.execution.get')" not in section
    assert "adminRequest('admin.execution.save'" not in section
    assert "adminRequest('admin.execution.test'" not in section


# --------------------------------------------------------------------------- #
# Cold-box / missing-file recovery (the "could not write" fix)                #
# --------------------------------------------------------------------------- #

from nanobot.agent.tools.upstash_backend import (  # noqa: E402
    UpstashError,
    UpstashFileNotFound,
    _looks_like_missing_file,
)


def test_detects_missing_file_500():
    assert _looks_like_missing_file("Failed to read file")
    assert _looks_like_missing_file("no such file or directory")
    assert not _looks_like_missing_file("rate limit exceeded")


@pytest.mark.asyncio
async def test_read_of_missing_file_returns_empty(monkeypatch):
    """A read of a never-written path must NOT raise — it returns ''."""
    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")

    async def fake_ensure(session):
        return "box-x"

    async def fake_request(session, method, path, **kw):
        if "files/read" in path:
            raise UpstashFileNotFound("GET .../files/read: file not found (Failed to read file)")
        return {}

    monkeypatch.setattr(backend, "ensure_box", fake_ensure)
    monkeypatch.setattr(backend, "_request", fake_request)
    result = await backend.read("does-not-exist.txt")
    assert result == ""


@pytest.mark.asyncio
async def test_write_retries_once_on_transient_error(monkeypatch):
    """First write attempt fails with a transient UpstashError; second succeeds.

    This models the cold/expired box that rejects the first call — the retry after
    re-ensuring recovers without surfacing 'could not write' to the user.
    """
    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")
    attempts = {"n": 0}

    async def fake_ensure(session):
        return "box-x"

    async def fake_exec(session, box_id, cmd, timeout):
        return {"exit_code": 0, "output": "", "error": ""}

    async def fake_request(session, method, path, **kw):
        if "files/write" in path:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise UpstashError("POST .../files/write failed with HTTP 500: transient")
            return {"success": True}
        return {}

    monkeypatch.setattr(backend, "ensure_box", fake_ensure)
    monkeypatch.setattr(backend, "_exec", fake_exec)
    monkeypatch.setattr(backend, "_request", fake_request)
    # Read-back verification: pretend content is present after successful write.
    async def fake_read(path):
        return "hello"
    monkeypatch.setattr(backend, "read", fake_read)
    # Avoid real sleep in tests.
    async def no_sleep(_):
        return None
    monkeypatch.setattr("nanobot.agent.tools.upstash_backend.asyncio.sleep", no_sleep)

    await backend.write("out.txt", "hello")
    assert attempts["n"] == 2  # retried and succeeded


@pytest.mark.asyncio
async def test_write_raises_after_two_failures(monkeypatch):
    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")

    async def fake_ensure(session):
        return "box-x"

    async def fake_exec(session, box_id, cmd, timeout):
        return {"exit_code": 0, "output": "", "error": ""}

    async def always_fail(session, method, path, **kw):
        if "files/write" in path:
            raise UpstashError("POST .../files/write failed with HTTP 503: down")
        return {}

    monkeypatch.setattr(backend, "ensure_box", fake_ensure)
    monkeypatch.setattr(backend, "_exec", fake_exec)
    monkeypatch.setattr(backend, "_request", always_fail)
    async def no_sleep(_):
        return None
    monkeypatch.setattr("nanobot.agent.tools.upstash_backend.asyncio.sleep", no_sleep)

    with pytest.raises(UpstashError):
        await backend.write("out.txt", "data")


@pytest.mark.asyncio
async def test_download_missing_file_clear_error(monkeypatch):
    """stat failing twice -> UpstashFileNotFound, not the old opaque message."""
    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")

    async def fake_run(cmd, timeout=120):
        # stat returns exit_code != 0 with empty size line
        return "\n[exit_code=1]"

    monkeypatch.setattr(backend, "run", fake_run)
    async def no_sleep(_):
        return None
    monkeypatch.setattr("nanobot.agent.tools.upstash_backend.asyncio.sleep", no_sleep)

    with pytest.raises(UpstashFileNotFound):
        await backend.download("missing.bin", "/tmp/x")



# --------------------------------------------------------------------------- #
# Admin key rotation ("endpoint has changed" fix)                             #
# --------------------------------------------------------------------------- #


def test_execution_env_preserves_admin_upstash_key(monkeypatch):
    """A saved admin key must win over the durable env default.

    The overlay used to re-apply the env key on EVERY config load, silently
    reverting an admin key change and making the box fail with stale
    credentials that read as "the endpoint has changed".
    """
    from nanobot.config.schema import Config
    from nanobot.execution_env import apply_render_execution_env

    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "upstash")
    monkeypatch.setenv("UPSTASH_BOX_API_KEY", "box_env_key")
    monkeypatch.setenv("NANOBOT_UPSTASH_BASE_URL", "https://eu-central-1.box.upstash.com")
    monkeypatch.setenv("NANOBOT_UPSTASH_SIZE", "medium")
    monkeypatch.setenv("NANOBOT_UPSTASH_TTL", "7200")
    config = Config()
    config.execution.backend = "upstash"
    config.execution.upstash.api_key = "box_admin_rotated"
    config.execution.upstash.base_url = "https://ap-south-1.box.upstash.com"
    config.execution.upstash.size = "large"
    config.execution.upstash.ttl_s = 9000
    overlay = apply_render_execution_env(config)
    assert overlay.execution.upstash.api_key == "box_admin_rotated"
    assert overlay.execution.upstash.base_url == "https://ap-south-1.box.upstash.com"
    assert overlay.execution.upstash.size == "large"
    assert overlay.execution.upstash.ttl_s == 9000
    # Env still fills the gaps on a fresh config (durable default behaviour).
    fresh = apply_render_execution_env(Config())
    assert fresh.execution.upstash.api_key == "box_env_key"
    assert fresh.execution.upstash.base_url == "https://eu-central-1.box.upstash.com"


class _FakeResp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def read(self):
        return self._text.encode("utf-8")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, status: int) -> None:
        self._status = status

    def request(self, method, url, **kw):
        return _FakeResp(self._status, '{"error": "unauthorized"}')


@pytest.mark.asyncio
async def test_rejected_key_says_key_not_endpoint():
    """HTTP 401 must read as a credential problem, never an endpoint problem."""
    backend = UpstashExecutionBackend(_config(api_key="box_stale"), box_name="px-test-1")
    with pytest.raises(UpstashError) as exc:
        await backend._request(_FakeSession(401), "GET", "/v2/box")
    message = str(exc.value)
    assert "rejected the configured API key" in message
    assert "endpoint itself is unchanged" in message
    with pytest.raises(UpstashError) as exc403:
        await backend._request(_FakeSession(403), "GET", "/v2/box")
    assert "rejected the configured API key" in str(exc403.value)


# --------------------------------------------------------------------------- #
# "Perfect box" persistence                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_snapshot_stores_workspace_in_archive_box(monkeypatch):
    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")
    archive_calls: list[str] = []

    archive = SimpleNamespace(
        ensure_box=None,
        write_bytes=None,
        workspace="/workspace/home",
        box_name="px-archive-x",
    )

    async def fake_ensure(session):
        return "box-self"

    async def fake_exec(session, box_id, cmd, timeout):
        return {"exit_code": 0, "output": "", "error": ""}

    async def fake_read_bytes(session, box_id, path, *, timeout=240):
        return b"tarball-bytes"

    async def fake_archive_ensure(session):
        return "box-archive"

    async def fake_archive_write(path, data):
        archive_calls.append((path, data))
        return None

    monkeypatch.setattr(backend, "ensure_box", fake_ensure)
    monkeypatch.setattr(backend, "_exec", fake_exec)
    monkeypatch.setattr(backend, "_read_box_bytes", fake_read_bytes)
    monkeypatch.setattr(backend, "_archive_backend", lambda: archive)
    monkeypatch.setattr(archive, "ensure_box", fake_archive_ensure)
    monkeypatch.setattr(archive, "write_bytes", fake_archive_write)

    assert await backend.snapshot_workspace() is True
    path, data = archive_calls[0]
    assert path.endswith("/snapshots/px-test-1.tgz")
    assert data == b"tarball-bytes"
    # The archive must never snapshot itself (real method, not the stub).
    real_archive_backend = UpstashExecutionBackend(_config(), box_name="px-test-1")._archive_backend()
    assert real_archive_backend.persist_workspace is False
    assert real_archive_backend.ttl_s == 86_400
    assert real_archive_backend.box_name != backend.box_name


@pytest.mark.asyncio
async def test_ensure_box_restores_snapshot_on_fresh_box(monkeypatch):
    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")
    restored: list[bool] = []

    async def fake_restore():
        restored.append(True)
        return True

    async def fake_wait_ready(session, box_id, timeout=120):
        return None

    async def fake_request(session, method, path, **kw):
        if method == "GET" and path == "/v2/box":
            return {"boxes": []}
        if method == "POST" and path == "/v2/box":
            return {"id": "box-new"}
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(backend, "wait_ready", fake_wait_ready)
    monkeypatch.setattr(backend, "_request", fake_request)
    monkeypatch.setattr(backend, "restore_workspace", fake_restore)
    box_id = await backend.ensure_box(_FakeSession(None))
    assert box_id == "box-new"
    assert restored == [True]


@pytest.mark.asyncio
async def test_release_keeps_box_when_persist_enabled(monkeypatch):
    """release_upstash_sandbox must snapshot instead of deleting when persisting."""
    from nanobot.agent.tools import novita_sandbox as ns

    cfg = SimpleNamespace(
        backend="upstash",
        vps=SimpleNamespace(host=""),
        upstash=_config(),
        novita_template=None,
    )
    tool = ns.NovitaSandboxTool()
    monkeypatch.setattr(ns.NovitaSandboxTool, "_selected_backend", staticmethod(
        lambda: ("upstash", cfg.upstash)))
    monkeypatch.setattr(ns._UPSTASH_STORE, "sandbox_id", lambda key: "box-live")
    removed: list[str] = []
    monkeypatch.setattr(ns._UPSTASH_STORE, "remove", lambda key: removed.append(key))
    snapped: list[bool] = []
    deleted: list[str] = []

    backend = SimpleNamespace(persist_workspace=True)

    async def fake_snap():
        snapped.append(True)
        return True

    async def fake_reset(box_id):
        deleted.append(box_id)

    backend.snapshot_workspace = fake_snap
    backend.reset = fake_reset
    monkeypatch.setattr(tool, "_upstash_backend", lambda config, key: backend)

    await tool.release_upstash_sandbox("webui:persist")
    assert snapped == [True]
    assert deleted == []
    assert removed == []


@pytest.mark.asyncio
async def test_release_still_deletes_when_persist_disabled(monkeypatch):
    from nanobot.agent.tools import novita_sandbox as ns

    cfg = SimpleNamespace(
        backend="upstash",
        vps=SimpleNamespace(host=""),
        upstash=_config(),
        novita_template=None,
    )
    tool = ns.NovitaSandboxTool()
    monkeypatch.setattr(ns.NovitaSandboxTool, "_selected_backend", staticmethod(
        lambda: ("upstash", cfg.upstash)))
    monkeypatch.setattr(ns._UPSTASH_STORE, "sandbox_id", lambda key: "box-live")
    removed: list[str] = []
    monkeypatch.setattr(ns._UPSTASH_STORE, "remove", lambda key: removed.append(key))

    backend = SimpleNamespace(persist_workspace=False)

    async def fake_reset(box_id):
        backend.deleted = box_id

    backend.reset = fake_reset
    monkeypatch.setattr(tool, "_upstash_backend", lambda config, key: backend)

    await tool.release_upstash_sandbox("webui:ephemeral")
    assert backend.deleted == "box-live"
    assert removed == ["webui:ephemeral"]


def test_persist_workspace_defaults_true():
    from nanobot.config.schema import Config

    assert Config().execution.upstash.persist_workspace is True
    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")
    assert backend.persist_workspace is True
    assert UpstashExecutionBackend(_config(persist_workspace=False), box_name="px-test-1").persist_workspace is False


@pytest.mark.asyncio
async def test_restore_workspace_skips_when_no_archive_box(monkeypatch):
    """Cold start with no snapshot must NOT create an archive box.

    The old path awaited archive ensure_box (create + wait_ready) inline inside
    ensure_box, adding minutes to the first command after box recreation —
    which users saw as "the AI hangs in the sandbox".
    """
    import asyncio as _asyncio

    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")
    assert backend.persist_workspace is True

    class _StubArchive:
        workspace = "/workspace"

        async def find_box(self, session):
            return None

        async def ensure_box(self, session):
            raise AssertionError("archive box must not be created when no snapshot exists")

    monkeypatch.setattr(backend, "_archive_backend", lambda: _StubArchive())
    assert await backend.restore_workspace() is False
    assert _asyncio.sleep  # no-op: keeps import used


@pytest.mark.asyncio
async def test_upstash_action_watchdog_bounds_hang(monkeypatch):
    """A wedged sandbox action must return a bounded error, never hang the turn."""
    import asyncio

    from nanobot.agent.tools import novita_sandbox as ns

    async def wedged_inner(self, action, kwargs, config, session_key):
        await asyncio.sleep(30)

    monkeypatch.setattr(ns.NovitaSandboxTool, "_execute_upstash_inner", wedged_inner)
    monkeypatch.setattr(
        ns.NovitaSandboxTool, "_upstash_action_budget", staticmethod(lambda action, kwargs: 1)
    )
    tool = object.__new__(ns.NovitaSandboxTool)
    result = await ns.NovitaSandboxTool._execute_upstash(tool, "list", {}, SimpleNamespace(), "sess")
    assert isinstance(result, str)
    assert "timed out" in str(result)
    assert "action=list" in str(result)


@pytest.mark.asyncio
async def test_release_snapshot_wedged_is_bounded(monkeypatch):
    """The task-end snapshot must never block the finished task's turn.

    Regression for the reported hang: release_upstash_sandbox is awaited INLINE
    at task end, and a wedged snapshot_workspace used to block the whole turn
    for minutes on a cold or stuck box.
    """
    import asyncio

    from nanobot.agent.tools import novita_sandbox as ns

    cfg = SimpleNamespace(
        backend="upstash",
        vps=SimpleNamespace(host=""),
        upstash=_config(),
        novita_template=None,
    )
    tool = ns.NovitaSandboxTool()
    monkeypatch.setattr(
        ns.NovitaSandboxTool, "_selected_backend", staticmethod(lambda: ("upstash", cfg.upstash))
    )
    monkeypatch.setattr(ns._UPSTASH_STORE, "sandbox_id", lambda key: "box-live")

    backend = SimpleNamespace(persist_workspace=True)

    async def wedged_snapshot():
        await asyncio.sleep(30)

    backend.snapshot_workspace = wedged_snapshot
    monkeypatch.setattr(tool, "_upstash_backend", lambda config, key: backend)
    monkeypatch.setattr(ns, "_UPSTASH_SNAPSHOT_BUDGET", 1)

    # A wedged snapshot is cancelled at the budget; release still completes.
    await asyncio.wait_for(tool.release_upstash_sandbox("webui:wedged"), timeout=10)


@pytest.mark.asyncio
async def test_snapshot_stage_budgets_are_bounded(monkeypatch):
    """Tar and staged-read budgets inside snapshot_workspace stay bounded."""
    backend = UpstashExecutionBackend(_config(), box_name="px-budget-1")
    exec_timeouts: list[int] = []
    read_timeouts: list[int] = []

    async def fake_ensure(session):
        return "box-self"

    async def fake_exec(session, box_id, cmd, timeout):
        exec_timeouts.append(timeout)
        return {"exit_code": 0, "output": "", "error": ""}

    async def fake_read_bytes(session, box_id, path, *, timeout=240):
        read_timeouts.append(timeout)
        return b"tarball"

    archive = SimpleNamespace(
        ensure_box=None,
        write_bytes=None,
        workspace="/workspace/home",
        box_name="px-archive-x",
    )

    async def fake_archive_ensure(session):
        return "box-archive"

    async def fake_archive_write(path, data):
        return None

    monkeypatch.setattr(backend, "ensure_box", fake_ensure)
    monkeypatch.setattr(backend, "_exec", fake_exec)
    monkeypatch.setattr(backend, "_read_box_bytes", fake_read_bytes)
    monkeypatch.setattr(backend, "_archive_backend", lambda: archive)
    monkeypatch.setattr(archive, "ensure_box", fake_archive_ensure)
    monkeypatch.setattr(archive, "write_bytes", fake_archive_write)

    assert await backend.snapshot_workspace() is True
    assert exec_timeouts and max(exec_timeouts) <= 150
    assert read_timeouts and max(read_timeouts) <= 120
