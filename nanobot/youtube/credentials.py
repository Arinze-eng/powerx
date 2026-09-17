"""Supabase-backed per-user YouTube (Google OAuth) credential store.

Stores encrypted Google access/refresh tokens in the ``youtube_credentials``
table. Uses the same AES-GCM encryption pattern as the Alpaca credential store
and SupabaseAuth session tokens.
"""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime, timezone
from typing import Any

import httpx

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover
    AESGCM = None


class YouTubeCredentialError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class YouTubeCredentialStore:
    """Service-role access to per-user YouTube tokens in Supabase."""

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
            raw_bytes = hashlib.sha256(b"nanobot-youtube-session-key").digest()
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
            raise YouTubeCredentialError("Supabase is not configured")
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.request(
                method,
                f"{self.url}{path}",
                headers=self._headers(),
                params=params,
                json=body,
            )
        if response.status_code >= 400:
            raise YouTubeCredentialError(
                f"Supabase {method} {path} failed ({response.status_code}): {response.text[:300]}"
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _encrypt(self, value: str) -> tuple[str, str]:
        if self._crypto is None:
            raise YouTubeCredentialError(
                "Encryption key not configured. Set SUPABASE_TOKEN_ENCRYPTION_KEY."
            )
        iv = os.urandom(12)
        ciphertext = self._crypto.encrypt(iv, value.encode(), None)
        return base64.b64encode(ciphertext).decode(), base64.b64encode(iv).decode()

    def _decrypt(self, ciphertext: str, iv_b64: str) -> str:
        if self._crypto is None:
            raise YouTubeCredentialError("Encryption key not configured.")
        plaintext = self._crypto.decrypt(
            base64.b64decode(iv_b64),
            base64.b64decode(ciphertext),
            None,
        )
        return plaintext.decode()

    async def store_credentials(
        self,
        *,
        user_id: str,
        access_token: str,
        refresh_token: str,
        token_expiry: str | None = None,
        scope: str = "",
        channel_id: str | None = None,
        channel_title: str | None = None,
    ) -> None:
        enc_access, iv_access = self._encrypt(access_token)
        enc_refresh, iv_refresh = self._encrypt(refresh_token)
        row: dict[str, Any] = {
            "user_id": user_id,
            "access_token_ciphertext": enc_access,
            "access_token_iv": iv_access,
            "refresh_token_ciphertext": enc_refresh,
            "refresh_token_iv": iv_refresh,
            "token_expiry": token_expiry,
            "scope": scope,
            "updated_at": _now(),
        }
        if channel_id is not None:
            row["channel_id"] = channel_id
        if channel_title is not None:
            row["channel_title"] = channel_title
        await self._request(
            "POST",
            "/rest/v1/youtube_credentials",
            body=row,
            params={"select": "id", "on_conflict": "user_id"},
        )

    async def get_credentials(self, user_id: str) -> dict[str, Any] | None:
        rows = await self._request(
            "GET",
            "/rest/v1/youtube_credentials",
            params={
                "user_id": f"eq.{user_id}",
                "limit": "1",
                # Explicit columns (egress fix): only the fields actually used.
                "select": (
                    "access_token_ciphertext,access_token_iv,"
                    "refresh_token_ciphertext,refresh_token_iv,"
                    "token_expiry,scope,channel_id,channel_title"
                ),
            },
        )
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        return {
            "access_token": self._decrypt(
                row["access_token_ciphertext"], row["access_token_iv"]
            ),
            "refresh_token": self._decrypt(
                row["refresh_token_ciphertext"], row["refresh_token_iv"]
            ),
            "token_expiry": row.get("token_expiry"),
            "scope": row.get("scope", ""),
            "channel_id": row.get("channel_id"),
            "channel_title": row.get("channel_title"),
        }

    async def update_tokens(
        self,
        *,
        user_id: str,
        access_token: str,
        token_expiry: str | None = None,
        refresh_token: str | None = None,
        scope: str | None = None,
    ) -> None:
        """Persist a refreshed access token (and rotation refresh token if any)."""
        row: dict[str, Any] = {"token_expiry": token_expiry, "updated_at": _now()}
        enc_access, iv_access = self._encrypt(access_token)
        row["access_token_ciphertext"] = enc_access
        row["access_token_iv"] = iv_access
        if refresh_token:
            enc_refresh, iv_refresh = self._encrypt(refresh_token)
            row["refresh_token_ciphertext"] = enc_refresh
            row["refresh_token_iv"] = iv_refresh
        if scope is not None:
            row["scope"] = scope
        await self._request(
            "PATCH",
            "/rest/v1/youtube_credentials",
            body=row,
            params={"user_id": f"eq.{user_id}"},
        )

    async def set_channel(
        self,
        user_id: str,
        *,
        channel_id: str | None,
        channel_title: str | None,
    ) -> None:
        await self._request(
            "PATCH",
            "/rest/v1/youtube_credentials",
            body={
                "channel_id": channel_id,
                "channel_title": channel_title,
                "updated_at": _now(),
            },
            params={"user_id": f"eq.{user_id}"},
        )

    async def delete_credentials(self, user_id: str) -> bool:
        result = await self._request(
            "DELETE",
            "/rest/v1/youtube_credentials",
            params={"user_id": f"eq.{user_id}"},
        )
        # _request() raises on transport/HTTP failure, so reaching this line
        # means the delete actually succeeded.
        return result is not False
