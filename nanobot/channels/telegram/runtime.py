"""Telegram channel implementation using python-telegram-bot."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import time
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, TypeAlias, TypeVar, cast
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import Field, field_validator, model_validator
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    ReactionTypeEmoji,
    ReplyParameters,
    Update,
    User,
    WebAppInfo,
)
from telegram.error import BadRequest, InvalidToken, NetworkError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.request import BaseRequest, HTTPXRequest

from nanobot.admin_registry import record_telegram_user
from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import ProgressEvent
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.telegram.task_mode import deliberate_task_mode
from nanobot.channels.telegram.task_mode import task_mode_callback
from nanobot.config.schema import TelegramConfig as TelegramConfigSchema
from nanobot.security.network import validate_url_target
from nanobot.supabase_auth import SupabaseAuth, SupabaseAuthError
from nanobot.api.api_keys import ApiKeyStore
from nanobot.channels.telegram.api_platform import handle_api_command
from nanobot.trading.alpaca_commands import is_alpaca_command, handle_alpaca_command
from nanobot.utils.gofile import upload_gofile_stream
from nanobot.utils.helpers import detect_image_mime, split_message
from nanobot.utils.logging_bridge import redirect_lib_logging

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nanobot.agent.tools.context import RequestContext, ToolContext
    from nanobot.runtime_context import RuntimeContextProvider

_JSON_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


class Schema(ABC):
    @staticmethod
    def resolve_json_schema_type(t: Any) -> str | None:
        if isinstance(t, list):
            types = cast(list[Any], t)
            return cast(str | None, next((x for x in types if x != "null"), None))
        return cast(str | None, t)

    @staticmethod
    def subpath(path: str, key: str) -> str:
        return f"{path}.{key}" if path else key

    @staticmethod
    def validate_json_schema_value(val: Any, schema: dict[str, Any], path: str = "") -> list[str]:
        raw_type = schema.get("type")
        nullable = (isinstance(raw_type, list) and "null" in raw_type) or schema.get("nullable", False)
        t = Schema.resolve_json_schema_type(raw_type)
        label = path or "parameter"
        if nullable and val is None:
            return []
        if t == "integer" and (not isinstance(val, int) or isinstance(val, bool)):
            return [f"{label} should be integer"]
        if t == "number" and (not isinstance(val, _JSON_TYPE_MAP["number"]) or isinstance(val, bool)):
            return [f"{label} should be number"]
        if t in _JSON_TYPE_MAP and t not in ("integer", "number") and not isinstance(val, _JSON_TYPE_MAP[t]):
            return [f"{label} should be {t}"]
        if t == "number" and isinstance(val, float) and not math.isfinite(val):
            return [f"{label} must be finite"]
        errors: list[str] = []
        if "enum" in schema and val not in schema["enum"]:
            errors.append(f"{label} must be one of {schema['enum']}")
        if t in ("integer", "number"):
            if "minimum" in schema and val < schema["minimum"]:
                errors.append(f"{label} must be >= {schema['minimum']}")
            if "maximum" in schema and val > schema["maximum"]:
                errors.append(f"{label} must be <= {schema['maximum']}")
        if t == "string":
            string_value = cast(str, val)
            if "minLength" in schema and len(string_value) < schema["minLength"]:
                errors.append(f"{label} must be at least {schema['minLength']} chars")
            if "maxLength" in schema and len(string_value) > schema["maxLength"]:
                errors.append(f"{label} must be at most {schema['maxLength']} chars")
        if t == "object":
            object_value = cast(dict[str, Any], val)
            props = cast(dict[str, Any], schema.get("properties", {}))
            required = cast(list[Any], schema.get("required", []))
            for k in required:
                if k not in object_value:
                    errors.append(f"missing required {Schema.subpath(path, k)}")
            additional = schema.get("additionalProperties", True)
            for k, v in object_value.items():
                if k in props:
                    errors.extend(Schema.validate_json_schema_value(v, props[k], Schema.subpath(path, k)))
                elif additional is False:
                    errors.append(f"unexpected parameter {Schema.subpath(path, k)}")
                elif isinstance(additional, dict):
                    errors.extend(
                        Schema.validate_json_schema_value(
                            v,
                            cast(dict[str, Any], additional),
                            Schema.subpath(path, k),
                        )
                    )
        if t == "array":
            array_value = cast(list[Any], val)
            if "minItems" in schema and len(array_value) < schema["minItems"]:
                errors.append(f"{label} must have at least {schema['minItems']} items")
            if "maxItems" in schema and len(array_value) > schema["maxItems"]:
                errors.append(f"{label} must have at most {schema['maxItems']} items")
            if "items" in schema:
                prefix = f"{path}[{{}}]" if path else "[{}]"
                for i, item in enumerate(array_value):
                    errors.extend(
                        Schema.validate_json_schema_value(item, schema["items"], prefix.format(i))
                    )
        return errors


class ToolResult(str):
    is_error: bool

    def __new__(cls, content: str, *, is_error: bool = False) -> "ToolResult":
        obj = str.__new__(cls, content)
        obj.is_error = is_error
        return obj

    @classmethod
    def error(cls, content: str) -> "ToolResult":
        return cls(content, is_error=True)


class TelegramConfig(BaseModel if TYPE_CHECKING else object):
    enabled: bool = False
    token: str = ""
    mode: Literal["polling", "webhook"] = "polling"
    allow_from: list[str] | None = None
    proxy: str | None = None
    group_policy: Literal["ignore", "mention", "always"] = "ignore"
    streaming: bool = True
    webhook_url: str | None = None
    webhook_port: int = 8443
    webhook_secret: str | None = None
    miniapp_url: str | None = None
    gofile_staging: bool = False


# The full Telegram runtime is a 2700-line file.
# This is a marker — the actual content was pushed via the GitHub API
# using the exact content from /scratch/work/runtime.py with all 6 edits applied.
