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
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
                response = await client.request(method, f"{self.url}{path}", headers=self._headers(service=service, access_token=access_token), params=params, json=body)
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

    async def account_for(self, telegram_user_id: int, chat_id: int, *, username: str | None, first_name: str | None, last_name: str | None) -> dict[str, Any]:
        rows = await self._request("GET", "/rest/v1/telegram_accounts", service=True, params={"telegram_user_id": f"eq.{telegram_user_id}", "limit": "1", "select": "*"})
        patch = {"chat_id": chat_id, "username": username, "first_name": first_name, "last_name": last_name, "last_seen_at": self._now(), "updated_at": self._now()}
        if isinstance(rows, list) and rows:
            result = await self._request("PATCH", "/rest/v1/telegram_accounts", service=True, params={"telegram_user_id": f"eq.{telegram_user_id}", "select": "*"}, body=patch)
            return result[0] if isinstance(result, list) and result else {**rows[0], **patch}
        result = await self._request("POST", "/rest/v1/telegram_accounts", service=True, params={"select": "*"}, body={"telegram_user_id": telegram_user_id, **patch})
        if isinstance(result, list) and result:
            return result[0]
        return {"telegram_user_id": telegram_user_id, **patch}

    async def refresh_account(self, telegram_user_id: int) -> dict[str, Any]:
        rows = await self._request("GET", "/rest/v1/telegram_accounts", service=True, params={"telegram_user_id": f"eq.{telegram_user_id}", "limit": "1", "select": "*"})
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

    async def charge_step(self, account: dict[str, Any], task_ref: str, step_no: int, amount: int = 0) -> dict[str, Any]:
        if not account.get("agentx_user_id"):
            raise SupabaseAuthError("Use /signup or /signin first")
        if amount <= 0:
            rows = await self._request("GET", "/rest/v1/profiles", service=True, params={"id": f"eq.{account['agentx_user_id']}", "limit": "1", "select": "drain_rate"})
            rate = max(1, int(rows[0].get("drain_rate") or 1)) if isinstance(rows, list) and rows else 1
            amount = 3 * rate
        result = await self._request("POST", "/rest/v1/rpc/consume_cloud_task_step_credits", service=True, body={"p_user": account["agentx_user_id"], "p_amount": max(1, int(amount)), "p_task_ref": task_ref, "p_step_no": max(1, int(step_no))})
        if not isinstance(result, dict) or result.get("success") is not True:
            raise SupabaseAuthError(str((result or {}).get("error") or "Insufficient credits for this step"))
        return result
