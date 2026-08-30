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
from nanobot.agent.tools.schema import (
    StringSchema,
    tool_parameters_schema,
)
from nanobot.runtime_context import runtime_context_blocks_from_metadata

_ADMIN_EMAIL = "allisonarinze@gmail.com"
_BASE_PATH = "/rest/v1"
_MAX_RESULT_CHARS = 16_000
_MAX_ROWS = 100
_MAX_CONTENT_CHARS = 20_000
_MAX_FILTER_CHARS = 200
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization|private[_-]?key|credential|session)"
)
_TOKEN_RE = re.compile(r"(?i)\b(?:sk|gsk|ghp|xoxb|xoxp|sbp|sb_secret)-[A-Za-z0-9_-]{8,}\b")

# These are intentionally explicit. Secret-bearing and credential tables are never
# available through this tool, even to the verified administrator.
_READ_COLUMNS: dict[str, tuple[str, ...]] = {
    "uniabuja_ai_access_controls": (
        "id",
        "ai_read_access",
        "ai_write_access",
        "ai_transcript_access",
        "remote_exec_access",
        "updated_by",
        "updated_at",
    ),
    "admin_agent_policy_memory": (
        "id",
        "admin_id",
        "scope_type",
        "scope_key",
        "title",
        "instruction",
        "version",
        "is_active",
        "metadata",
        "created_at",
        "updated_at",
    ),
    "admin_audit_log": (
        "id",
        "admin_id",
        "action",
        "target_user_id",
        "metadata",
        "created_at",
    ),
    "training_data": (
        "id",
        "user_id",
        "title",
        "content",
        "is_sensitive",
        "created_at",
        "updated_at",
    ),
    "user_questions": (
        "id",
        "user_id",
        "question",
        "created_at",
    ),
    "telegram_question_history": (
        "id",
        "telegram_user_id",
        "chat_id",
        "telegram_message_id",
        "question",
        "task_id",
        "has_attachment",
        "created_at",
    ),
    "profiles": (
        "id",
        "name",
        "email",
        "role",
        "status",
        "daily_credits",
        "purchased_credits",
        "granted_credits",
        "blocked",
        "drain_rate",
        "questions_count",
        "created_at",
        "last_seen_at",
    ),
    "telegram_accounts": (
        "telegram_user_id",
        "chat_id",
        "agentx_user_id",
        "username",
        "first_name",
        "last_name",
        "last_seen_at",
        "auth_email",
    ),
    "payment_claims": (
        "id",
        "user_id",
        "amount_usd",
        "credits",
        "tx_ref",
        "flutterwave_transaction_id",
        "status",
        "currency",
        "created_at",
        "verified_at",
        "credited_at",
    ),
    "announcements": (
        "id",
        "title",
        "message",
        "is_active",
        "created_by",
        "created_at",
        "updated_at",
    ),
    # Values are intentionally excluded because this table can contain provider
    # configuration and other sensitive application settings.
    "system_settings": ("key", "updated_by", "updated_at"),
}

_WRITE_COLUMNS: dict[str, frozenset[str]] = {
    "admin_agent_policy_memory": frozenset(
        {"scope_type", "scope_key", "title", "instruction", "version", "is_active", "metadata"}
    ),
    "training_data": frozenset({"title", "content", "is_sensitive"}),
}


class UniAbujaAdminError(RuntimeError):
    pass


def _base_url() -> str:
    return os.getenv("SUPABASE_URL", "").strip().rstrip("/")


def _service_key() -> str:
    return os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()


def _configured() -> bool:
    return bool(_base_url() and _service_key())


def _redact(value: Any, *, key: str = "") -> Any:
    if _SENSITIVE_KEY_RE.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value[:_MAX_ROWS]]
    if isinstance(value, str):
        value = _TOKEN_RE.sub("[redacted-token]", value)
        return value[:_MAX_CONTENT_CHARS]
    return value


def _bounded_json(value: Any) -> str:
    text = json.dumps(_redact(value), ensure_ascii=False, default=str)
    if len(text) <= _MAX_RESULT_CHARS:
        return text
    return text[:_MAX_RESULT_CHARS] + "…"


def _request(method: str, path: str, *, params: dict[str, str] | None = None, body: Any = None) -> Any:
    if not _configured():
        raise UniAbujaAdminError("UniAbuja database integration is not configured")
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
        raise UniAbujaAdminError("UniAbuja database request failed") from exc
    if not response.is_success:
        raise UniAbujaAdminError(f"UniAbuja database request failed with HTTP {response.status_code}")
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise UniAbujaAdminError("UniAbuja database returned invalid JSON") from exc


