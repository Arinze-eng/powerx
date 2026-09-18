"""Tests for the admin provider-pool routes."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

import nanobot.admin_registry as admin_registry
import nanobot.northflank_env as northflank_env
import nanobot.provider_pool as provider_pool


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv(provider_pool.POOL_ENV_VAR, raising=False)
    monkeypatch.delenv(northflank_env.TOKEN_VAR, raising=False)


def _request(path: str, *, payload: dict | None = None, password: str = "nethunter") -> SimpleNamespace:
    encoded = base64.b64encode(f"admin:{password}".encode()).decode()
    request = SimpleNamespace(path=path, headers={"Authorization": f"Basic {encoded}"})
    if payload is not None:
        request._nanobot_webui_mutation_payload = payload
    return request


def _json(response) -> dict:
    return json.loads(bytes(response.body).decode())


def test_pool_routes_require_auth(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    for path in (
        "/api/admin/provider-pool",
        "/api/admin/provider-pool/add",
        "/api/admin/provider-pool/delete",
        "/api/admin/provider-pool/update",
        "/api/admin/provider-pool/test",
        "/api/admin/provider-pool/models",
    ):
        response = admin_registry.admin_route(SimpleNamespace(path=path, headers={}), path)
        assert response is not None
        assert response.status_code == 401


def test_dashboard_renders_pool_section(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    response = admin_registry.admin_route(_request("/admin"), "/admin")
    assert response is not None
    body = bytes(response.body).decode()
    assert "Provider pool" in body
    assert "poolRows" in body


def test_pool_add_list_update_delete_flow(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setenv("PROVIDER_POOL_PATH", str(tmp_path / "pool.json"))

    added = admin_registry.admin_route(
        _request(
            "/api/admin/provider-pool/add",
            payload={
                "baseUrl": "https://api.example.com/v1",
                "apiKey": "sk-abcdefghijklmnop",
                "model": "gpt-4o-mini",
                "label": "kyma-1",
            },
        ),
        "/api/admin/provider-pool/add",
    )
    assert added is not None
    assert added.status_code == 200
    body = _json(added)
    assert body["count"] == 1
    assert body["max"] == 40
    assert body["entries"][0]["apiKeyMasked"] == "sk-a...mnop"
    assert "sk-abcdefghijklmnop" not in bytes(added.body).decode()
    entry_id = body["entries"][0]["id"]

    listed = admin_registry.admin_route(_request("/api/admin/provider-pool"), "/api/admin/provider-pool")
    assert listed is not None
    assert _json(listed)["count"] == 1

    toggled = admin_registry.admin_route(
        _request("/api/admin/provider-pool/update", payload={"id": entry_id, "enabled": False}),
        "/api/admin/provider-pool/update",
    )
    assert _json(toggled)["entries"][0]["enabled"] is False
    assert provider_pool.enabled_entries() == []

    deleted = admin_registry.admin_route(
        _request("/api/admin/provider-pool/delete", payload={"id": entry_id}),
        "/api/admin/provider-pool/delete",
    )
    assert _json(deleted)["count"] == 0


def test_pool_add_rejects_invalid_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setenv("PROVIDER_POOL_PATH", str(tmp_path / "pool.json"))
    response = admin_registry.admin_route(
        _request("/api/admin/provider-pool/add", payload={"baseUrl": "not-a-url", "apiKey": "k" * 12, "model": "m"}),
        "/api/admin/provider-pool/add",
    )
    assert response is not None
    assert response.status_code == 400


def test_pool_test_route_builds_request(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setenv("PROVIDER_POOL_PATH", str(tmp_path / "pool.json"))
    entry = provider_pool.add_entry(
        {"baseUrl": "https://api.example.com/v1", "apiKey": "sk-abcdefghijklmnop", "model": "gpt-4o-mini"}
    )

    captured: dict = {}

    class _Response:
        status_code = 200

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "OK"}}]}

    class _Client:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            return _Response()

    monkeypatch.setattr(admin_registry.httpx, "Client", _Client)

    response = admin_registry.admin_route(
        _request("/api/admin/provider-pool/test", payload={"id": entry["id"]}),
        "/api/admin/provider-pool/test",
    )
    result = _json(response)["results"][0]
    assert result["ok"] is True
    assert result["response"] == "OK"
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-abcdefghijklmnop"


def test_pool_save_syncs_to_northflank(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setenv("PROVIDER_POOL_PATH", str(tmp_path / "pool.json"))
    monkeypatch.setenv(northflank_env.TOKEN_VAR, "nf-test-token")

    synced: dict = {}

    def _fake_set(name, value):
        synced["name"] = name
        synced["value"] = value
        return {name: value}

    monkeypatch.setattr(northflank_env, "set_env_var", _fake_set)

    response = admin_registry.admin_route(
        _request(
            "/api/admin/provider-pool/add",
            payload={
                "baseUrl": "https://api.example.com/v1",
                "apiKey": "sk-abcdefghijklmnop",
                "model": "gpt-4o-mini",
            },
        ),
        "/api/admin/provider-pool/add",
    )
    body = _json(response)
    assert body["northflank"] == {"configured": True, "synced": True}
    assert synced["name"] == provider_pool.POOL_ENV_VAR
    assert "sk-abcdefghijklmnop" in synced["value"]


def test_pool_save_reports_when_northflank_unconfigured(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setenv("PROVIDER_POOL_PATH", str(tmp_path / "pool.json"))
    response = admin_registry.admin_route(
        _request(
            "/api/admin/provider-pool/add",
            payload={
                "baseUrl": "https://api.example.com/v1",
                "apiKey": "sk-abcdefghijklmnop",
                "model": "gpt-4o-mini",
            },
        ),
        "/api/admin/provider-pool/add",
    )
    assert _json(response)["northflank"] == {"configured": False, "synced": False}


def test_pool_models_route_lists_lane_models(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    captured: dict = {}

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": [{"id": "gemini-3.1-flash-lite"}, {"id": "gpt-4o-mini"}, {"nope": 1}]}

    class _Client:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return _Response()

    monkeypatch.setattr(admin_registry.httpx, "Client", _Client)

    response = admin_registry.admin_route(
        _request(
            "/api/admin/provider-pool/models",
            payload={"baseUrl": "https://api.example.com/v1", "apiKey": "sk-abcdefghijklmnop"},
        ),
        "/api/admin/provider-pool/models",
    )
    body = _json(response)
    assert body["models"] == ["gemini-3.1-flash-lite", "gpt-4o-mini"]
    assert body["count"] == 2
    assert captured["url"] == "https://api.example.com/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-abcdefghijklmnop"


def test_pool_models_route_rejects_missing_base_url(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    response = admin_registry.admin_route(
        _request("/api/admin/provider-pool/models", payload={}),
        "/api/admin/provider-pool/models",
    )
    assert response is not None
    assert response.status_code == 400


def test_pool_models_route_can_use_a_saved_entry(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setenv("PROVIDER_POOL_PATH", str(tmp_path / "pool.json"))
    entry = provider_pool.add_entry(
        {"baseUrl": "https://api.example.com/v1", "apiKey": "sk-abcdefghijklmnop", "model": "gpt-4o-mini"}
    )
    captured: dict = {}

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": [{"id": "gpt-4o-mini"}]}

    class _Client:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return _Response()

    monkeypatch.setattr(admin_registry.httpx, "Client", _Client)

    response = admin_registry.admin_route(
        _request("/api/admin/provider-pool/models", payload={"id": entry["id"]}),
        "/api/admin/provider-pool/models",
    )
    assert _json(response)["models"] == ["gpt-4o-mini"]
    assert captured["headers"]["Authorization"] == "Bearer sk-abcdefghijklmnop"


def test_pool_test_all_reports_every_enabled_lane(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setenv("PROVIDER_POOL_PATH", str(tmp_path / "pool.json"))
    provider_pool.add_entry({"baseUrl": "https://a.example.com/v1", "apiKey": "sk-aaaaaaaaaaaa", "model": "m1"})
    provider_pool.add_entry({"baseUrl": "https://b.example.com/v1", "apiKey": "sk-bbbbbbbbbbbb", "model": "m2"})

    class _Response:
        status_code = 200

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "OK"}}]}

    class _Client:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def post(self, url, headers=None, json=None):
            return _Response()

    monkeypatch.setattr(admin_registry.httpx, "Client", _Client)

    response = admin_registry.admin_route(
        _request("/api/admin/provider-pool/test", payload={"all": True}),
        "/api/admin/provider-pool/test",
    )
    results = _json(response)["results"]
    assert len(results) == 2
    assert all(result["ok"] is True for result in results)


def test_pool_test_surfaces_a_lane_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setenv("PROVIDER_POOL_PATH", str(tmp_path / "pool.json"))
    entry = provider_pool.add_entry(
        {"baseUrl": "https://api.example.com/v1", "apiKey": "sk-abcdefghijklmnop", "model": "gpt-4o-mini"}
    )

    class _Response:
        status_code = 429

        def json(self) -> dict:
            return {}

    class _Client:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def post(self, url, headers=None, json=None):
            return _Response()

    monkeypatch.setattr(admin_registry.httpx, "Client", _Client)

    response = admin_registry.admin_route(
        _request("/api/admin/provider-pool/test", payload={"id": entry["id"]}),
        "/api/admin/provider-pool/test",
    )
    result = _json(response)["results"][0]
    assert result["ok"] is False
    assert result["status"] == 429
    assert result["error"] == "HTTP 429"
