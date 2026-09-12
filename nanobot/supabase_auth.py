from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Explicit columns (egress fix): telegram_accounts is fetched on every inbound
# message. select=* shipped the whole row (crypto/auth blobs, opt-ins) each
# time; only these fields are ever read back by this module.
_ACCOUNT_COLUMNS = (
    "telegram_user_id,agentx_user_id,chat_id,username,first_name,last_name,"
    "last_seen_at,auth_email,auth_state,pending_attachment,"
    "session_token_ciphertext,session_token_iv,refresh_token_ciphertext,refresh_token_iv"
)


class SupabaseAuthError(RuntimeError):
    pass


class SupabaseAuth:
    """Small async client for the existing AgentX Supabase Auth/credit schema."""

    def __init__(self) -> None:
        self.url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        self.anon_key = os.getenv("SUPABASE_ANON_KEY", "").strip()
        self.service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        current_key = os.getenv("SUPABASE_TOKEN_ENCRYPTION_KEY", "").strip()
        previous_keys = [
            os.getenv("SUPABASE_TOKEN_ENCRYPTION_KEY_PREVIOUS", "").strip(),
            os.getenv("SUPABASE_TOKEN_ENCRYPTION_KEY_OLD", "").strip(),
        ]
        self._crypto_candidates = self._build_crypto_candidates(
            current_key,
            previous_keys,
        )
        self._crypto = self._crypto_candidates[0]

    @staticmethod
    def _build_crypto(value: str) -> AESGCM:
        raw = b""
        if value:
            try:
                raw = base64.b64decode(value, validate=True)
            except Exception:
                raw = b""
            if len(raw) != 32:
                raw = hashlib.sha256(value.encode()).digest()
        if len(raw) != 32:
            raw = hashlib.sha256(b"nanobot-supabase-session-key").digest()
        return AESGCM(raw)

    @classmethod
    def _build_crypto_candidates(
        cls,
        current_key: str,
        previous_keys: list[str],
    ) -> tuple[AESGCM, ...]:
        """Build a decryption key ring while keeping new writes on the current key."""
        values: list[str] = []
        for value in [current_key, *previous_keys]:
            if value and value not in values:
                values.append(value)
        if not values:
            return (cls._build_crypto(""),)
        return tuple(cls._build_crypto(value) for value in values)

    @property
    def configured(self) -> bool:
        return bool(self.url and self.anon_key and self.service_key)

    def public_config(self) -> dict[str, Any]:
        """Public-safe auth config shared with the WebUI so the client can
        render a sign-up / sign-in page without any secret keys."""
        return {
            "enabled": self.enabled,
            "url": self.url,
            "anon_key": self.anon_key,
        }

    # ------------------------------------------------------------------
    # Local JWT verification (egress policy 2026-09-10)
    # ------------------------------------------------------------------
    # The auth+secrets-only egress policy means: no REST reads for user
    # identity on the hot message path. The WebUI session token is a
    # Supabase-issued JWT — verify it locally (signature via the project's
    # published JWKS + exp check) and cache results in memory. Falls back to
    # /auth/v1/user only when local verification cannot be trusted (unknown
    # kid, key fetch failure). This keeps the security property (only valid
    # Supabase tokens pass) while reducing per-request egress from ~500 B to
    # 0 for cached tokens. Set NANOBOT_JWT_LOCAL_VERIFY=false to force remote
    # verification everywhere.

    _JWKS_REFRESH_SECONDS = 24 * 3600.0
    _jwt_verifiers: dict[str, Any] = {}      # url -> {"jwks": dict, "fetched_at": float}
    # Verified-token cache: sha256(token) -> ({"id","email"}, exp_epoch).
    # Class-level so every SupabaseAuth() construction reuses it. Bounded by
    # TTL (tokens expire ~1 h) plus a hard cap to keep memory tiny.
    _verified_tokens: dict[str, tuple[dict[str, str], float]] = {}
    _VERIFIED_CACHE_MAX = 2048

    @classmethod
    def _verified_cache_key(cls, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @classmethod
    def _verified_cache_get(cls, token: str) -> dict[str, str] | None:
        import time as _time

        entry = cls._verified_tokens.get(cls._verified_cache_key(token))
        if not entry:
            return None
        result, exp = entry
        if exp <= _time.time():
            cls._verified_tokens.pop(cls._verified_cache_key(token), None)
            return None
        return dict(result)

    @classmethod
    def _verified_cache_put(cls, token: str, result: dict[str, str]) -> None:
        import time as _time

        try:
            decoded = token.split(".")
            padded = decoded[1] + "=" * (-len(decoded[1]) % 4)
            exp = float(json.loads(base64.urlsafe_b64decode(padded)).get("exp") or 0)
        except Exception:
            return
        if exp <= 0:
            return
        if len(cls._verified_tokens) >= cls._VERIFIED_CACHE_MAX:
            # Drop entries that are expired; if still full, clear wholesale
            # (a cold JWKS fetch is cheap — once/day steady state).
            now = _time.time()
            for key in [k for k, (_, e) in cls._verified_tokens.items() if e <= now]:
                cls._verified_tokens.pop(key, None)
            if len(cls._verified_tokens) >= cls._VERIFIED_CACHE_MAX:
                cls._verified_tokens.clear()
        cls._verified_tokens[cls._verified_cache_key(token)] = (dict(result), exp)


    @property
    def _local_verify_enabled(self) -> bool:
        return os.getenv("NANOBOT_JWT_LOCAL_VERIFY", "true").lower() not in {
            "0", "false", "no",
        }

    def _decode_jwt(self, token: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Decode (not verify) a compact JWS into (header, payload); None if malformed."""
        parts = token.split(".")
        if len(parts) != 3:
            return None

        def _b64json(chunk: str) -> dict[str, Any]:
            padded = chunk + "=" * (-len(chunk) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded.encode()))
            return data if isinstance(data, dict) else {}

        try:
            header = _b64json(parts[0])
            payload = _b64json(parts[1])
        except Exception:
            return None
        return header, payload

    async def _get_jwks(self) -> dict[str, Any] | None:
        """Fetch (and process-cached) the project's GoTrue JWKS.

        Cached in memory for 24 h; shared across all SupabaseAuth instances via
        the class-level ``_jwt_verifiers`` map keyed by project URL. A cold
        fetch costs one small request (~1 KB) once per day.
        """
        import time as _time

        entry = self._jwt_verifiers.get(self.url)
        now = _time.monotonic()
        if entry and now - entry["fetched_at"] < self._JWKS_REFRESH_SECONDS:
            return entry["jwks"]
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(f"{self.url}/auth/v1/.well-known/jwks.json")
            if not response.is_success:
                return entry["jwks"] if entry else None
            jwks = response.json()
        except (httpx.HTTPError, ValueError):
            return entry["jwks"] if entry else None
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            return entry["jwks"] if entry else None
        self._jwt_verifiers[self.url] = {"jwks": jwks, "fetched_at": now}
        return jwks

    async def _verify_locally(self, token: str) -> dict[str, str] | None:
        """Verify a Supabase access token against the project JWKS.

        Returns ``{"id", "email"}`` on success, ``None`` when the token cannot
        be verified locally (caller should fall back to the remote check).
        """
        import time as _time

        decoded = self._decode_jwt(token)
        if not decoded:
            return None
        header, payload = decoded
        alg = str(header.get("alg") or "")
        if alg not in {"RS256", "ES256"}:
            # HS256 legacy tokens (or anything unexpected) are verified remotely.
            return None
        jwks = await self._get_jwks()
        if not jwks:
            return None
        keys = jwks.get("keys") or []
        kid = header.get("kid")
        candidates = [k for k in keys if isinstance(k, dict)]
        if kid:
            candidates = [k for k in candidates if k.get("kid") == kid] or candidates
        signing_input = ".".join(token.split(".")[:2]).encode()
        try:
            sig = base64.urlsafe_b64decode(token.split(".")[2] + "=" * (-len(token.split(".")[2]) % 4))
        except Exception:
            return None
        verified = False
        for key in candidates:
            kty = key.get("kty")
            try:
                if kty == "RSA" and alg == "RS256":
                    from cryptography.hazmat.primitives import hashes
                    from cryptography.hazmat.primitives.asymmetric import padding, rsa

                    n = int.from_bytes(base64.urlsafe_b64decode(key["n"] + "=" * 4), "big")
                    e = int.from_bytes(base64.urlsafe_b64decode(key["e"] + "=" * 4), "big")
                    public_key = rsa.RSAPublicNumbers(e, n).public_key()
                    public_key.verify(sig, signing_input, padding.PKCS1v15(), hashes.SHA256())
                    verified = True
                    break
                if kty == "EC" and alg == "ES256":
                    import hashlib

                    from cryptography.hazmat.primitives.asymmetric import ec, utils as _utils

                    x = int.from_bytes(base64.urlsafe_b64decode(key["x"] + "=" * 4), "big")
                    y = int.from_bytes(base64.urlsafe_b64decode(key["y"] + "=" * 4), "big")
                    public_key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
                    r = int.from_bytes(sig[: len(sig) // 2], "big")
                    s = int.from_bytes(sig[len(sig) // 2 :], "big")
                    public_key.verify(
                        _utils.encode_dss_signature(r, s), signing_input, ec.ECDSA(hashes.SHA256())
                    )
                    verified = True
                    break
            except Exception:
                continue
        if not verified:
            return None
        now = _time.time()
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)) or exp <= now + 30:
            return None
        user_id = str(payload.get("sub") or "")
        if not user_id:
            return None
        email = str(payload.get("email") or "")
        return {"id": user_id, "email": email}

    async def verify_access_token(self, access_token: str) -> str | None:
        """Validate an access token and return the user id.

        Prefers local JWKS verification (zero steady-state egress); falls back
        to Supabase's ``/auth/v1/user`` endpoint whenever local verification
        cannot be trusted. Returns ``None`` when the token is absent or invalid.
        """
        return (await self.verify_access_token_details(access_token) or {}).get("id")

    async def verify_access_token_details(
        self, access_token: str,
    ) -> dict[str, str] | None:
        """Return ``{"id": ..., "email": ...}`` for a valid access token, else None.

        Egress policy (2026-09-10): try local JWKS verification + in-memory
        cache first; only uncached/legacy tokens hit the network.
        """
        token = (access_token or "").strip()
        if not token:
            return None
        if not self.configured:
            return None
        # 1) In-memory cache of previously verified tokens (keyed by token hash).
        cached = self._verified_cache_get(token)
        if cached is not None:
            return cached
        # 2) Local JWKS verification — zero steady-state egress.
        if self._local_verify_enabled:
            local = await self._verify_locally(token)
            if local is not None:
                self._verified_cache_put(token, local)
                return local
        # 3) Remote fallback (HS256 legacy tokens, unknown kid, JWKS cold
        #    fetch failure). One small request, then cached.
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                response = await client.request(
                    "GET",
                    f"{self.url}/auth/v1/user",
                    headers={
                        "apikey": self.anon_key,
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError:
            return None
        if not response.is_success:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        user_id = (payload.get("id") or "")
        if not user_id:
            return None
        email = (payload.get("email") or "")
        result = {"id": str(user_id), "email": str(email)}
        # Cache the remote verification so repeat requests are free until the
        # token expires.
        self._verified_cache_put(token, result)
        return result

    def verify_access_token_sync(self, access_token: str) -> tuple[str, str]:
        """Synchronous variant returning ``(user_id, email)`` for sync handlers.

        Avoids the async plumbing that a synchronous HTTP route cannot await.
        Egress policy: consults the shared verified-token cache and local JWKS
        verification first; only uncached legacy tokens hit /auth/v1/user.
        """
        token = (access_token or "").strip()
        if not token or not self.configured:
            return ("", "")
        cached = self._verified_cache_get(token)
        if cached is not None:
            return (cached.get("id", ""), cached.get("email", ""))
        # Local JWKS path needs an await; run it on a short-lived loop only
        # when no event loop is already running this frame's caller context.
        if self._local_verify_enabled:
            try:
                import asyncio as _asyncio

                try:
                    _asyncio.get_running_loop()
                    in_loop = True
                except RuntimeError:
                    in_loop = False
                if not in_loop:
                    local = _asyncio.run(self._verify_locally(token))
                else:
                    # Called from inside a running loop but must stay sync —
                    # do the verification inline (JWKS fetch is awaited via a
                    # dedicated thread).
                    import threading
                    box: dict[str, Any] = {}

                    def _worker() -> None:
                        box["result"] = _asyncio.run(self._verify_locally(token))

                    thread = threading.Thread(target=_worker, daemon=True)
                    thread.start()
                    thread.join(timeout=20)
                    local = box.get("result")
                if local is not None:
                    self._verified_cache_put(token, local)
                    return (local.get("id", ""), local.get("email", ""))
            except Exception:
                pass
        try:
            import urllib.request

            req = urllib.request.Request(
                f"{self.url}/auth/v1/user",
                headers={
                    "apikey": self.anon_key,
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return ("", "")
        if not isinstance(payload, dict):
            return ("", "")
        user_id = str(payload.get("id") or "")
        email = str(payload.get("email") or "")
        if user_id:
            self._verified_cache_put(token, {"id": user_id, "email": email})
        return (user_id, email)

    @property
    def enabled(self) -> bool:
        return self.configured and os.getenv("SUPABASE_AUTH_ENABLED", "true").lower() not in {"0", "false", "no"}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _headers(self, *, service: bool = False, access_token: str = "") -> dict[str, str]:
        key = self.service_key if service else self.anon_key
        headers = {"apikey": key, "Authorization": f"Bearer {access_token or key}", "Content-Type": "application/json"}
        return headers

    async def _request(self, method: str, path: str, *, service: bool = False, access_token: str = "", params: dict[str, str] | None = None, body: Any = None) -> Any:
        if not self.enabled:
            raise SupabaseAuthError("Supabase integration is not configured")
        try:
            # Supabase cold starts surface as connect timeouts or transient
            # 5xx; a single attempt makes presence writes fail exactly when the
            # site is busiest. Two quick retries with backoff fix it cheaply.
            import asyncio as _asyncio

            response = None
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
                        response = await client.request(method, f"{self.url}{path}", headers=self._headers(service=service, access_token=access_token), params=params, json=body)
                    if response.status_code < 500 or attempt == 2:
                        break
                except httpx.HTTPError:
                    if attempt == 2:
                        raise
                    await _asyncio.sleep(0.8 * (attempt + 1))
        except httpx.HTTPError as exc:
            raise SupabaseAuthError("Supabase request failed") from exc
        if not response.is_success:
            detail = ""
            try:
                payload = response.json()
                detail = str(payload.get("msg") or payload.get("message") or payload.get("error_description") or payload.get("error") or "")
            except ValueError:
                pass
            raise SupabaseAuthError(detail[:400] or f"Supabase request failed with HTTP {response.status_code}")
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise SupabaseAuthError("Supabase returned invalid JSON") from exc

    @staticmethod
    def _sanitize_question(value: str) -> str:
        text = value.strip()[:4000]
        text = re.sub(
            r"(?i)(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret|authorization|bearer)\s*[:=]\s*[^\s,;]+",
            r"\1=[redacted]",
            text,
        )
        text = re.sub(r"(?i)\b(?:sk|gsk|ghp|xoxb|xoxp)-[A-Za-z0-9_-]{12,}\b", "[redacted-token]", text)
        return text

    async def record_telegram_question(
        self,
        account: dict[str, Any],
        *,
        chat_id: int,
        message_id: int | None,
        question: str,
        has_attachment: bool,
        task_id: str | None = None,
    ) -> None:
        """Store bounded, redacted task text for the protected admin history view."""
        if not self.enabled or not account.get("telegram_user_id"):
            return
        text = self._sanitize_question(question)
        if not text:
            return
        body: dict[str, Any] = {
            "telegram_user_id": int(account["telegram_user_id"]),
            "chat_id": int(chat_id),
            "question": text,
            "has_attachment": bool(has_attachment),
        }
        if message_id is not None:
            body["telegram_message_id"] = int(message_id)
        if task_id:
            body["task_id"] = str(task_id)[:200]
        await self._request(
            "POST",
            "/rest/v1/telegram_question_history",
            service=True,
            params={"select": "id"},
            body=body,
        )

    async def record_webui_activity(
        self,
        user_id: str,
        *,
        question: str,
        channel: str = "webui",
    ) -> None:
        """Record WebUI user activity for the admin dashboard.

        Egress policy (2026-09-10): this used to cost 4-5 Supabase requests per
        WebUI message (last_seen RPC + question insert + count read + count
        write). Now it is ONE batched RPC per message, and presence updates are
        throttled to one per user per hour in-process. If the batched RPC is
        not deployed yet, falls back to the single cheapest legacy call (the
        question insert) with presence/counter throttling — never the old
        5-call path.
        """
        import time as _time

        user_id = (user_id or "").strip()
        if not user_id or not self.enabled:
            return
        text = self._sanitize_question(question or "")

        # Throttle cosmetic presence writes: at most once per user per TTL.
        seen = self._presence_seen.get(user_id, 0.0)
        touch = _time.time() - seen >= self._PRESENCE_TTL
        if touch:
            self._presence_seen[user_id] = _time.time()

        # Preferred: one atomic RPC that does last_seen + question + counter
        # server-side (zero response payload).
        if self._batch_rpc_available is not False:
            try:
                await self._request(
                    "POST",
                    "/rest/v1/rpc/record_user_activity",
                    service=True,
                    body={
                        "p_user": user_id,
                        "p_question": text[:4000] if text else None,
                        "p_category": channel,
                        "p_touch_presence": touch,
                    },
                )
                self._batch_rpc_available = True
                return
            except SupabaseAuthError:
                # 404 => function not deployed; remember and use legacy path.
                self._batch_rpc_available = False

        # Legacy fallback, trimmed to essentials (best-effort, never raises).
        if text:
            try:
                await self._request(
                    "POST",
                    "/rest/v1/user_questions",
                    service=True,
                    params={"select": "id"},
                    body={
                        "user_id": user_id,
                        "title": text[:500],
                        "message": text,
                        "category": channel,
                        "created_by": user_id,
                    },
                )
            except SupabaseAuthError:
                pass
        if touch:
            try:
                await self._request(
                    "POST",
                    "/rest/v1/rpc/update_last_seen",
                    service=True,
                    body={"p_user": user_id},
                )
            except SupabaseAuthError:
                pass

    # ------------------------------------------------------------------
    # Telegram-account read cache (egress policy 2026-09-10)
    # ------------------------------------------------------------------
    # ``account_for`` used to run GET + PATCH on EVERY inbound Telegram
    # message. The row carries auth/crypto blobs needed only for sign-in
    # flows and token refresh — not for ordinary turns. Steady state now:
    #   * first sighting per user per process → GET (+ create/update patch)
    #   * within NANOBOT_ACCOUNT_CACHE_TTL (default 3600 s) → memory hit,
    #     zero requests, no presence write (last_seen_at is cosmetic)
    # Mutating flows (sign in/out, token refresh, credential edits) call
    # ``invalidate_account_cache`` so they always observe fresh rows.
    _account_cache: dict[int, tuple[dict[str, Any], float]] = {}
    # Presence-write throttle (record_webui_activity): user -> last write epoch.
    _presence_seen: dict[str, float] = {}
    _PRESENCE_TTL = 3600.0
    # None = untried, True = batched RPC works, False = fall back to legacy.
    _batch_rpc_available: bool | None = None

    @staticmethod
    def _account_ttl() -> float:
        try:
            return max(60.0, float(os.getenv("NANOBOT_ACCOUNT_CACHE_TTL", "3600")))
        except ValueError:
            return 3600.0

    @classmethod
    def invalidate_account_cache(cls, telegram_user_id: int | None = None) -> None:
        if telegram_user_id is None:
            cls._account_cache.clear()
        else:
            cls._account_cache.pop(int(telegram_user_id), None)

    async def account_for(self, telegram_user_id: int, chat_id: int, *, username: str | None, first_name: str | None, last_name: str | None) -> dict[str, Any]:
        import time as _time

        cached = self._account_cache.get(int(telegram_user_id))
        if cached and cached[1] > _time.time():
            account = dict(cached[0])
            # Keep the in-memory view fresh for routing without touching the
            # network; the durable copy syncs on the next TTL expiry.
            account["chat_id"] = chat_id
            return account
        rows = await self._request("GET", "/rest/v1/telegram_accounts", service=True, params={"telegram_user_id": f"eq.{telegram_user_id}", "limit": "1", "select": _ACCOUNT_COLUMNS})
        patch = {"chat_id": chat_id, "username": username, "first_name": first_name, "last_name": last_name, "last_seen_at": self._now(), "updated_at": self._now()}
        if isinstance(rows, list) and rows:
            result = await self._request("PATCH", "/rest/v1/telegram_accounts", service=True, params={"telegram_user_id": f"eq.{telegram_user_id}", "select": _ACCOUNT_COLUMNS}, body=patch)
            account = result[0] if isinstance(result, list) and result else {**rows[0], **patch}
        else:
            result = await self._request("POST", "/rest/v1/telegram_accounts", service=True, params={"select": _ACCOUNT_COLUMNS}, body={"telegram_user_id": telegram_user_id, **patch})
            account = result[0] if isinstance(result, list) and result else {"telegram_user_id": telegram_user_id, **patch}
        self._account_cache[int(telegram_user_id)] = (dict(account), _time.time() + self._account_ttl())
        return account

    async def refresh_account(self, telegram_user_id: int) -> dict[str, Any]:
        rows = await self._request("GET", "/rest/v1/telegram_accounts", service=True, params={"telegram_user_id": f"eq.{telegram_user_id}", "limit": "1", "select": _ACCOUNT_COLUMNS})
        if not isinstance(rows, list) or not rows:
            raise SupabaseAuthError("Telegram account not found")
        return rows[0]

    @staticmethod
    def auth_state(account: dict[str, Any]) -> dict[str, Any] | None:
        state = account.get("auth_state")
        if not isinstance(state, dict) or state.get("flow") not in {"signup", "signin"} or state.get("step") not in {"name", "email", "password"}:
            return None
        try:
            started = datetime.fromisoformat(str(state.get("started_at", "")).replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - started).total_seconds() > 15 * 60:
                return None
        except ValueError:
            return None
        return state

    @staticmethod
    def is_authenticated(account: dict[str, Any]) -> bool:
        return bool(account.get("agentx_user_id") and account.get("session_token_ciphertext") and account.get("session_token_iv") and account.get("refresh_token_ciphertext") and account.get("refresh_token_iv"))

    async def save_state(self, telegram_user_id: int, state: dict[str, Any] | None) -> None:
        await self._request("PATCH", "/rest/v1/telegram_accounts", service=True, params={"telegram_user_id": f"eq.{telegram_user_id}"}, body={"auth_state": state or {}, "updated_at": self._now()})
        # Mutating flows must never read a stale cached row.
        self.invalidate_account_cache(telegram_user_id)

    async def start_auth(self, account: dict[str, Any], flow: str) -> str:
        if flow not in {"signup", "signin"}:
            raise SupabaseAuthError("unknown authentication flow")
        if flow == "signup" and account.get("agentx_user_id"):
            return "This Telegram account is already linked. After signout, use /signin only; /signup is disabled for this linked account."
        if self.is_authenticated(account):
            return f"You are already signed in as {account.get('auth_email') or 'your AgentX account'}. Use /signout first."
        step = "name" if flow == "signup" else "email"
        await self.save_state(int(account["telegram_user_id"]), {"flow": flow, "step": step, "started_at": self._now()})
        return "Sign-up step 1 of 3: send your name. Send /cancel to stop." if flow == "signup" else "Sign-in step 1 of 2: send your AgentX email address. Send /cancel to stop."

    def _encrypt(self, value: str) -> tuple[str, str]:
        iv = os.urandom(12)
        ciphertext = self._crypto.encrypt(iv, value.encode(), None)
        return base64.b64encode(ciphertext).decode(), base64.b64encode(iv).decode()

    async def _authenticate(self, account: dict[str, Any], state: dict[str, Any]) -> str:
        email = str(state.get("email") or "").strip().lower()
        password = str(state.get("password") or "")
        flow = str(state.get("flow"))
        if len(password) < 8 or len(password) > 72:
            raise SupabaseAuthError("Passwords must be between 8 and 72 characters")
        endpoint = "/auth/v1/signup" if flow == "signup" else "/auth/v1/token"
        params = None if flow == "signup" else {"grant_type": "password"}
        body: dict[str, Any] = {"email": email, "password": password}
        if flow == "signup":
            body["data"] = {"name": str(state.get("name") or "Telegram User")[:120], "role": "user", "source": "telegram"}
        payload = await self._request("POST", endpoint, params=params, body=body)
        if flow == "signup" and not payload.get("access_token"):
            payload = await self._request("POST", "/auth/v1/token", params={"grant_type": "password"}, body={"email": email, "password": password})
        user_id = str((payload.get("user") or {}).get("id") or payload.get("id") or "")
        access = str(payload.get("access_token") or "")
        refresh = str(payload.get("refresh_token") or "")
        if not user_id or not access or not refresh:
            raise SupabaseAuthError("Supabase did not return a complete session")
        linked = await self._request("GET", "/rest/v1/telegram_accounts", service=True, params={"agentx_user_id": f"eq.{user_id}", "telegram_user_id": f"neq.{int(account['telegram_user_id'])}", "limit": "1", "select": "telegram_user_id"})
        if isinstance(linked, list) and linked:
            raise SupabaseAuthError("That AgentX account is already connected to another Telegram account")
        enc_access, iv_access = self._encrypt(access)
        enc_refresh, iv_refresh = self._encrypt(refresh)
        profile = await self._request("GET", "/rest/v1/profiles", service=True, params={"id": f"eq.{user_id}", "limit": "1", "select": "novita_user_opt_in,vps_docker_user_opt_in,github_user_opt_in"})
        profile_row = profile[0] if isinstance(profile, list) and profile else {}
        await self._request("PATCH", "/rest/v1/telegram_accounts", service=True, params={"telegram_user_id": f"eq.{int(account['telegram_user_id'])}"}, body={"agentx_user_id": user_id, "auth_email": email, "session_token_ciphertext": enc_access, "session_token_iv": iv_access, "refresh_token_ciphertext": enc_refresh, "refresh_token_iv": iv_refresh, "auth_state": {}, "novita_user_opt_in": profile_row.get("novita_user_opt_in") is True, "vps_docker_user_opt_in": profile_row.get("vps_docker_user_opt_in") is True, "github_user_opt_in": profile_row.get("github_user_opt_in") is True, "updated_at": self._now()})
        self.invalidate_account_cache(int(account["telegram_user_id"]))
        return f"{'Your AgentX account was created' if flow == 'signup' else 'You are signed in'} successfully as {email}."

    async def handle_auth_message(self, account: dict[str, Any], text: str) -> str | None:
        state = self.auth_state(account)
        if not state or not text or text.startswith("/"):
            return None
        value = text.strip()
        if state["step"] == "name":
            await self.save_state(int(account["telegram_user_id"]), {**state, "step": "email", "name": value[:120]})
            return "Sign-up step 2 of 3: send your AgentX email address."
        if state["step"] == "email":
            if not __import__("re").match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
                return "That does not look like a valid email address. Please send the email again or /cancel."
            await self.save_state(int(account["telegram_user_id"]), {**state, "step": "password", "email": value.lower()})
            return f"{'Sign-up' if state['flow'] == 'signup' else 'Sign-in'} final step: send your password now in this private chat. The password is not stored by nanobot."
        if state["step"] == "password":
            try:
                return await self._authenticate(account, {**state, "password": value})
            except SupabaseAuthError as exc:
                return f"{exc}. Please send the password again or /cancel."
        return None

    async def signout(self, account: dict[str, Any]) -> str:
        if not account.get("agentx_user_id"):
            return "You are not signed in. Use /signup or /signin first."
        if not self.is_authenticated(account):
            return "You are already signed out. Use /signin again; /signup is disabled for this linked account."
        await self._request("PATCH", "/rest/v1/telegram_accounts", service=True, params={"telegram_user_id": f"eq.{int(account['telegram_user_id'])}"}, body={"session_token_ciphertext": None, "session_token_iv": None, "refresh_token_ciphertext": None, "refresh_token_iv": None, "auth_state": {}, "updated_at": self._now()})
        self.invalidate_account_cache(int(account["telegram_user_id"]))
        return "You are signed out. Your AgentX account and tasks are preserved. Use /signin to authenticate again."

    async def credits(self, account: dict[str, Any]) -> str:
        if not account.get("agentx_user_id"):
            raise SupabaseAuthError("Use /signup or /signin first")
        rows = await self._request("GET", "/rest/v1/profiles", service=True, params={"id": f"eq.{account['agentx_user_id']}", "limit": "1", "select": "daily_credits,purchased_credits,granted_credits,drain_rate"})
        if not isinstance(rows, list) or not rows:
            raise SupabaseAuthError("AgentX profile is unavailable")
        row = rows[0]
        daily = int(row.get("daily_credits") or 0)
        purchased = int(row.get("purchased_credits") or 0)
        granted = int(row.get("granted_credits") or 0)
        rate = max(1, int(row.get("drain_rate") or 1))
        return f"Available credits: {daily + purchased + granted}\nDaily: {daily}\nPurchased: {purchased}\nGranted: {granted}\nDrain rate: {rate}x\nNovita cost per step: {3 * rate}"

    def _decrypt(self, ciphertext: str, iv_b64: str) -> str:
        candidates = getattr(self, "_crypto_candidates", (self._crypto,))
        for crypto in candidates:
            try:
                plaintext = crypto.decrypt(
                    base64.b64decode(iv_b64),
                    base64.b64decode(ciphertext),
                    None,
                )
                return plaintext.decode()
            except Exception:
                continue
        raise SupabaseAuthError(
            "Your stored Supabase session is no longer valid. "
            "Please use /signin again before using this feature."
        )

    def _session_access_token(self, account: dict[str, Any]) -> str:
        if not self.is_authenticated(account):
            raise SupabaseAuthError("Please sign in before using this feature")
        return self._decrypt(
            str(account.get("session_token_ciphertext") or ""),
            str(account.get("session_token_iv") or ""),
        )

    @staticmethod
    def _access_token_needs_refresh(token: str) -> bool:
        """Return whether a JWT is expired or close enough to expiry to refresh."""
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
            expires_at = float(claims.get("exp"))
        except (IndexError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            # Test doubles and non-JWT tokens cannot be expiry-checked here.
            return False
        return expires_at <= time.time() + 60

    async def _refresh_session(self, account: dict[str, Any]) -> str:
        """Refresh an access token and rotate the encrypted session fields."""
        refresh_token = self._decrypt(
            str(account.get("refresh_token_ciphertext") or ""),
            str(account.get("refresh_token_iv") or ""),
        )
        payload = await self._request(
            "POST",
            "/auth/v1/token",
            params={"grant_type": "refresh_token"},
            body={"refresh_token": refresh_token},
        )
        access_token = str((payload or {}).get("access_token") or "")
        if not access_token:
            raise SupabaseAuthError(
                "Your Supabase session expired. Please use /signin again before using this feature."
            )
        refreshed_user_id = str(((payload or {}).get("user") or {}).get("id") or "")
        if refreshed_user_id and refreshed_user_id != str(account.get("agentx_user_id") or ""):
            raise SupabaseAuthError("Supabase returned a session for a different user")
        next_refresh_token = str((payload or {}).get("refresh_token") or refresh_token)
        enc_access, iv_access = self._encrypt(access_token)
        enc_refresh, iv_refresh = self._encrypt(next_refresh_token)
        telegram_user_id = account.get("telegram_user_id")
        if not telegram_user_id:
            raise SupabaseAuthError("Telegram account is missing a user identifier")
        await self._request(
            "PATCH",
            "/rest/v1/telegram_accounts",
            service=True,
            params={"telegram_user_id": f"eq.{int(telegram_user_id)}"},
            body={
                "session_token_ciphertext": enc_access,
                "session_token_iv": iv_access,
                "refresh_token_ciphertext": enc_refresh,
                "refresh_token_iv": iv_refresh,
                "updated_at": self._now(),
            },
        )
        account.update(
            {
                "session_token_ciphertext": enc_access,
                "session_token_iv": iv_access,
                "refresh_token_ciphertext": enc_refresh,
                "refresh_token_iv": iv_refresh,
            }
        )
        # Rotated tokens: drop the cached row so later turns re-read fresh state.
        self.invalidate_account_cache(int(telegram_user_id))
        return access_token

    async def session_is_usable(self, account: dict[str, Any]) -> bool:
        """Validate and refresh the stored session, revoking only unusable sessions."""
        if not self.is_authenticated(account):
            return False
        try:
            access_token = self._session_access_token(account)
            if self._access_token_needs_refresh(access_token):
                await self._refresh_session(account)
        except SupabaseAuthError:
            telegram_user_id = account.get("telegram_user_id")
            if telegram_user_id:
                await self._request(
                    "PATCH",
                    "/rest/v1/telegram_accounts",
                    service=True,
                    params={"telegram_user_id": f"eq.{int(telegram_user_id)}"},
                    body={
                        "session_token_ciphertext": None,
                        "session_token_iv": None,
                        "refresh_token_ciphertext": None,
                        "refresh_token_iv": None,
                        "updated_at": self._now(),
                    },
                )
            return False
        return True

    @staticmethod
    def payment_packages() -> tuple[dict[str, Any], ...]:
        """Return the same fixed USD credit packages enforced by pay-verify."""
        return (
            {"name": "Starter", "slug": "starter", "credits": 1000, "amount_usd": 1.50},
            {"name": "Standard", "slug": "standard", "credits": 2000, "amount_usd": 3.00},
            {"name": "Popular", "slug": "popular", "credits": 3500, "amount_usd": 5.00},
            {"name": "Best Value", "slug": "best_value", "credits": 7500, "amount_usd": 10.00},
        )

    @classmethod
    def payment_packages_text(cls, payment_url: str) -> str:
        lines = ["Credit packages (USD; purchased credits never expire):"]
        for package in cls.payment_packages():
            lines.append(
                f"• {package['name']}: {package['credits']} credits — ${package['amount_usd']:.2f}"
            )
        lines.extend([
            "",
            f"Pay here: {payment_url}",
            "After payment, send /verify-payment <Flutterwave transaction reference>.",
            "If automatic lookup cannot find it, send /verify-payment <reference> <transaction ID>.",
        ])
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Per-payment unique links (fixes Flutterwave Pages tx_ref collisions)
    # ------------------------------------------------------------------
    # A static Flutterwave *Payment Page* (https://flutterwave.com/pay/<slug>)
    # hands every payer the SAME auto-generated ``tx_ref`` (``Rave-Pages<id>``),
    # so two different users paying through one link collide on the idempotency
    # key in pay-verify: whoever verifies first permanently blocks the other.
    # The fix is to mint a UNIQUE tx_ref per payment. We embed the package
    # amount (in cents) into the ref using the format the Edge Function already
    # self-validates — ``txn_<unix_ts>_<rand>_<amountCents>`` — and create a
    # dedicated Flutterwave *Payment Link* for it via the v3 API. Each link is
    # single-use by construction, so refs can never be reused across payments.

    @staticmethod
    def _new_tx_ref(amount_usd: float) -> str:
        """Build a collision-free, self-describing transaction reference."""
        import secrets as _secrets
        import time as _time

        amount_cents = int(round(float(amount_usd) * 100))
        ts = int(_time.time())
        rand = _secrets.token_hex(4)
        return f"txn_{ts}_{rand}_{amount_cents}"

    async def create_payment_link(
        self, amount_usd: float, description: str = "AgentX credits", email: str | None = None,
    ) -> dict[str, Any]:
        """Create a unique single-use Flutterwave Payment Link for ``amount_usd``.

        Returns ``{"ok": True, "link": "<url>", "tx_ref": "<unique ref>"}`` or
        ``{"ok": False, "error": "..."}``. Requires the ``FLWS_SECRET_KEY`` env
        var (same secret pay-verify uses). Never raises for provider errors —
        callers get a structured result so the UI can fall back gracefully.
        """
        secret_key = os.getenv("FLWS_SECRET_KEY", "").strip()
        if not secret_key:
            return {"ok": False, "error": "Payments are not configured (missing FLWS_SECRET_KEY)."}
        try:
            amount_cents = int(round(float(amount_usd) * 100))
        except (TypeError, ValueError):
            return {"ok": False, "error": "Invalid payment amount."}
        if amount_cents <= 0:
            return {"ok": False, "error": "Invalid payment amount."}
        tx_ref = self._new_tx_ref(amount_usd)
        payload: dict[str, Any] = {
            "amount": round(amount_cents / 100, 2),
            "currency": "USD",
            "tx_ref": tx_ref,
            "title": description[:120] or "AgentX credits",
            "description": description[:200] or "Credit purchase",
            "meta": {"source": "nanobot", "tx_ref": tx_ref},
            "customizations": {"title": "AgentX", "content": description[:120] or "Buy credits"},
        }
        if email:
            payload["customer"] = {"email": email[:200]}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.flutterwave.com/v3/payment-links",
                    headers={"Authorization": f"Bearer {secret_key}"},
                    json=payload,
                )
            data = resp.json() if resp.status_code < 500 else {}
        except Exception as exc:  # network / parse failure → caller falls back
            return {"ok": False, "error": f"Could not create payment link: {exc}"}
        if isinstance(data, dict) and data.get("status") == "success":
            link = ((data.get("data") or {}).get("link")) or ""
            if link:
                return {"ok": True, "link": link, "tx_ref": tx_ref}
        return {
            "ok": False,
            "error": (data.get("message") if isinstance(data, dict) else None) or "Failed to create payment link.",
        }

    async def verify_payment(
        self, account: dict[str, Any], tx_ref: str, transaction_id: str | None = None,
    ) -> dict[str, Any]:
        """Verify a Flutterwave payment through the existing Supabase Edge Function."""
        tx_ref = tx_ref.strip()[:300]
        transaction_id = (transaction_id or "").strip()[:100]
        if not tx_ref:
            raise SupabaseAuthError("A Flutterwave transaction reference is required")
        body: dict[str, str] = {"tx_ref": tx_ref}
        if transaction_id:
            body["transaction_id"] = transaction_id
        payload = await self._request(
            "POST",
            "/functions/v1/pay-verify",
            access_token=self._session_access_token(account),
            body=body,
        )
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise SupabaseAuthError(str((payload or {}).get("error") or "Payment verification failed"))
        return payload

    async def _puter_http_request(
        self, gateway_key: str, body: dict[str, Any]
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=90.0, follow_redirects=False) as client:
            return await client.post(
                f"{self.url}/functions/v1/puter-admin",
                headers={
                    "apikey": self.anon_key,
                    "Authorization": f"Bearer {gateway_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )

    @staticmethod
    def _puter_session_rejected(response: httpx.Response, payload: Any) -> bool:
        if response.status_code == 401:
            return True
        detail = str(payload.get("error") or "") if isinstance(payload, dict) else ""
        detail = detail.lower()
        return "invalid user session" in detail or "invalid session" in detail or "jwt" in detail

    async def _puter_request(self, account: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        gateway_key = os.getenv("SUPABASE_GATEWAY_KEY", "").strip()
        if not gateway_key:
            raise SupabaseAuthError("Puter integration is not configured on the Telegram service")
        base_body = dict(body)
        try:
            access_token = self._session_access_token(account)
            if self._access_token_needs_refresh(access_token):
                access_token = await self._refresh_session(account)
            response = await self._puter_http_request(
                gateway_key, {**base_body, "user_jwt": access_token}
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise SupabaseAuthError("Puter returned invalid JSON") from exc
            if self._puter_session_rejected(response, payload):
                access_token = await self._refresh_session(account)
                response = await self._puter_http_request(
                    gateway_key, {**base_body, "user_jwt": access_token}
                )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise SupabaseAuthError("Puter returned invalid JSON") from exc
        except httpx.HTTPError as exc:
            raise SupabaseAuthError("Puter request failed") from exc
        if not response.is_success or not isinstance(payload, dict) or payload.get("ok") is not True:
            detail = payload.get("error") if isinstance(payload, dict) else None
            raise SupabaseAuthError(str(detail or f"Puter request failed with HTTP {response.status_code}")[:500])
        return payload

    async def puter_generate(
        self, account: dict[str, Any], action: str, prompt: str, *, model: str = "",
        seconds: int | None = None,
    ) -> dict[str, Any]:
        if action not in {"generate_image", "generate_video"}:
            raise SupabaseAuthError("Unsupported Puter generation action")
        prompt = prompt.strip()[:4000]
        if not prompt:
            raise SupabaseAuthError("A generation prompt is required")
        body: dict[str, Any] = {"action": action, "prompt": prompt}
        if model.strip():
            body["model"] = model.strip()[:200]
        if seconds is not None:
            body["seconds"] = max(4, min(12, int(seconds)))
        return await self._puter_request(account, body)

    async def puter_edit_image(
        self,
        account: dict[str, Any],
        prompt: str,
        input_images: list[str],
        *,
        model: str = "",
    ) -> dict[str, Any]:
        """Edit one to three image data URIs through the administrator Puter policy."""
        prompt = prompt.strip()[:4000]
        if not prompt:
            raise SupabaseAuthError("An image-edit instruction is required")
        bounded_images = [str(image).strip() for image in input_images[:3] if str(image).strip()]
        if not bounded_images:
            raise SupabaseAuthError("Attach an image to edit")
        if any(not image.startswith("data:image/") or "," not in image for image in bounded_images):
            raise SupabaseAuthError("The image attachment has an unsupported format")
        body: dict[str, Any] = {
            "action": "edit_image",
            "prompt": prompt,
            "input_images": bounded_images,
        }
        if model.strip():
            body["model"] = model.strip()[:200]
        return await self._puter_request(account, body)

    async def _puter_request_with_jwt(
        self, access_token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Call ``puter-admin`` as a specific signed-in user via their live JWT.

        This is the WebUI path. Unlike :meth:`_puter_request`, it does NOT rely
        on a stored Telegram session/refresh token — webui users have none, which
        is what produced "Invalid Refresh Token: Refresh Token Not Found". It
        sends the gateway key in the header and the caller's raw Supabase access
        JWT as ``user_jwt``, exactly matching how the admin Puter flow works.
        """
        gateway_key = os.getenv("SUPABASE_GATEWAY_KEY", "").strip()
        if not gateway_key:
            raise SupabaseAuthError("Puter integration is not configured")
        access_token = (access_token or "").strip()
        if not access_token:
            raise SupabaseAuthError(
                "You must be signed in to use image generation. Please sign in and try again."
            )
        try:
            response = await self._puter_http_request(
                gateway_key, {**body, "user_jwt": access_token}
            )
            payload = response.json()
        except httpx.HTTPError as exc:
            raise SupabaseAuthError("Puter request failed") from exc
        except ValueError as exc:
            raise SupabaseAuthError("Puter returned invalid JSON") from None
        if (
            not response.is_success
            or not isinstance(payload, dict)
            or payload.get("ok") is not True
        ):
            detail = payload.get("error") if isinstance(payload, dict) else None
            raise SupabaseAuthError(
                str(detail or f"Puter request failed with HTTP {response.status_code}")[:500]
            )
        return payload

    async def puter_generate_with_jwt(
        self, access_token: str, action: str, prompt: str, *, model: str = "",
        seconds: int | None = None,
    ) -> dict[str, Any]:
        if action not in {"generate_image", "generate_video"}:
            raise SupabaseAuthError("Unsupported Puter generation action")
        prompt = prompt.strip()[:4000]
        if not prompt:
            raise SupabaseAuthError("A generation prompt is required")
        body: dict[str, Any] = {"action": action, "prompt": prompt}
        if model.strip():
            body["model"] = model.strip()[:200]
        if seconds is not None:
            body["seconds"] = max(4, min(12, int(seconds)))
        return await self._puter_request_with_jwt(access_token, body)

    async def puter_edit_image_with_jwt(
        self, access_token: str, prompt: str, input_images: list[str], *, model: str = "",
    ) -> dict[str, Any]:
        prompt = prompt.strip()[:4000]
        if not prompt:
            raise SupabaseAuthError("An image-edit instruction is required")
        bounded_images = [str(image).strip() for image in input_images[:3] if str(image).strip()]
        if not bounded_images:
            raise SupabaseAuthError("Attach an image to edit")
        if any(not image.startswith("data:image/") or "," not in image for image in bounded_images):
            raise SupabaseAuthError("The image attachment has an unsupported format")
        body: dict[str, Any] = {
            "action": "edit_image",
            "prompt": prompt,
            "input_images": bounded_images,
        }
        if model.strip():
            body["model"] = model.strip()[:200]
        return await self._puter_request_with_jwt(access_token, body)

    # drain_rate is near-static user metadata; caching it removes one profiles
    # GET from every billed task. TTL keeps it honest if an admin changes it.
    _drain_rates: dict[str, tuple[int, float]] = {}
    _DRAIN_RATE_TTL = 300.0

    async def _cached_drain_rate(self, agentx_user_id: str) -> int:
        import time as _time

        entry = self._drain_rates.get(agentx_user_id)
        if entry and entry[1] > _time.time():
            return entry[0]
        rows = await self._request(
            "GET", "/rest/v1/profiles", service=True,
            params={"id": f"eq.{agentx_user_id}", "limit": "1", "select": "drain_rate"},
        )
        rate = max(1, int(rows[0].get("drain_rate") or 1)) if isinstance(rows, list) and rows else 1
        self._drain_rates[agentx_user_id] = (rate, _time.time() + self._DRAIN_RATE_TTL)
        return rate

    async def charge_step(self, account: dict[str, Any], task_ref: str, step_no: int, amount: int = 0) -> dict[str, Any]:
        if not account.get("agentx_user_id"):
            raise SupabaseAuthError("Use /signup or /signin first")
        if amount <= 0:
            rate = await self._cached_drain_rate(str(account["agentx_user_id"]))
            amount = 3 * rate
        result = await self._request("POST", "/rest/v1/rpc/consume_cloud_task_step_credits", service=True, body={"p_user": account["agentx_user_id"], "p_amount": max(1, int(amount)), "p_task_ref": task_ref, "p_step_no": max(1, int(step_no))})
        if not isinstance(result, dict) or result.get("success") is not True:
            raise SupabaseAuthError(str((result or {}).get("error") or "Insufficient credits for this step"))
        return result

    async def charge_task(self, account: dict[str, Any], task_ref: str, total_steps: int, amount: int = 0) -> dict[str, Any]:
        """Drain credits ONCE for an entire finished task (no per-step calls).

        Replaces the per-step ``charge_step`` flow. Reads the user's drain_rate
        a single time and issues ONE Supabase credit RPC for the whole task,
        budgeted as ``3 * rate * total_steps``. This keeps egress constant (2
        calls) no matter how many model iterations the task took.
        """
        if not account.get("agentx_user_id"):
            raise SupabaseAuthError("Use /signup or /signin first")
        if amount <= 0:
            rate = await self._cached_drain_rate(str(account["agentx_user_id"]))
            amount = 3 * rate
        total = max(1, int(total_steps))
        result = await self._request(
            "POST", "/rest/v1/rpc/consume_cloud_task_step_credits", service=True,
            body={
                "p_user": account["agentx_user_id"],
                "p_amount": max(1, int(amount)) * total,
                "p_task_ref": task_ref,
                "p_step_no": total,
            },
        )
        if not isinstance(result, dict) or result.get("success") is not True:
            raise SupabaseAuthError(str((result or {}).get("error") or "Insufficient credits for this task"))
        return result
