from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from nanobot.supabase_auth import SupabaseAuth, SupabaseAuthError


class FakeSupabase(SupabaseAuth):
    def __init__(self) -> None:
        self.url = "https://example.supabase.co"
        self.anon_key = "anon"
        self.service_key = "service"
        self._crypto = self._build_crypto("test-key")
        self.calls: list[tuple[str, str, dict[str, str] | None, object]] = []
        self.rows: dict[str, object] = {}

    @property
    def enabled(self) -> bool:
        return True

    async def _request(self, method: str, path: str, *, service: bool = False, access_token: str = "", params: dict[str, str] | None = None, body: object = None) -> object:
        self.calls.append((method, path, params, body))
        if path == "/rest/v1/profiles" and method == "GET":
            return [{"daily_credits": 900, "purchased_credits": 0, "granted_credits": 0, "drain_rate": 5}]
        if path == "/rest/v1/rpc/consume_cloud_task_step_credits":
            return {"success": True, "balance": 897}
        if path == "/functions/v1/pay-verify":
            return {"ok": True, "credits": 1000, "pkg": "starter", "tx_ref": "tx-1"}
        if path == "/auth/v1/token":
            return {"user": {"id": "11111111-1111-1111-1111-111111111111"}, "access_token": "access", "refresh_token": "refresh"}
        if path == "/rest/v1/telegram_accounts" and method == "GET":
            if params and "agentx_user_id" in params:
                return []
            return [{"telegram_user_id": 42, "agentx_user_id": None, "auth_state": {}}]
        if path == "/rest/v1/telegram_accounts" and method == "PATCH":
            return [{"telegram_user_id": 42}]
        return []


@pytest.mark.asyncio
async def test_signup_state_requires_private_flow_and_auth_continuation() -> None:
    client = FakeSupabase()
    account = {"telegram_user_id": 42, "agentx_user_id": None, "auth_state": {}}
    prompt = await client.start_auth(account, "signup")
    assert "Sign-up step 1" in prompt
    account["auth_state"] = {"flow": "signup", "step": "name", "started_at": client._now()}
    assert "step 2" in await client.handle_auth_message(account, "Ada Lovelace")
    account["auth_state"] = {"flow": "signup", "step": "email", "name": "Ada", "started_at": client._now()}
    assert "final step" in await client.handle_auth_message(account, "ada@example.com")


@pytest.mark.asyncio
async def test_signin_links_existing_account_and_signout_clears_tokens() -> None:
    client = FakeSupabase()
    account = {"telegram_user_id": 42, "agentx_user_id": None, "auth_state": {}}
    await client.start_auth(account, "signin")
    account["auth_state"] = {"flow": "signin", "step": "password", "email": "ada@example.com", "started_at": client._now()}
    response = await client.handle_auth_message(account, "strong-password")
    assert "signed in successfully" in response
    patch_bodies = [call[3] for call in client.calls if call[0] == "PATCH"]
    assert any(isinstance(body, dict) and body.get("agentx_user_id") for body in patch_bodies)


@pytest.mark.asyncio
async def test_charge_step_uses_existing_rpc_and_drain_rate() -> None:
    client = FakeSupabase()
    result = await client.charge_step({"agentx_user_id": "11111111-1111-1111-1111-111111111111"}, "nanobot:turn-1", 2)
    assert result["success"] is True
    call = next(call for call in client.calls if call[1] == "/rest/v1/rpc/consume_cloud_task_step_credits")
    assert call[3] == {
        "p_user": "11111111-1111-1111-1111-111111111111",
        "p_amount": 15,
        "p_task_ref": "nanobot:turn-1",
        "p_step_no": 2,
    }


@pytest.mark.asyncio
async def test_question_history_is_bounded_and_redacts_credentials() -> None:
    client = FakeSupabase()
    await client.record_telegram_question(
        {"telegram_user_id": 42},
        chat_id=-100123,
        message_id=9,
        question="deploy with api_key=super-secret and bearer: sk-test-abcdefghijklmnop",
        has_attachment=True,
    )
    call = next(call for call in client.calls if call[1] == "/rest/v1/telegram_question_history")
    body = call[3]
    assert body["has_attachment"] is True
    assert "super-secret" not in body["question"]
    assert "sk-test-abcdefghijklmnop" not in body["question"]
    assert len(body["question"]) <= 4000


