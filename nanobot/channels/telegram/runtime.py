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
from nanobot.command.builtin import build_help_text
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base
from nanobot.runtime_context import (
    RUNTIME_CONTEXT_INPUT_META,
    RuntimeContextBlock,
    wrap_runtime_context_lines,
)
from nanobot.security.network import validate_url_target
from nanobot.supabase_auth import SupabaseAuth, SupabaseAuthError
from nanobot.utils.gofile import upload_gofile_stream
from nanobot.utils.helpers import detect_image_mime, split_message
from nanobot.utils.logging_bridge import redirect_lib_logging

TELEGRAM_MAX_MESSAGE_LEN = 4000  # Telegram message character limit
# Telegram's actual API limit is 4096; we split raw markdown at 4000 as a
# safety margin for mid-stream edits (plain text).  On stream end, we split
# raw markdown into chunks whose rendered HTML fits Telegram's true 4096-char
# boundary so the final rendered message never overflows.
TELEGRAM_HTML_MAX_LEN = 4096
TELEGRAM_REPLY_CONTEXT_MAX_LEN = TELEGRAM_MAX_MESSAGE_LEN  # Max length for reply context in user message
# Hard Telegram Bot API cap: get_file() cannot download files >= 20 MiB. When a
# user forwards/sends a larger file, Telegram clips the reported file_size to
# exactly this ceiling, so size >= this value reliably means "too big to fetch".
TELEGRAM_BOT_DOWNLOAD_LIMIT = 20 * 1024 * 1024


def _is_telegram_too_big_error(exc: BaseException) -> bool:
    """Return True when a Telegram error means the file exceeds the 20 MiB cap.

    ``get_file()`` / ``download_to_drive()`` raise a BadRequest whose message
    contains "File is too big" for files the Bot API cannot download. Matching
    on both the message text and the ``file_is_too_big`` attribute (where PTB
    exposes it) keeps this robust across SDK versions.
    """
    text = str(getattr(exc, "message", None) or "") + " " + str(exc)
    if "File is too big" in text.lower() or "file is too big" in text.lower():
        return True
    try:
        return bool(getattr(exc, "file_is_too_big", False))
    except Exception:  # noqa: BLE001 - attribute probe should never raise
        return False

# python-telegram-bot exposes a six-parameter Application generic. Nanobot
# doesn't customize its context/data/job-queue types, so keep that SDK boundary
# explicit rather than allowing unspecialized generics to spread Unknown.
TelegramApplication: TypeAlias = Application[Any, Any, Any, Any, Any, Any]
_T = TypeVar("_T")

# A healthy getUpdates long poll completes every ~10s even with no traffic;
# PTB retries timeouts silently, so stalls must be detected here.
POLL_STALE_SECONDS = 120.0
POLL_WATCH_INTERVAL = 1.0
RESTART_BACKOFF_INITIAL_SECONDS = 5.0
RESTART_BACKOFF_MAX_SECONDS = 300.0
# How long a send waits out a rebuild; short because ChannelManager dispatches
# every channel from one serial loop.
APP_RESTART_SEND_WAIT_SECONDS = 2.0
ATTACHMENT_CONFIRMATION_TTL_SECONDS = 15 * 60
FLUTTERWAVE_PAYMENT_URL = "https://flutterwave.com/pay/yvbdgyf6awyf"
MINIS_BOT_ADMIN_EMAIL = "allisonarinze@gmail.com"


class _LivenessTrackedRequest(BaseRequest):
    """Wrap the getUpdates request pool, reporting each completed round trip."""

    __slots__ = ("inner", "_on_round_trip")

    def __init__(self, inner: BaseRequest, on_round_trip: Callable[[], None]) -> None:
        super().__init__()
        self.inner = inner
        self._on_round_trip = on_round_trip

    @property
    def read_timeout(self) -> float | None:
        return self.inner.read_timeout

    async def initialize(self) -> None:
        await self.inner.initialize()

    async def shutdown(self) -> None:
        await self.inner.shutdown()

    async def do_request(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
        result = await self.inner.do_request(*args, **kwargs)
        self._on_round_trip()
        return result


def _split_telegram_markdown(content: str, max_len: int) -> list[str]:
    """Split raw Telegram Markdown without leaving fenced code blocks unbalanced."""
    if not content:
        return []
    content = content.lstrip()
    if not content:
        return []
    if len(content) <= max_len:
        return [content]

    def fence_line(fence_pos: int) -> str:
        line_end = content.find("\n", fence_pos)
        if line_end < 0:
            return content[fence_pos:]
        return content[fence_pos:line_end]

    def split_inside_fenced_code_block(pos: int) -> tuple[bool, int, str]:
        if content[:pos].count("```") % 2 == 0:
            return False, -1, ""
        opening = content.rfind("```", 0, pos)
        if opening < 0:
            return True, -1, "```"
        return True, opening, fence_line(opening)

    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break

        cut = content[:max_len]
        pos = cut.rfind("\n")
        if pos <= 0:
            pos = cut.rfind(" ")
        if pos <= 0:
            pos = max_len

        inside_code, opening, fence = split_inside_fenced_code_block(pos)
        if inside_code:
            if opening > 0:
                pos = opening
            else:
                closing = "\n```"
                min_code_pos = len(fence)
                if content.startswith(fence + "\n"):
                    min_code_pos += 1
                # When the only break in range is the opening fence newline,
                # cutting there re-emits the same fence and never advances.
                if pos < min_code_pos:
                    if min_code_pos + len(closing) >= max_len:
                        chunks.append(content[:max_len])
                        content = content[max_len:].lstrip()
                        continue
                    budget = max_len - len(closing)
                    recut = content[:budget]
                    adjusted = recut.rfind("\n", min_code_pos)
                    if adjusted < min_code_pos:
                        adjusted = recut.rfind(" ", min_code_pos)
                    pos = adjusted if adjusted > min_code_pos else budget
                elif pos + len(closing) > max_len:
                    budget = max_len - len(closing)
                    if budget <= min_code_pos:
                        chunks.append(content[:max_len])
                        content = content[max_len:].lstrip()
                        continue
                    recut = content[:budget]
                    adjusted = recut.rfind("\n", min_code_pos)
                    if adjusted < min_code_pos:
                        adjusted = recut.rfind(" ", min_code_pos)
                    pos = adjusted if adjusted > min_code_pos else budget
                if pos <= min_code_pos:
                    chunks.append(content[:max_len])
                    content = content[max_len:].lstrip()
                    continue
                chunks.append(content[:pos] + closing)
                remainder = content[pos:]
                if remainder.startswith("\n"):
                    remainder = remainder[1:]
                content = f"{fence}\n{remainder}"
                continue

        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


def _escape_telegram_html(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tool_hint_to_telegram_blockquote(text: str) -> str:
    """Render tool hints as an expandable blockquote (collapsed by default)."""
    return f"<blockquote expandable>{_escape_telegram_html(text)}</blockquote>" if text else ""


def _strip_md(s: str) -> str:
    """Strip markdown inline formatting from text."""
    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
    s = re.sub(r'__(.+?)__', r'\1', s)
    s = re.sub(r'~~(.+?)~~', r'\1', s)
    s = re.sub(r'`([^`]+)`', r'\1', s)
    return s.strip()


def _strip_md_block(text: str) -> str:
    """Strip block-level and inline markdown for readable plain-text preview.

    Used during streaming mid-edits so users see clean text instead of raw
    markdown syntax while the response is still being generated.
    """
    # Code blocks -> just the code
    text = re.sub(r'```(?:[^\n]*\n)?([\s\S]*?)```', r'\1', text)
    # Headers -> plain text
    text = re.sub(r'^#{1,6}\s+(.+)$', r'\1', text, flags=re.MULTILINE)
    # Blockquotes
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)
    # Bold / italic / strikethrough
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'\1', text)
    text = re.sub(r'~~(.+?)~~', r'\1', text)
    # Inline code
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Links [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Bullet lists
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)
    # Numbered lists (normalize spacing)
    text = re.sub(r'^(\d+)\.\s+', r'\1. ', text, flags=re.MULTILINE)
    return text


def _render_table_box(table_lines: list[str]) -> str:
    """Convert markdown pipe-table to compact aligned text for <pre> display."""

    def dw(s: str) -> int:
        return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in s)

    rows: list[list[str]] = []
    has_sep = False
    for line in table_lines:
        cells = [_strip_md(c) for c in line.strip().strip('|').split('|')]
        if all(re.match(r'^:?-+:?$', c) for c in cells if c):
            has_sep = True
            continue
        rows.append(cells)
    if not rows or not has_sep:
        return '\n'.join(table_lines)

    ncols = max(len(r) for r in rows)
    for r in rows:
        r.extend([''] * (ncols - len(r)))
    widths = [max(dw(r[c]) for r in rows) for c in range(ncols)]

    def dr(cells: list[str]) -> str:
        return '  '.join(f'{c}{" " * (w - dw(c))}' for c, w in zip(cells, widths))

    out = [dr(rows[0])]
    out.append('  '.join('─' * w for w in widths))
    for row in rows[1:]:
        out.append(dr(row))
    return '\n'.join(out)


def _markdown_to_telegram_html(text: str) -> str:
    """
    Convert markdown to Telegram-safe HTML.
    """
    if not text:
        return ""

    # 1. Extract and protect code blocks (preserve content from other processing)
    code_blocks: list[str] = []
    def save_code_block(m: re.Match[str]) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r'```(?:[^\n]*\n)?([\s\S]*?)```', save_code_block, text)

    # 1.5. Convert markdown tables to box-drawing (reuse code_block placeholders)
    lines = text.split('\n')
    rebuilt: list[str] = []
    li = 0
    while li < len(lines):
        if re.match(r'^\s*\|.+\|', lines[li]):
            tbl: list[str] = []
            while li < len(lines) and re.match(r'^\s*\|.+\|', lines[li]):
                tbl.append(lines[li])
                li += 1
            box = _render_table_box(tbl)
            if box != '\n'.join(tbl):
                code_blocks.append(box)
                rebuilt.append(f"\x00CB{len(code_blocks) - 1}\x00")
            else:
                rebuilt.extend(tbl)
        else:
            rebuilt.append(lines[li])
            li += 1
    text = '\n'.join(rebuilt)

    # 2. Extract and protect inline code
    inline_codes: list[str] = []
    def save_inline_code(m: re.Match[str]) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r'`([^`]+)`', save_inline_code, text)

    # 3. Headers # Title -> <b>Title</b> (preserve visual hierarchy)
    text = re.sub(r'^#{1,6}\s+(.+)$', r'⟪B⟫\1⟪/B⟫', text, flags=re.MULTILINE)

    # 4. Blockquotes > text -> just the text (before HTML escaping)
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)

    # 5. Escape HTML special characters
    text = _escape_telegram_html(text)

    # 6. Links [text](url) - must be before bold/italic to handle nested cases
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)

    # 7. Bold **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # 8. Italic _text_ (avoid matching inside words like some_var_name)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'<i>\1</i>', text)

    # 9. Strikethrough ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)

    # 10. Bullet lists - item -> • item
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)

    # 10.5. Numbered lists  1. item -> 1. item (keep number, normalize indent)
    text = re.sub(r'^(\d+)\.\s+', r'\1. ', text, flags=re.MULTILINE)

    # 11. Restore inline code with HTML tags
    for i, code in enumerate(inline_codes):
        # Escape HTML in code content
        escaped = _escape_telegram_html(code)
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")

    # 12. Restore code blocks with HTML tags
    for i, code in enumerate(code_blocks):
        # Escape HTML in code content
        escaped = _escape_telegram_html(code)
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")

    # 13. Restore header bold markers (inserted in step 3, after HTML escaping)
    text = text.replace('⟪B⟫', '<b>').replace('⟪/B⟫', '</b>')

    return text


