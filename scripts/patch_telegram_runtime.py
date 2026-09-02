#!/usr/bin/env python3
"""Restore runtime.py with Alpaca trading command edits.

This script:
1. Downloads the original runtime.py from the pre-merge commit
2. Applies 6 edits to add /trade, /backtest, /alpaca commands
3. Writes the updated file to nanobot/channels/telegram/runtime.py
4. Commits the change via the GitHub API

Usage:
    export GITHUB_TOKEN="your_token"
    python scripts/patch_telegram_runtime.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

OWNER = "Arinze-eng"
REPO = "powerx"
BRANCH = "main"
PATH = "nanobot/channels/telegram/runtime.py"
# The pre-merge commit that still has the full runtime.py
# This is the commit just before the merge of feat/alpaca-trading
PRE_MERGE_REF = "cf61d11c85fe767051d0864c46d281132e5812a1"


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


def download_original() -> str:
    """Download the original runtime.py from the pre-merge commit."""
    url = (
        f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{PATH}"
        f"?ref={PRE_MERGE_REF}"
    )
    data = github_request(url)
    return base64.b64decode(data["content"]).decode("utf-8")


def apply_edits(content: str) -> str:
    """Apply the 6 trading command edits to runtime.py."""

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
    old = r"video|cancel|discard)(?:@\w+)?(?:\s+.*)?$"
    new = r"video|trade|backtest|cancel|discard)(?:@\w+)?(?:\s+.*)?$"
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
        r'        r"^/alpaca(?:@\w+)?(?:\s+.*)?$"'
        "\n    )"
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

    return content


def commit_file(content: str) -> None:
    """Commit the updated runtime.py via the GitHub API."""
    # Get the current file SHA
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{PATH}?ref={BRANCH}"
    try:
        data = github_request(url)
        sha = data["sha"]
    except Exception:
        sha = None

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    body = {
        "message": "fix: restore full runtime.py with /trade /backtest /alpaca command edits",
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


def main() -> int:
    print("📥 Downloading original runtime.py from pre-merge commit...")
    content = download_original()
    print(f"   Downloaded: {len(content)} chars")

    print("🔧 Applying 6 trading command edits...")
    content = apply_edits(content)
    print(f"   Result: {len(content)} chars")

    # Verify edits
    checks = {
        "import": "from nanobot.trading.alpaca_commands import" in content,
        "BOT_COMMANDS": 'BotCommand("trade"' in content,
        "bus_regex": "trade|backtest" in content,
        "alpaca_regex": "TELEGRAM_ALPACA_COMMAND_RE" in content,
        "handler": "filters.Regex(TelegramChannel.TELEGRAM_ALPACA_COMMAND_RE)" in content,
        "dispatch": 'command_name == "/alpaca"' in content,
    }
    all_ok = True
    for name, ok in checks.items():
        print(f"   {'✅' if ok else '❌'} {name}")
        if not ok:
            all_ok = False

    if not all_ok:
        print("\n❌ Some edits failed verification. Aborting.")
        return 1

    print("\n📤 Committing to GitHub...")
    commit_file(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
