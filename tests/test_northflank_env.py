"""Tests for the Northflank environment-variable client."""

from __future__ import annotations

import pytest

import nanobot.northflank_env as northflank_env


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = b"{}" if payload is not None else b""

    def json(self) -> dict:
        return self._payload or {}


class _FakeClient:
    calls: list[dict] = []
    responses: list[_FakeResponse] = []

    def __init__(self, **kwargs) -> None:
        pass

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def request(self, method, url, headers=None, json=None):
        _FakeClient.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        return _FakeClient.responses.pop(0)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.responses = []
    monkeypatch.delenv(northflank_env.TOKEN_VAR, raising=False)
    monkeypatch.delenv(northflank_env.PROJECT_VAR, raising=False)
    monkeypatch.delenv(northflank_env.SERVICE_VAR, raising=False)
    monkeypatch.setattr(northflank_env.httpx, "Client", _FakeClient)
    yield


def test_configured_reflects_token(monkeypatch) -> None:
    assert northflank_env.configured() is False
    monkeypatch.setenv(northflank_env.TOKEN_VAR, "nf-test-token")
    assert northflank_env.configured() is True
    cfg = northflank_env.config()
    assert cfg == {"token": "nf-test-token", "project": "minis", "service": "powerx"}


def test_defaults_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv(northflank_env.TOKEN_VAR, "nf-test-token")
    monkeypatch.setenv(northflank_env.PROJECT_VAR, "proj-x")
    monkeypatch.setenv(northflank_env.SERVICE_VAR, "svc-y")
    cfg = northflank_env.config()
    assert cfg == {"token": "nf-test-token", "project": "proj-x", "service": "svc-y"}


def test_get_runtime_environment(monkeypatch) -> None:
    monkeypatch.setenv(northflank_env.TOKEN_VAR, "nf-test-token")
    _FakeClient.responses = [_FakeResponse(200, {"data": {"runtimeEnvironment": {"A": "1"}}})]
    assert northflank_env.get_runtime_environment() == {"A": "1"}
    call = _FakeClient.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/projects/minis/services/powerx/runtime-environment")
    assert call["headers"]["Authorization"] == "Bearer nf-test-token"


def test_set_env_var_merges_and_patches(monkeypatch) -> None:
    monkeypatch.setenv(northflank_env.TOKEN_VAR, "nf-test-token")
    _FakeClient.responses = [
        _FakeResponse(200, {"data": {"runtimeEnvironment": {"EXISTING": "keep-me"}}}),
        _FakeResponse(200, {}),
    ]
    merged = northflank_env.set_env_var("PROVIDER-POOL-JSON", "{\"entries\":[]}")
    assert merged == {"EXISTING": "keep-me", "PROVIDER-POOL-JSON": "{\"entries\":[]}"}
    patch = _FakeClient.calls[1]
    assert patch["method"] == "PATCH"
    assert patch["url"].endswith("/projects/minis/services/combined/powerx")
    assert patch["json"]["runtimeEnvironment"]["EXISTING"] == "keep-me"
    assert patch["json"]["runtimeEnvironment"]["PROVIDER-POOL-JSON"] == "{\"entries\":[]}"


def test_restart_service(monkeypatch) -> None:
    monkeypatch.setenv(northflank_env.TOKEN_VAR, "nf-test-token")
    _FakeClient.responses = [_FakeResponse(200, {})]
    northflank_env.restart_service()
    call = _FakeClient.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/projects/minis/services/powerx/restart")


def test_http_error_raises(monkeypatch) -> None:
    monkeypatch.setenv(northflank_env.TOKEN_VAR, "nf-test-token")
    _FakeClient.responses = [_FakeResponse(403, {"error": "nope"})]
    with pytest.raises(northflank_env.NorthflankError):
        northflank_env.get_runtime_environment()


def test_unconfigured_raises() -> None:
    with pytest.raises(northflank_env.NorthflankError):
        northflank_env.set_env_var("PROVIDER-POOL-JSON", "{}")
