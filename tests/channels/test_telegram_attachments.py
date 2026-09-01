from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.runtime import (
    TelegramChannel,
    TelegramConfig,
    _is_telegram_too_big_error,
    _PendingTelegramAttachments,
)


@pytest.fixture
def channel() -> TelegramChannel:
    instance = TelegramChannel(
        {"allowFrom": ["*"], "reply_to_message": False},
        MessageBus(),
    )
    instance._send_text = AsyncMock()  # type: ignore[method-assign]
    return instance


@pytest.mark.asyncio
async def test_attachment_upload_prompts_before_agent_dispatch(channel, tmp_path) -> None:
    image = tmp_path / "photo.png"
    source = tmp_path / "main.py"
    image.write_bytes(b"png")
    source.write_text("print('hello')")

    content, media, waiting = await channel._resolve_pending_attachments(
        sender_id="42",
        chat_id="1001",
        content="[image: photo.png]\nplease inspect this",
        attachment_context="please inspect this",
        media_paths=[str(image), str(source)],
        metadata={"message_id": 8, "message_thread_id": 12},
    )

    assert waiting is True
    assert content == "[empty message]"
    assert media == []
    prompt = channel._send_text.await_args.args[1]
    assert "photo.png" in prompt
    assert "main.py" in prompt
    assert "What would you like me to do" in prompt
    assert "message_id" not in prompt


@pytest.mark.asyncio
async def test_next_instruction_resumes_with_pending_files(channel, tmp_path) -> None:
    document = tmp_path / "report.pdf"
    document.write_bytes(b"pdf")

    await channel._resolve_pending_attachments(
        sender_id="42",
        chat_id="1001",
        content="[file: report.pdf]",
        attachment_context="",
        media_paths=[str(document)],
        metadata={},
    )

    content, media, waiting = await channel._resolve_pending_attachments(
        sender_id="42",
        chat_id="1001",
        content="summarize this and list the action items",
        attachment_context="",
        media_paths=[],
        metadata={},
    )

    assert waiting is False
    assert media == [str(document)]
    assert content == "summarize this and list the action items"
    assert channel._pending_attachments == {}


