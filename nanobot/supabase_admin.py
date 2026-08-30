from __future__ import annotations

import os
import time
from typing import Any
from uuid import UUID

import httpx


class SupabaseAdminError(RuntimeError):
    pass


def _base_url() -> str:
    return os.getenv("SUPABASE_URL", "").strip().rstrip("/")


def _service_key() -> str:
    return os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()


def configured() -> bool:
    return bool(_base_url() and _service_key())


def _headers() -> dict[str, str]:
    key = _service_key()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, *, json: Any = None, params: dict[str, str] | None = None) -> Any:
    if not configured():
        raise SupabaseAdminError("Supabase admin integration is not configured")
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            response = client.request(
                method,
                f"{_base_url()}{path}",
                headers=_headers(),
                params=params,
                json=json,
            )
        if not response.is_success:
            raise SupabaseAdminError(f"Supabase request failed with HTTP {response.status_code}")
        if response.status_code == 204 or not response.content:
            return None
        return response.json()
    except httpx.HTTPError as exc:
        raise SupabaseAdminError("Supabase request failed") from exc
    except ValueError as exc:
        raise SupabaseAdminError("Supabase returned invalid JSON") from exc


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError):
        raise SupabaseAdminError("invalid user ID") from None


def users() -> list[dict[str, Any]]:
    profiles = _request(
        "GET",
        "/rest/v1/profiles",
        params={
            "select": "id,name,email,role,status,daily_credits,purchased_credits,granted_credits,blocked,drain_rate,questions_count,created_at,last_seen_at",
            "order": "created_at.desc",
            "limit": "500",
        },
    )
    rows = profiles if isinstance(profiles, list) else []
    telegram = _request(
        "GET",
        "/rest/v1/telegram_accounts",
        params={
            "select": "telegram_user_id,chat_id,agentx_user_id,username,first_name,last_name,last_seen_at,auth_email",
            "limit": "1000",
        },
    )
    by_user: dict[str, list[dict[str, Any]]] = {}
    for account in telegram if isinstance(telegram, list) else []:
        user_id = str(account.get("agentx_user_id") or "")
        if user_id:
            by_user.setdefault(user_id, []).append({
                "telegram_user_id": account.get("telegram_user_id"),
                "chat_id": account.get("chat_id"),
                "username": account.get("username"),
                "first_name": account.get("first_name"),
                "last_name": account.get("last_name"),
                "last_seen_at": account.get("last_seen_at"),
                "auth_email": account.get("auth_email"),
            })
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        enriched = dict(row)
        enriched["telegram_accounts"] = by_user.get(str(row.get("id")), [])
        enriched["total_credits"] = sum(
            max(0, int(row.get(field) or 0))
            for field in ("daily_credits", "purchased_credits", "granted_credits")
        )
        result.append(enriched)
    return result


def payment_claims() -> list[dict[str, Any]]:
    rows = _request(
        "GET",
        "/rest/v1/payment_claims",
        params={
            "select": "id,user_id,amount_usd,credits,tx_ref,flutterwave_transaction_id,status,currency,created_at,verified_at,credited_at",
            "order": "created_at.desc",
            "limit": "500",
        },
    )
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def telegram_question_history() -> list[dict[str, Any]]:
    rows = _request(
        "GET",
        "/rest/v1/telegram_question_history",
        params={
            "select": "id,telegram_user_id,chat_id,telegram_message_id,question,task_id,has_attachment,created_at",
            "order": "created_at.desc",
            "limit": "500",
        },
    )
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _profile(user_id: str) -> dict[str, Any]:
    rows = _request(
        "GET",
        "/rest/v1/profiles",
        params={"select": "id,granted_credits", "id": f"eq.{user_id}", "limit": "1"},
    )
    if not isinstance(rows, list) or not rows:
        raise SupabaseAdminError("user profile not found")
    return rows[0]