@pytest.mark.asyncio
async def test_payment_packages_and_authenticated_verification_use_existing_edge_function() -> None:
    client = FakeSupabase()
    access_ciphertext, access_iv = client._encrypt("user-jwt")
    account = {
        "telegram_user_id": 42,
        "agentx_user_id": "11111111-1111-1111-1111-111111111111",
        "session_token_ciphertext": access_ciphertext,
        "session_token_iv": access_iv,
        "refresh_token_ciphertext": access_ciphertext,
        "refresh_token_iv": access_iv,
    }

    packages = client.payment_packages()
    assert [(row["credits"], row["amount_usd"]) for row in packages] == [
        (1000, 1.50), (2000, 3.00), (3500, 5.00), (7500, 10.00)
    ]
    assert "flutterwave.com/pay/yvbdgyf6awyf" in client.payment_packages_text(
        "https://flutterwave.com/pay/yvbdgyf6awyf"
    )

    result = await client.verify_payment(account, "tx-1", "12345")
    assert result["ok"] is True
    call = next(call for call in client.calls if call[1] == "/functions/v1/pay-verify")
    assert call[3] == {"tx_ref": "tx-1", "transaction_id": "12345"}


@pytest.mark.asyncio
async def test_puter_generation_requires_gateway_and_forwards_selected_action(monkeypatch) -> None:
    client = FakeSupabase()
    access_ciphertext, access_iv = client._encrypt("user-jwt")
    account = {
        "agentx_user_id": "11111111-1111-1111-1111-111111111111",
        "session_token_ciphertext": access_ciphertext,
        "session_token_iv": access_iv,
        "refresh_token_ciphertext": access_ciphertext,
        "refresh_token_iv": access_iv,
    }
    monkeypatch.setenv("SUPABASE_GATEWAY_KEY", "gateway-test")
    client._puter_request = AsyncMock(
        return_value={"ok": True, "mime": "image/png", "data_uri": "data:image/png;base64,AA=="}
    )

    result = await client.puter_generate(account, "generate_image", "a blue robot")

    assert result["ok"] is True
    client._puter_request.assert_awaited_once()
    request_body = client._puter_request.await_args.args[1]
    assert request_body == {"action": "generate_image", "prompt": "a blue robot"}


@pytest.mark.asyncio
async def test_puter_generation_reports_edge_function_failure(monkeypatch) -> None:
    client = FakeSupabase()
    monkeypatch.setenv("SUPABASE_GATEWAY_KEY", "gateway-test")
    client._puter_request = AsyncMock(
        side_effect=Exception("Puter is disabled by the administrator")
    )

    with pytest.raises(Exception, match="Puter is disabled"):
        await client.puter_generate({"agentx_user_id": "user"}, "generate_image", "x")


def test_decrypt_supports_optional_previous_encryption_key(monkeypatch) -> None:
    legacy = SupabaseAuth()
    legacy._crypto = legacy._build_crypto("legacy-key")
    ciphertext, iv = legacy._encrypt("legacy-access-token")

    monkeypatch.setenv("SUPABASE_TOKEN_ENCRYPTION_KEY", "current-key")
    monkeypatch.setenv("SUPABASE_TOKEN_ENCRYPTION_KEY_PREVIOUS", "legacy-key")
    rotated = SupabaseAuth()

    assert rotated._decrypt(ciphertext, iv) == "legacy-access-token"


def test_decrypt_failure_has_signin_recovery_message() -> None:
    client = FakeSupabase()
    with pytest.raises(SupabaseAuthError, match="signin"):
        client._decrypt("not-valid-ciphertext", "not-valid-iv")


@pytest.mark.asyncio
async def test_session_is_usable_refreshes_expired_encrypted_access_token() -> None:
    client = FakeSupabase()
    expired_payload = base64.urlsafe_b64encode(
        json.dumps({"exp": 0}).encode()
    ).decode().rstrip("=")
    expired_access = f"header.{expired_payload}.signature"
    access_ciphertext, access_iv = client._encrypt(expired_access)
    refresh_ciphertext, refresh_iv = client._encrypt("refresh-token")
    account = {
        "telegram_user_id": 42,
        "agentx_user_id": "11111111-1111-1111-1111-111111111111",
        "session_token_ciphertext": access_ciphertext,
        "session_token_iv": access_iv,
        "refresh_token_ciphertext": refresh_ciphertext,
        "refresh_token_iv": refresh_iv,
    }

    assert await client.session_is_usable(account) is True
    assert client._decrypt(
        account["session_token_ciphertext"], account["session_token_iv"]
    ) == "access"
    refresh_call = next(call for call in client.calls if call[1] == "/auth/v1/token")
    assert refresh_call[2] == {"grant_type": "refresh_token"}
    assert refresh_call[3] == {"refresh_token": "refresh-token"}


