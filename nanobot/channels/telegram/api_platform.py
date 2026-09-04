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

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from nanobot.api.api_keys import ApiKeyError, ApiKeyStore, hash_api_key  # noqa: F401 (re-export)

_MAX_KEYS_PER_USER = 10


def _api_port() -> int:
    raw = os.getenv("NANOBOT_API_PORT", "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        from nanobot.config.loader import get_config_path

        cfg_path = Path(get_config_path())
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            # Single-process (Render gateway) deployment: the public reverse
            # proxy forwards every non-Telegram path — including /v1/* — to
            # the WebUI websocket channel, so the API lives on the webhook
            # origin itself (standard HTTPS port).
            ws = (data.get("channels") or {}).get("websocket") or {}
            if isinstance(ws, dict) and ws.get("enabled"):
                return 443
            port = data.get("api", {}).get("port")
            if isinstance(port, int):
                return port
    except Exception as exc:  # config unreadable — fall back to the default port
        logger.debug("api port lookup from config failed: {}", exc)
    return 8900


def _webhook_origin(parsed) -> str:
    """scheme://host[:port] of the webhook URL (default HTTPS ports omitted)."""
    host = parsed.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = host if parsed.port in (None, 443) else f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{netloc}"


def resolve_base_url() -> str:
    """Public base URL of the OpenAI-compatible API, e.g. ``https://api.powerx.ai``.

    Resolution order:
    1. ``NANOBOT_API_PUBLIC_URL`` / ``API_SERVER_URL`` env override.
    2. Derived from the Telegram webhook URL host (the bot's public origin),
       with the API port attached — works when both run on the same server.
    Returns "" when neither is available; callers must handle that.
    """
    for var in ("NANOBOT_API_PUBLIC_URL", "API_SERVER_URL"):
        value = os.getenv(var, "").strip().rstrip("/")
        if value and "YOUR-SERVER" not in value.upper():
            return value
    try:
        from nanobot.config.loader import get_config_path

        cfg_path = Path(get_config_path())
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            telegram = data.get("channels", {}).get("telegram", {}) or {}
            webhook = str(telegram.get("webhook_url", "")).strip()
            parsed = urlparse(webhook)
            if parsed.scheme == "https" and parsed.hostname:
                api_port = _api_port()
                # API on standard HTTPS port → same origin as the webhook.
                if api_port in (443, None):
                    return _webhook_origin(parsed)
                host = parsed.hostname
                if ":" in host:  # IPv6 literal
                    host = f"[{host}]"
                # Webhook itself sits on the API port → already the right origin.
                if parsed.port == api_port:
                    return _webhook_origin(parsed)
                return f"https://{host}:{api_port}"
    except Exception as exc:  # malformed config etc.
        logger.debug("api base url derivation failed: {}", exc)
    return ""

DOC_TEMPLATE = """🔌 <b>PowerX Agent API</b> — OpenAI-compatible

Your API key lets your own apps, agents, and platforms call the PowerX agent directly. Questions, file uploads, and image analysis all run fully on our servers; results come back to your app. Billed with your existing credits (same balance as this bot).

<b>Base URL</b>
<code>{base_url}/v1</code>

⚠️ <b>How to send requests:</b> put everything in the URL query string — the <code>token</code> parameter carries your key and the <code>payload</code> parameter carries the JSON request body (URL-encoded). Do not use POST bodies.

<b>1) Chat</b>
<pre><code>GET {base_url}/v1/chat/completions?token=$POWERX_API_KEY&amp;payload={{"model":"{model}","messages":[{{"role":"user","content":"What is 2+2?"}}]}}</code></pre>
curl example:
<pre><code>curl -G {base_url}/v1/chat/completions \\
  --data-urlencode "token=$POWERX_API_KEY" \\
  --data-urlencode 'payload={{"model":"{model}","messages":[{{"role":"user","content":"What is 2+2?"}}]}}'</code></pre>

Python example:
<pre><code>import json, urllib.parse, urllib.request
payload = {{"model": "{model}", "messages": [{{"role": "user", "content": "What is 2+2?"}}]}}
url = ("{base_url}/v1/chat/completions?token=" + KEY +
       "&payload=" + urllib.parse.quote(json.dumps(payload)))
print(json.loads(urllib.request.urlopen(url, timeout=420).read())["choices"][0]["message"]["content"])</code></pre>

<b>2) File / image upload</b>
Attach files inside the payload as base64 data URLs (OpenAI multimodal format). Keep each file under ~10 MB:
<pre><code>{{"model": "{model}", "messages": [{{"role": "user", "content": [
  {{"type": "text", "text": "What is in this image?"}},
  {{"type": "image_url", "image_url": {{"url": "data:image/png;base64,&lt;BASE64&gt;"}}}}
]}}]}}</code></pre>
<b>Any file type works</b> (PDF, XLSX, DOCX, PPTX, ZIP, APK, CSV, text, images…). For non-image files use a <code>"file"</code> part with a filename and the real mime type in the data URL — the agent saves it into its workspace and reads it with its file tools (documents, spreadsheets and archives are parsed natively):
<pre><code>{{"model": "{model}", "messages": [{{"role": "user", "content": [
  {{"type": "text", "text": "Summarize this spreadsheet"}},
  {{"type": "file", "file": {{"filename": "data.xlsx", "file_data": "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,&lt;BASE64&gt;"}}}}
]}}]}}</code></pre>
Keep each file under ~10 MB. To build the <code>file_data</code>, base64-encode the file (e.g. <code>base64 -w0 report.pdf</code>) and prefix it with <code>data:&lt;mime&gt;;base64,</code>.

<b>3) Streaming (optional)</b>
Add <code>"stream":true</code> to the payload → response arrives as SSE chunks ending with <code>data: [DONE]</code>.

<b>Other endpoints</b>
• <code>GET {base_url}/v1/models</code> — list available models
• <code>GET {base_url}/v1/api-docs</code> — these docs as plain text

<b>Response shape (OpenAI-compatible)</b>
<pre><code>{{"id":"chatcmpl-…","object":"chat.completion","model":"{model}",
 "choices":[{{"index":0,"message":{{"role":"assistant","content":"…"}},"finish_reason":"stop"}}],
 "usage":{{"prompt_tokens":N,"completion_tokens":N,"total_tokens":N}}}}</code></pre>

<b>Errors</b>
401 bad/missing key • 400 invalid payload JSON • 402 out of credits • 503 starting up, retry shortly

<b>Notes</b>
• Multi-step agentic tasks consume 1 credit per step, same as chatting here.
• Conversation context persists server-side per user across calls.
• Keep your key secret — anyone with it can spend your credits.
• Manage keys anytime: /apikey, /listapikeys, /revokeapikey

Need help? Just ask me here in Telegram."""


def _model_name() -> str:
    return os.getenv("NANOBOT_API_MODEL_NAME", "").strip() or "powerx-agent"


def render_docs() -> str:
    base_url = resolve_base_url()
    if not base_url:
        return (
            "⚠️ The API server address is not configured on this bot yet.\n\n"
            "The admin must set NANOBOT_API_PUBLIC_URL (e.g. https://api.example.com) "
            "in the server environment, or run the bot in Telegram webhook mode so the "
            "address can be derived automatically. Everything else — /apikey, "
            "/listapikeys, /revokeapikey — already works."
        )
    return DOC_TEMPLATE.format(base_url=base_url, model=_model_name())


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
    base_url = resolve_base_url()
    where = f"\nBase URL: {base_url}/v1 (see /apidoc for examples)" if base_url else ""
    await message.reply_text(
        "✅ <b>New API key created</b>\n\n"
        f"<code>{plain}</code>\n\n"
        "⚠️ Copy this key now — it is shown only once and cannot be retrieved later.\n\n"
        f"Name: {name or 'default'} (ID {key_id}){where}\n"
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
