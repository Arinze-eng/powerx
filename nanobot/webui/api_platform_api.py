"""The OpenAI-compatible API platform, served to the WebUI settings section.

The Telegram bot has had this for a while behind ``/apikey``, ``/listapikeys``,
``/revokeapikey`` and ``/apidoc`` (``nanobot.channels.telegram.api_platform``).
This module is the same capability for the web app: the base URL, the models a
caller can ask for, and the keys on the account.

Nothing here is Telegram-specific. Both front ends share ``ApiKeyStore`` (hashed
keys in Supabase, billed through the existing credit system) and ``resolve_base_url``,
so a key minted in the bot works from the web and the other way round. What this
adds is the piece the bot never needed: an :func:`platform_action` that returns
the plaintext key to a signed-in web session exactly once, the same way
``/apikey`` does.
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

from nanobot.api.api_keys import ApiKeyError, ApiKeyStore
from nanobot.channels.telegram.api_platform import (
    _MAX_KEYS_PER_USER,
    _model_name,
    resolve_base_url,
)

__all__ = ["platform_action", "platform_payload", "PlatformError"]


class PlatformError(RuntimeError):
    """A user-facing failure: bad input, a limit, or the feature being off."""


def _env(name: str) -> str:
    return os.getenv(name, "").strip().rstrip("/")


def platform_base_url(origin: str = "") -> str:
    """Public base URL of the OpenAI-compatible API, best effort.

    Resolution order, and the reason for each:

    1. ``NANOBOT_API_PUBLIC_URL`` / ``API_SERVER_URL`` — an operator's explicit
       answer, which beats any inference.
    2. The origin the settings page was served from. When the gateway serves the
       WebUI and ``/v1/*`` on one host — the single-process Render deployment —
       the address the browser already reached the page on *is* the API address.
    3. The Telegram webhook host, which the bot already uses to work this out.
    """
    explicit = _env("NANOBOT_API_PUBLIC_URL") or _env("API_SERVER_URL")
    if explicit and "YOUR-SERVER" not in explicit.upper():
        return explicit
    candidate = (origin or "").strip().rstrip("/")
    if candidate.startswith("http://") or candidate.startswith("https://"):
        return candidate
    try:
        return resolve_base_url()
    except Exception as exc:  # unreadable config, no webhook, and so on
        logger.debug("api platform base url unavailable: {}", exc)
        return ""


def platform_models() -> list[dict[str, Any]]:
    """The model ids ``GET /v1/models`` advertises.

    A single agent model, not a catalogue: every request runs the full agent
    pipeline on the server, so the id exists to satisfy OpenAI-compatible
    clients that insist on naming a model rather than to select one.
    """
    return [
        {
            "id": _model_name(),
            "object": "model",
            "created": 0,
            "owned_by": "powerx",
        }
    ]


def _redact(row: dict[str, Any]) -> dict[str, Any]:
    """One key row, with nothing in it that could be replayed.

    ``list_keys`` already selects no hash, but this is the boundary a web client
    reads, so it is written to be safe on its own rather than by trusting the
    query upstream of it.
    """
    return {
        "id": row.get("id"),
        "name": row.get("name") or "default",
        "prefix": row.get("key_prefix") or "",
        "active": bool(row.get("is_active")),
        "requests": int(row.get("total_requests") or 0),
        "last_used_at": row.get("last_used_at"),
        "created_at": row.get("created_at"),
    }


async def platform_payload(agentx_user_id: str, *, origin: str = "") -> dict[str, Any]:
    """Everything the settings section needs to render, in one call."""
    store = ApiKeyStore()
    base_url = platform_base_url(origin)
    payload: dict[str, Any] = {
        "enabled": store.enabled,
        "base_url": base_url,
        "endpoint": f"{base_url}/v1" if base_url else "",
        "configured": bool(base_url),
        "models": platform_models(),
        "keys": [],
        "max_keys": _MAX_KEYS_PER_USER,
        "signed_in": bool(agentx_user_id),
        "docs": {
            "chat_completions": f"{base_url}/v1/chat/completions" if base_url else "",
            "models": f"{base_url}/v1/models" if base_url else "",
            "api_docs": f"{base_url}/v1/api-docs" if base_url else "",
        },
    }
    if not store.enabled:
        payload["notice"] = (
            "The API platform is not configured on this server yet."
        )
        return payload
    if not agentx_user_id:
        payload["notice"] = "Sign in to generate and manage API keys."
        return payload
    try:
        payload["keys"] = [_redact(row) for row in await store.list_keys(agentx_user_id)]
    except ApiKeyError as exc:
        logger.warning("api platform key list failed: {}", str(exc)[:300])
        payload["notice"] = "Could not read your API keys just now."
    if not base_url:
        payload["notice"] = (
            "The server address is not configured, so keys cannot be used yet. An "
            "admin must set NANOBOT_API_PUBLIC_URL."
        )
    return payload


async def platform_action(
    action: str,
    *,
    agentx_user_id: str,
    payload: dict[str, Any] | None = None,
    origin: str = "",
) -> dict[str, Any]:
    """Create or revoke keys from the web app, then return the fresh view.

    ``create`` is the one call that returns a plaintext key, and it does so once
    — the same contract as ``/apikey``. Nothing can ever read it back, because
    only the SHA-256 hash is stored.
    """
    store = ApiKeyStore()
    if not store.enabled:
        raise PlatformError("The API platform is not configured on this server yet.")
    if not agentx_user_id:
        raise PlatformError("Sign in to manage API keys.")
    data = payload or {}

    if action == "create":
        name = str(data.get("name") or "").strip()[:64] or "default"
        existing = await store.list_keys(agentx_user_id)
        active = [row for row in existing if row.get("is_active")]
        if len(active) >= _MAX_KEYS_PER_USER:
            raise PlatformError(
                f"You already have {len(active)} active API keys (limit "
                f"{_MAX_KEYS_PER_USER}). Revoke one first."
            )
        try:
            plain, row = await store.create_key(
                agentx_user_id=agentx_user_id, telegram_user_id=None, name=name
            )
        except ApiKeyError as exc:
            logger.warning("api platform key create failed: {}", str(exc)[:300])
            raise PlatformError("Could not create the key. Please try again.") from exc
        result = await platform_payload(agentx_user_id, origin=origin)
        result["created"] = {
            "key": plain,
            "name": name,
            "id": row.get("id"),
            "prefix": row.get("key_prefix") or plain[:11],
        }
        return result

    if action in {"revoke", "revoke_all"}:
        try:
            revoked = await store.revoke_all(agentx_user_id)
        except ApiKeyError as exc:
            logger.warning("api platform key revoke failed: {}", str(exc)[:300])
            raise PlatformError("Could not revoke the keys. Please try again.") from exc
        result = await platform_payload(agentx_user_id, origin=origin)
        result["revoked"] = revoked
        return result

    raise PlatformError(f"Unknown API platform action: {action}")