@pytest.mark.asyncio
async def test_pending_attachments_are_isolated_by_sender(channel, tmp_path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first")
    second.write_text("second")

    await channel._resolve_pending_attachments(
        sender_id="alice",
        chat_id="2002",
        content="[file: first.txt]",
        attachment_context="",
        media_paths=[str(first)],
        metadata={},
    )
    await channel._resolve_pending_attachments(
        sender_id="bob",
        chat_id="2002",
        content="[file: second.txt]",
        attachment_context="",
        media_paths=[str(second)],
        metadata={},
    )

    _, alice_media, _ = await channel._resolve_pending_attachments(
        sender_id="alice",
        chat_id="2002",
        content="use the first file",
        attachment_context="",
        media_paths=[],
        metadata={},
    )
    _, bob_media, _ = await channel._resolve_pending_attachments(
        sender_id="bob",
        chat_id="2002",
        content="use the second file",
        attachment_context="",
        media_paths=[],
        metadata={},
    )

    assert alice_media == [str(first)]
    assert bob_media == [str(second)]


def test_discard_pending_attachment_deletes_downloaded_file(channel, tmp_path) -> None:
    document = tmp_path / "discard-me.txt"
    document.write_text("temporary")
    key = channel._attachment_key("1001", "42")
    channel._pending_attachments[key] = _PendingTelegramAttachments(
        media_paths=[str(document)], context="", expires_at=time.monotonic() + 30
    )

    assert channel._discard_pending_attachments(key) is True
    assert not document.exists()
    assert channel._discard_pending_attachments(key) is False


@pytest.mark.asyncio
async def test_resolved_attachment_instruction_is_recorded_for_admin_review(channel) -> None:
    channel._supabase.record_telegram_question = AsyncMock()  # type: ignore[method-assign]

    await channel._record_question_history(
        {"telegram_user_id": 42},
        chat_id=1001,
        message_id=8,
        question=channel._question_for_history("caption\n\nUser instruction: summarize it"),
        has_attachment=True,
    )

    channel._supabase.record_telegram_question.assert_awaited_once_with(
        {"telegram_user_id": 42},
        chat_id=1001,
        message_id=8,
        question="summarize it",
        has_attachment=True,
    )


def test_expired_pending_attachment_is_pruned(channel, tmp_path) -> None:
    document = tmp_path / "expired.txt"
    document.write_text("temporary")
    key = channel._attachment_key("1001", "42")
    channel._pending_attachments[key] = _PendingTelegramAttachments(
        media_paths=[str(document)], context="", expires_at=time.monotonic() - 1
    )

    channel._prune_pending_attachments()

    assert key not in channel._pending_attachments
    assert not document.exists()


def test_is_telegram_too_big_error_matches_message_text() -> None:
    error = RuntimeError("400 Bad Request: File is too big")
    assert _is_telegram_too_big_error(error) is True


def test_is_telegram_too_big_error_is_robust_to_case() -> None:
    error = RuntimeError("bad message: FILE IS TOO BIG")
    assert _is_telegram_too_big_error(error) is True


def test_is_telegram_too_big_error_checks_attribute() -> None:
    error = RuntimeError("generic failure")
    error.file_is_too_big = True  # type: ignore[attr-defined]
    assert _is_telegram_too_big_error(error) is True


def test_is_telegram_too_big_error_rejects_other_errors() -> None:
    assert _is_telegram_too_big_error(RuntimeError("timed out")) is False
    assert _is_telegram_too_big_error(ValueError("file not found")) is False


class _FakeMediaFile:
    def __init__(self, size: int) -> None:
        self.file_size = size
        self.file_id = "fake-id"
        self.file_unique_id = "fake-unique"


class _FakeMessage:
    def __init__(self, document=None) -> None:
        self.document = document
        self.photo = None
        self.voice = None
        self.audio = None
        self.video = None
        self.video_note = None
        self.animation = None


def test_media_file_too_big_flags_at_the_download_ceiling() -> None:
    msg = _FakeMessage(document=_FakeMediaFile(20 * 1024 * 1024))
    assert TelegramChannel._media_file_too_big(msg) is True


def test_media_file_too_big_flags_files_above_the_ceiling() -> None:
    msg = _FakeMessage(document=_FakeMediaFile(150 * 1024 * 1024))
    assert TelegramChannel._media_file_too_big(msg) is True


def test_media_file_too_big_allows_small_files() -> None:
    msg = _FakeMessage(document=_FakeMediaFile(5 * 1024 * 1024))
    assert TelegramChannel._media_file_too_big(msg) is False


def test_media_file_too_big_ignores_messages_without_media() -> None:
    assert TelegramChannel._media_file_too_big(_FakeMessage()) is False


@pytest.mark.asyncio
async def test_chat_command_opens_chat_mini_app(channel, monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock as _AsyncMock

    monkeypatch.setenv("NANOBOT_CHATAPP_URL", "https://example.onrender.com/app")
    message = SimpleNamespace(
        reply_text=_AsyncMock(),
        chat=SimpleNamespace(type="private"),
    )
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=42, username="tester", first_name="T"),
    )
    await channel._on_chat(update, None)  # type: ignore[arg-type]

    message.reply_text.assert_awaited_once()
    kwargs = message.reply_text.await_args.kwargs
    keyboard = kwargs["reply_markup"]
    button = keyboard.inline_keyboard[0][0]
    assert button.web_app.url == "https://example.onrender.com/app"


def test_chatapp_url_derives_from_webhook_url() -> None:
    instance = TelegramChannel.__new__(TelegramChannel)
    instance.config = TelegramConfig(webhook_url="https://bot.example.com/telegram")
    import os

    os.environ.pop("NANOBOT_CHATAPP_URL", None)
    assert instance._chatapp_url() == "https://bot.example.com/app"
