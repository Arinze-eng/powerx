"""Supabase-backed management of public agent API keys (OpenAI-compatible).

Keys are generated in the Telegram bot (/apikey), stored hashed (SHA-256) in
``public.agent_api_keys``, and validated by the ``nanobot serve`` API gateway on
every ``/v1/*`` request. Requests run the full agentic pipeline server-side and
are billed through the existing credit system.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from typing import Any

import httpx
from loguru import logger

KEY_PREFIX = "px_"
_KEY_RE = re.compile(r"^px_[A-Za-z0-9]{40}$")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


class ApiKeyError(RuntimeError):
    pass


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(plain_key, key_hash, key_prefix)`` for a fresh API key."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    random = secrets.SystemRandom()
    plain = KEY_PREFIX + "".join(random.choice(alphabet) for _ in range(40))
    digest = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    prefix = plain[:11]  # px_ + first 8 chars
    return plain, digest, prefix


def hash_api_key(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def looks_like_api_key(value: str) -> bool:
    return bool(_KEY_RE.match(value.strip()))


class ApiKeyStore:
    """Service-role access to the ``agent_api_keys`` / ``agent_api_request_log`` tables."""

    def __init__(self) -> None:
        self.url = _env("SUPABASE_URL").rstrip("/")
        self.service_key = _env("SUPABASE_SERVICE_ROLE_KEY")

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
        self, method: str, path: str, *, params: dict[str, str] | None = None,
        body: Any = None, headers: dict[str, str] | None = None,
    ) -> Any:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.request(
                method, f"{self.url}{path}", params=params, json=body,
                headers=headers or self._headers(),
            )
        if response.status_code >= 400:
            raise ApiKeyError(
                f"Supabase {method} {path} failed ({response.status_code}): {response.text[:300]}"
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def create_key(
        self, *, agentx_user_id: str, telegram_user_id: int | None, name: str,
    ) -> tuple[str, dict[str, Any]]:
        """Create a key row; returns the plaintext key ONCE plus the stored row."""
        plain, digest, prefix = generate_api_key()
        rows = await self._request(
            "POST", "/rest/v1/agent_api_keys",
            params={"select": "*"},
            body={
                "agentx_user_id": agentx_user_id,
                "telegram_user_id": telegram_user_id,
                "name": name.strip()[:64] or "default",
                "key_prefix": prefix,
                "key_hash": digest,
            },
            headers={**self._headers(), "Prefer": "return=representation"},
        )
        if isinstance(rows, list) and rows:
            return plain, rows[0]
        return plain, {}

    async def list_keys(self, agentx_user_id: str) -> list[dict[str, Any]]:
        rows = await self._request(
            "GET", "/rest/v1/agent_api_keys",
            params={
                "agentx_user_id": f"eq.{agentx_user_id}",
                "order": "created_at.desc",
                "select": "id,name,key_prefix,is_active,total_requests,last_used_at,created_at,revoked_at",
            },
        )
        return rows if isinstance(rows, list) else []

    async def revoke_all(self, agentx_user_id: str) -> int:
        rows = await self._request(
            "PATCH", "/rest/v1/agent_api_keys",
            params={
                "agentx_user_id": f"eq.{agentx_user_id}",
                "is_active": "eq.true",
                "select": "id",
            },
            body={"is_active": False},
            headers={**self._headers(), "Prefer": "return=representation"},
        )
        return len(rows) if isinstance(rows, list) else 0

    async def find_active_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        rows = await self._request(
            "GET", "/rest/v1/agent_api_keys",
            params={"key_hash": f"eq.{key_hash}", "is_active": "eq.true", "select": "*"},
        )
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    async def bump_usage(self, key_id: int) -> None:
        """Best-effort usage bookkeeping; never blocks the request path."""
        try:
            await self._request(
                "PATCH", "/rest/v1/agent_api_keys",
                params={"id": f"eq.{key_id}"},
                body={"last_used_at": "now()"},
            )
        except Exception as exc:  # pragma: no cover - bookkeeping only
            logger.debug("api key usage bump failed: {}", exc)

    async def record_request(self, entry: dict[str, Any]) -> None:
        """Fire-and-forget request logging into agent_api_request_log."""
        if not self.enabled:
            return
        try:
            await self._request("POST", "/rest/v1/agent_api_request_log", body=entry)
        except Exception as exc:  # pragma: no cover - logging must never break serving
            logger.debug("api request log write failed: {}", exc)
