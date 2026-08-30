from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.runtime import TelegramChannel
from nanobot.supabase_auth import SupabaseAuthError


@pytest.fixture
def account() -> dict[str, str]:
    return {
        "telegram_user_id": "42",
        "agentx_user_id": "11111111-1111-1111-1111-111111111111",
        "session_token_ciphertext": "access",
        "session_token_iv": "iv",
        "refresh_token_ciphertext": "refresh",
        "refresh_token_iv": "iv",
    }


def make_message(text: str, message_id: int = 7):
    return SimpleNamespace(
        chat_id=-100123,
        message_id=message_id,
        chat=SimpleNamespace(type="private", is_forum=False),
        reply_text=AsyncMock(),
    )


def make_user():
    return SimpleNamespace(id=42, username="alice", first_name="Alice")


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/buy", "/credit"])
async def test_buy_displays_database_aligned_packages_and_checkout_url(account, command) -> None:
    channel = TelegramChannel({"allowFrom": ["*"]}, MessageBus())
    message = make_message(command)
    handled = await channel._handle_supabase_command(message, make_user(), command, account)

    assert handled is True
    text = message.reply_text.await_args.args[0]
    assert "Starter: 1000 credits" in text
    assert "Standard: 2000 credits" in text
    assert "Popular: 3500 credits" in text
    assert "Best Value: 7500 credits" in text
    assert "https://flutterwave.com/pay/yvbdgyf6awyf" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/verify-payment", "/verify_payment"])
async def test_verify_payment_delegates_to_existing_edge_function(account, command) -> None:
    channel = TelegramChannel({"allowFrom": ["*"]}, MessageBus())
    channel._supabase.verify_payment = AsyncMock(
        return_value={"ok": True, "credits": 2000, "pkg": "standard"}
    )
    message = make_message(f"{command} tx-ref 12345")

    handled = await channel._handle_supabase_command(
        message, make_user(), f"{command} tx-ref 12345", account
    )

    assert handled is True
    channel._supabase.verify_payment.assert_awaited_once_with(account, "tx-ref", "12345")
    assert "2000 credits were added" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_image_command_charges_one_step_and_sends_puter_result(monkeypatch, tmp_path, account) -> None:
    monkeypatch.setattr(
        "nanobot.channels.telegram.runtime.get_media_dir",
        lambda _channel=None: tmp_path,
    )
    channel = TelegramChannel({"allowFrom": ["*"]}, MessageBus())
    channel._supabase.charge_step = AsyncMock(return_value={"success": True})
    channel._supabase.puter_generate = AsyncMock(
        return_value={"ok": True, "mime": "image/png", "data_uri": "data:image/png;base64,AA=="}
    )
    channel.send = AsyncMock()
    message = make_message("/image a blue robot")

    handled = await channel._handle_supabase_command(
        message, make_user(), "/image a blue robot", account
    )

    assert handled is True
    channel._supabase.charge_step.assert_awaited_once_with(account, "telegram:puter:-100123:7", 1)
    channel._supabase.puter_generate.assert_awaited_once_with(
        account, "generate_image", "a blue robot"
    )
    outbound = channel.send.await_args.args[0]
    assert isinstance(outbound, OutboundMessage)
    assert outbound.content == "Puter generation completed."
    assert outbound.media[0].endswith(".png")
    assert (tmp_path / outbound.media[0].split("/")[-1]).read_bytes() == b"\x00"


@pytest.mark.asyncio
async def test_puter_failure_is_reported_without_sending_media(account) -> None:
    channel = TelegramChannel({"allowFrom": ["*"]}, MessageBus())
    channel._supabase.charge_step = AsyncMock(return_value={"success": True})
    channel._supabase.puter_generate = AsyncMock(
        side_effect=SupabaseAuthError("Puter is disabled by the administrator")
    )
    channel.send = AsyncMock()
    message = make_message("/image test")

    handled = await channel._handle_supabase_command(
        message, make_user(), "/image test", account
    )

    assert handled is True
    channel.send.assert_not_awaited()
    assert "Puter is disabled" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_image_edit_command_forwards_attached_image_and_sends_result(monkeypatch, tmp_path, account) -> None:
    monkeypatch.setattr(
        "nanobot.channels.telegram.runtime.get_media_dir",
        lambda _channel=None: tmp_path,
    )
    source = tmp_path / "source.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload")
    channel = TelegramChannel({"allowFrom": ["*"]}, MessageBus())
    channel._supabase.charge_step = AsyncMock(return_value={"success": True})
    channel._supabase.puter_edit_image = AsyncMock(
        return_value={"ok": True, "mime": "image/png", "data_uri": "data:image/png;base64,AA=="}
    )
    channel._download_message_media = AsyncMock(return_value=([str(source)], ["[image: source.png]"]))
    channel.send = AsyncMock()
    message = make_message("/image edit replace the sky with a sunset")
    message.reply_to_message = object()

    handled = await channel._handle_image_edit_command(
        message,
        make_user(),
        "/image edit replace the sky with a sunset",
        account,
    )

    assert handled is True
    channel._supabase.charge_step.assert_awaited_once_with(account, "telegram:puter-edit:-100123:7", 1)
    channel._supabase.puter_edit_image.assert_awaited_once()
    edit_args = channel._supabase.puter_edit_image.await_args.args
    assert edit_args[0] == account
    assert edit_args[1] == "replace the sky with a sunset"
    assert edit_args[2][0].startswith("data:image/png;base64,")
    outbound = channel.send.await_args.args[0]
    assert isinstance(outbound, OutboundMessage)
    assert outbound.content == "Puter image edit completed."
    assert outbound.media[0].endswith(".png")


@pytest.mark.asyncio
async def test_image_edit_command_requires_an_image(account) -> None:
    channel = TelegramChannel({"allowFrom": ["*"]}, MessageBus())
    channel._supabase.charge_step = AsyncMock(return_value={"success": True})
    channel._supabase.puter_edit_image = AsyncMock()
    message = make_message("/image edit make it brighter")
    message.reply_to_message = None

    handled = await channel._handle_image_edit_command(
        message,
        make_user(),
        "/image edit make it brighter",
        account,
    )

    assert handled is True
    channel._supabase.charge_step.assert_not_awaited()
    channel._supabase.puter_edit_image.assert_not_awaited()
    assert "Attach an image" in message.reply_text.await_args.args[0]