def _telegram_numeric_sender_id(ctx: Any) -> int:
    raw = str(ctx.sender_id or "").strip()
    candidate = raw.split("|", 1)[0].strip()
    if not candidate.isdigit():
        metadata_id = str(ctx.metadata.get("user_id") or "").strip()
        candidate = metadata_id.split("|", 1)[0].strip()
    if not candidate.isdigit():
        raise UniAbujaAdminError("Verified Telegram sender identity is unavailable")
    return int(candidate)


def _verified_admin_context() -> tuple[Any, str]:
    ctx = current_request_context()
    if ctx is None or ctx.channel != "telegram":
        raise UniAbujaAdminError("This database capability is available only to the verified Telegram administrator")
    blocks = runtime_context_blocks_from_metadata(ctx.metadata)
    verified = any(
        block.source == "telegram_verified_admin"
        and f"Verified administrator account: {_ADMIN_EMAIL}" in block.content
        for block in blocks
    )
    if not verified:
        raise UniAbujaAdminError("Verified administrator authorization is required")
    sender_id = _telegram_numeric_sender_id(ctx)
    rows = _request(
        "GET",
        f"{_BASE_PATH}/telegram_accounts",
        params={
            "select": "agentx_user_id,auth_email",
            "telegram_user_id": f"eq.{sender_id}",
            "limit": "1",
        },
    )
    row = rows[0] if isinstance(rows, list) and rows else None
    if not isinstance(row, dict):
        raise UniAbujaAdminError("Verified administrator account is not linked in Supabase")
    email = str(row.get("auth_email") or "").strip().lower()
    admin_id = str(row.get("agentx_user_id") or "").strip()
    try:
        UUID(admin_id)
    except (ValueError, AttributeError):
        raise UniAbujaAdminError("Verified administrator account has no valid Supabase user ID") from None
    if email != _ADMIN_EMAIL:
        raise UniAbujaAdminError("Verified administrator email does not match the protected administrator account")
    return ctx, admin_id


def _access_status() -> dict[str, Any]:
    result = _request("POST", f"{_BASE_PATH}/rpc/uniabuja_get_access_status", body={})
    if not isinstance(result, dict):
        raise UniAbujaAdminError("UniAbuja access status has an unexpected shape")
    return {
        "read_access": bool(result.get("read_access")),
        "write_access": bool(result.get("write_access")),
        "transcript_access": bool(result.get("transcript_access")),
        "is_admin": bool(result.get("is_admin")),
        "updated_at": result.get("updated_at"),
    }


def _audit(ctx: Any, admin_id: str, action: str, *, table: str = "") -> None:
    metadata: dict[str, Any] = {"channel": "telegram", "chat_id": str(ctx.chat_id)}
    if table:
        metadata["table"] = table
    try:
        _request(
            "POST",
            f"{_BASE_PATH}/admin_audit_log",
            body={
                "admin_id": admin_id,
                "action": f"telegram_uniabuja_{action}"[:200],
                "metadata": metadata,
            },
        )
    except Exception as exc:  # audit failure must not leak or undo a completed read
        logger.warning("Could not write UniAbuja admin audit record: {}", type(exc).__name__)


def _validate_table(table: str, *, writable: bool = False) -> str:
    value = table.strip()
    if value not in _READ_COLUMNS:
        raise UniAbujaAdminError("Table is not in the protected UniAbuja allowlist")
    if writable and value not in _WRITE_COLUMNS:
        raise UniAbujaAdminError("That table is read-only through Telegram")
    return value


def _columns(table: str, raw: str) -> str:
    allowed = set(_READ_COLUMNS[table])
    requested = [item.strip() for item in raw.split(",") if item.strip()] if raw.strip() else list(_READ_COLUMNS[table])
    if not requested or any(item not in allowed for item in requested):
        raise UniAbujaAdminError("Requested columns are not allowed for this table")
    return ",".join(dict.fromkeys(requested))


def _filters(table: str, raw: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    allowed = set(_READ_COLUMNS[table])
    for key, value in raw.items():
        if key not in allowed:
            raise UniAbujaAdminError(f"Filter column is not allowed for {table}: {key}")
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value)
        else:
            raise UniAbujaAdminError("Filters must use simple string, number, or boolean equality values")
        if not text or len(text) > _MAX_FILTER_CHARS or any(ch in text for ch in "\r\n,"):
            raise UniAbujaAdminError("Filter value is invalid or too long")
        result[key] = f"eq.{text}"
    return result


