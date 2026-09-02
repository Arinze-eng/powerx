"""Telegram commands for the public OpenAI-compatible API platform.

Commands (handled inside ``nanobot.channels.telegram.runtime``):

- ``/apikey [name]``      — generate a new API key (shown once)
- ``/listapikeys``        — list this account's API keys and usage
- ``/revokeapikey all``   — revoke all active API keys
- ``/apidoc``             — integration documentation + server base URL

Keys are stored hashed in Supabase (``agent_api_keys``) and billed through the
existing credit system when used against ``/v1/chat/completions``.
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

from nanobot.api.api_keys import ApiKeyError, ApiKeyStore

_MAX_KEYS_PER_USER = 10

DOC_TEMPLATE = """🔌 <b>PowerX Agent API</b> — OpenAI-compatible

Your API key lets your own apps, agents, and platforms call the PowerX agent directly. Every request runs fully on our servers: questions, file uploads, and image analysis are processed here, and results come back to your app. Usage is billed with your existing credits (same balance as this bot).

<b>Base URL</b>
<code>{base_url}/v1</code>

<b>Authentication</b>
Send your key as a bearer token:
<code>Authorization: Bearer YOUR_API_KEY</code>

<b>Endpoints</b>
• POST /v1/chat/completions — run the agent (JSON or multipart)
• GET /v1/models — list available models
• GET /health — server status

<b>Example — text question</b>
<pre><code>curl {base_url}/v1/chat/completions \\
  -H "Authorization: Bearer $POWERX_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{{"model": "{model}", "messages": [{{"role": "user", "content": "What is 2+2?"}}]}}'</code></pre>

<b>Example — upload a file or image</b>
Send <code>multipart/form-data</code> with these fields:
• <code>file</code> — the image/file to analyze
• <code>content</code> — your question about it
• <code>model</code> — {model}
• <code>session_id</code> — optional; reuse one value to keep conversation context

<pre><code>curl {base_url}/v1/chat/completions \\
  -H "Authorization: Bearer $POWERX_API_KEY" \\
  -F "file=@photo.png" \\
  -F "content=What is in this image?" \\
  -F "model={model}"</code></pre>

<b>Notes</b>
• Multi-step agentic tasks consume 1 credit per step, same as chatting here.
• Keep your key secret — anyone with it can spend your credits.
• Manage keys anytime: /apikey, /listapikeys, /revokeapikey

Need help? Just ask me here in Telegram."""


def _base_url() -> str:
    return (
        os.getenv("NANOBOT_API_PUBLIC_URL", "").strip().rstrip("/")
        or os.getenv("API_SERVER_URL", "").strip().rstrip("/")
        or "https://YOUR-SERVER-HOST:8900"
    )


def _model_name() -> str:
    return os.getenv("NANOBOT_API_MODEL_NAME", "").strip() or "powerx-agent"


def render_docs() -> str:
    return DOC_TEMPLATE.format(base_url=_base_url(), model=_model_name())


def _require_account(message: Any, account: dict[str, Any] | None) -> str | None:
    if message.chat.type != "private":
        return "Please use the API commands in a private chat with me."
    if not account or not account.get("agentx_user_id"):
        return "You need an AgentX account first. Use /signup or /signin before managing API keys."
    return None


async def handle_api_command(
    store: ApiKeyStore,
    message: Any,
    user: Any,
    command: str,
    args: str,
    account: dict[str, Any] | None,
) -> None:
    """Dispatch one of the /apikey family of commands. Always replies to the user."""
    problem = _require_account(message, account)
    if problem:
        await message.reply_text(problem)
        return
    if not store.enabled:
        await message.reply_text("The API platform is not configured on this server yet. Please try again later.")
        return

    user_id = str(account["agentx_user_id"])
    telegram_id = int(account.get("telegram_user_id") or getattr(user, "id", 0) or 0) or None

    try:
        if command == "/apikey":
            await _cmd_create_key(store, message, user_id, telegram_id, args)
        elif command == "/listapikeys":
            await _cmd_list_keys(store, message, user_id)
        elif command == "/revokeapikey":
            await _cmd_revoke_keys(store, message, user_id, args)
        elif command == "/apidoc":
            await message.reply_text(render_docs(), parse_mode="HTML", disable_web_page_preview=True)
    except ApiKeyError as exc:
        logger.warning("API key command {} failed: {}", command, str(exc)[:300])
        await message.reply_text("Something went wrong while managing your API keys. Please try again.")


async def _cmd_create_key(store: ApiKeyStore, message: Any, user_id: str, telegram_id: int | None, name: str) -> None:
    existing = await store.list_keys(user_id)
    active = [row for row in existing if row.get("is_active")]
    if len(active) >= _MAX_KEYS_PER_USER:
        await message.reply_text(
            f"You already have {len(active)} active API keys (limit {_MAX_KEYS_PER_USER}). "
            "Revoke one first with /revokeapikey all, then create a new key."
        )
        return
    plain, row = await store.create_key(
        agentx_user_id=user_id, telegram_user_id=telegram_id, name=name or "default"
    )
    key_id = row.get("id", "?")
    await message.reply_text(
        "✅ <b>New API key created</b>\n\n"
        f"<code>{plain}</code>\n\n"
        "⚠️ Copy this key now — it is shown only once and cannot be retrieved later.\n\n"
        f"Name: {name or 'default'} (ID {key_id})\n"
        "Use /apidoc for integration docs, /listapikeys to manage keys.",
        parse_mode="HTML",
    )


async def _cmd_list_keys(store: ApiKeyStore, message: Any, user_id: str) -> None:
    rows = await store.list_keys(user_id)
    if not rows:
        await message.reply_text(
            "You have no API keys yet. Create one with /apikey (optionally add a name, e.g. /apikey my-app)."
        )
        return
    lines = ["🔑 <b>Your API keys</b>", ""]
    for row in rows:
        status = "active ✅" if row.get("is_active") else "revoked ❌"
        last_used = str(row.get("last_used_at") or "never")[:16].replace("T", " ")
        lines.append(
            f"• <code>{row.get('key_prefix')}…</code> — {row.get('name') or 'default'}\n"
            f"  ID {row.get('id')} | {status} | requests: {row.get('total_requests') or 0} | last used: {last_used}"
        )
    lines.append("")
    lines.append("Revoke all keys with /revokeapikey all")
    await message.reply_text("\n".join(lines), parse_mode="HTML")


async def _cmd_revoke_keys(store: ApiKeyStore, message: Any, user_id: str, args: str) -> None:
    target = (args or "").strip().lower()
    if target not in {"all", "-a"}:
        await message.reply_text(
            "Usage: /revokeapikey all\n\nThis revokes every active API key on your account. "
            "Create fresh keys afterwards with /apikey."
        )
        return
    revoked = await store.revoke_all(user_id)
    if revoked:
        await message.reply_text(f"🗑 Revoked {revoked} API key(s). They can no longer be used.")
    else:
        await message.reply_text("You have no active API keys to revoke.")
