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
from nanobot.channels.telegram.task_mode import deliberate_task_metadata
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

# Placeholder - full content to be applied via scripts/patch_telegram_runtime.py