def _write_record(table: str, mode: str, record: dict[str, Any], record_id: str, admin_id: str) -> Any:
    allowed = _WRITE_COLUMNS[table]
    if not record or any(key not in allowed for key in record):
        raise UniAbujaAdminError("Write contains fields outside the table’s safe allowlist")
    clean: dict[str, Any] = {}
    for key, value in record.items():
        if key in {"title", "instruction", "scope_type", "scope_key"}:
            if not isinstance(value, str) or not value.strip() or len(value) > 5000:
                raise UniAbujaAdminError(f"{key} must be a non-empty string of at most 5,000 characters")
            clean[key] = value.strip()
        elif key == "content":
            if not isinstance(value, str) or len(value) > _MAX_CONTENT_CHARS:
                raise UniAbujaAdminError("content must be text of at most 20,000 characters")
            clean[key] = value
        elif key in {"version"}:
            if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 1_000_000:
                raise UniAbujaAdminError("version must be an integer between 1 and 1,000,000")
            clean[key] = value
        elif key in {"is_active", "is_sensitive"}:
            if not isinstance(value, bool):
                raise UniAbujaAdminError(f"{key} must be boolean")
            clean[key] = value
        elif key == "metadata":
            if not isinstance(value, dict) or len(value) > 40:
                raise UniAbujaAdminError("metadata must be an object with at most 40 keys")
            clean[key] = _redact(value)
        else:
            raise UniAbujaAdminError(f"Unsupported write field: {key}")
    if table == "admin_agent_policy_memory":
        clean["admin_id"] = admin_id
    else:
        clean["user_id"] = admin_id
    if mode == "insert":
        return _request("POST", f"{_BASE_PATH}/{table}", params={"select": ",".join(_READ_COLUMNS[table])}, body=clean)
    if mode == "update":
        try:
            UUID(record_id)
        except (ValueError, AttributeError):
            raise UniAbujaAdminError("update requires a valid record UUID") from None
        return _request(
            "PATCH",
            f"{_BASE_PATH}/{table}",
            params={"id": f"eq.{record_id}", "select": ",".join(_READ_COLUMNS[table])},
            body=clean,
        )
    raise UniAbujaAdminError("mode must be insert or update")