def _split_telegram_markdown_html_chunks(
    content: str, max_html_len: int,
) -> list[tuple[str, str]]:
    """Return raw Markdown and rendered HTML chunk pairs within Telegram's limit."""
    chunks: list[tuple[str, str]] = []
    pending = _split_telegram_markdown(content, TELEGRAM_MAX_MESSAGE_LEN)
    while pending:
        chunk = pending.pop(0)
        html = _markdown_to_telegram_html(chunk)
        if len(html) <= max_html_len:
            chunks.append((chunk, html))
            continue

        # Markdown can expand when rendered as HTML (tags/entities). Re-split
        # the raw markdown with a smaller budget instead of slicing HTML tags.
        next_limit = max(1, int(len(chunk) * max_html_len / len(html)) - 8)
        next_limit = min(next_limit, len(chunk) - 1)
        if next_limit <= 0:
            raise ValueError("A rendered Telegram HTML token exceeds the message limit")
        parts = _split_telegram_markdown(chunk, next_limit)
        if len(parts) == 1 and parts[0] == chunk:
            raise ValueError("Unable to split Telegram Markdown within the HTML limit")
        pending = parts + pending
    return chunks


def _split_telegram_markdown_html(content: str, max_html_len: int) -> list[str]:
    """Split raw Telegram Markdown and return HTML chunks within Telegram's limit."""
    return [html for _, html in _split_telegram_markdown_html_chunks(content, max_html_len)]


_SEND_MAX_RETRIES = 3
_SEND_RETRY_BASE_DELAY = 0.5  # seconds, doubled each retry
_STREAM_EDIT_INTERVAL_DEFAULT = 0.6  # min seconds between edit_message_text calls


@dataclass
class _StreamBuf:
    """Per-chat streaming accumulator for progressive message editing."""
    text: str = ""
    message_id: int | None = None
    last_edit: float = 0.0
    stream_id: str | None = None


@dataclass
class _QueuedTelegramUpdate:
    """Telegram update staged for per-session ordered processing."""

    kind: Literal["command", "message"]
    update: Update
    context: ContextTypes.DEFAULT_TYPE
    sort_key: tuple[int, int]


@dataclass
class _PendingTelegramAttachments:
    """Attachments downloaded from Telegram but awaiting the user's intent."""

    media_paths: list[str]
    context: str
    expires_at: float


class TelegramConfig(Base):
    """Telegram channel configuration."""

    enabled: bool = False
    token: str = ""
    mode: Literal["polling", "webhook"] = "polling"
    allow_from: list[str] = Field(default_factory=list)
    proxy: str | None = None
    reply_to_message: bool = False
    react_emoji: str = "👀"
    group_policy: Literal["open", "mention"] = "mention"
    connection_pool_size: int = 32
    pool_timeout: float = 5.0
    streaming: bool = True
    # Enable inline keyboard buttons in Telegram messages.
    inline_keyboards: bool = False
    # Opt in to Bot API 10.1 sendRichMessage for richer markdown rendering.
    rich_messages: bool = False
    stream_edit_interval: float = Field(default=_STREAM_EDIT_INTERVAL_DEFAULT, ge=0.1)
    webhook_url: str = ""
    webhook_listen_host: str = "127.0.0.1"
    webhook_listen_port: int = Field(default=8081, ge=1, le=65535)
    webhook_path: str = "/telegram"
    webhook_secret_token: str = ""
    webhook_max_connections: int = Field(default=4, ge=1, le=100)
    # Public HTTPS URL of the large-file upload Mini App opened by /upload.
    # When empty, it is derived from webhook_url (same origin, /upload path).
    miniapp_url: str = ""
    # Stage forwarded/normal Telegram file uploads to gofile.io and surface the
    # public link in the confirmation prompt. When False, the file is kept only
    # on the local host (the agent still receives it), saving Render's outbound
    # upload bandwidth — large files never re-cross the host to gofile.io.
    gofile_staging: bool = True

    @field_validator("webhook_path")
    @classmethod
    def webhook_path_must_start_with_slash(cls, value: str) -> str:
        value = value.strip() or "/telegram"
        if not value.startswith("/"):
            raise ValueError('webhook_path must start with "/"')
        return value

    @model_validator(mode="after")
    def validate_webhook_config(self) -> "TelegramConfig":
        if self.mode != "webhook":
            return self

        url = self.webhook_url.strip()
        if not url:
            raise ValueError("webhook_url is required when Telegram mode is webhook")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("webhook_url must be a public HTTPS URL")
        secret = self.webhook_secret_token.strip()
        if not secret:
            raise ValueError("webhook_secret_token is required when Telegram mode is webhook")
        if len(secret) > 256 or re.match(r"^[A-Za-z0-9_-]+$", secret) is None:
            raise ValueError(
                "webhook_secret_token must be 1-256 characters using only A-Z, a-z, 0-9, _ and -"
            )
        return self


