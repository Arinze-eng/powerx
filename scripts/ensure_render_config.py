"""Apply non-destructive Render runtime config migrations."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_DEFAULT_MAX_TOOL_ITERATIONS = 120
_LEGACY_DEFAULT_MAX_TOOL_ITERATIONS = 80
_DEFAULT_REASONING_EFFORT = "high"
_DEFAULT_RENDER_MODEL = "gemini-3.1-flash-lite"


def _load_config(config_path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_config(config_path: Path, data: dict[str, Any]) -> bool:
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(config_path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return False
    return True


def _ensure_tools_file(data: dict[str, Any]) -> bool:
    tools = data.setdefault("tools", {})
    if not isinstance(tools, dict):
        return False
    file_config = tools.setdefault("file", {})
    if not isinstance(file_config, dict):
        return False
    if file_config.get("enable") is True:
        return False
    file_config["enable"] = True
    return True


def _ensure_browser_defaults(data: dict[str, Any]) -> bool:
    tools = data.setdefault("tools", {})
    if not isinstance(tools, dict):
        return False
    browser = tools.setdefault("browser", {})
    if not isinstance(browser, dict):
        return False
    changed = False
    for obsolete_key in ("headless", "executablePath"):
        if obsolete_key in browser:
            browser.pop(obsolete_key, None)
            changed = True
    defaults = {
        "enable": True,
        "provider": "novita",
        "novitaApiKeyEnv": "NOVITA_API_KEY",
        "novitaTemplate": "browser-chromium",
        "novitaTimeoutSeconds": 600,
        "novitaBrowserPort": 9223,
        "navigationTimeoutMs": 30_000,
        "actionTimeoutMs": 15_000,
        "sessionIdleSeconds": 900,
        "maxPageTextChars": 12_000,
    }
    for key, value in defaults.items():
        if key not in browser:
            browser[key] = value
            changed = True
    if browser.get("provider") != "novita":
        browser["provider"] = "novita"
        changed = True
    return changed


def _ensure_provider_defaults(data: dict[str, Any]) -> bool:
    """Activate Render's environment-backed custom provider over the old Gemini default.

    Existing admin-selected models are preserved. The migration only changes the
    known legacy Render fallback, keeping the endpoint and key as environment
    references rather than writing secrets into the persistent config.
    """
    render_model = os.environ.get("LLM_MODEL", "").strip()
    render_base = os.environ.get("LLM_BASE_URL", "").strip()
    render_key = os.environ.get("LLM_API_KEY", "").strip()
    if not render_model or not render_base or not render_key:
        return False

    agents = data.setdefault("agents", {})
    defaults = agents.setdefault("defaults", {}) if isinstance(agents, dict) else None
    if not isinstance(defaults, dict):
        return False
    current_model = str(defaults.get("model") or "").strip()
    current_model_name = current_model.removeprefix("custom/")
    if current_model_name not in {"", _DEFAULT_RENDER_MODEL, "${LLM_MODEL}"}:
        return False

    providers = data.setdefault("providers", {})
    custom = providers.setdefault("custom", {}) if isinstance(providers, dict) else None
    if not isinstance(custom, dict):
        return False

    changed = False
    if defaults.get("model") != "custom/${LLM_MODEL}":
        defaults["model"] = "custom/${LLM_MODEL}"
        changed = True
    if custom.get("apiBase") != "${LLM_BASE_URL}":
        custom["apiBase"] = "${LLM_BASE_URL}"
        changed = True
    if custom.get("apiKey") != "${LLM_API_KEY}":
        custom["apiKey"] = "${LLM_API_KEY}"
        changed = True
    return changed


def _ensure_telegram_polling_defaults(data: dict[str, Any]) -> bool:
    """Force Telegram into pure polling mode on Render.

    The previous default forced `mode: webhook` every boot, which silently
    reverted any polling config on each deploy. The bot runs pure polling now,
    so strip any stale webhook keys and force streamed progress like a normal
    interactive session.
    """
    channels = data.setdefault("channels", {})
    if not isinstance(channels, dict):
        return False
    telegram = channels.setdefault("telegram", {})
    if not isinstance(telegram, dict):
        return False

    changed = False
    if telegram.get("mode") != "polling":
        telegram["mode"] = "polling"
        changed = True
    if not telegram.get("streaming", True):
        telegram["streaming"] = True
        changed = True
    for webhook_key in (
        "webhookUrl",
        "webhookSecretToken",
        "webhookListenHost",
        "webhookListenPort",
        "webhookPath",
        "webhookMaxConnections",
    ):
        if webhook_key in telegram:
            telegram.pop(webhook_key, None)
            changed = True
    return changed


def _ensure_deliberate_defaults(data: dict[str, Any]) -> bool:
    agents = data.setdefault("agents", {})
    if not isinstance(agents, dict):
        return False
    defaults = agents.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        return False

    changed = False
    current_iterations = defaults.get("maxToolIterations", defaults.get("max_tool_iterations"))
    # 80 was the old committed Render default. Upgrade only that known default;
    # preserve any other user-selected value.
    if current_iterations in (None, _LEGACY_DEFAULT_MAX_TOOL_ITERATIONS):
        if "max_tool_iterations" in defaults and "maxToolIterations" not in defaults:
            defaults["max_tool_iterations"] = _DEFAULT_MAX_TOOL_ITERATIONS
        else:
            defaults["maxToolIterations"] = _DEFAULT_MAX_TOOL_ITERATIONS
        changed = True

    reasoning = defaults.get("reasoningEffort", defaults.get("reasoning_effort"))
    if reasoning is None or (isinstance(reasoning, str) and not reasoning.strip()):
        if "reasoning_effort" in defaults and "reasoningEffort" not in defaults:
            defaults["reasoning_effort"] = _DEFAULT_REASONING_EFFORT
        else:
            defaults["reasoningEffort"] = _DEFAULT_REASONING_EFFORT
        changed = True
    return changed


def ensure_render_defaults(config_path: Path) -> bool:
    """Apply attachment and deliberate-execution defaults without clobbering config."""
    data = _load_config(config_path)
    if data is None:
        return False
    changed = _ensure_tools_file(data) or False
    changed = _ensure_browser_defaults(data) or changed
    changed = _ensure_provider_defaults(data) or changed
    changed = _ensure_telegram_polling_defaults(data) or changed
    changed = _ensure_deliberate_defaults(data) or changed
    return _write_config(config_path, data) if changed else False


def ensure_file_tool_enabled(config_path: Path) -> bool:
    """Enable local file reads required for Telegram attachment handling."""
    data = _load_config(config_path)
    if data is None or not _ensure_tools_file(data):
        return False
    return _write_config(config_path, data)


def main() -> int:
    if os.environ.get("RENDER") != "true" or len(sys.argv) != 2:
        return 0
    changed = ensure_render_defaults(Path(sys.argv[1]).expanduser())
    if changed:
        print("[entrypoint] applied Render runtime defaults (Telegram polling mode)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
