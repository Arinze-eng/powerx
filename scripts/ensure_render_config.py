"""Apply non-destructive Render runtime config migrations."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_DEFAULT_MAX_TOOL_ITERATIONS = 120
_LEGACY_DEFAULT_MAX_TOOL_ITERATIONS = 80
_DEFAULT_REASONING_EFFORT = "max"
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
    """Make the environment-configured provider win over any stale on-disk model.

    Render persists ``~/.nanobot/config.json`` across deploys. Historically this
    file could end up pinned to the old ``gemini-3.1-flash-lite`` fallback (from
    an early boot or an admin-panel save), and the previous migration only
    rewrote a narrow set of known-legacy values — so a stale model survived even
    after the operator configured LLM_* env vars, and users kept seeing Gemini.

    New behaviour: whenever the full set of LLM_MODEL / LLM_BASE_URL /
    LLM_API_KEY env vars is present, the agents.defaults.model and the custom
    provider endpoint/key are FORCE-refreshed to reference those env vars on
    every boot. Env is the single source of truth over stale disk state. When
    the env vars are absent we leave the config untouched (so local/dev setups
    and admin-only flows are unaffected). Secrets stay as ``${VAR}`` references;
    nothing literal is written to disk.
    """
    render_model = os.environ.get("LLM_MODEL", "").strip()
    render_base = os.environ.get("LLM_BASE_URL", "").strip()
    render_key = os.environ.get("LLM_API_KEY", "").strip()
    if not render_model or not render_base or not render_key:
        # Operator has not wired the environment-backed provider; do not touch.
        return False

    agents = data.setdefault("agents", {})
    defaults = agents.setdefault("defaults", {}) if isinstance(agents, dict) else None
    if not isinstance(defaults, dict):
        return False

    providers = data.setdefault("providers", {})
    custom = providers.setdefault("custom", {}) if isinstance(providers, dict) else None
    if not isinstance(custom, dict):
        return False

    changed = False
    # On Render the environment is the single source of truth for the provider.
    # Whenever LLM_MODEL/BASE_URL/API_KEY are all present, the persisted config
    # is forced to reference them — this is what stops a stale on-disk model
    # (e.g. the old Gemini fallback) from surviving deploys. Secrets remain as
    # ${VAR} references; nothing literal is written to disk.
    current_model = str(defaults.get("model") or "").strip()
    if current_model != "custom/${LLM_MODEL}":
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
    """Force Telegram into pure polling mode and stop step/progress noise on Render.

    The previous default forced streamed step/tool/reasoning events into a single
    live "working" message (liveActivity) so users saw every step. The operator
    wants the bot to STOP showing steps, so the migration now hard-pins the
    progress-affecting flags to False. Setting them False here (rather than just
    leaving the committed render-config.json) matters because Render keeps a live
    ``~/.nanobot/config.json`` that already has these set True — this migration
    runs on every boot (entrypoint) and flips the existing on-disk config so the
    change takes effect on the next deploy/restart.

    * ``liveActivity``   — the single in-place-edited step/working message.
    * ``sendToolHints``  — tool-call hints (e.g. ``read_file("...")``).
    * ``sendProgress``   — per-step progress text (also the fallback that would
      otherwise spam a separate message per step when liveActivity is off).
    * ``showReasoning``  — the model's 💭 "thinking" block.
    All are pinned to False so the bot replies with just the final answer.
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
    if telegram.get("streaming") is not True:
        telegram["streaming"] = True
        changed = True
    # Stop showing steps: disable the live activity card, tool/progress events,
    # and model reasoning on Telegram (the operator wants only the final reply).
    step_flags = (
        ("liveActivity", "live_activity"),
        ("sendToolHints", "send_tool_hints"),
        ("sendProgress", "send_progress"),
        ("showReasoning", "show_reasoning"),
    )
    for camel, snake in step_flags:
        current = telegram.get(camel, telegram.get(snake))
        if current is not False:
            telegram[camel] = False
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


# Elevated execution/context limits so the running agent can read through large
# repos in full instead of truncating ("reads what it can, then gives partial
# results"). Only filled in when the value is absent or structurally null, so a
# value the operator set even higher is never reduced here. The deployed render
# config leaves these null, so they default to the old code defaults (ctx 200K,
# out 8K, tool-result 16K chars) — well below what the custom provider supports.
_ELEVATED_LIMITS: dict[str, int] = {
    # context_window_tokens: accept one of the WebUI-valid options; 1M tokens.
    "context_window_tokens": 1_048_576,
    # max_tokens (max output tokens): raise to 32K so long responses are not cut.
    "max_tokens": 32_768,
    # max_tool_result_chars: how many chars a tool result may carry before the
    # context governor snips it. 16K was far too small for full file reads.
    "max_tool_result_chars": 524_288,
}


def _ensure_high_context_defaults(data: dict[str, Any]) -> bool:
    """Raise execution/context limits to the elevated defaults on Render."""
    agents = data.setdefault("agents", {})
    if not isinstance(agents, dict):
        return False
    defaults = agents.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        return False

    changed = False
    for snake_key, value in _ELEVATED_LIMITS.items():
        camel_key = {
            "context_window_tokens": "contextWindowTokens",
            "max_tokens": "maxTokens",
            "max_tool_result_chars": "maxToolResultChars",
        }[snake_key]
        existing = defaults.get(camel_key)
        try:
            cur = int(existing) if existing is not None else None
        except (TypeError, ValueError):
            cur = None
        # Raise only when unset/null or below our elevated target.
        if cur is None or cur < value:
            defaults[camel_key] = value
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
    changed = _ensure_high_context_defaults(data) or changed
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