def user_action(action: str, user_id: Any, *, amount: Any = 0, blocked: Any = False) -> dict[str, Any]:
    user = _uuid(user_id)
    if action == "grant":
        try:
            credits = int(amount)
        except (TypeError, ValueError):
            raise SupabaseAdminError("credit amount must be an integer") from None
        if credits <= 0 or credits > 1_000_000:
            raise SupabaseAdminError("credit amount must be between 1 and 1,000,000")
        current = int(_profile(user).get("granted_credits") or 0)
        next_value = current + credits
        _request("PATCH", "/rest/v1/profiles", params={"id": f"eq.{user}"}, json={"granted_credits": next_value})
        _request(
            "POST",
            "/rest/v1/credit_ledger",
            json={
                "user_id": user,
                "delta": credits,
                "bucket": "admin",
                "reason": "admin_grant",
            },
        )
        return {"ok": True, "action": action, "user_id": user, "granted_credits": next_value}
    if action == "block":
        is_blocked = bool(blocked)
        _request(
            "PATCH",
            "/rest/v1/profiles",
            params={"id": f"eq.{user}"},
            json={"status": "blocked" if is_blocked else "active", "blocked": 1 if is_blocked else 0},
        )
        return {"ok": True, "action": action, "user_id": user, "blocked": is_blocked}
    if action == "delete":
        result = _request("POST", "/rest/v1/rpc/admin_delete_user", json={"target_user": user})
        return {"ok": bool(result is True or result is None or result), "action": action, "user_id": user}
    raise SupabaseAdminError("unknown user action")


_TELEGRAM_MESSAGE_MAX = 4096
_ANNOUNCEMENT_MAX_CHARS = 3800


def _announcement_chat_ids() -> list[int]:
    rows = _request(
        "GET",
        "/rest/v1/telegram_accounts",
        params={
            "select": "chat_id",
            "chat_id": "not.is.null",
            "limit": "5000",
        },
    )
    chat_ids: set[int] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            chat_id = int(row.get("chat_id"))
        except (TypeError, ValueError):
            continue
        chat_ids.add(chat_id)
    return sorted(chat_ids)


def _telegram_announcement_text(title: str, message: str) -> str:
    text = f"{title}\n\n{message}".strip()
    return text[:_TELEGRAM_MESSAGE_MAX]


def _send_telegram_announcement(token: str, chat_id: int, text: str) -> bool:
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            response = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
        if not response.is_success:
            return False
        payload = response.json()
        return isinstance(payload, dict) and payload.get("ok") is True
    except (httpx.HTTPError, ValueError):
        return False


def broadcast_announcement(title: Any, message: Any) -> dict[str, Any]:
    """Persist and broadcast one bounded plain-text announcement.

    Recipients are deduplicated by chat ID. Each chat is attempted once per
    admin action; ambiguous network failures are not retried to avoid duplicate
    Telegram messages.
    """
    safe_title = str(title or "Announcement").strip()[:200] or "Announcement"
    safe_message = str(message or "").strip()[:_ANNOUNCEMENT_MAX_CHARS]
    if not safe_message:
        raise SupabaseAdminError("announcement message is required")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SupabaseAdminError("Telegram broadcast is not configured")

    body: dict[str, Any] = {
        "title": safe_title,
        "message": safe_message,
        "is_active": True,
    }
    admin_id = os.getenv("ADMIN_SUPABASE_USER_ID", "").strip()
    if admin_id:
        body["created_by"] = _uuid(admin_id)
    _request("POST", "/rest/v1/announcements", json=body)

    text = _telegram_announcement_text(safe_title, safe_message)
    recipients = _announcement_chat_ids()
    sent = sum(_send_telegram_announcement(token, chat_id, text) for chat_id in recipients)
    failed = len(recipients) - sent
    return {
        "ok": True,
        "action": "announcement",
        "total": len(recipients),
        "sent": sent,
        "failed": failed,
        "completed_at": time.time(),
    }


def announcement(title: Any, message: Any) -> dict[str, Any]:
    """Backward-compatible alias for the admin broadcast operation."""
    return broadcast_announcement(title, message)
