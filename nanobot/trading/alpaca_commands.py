"""Telegram command handlers for /alpaca connect and /alpaca disconnect.

Stores per-user Alpaca API credentials in Supabase using the same
AES-GCM encryption pattern as the SupabaseAuth session store.
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from nanobot.supabase_auth import SupabaseAuth
from nanobot.trading.alpaca_credentials import AlpacaCredentialStore


_ALPACA_CONNECT_RE = re.compile(r"^/alpaca\s+(connect|disconnect|status)\b", re.IGNORECASE)
_API_KEY_RE = re.compile(r"^[A-Za-z0-9]{20}$")


def is_alpaca_command(text: str) -> bool:
    """Check if the message is an /alpaca slash command."""
    return bool(_ALPACA_CONNECT_RE.match(text.strip()))


async def handle_alpaca_command(
    account: dict[str, Any],
    text: str,
    chat_id: int,
    message_id: int | None = None,
) -> str:
    """Handle /alpaca connect, /alpaca disconnect, /alpaca status.

    Args:
        account: The Telegram account dict from SupabaseAuth.
        text: The full message text.
        chat_id: The Telegram chat ID.
        message_id: Optional message ID for reply context.

    Returns:
        Response text to send back to the user.
    """
    telegram_user_id = int(account.get("telegram_user_id", 0))
    if not telegram_user_id:
        return "Could not identify your Telegram account. Please /signin first."

    match = _ALPACA_CONNECT_RE.match(text.strip())
    if not match:
        return (
            "Usage:\n"
            "  /alpaca connect — Link your Alpaca paper account\n"
            "  /alpaca disconnect — Remove your Alpaca credentials\n"
            "  /alpaca status — Check if your Alpaca account is connected"
        )

    subcommand = match.group(1).lower()

    if subcommand == "status":
        return await _handle_status(telegram_user_id)
    elif subcommand == "disconnect":
        return await _handle_disconnect(telegram_user_id)
    elif subcommand == "connect":
        return await _handle_connect(account, text, telegram_user_id)
    return "Unknown /alpaca subcommand."


async def _handle_status(telegram_user_id: int) -> str:
    store = AlpacaCredentialStore()
    if not store.enabled:
        return (
            "⚠️ Supabase is not configured on this server. "
            "The bot administrator needs to set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY."
        )
    try:
        creds = await store.get_credentials(telegram_user_id)
        if creds:
            return (
                "✅ Your Alpaca paper account is connected.\n"
                f"  API Key: {creds['api_key'][:8]}...{creds['api_key'][-4:]}\n"
                f"  Base URL: {creds['base_url']}\n"
                "Use /trade to analyze, buy, sell, or check positions."
            )
        return (
            "❌ Your Alpaca account is not connected.\n"
            "Use /alpaca connect to link your Alpaca paper account."
        )
    except Exception as exc:
        logger.error(f"Alpaca status check failed: {exc}")
        return f"Error checking Alpaca status: {exc}"


async def _handle_disconnect(telegram_user_id: int) -> str:
    store = AlpacaCredentialStore()
    if not store.enabled:
        return "⚠️ Supabase is not configured on this server."
    try:
        await store.delete_credentials(telegram_user_id)
        return (
            "✅ Your Alpaca credentials have been removed.\n"
            "Your Alpaca paper account is now disconnected. "
            "Use /alpaca connect to link it again."
        )
    except Exception as exc:
        logger.error(f"Alpaca disconnect failed: {exc}")
        return f"Error disconnecting Alpaca account: {exc}"


async def _handle_connect(
    account: dict[str, Any],
    text: str,
    telegram_user_id: int,
) -> str:
    store = AlpacaCredentialStore()
    if not store.enabled:
        return (
            "⚠️ Supabase is not configured on this server. "
            "The bot administrator needs to set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY and create the alpaca_credentials table."
        )

    # Parse inline: /alpaca connect <api_key> <secret_key>
    parts = text.strip().split()
    if len(parts) >= 4:
        api_key = parts[2].strip()
        secret_key = parts[3].strip()
        return await _store_credentials(store, telegram_user_id, api_key, secret_key)

    return (
        "🔗 Connect your Alpaca paper account\n\n"
        "Send your Alpaca API credentials in this format:\n"
        "```
/alpaca connect YOUR_API_KEY YOUR_SECRET_KEY\n```\n\n"
        "Get your API keys from: https://app.alpaca.markets/paper/dashboard/overview\n"
        "Make sure you're using your **paper trading** keys.\n\n"
        "Your credentials will be encrypted and stored securely."
    )


async def _store_credentials(
    store: AlpacaCredentialStore,
    telegram_user_id: int,
    api_key: str,
    secret_key: str,
) -> str:
    api_key = api_key.strip()
    secret_key = secret_key.strip()
    if not api_key or not secret_key:
        return "❌ Both API key and secret key are required."
    try:
        # Upsert: delete existing then insert
        await store.delete_credentials(telegram_user_id)
        await store.store_credentials(
            telegram_user_id=telegram_user_id,
            api_key=api_key,
            secret_key=secret_key,
        )
        return (
            "✅ Your Alpaca paper account is now connected!\n"
            f"  API Key: {api_key[:8]}...{api_key[-4:]}\n"
            "You can now use /trade to buy, sell, analyze, and backtest.\n"
            "Use /alpaca disconnect to remove your credentials at any time."
        )
    except Exception as exc:
        logger.error(f"Alpaca connect failed: {exc}")
        return (
            f"❌ Failed to store credentials: {exc}\n"
            "Make sure the alpaca_credentials table exists in Supabase."
        )
