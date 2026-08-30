from types import SimpleNamespace

from nanobot.execution_env import apply_render_execution_env


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        execution=SimpleNamespace(
            backend="novita",
            vps=SimpleNamespace(
                host="",
                port=22,
                username="",
                password="",
                private_key="",
                host_key_fingerprint="",
                host_key_policy="fingerprint",
                workspace_dir="/workspace",
                connect_timeout=15,
            ),
        )
    )


def test_render_execution_env_is_noop_without_backend(monkeypatch) -> None:
    monkeypatch.delenv("NANOBOT_EXECUTION_BACKEND", raising=False)
    config = _config()
    assert apply_render_execution_env(config) is config
    assert config.execution.backend == "novita"
    assert config.execution.vps.host == ""


def test_render_execution_env_applies_vps_settings(monkeypatch) -> None:
    values = {
        "NANOBOT_EXECUTION_BACKEND": "vps",
        "NANOBOT_VPS_HOST": "vps.example.test",
        "NANOBOT_VPS_PORT": "10050",
        "NANOBOT_VPS_USERNAME": "administrator",
        "NANOBOT_VPS_PASSWORD": "fixture-secret",
        "NANOBOT_VPS_PRIVATE_KEY": "fixture-private-key",
        "NANOBOT_VPS_FINGERPRINT": "SHA256:fixture",
        "NANOBOT_VPS_HOST_KEY_POLICY": "accept_any",
        "NANOBOT_VPS_WORKSPACE": "/workspace",
        "NANOBOT_VPS_TIMEOUT": "30",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    config = _config()
    assert apply_render_execution_env(config) is config
    vps = config.execution.vps
    assert config.execution.backend == "vps"
    assert vps.host == "vps.example.test"
    assert vps.port == 10050
    assert vps.username == "administrator"
    assert vps.password == "fixture-secret"
    assert vps.private_key == "fixture-private-key"
    assert vps.host_key_fingerprint == "SHA256:fixture"
    assert vps.host_key_policy == "accept_any"
    assert vps.connect_timeout == 30


def test_render_execution_env_ignores_malformed_numeric_values(monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "vps")
    monkeypatch.setenv("NANOBOT_VPS_PORT", "not-a-port")
    monkeypatch.setenv("NANOBOT_VPS_TIMEOUT", "0")
    config = _config()
    apply_render_execution_env(config)
    assert config.execution.vps.port == 22
    assert config.execution.vps.connect_timeout == 15


def test_render_execution_env_can_explicitly_preserve_novita(monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "novita")
    config = _config()
    apply_render_execution_env(config)
    assert config.execution.backend == "novita"


def test_saved_novita_choice_wins_over_durable_vps_values(monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "vps")
    monkeypatch.setenv("NANOBOT_VPS_HOST", "vps.example.test")
    monkeypatch.setenv("NANOBOT_VPS_PORT", "10050")
    monkeypatch.setenv("NANOBOT_VPS_USERNAME", "administrator")
    monkeypatch.setenv("NANOBOT_VPS_PASSWORD", "fixture-secret")
    config = _config()
    config.execution.backend = "novita"
    config.execution.vps.host = "vps.example.test"
    config.execution.vps.username = "administrator"
    config.execution.vps.password = "existing-secret"

    apply_render_execution_env(config)

    assert config.execution.backend == "novita"
    assert config.execution.vps.host == "vps.example.test"
    assert config.execution.vps.port == 10050
    assert config.execution.vps.password == "fixture-secret"


def test_switching_saved_novita_back_to_vps_uses_durable_credentials(monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "vps")
    monkeypatch.setenv("NANOBOT_VPS_HOST", "vps.example.test")
    monkeypatch.setenv("NANOBOT_VPS_USERNAME", "administrator")
    monkeypatch.setenv("NANOBOT_VPS_PASSWORD", "fixture-secret")
    config = _config()
    config.execution.backend = "novita"
    config.execution.vps.host = "vps.example.test"
    config.execution.vps.username = "administrator"
    config.execution.vps.password = "existing-secret"
    config.execution.vps.port = 10050

    apply_render_execution_env(config)
    assert config.execution.backend == "novita"

    config.execution.backend = "vps"
    apply_render_execution_env(config)

    assert config.execution.backend == "vps"
    assert config.execution.vps.host == "vps.example.test"
    assert config.execution.vps.username == "administrator"
    assert config.execution.vps.password == "fixture-secret"


def test_fresh_config_still_restores_durable_vps_selection(monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "vps")
    monkeypatch.setenv("NANOBOT_VPS_HOST", "vps.example.test")
    monkeypatch.setenv("NANOBOT_VPS_USERNAME", "administrator")
    monkeypatch.setenv("NANOBOT_VPS_PASSWORD", "fixture-secret")
    config = _config()

    apply_render_execution_env(config)

    assert config.execution.backend == "vps"
    assert config.execution.vps.host == "vps.example.test"
    assert config.execution.vps.password == "fixture-secret"
