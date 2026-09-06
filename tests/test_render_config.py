import json
from pathlib import Path

from scripts.ensure_render_config import (
    _ensure_provider_defaults,
    ensure_file_tool_enabled,
    ensure_render_defaults,
)


def test_enable_file_tool_preserves_existing_runtime_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    original = {
        "agents": {"defaults": {"model": "custom/muse"}},
        "providers": {"custom": {"apiBase": "https://example.invalid"}},
        "tools": {"restrictToWorkspace": True, "file": {"enable": False}},
        "channels": {"telegram": {"mode": "polling"}},
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")

    assert ensure_file_tool_enabled(config_path) is True
    updated = json.loads(config_path.read_text(encoding="utf-8"))

    assert updated["tools"]["file"]["enable"] is True
    assert updated["agents"] == original["agents"]
    assert updated["providers"] == original["providers"]
    assert updated["channels"] == original["channels"]


def test_render_defaults_upgrade_known_legacy_values(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    original = {
        "agents": {
            "defaults": {
                "model": "custom/muse",
                "maxToolIterations": 80,
            }
        },
        "tools": {"file": {"enable": True}},
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")

    assert ensure_render_defaults(config_path) is True
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    defaults = updated["agents"]["defaults"]
    assert defaults["maxToolIterations"] == 120
    assert defaults["reasoningEffort"] == "max"
    assert defaults["model"] == "custom/muse"


def test_render_defaults_preserve_custom_values(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    original = {
        "agents": {
            "defaults": {
                "maxToolIterations": 35,
                "reasoningEffort": "medium",
            }
        }
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")

    assert ensure_render_defaults(config_path) is True
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    defaults = updated["agents"]["defaults"]
    assert defaults["maxToolIterations"] == 35
    assert defaults["reasoningEffort"] == "medium"
    assert updated["tools"]["file"]["enable"] is True


def test_enabled_file_tool_is_not_rewritten(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"tools": {"file": {"enable": True}}}),
        encoding="utf-8",
    )

    assert ensure_file_tool_enabled(config_path) is False


def test_render_defaults_activate_environment_provider_over_legacy_gemini(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "custom/gemini-3.1-flash-lite"}},
                "providers": {
                    "custom": {
                        "apiBase": "https://legacy.example/api",
                        "apiKey": "legacy-key",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.example/api")
    monkeypatch.setenv("LLM_API_KEY", "environment-key")
    monkeypatch.setenv("LLM_MODEL", "muse-spark-1.2-contributor-free")

    assert ensure_render_defaults(config_path) is True
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["agents"]["defaults"]["model"] == "custom/${LLM_MODEL}"
    assert updated["providers"]["custom"] == {
        "apiBase": "${LLM_BASE_URL}",
        "apiKey": "${LLM_API_KEY}",
    }


def test_render_defaults_env_overrides_stale_admin_provider(
    tmp_path: Path, monkeypatch
) -> None:
    """Env-backed provider must win over any persisted value, including a model
    previously saved via the admin panel."""
    config_path = tmp_path / "config.json"
    original = {
        "agents": {"defaults": {"model": "custom/admin-selected-model"}},
        "providers": {
            "custom": {
                "apiBase": "https://admin.example/api",
                "apiKey": "admin-selected-key",
            }
        },
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.example/api")
    monkeypatch.setenv("LLM_API_KEY", "environment-key")
    monkeypatch.setenv("LLM_MODEL", "muse-spark-1.2-contributor-free")

    assert ensure_render_defaults(config_path) is True
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["agents"]["defaults"]["model"] == "custom/${LLM_MODEL}"
    assert updated["providers"]["custom"]["apiBase"] == "${LLM_BASE_URL}"
    assert updated["providers"]["custom"]["apiKey"] == "${LLM_API_KEY}"


def test_render_defaults_clear_stale_gemini_fallback(tmp_path: Path, monkeypatch) -> None:
    """A persisted fallback_models entry (e.g. gemini left by an old admin save)
    must be cleared when env defines the provider, otherwise FallbackProvider
    silently fails DeepSeek over to Gemini on transient errors — the exact
    'first call DeepSeek, second call Gemini' symptom."""
    config_path = tmp_path / "config.json"
    original = {
        "agents": {
            "defaults": {
                "model": "custom/deepseek-v4-flash",
                "provider": "custom",
                "fallback_models": ["custom/gemini-3.1-flash-lite"],
            }
        },
        "providers": {"custom": {"apiBase": "${LLM_BASE_URL}", "apiKey": "${LLM_API_KEY}"}},
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("LLM_BASE_URL", "https://kymaapi.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "kyma-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-flash")

    assert ensure_render_defaults(config_path) is True
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["agents"]["defaults"]["fallback_models"] == []
    assert updated["agents"]["defaults"]["model"] == "custom/${LLM_MODEL}"


def test_render_defaults_clear_stale_active_model_preset(tmp_path: Path, monkeypatch) -> None:
    """A persisted active model_preset (which takes precedence over defaults.model)
    must be dropped when env defines the provider — otherwise /status keeps showing
    the preset's model (e.g. gemini) even though defaults.model was forced to env."""
    config_path = tmp_path / "config.json"
    original = {
        "agents": {
            "defaults": {
                "model": "custom/deepseek-v4-flash",
                "provider": "custom",
                "model_preset": "GeminiFlash",
            }
        },
        "model_presets": {"GeminiFlash": {"model": "custom/gemini-3.1-flash-lite"}},
        "providers": {"custom": {"apiBase": "${LLM_BASE_URL}", "apiKey": "${LLM_API_KEY}"}},
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("LLM_BASE_URL", "https://kymaapi.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "kyma-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-flash")

    assert ensure_render_defaults(config_path) is True
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    d = updated["agents"]["defaults"]
    assert not d.get("model_preset"), f"preset should be cleared, got {d.get('model_preset')}"
    assert d["model"] == "custom/${LLM_MODEL}"


def test_render_defaults_leave_config_untouched_without_env(
    tmp_path: Path, monkeypatch
) -> None:
    """With no LLM_* env vars, the migration must not clobber existing config
    (protects local/dev and admin-only flows)."""
    config_path = tmp_path / "config.json"
    original = {
        "agents": {"defaults": {"model": "custom/admin-selected-model"}},
        "providers": {
            "custom": {
                "apiBase": "https://admin.example/api",
                "apiKey": "admin-selected-key",
            }
        },
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")
    for var in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    changed = _ensure_provider_defaults(json.loads(json.dumps(original)))
    assert changed is False


def test_render_defaults_migrate_local_browser_to_novita(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "tools": {
                    "browser": {
                        "enable": True,
                        "headless": True,
                        "executablePath": "/usr/bin/chromium",
                        "actionTimeoutMs": 9000,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert ensure_render_defaults(config_path) is True
    browser = json.loads(config_path.read_text(encoding="utf-8"))["tools"]["browser"]
    assert "headless" not in browser
    assert "executablePath" not in browser
    assert browser["enable"] is True
    assert browser["provider"] == "novita"
    assert browser["novitaApiKeyEnv"] == "NOVITA_API_KEY"
    assert browser["novitaTemplate"] == "browser-chromium"
    assert browser["novitaTimeoutSeconds"] == 600
    assert browser["novitaBrowserPort"] == 9223
    assert browser["actionTimeoutMs"] == 9000


def test_render_defaults_raise_execution_limits_when_null(tmp_path: Path) -> None:
    """A config with no explicit context limits gets the elevated defaults."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"agents": {"defaults": {"model": "custom/muse"}}}),
        encoding="utf-8",
    )

    assert ensure_render_defaults(config_path) is True
    defaults = json.loads(config_path.read_text(encoding="utf-8"))["agents"]["defaults"]
    assert defaults["contextWindowTokens"] == 1_048_576
    assert defaults["maxTokens"] == 32_768
    assert defaults["maxToolResultChars"] == 524_288


def test_render_defaults_do_not_raise_already_higher_values(tmp_path: Path) -> None:
    """Operator-set values equal to or above our elevated defaults are preserved."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "contextWindowTokens": 2_000_000,
                        "maxTokens": 64_000,
                        "maxToolResultChars": 1_000_000,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert ensure_render_defaults(config_path) is True
    defaults = json.loads(config_path.read_text(encoding="utf-8"))["agents"]["defaults"]
    assert defaults["contextWindowTokens"] == 2_000_000
    assert defaults["maxTokens"] == 64_000
    assert defaults["maxToolResultChars"] == 1_000_000


def test_render_defaults_enable_telegram_steps(tmp_path: Path) -> None:
    """Telegram step/progress/reasoning flags are forced ON for Render.

    The operator wants the bot to mirror the WebUI experience: every agent
    step, tool hint and the model's reasoning should be visible as the turn
    progresses. The migration must overwrite the existing on-disk config
    (which a previous migration forced to False) with True so the live bot
    shows steps again on the next deploy/restart.
    """
    config_path = tmp_path / "config.json"
    # Simulate the live on-disk config that previously had steps disabled.
    config_path.write_text(
        json.dumps(
            {
                "channels": {
                    "telegram": {
                        "mode": "polling",
                        "liveActivity": False,
                        "sendToolHints": False,
                        "sendProgress": False,
                        "showReasoning": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert ensure_render_defaults(config_path) is True
    telegram = json.loads(config_path.read_text(encoding="utf-8"))["channels"]["telegram"]
    assert telegram["liveActivity"] is True
    assert telegram["sendToolHints"] is True
    assert telegram["sendProgress"] is True
    assert telegram["showReasoning"] is True
