from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any
from uuid import UUID

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

_BASE_PATH = "/rest/v1"
_MAX_ROWS = 20
_MAX_QUESTION_CHARS = 4_000
_MAX_RESULT_CHARS = 16_000
_TOKEN_RE = re.compile(r"(?i)\b(?:sk|gsk|ghp|xoxb|xoxp|sbp|sb_secret)-[A-Za-z0-9_-]{8,}\b")


class UniAbujaStudentError(RuntimeError):
    pass


def _base_url() -> str:
    return os.getenv("SUPABASE_URL", "").strip().rstrip("/")


def _service_key() -> str:
    return os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()


def _configured() -> bool:
    return bool(_base_url() and _service_key())


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[redacted]"
            if re.search(r"(?i)(token|secret|password|credential|authorization|private.?key|session)", str(key))
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value[:_MAX_ROWS]]
    if isinstance(value, str):
        return _TOKEN_RE.sub("[redacted-token]", value)[:_MAX_RESULT_CHARS]
    return value


def _request(method: str, path: str, *, params: dict[str, str] | None = None, body: Any = None) -> Any:
    if not _configured():
        raise UniAbujaStudentError("UniAbuja database integration is not configured")
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            response = client.request(
                method,
                f"{_base_url()}{path}",
                headers={
                    "apikey": _service_key(),
                    "Authorization": f"Bearer {_service_key()}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                params=params,
                json=body,
            )
    except httpx.HTTPError as exc:
        raise UniAbujaStudentError("UniAbuja database request failed") from exc
    if not response.is_success:
        raise UniAbujaStudentError(f"UniAbuja database request failed with HTTP {response.status_code}")
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise UniAbujaStudentError("UniAbuja database returned invalid JSON") from exc


def _telegram_numeric_sender_id(ctx: Any) -> int:
    raw = str(ctx.sender_id or "").strip()
    candidate = raw.split("|", 1)[0].strip()
    if not candidate.isdigit():
        metadata_id = str(ctx.metadata.get("user_id") or "").strip()
        candidate = metadata_id.split("|", 1)[0].strip()
    if not candidate.isdigit():
        raise UniAbujaStudentError("Telegram sender identity is unavailable")
    return int(candidate)


def _student_identity() -> tuple[Any, str]:
    ctx = current_request_context()
    if ctx is None or ctx.channel != "telegram":
        raise UniAbujaStudentError("UniAbuja student access is available only from an authenticated Telegram account")
    raw_user_id = str(ctx.metadata.get("supabase_user_id") or "").strip()
    sender_id = _telegram_numeric_sender_id(ctx)
    if not raw_user_id:
        raise UniAbujaStudentError("Sign in to your linked AgentX account before using UniAbuja access")
    try:
        UUID(raw_user_id)
    except (ValueError, AttributeError):
        raise UniAbujaStudentError("The linked Supabase identity is invalid") from None
    rows = _request(
        "GET",
        f"{_BASE_PATH}/telegram_accounts",
        params={
            "select": "telegram_user_id,agentx_user_id,auth_email",
            "telegram_user_id": f"eq.{sender_id}",
            "limit": "1",
        },
    )
    row = rows[0] if isinstance(rows, list) and rows else None
    if not isinstance(row, dict) or str(row.get("agentx_user_id") or "") != raw_user_id:
        raise UniAbujaStudentError("The Telegram account is not linked to the active Supabase identity")
    if not str(row.get("auth_email") or "").strip():
        raise UniAbujaStudentError("Complete Supabase sign-in before using UniAbuja access")
    return ctx, raw_user_id


def _status() -> dict[str, Any]:
    result = _request("POST", f"{_BASE_PATH}/rpc/uniabuja_get_access_status", body={})
    if not isinstance(result, dict):
        raise UniAbujaStudentError("UniAbuja access status has an unexpected shape")
    return {
        "read_access": bool(result.get("read_access")),
        "write_access": bool(result.get("write_access")),
        "transcript_access": bool(result.get("transcript_access")),
    }


def _bounded(value: Any) -> str:
    text = json.dumps(_redact(value), ensure_ascii=False, default=str)
    return text if len(text) <= _MAX_RESULT_CHARS else text[:_MAX_RESULT_CHARS] + "…"


def _audit(ctx: Any, user_id: str, action: str, resource: str = "") -> None:
    metadata: dict[str, Any] = {
        "channel": "telegram",
        "chat_id": str(ctx.chat_id),
        "student_access": True,
    }
    if resource:
        metadata["resource"] = resource
    try:
        _request(
            "POST",
            f"{_BASE_PATH}/admin_audit_log",
            body={
                "admin_id": user_id,
                "action": f"telegram_uniabuja_student_{action}"[:200],
                "metadata": metadata,
            },
        )
    except Exception as exc:
        # The existing table is admin-oriented; student activity is already
        # recorded in user_questions/telegram_question_history. Do not fail a
        # permitted read because optional audit compatibility is unavailable.
        logger.debug("Optional UniAbuja student audit unavailable: {}", type(exc).__name__)


def _query(resource: str, user_id: str) -> Any:
    if resource == "knowledge":
        return _request(
            "GET",
            f"{_BASE_PATH}/training_data",
            params={
                "select": "id,title,content,created_at,updated_at",
                "is_sensitive": "eq.false",
                "order": "updated_at.desc",
                "limit": str(_MAX_ROWS),
            },
        )
    if resource == "announcements":
        return _request(
            "GET",
            f"{_BASE_PATH}/announcements",
            params={
                "select": "id,title,message,created_at,updated_at",
                "is_active": "eq.true",
                "order": "created_at.desc",
                "limit": str(_MAX_ROWS),
            },
        )
    if resource == "my_questions":
        return _request(
            "GET",
            f"{_BASE_PATH}/user_questions",
            params={
                "select": "id,user_id,title,message,category,created_by,created_at,updated_at",
                "user_id": f"eq.{user_id}",
                "order": "created_at.desc",
                "limit": str(_MAX_ROWS),
            },
        )
    raise UniAbujaStudentError("resource must be knowledge, announcements, or my_questions")


@tool_parameters(
    tool_parameters_schema(
        required=["action"],
        action=StringSchema(
            "Student UniAbuja operation",
            enum=["status", "query", "submit_question", "transcript"],
        ),
        resource=StringSchema(
            "Safe resource for query",
            enum=["knowledge", "announcements", "my_questions"],
        ),
        question=StringSchema("Question to store when write access is enabled"),
    )
)
class UniAbujaStudentTool(Tool):
    """Authenticated student-facing UniAbuja access controlled by database switches."""

    _scopes = {"core"}
    config_key = "uniabuja_student"

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return _configured()

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls()

    @property
    def name(self) -> str:
        return "uniabuja_student"

    @property
    def description(self) -> str:
        return (
            "Use for an authenticated Telegram student or ordinary user only. "
            "The UniAbuja database administrator’s global read, write, and transcript switches "
            "are checked on every call. Query only public non-sensitive knowledge, active announcements, "
            "or the current user’s own questions; submit only the current user’s question when write access "
            "is enabled; and retrieve only the current user’s transcript when transcript access is enabled. "
            "Never query secret, credential, payment, session, or arbitrary tables."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "query", "submit_question", "transcript"]},
                "resource": {"type": "string", "enum": ["knowledge", "announcements", "my_questions"]},
                "question": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> ToolResult | str:
        try:
            ctx, user_id = await asyncio.to_thread(_student_identity)
            action = str(kwargs.get("action") or "").strip().lower()
            if action == "status":
                status = await asyncio.to_thread(_status)
                await asyncio.to_thread(_audit, ctx, user_id, action)
                return _bounded({"ok": True, "action": action, **status})
            status = await asyncio.to_thread(_status)
            if action == "query":
                if not status["read_access"]:
                    raise UniAbujaStudentError("UniAbuja student read access is disabled by the administrator")
                resource = str(kwargs.get("resource") or "").strip().lower()
                rows = await asyncio.to_thread(_query, resource, user_id)
                await asyncio.to_thread(_audit, ctx, user_id, action, resource)
                return _bounded({"ok": True, "action": action, "resource": resource, "rows": rows})
            if action == "submit_question":
                if not status["write_access"]:
                    raise UniAbujaStudentError("UniAbuja student write access is disabled by the administrator")
                question = str(kwargs.get("question") or "").strip()
                if not question or len(question) > _MAX_QUESTION_CHARS:
                    raise UniAbujaStudentError("question is required and must be at most 4,000 characters")
                result = await asyncio.to_thread(
                    _request,
                    "POST",
                    f"{_BASE_PATH}/user_questions",
                    params={"select": "id,user_id,title,message,category,created_by,created_at,updated_at"},
                    body={
                        "user_id": user_id,
                        "title": "Telegram UniAbuja question",
                        "message": question,
                        "category": "telegram",
                        "created_by": user_id,
                    },
                )
                await asyncio.to_thread(_audit, ctx, user_id, action)
                return _bounded({"ok": True, "action": action, "result": result})
            if action == "transcript":
                if not status["transcript_access"]:
                    raise UniAbujaStudentError("UniAbuja student transcript access is disabled by the administrator")
                rows = await asyncio.to_thread(
                    _request,
                    "GET",
                    f"{_BASE_PATH}/telegram_question_history",
                    params={
                        "select": "id,telegram_user_id,telegram_message_id,question,task_id,has_attachment,created_at",
                        "telegram_user_id": f"eq.{_telegram_numeric_sender_id(ctx)}",
                        "order": "created_at.desc",
                        "limit": str(_MAX_ROWS),
                    },
                )
                await asyncio.to_thread(_audit, ctx, user_id, action)
                return _bounded({"ok": True, "action": action, "rows": rows})
            raise UniAbujaStudentError("Unknown UniAbuja student action")
        except UniAbujaStudentError as exc:
            return ToolResult.error(f"UniAbuja student access unavailable: {exc}")
        except Exception as exc:
            logger.exception("UniAbuja student operation failed")
            return ToolResult.error(f"UniAbuja student error: {type(exc).__name__}")
