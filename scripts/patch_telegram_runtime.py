"""Patch script: adds /trade, /backtest, /alpaca commands to Telegram runtime.

This script downloads the current nanobot/channels/telegram/runtime.py from
the feat/alpaca-trading branch, applies 6 targeted edits to add trading
commands, and commits the updated file back.

Usage:
    python scripts/patch_telegram_runtime.py

Requires GITHUB_TOKEN environment variable with repo write access.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

OWNER = "Arinze-eng"
REPO = "powerx"
BRANCH = "feat/alpaca-trading"
PATH = "nanobot/channels/telegram/runtime.py"


def github_request(url: str, method: str = "GET", body: dict | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"token {token}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main() -> int:
    # 1. Download current runtime.py from main branch (the original, untruncated)
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{PATH}?ref=main"
    data = github_request(url)
    import base64
    content = base64.b64decode(data["content"]).decode("utf-8")
    sha = None  # We'll get the SHA from the feature branch

    # 2. Get the SHA from the feature branch (if it exists)
    try:
        feat_url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{PATH}?ref={BRANCH}"
        feat_data = github_request(feat_url)
        sha = feat_data["sha"]
    except Exception:
        pass  # File doesn't exist on branch yet, will create

    # 3. Apply the 6 edits

    # Edit 1: Add import for alpaca_commands
    old = "from nanobot.channels.telegram.api_platform import handle_api_command"
    new = (
        "from nanobot.channels.telegram.api_platform import handle_api_command\n"
        "from nanobot.trading.alpaca_commands import is_alpaca_command, handle_alpaca_command"
    )
    assert old in content, "Could not find api_platform import"
    content = content.replace(old, new, 1)

    # Edit 2: Add trade, backtest, alpaca to BOT_COMMANDS
    old = '        BotCommand("cancel", "Cancel auth or pending work"),'
    new = (
        '        BotCommand("trade", "Trading: analyze, buy, sell, positions"),\n'
        '        BotCommand("backtest", "Run a strategy backtest"),\n'
        '        BotCommand("alpaca", "Connect/disconnect Alpaca account"),\n'
        '        BotCommand("cancel", "Cancel auth or pending work"),'
    )
    assert old in content, "Could not find BOT_COMMANDS cancel entry"
    content = content.replace(old, new, 1)

    # Edit 3: Add trade|backtest to TELEGRAM_BUS_SLASH_COMMAND_RE
    old = "video|cancel|discard)(?:@\\\\w+)?(?:\\\\s+.*)?$"
    new = "video|trade|backtest|cancel|discard)(?:@\\\\w+)?(?:\\\\s+.*)?$"
    assert old in content, "Could not find bus slash command regex"
    content = content.replace(old, new, 1)

    # Edit 4: Add TELEGRAM_ALPACA_COMMAND_RE after API_PLATFORM regex
    idx = content.find("TELEGRAM_API_PLATFORM_COMMAND_RE = re.compile(")
    assert idx >= 0, "Could not find API_PLATFORM regex"
    end_idx = content.find("    )", idx)
    assert end_idx >= 0, "Could not find closing of API_PLATFORM regex"
    alpaca_re = (
        "\n\n"
        "    # Commands for the Alpaca trading integration (connect/disconnect/status).\n"
        "    TELEGRAM_ALPACA_COMMAND_RE = re.compile(\n"
        '        r"^/alpaca(?:@\\\\w+)?(?:\\\\s+.*)?$"\n'
        "    )"
    )
    content = content[: end_idx + 5] + alpaca_re + content[end_idx + 5 :]

    # Edit 5: Add handler registration for ALPACA regex
    old = (
        "                filters.Regex(TelegramChannel.TELEGRAM_API_PLATFORM_COMMAND_RE),\n"
        "                self._forward_command,\n"
        "            )\n"
        "        )\n"
        "        self._app.add_handler(\n"
        "            MessageHandler(\n"
        "                filters.Regex(TelegramChannel.TELEGRAM_BUS_SLASH_COMMAND_RE),"
    )
    new = (
        "                filters.Regex(TelegramChannel.TELEGRAM_API_PLATFORM_COMMAND_RE),\n"
        "                self._forward_command,\n"
        "            )\n"
        "        )\n"
        "        self._app.add_handler(\n"
        "            MessageHandler(\n"
        "                filters.Regex(TelegramChannel.TELEGRAM_ALPACA_COMMAND_RE),\n"
        "                self._forward_command,\n"
        "            )\n"
        "        )\n"
        "        self._app.add_handler(\n"
        "            MessageHandler(\n"
        "                filters.Regex(TelegramChannel.TELEGRAM_BUS_SLASH_COMMAND_RE),"
    )
    assert old in content, "Could not find handler registration block"
    content = content.replace(old, new, 1)

    # Edit 6: Add alpaca command dispatch
    old = (
        '        if command_name in {"/apikey", "/listapikeys", "/revokeapikey", "/apidoc"}:\n'
        '            args = content.split(None, 1)[1] if " " in content else ""\n'
        "            await handle_api_command(self._api_keys, message, user, command_name, args, account)\n"
        "            return"
    )
    new = (
        '        if command_name in {"/apikey", "/listapikeys", "/revokeapikey", "/apidoc"}:\n'
        '            args = content.split(None, 1)[1] if " " in content else ""\n'
        "            await handle_api_command(self._api_keys, message, user, command_name, args, account)\n"
        "            return\n"
        '        if command_name == "/alpaca" or content.strip().startswith("/alpaca "):\n'
        "            response = await handle_alpaca_command(\n"
        "                account, content, message.chat_id, message.message_id\n"
        "            )\n"
        "            await self._reply_text(message, response)\n"
        "            return"
    )
    assert old in content, "Could not find apikey dispatch block"
    content = content.replace(old, new, 1)

    # 4. Commit the updated file
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    body = {
        "message": "feat: add /trade, /backtest, /alpaca commands to Telegram runtime",
        "content": encoded,
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha

    result = github_request(
        f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{PATH}",
        method="PUT",
        body=body,
    )
    print(f"✅ Committed: {result['commit']['html_url']}")
    print(f"   File size: {len(content)} chars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