@pytest.mark.asyncio
async def test_puter_invalid_session_refreshes_and_retries_once(monkeypatch) -> None:
    client = FakeSupabase()
    access_ciphertext, access_iv = client._encrypt("user-jwt")
    refresh_ciphertext, refresh_iv = client._encrypt("refresh-token")
    account = {
        "telegram_user_id": 42,
        "agentx_user_id": "11111111-1111-1111-1111-111111111111",
        "session_token_ciphertext": access_ciphertext,
        "session_token_iv": access_iv,
        "refresh_token_ciphertext": refresh_ciphertext,
        "refresh_token_iv": refresh_iv,
    }
    monkeypatch.setenv("SUPABASE_GATEWAY_KEY", "gateway-test")
    client._puter_http_request = AsyncMock(
        side_effect=[
            httpx.Response(401, json={"error": "invalid user session"}),
            httpx.Response(200, json={"ok": True, "data_uri": "data:image/png;base64,AA=="}),
        ]
    )

    result = await client._puter_request(account, {"action": "edit_image"})

    assert result["ok"] is True
    assert client._puter_http_request.await_count == 2
    first_body = client._puter_http_request.await_args_list[0].args[1]
    second_body = client._puter_http_request.await_args_list[1].args[1]
    assert first_body["user_jwt"] == "user-jwt"
    assert second_body["user_jwt"] == "access"


@pytest.mark.asyncio
async def test_session_is_usable_revokes_only_undecryptable_session() -> None:
    client = FakeSupabase()
    account = {
        "telegram_user_id": 42,
        "agentx_user_id": "user",
        "session_token_ciphertext": "bad",
        "session_token_iv": "bad",
        "refresh_token_ciphertext": "bad",
        "refresh_token_iv": "bad",
    }

    assert await client.session_is_usable(account) is False
    patch_bodies = [call[3] for call in client.calls if call[0] == "PATCH"]
    assert any(
        isinstance(body, dict)
        and body.get("session_token_ciphertext") is None
        and body.get("refresh_token_ciphertext") is None
        for body in patch_bodies
    )


def test_auth_state_rejects_expired_or_malformed_state() -> None:
    client = FakeSupabase()
    assert client.auth_state({"auth_state": {"flow": "signup", "step": "name"}}) is None
    assert client.auth_state({"auth_state": {"flow": "other", "step": "name", "started_at": client._now()}}) is None


def test_authentication_requires_complete_encrypted_session() -> None:
    assert not SupabaseAuth.is_authenticated({"agentx_user_id": "user"})
    assert SupabaseAuth.is_authenticated({
        "agentx_user_id": "user",
        "session_token_ciphertext": "access",
        "session_token_iv": "iv",
        "refresh_token_ciphertext": "refresh",
        "refresh_token_iv": "iv",
    })


@pytest.mark.asyncio
async def test_puter_image_edit_forwards_bounded_input_images(monkeypatch) -> None:
    client = FakeSupabase()
    monkeypatch.setenv("SUPABASE_GATEWAY_KEY", "gateway-test")
    client._puter_request = AsyncMock(
        return_value={"ok": True, "mime": "image/png", "data_uri": "data:image/png;base64,AA=="}
    )

    result = await client.puter_edit_image(
        {"agentx_user_id": "user"},
        "remove the background",
        ["data:image/png;base64,AA==", "data:image/jpeg;base64,/9j/", "data:image/webp;base64,UklG", "data:image/png;base64,ignored"],
    )

    assert result["ok"] is True
    request_body = client._puter_request.await_args.args[1]
    assert request_body["action"] == "edit_image"
    assert request_body["prompt"] == "remove the background"
    assert len(request_body["input_images"]) == 3


@pytest.mark.asyncio
async def test_puter_image_edit_rejects_non_image_data_uri(monkeypatch) -> None:
    client = FakeSupabase()
    monkeypatch.setenv("SUPABASE_GATEWAY_KEY", "gateway-test")

    with pytest.raises(SupabaseAuthError, match="unsupported format"):
        await client.puter_edit_image({"agentx_user_id": "user"}, "edit", ["data:text/plain;base64,QQ=="])
