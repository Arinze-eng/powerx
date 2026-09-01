"""Tests for the Chat Mini App: initData auth, token minting, and routing."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote_plus

import pytest

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover - aiohttp is a hard dependency here
    HAS_AIOHTTP = False

from nanobot.api.server import create_app
from nanobot.api.telegram_auth import (
    MiniAppTokenStore,
    validate_init_data,
)

pytest_plugins = ("pytest_asyncio",)

API_KEY = "secret"
BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


def _build_init_data(bot_token: str, user_id: int = 42) -> str:
    """Construct a cryptographically valid Telegram Web App initData string."""
    user = {"id": user_id, "first_name": "Test", "username": "tester"}
    user_json = json.dumps(user, separators=(",", ":"))
    auth_date = "1700000000"
    query_id = "abc"
    pairs = {"auth_date": auth_date, "query_id": query_id, "user": user_json}
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return "&".join(
        [
            f"auth_date={auth_date}",
            f"query_id={query_id}",
            f"user={quote_plus(user_json)}",
            f"hash={computed_hash}",
        ]
    )


# ---------------------------------------------------------------------------
# Unit tests: initData validation
# ---------------------------------------------------------------------------


def test_validate_init_data_accepts_valid_signature() -> None:
    init_data = _build_init_data(BOT_TOKEN)
    fields = validate_init_data(init_data, BOT_TOKEN)
    assert fields is not None
    assert json.loads(fields["user"])["id"] == 42


def test_validate_init_data_rejects_tampered_hash() -> None:
    init_data = _build_init_data(BOT_TOKEN)
    tampered = init_data.replace("hash=", "hash=" + "0" * 8)
    assert validate_init_data(tampered, BOT_TOKEN) is None


def test_validate_init_data_rejects_wrong_token() -> None:
    init_data = _build_init_data(BOT_TOKEN)
    assert validate_init_data(init_data, "999:different") is None


def test_validate_init_data_handles_empty_inputs() -> None:
    assert validate_init_data("", BOT_TOKEN) is None
    assert validate_init_data("auth_date=1&user=x", "") is None


# ---------------------------------------------------------------------------
# Unit tests: token store
# ---------------------------------------------------------------------------


def test_token_store_mint_and_verify() -> None:
    store = MiniAppTokenStore()
    token = store.mint("42")
    assert store.verify(token) == "42"


def test_token_store_rejects_unknown_token() -> None:
    store = MiniAppTokenStore()
    assert store.verify("does-not-exist") is None


def test_token_store_expiry() -> None:
    store = MiniAppTokenStore(ttl_seconds=60)
    token = store.mint("42")
    # Force expiry by rewinding the stored deadline.
    key, _exp = store._tokens[token]
    store._tokens[token] = (key, 0.0)
    assert store.verify(token) is None


def test_token_store_revoke() -> None:
    store = MiniAppTokenStore()
    token = store.mint("42")
    store.revoke(token)
    assert store.verify(token) is None


# ---------------------------------------------------------------------------
# Integration tests with aiohttp TestClient
# ---------------------------------------------------------------------------


def _make_agent() -> MagicMock:
    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value="ok")
    agent.aclose = AsyncMock()
    agent._last_usage = {}
    return agent


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_chat_page_served_without_auth() -> None:
    agent = _make_agent()
    app = create_app(agent, model_name="m", api_key=API_KEY)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/app")
        assert resp.status == 200
        text = await resp.text()
        assert "AI Assistant" in text
        assert "/app/chat" in text
    finally:
        await client.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_mint_token_returns_bearer_for_valid_initdata(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    agent = _make_agent()
    app = create_app(agent, model_name="m", api_key=API_KEY)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/app/token", json={"initData": _build_init_data(BOT_TOKEN)})
        assert resp.status == 200
        body = await resp.json()
        assert body["session_key"] == "42"
        assert len(body["token"]) >= 16
    finally:
        await client.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_mint_token_rejects_invalid_initdata(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    agent = _make_agent()
    app = create_app(agent, model_name="m", api_key=API_KEY)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/app/token", json={"initData": "auth_date=1&user=%7B%7D&hash=bad"})
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_mint_token_503_when_bot_token_unset(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    agent = _make_agent()
    app = create_app(agent, model_name="m", api_key=API_KEY)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/app/token", json={"initData": _build_init_data(BOT_TOKEN)})
        assert resp.status == 503
    finally:
        await client.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.skipif(
    not hasattr(asyncio, "timeout"),
    reason="agent handler needs Python 3.11 asyncio.timeout",
)
@pytest.mark.asyncio
async def test_chat_completions_accepts_miniapp_token(monkeypatch) -> None:
    """A minted short-lived token authenticates /v1/chat/completions like api_key."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    agent = _make_agent()
    app = create_app(agent, model_name="m", api_key=API_KEY)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        mint = await client.post("/app/token", json={"initData": _build_init_data(BOT_TOKEN)})
        token = (await mint.json())["token"]
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"messages": [{"role": "user", "content": "hi"}], "session_id": "42"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["choices"][0]["message"]["content"] == "ok"
    finally:
        await client.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_chat_completions_rejects_bad_token() -> None:
    agent = _make_agent()
    app = create_app(agent, model_name="m", api_key=API_KEY)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer not-a-real-token"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status == 401
    finally:
        await client.close()