@tool_parameters(
    tool_parameters_schema(
        required=["action"],
        action=StringSchema(
            "UniAbuja administrator operation",
            enum=["access_status", "list_tables", "read", "write", "set_access"],
        ),
        table=StringSchema("Allowlisted non-secret table name"),
        columns=StringSchema("Comma-separated allowlisted columns for a read"),
        filters=StringSchema("JSON object of simple equality filters"),
        mode=StringSchema("Write mode", enum=["insert", "update"]),
        record=StringSchema("JSON object containing allowlisted write fields"),
        record_id=StringSchema("UUID of an existing record for update"),
        read_access=StringSchema("Whether AI read access should be enabled"),
        write_access=StringSchema("Whether AI write access should be enabled"),
        transcript_access=StringSchema("Whether transcript access should be enabled"),
        remote_exec_access=StringSchema("Must remain false; remote transcript-server execution is not exposed"),
    )
)
class UniAbujaAdminTool(Tool):
    """Verified-administrator-only, bounded access to the UniAbuja Supabase controls."""

    _scopes = {"core"}
    config_key = "uniabuja_admin"

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return _configured()

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls()

    @property
    def name(self) -> str:
        return "uniabuja_admin"

    @property
    def description(self) -> str:
        return (
            "Use only when the current Telegram turn has trusted verified-administrator context. "
            "Inspect the UniAbuja Supabase access status, read allowlisted non-secret tables, "
            "write only approved policy/training tables when database write access is enabled, "
            "or change the UniAbuja read/write/transcript switches. Secret and credential tables "
            "are never exposed. For shell work use the existing isolated novita_sandbox tool; "
            "this tool never executes shell commands on Render, Supabase, or a database host."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["access_status", "list_tables", "read", "write", "set_access"]},
                "table": {"type": "string"},
                "columns": {"type": "string"},
                "filters": {"type": "string"},
                "mode": {"type": "string", "enum": ["insert", "update"]},
                "record": {"type": "string"},
                "record_id": {"type": "string"},
                "read_access": {"type": "string", "enum": ["true", "false"]},
                "write_access": {"type": "string", "enum": ["true", "false"]},
                "transcript_access": {"type": "string", "enum": ["true", "false"]},
                "remote_exec_access": {"type": "string", "enum": ["false"]},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @staticmethod
    def _bool_arg(value: Any, name: str) -> bool:
        text = str(value or "").strip().lower()
        if text not in {"true", "false"}:
            raise UniAbujaAdminError(f"{name} must be true or false")
        return text == "true"

    async def execute(self, **kwargs: Any) -> ToolResult | str:
        try:
            ctx, admin_id = await asyncio.to_thread(_verified_admin_context)
            action = str(kwargs.get("action") or "").strip().lower()
            if action == "access_status":
                result = {"ok": True, "action": action, **await asyncio.to_thread(_access_status)}
                await asyncio.to_thread(_audit, ctx, admin_id, action)
                return _bounded_json(result)
            if action == "list_tables":
                result = {
                    "ok": True,
                    "action": action,
                    "tables": {table: list(columns) for table, columns in _READ_COLUMNS.items()},
                    "write_tables": sorted(_WRITE_COLUMNS),
                    "shell": "Use novita_sandbox; no host/database shell is exposed.",
                }
                await asyncio.to_thread(_audit, ctx, admin_id, action)
                return _bounded_json(result)
            status = await asyncio.to_thread(_access_status)
            if action == "read":
                if not status["read_access"]:
                    raise UniAbujaAdminError("UniAbuja AI read access is disabled by the database control")
                table = _validate_table(str(kwargs.get("table") or ""))
                raw_filters = kwargs.get("filters") or "{}"
                try:
                    filters = json.loads(str(raw_filters))
                except json.JSONDecodeError:
                    raise UniAbujaAdminError("filters must be a JSON object") from None
                if not isinstance(filters, dict):
                    raise UniAbujaAdminError("filters must be a JSON object")
                params = {"select": _columns(table, str(kwargs.get("columns") or "")), "limit": str(_MAX_ROWS)}
                for key, value in _filters(table, filters).items():
                    params[key] = value
                rows = await asyncio.to_thread(_request, "GET", f"{_BASE_PATH}/{table}", params=params)
                result = {"ok": True, "action": action, "table": table, "rows": _redact(rows)}
                await asyncio.to_thread(_audit, ctx, admin_id, action, table=table)
                return _bounded_json(result)
            if action == "write":
                if not status["write_access"]:
                    raise UniAbujaAdminError("UniAbuja AI write access is disabled by the database control")
                table = _validate_table(str(kwargs.get("table") or ""), writable=True)
                try:
                    record = json.loads(str(kwargs.get("record") or ""))
                except json.JSONDecodeError:
                    raise UniAbujaAdminError("record must be a JSON object") from None
                if not isinstance(record, dict):
                    raise UniAbujaAdminError("record must be a JSON object")
                result = await asyncio.to_thread(
                    _write_record,
                    table,
                    str(kwargs.get("mode") or "").strip().lower(),
                    record,
                    str(kwargs.get("record_id") or "").strip(),
                    admin_id,
                )
                await asyncio.to_thread(_audit, ctx, admin_id, action, table=table)
                return _bounded_json({"ok": True, "action": action, "table": table, "result": result})
            if action == "set_access":
                read_access = self._bool_arg(kwargs.get("read_access"), "read_access")
                write_access = self._bool_arg(kwargs.get("write_access"), "write_access")
                transcript_access = self._bool_arg(kwargs.get("transcript_access"), "transcript_access")
                remote_exec = str(kwargs.get("remote_exec_access") or "false").strip().lower()
                if remote_exec != "false":
                    raise UniAbujaAdminError("Remote transcript-server execution cannot be enabled through Telegram")
                result = await asyncio.to_thread(
                    _request,
                    "POST",
                    f"{_BASE_PATH}/rpc/uniabuja_set_access_status",
                    body={
                        "p_read": read_access,
                        "p_write": write_access,
                        "p_transcript": transcript_access,
                        "p_remote_exec": False,
                    },
                )
                await asyncio.to_thread(_audit, ctx, admin_id, action)
                return _bounded_json({"ok": True, "action": action, "result": result, "remote_exec_access": False})
            raise UniAbujaAdminError("Unknown UniAbuja administrator action")
        except UniAbujaAdminError as exc:
            return ToolResult.error(f"UniAbuja admin access denied: {exc}")
        except Exception as exc:
            logger.exception("UniAbuja administrator operation failed")
            return ToolResult.error(f"UniAbuja administrator error: {type(exc).__name__}")
