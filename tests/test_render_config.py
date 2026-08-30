import json
from pathlib import Path

from scripts.ensure_render_config import ensure_file_tool_enabled, ensure_render_defaults


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
    assert defaults["reasoningEffort"] == "high"
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


def test_render_defaults_preserve_intentional_admin_provider_selection(
    tmp_path: Path, monkeypatch
) -> None:
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

    ensure_render_defaults(config_path)
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["agents"]["defaults"]["model"] == original["agents"]["defaults"]["model"]
    assert updated["providers"] == original["providers"]


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
