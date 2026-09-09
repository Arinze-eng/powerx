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


def test_execution_section_defers_load_until_admin_websocket_ready():
    """The execution section must not run adminRequest at parse time.

    The admin WebSocket client (window.nanobotAdminRequest) is assigned by the
    provider section script which appears later in the page.  If the execution
    section called its local helper immediately it would throw
    "adminRequest is not defined" before the socket is ready.  It must poll
    for window.nanobotAdminRequest instead.
    """
    import nanobot.admin_registry as admin_registry

    section = admin_registry._execution_admin_section()
    # No local helper / no synchronous load call.
    assert "const adminRequest=" not in section
    assert "void load().catch(" not in section
    # Uses the shared window client, deferred until it exists.
    assert "window.nanobotAdminRequest" in section
    assert "__execReady" in section
    assert "typeof window.nanobotAdminRequest==='function'" in section
    assert "saved=await window.nanobotAdminRequest('admin.execution.get')" in section
    assert "adminRequest('admin.execution.get')" not in section
    assert "adminRequest('admin.execution.save'" not in section
    assert "adminRequest('admin.execution.test'" not in section
