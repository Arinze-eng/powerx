"""Supabase-backed per-user MT5 broker credential store.

This is the storage half of the "Brokers" method: a user types their own
broker login, password and server in Settings -> Brokers and the
``broker_trade`` tool drives that account directly. It is deliberately a
separate table and a separate code path from the Wine/MT5 sandbox method
(``nanobot.agent.tools.mt5_sandbox``), which stays exactly as it was.

Encryption is the same AES-GCM scheme the Alpaca store and the Supabase
session store use, keyed by ``SUPABASE_TOKEN_ENCRYPTION_KEY``. The account
number is stored in the clear because it is not a secret and the settings pane
has to show which account is connected; only the password is encrypted.

Degradation mirrors the Alpaca store: when Supabase is not configured the
store reports ``enabled == False`` and callers fall back to the deployment's
own broker configuration rather than failing.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
from typing import Any

import httpx
from loguru import logger as module_logger

from nanobot.utils.lazy_import import lazy_attr

# ``cryptography`` is a large compiled package and this module is imported by
# the tool loader at startup, so it is resolved on first use rather than at
# import time. See the same note in nanobot/trading/alpaca_credentials.py.
AESGCM = lazy_attr("cryptography.hazmat.primitives.ciphers.aead", "AESGCM")

#: How a broker account is identified when the caller does not name one.
DEFAULT_ROW_LIMIT = 20

#: A single statement to Supabase slower than this is logged. Saving an account
#: is several statements in a row, so one slow line here is what turns "the
#: settings pane timed out" into a number.
_SLOW_REQUEST_LOG_MS = 2_000


class BrokerCredentialError(RuntimeError):
    """A broker credential could not be read, written or decrypted."""


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class BrokerCredentialStore:
    """Service-role access to per-user MT5 broker logins in Supabase."""

    def __init__(self) -> None:
        self.url = _env("SUPABASE_URL").rstrip("/")
        self.service_key = _env("SUPABASE_SERVICE_ROLE_KEY")
        self._crypto = self._build_crypto()
        self._client: httpx.AsyncClient | None = None
        self._client_loop: Any = None

    def _build_crypto(self):
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
            raw_bytes = hashlib.sha256(b"nanobot-broker-session-key").digest()
        try:
            return AESGCM(raw_bytes)
        except ImportError:  # pragma: no cover - cryptography is an optional extra
            return None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.service_key)

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
        }

    def _http_client(self) -> httpx.AsyncClient:
        """The store's HTTP client, kept for the store's lifetime.

        A fresh ``AsyncClient`` per statement paid a DNS lookup and a TLS
        handshake for every one of them, and saving an account is up to three
        statements in a row -- on a small deployment that handshake is a large
        share of the round trip the user waits on. The client is kept per event
        loop because a pooled connection belongs to the loop that opened it:
        tests drive this store from a new ``asyncio.run`` per case, and a
        client carried across loops would fail with "Event loop is closed".
        """
        try:
            loop: Any = asyncio.get_running_loop()
        except RuntimeError:  # a caller outside any loop: nothing to key on
            loop = None
        client = self._client
        if client is None or client.is_closed or self._client_loop is not loop:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=10.0),
                limits=httpx.Limits(max_keepalive_connections=8, max_connections=8),
            )
            self._client = client
            self._client_loop = loop
        return client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: Any = None,
        prefer: str = "",
    ) -> Any:
        if not self.enabled:
            raise BrokerCredentialError("Supabase is not configured")
        headers = self._headers()
        if prefer:
            headers["Prefer"] = prefer
        started = time.perf_counter()
        response = await self._http_client().request(
            method,
            f"{self.url}{path}",
            headers=headers,
            params=params,
            json=body,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if elapsed_ms >= _SLOW_REQUEST_LOG_MS:
            module_logger.warning(
                "slow supabase broker request method={} path={} status={} duration_ms={}",
                method,
                path,
                response.status_code,
                elapsed_ms,
            )
        if response.status_code >= 400:
            raise BrokerCredentialError(
                f"Supabase {method} {path} failed ({response.status_code}): "
                f"{response.text[:300]}"
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _encrypt(self, value: str) -> tuple[str, str]:
        if self._crypto is None:
            raise BrokerCredentialError(
                "Encryption key not configured. Set SUPABASE_TOKEN_ENCRYPTION_KEY."
            )
        iv = os.urandom(12)
        ciphertext = self._crypto.encrypt(iv, value.encode(), None)
        return base64.b64encode(ciphertext).decode(), base64.b64encode(iv).decode()

    def _decrypt(self, ciphertext: str, iv_b64: str) -> str:
        if self._crypto is None:
            raise BrokerCredentialError("Encryption key not configured.")
        plaintext = self._crypto.decrypt(
            base64.b64decode(iv_b64),
            base64.b64decode(ciphertext),
            None,
        )
        return plaintext.decode()

    async def store_credentials(
        self,
        *,
        user_key: str,
        login: str,
        password: str,
        server: str = "",
        broker: str = "",
        make_default: bool = False,
    ) -> dict[str, Any]:
        """Insert or update one broker login for *user_key*.

        The uniqueness key is ``(user_key, login, server)``, so re-saving the
        same account corrects the password instead of creating a second row --
        which is what a user fixing a typo in their password expects.
        """
        if not user_key:
            raise BrokerCredentialError("A user key is required to store broker credentials.")
        login = (login or "").strip()
        server = (server or "").strip()
        broker = (broker or "").strip()
        if not login:
            raise BrokerCredentialError("A broker account number is required.")
        if not password:
            raise BrokerCredentialError("A broker password is required.")
        enc_password, iv_password = self._encrypt(password)
        if make_default:
            # The flag is cleared on the user's other rows *before* the write
            # that sets it, rather than after it: the same single statement
            # leaves no window in which two accounts are the default, and it
            # keeps a save to three statements instead of four. The write below
            # carries is_default=True for the row being saved.
            await self._clear_default(user_key)
        rows = await self._request(
            "POST",
            "/rest/v1/broker_credentials",
            params={
                "on_conflict": "user_key,login,server",
                "select": "id,user_key,broker,server,login,is_default",
            },
            body={
                "user_key": user_key,
                "broker": broker,
                "server": server,
                "login": login,
                "password_ciphertext": enc_password,
                "password_iv": iv_password,
                "is_default": bool(make_default),
                "updated_at": _now(),
            },
            # A re-save is a correction, so the row is merged rather than refused.
            prefer="resolution=merge-duplicates,return=representation",
        )
        row = rows[0] if isinstance(rows, list) and rows else None
        return row or {"user_key": user_key, "broker": broker, "server": server, "login": login}

    async def _clear_default(self, user_key: str) -> None:
        """Drop the default flag from every account *user_key* has."""
        await self._request(
            "PATCH",
            "/rest/v1/broker_credentials",
            params={"user_key": f"eq.{user_key}"},
            body={"is_default": False},
        )

    async def list_credentials(self, user_key: str) -> list[dict[str, Any]]:
        """Every stored broker account for *user_key*, without passwords."""
        if not user_key:
            return []
        rows = await self._request(
            "GET",
            "/rest/v1/broker_credentials",
            params={
                "user_key": f"eq.{user_key}",
                "order": "is_default.desc,updated_at.desc",
                "limit": str(DEFAULT_ROW_LIMIT),
                "select": "id,broker,server,login,is_default,updated_at",
            },
        )
        return rows if isinstance(rows, list) else []

    async def get_credentials(
        self,
        user_key: str,
        *,
        login: str = "",
        server: str = "",
    ) -> dict[str, str] | None:
        """The decrypted login/password/server for one of *user_key*'s accounts.

        With no ``login`` the user's default account is returned (falling back
        to the most recently updated row), so ``broker_trade`` can be called
        without naming an account every time.
        """
        if not user_key:
            return None
        params: dict[str, str] = {
            "user_key": f"eq.{user_key}",
            "limit": "1",
            "select": (
                "broker,server,login,password_ciphertext,password_iv,is_default"
            ),
        }
        if login:
            params["login"] = f"eq.{login}"
            if server:
                params["server"] = f"eq.{server}"
        else:
            params["order"] = "is_default.desc,updated_at.desc"
        rows = await self._request("GET", "/rest/v1/broker_credentials", params=params)
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        return {
            "broker": row.get("broker") or "",
            "server": row.get("server") or "",
            "login": str(row.get("login") or ""),
            "password": self._decrypt(
                row["password_ciphertext"],
                row["password_iv"],
            ),
        }

    async def set_default(self, user_key: str, *, login: str, server: str = "") -> None:
        """Make one account the user's default and clear the flag on the rest."""
        if not user_key:
            raise BrokerCredentialError("A user key is required.")
        # Every other row loses the flag first: a partial unique index is not
        # assumed, so two defaults must not be able to coexist even briefly
        # between the two statements.
        await self._clear_default(user_key)
        await self._request(
            "PATCH",
            "/rest/v1/broker_credentials",
            params={
                "user_key": f"eq.{user_key}",
                "login": f"eq.{login}",
                **({"server": f"eq.{server}"} if server else {}),
            },
            body={"is_default": True, "updated_at": _now()},
        )

    async def delete_credentials(
        self,
        user_key: str,
        *,
        login: str = "",
        server: str = "",
    ) -> bool:
        """Remove one account, or every account for *user_key* when none is named."""
        if not user_key:
            raise BrokerCredentialError("A user key is required.")
        params: dict[str, str] = {"user_key": f"eq.{user_key}"}
        if login:
            params["login"] = f"eq.{login}"
        if server:
            params["server"] = f"eq.{server}"
        await self._request("DELETE", "/rest/v1/broker_credentials", params=params)
        # _request raises on any transport or HTTP failure, so reaching here means
        # the delete landed; there is no separate row count to check.
        return True
