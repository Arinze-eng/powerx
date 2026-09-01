"""Telegram Web App (Mini App) initData validation and short-lived tokens.

A Telegram Mini App runs inside the user's client and can hand its HTML page a
way to talk to the agent API without embedding the long-lived ``api_key`` in
client-side JavaScript (which anyone could extract). Instead:

1. The page posts Telegram's raw ``initData`` string to ``/app/token``.
2. We verify it against the bot token using Telegram's documented HMAC scheme,
   proving the request really came from an authenticated Telegram user.
3. We mint a short-lived bearer token bound to that user's id, which the page
   uses as ``Authorization: Bearer <token>`` on ``/v1/chat/completions``.

Tokens live only in this process's memory and expire quickly, so a leaked one
is useless after the TTL and grants access to exactly one Telegram chat session.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
import uuid
from typing import Any
from urllib.parse import unquote

# How long a minted Mini App token stays valid. Long enough for a working
# session, short enough that a leaked token self-heals.
TOKEN_TTL_SECONDS = 60 * 60  # 1 hour


def validate_init_data(init_data: str, bot_token: str) -> dict[str, Any] | None:
    """Verify a Telegram Web App ``initData`` string against *bot_token*.

    Follows Telegram's documented algorithm: the query-string pairs except
    ``hash``/``signature`` are URL-decoded, sorted, joined with newlines into a
    "data check string", and HMAC-SHA256'd with a key derived from the bot
    token (``WebAppData`` + token). Returns the parsed fields (including a
    ``user`` JSON blob) when the hash matches, otherwise ``None``.
    """
    if not init_data or not bot_token:
        return None
    try:
        pairs = [p.split("=", 1) for p in init_data.split("&") if "=" in p]
    except ValueError:
        return None
    fields: dict[str, str] = {}
    for key, value in pairs:
        fields[key] = value
    received_hash = fields.pop("hash", "") or fields.pop("signature", "")
    if not received_hash:
        return None
    # Percent-decode each value before building the check string.
    decoded = {k: unquote(v) for k, v in fields.items()}
    check_string = "\n".join(f"{k}={decoded[k]}" for k in sorted(decoded))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None
    return decoded


class MiniAppTokenStore:
    """Thread-safe registry of short-lived Mini App bearer tokens."""

    def __init__(self, ttl_seconds: int = TOKEN_TTL_SECONDS) -> None:
        self._ttl = max(60, int(ttl_seconds))
        self._lock = threading.RLock()
        # token -> (session_key, expires_at)
        self._tokens: dict[str, tuple[str, float]] = {}

    def _prune(self, now: float) -> None:
        expired = [t for t, (_key, exp) in self._tokens.items() if exp <= now]
        for token in expired:
            self._tokens.pop(token, None)

    def mint(self, session_key: str) -> str:
        """Return a fresh opaque token bound to *session_key*."""
        token = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._prune(now)
            self._tokens[token] = (str(session_key or "unknown"), now + self._ttl)
        return token

    def verify(self, token: str) -> str | None:
        """Return the bound session key for a live *token*, else ``None``."""
        if not token:
            return None
        now = time.time()
        with self._lock:
            self._prune(now)
            entry = self._tokens.get(token)
            if not entry:
                return None
            session_key, expires_at = entry
            if expires_at <= now:
                self._tokens.pop(token, None)
                return None
            return session_key

    def revoke(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)


# Process-wide singleton shared by the HTTP handlers and the auth middleware.
miniapp_tokens = MiniAppTokenStore()
