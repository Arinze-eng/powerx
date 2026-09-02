"""Supabase-backed per-user Alpaca API key store.

Stores encrypted Alpaca credentials in the `alpaca_credentials` table.
Uses the same AES-GCM encryption pattern as SupabaseAuth session tokens.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

import httpx
from loguru import logger

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover
    AESGCM = None


class AlpacaCredentialError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


class AlpacaCredentialStore:
    """Service-role access to per-user Alpaca credentials in Supabase."""

    def __init__(self) -> None:
        self.url = _env("SUPABASE_URL").rstrip("/")
        self.service_key = _env("SUPABASE_SERVICE_ROLE_KEY")
        self._crypto = self._build_crypto()

    def _build_crypto(self):
        if AESGCM is None:
            return None
        token_key = _env("SUPABASE_TOKEN_ENCRYPTION_KEY")
        if not token_key:
            return None
        raw = token_key
        try:
            raw_bytes = base64.b64decode(raw, validate=True)
        except Exception:
            raw_bytes = raw.encode()
        if len(raw_bytes) != 32:
            raw_bytes = hashlib.sha256(raw_bytes).digest()
        if len(raw_bytes) != 32:
            raw_bytes = hashlib.sha256(b"nanobot-alpaca-session-key").digest()
        return AESGCM(raw_bytes)

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.service_key)

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
        }

    async def _request(
        self, method: str, path: str, *, params: dict[str, str] | None = None, body: Any = None
    ) -> Any:
        if not self.enabled:
            raise AlpacaCredentialError("Supabase is not configured")
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.request(
                method,
                f"{self.url}{path}",
                headers=self._headers(),
                params=params,
                json=body,
            )
        if response.status_code >= 400:
            raise AlpacaCredentialError(
                f"Supabase {method} {path} failed ({response.status_code}): {response.text[:300]}"
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _encrypt(self, value: str) -> tuple[str, str]:
        if self._crypto is None:
            raise AlpacaCredentialError(
                "Encryption key not configured. Set SUPABASE_TOKEN_ENCRYPTION_KEY."
            )
        iv = os.urandom(12)
        ciphertext = self._crypto.encrypt(iv, value.encode(), None)
        return base64.b64encode(ciphertext).decode(), base64.b64encode(iv).decode()

    def _decrypt(self, ciphertext: str, iv_b64: str) -> str:
        if self._crypto is None:
            raise AlpacaCredentialError("Encryption key not configured.")
        plaintext = self._crypto.decrypt(
            base64.b64decode(iv_b64),
            base64.b64decode(ciphertext),
            None,
        )
        return plaintext.decode()

    async def store_credentials(
        self,
        *,
        telegram_user_id: int,
        api_key: str,
        secret_key: str,
        base_url: str = "https://paper-api.alpaca.markets",
    ) -> None:
        enc_api, iv_api = self._encrypt(api_key)
        enc_secret, iv_secret = self._encrypt(secret_key)
        await self._request(
            "POST",
            "/rest/v1/alpaca_credentials",
            body={
                "telegram_user_id": telegram_user_id,
                "api_key_ciphertext": enc_api,
                "api_key_iv": iv_api,
                "secret_key_ciphertext": enc_secret,
                "secret_key_iv": iv_secret,
                "base_url": base_url,
                "updated_at": _now(),
            },
            params={"select": "id"},
        )

    async def get_credentials(self, telegram_user_id: int) -> dict[str, str] | None:
        rows = await self._request(
            "GET",
            "/rest/v1/alpaca_credentials",
            params={
                "telegram_user_id": f"eq.{telegram_user_id}",
                "limit": "1",
                "select": "*",
            },
        )
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        return {
            "api_key": self._decrypt(row["api_key_ciphertext"], row["api_key_iv"]),
            "secret_key": self._decrypt(row["secret_key_ciphertext"], row["secret_key_iv"]),
            "base_url": row.get("base_url", "https://paper-api.alpaca.markets"),
        }

    async def delete_credentials(self, telegram_user_id: int) -> bool:
        result = await self._request(
            "DELETE",
            "/rest/v1/alpaca_credentials",
            params={"telegram_user_id": f"eq.{telegram_user_id}"},
        )
        return True


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