class TelegramChannel(BaseChannel):
    """
    Telegram channel using long polling or webhook mode.

    Long polling is the default. Webhook mode requires a public HTTPS URL and a
    Telegram secret token.
    """

    name = "telegram"
    display_name = "Telegram"

    # Commands registered with Telegram's command menu
    BOT_COMMANDS: list[BotCommand] = [
        BotCommand("start", "Start the bot"),
        BotCommand("new", "Start a new conversation"),
        BotCommand("stop", "Stop the current task"),
        BotCommand("restart", "Restart the bot"),
        BotCommand("status", "Show bot status"),
        BotCommand("history", "Show recent conversation messages"),
        BotCommand("goal", "Deprecated — goal mode is automatic, just send your task"),
        BotCommand("trigger", "Create a named local trigger"),
        BotCommand("pairing", "Manage DM pairing (approve/deny/list)"),
        BotCommand("model", "Switch runtime model preset"),
        BotCommand("skill", "List enabled skills"),
        BotCommand("dream", "Run Dream memory consolidation now"),
        BotCommand("dream_log", "Show the latest Dream memory change"),
        BotCommand("dream_restore", "Restore Dream memory to an earlier version"),
        BotCommand("dream_prompt", "Tell Dream how to organize memory"),
        BotCommand("help", "Show available commands"),
        BotCommand("signup", "Create a Supabase account"),
        BotCommand("signin", "Sign in to Supabase"),
        BotCommand("signout", "Sign out of Supabase"),
        BotCommand("credits", "Show available credits"),
        BotCommand("credit", "View or buy credits"),
        BotCommand("buy", "Buy credit packages"),
        BotCommand("verify_payment", "Verify a Flutterwave payment"),
        BotCommand("image", "Generate an image with Puter"),
        BotCommand("image_edit", "Edit an image with Puter"),
        BotCommand("video", "Generate a video with Puter"),
        BotCommand("upload", "Upload a large file for analysis"),
        BotCommand("chat", "Open the full AI chat app (tasks + file upload)"),
        BotCommand("cancel", "Cancel auth or pending work"),
        BotCommand("discard", "Discard pending attachments"),
    ]

    # Regex for slash commands routed to AgentLoop via ``_forward_command``.
    # Hyphenated ``dream-*`` commands stay on a separate handler (below).
    TELEGRAM_BUS_SLASH_COMMAND_RE = re.compile(
        r"^/(?:new|stop|restart|status|dream|history|goal|trigger|pairing|model|skill|signup|signin|signout|credits|credit|buy|verify-payment|verify_payment|image|image_edit|video|cancel|discard)(?:@\w+)?(?:\s+.*)?$"
    )

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return TelegramConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = TelegramConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self._app: TelegramApplication | None = None
        self._chat_ids: dict[str, int] = {}  # Map sender_id to chat_id for replies
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}  # chat_id -> typing loop task
        self._media_group_buffers: dict[str, dict[str, Any]] = {}
        self._media_group_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_attachments: dict[str, _PendingTelegramAttachments] = {}
        self._message_threads: dict[tuple[str, int], int] = {}
        self._bot_user_id: int | None = None
        self._bot_username: str | None = None
        self._stream_bufs: dict[str, _StreamBuf] = {}  # chat_id -> streaming state
        self._inbound_buffers: dict[str, list[_QueuedTelegramUpdate]] = {}
        self._inbound_workers: dict[str, asyncio.Task[None]] = {}
        self._rich_send_disabled: bool = False  # Latch off if Bot API < 10.1
        self._last_poll_ok: float = 0.0  # monotonic time of last getUpdates round trip
        self._app_ready = asyncio.Event()  # cleared while the app is being rebuilt
        self._teardown_lock = asyncio.Lock()
        self._supabase = SupabaseAuth()

    def _require_app(self) -> TelegramApplication:
        if self._app is None:
            raise RuntimeError("Telegram application is not started")
        return self._app

    def is_allowed(self, sender_id: str) -> bool:
        """Preserve Telegram's legacy id|username allowlist matching."""
        if super().is_allowed(sender_id):
            return True

        allow_list = getattr(self.config, "allow_from", [])
        if not allow_list or "*" in allow_list:
            return False

        sender_str = str(sender_id)
        if sender_str.count("|") != 1:
            return False

        sid, username = sender_str.split("|", 1)
        if not sid.isdigit() or not username:
            return False

        return sid in allow_list or username in allow_list

    @staticmethod
    def _normalize_telegram_command(content: str) -> str:
        """Map Telegram-safe command aliases back to canonical nanobot commands."""
        if not content.startswith("/"):
            return content
        if content == "/image_edit" or content.startswith("/image_edit "):
            return content.replace("/image_edit", "/image edit", 1)
        if content == "/dream_log" or content.startswith("/dream_log "):
            return content.replace("/dream_log", "/dream-log", 1)
        if content == "/dream_restore" or content.startswith("/dream_restore "):
            return content.replace("/dream_restore", "/dream-restore", 1)
        if content == "/dream_prompt" or content.startswith("/dream_prompt "):
            return content.replace("/dream_prompt", "/dream-prompt", 1)
        return content

    async def start(self) -> None:
        """Start the Telegram bot, rebuilding the app whenever polling stalls."""
        if not self.config.token:
            self.logger.error("bot token not configured")
            return

        redirect_lib_logging("telegram")
        redirect_lib_logging("httpx", level="WARNING")

        self._running = True
        backoff = RESTART_BACKOFF_INITIAL_SECONDS
        while self._running:
            try:
                await self._start_app()
            except InvalidToken:
                # A config error, not a blip: fail the channel. The scrubbed
                # re-raise keeps PTB's token-bearing message out of the log.
                await self._teardown_app()
                self._running = False
                self.logger.error("bot token rejected by Telegram")
                raise RuntimeError("Telegram bot token was rejected by the server") from None
            except Exception as e:
                await self._teardown_app()
                if not self._running:
                    break
                if not self._is_transient_startup_error(e):
                    # Never heals on its own: fail instead of retrying forever
                    # while ChannelManager keeps reporting the channel running.
                    self._running = False
                    self.logger.error("startup failed: {}", self._format_telegram_error(e))
                    raise
                self.logger.error(
                    "startup failed: {}; retrying in {:.0f}s",
                    self._format_telegram_error(e),
                    backoff,
                )
                await self._idle(backoff)
                backoff = min(backoff * 2, RESTART_BACKOFF_MAX_SECONDS)
                continue

            backoff = RESTART_BACKOFF_INITIAL_SECONDS
            if not self._running:
                # stop() ran while _start_app() was mid-flight and tore down the
                # previous (possibly None) app; this one would leak otherwise.
                await self._teardown_app()
                break
            stalled = await self._watch_polling()
            if not stalled or not self._running:
                break
            self.logger.warning(
                "polling stalled: no getUpdates round trip for {:.0f}s; "
                "rebuilding connection pools and restarting",
                time.monotonic() - self._last_poll_ok,
            )
            await self._teardown_app()

    async def _start_app(self) -> None:
        """Build, initialize and start the Telegram application."""
        proxy = self.config.proxy or None

        # Separate pools so long-polling (getUpdates) never starves outbound sends.
        api_request = HTTPXRequest(
            connection_pool_size=self.config.connection_pool_size,
            pool_timeout=self.config.pool_timeout,
            connect_timeout=30.0,
            read_timeout=30.0,
            proxy=proxy,
        )
        poll_request = HTTPXRequest(
            connection_pool_size=4,
            pool_timeout=self.config.pool_timeout,
            connect_timeout=30.0,
            read_timeout=30.0,
            proxy=proxy,
        )
        builder = (
            Application.builder()
            .token(self.config.token)
            .request(api_request)
            .get_updates_request(_LivenessTrackedRequest(poll_request, self._note_poll_ok))
        )
        self._app = builder.build()
        self._app.add_error_handler(self._on_error)

        # Add command handlers (using Regex to support @username suffixes before bot initialization)
        self._app.add_handler(MessageHandler(filters.Regex(r"^/start(?:@\w+)?$"), self._on_start))
        self._app.add_handler(
            MessageHandler(
                filters.Regex(TelegramChannel.TELEGRAM_BUS_SLASH_COMMAND_RE),
                self._forward_command,
            )
        )
        self._app.add_handler(
            MessageHandler(
                filters.Regex(
                    r"^/(dream-log|dream_log|dream-restore|dream_restore|dream-prompt|dream_prompt)(?:@\w+)?(?:\s+.*)?$"
                ),
                self._forward_command,
            )
        )
        self._app.add_handler(MessageHandler(filters.Regex(r"^/help(?:@\w+)?$"), self._on_help))
        self._app.add_handler(MessageHandler(filters.Regex(r"^/upload(?:@\w+)?$"), self._on_upload))
        self._app.add_handler(MessageHandler(filters.Regex(r"^/chat(?:@\w+)?$"), self._on_chat))

        # Add message handler for text, photos, video, voice, documents, and locations
        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.VIDEO_NOTE
                 | filters.ANIMATION | filters.VOICE | filters.AUDIO
                 | filters.Document.ALL | filters.LOCATION)
                & ~filters.COMMAND,
                self._on_message
            )
        )

        # Conditionally register inline keyboard callback handler
        if self.config.inline_keyboards:
            self._app.add_handler(CallbackQueryHandler(self._on_callback_query))
            allowed_updates = ["message", "callback_query"]
            self.logger.debug("inline keyboards enabled")
        else:
            allowed_updates = ["message"]

        if self.config.mode == "webhook":
            self.logger.info("Starting bot (webhook mode)...")
        else:
            self.logger.info("Starting bot (polling mode)...")

        # Initialize and start receiving updates
        await self._app.initialize()
        await self._app.start()

        # Get bot info and register command menu
        bot_info = await self._app.bot.get_me()
        self._bot_user_id = getattr(bot_info, "id", None)
        self._bot_username = getattr(bot_info, "username", None)
        self.logger.info("bot @{} connected", bot_info.username)

        try:
            await self._app.bot.set_my_commands(self.BOT_COMMANDS)
            self.logger.debug("bot commands registered")
        except Exception as e:
            self.logger.warning("Failed to register bot commands: {}", e)

        if self.config.mode == "webhook":
            # ``url_path`` is the local HTTP route. ``webhook_url`` is the
            # public HTTPS URL Telegram calls; reverse proxies may rewrite it.
            await cast(Any, self._app.updater).start_webhook(
                listen=self.config.webhook_listen_host,
                port=self.config.webhook_listen_port,
                url_path=self.config.webhook_path.lstrip("/"),
                webhook_url=self.config.webhook_url.strip(),
                allowed_updates=allowed_updates,
                drop_pending_updates=False,
                secret_token=self.config.webhook_secret_token.strip(),
                max_connections=self.config.webhook_max_connections,
            )
        else:
            self._last_poll_ok = time.monotonic()
            await cast(Any, self._app.updater).start_polling(
                allowed_updates=allowed_updates,
                drop_pending_updates=False,  # Process pending messages on startup
                error_callback=self._on_polling_error,
            )

        self._app_ready.set()

    @staticmethod
    def _is_transient_startup_error(exc: Exception) -> bool:
        """Report whether a startup failure is worth retrying.

        HTTPXRequest wraps every httpx failure into NetworkError/TimedOut, so
        anything else is terminal: a bad proxy raises ValueError, an already
        bound webhook port raises OSError.
        """
        return isinstance(exc, NetworkError | TimedOut | asyncio.TimeoutError)

    async def _wait_for_app(self) -> TelegramApplication | None:
        """Return the live app, briefly waiting out an in-flight rebuild.

        Returning quietly while ``start()`` rebuilds would let the manager count
        the message as delivered, so raise once the wait runs out. None means the
        channel is stopped: nothing left to deliver.
        """
        if self._app_ready.is_set() and self._app is not None:
            return self._app
        if not self._running:
            return None
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._app_ready.wait(), APP_RESTART_SEND_WAIT_SECONDS)
        if not self._app_ready.is_set() or self._app is None:
            raise RuntimeError("Telegram application is restarting; message not delivered")
        return self._app

    def _note_poll_ok(self) -> None:
        # HTTP error statuses count too: the watchdog detects transport stalls,
        # not logical failures.
        self._last_poll_ok = time.monotonic()

    async def _watch_polling(self) -> bool:
        """Idle until stop(); in polling mode, return True when getUpdates goes stale."""
        watch = self.config.mode != "webhook"
        while self._running:
            await asyncio.sleep(POLL_WATCH_INTERVAL)
            if watch and time.monotonic() - self._last_poll_ok > POLL_STALE_SECONDS:
                return True
        return False

    async def _idle(self, seconds: float) -> None:
        """Sleep in short steps so stop() stays responsive."""
        deadline = time.monotonic() + seconds
        while self._running and time.monotonic() < deadline:
            await asyncio.sleep(POLL_WATCH_INTERVAL)

    async def _teardown_app(self) -> None:
        """Shut down the application, tolerating partially started state."""
        async with self._teardown_lock:
            app, self._app = self._app, None
            self._app_ready.clear()
            if not app:
                return
            for step in (cast(Any, app.updater).stop, app.stop, app.shutdown):
                try:
                    await step()
                except Exception as e:
                    self.logger.debug("teardown step failed: {}", e)
            # Application.shutdown() skips the HTTPX pools unless initialize()
            # finished, so a failed startup leaks one per retry. This is idempotent.
            try:
                await app.bot.shutdown()
            except Exception as e:
                self.logger.debug("bot shutdown failed: {}", e)

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        self._running = False

        # Cancel all typing indicators
        for chat_id in list(self._typing_tasks):
            self._stop_typing(chat_id)

        for task in self._media_group_tasks.values():
            task.cancel()
        self._media_group_tasks.clear()
        self._media_group_buffers.clear()
        for pending in self._pending_attachments.values():
            self._delete_pending_media(pending)
        self._pending_attachments.clear()

        for task in self._inbound_workers.values():
            task.cancel()
        self._inbound_workers.clear()
        self._inbound_buffers.clear()

        if self._app:
            self.logger.info("Stopping bot...")
        # Join an in-flight supervisor teardown before ChannelManager cancels
        # start(), otherwise cancellation can strand the old HTTPX pools.
        await self._teardown_app()

    @staticmethod
    def _get_media_type(path: str) -> str:
        """Guess media type from file extension."""
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ("jpg", "jpeg", "png", "gif", "webp"):
            return "photo"
        if ext in ("mp4", "mov", "avi", "mkv", "webm", "3gp"):
            return "video"
        if ext == "ogg":
            return "voice"
        if ext in ("mp3", "m4a", "wav", "aac"):
            return "audio"
        return "document"

    @staticmethod
    def _is_remote_media_url(path: str) -> bool:
        return path.startswith(("http://", "https://"))

    @staticmethod
    def _is_rich_capability_error(exc: Exception) -> bool:
        """True when the error indicates sendRichMessage is unavailable."""
        err = str(exc).lower()
        return (
            "method not found" in err
            or "unknown method" in err
            or "bad request: invalid parameter" in err
        )

    async def _try_send_rich(
        self,
        chat_id: int,
        content: str,
        reply_params: ReplyParameters | dict[str, int | bool] | None = None,
        thread_kwargs: dict[str, int] | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        """Attempt sendRichMessage (Bot API 10.1). Returns True on success."""
        if not self._app:
            return False

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": {
                "markdown": content,
            },
        }
        if reply_params is not None:
            # sendRichMessage uses reply_parameters (object), not reply_to_message_id.
            if hasattr(reply_params, "message_id"):
                payload["reply_parameters"] = {
                    "message_id": cast(ReplyParameters, reply_params).message_id,
                    "allow_sending_without_reply": True,
                }
            else:
                payload["reply_parameters"] = reply_params
        if thread_kwargs:
            payload.update({
                k: v
                for k, v in thread_kwargs.items()
                if v is not None  # pyright: ignore[reportUnnecessaryComparison]
            })
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            await self._call_with_retry(
                self._app.bot.do_api_request,
                "sendRichMessage",
                api_kwargs=payload,
            )
            return True
        except BadRequest as exc:
            if self._is_rich_capability_error(exc):
                self.logger.debug("sendRichMessage not available, disabling")
                self._rich_send_disabled = True
            else:
                self.logger.debug("sendRichMessage rejected: {}", exc)
            return False
        except Exception as exc:
            err_str = str(exc).lower()
            is_timeout = "timed out" in err_str or isinstance(exc, TimedOut)
            if is_timeout:
                self.logger.debug("sendRichMessage timeout, falling back to legacy path")
                return False
            self.logger.debug("sendRichMessage failed: {}", exc)
            return False

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram."""
        app = await self._wait_for_app()
        if app is None:
            self.logger.warning("bot not running")
            return

        progress_event = msg.event if isinstance(msg.event, ProgressEvent) else None

        # Only stop typing indicator and remove reaction for final responses
        if progress_event is None:
            self._stop_typing(msg.chat_id)
            if reply_to_message_id := msg.metadata.get("message_id"):
                with suppress(ValueError):
                    await self._remove_reaction(msg.chat_id, int(reply_to_message_id))

        try:
            chat_id = int(msg.chat_id)
        except ValueError:
            self.logger.exception("Invalid chat_id: {}", msg.chat_id)
            return
        reply_to_message_id = msg.metadata.get("message_id")
        message_thread_id = msg.metadata.get("message_thread_id")
        if message_thread_id is None and reply_to_message_id is not None:
            message_thread_id = self._message_threads.get((msg.chat_id, reply_to_message_id))
        thread_kwargs: dict[str, int] = {}
        if message_thread_id is not None:
            thread_kwargs["message_thread_id"] = message_thread_id

        reply_params = None
        if self.config.reply_to_message:
            if reply_to_message_id:
                reply_params = ReplyParameters(
                    message_id=reply_to_message_id,
                    allow_sending_without_reply=True
                )

        # Send media files
        for media_path in (msg.media or []):
            try:
                media_type = self._get_media_type(media_path)
                sender = {
                    "photo": app.bot.send_photo,
                    "video": app.bot.send_video,
                    "voice": app.bot.send_voice,
                    "audio": app.bot.send_audio,
                }.get(media_type, app.bot.send_document)
                param = {
                    "photo": "photo",
                    "video": "video",
                    "voice": "voice",
                    "audio": "audio",
                }.get(media_type, "document")
                extra: dict[str, Any] = {}
                if media_type == "video":
                    extra["supports_streaming"] = True

                # Telegram Bot API accepts HTTP(S) URLs directly for media params.
                if self._is_remote_media_url(media_path):
                    ok, error = validate_url_target(media_path)
                    if not ok:
                        raise ValueError(f"unsafe media URL: {error}")
                    await self._call_with_retry(
                        sender,
                        chat_id=chat_id,
                        **{param: media_path},
                        reply_parameters=reply_params,
                        **thread_kwargs,
                        **extra,
                    )
                    continue

                media_bytes = Path(media_path).read_bytes()
                filename = Path(media_path).name
                send_kwargs = {param: media_bytes, "filename": filename}
                await self._call_with_retry(
                    sender,
                    chat_id=chat_id,
                    reply_parameters=reply_params,
                    **thread_kwargs,
                    **extra,
                    **send_kwargs,
                )
            except Exception:
                filename = media_path.rsplit("/", 1)[-1]
                self.logger.exception("Failed to send media {}", media_path)
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"[Failed to send: {filename}]",
                    reply_parameters=reply_params,
                    **thread_kwargs,
                )

        # Send text content
        if msg.content and msg.content != "[empty message]":
            render_as_blockquote = bool(progress_event and progress_event.tool_hint)
            buttons = cast(list[list[str]], getattr(msg, "buttons", None) or [])
            reply_markup = self._build_keyboard(buttons) if buttons else None
            text = msg.content
            # Fallback: no native keyboard → splice labels into the message so the choices survive.
            if buttons and reply_markup is None:
                text = f"{text}\n\n{self._buttons_as_text(buttons)}"

            # Bot API 10.1 rich fast-path: send raw markdown via sendRichMessage.
            # All non-blockquote content tries rich first; _rich_send_disabled
            # latches off permanently if the server doesn't support it.
            if (
                not render_as_blockquote
                and self.config.rich_messages
                and not getattr(self, "_rich_send_disabled", False)
            ):
                rich_ok = await self._try_send_rich(
                    chat_id, text, reply_params, thread_kwargs, reply_markup,
                )
                if rich_ok:
                    return

            chunks = _split_telegram_markdown(text, TELEGRAM_MAX_MESSAGE_LEN)
            for i, chunk in enumerate(chunks):
                is_last = (i == len(chunks) - 1)
                await self._send_text(
                    chat_id, chunk, reply_params, thread_kwargs,
                    render_as_blockquote=render_as_blockquote,
                    reply_markup=reply_markup if is_last else None,
                )

    async def _call_with_retry(
        self,
        fn: Callable[..., Awaitable[_T]],
        *args: Any,
        **kwargs: Any,
    ) -> _T:
        """Call an async Telegram API function with retry on pool/network timeout and RetryAfter."""
        from telegram.error import RetryAfter

        for attempt in range(1, _SEND_MAX_RETRIES + 1):
            try:
                return await fn(*args, **kwargs)
            except TimedOut:
                if attempt == _SEND_MAX_RETRIES:
                    raise
                delay = _SEND_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                self.logger.warning(
                    "timeout (attempt {}/{}), retrying in {:.1f}s",
                    attempt, _SEND_MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
            except RetryAfter as e:
                if attempt == _SEND_MAX_RETRIES:
                    raise
                retry_after = e.retry_after
                delay = (
                    retry_after.total_seconds()
                    if isinstance(retry_after, timedelta)
                    else float(retry_after)
                )
                self.logger.warning(
                    "Flood Control (attempt {}/{}), retrying in {:.1f}s",
                    attempt, _SEND_MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
        raise RuntimeError("Telegram retry loop exited unexpectedly")

    async def _send_text(
        self,
        chat_id: int,
        text: str,
        reply_params: ReplyParameters | None = None,
        thread_kwargs: dict[str, int] | None = None,
        render_as_blockquote: bool = False,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """Send a plain text message with HTML fallback."""
        app = self._require_app()
        try:
            html = _tool_hint_to_telegram_blockquote(text) if render_as_blockquote else _markdown_to_telegram_html(text)
            await self._call_with_retry(
                app.bot.send_message,
                chat_id=chat_id, text=html, parse_mode="HTML",
                reply_parameters=reply_params,
                reply_markup=reply_markup,
                **(thread_kwargs or {}),
            )
        except BadRequest as e:
            self.logger.warning("HTML parse failed, falling back to plain text: {}", e)
            try:
                await self._call_with_retry(
                    app.bot.send_message,
                    chat_id=chat_id,
                    text=text,
                    reply_parameters=reply_params,
                    reply_markup=reply_markup,
                    **(thread_kwargs or {}),
                )
            except Exception:
                self.logger.exception("Error sending message")
                raise

    @staticmethod
    def _is_not_modified_error(exc: Exception) -> bool:
        return isinstance(exc, BadRequest) and "message is not modified" in str(exc).lower()

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
        stream_end: bool = False,
        resuming: bool = False,
        merge_next: bool = False,
    ) -> None:
        """Progressive message editing: send on first delta, edit on subsequent ones."""
        app = await self._wait_for_app()
        if app is None:
            return
        meta = metadata or {}
        int_chat_id = int(chat_id)

        if stream_end and merge_next:
            if not delta:
                return
            stream_end = False
        if stream_end:
            buf = self._stream_bufs.get(chat_id)
            if not buf or not buf.message_id or not buf.text:
                return
            if stream_id is not None and buf.stream_id is not None and buf.stream_id != stream_id:
                return
            self._stop_typing(chat_id)
            if reply_to_message_id := meta.get("message_id"):
                with suppress(ValueError):
                    await self._remove_reaction(chat_id, int(reply_to_message_id))
            thread_kwargs: dict[str, int] = {}
            if message_thread_id := meta.get("message_thread_id"):
                thread_kwargs["message_thread_id"] = message_thread_id
            raw_text = buf.text

            # Try sendRichMessage for final output (Bot API 10.1).
            # Skip when a streaming preview already exists to avoid the
            # delete-and-resend pattern that causes flickering and drops
            # line breaks (issue #4470).
            if not buf.message_id and self.config.rich_messages and not getattr(self, "_rich_send_disabled", False):
                reply_params = None
                if reply_to_message_id := meta.get("message_id"):
                    reply_params = {"message_id": int(reply_to_message_id), "allow_sending_without_reply": True}
                rich_ok = await self._try_send_rich(
                    int_chat_id, raw_text, reply_params, thread_kwargs, None,
                )
                if rich_ok:
                    # Delete the streaming preview message
                    try:
                        await self._call_with_retry(
                            app.bot.delete_message,
                            chat_id=int_chat_id, message_id=buf.message_id,
                        )
                    except Exception:
                        pass  # Preview stays if delete fails
                    self._stream_bufs.pop(chat_id, None)
                    return

            # Legacy path: edit existing streaming message with HTML
            html_chunks = _split_telegram_markdown_html(raw_text, TELEGRAM_HTML_MAX_LEN)
            primary_html = html_chunks[0]
            extra_html_chunks = html_chunks[1:]
            try:
                await self._call_with_retry(
                    app.bot.edit_message_text,
                    chat_id=int_chat_id, message_id=buf.message_id,
                    text=primary_html, parse_mode="HTML",
                )
            except BadRequest as e:
                # Only fall back to plain text on actual HTML parse/format errors.
                # Network errors (TimedOut, NetworkError) should propagate immediately
                # to avoid doubling connection demand during pool exhaustion.
                if self._is_not_modified_error(e):
                    self.logger.debug("Final stream edit already applied for {}", chat_id)
                    self._stream_bufs.pop(chat_id, None)
                    return
                self.logger.debug("Final stream edit failed (HTML), trying plain: {}", e)
                # Fall back to raw markdown (not HTML) so users don't see raw tags.
                primary_plain = split_message(raw_text, TELEGRAM_MAX_MESSAGE_LEN)[0] if len(raw_text) > TELEGRAM_MAX_MESSAGE_LEN else raw_text
                try:
                    await self._call_with_retry(
                        app.bot.edit_message_text,
                        chat_id=int_chat_id, message_id=buf.message_id,
                        text=primary_plain,
                    )
                except Exception as e2:
                    if self._is_not_modified_error(e2):
                        self.logger.debug("Final stream plain edit already applied for {}", chat_id)
                    else:
                        self.logger.warning("Final stream edit failed: {}", e2)
                        raise  # Let ChannelManager handle retry
            for extra_html_chunk in extra_html_chunks:
                try:
                    await self._call_with_retry(
                        app.bot.send_message,
                        chat_id=int_chat_id, text=extra_html_chunk,
                        parse_mode="HTML",
                        **thread_kwargs,
                    )
                except Exception:
                    # Fall back to _send_text which handles HTML→plain gracefully.
                    await self._send_text(int_chat_id, extra_html_chunk)
            self._stream_bufs.pop(chat_id, None)
            return

        buf = self._stream_bufs.get(chat_id)
        if buf is None or (stream_id is not None and buf.stream_id is not None and buf.stream_id != stream_id):
            buf = _StreamBuf(stream_id=stream_id)
            self._stream_bufs[chat_id] = buf
        elif buf.stream_id is None:
            buf.stream_id = stream_id
        buf.text += delta

        if not buf.text.strip():
            return

        now = time.monotonic()
        stream_thread_kwargs: dict[str, int] = {}
        if message_thread_id := meta.get("message_thread_id"):
            stream_thread_kwargs["message_thread_id"] = message_thread_id
        if buf.message_id is None:
            preview = _strip_md_block(buf.text)
            try:
                sent = await self._call_with_retry(
                    app.bot.send_message,
                    chat_id=int_chat_id, text=preview,
                    **stream_thread_kwargs,
                )
                buf.message_id = sent.message_id
                buf.last_edit = now
            except Exception as e:
                self.logger.warning("Stream initial send failed: {}", e)
                raise  # Let ChannelManager handle retry
        elif (now - buf.last_edit) >= self.config.stream_edit_interval:
            if len(buf.text) > TELEGRAM_MAX_MESSAGE_LEN:
                await self._flush_stream_overflow(int_chat_id, buf, stream_thread_kwargs)
                buf.last_edit = now
                return
            preview = _strip_md_block(buf.text)
            try:
                await self._call_with_retry(
                    app.bot.edit_message_text,
                    chat_id=int_chat_id, message_id=buf.message_id,
                    text=preview,
                )
                buf.last_edit = now
            except Exception as e:
                if self._is_not_modified_error(e):
                    buf.last_edit = now
                    return
                self.logger.warning("Stream edit failed: {}", e)
                raise  # Let ChannelManager handle retry

    async def _flush_stream_overflow(
        self,
        chat_id: int,
        buf: "_StreamBuf",
        thread_kwargs: dict[str, int],
    ) -> None:
        """Split an oversized stream buffer mid-flight.

        Edits the current stream message with the first chunk, sends any
        intermediate chunks as standalone messages, then opens a new message
        for the tail so subsequent deltas continue streaming into it.
        """
        chunks = _split_telegram_markdown_html_chunks(buf.text, TELEGRAM_HTML_MAX_LEN)
        if len(chunks) <= 1:
            return
        app = self._require_app()
        first_markdown, first_html = chunks[0]
        try:
            await self._call_with_retry(
                app.bot.edit_message_text,
                chat_id=chat_id, message_id=buf.message_id,
                text=first_html,
                parse_mode="HTML",
            )
        except BadRequest as e:
            if not self._is_not_modified_error(e):
                self.logger.warning(
                    "Stream overflow HTML edit failed, falling back to plain text: {}", e
                )
                try:
                    await self._call_with_retry(
                        app.bot.edit_message_text,
                        chat_id=chat_id, message_id=buf.message_id,
                        text=first_markdown,
                    )
                except Exception as plain_error:
                    if not self._is_not_modified_error(plain_error):
                        self.logger.warning("Stream overflow plain edit failed: {}", plain_error)
                        raise
        except Exception as e:
            self.logger.warning("Stream overflow edit failed: {}", e)
            raise

        async def send_chunk(markdown: str, html: str) -> Any:
            try:
                return await self._call_with_retry(
                    app.bot.send_message,
                    chat_id=chat_id, text=html, parse_mode="HTML", **thread_kwargs,
                )
            except BadRequest as e:
                self.logger.warning(
                    "Stream overflow HTML send failed, falling back to plain text: {}", e
                )
                return await self._call_with_retry(
                    app.bot.send_message,
                    chat_id=chat_id, text=markdown, **thread_kwargs,
                )

        for markdown, html in chunks[1:-1]:
            await send_chunk(markdown, html)
        markdown_tail, tail_html = chunks[-1]
        sent = await send_chunk(markdown_tail, tail_html)
        buf.message_id = sent.message_id
        buf.text = markdown_tail

    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not update.message or not update.effective_user:
            return

        user = update.effective_user
        sender_id = self._sender_id(user)
        if not self.is_allowed(sender_id):
            await self._send_pairing_code_if_private(sender_id, update.message, user)
            return
        await update.message.reply_text(
            f"👋 Hi {user.first_name}! I'm nanobot.\n\n"
            "Send me a message and I'll respond!\n"
            "Type /help to see available commands."
        )

    async def _on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command for allowed users only."""
        if not update.message or not update.effective_user:
            return
        user = update.effective_user
        sender_id = self._sender_id(user)
        if not self.is_allowed(sender_id):
            await self._send_pairing_code_if_private(sender_id, update.message, user)
            return
        await update.message.reply_text(
            build_help_text()
            + "\n/image <prompt> — Generate an image with Puter."
            + "\n/image edit <instruction> — Edit an attached or replied-to image with Puter."
            + "\n/image_edit <instruction> — Telegram command-menu alias for /image edit."
        )

    def _miniapp_url(self) -> str:
        """Public URL for the large-file upload Mini App (/upload command)."""
        explicit = self.config.miniapp_url.strip()
        if explicit:
            return explicit
        webhook = self.config.webhook_url.strip()
        if webhook:
            parsed = urlparse(webhook)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}/upload"
        env_url = os.getenv("NANOBOT_MINIAPP_URL", "").strip()
        if env_url:
            return env_url
        return "https://minis-yzdb.onrender.com/upload"

    def _chatapp_url(self) -> str:
        """Public URL for the chat Mini App (/chat command).

        Same origin as the upload app but on the /app path; overridable via
        NANOBOT_CHATAPP_URL or by pointing miniapp_url at an explicit URL.
        """
        explicit = os.getenv("NANOBOT_CHATAPP_URL", "").strip()
        if explicit:
            return explicit
        webhook = self.config.webhook_url.strip()
        if webhook:
            parsed = urlparse(webhook)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}/app"
        return "https://minis-yzdb.onrender.com/app"

    async def _on_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /chat command — open the full AI-agent chat Mini App."""
        if not update.message or not update.effective_user:
            return
        user = update.effective_user
        sender_id = self._sender_id(user)
        if not self.is_allowed(sender_id):
            await self._send_pairing_code_if_private(sender_id, update.message, user)
            return

        url = self._chatapp_url()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🤖 Open Chat", web_app=WebAppInfo(url=url))]
        ])
        await update.message.reply_text(
            "Chat with me in the full app — send messages, run real tasks "
            "(files get analyzed on the sandbox), and attach files of any size.\n\n"
            "Large files upload straight to gofile.io from your phone, so nothing "
            "heavy crosses the server.",
            reply_markup=keyboard,
        )

    async def _on_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /upload command — open the large-file upload Mini App."""
        if not update.message or not update.effective_user:
            return
        user = update.effective_user
        sender_id = self._sender_id(user)
        if not self.is_allowed(sender_id):
            await self._send_pairing_code_if_private(sender_id, update.message, user)
            return

        url = self._miniapp_url()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Open Upload App", web_app=WebAppInfo(url=url))]
        ])
        await update.message.reply_text(
            "Ready to upload a large file (100 MB+).\n\n"
            "Tap the button below to open the upload app. Once your file finishes "
            "uploading, send a message here like: *analyze the file I just uploaded*.",
            reply_markup=keyboard,
        )

    @staticmethod
    def _sender_id(user: User) -> str:
        """Build sender_id with username for allowlist matching."""
        sid = str(user.id)
        return f"{sid}|{user.username}" if user.username else sid

    async def _send_pairing_code_if_private(
        self, sender_id: str, message: Message, user: User
    ) -> None:
        if message.chat.type != "private":
            return
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(message.chat_id),
            content="",
            metadata=self._build_message_metadata(message, user),
            is_dm=True,
        )

    async def _supabase_account(self, message: Message, user: User) -> dict[str, Any] | None:
        if not self._supabase.enabled:
            return None
        try:
            return await self._supabase.account_for(
                int(user.id),
                int(message.chat_id),
                username=user.username,
                first_name=user.first_name,
                last_name=getattr(user, "last_name", None),
            )
        except SupabaseAuthError as exc:
            self.logger.warning("Supabase Telegram account sync failed: {}", str(exc)[:200])
            return None

    @staticmethod
    def _is_image_edit_request(content: str) -> bool:
        parts = content.strip().split(None, 2)
        return len(parts) >= 2 and parts[0].split("@", 1)[0].lower() == "/image" and parts[1].lower() == "edit"

    @staticmethod
    def _image_path_to_puter_data_uri(path: str) -> str:
        media_root = get_media_dir("telegram").expanduser().resolve()
        image_path = Path(path).expanduser().resolve()
        try:
            image_path.relative_to(media_root)
        except ValueError as exc:
            raise SupabaseAuthError("Image edits accept only Telegram-uploaded images") from exc
        if not image_path.is_file():
            raise SupabaseAuthError("The Telegram image is no longer available")
        raw = image_path.read_bytes()
        if not raw or len(raw) > 12 * 1024 * 1024:
            raise SupabaseAuthError("Image edits accept images up to 12 MB")
        mime = detect_image_mime(raw)
        if not mime or not mime.startswith("image/"):
            raise SupabaseAuthError("Please attach a supported image (PNG, JPEG, GIF, or WebP)")
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    async def _handle_image_edit_command(
        self,
        message: Message,
        user: User,
        content: str,
        account: dict[str, Any] | None,
        *,
        media_paths: list[str] | None = None,
    ) -> bool:
        if account is None or not self._is_image_edit_request(content):
            return False
        if not self._supabase.is_authenticated(account):
            await message.reply_text("Please use /signin before using Puter image editing.")
            return True
        parts = content.strip().split(None, 2)
        prompt = parts[2].strip() if len(parts) >= 3 else ""
        if not prompt:
            await message.reply_text("Usage: /image edit <what to change> with an attached image or a reply to an image.")
            return True

        paths = list(media_paths or [])
        if not paths:
            reply = getattr(message, "reply_to_message", None)
            if reply is not None:
                reply_media, _ = await self._download_message_media(reply)
                paths.extend(reply_media)
        if not paths:
            pending = self._pending_attachments.pop(
                self._attachment_key(message.chat_id, self._sender_id(user)), None
            )
            if pending is not None:
                paths.extend(pending.media_paths)
        if not paths:
            await message.reply_text("Attach an image or reply to an image, then use /image edit <instruction>.")
            return True

        input_images = [self._image_path_to_puter_data_uri(path) for path in paths[:3]]
        await self._supabase.charge_step(
            account,
            f"telegram:puter-edit:{message.chat_id}:{message.message_id}",
            1,
        )
        result = await self._supabase.puter_edit_image(account, prompt, input_images)
        media_path = self._save_puter_media(
            str(result.get("data_uri") or ""),
            str(result.get("mime") or ""),
        )
        await self.send(OutboundMessage(
            channel="telegram",
            chat_id=str(message.chat_id),
            content="Puter image edit completed.",
            media=[str(media_path)],
            metadata=self._build_message_metadata(message, user),
        ))
        return True

    async def _handle_supabase_command(
        self, message: Message, user: User, content: str, account: dict[str, Any] | None,
    ) -> bool:
        if account is None or not content.startswith("/"):
            return False
        command = content.split(None, 1)[0].split("@", 1)[0].lower()
        try:
            if command in {"/signup", "/signin"}:
                if message.chat.type != "private":
                    await message.reply_text("Please start signup or signin in a private chat with me.")
                    return True
                flow = command[1:]
                await message.reply_text(await self._supabase.start_auth(account, flow))
                return True
            if command == "/signout":
                await message.reply_text(await self._supabase.signout(account))
                return True
            if command == "/credits":
                await message.reply_text(await self._supabase.credits(account))
                return True
            if command in {"/buy", "/credit"}:
                package_text = self._supabase.payment_packages_text(FLUTTERWAVE_PAYMENT_URL)
                if not self._supabase.is_authenticated(account):
                    package_text += "\n\nSign in with /signin before verifying payment or using purchased credits."
                await message.reply_text(package_text, disable_web_page_preview=False)
                return True
            if command in {"/verify-payment", "/verify_payment"}:
                if not self._supabase.is_authenticated(account):
                    await message.reply_text("Please use /signin before verifying a payment.")
                    return True
                parts = content.split()
                if len(parts) not in {2, 3}:
                    await message.reply_text(
                        "Usage: /verify-payment <Flutterwave reference> [transaction ID]"
                    )
                    return True
                result = await self._supabase.verify_payment(
                    account,
                    parts[1],
                    parts[2] if len(parts) == 3 else None,
                )
                await message.reply_text(
                    f"Payment verified successfully. {int(result.get('credits') or 0)} credits were added. "
                    f"Package: {result.get('pkg') or 'credit purchase'}."
                )
                return True
            if command in {"/image", "/video"}:
                if command == "/image" and self._is_image_edit_request(content):
                    return await self._handle_image_edit_command(message, user, content, account)
                if not self._supabase.is_authenticated(account):
                    await message.reply_text("Please use /signin before using Puter generation.")
                    return True
                prompt = content.partition(" ")[2].strip()
                if not prompt:
                    usage = "/image <prompt>" if command == "/image" else "/video <prompt>"
                    await message.reply_text(f"Usage: {usage}")
                    return True
                action = "generate_image" if command == "/image" else "generate_video"
                await self._supabase.charge_step(
                    account,
                    f"telegram:puter:{message.chat_id}:{message.message_id}",
                    1,
                )
                result = await self._supabase.puter_generate(account, action, prompt)
                media_path = self._save_puter_media(
                    str(result.get("data_uri") or ""),
                    str(result.get("mime") or ""),
                )
                await self.send(OutboundMessage(
                    channel="telegram",
                    chat_id=str(message.chat_id),
                    content="Puter generation completed.",
                    media=[str(media_path)],
                    metadata=self._build_message_metadata(message, user),
                ))
                return True
            if command == "/cancel":
                await self._supabase.save_state(int(account["telegram_user_id"]), None)
                await message.reply_text("The current Supabase authentication conversation was cancelled. Your linked account was not changed.")
                return True
        except SupabaseAuthError as exc:
            await message.reply_text(str(exc)[:1000])
            return True
        return False

    async def _supabase_auth_continuation(
        self, message: Message, content: str, account: dict[str, Any] | None,
    ) -> bool:
        if account is None or message.chat.type != "private" or not content or content.startswith("/"):
            return False
        if self._supabase.auth_state(account) is None:
            return False
        try:
            response = await self._supabase.handle_auth_message(account, content)
        except SupabaseAuthError as exc:
            response = str(exc)[:1000]
        if response:
            await message.reply_text(response)
        return True

    async def _require_supabase_auth(
        self, message: Message, account: dict[str, Any] | None,
    ) -> bool:
        if not self._supabase.enabled:
            return True
        if account is not None and self._supabase.is_authenticated(account):
            if await self._supabase.session_is_usable(account):
                return True
            self.logger.info(
                "Cleared an undecryptable Telegram session for user {}",
                account.get("telegram_user_id", "unknown"),
            )
        if message.chat.type == "private":
            await message.reply_text("Please use /signup to create an account or /signin to access your existing account before sending tasks.")
        else:
            await message.reply_text("Please message me privately and use /signup or /signin before sending a task here.")
        return False

    @staticmethod
    def _derive_topic_session_key(message: Message) -> str | None:
        """Derive topic-scoped session key for Telegram chats with threads."""
        message_thread_id = getattr(message, "message_thread_id", None)
        if message_thread_id is None:
            return None
        return f"telegram:{message.chat_id}:topic:{message_thread_id}"

    @staticmethod
    def _build_message_metadata(message: Message, user: User) -> dict[str, Any]:
        """Build common Telegram inbound metadata payload."""
        reply_to = getattr(message, "reply_to_message", None)
        return {
            "message_id": message.message_id,
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "is_group": message.chat.type != "private",
            "message_thread_id": getattr(message, "message_thread_id", None),
            "is_forum": bool(getattr(message.chat, "is_forum", False)),
            "reply_to_message_id": getattr(reply_to, "message_id", None) if reply_to else None,
        }

    @staticmethod
    def _add_verified_admin_context(
        metadata: dict[str, Any], account: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Attach model-only administrator guidance from verified account data."""
        enriched = dict(metadata)
        if not account or not account.get("agentx_user_id"):
            return enriched
        email = str(account.get("auth_email") or "").strip().lower()
        if email != MINIS_BOT_ADMIN_EMAIL:
            return enriched
        content = wrap_runtime_context_lines([
            "The current Telegram sender is the verified Minis Bot administrator.",
            f"Verified administrator account: {MINIS_BOT_ADMIN_EMAIL}",
            "Address the administrator respectfully and follow legitimate instructions within safety, privacy, authorization, and platform boundaries.",
            "Administrator status does not authorize credential exposure, unauthorized access, harmful activity, or bypassing security controls.",
        ])
        existing = enriched.get(RUNTIME_CONTEXT_INPUT_META)
        blocks = list(existing) if isinstance(existing, list) else []
        blocks.append(RuntimeContextBlock(source="telegram_verified_admin", content=content))
        enriched[RUNTIME_CONTEXT_INPUT_META] = blocks
        return enriched

    async def _extract_reply_context(self, message: Message) -> str | None:
        """Extract text from the message being replied to, if any."""
        reply = getattr(message, "reply_to_message", None)
        if not reply:
            return None
        text = getattr(reply, "text", None) or getattr(reply, "caption", None) or ""
        if len(text) > TELEGRAM_REPLY_CONTEXT_MAX_LEN:
            text = text[:TELEGRAM_REPLY_CONTEXT_MAX_LEN] + "..."

        if not text:
            return None

        bot_id, _ = await self._ensure_bot_identity()
        reply_user = getattr(reply, "from_user", None)

        if bot_id and reply_user and getattr(reply_user, "id", None) == bot_id:
            return f"[Reply to bot: {text}]"
        elif reply_user and getattr(reply_user, "username", None):
            return f"[Reply to @{reply_user.username}: {text}]"
        elif reply_user and getattr(reply_user, "first_name", None):
            return f"[Reply to {reply_user.first_name}: {text}]"
        else:
            return f"[Reply to: {text}]"

    async def _download_message_media(
        self, msg: Message, *, add_failure_content: bool = False
    ) -> tuple[list[str], list[str]]:
        """Download media from a message (current or reply). Returns (media_paths, content_parts)."""
        media_file = None
        media_type = None
        if getattr(msg, "photo", None):
            media_file = msg.photo[-1]
            media_type = "image"
        elif getattr(msg, "voice", None):
            media_file = msg.voice
            media_type = "voice"
        elif getattr(msg, "audio", None):
            media_file = msg.audio
            media_type = "audio"
        elif getattr(msg, "document", None):
            media_file = msg.document
            media_type = "file"
        elif getattr(msg, "video", None):
            media_file = msg.video
            media_type = "video"
        elif getattr(msg, "video_note", None):
            media_file = msg.video_note
            media_type = "video"
        elif getattr(msg, "animation", None):
            media_file = msg.animation
            media_type = "animation"
        if not media_file or not self._app:
            return [], []
        try:
            file = await self._app.bot.get_file(media_file.file_id)
            ext = self._get_extension(
                cast(str, media_type),
                getattr(media_file, "mime_type", None),
                getattr(media_file, "file_name", None),
            )
            media_dir = get_media_dir("telegram")
            unique_id = getattr(media_file, "file_unique_id", media_file.file_id)
            file_path = media_dir / f"{unique_id}{ext}"
            await file.download_to_drive(str(file_path))
            path_str = str(file_path)
            if media_type in ("voice", "audio"):
                transcription = await self.transcribe_audio(file_path)
                if transcription:
                    self.logger.info("Transcribed {}: {}...", media_type, transcription[:50])
                    return [path_str], [f"[transcription: {transcription}]"]
                return [path_str], [f"[{media_type}: {path_str}]"]
            return [path_str], [f"[{media_type}: {path_str}]"]
        except Exception as e:
            self.logger.warning("Failed to download message media: {}", e)
            if not add_failure_content:
                return [], []
            # Telegram refuses get_file() for >= 20 MiB. Surface a clear, actionable
            # note so the user opens the large-file upload Mini App instead of
            # staring at a generic "download failed".
            if isinstance(e, Exception) and _is_telegram_too_big_error(e):
                return [], ["[file: too large for Telegram's 20 MB bot download limit — use /upload]"]
            return [], [f"[{media_type}: download failed]"]

    @staticmethod
    def _media_file_too_big(msg: Message) -> bool:
        """Return True when the message carries a file too large for get_file.

        Telegram clips ``file_size`` to the 20 MiB bot-download ceiling for any
        file >= that size, so ``file_size >= TELEGRAM_BOT_DOWNLOAD_LIMIT`` means
        the file cannot be fetched server-side and the user should use the
        large-file upload Mini App instead.
        """
        media_file = None
        if getattr(msg, "photo", None):
            media_file = msg.photo[-1]
        elif getattr(msg, "voice", None):
            media_file = msg.voice
        elif getattr(msg, "audio", None):
            media_file = msg.audio
        elif getattr(msg, "document", None):
            media_file = msg.document
        elif getattr(msg, "video", None):
            media_file = msg.video
        elif getattr(msg, "video_note", None):
            media_file = msg.video_note
        elif getattr(msg, "animation", None):
            media_file = msg.animation
        if media_file is None:
            return False
        try:
            size = int(getattr(media_file, "file_size") or 0)
        except (TypeError, ValueError):
            return False
        return size >= TELEGRAM_BOT_DOWNLOAD_LIMIT

    async def _offer_upload_app(self, message: Message) -> None:
        """Reply with the large-file Upload App button when a file is too big.

        Downloads >= 20 MiB are impossible through the Bot API (Telegram's hard
        cap). Route the user to the existing Mini App, which streams the file
        straight from their browser to gofile.io, bypassing both the 20 MiB
        limit and the Render-hosted bot entirely.
        """
        url = self._miniapp_url()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Open Upload App", web_app=WebAppInfo(url=url))]
        ])
        await message.reply_text(
            "That file is too large to fetch through Telegram's bot download "
            "limit (20 MB).\n\n"
            "Tap the button below to open the upload app and send the file "
            "straight to gofile.io. When it finishes, just say: "
            "*analyze the file I just uploaded*.",
            reply_markup=keyboard,
        )

    async def _ensure_bot_identity(self) -> tuple[int | None, str | None]:
        """Load bot identity once and reuse it for mention/reply checks."""
        if self._bot_user_id is not None or self._bot_username is not None:
            return self._bot_user_id, self._bot_username
        if not self._app:
            return None, None
        bot_info = await self._app.bot.get_me()
        self._bot_user_id = getattr(bot_info, "id", None)
        self._bot_username = getattr(bot_info, "username", None)
        return self._bot_user_id, self._bot_username

    @staticmethod
    def _has_mention_entity(
        text: str,
        entities: list[MessageEntity] | None,
        bot_username: str,
        bot_id: int | None,
    ) -> bool:
        """Check Telegram mention entities against the bot username."""
        handle = f"@{bot_username}".lower()
        for entity in entities or []:
            entity_type = getattr(entity, "type", None)
            if entity_type == "text_mention":
                user = getattr(entity, "user", None)
                if user is not None and bot_id is not None and getattr(user, "id", None) == bot_id:
                    return True
                continue
            if entity_type != "mention":
                continue
            offset = getattr(entity, "offset", None)
            length = getattr(entity, "length", None)
            if offset is None or length is None:
                continue
            if text[offset : offset + length].lower() == handle:
                return True
        return handle in text.lower()

    async def _is_group_message_for_bot(self, message: Message) -> bool:
        """Allow group messages when policy is open, @mentioned, or replying to the bot."""
        if message.chat.type == "private" or self.config.group_policy == "open":
            return True

        bot_id, bot_username = await self._ensure_bot_identity()
        if bot_username:
            text = message.text or ""
            caption = message.caption or ""
            if self._has_mention_entity(
                text,
                getattr(message, "entities", None),
                bot_username,
                bot_id,
            ):
                return True
            if self._has_mention_entity(
                caption,
                getattr(message, "caption_entities", None),
                bot_username,
                bot_id,
            ):
                return True

        reply_user = getattr(getattr(message, "reply_to_message", None), "from_user", None)
        return bool(bot_id and reply_user and reply_user.id == bot_id)

    def _remember_thread_context(self, message: Message) -> None:
        """Cache Telegram thread context by chat/message id for follow-up replies."""
        message_thread_id = getattr(message, "message_thread_id", None)
        if message_thread_id is None:
            return
        key = (str(message.chat_id), message.message_id)
        self._message_threads[key] = message_thread_id
        if len(self._message_threads) > 1000:
            self._message_threads.pop(next(iter(self._message_threads)))

    @staticmethod
    def _queue_key_for_message(message: Message) -> str:
        """Return the final nanobot session key used for ordered Telegram ingress."""
        return TelegramChannel._derive_topic_session_key(message) or f"telegram:{message.chat_id}"

    @staticmethod
    def _sort_key_for_update(update: Update) -> tuple[int, int]:
        """Sort by chat message id first, then Telegram update id."""
        message = getattr(update, "message", None)
        message_id = int(getattr(message, "message_id", 0) or 0)
        update_id = int(getattr(update, "update_id", 0) or 0)
        return (message_id, update_id)

    def _enqueue_ordered_update(
        self,
        *,
        kind: Literal["command", "message"],
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Stage a Telegram update behind a short per-session reorder window."""
        message = update.message
        if message is None:
            return
        key = self._queue_key_for_message(message)
        self._inbound_buffers.setdefault(key, []).append(
            _QueuedTelegramUpdate(
                kind=kind,
                update=update,
                context=context,
                sort_key=self._sort_key_for_update(update),
            )
        )
        if key not in self._inbound_workers:
            self._inbound_workers[key] = asyncio.create_task(
                self._drain_ordered_updates(key)
            )

    async def _drain_ordered_updates(self, key: str) -> None:
        """Drain one Telegram session buffer in stable message order."""
        try:
            while self._running:
                await asyncio.sleep(0.2)
                batch = self._inbound_buffers.get(key, [])
                if not batch:
                    break
                self._inbound_buffers[key] = []
                batch.sort(key=lambda item: item.sort_key)
                for item in batch:
                    try:
                        if item.kind == "command":
                            await self._process_forward_command(item.update, item.context)
                        else:
                            await self._process_message_update(item.update, item.context)
                    except Exception as e:
                        self.logger.warning(
                            "Telegram queued update handling failed for {}: {}",
                            key,
                            e,
                        )
            if not self._inbound_buffers.get(key):
                self._inbound_buffers.pop(key, None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.warning("Telegram ordered update worker failed for {}: {}", key, e)
        finally:
            if not self._inbound_buffers.get(key):
                self._inbound_workers.pop(key, None)

    async def _forward_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward slash commands to the bus for unified handling in AgentLoop."""
        if not update.message or not update.effective_user:
            return
        if not self._running:
            await self._process_forward_command(update, context)
            return
        self._enqueue_ordered_update(kind="command", update=update, context=context)

    async def _process_forward_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process a queued slash command."""
        message = update.message
        user = update.effective_user
        if message is None or user is None:
            return
        sender_id = self._sender_id(user)
        record_telegram_user(
            sender_id=sender_id,
            chat_id=str(message.chat_id),
            username=user.username,
            first_name=user.first_name,
            last_name=getattr(user, "last_name", None),
            is_bot=getattr(user, "is_bot", False),
        )
        if not self.is_allowed(sender_id):
            await self._send_pairing_code_if_private(sender_id, message, user)
            return
        self._remember_thread_context(message)

        # Strip @bot_username suffix if present
        content = message.text or ""
        if content.startswith("/") and "@" in content:
            cmd_part, *rest = content.split(" ", 1)
            cmd_part = cmd_part.split("@")[0]
            content = f"{cmd_part} {rest[0]}" if rest else cmd_part
        content = self._normalize_telegram_command(content)
        account = await self._supabase_account(message, user)
        command_name = content.split(None, 1)[0].split("@", 1)[0].lower()
        if command_name in {"/cancel", "/discard"}:
            discarded = self._discard_pending_attachments(
                self._attachment_key(message.chat_id, sender_id)
            )
            if command_name == "/discard":
                await message.reply_text(
                    "Pending attachment(s) discarded." if discarded else "There are no pending attachments to discard."
                )
                return
        if await self._handle_supabase_command(message, user, content, account):
            return
        if await self._supabase_auth_continuation(message, content, account):
            return
        if not await self._require_supabase_auth(message, account):
            return

        metadata = self._build_message_metadata(message, user)
        if account and account.get("agentx_user_id"):
            metadata["supabase_user_id"] = str(account["agentx_user_id"])
        metadata = self._add_verified_admin_context(metadata, account)
        await self._record_question_history(
            account,
            chat_id=int(message.chat_id),
            message_id=metadata.get("message_id"),
            question=content,
            has_attachment=False,
        )
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(message.chat_id),
            content=content,
            metadata=metadata,
            session_key=self._derive_topic_session_key(message),
            is_dm=message.chat.type == "private",
        )

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return
        if not self._running:
            await self._process_message_update(update, context)
            return
        self._enqueue_ordered_update(kind="message", update=update, context=context)

    async def _process_message_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process a queued Telegram message update."""

        message = update.message
        user = update.effective_user
        if message is None or user is None:
            return
        chat_id = message.chat_id
        sender_id = self._sender_id(user)
        record_telegram_user(
            sender_id=sender_id,
            chat_id=str(chat_id),
            username=user.username,
            first_name=user.first_name,
            last_name=getattr(user, "last_name", None),
            is_bot=getattr(user, "is_bot", False),
        )
        if not self.is_allowed(sender_id):
            await self._send_pairing_code_if_private(sender_id, message, user)
            return
        self._remember_thread_context(message)

        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id

        if not await self._is_group_message_for_bot(message):
            return

        account = await self._supabase_account(message, user)
        if await self._supabase_auth_continuation(message, message.text or message.caption or "", account):
            return
        if not await self._require_supabase_auth(message, account):
            return

        # Build content from text and/or media
        content_parts: list[str] = []
        media_paths: list[str] = []

        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)

        # Location content
        if message.location:
            lat = message.location.latitude
            lon = message.location.longitude
            content_parts.append(f"[location: {lat}, {lon}]")

        # Files >= 20 MiB cannot be downloaded via the Bot API (Telegram's hard
        # cap). Route the user straight to the large-file upload Mini App instead
        # of attempting a doomed get_file() that would just fail and waste a hop.
        if self._media_file_too_big(message):
            await self._offer_upload_app(message)
            return

        # Download current message media
        current_media_paths, current_media_parts = await self._download_message_media(
            message, add_failure_content=True
        )
        media_paths.extend(current_media_paths)
        content_parts.extend(current_media_parts)
        if current_media_paths:
            self.logger.debug("Downloaded message media to {}", current_media_paths[0])

        # Reply context: text and/or media from the replied-to message
        reply = getattr(message, "reply_to_message", None)
        if reply is not None:
            reply_ctx = await self._extract_reply_context(message)
            reply_media, reply_media_parts = await self._download_message_media(reply)
            if reply_media:
                media_paths = reply_media + media_paths
                self.logger.debug("Attached replied-to media: {}", reply_media[0])
            tag = reply_ctx or (f"[Reply to: {reply_media_parts[0]}]" if reply_media_parts else None)
            if tag:
                content_parts.insert(0, tag)
        content = "\n".join(content_parts) if content_parts else "[empty message]"
        attachment_context = "\n".join(
            part for part in content_parts
            if not part.startswith(("[image:", "[file:", "[video:", "[voice:", "[audio:", "[animation:"))
        )

        self.logger.debug("message from {}: {}...", sender_id, content[:50])

        str_chat_id = str(chat_id)
        metadata = self._build_message_metadata(message, user)
        if account and account.get("agentx_user_id"):
            metadata["supabase_user_id"] = str(account["agentx_user_id"])
        metadata = self._add_verified_admin_context(metadata, account)
        session_key = self._derive_topic_session_key(message)

        # Telegram media groups: buffer briefly, forward as one aggregated turn.
        if media_group_id := getattr(message, "media_group_id", None):
            key = f"{str_chat_id}:{media_group_id}"
            if key not in self._media_group_buffers:
                self._media_group_buffers[key] = {
                    "sender_id": sender_id, "chat_id": str_chat_id,
                    "telegram_user_id": int(getattr(user, "id", sender_id)),
                    "contents": [], "media": [],
                    "metadata": metadata,
                    "session_key": session_key,
                }
                self._start_typing(str_chat_id)
                await self._add_reaction(str_chat_id, message.message_id, self.config.react_emoji)
            buf = self._media_group_buffers[key]
            if content and content != "[empty message]":
                buf["contents"].append(content)
            buf["media"].extend(media_paths)
            if key not in self._media_group_tasks:
                self._media_group_tasks[key] = asyncio.create_task(self._flush_media_group(key))
            return

        if self._is_image_edit_request(content) and media_paths:
            await self._handle_image_edit_command(
                message,
                user,
                content,
                account,
                media_paths=media_paths,
            )
            return

        content, media_paths, waiting_for_intent = await self._resolve_pending_attachments(
            sender_id=sender_id,
            chat_id=str_chat_id,
            content=content,
            attachment_context=attachment_context,
            media_paths=media_paths,
            metadata=metadata,
        )
        if waiting_for_intent:
            return

        metadata = deliberate_task_metadata(
            metadata,
            content=content,
            media=media_paths,
        )

        # Start typing indicator before processing
        self._start_typing(str_chat_id)
        await self._add_reaction(str_chat_id, message.message_id, self.config.react_emoji)

        await self._record_question_history(
            account,
            chat_id=chat_id,
            message_id=metadata.get("message_id"),
            question=self._question_for_history(content),
            has_attachment=bool(media_paths),
        )

        # Forward to the message bus
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str_chat_id,
            content=content,
            media=media_paths,
            metadata=metadata,
            session_key=session_key,
        )

    async def _flush_media_group(self, key: str) -> None:
        """Wait briefly, then forward buffered media-group as one turn."""
        try:
            await asyncio.sleep(0.6)
            if not (buf := self._media_group_buffers.pop(key, None)):
                return
            content = "\n".join(buf["contents"]) or "[empty message]"
            media = list(dict.fromkeys(buf["media"]))
            attachment_context = "\n".join(
                part for part in buf["contents"]
                if not part.startswith(("[image:", "[file:", "[video:", "[voice:", "[audio:", "[animation:"))
            )
            content, media, waiting_for_intent = await self._resolve_pending_attachments(
                sender_id=buf["sender_id"],
                chat_id=buf["chat_id"],
                content=content,
                attachment_context=attachment_context,
                media_paths=media,
                metadata=buf["metadata"],
            )
            if waiting_for_intent:
                return
            buf["metadata"] = deliberate_task_metadata(
                buf["metadata"],
                content=content,
                media=media,
            )
            await self._record_question_history(
                {"telegram_user_id": buf["telegram_user_id"]},
                chat_id=int(buf["chat_id"]),
                message_id=buf["metadata"].get("message_id"),
                question=self._question_for_history(content),
                has_attachment=bool(media),
            )
            await self._handle_message(
                sender_id=buf["sender_id"], chat_id=buf["chat_id"],
                content=content, media=media,
                metadata=buf["metadata"],
                session_key=buf.get("session_key"),
            )
        finally:
            self._media_group_tasks.pop(key, None)

    @staticmethod
    def _question_for_history(content: str) -> str:
        marker = "\n\nUser instruction:"
        if marker in content:
            return content.rsplit(marker, 1)[1].strip()
        return content.strip()

    async def _record_question_history(
        self,
        account: dict[str, Any],
        *,
        chat_id: int,
        message_id: Any,
        question: str,
        has_attachment: bool,
    ) -> None:
        try:
            await self._supabase.record_telegram_question(
                account,
                chat_id=int(chat_id),
                message_id=int(message_id) if message_id is not None else None,
                question=question,
                has_attachment=has_attachment,
            )
        except SupabaseAuthError as exc:
            self.logger.warning("Telegram question telemetry failed: {}", exc)

    @staticmethod
    def _save_puter_media(data_uri: str, mime: str) -> Path:
        """Persist a bounded Puter data URI for Telegram's media sender."""
        if not data_uri.startswith("data:") or "," not in data_uri:
            raise SupabaseAuthError("Puter returned an unsupported media result")
        header, encoded = data_uri.split(",", 1)
        try:
            payload = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise SupabaseAuthError("Puter returned invalid media data") from exc
        if not payload or len(payload) > 25 * 1024 * 1024:
            raise SupabaseAuthError("Puter returned an empty or oversized media result")
        content_type = (mime or header[5:].split(";", 1)[0]).lower()
        extension = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
        }.get(content_type, ".mp4" if content_type.startswith("video/") else ".png")
        path = get_media_dir("telegram") / f"puter_{uuid4().hex}{extension}"
        path.write_bytes(payload)
        return path

    @staticmethod
    def _attachment_key(chat_id: str | int, sender_id: str) -> str:
        """Keep pending attachments isolated per Telegram chat and sender."""
        return f"{chat_id}:{sender_id}"

    @staticmethod
    def _delete_pending_media(pending: _PendingTelegramAttachments) -> None:
        """Remove downloaded files that were discarded before agent processing."""
        for path in pending.media_paths:
            with suppress(OSError):
                Path(path).unlink()

    def _discard_pending_attachments(self, key: str) -> bool:
        pending = self._pending_attachments.pop(key, None)
        if pending is None:
            return False
        self._delete_pending_media(pending)
        return True

    def _prune_pending_attachments(self) -> None:
        now = time.monotonic()
        for key, pending in list(self._pending_attachments.items()):
            if pending.expires_at <= now:
                self._discard_pending_attachments(key)

    async def _send_attachment_prompt(
        self, *, chat_id: str, metadata: dict[str, Any], media_paths: list[str], context: str,
    ) -> None:
        """Ask what the user wants before attachments enter the agent workflow.

        Forwarded files are staged to gofile.io and their public link is shown
        in the prompt so the user sees a ready-to-use link before typing their
        instruction. Staging is best-effort and never blocks the prompt. When
        ``gofile_staging`` is disabled the file stays local-only so Render's
        outbound upload bandwidth is not spent re-crossing it to gofile.io.
        """
        gofile_links: list[str] = []
        if self.config.gofile_staging:
            try:
                for media_path in list(media_paths)[:3]:
                    path = Path(media_path)
                    if not path.is_file():
                        continue
                    # Stream the file into the multipart body instead of loading
                    # the whole blob into RAM (large files OOM low-memory hosts).
                    with path.open("rb") as fp:
                        result = await upload_gofile_stream(
                            fp, filename=path.name, timeout_seconds=120
                        )
                    gofile_links.append(str(result["url"]))
            except Exception as exc:  # noqa: BLE001 - staging is best-effort
                self.logger.warning("gofile staging failed: {}", exc)

        names = ", ".join(Path(path).name for path in media_paths)
        count = len(media_paths)
        noun = "attachment" if count == 1 else "attachments"
        prompt = f"I received {count} {noun}: {names}.\n\n"
        if gofile_links:
            prompt += "Ready link(s) you can also reuse:\n" + "\n".join(
                f"{url}" for url in gofile_links
            ) + "\n\n"
        prompt += (
            "What would you like me to do with it? Reply with your instruction, "
            "for example: `analyze this`, `summarize it`, `edit the code`, or `upload it to the workspace`."
            "\n\nReply /cancel or /discard to remove the pending attachment(s)."
        )
        if context:
            prompt += f"\n\nCaption/note received: {context[:1000]}"
        reply_to_message_id = metadata.get("message_id")
        reply_params = None
        if self.config.reply_to_message and reply_to_message_id:
            reply_params = ReplyParameters(
                message_id=int(reply_to_message_id),
                allow_sending_without_reply=True,
            )
        thread_kwargs: dict[str, int] = {}
        if metadata.get("message_thread_id") is not None:
            thread_kwargs["message_thread_id"] = int(metadata["message_thread_id"])
        await self._send_text(int(chat_id), prompt, reply_params, thread_kwargs)

    async def _resolve_pending_attachments(
        self, *, sender_id: str, chat_id: str, content: str,
        attachment_context: str, media_paths: list[str], metadata: dict[str, Any],
    ) -> tuple[str, list[str], bool]:
        """Hold new attachments or resume a held attachment with the next instruction."""
        self._prune_pending_attachments()
        key = self._attachment_key(chat_id, sender_id)
        pending = self._pending_attachments.get(key)
        if media_paths:
            if pending is not None:
                pending.media_paths.extend(media_paths)
                if attachment_context:
                    pending.context = f"{pending.context}\n{attachment_context}".strip()
            else:
                pending = _PendingTelegramAttachments(
                    media_paths=list(media_paths),
                    context=attachment_context,
                    expires_at=time.monotonic() + ATTACHMENT_CONFIRMATION_TTL_SECONDS,
                )
                self._pending_attachments[key] = pending
            await self._send_attachment_prompt(
                chat_id=chat_id,
                metadata=metadata,
                media_paths=pending.media_paths,
                context=pending.context,
            )
            return "[empty message]", [], True
        if pending is None or content == "[empty message]":
            return content, media_paths, False
        self._pending_attachments.pop(key, None)
        instruction = content.strip()
        if pending.context:
            instruction = f"{pending.context}\n\nUser instruction: {instruction}"
        return instruction, pending.media_paths, False

    def _start_typing(self, chat_id: str) -> None:
        """Start sending 'typing...' indicator for a chat."""
        # Cancel any existing typing task for this chat
        self._stop_typing(chat_id)
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    def _stop_typing(self, chat_id: str) -> None:
        """Stop the typing indicator for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _add_reaction(self, chat_id: str, message_id: int, emoji: str) -> None:
        """Add emoji reaction to a message (best-effort, non-blocking)."""
        if not self._app or not emoji:
            return
        try:
            await self._app.bot.set_message_reaction(
                chat_id=int(chat_id),
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
        except Exception as e:
            self.logger.debug("reaction failed: {}", e)

    async def _remove_reaction(self, chat_id: str, message_id: int) -> None:
        """Remove emoji reaction from a message (best-effort, non-blocking)."""
        if not self._app:
            return
        try:
            await self._app.bot.set_message_reaction(
                chat_id=int(chat_id),
                message_id=message_id,
                reaction=[],
            )
        except Exception as e:
            self.logger.debug("reaction removal failed: {}", e)

    async def _typing_loop(self, chat_id: str) -> None:
        """Repeatedly send 'typing' action until cancelled."""
        try:
            with suppress(asyncio.CancelledError):
                while self._app:
                    await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
                    await asyncio.sleep(4)
        except Exception as e:
            self.logger.debug("Typing indicator stopped for {}: {}", chat_id, e)

    @staticmethod
    def _format_telegram_error(exc: Exception | None) -> str:
        """Return a short, readable error summary for logs."""
        if exc is None:
            return "None"
        text = str(exc).strip()
        if text:
            return text
        if exc.__cause__ is not None:
            cause = exc.__cause__
            cause_text = str(cause).strip()
            if cause_text:
                return f"{exc.__class__.__name__} ({cause_text})"
            return f"{exc.__class__.__name__} ({cause.__class__.__name__})"
        return exc.__class__.__name__

    def _on_polling_error(self, exc: Exception) -> None:
        """Keep long-polling network failures to a single readable line."""
        summary = self._format_telegram_error(exc)
        if isinstance(exc, (NetworkError, TimedOut)):
            self.logger.warning("polling network issue: {}", summary)
        else:
            self.logger.error("polling error: {}", summary)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log polling / handler errors instead of silently swallowing them."""
        summary = self._format_telegram_error(context.error)

        if isinstance(context.error, (NetworkError, TimedOut)):
            self.logger.warning("network issue: {}", summary)
        else:
            self.logger.error("error: {}", summary)

    def _get_extension(
        self,
        media_type: str,
        mime_type: str | None,
        filename: str | None = None,
    ) -> str:
        """Get file extension based on media type or original filename."""
        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "image/webp": ".webp",
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
                "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
                "video/x-matroska": ".mkv", "video/3gpp": ".3gp",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]

        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "video": ".mp4", "file": ""}
        if ext := type_map.get(media_type, ""):
            return ext

        if filename:
            return "".join(Path(filename).suffixes)

        return ""

    def _build_keyboard(self, buttons: list[list[str]]) -> InlineKeyboardMarkup | None:
        """Build inline keyboard markup if inline_keyboards is enabled."""
        if not buttons or not self.config.inline_keyboards:
            return None
        keyboard = [
            [InlineKeyboardButton(label, callback_data=self._safe_callback_data(label)) for label in row]
            for row in buttons
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def _safe_callback_data(label: str) -> str:
        # Telegram caps callback_data at 64 bytes UTF-8; truncate at a char boundary so the keyboard still sends.
        encoded = label.encode("utf-8")
        if len(encoded) <= 64:
            return label
        return encoded[:64].decode("utf-8", errors="ignore")

    @staticmethod
    def _buttons_as_text(buttons: list[list[str]]) -> str:
        # Buttons are semantic options; when we can't render a keyboard, the user still needs to see them.
        return "\n".join(" ".join(f"[{label}]" for label in row) for row in buttons if row)

    async def _on_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline keyboard button clicks (callback queries)."""
        if not update.callback_query or not update.effective_user:
            return
        query = update.callback_query
        user = update.effective_user
        query_message = query.message
        chat_id = query_message.chat.id if query_message else None
        sender_id = self._sender_id(user)
        if not chat_id:
            self.logger.warning("Callback query without chat_id")
            return
        if not self.is_allowed(sender_id):
            return
        button_label = query.data or ""
        await query.answer()
        if isinstance(query_message, Message):
            with suppress(Exception):
                await query_message.edit_reply_markup(reply_markup=None)
        self.logger.debug("Inline button tap from {}: {}", sender_id, button_label)
        self._start_typing(str(chat_id))
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(chat_id),
            content=button_label,
            metadata={
                "callback_query_id": query.id,
                "button_label": button_label,
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "is_callback": True,
            },
        )
