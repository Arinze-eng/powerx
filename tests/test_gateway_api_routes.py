"""Tests for the gateway /v1/* OpenAI-compatible bridge routes."""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from nanobot.api import api_bridge as ab_mod
from nanobot.api import gateway_routes as gr


class _FakeHeaders:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = {k.lower(): v for k, v in mapping.items()}

    def get(self, key: str, default: str = "") -> str:
        return self._m.get(key.lower(), default)


class _FakeRequest:
    def __init__(self, path: str, headers: dict[str, str] | None = None) -> None:
        self.path = path
        self.headers = _FakeHeaders(headers or {})


def _payload(messages=None, **extra):
    body = {"model": "powerx-agent", "messages": messages if messages is not None else [
        {"role": "user", "content": "hi"}]}
    body.update(extra)
    return "?payload=" + quote(json.dumps(body))


@pytest.fixture()
def wired_bridge(monkeypatch):
    """Pretend the websocket channel is attached and turns succeed."""
    calls: list[dict] = []

    async def fake_run_turn(*, user_id, content, media_paths=None, api_key_id=None,
                            supabase_user_id=None, model="", stream=False):
        calls.append({"user_id": user_id, "content": content, "api_key_id": api_key_id,
                      "supabase_user_id": supabase_user_id, "stream": stream})
        return f"echo:{content}", {"usage": {}}

    monkeypatch.setattr(ab_mod.api_bridge, "_agent", object())  # ready=True
    monkeypatch.setattr(ab_mod.api_bridge, "run_turn", fake_run_turn)
    return calls


@pytest.mark.asyncio
async def test_models_and_docs_public(monkeypatch):
    monkeypatch.setattr(
        "nanobot.channels.telegram.api_platform.resolve_base_url", lambda: "https://x.onrender.com"
    )
    resp = await gr.dispatch_v1_routes(_FakeRequest("/v1/models"), "/v1/models")
    assert resp.status_code == 200
    data = json.loads(bytes(resp.body))
    assert data["data"][0]["id"]

    resp = await gr.dispatch_v1_routes(_FakeRequest("/v1/api-docs"), "/v1/api-docs")
    assert resp.status_code == 200
    assert b"/v1/chat/completions" in bytes(resp.body)


@pytest.mark.asyncio
async def test_missing_auth_rejected(wired_bridge, monkeypatch):
    class _Store:
        enabled = True

        async def find_active_by_hash(self, digest):
            return None

    monkeypatch.setattr("nanobot.api.api_keys.ApiKeyStore", lambda: _Store())
    resp = await gr.dispatch_v1_routes(
        _FakeRequest("/v1/chat/completions" + _payload()), "/v1/chat/completions"
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unknown_route_404():
    resp = await gr.dispatch_v1_routes(_FakeRequest("/v1/nope"), "/v1/nope")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bad_json_payload(wired_bridge):
    resp = await gr.dispatch_v1_routes(
        _FakeRequest("/v1/chat/completions?payload=not-json"), "/v1/chat/completions"
    )
    assert resp.status_code == 400




@pytest.mark.asyncio
async def test_valid_key_runs_turn(wired_bridge, monkeypatch):
    class _Store:
        enabled = True

        async def find_active_by_hash(self, digest):
            return {"id": 7, "agentx_user_id": "user-123"}

    monkeypatch.setattr("nanobot.api.api_keys.ApiKeyStore", lambda: _Store())
    req = _FakeRequest(
        "/v1/chat/completions" + _payload([{"role": "user", "content": "ping"}]),
        {"authorization": "Bearer px_" + "a" * 40},
    )
    resp = await gr.dispatch_v1_routes(req, "/v1/chat/completions")
    assert resp.status_code == 200
    body = json.loads(bytes(resp.body))
    assert body["choices"][0]["message"]["content"] == "echo:ping"
    assert body["object"] == "chat.completion"
    assert len(wired_bridge) == 1
    call = wired_bridge[0]
    assert call["user_id"] == "user-123"
    assert call["content"] == "ping"
    assert call["api_key_id"] == 7
    assert call["supabase_user_id"] == "user-123"


@pytest.mark.asyncio
async def test_stream_flag_returns_sse(wired_bridge, monkeypatch):
    class _Store:
        enabled = True

        async def find_active_by_hash(self, digest):
            return {"id": 1, "agentx_user_id": "u"}

    monkeypatch.setattr("nanobot.api.api_keys.ApiKeyStore", lambda: _Store())
    req = _FakeRequest(
        "/v1/chat/completions" + _payload(stream=True),
        {"authorization": "Bearer px_" + "b" * 40},
    )
    resp = await gr.dispatch_v1_routes(req, "/v1/chat/completions")
    assert resp.status_code == 200
    text = bytes(resp.body).decode()
    assert text.startswith("data: ")
    assert text.rstrip().endswith("[DONE]")


@pytest.mark.asyncio
async def test_credit_error_maps_402(wired_bridge, monkeypatch):
    async def failing_run_turn(*, user_id, content, media_paths=None, api_key_id=None, supabase_user_id=None, model="", stream=False):
        return "", {"error": "Insufficient credits"}

    class _Store:
        enabled = True

        async def find_active_by_hash(self, digest):
            return {"id": 2, "agentx_user_id": "u"}

    monkeypatch.setattr("nanobot.api.api_keys.ApiKeyStore", lambda: _Store())
    monkeypatch.setattr(ab_mod.api_bridge, "run_turn", failing_run_turn)
    req = _FakeRequest(
        "/v1/chat/completions" + _payload(),
        {"authorization": "Bearer px_" + "c" * 40},
    )
    resp = await gr.dispatch_v1_routes(req, "/v1/chat/completions")
    assert resp.status_code == 402


@pytest.mark.asyncio
async def test_invalid_key_401(wired_bridge, monkeypatch):
    class _Store:
        enabled = True

        async def find_active_by_hash(self, digest):
            return None

    monkeypatch.setattr("nanobot.api.api_keys.ApiKeyStore", lambda: _Store())
    req = _FakeRequest(
        "/v1/chat/completions" + _payload(),
        {"authorization": "Bearer px_" + "d" * 40},
    )
    resp = await gr.dispatch_v1_routes(req, "/v1/chat/completions")
    assert resp.status_code == 401
