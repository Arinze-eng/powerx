from types import SimpleNamespace

from nanobot.execution_env import apply_render_execution_env


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        execution=SimpleNamespace(
            backend="novita",
            backend_source="default",
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
            upstash=SimpleNamespace(
                api_key="",
                base_url="https://us-east-1.box.upstash.com",
                runtime="python",
                size="small",
                ttl_s=3600,
            ),
            daytona=SimpleNamespace(
                api_key="",
                api_url="",
                snapshot="daytona-small",
                domain_allow_list="",
                network_allow_list="0.0.0.0/0",
                fetch_allow_hosts="",
                outbound_proxy_url="",
                ttl_minutes=60,
            ),
        )
    )


# --------------------------------------------------------------------------
# The overlay is credential-only: choosing a backend is an admin decision.
# --------------------------------------------------------------------------


def test_render_execution_env_is_noop_without_backend(monkeypatch) -> None:
    monkeypatch.delenv("NANOBOT_EXECUTION_BACKEND", raising=False)
    config = _config()
    assert apply_render_execution_env(config) is config
    assert config.execution.backend == "novita"
    assert config.execution.vps.host == ""


def test_overlay_never_changes_the_backend_label(monkeypatch) -> None:
    """Regression: an admin's saved selection used to be silently reverted.

    Selecting VPS while the durable platform env pinned ``daytona`` (or the
    reverse) must not move ``execution.backend`` any more; that revert is what
    made tasks land in the wrong sandbox.
    """
    for env_backend in ("novita", "vps", "upstash", "daytona"):
        monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", env_backend)
        for saved_backend in ("novita", "vps", "upstash", "daytona"):
            config = _config()
            config.execution.backend = saved_backend
            config.execution.backend_source = "admin"

            apply_render_execution_env(config)

            assert config.execution.backend == saved_backend, (
                f"env={env_backend} overwrote saved={saved_backend}"
            )


def test_overlay_never_changes_backend_even_without_provenance(monkeypatch) -> None:
    # Boot-time bootstrap owns first-run seeding; this module never does.
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "daytona")
    config = _config()
    apply_render_execution_env(config)
    assert config.execution.backend == "novita"


def test_overlay_fills_vps_credentials(monkeypatch) -> None:
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
    assert vps.host == "vps.example.test"
    assert vps.port == 10050
    assert vps.username == "administrator"
    assert vps.password == "fixture-secret"
    assert vps.private_key == "fixture-private-key"
    assert vps.host_key_fingerprint == "SHA256:fixture"
    assert vps.host_key_policy == "accept_any"
    assert vps.connect_timeout == 30


def test_overlay_ignores_malformed_numeric_values(monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "vps")
    monkeypatch.setenv("NANOBOT_VPS_PORT", "not-a-port")
    monkeypatch.setenv("NANOBOT_VPS_TIMEOUT", "0")
    config = _config()
    apply_render_execution_env(config)
    assert config.execution.vps.port == 22
    assert config.execution.vps.connect_timeout == 15


# --------------------------------------------------------------------------
# Saved values win over durable env values (no silent credential overwrite).
# --------------------------------------------------------------------------


def test_saved_vps_password_wins_over_durable_env(monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "vps")
    monkeypatch.setenv("NANOBOT_VPS_HOST", "vps.example.test")
    monkeypatch.setenv("NANOBOT_VPS_USERNAME", "administrator")
    monkeypatch.setenv("NANOBOT_VPS_PASSWORD", "fixture-secret")
    config = _config()
    config.execution.backend = "novita"
    config.execution.backend_source = "admin"
    config.execution.vps.host = "saved.example.test"
    config.execution.vps.username = "administrator"
    config.execution.vps.password = "existing-secret"

    apply_render_execution_env(config)

    assert config.execution.backend == "novita"
    assert config.execution.vps.host == "saved.example.test"
    # Port was untouched by the admin, so the durable env default still applies.
    assert config.execution.vps.port == 22
    assert config.execution.vps.password == "existing-secret"


def test_daytona_saved_key_wins_over_durable_env(monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "daytona")
    monkeypatch.setenv("NANOBOT_DAYTONA_API_KEY", "dtn_env_key")
    monkeypatch.setenv("NANOBOT_DAYTONA_API_URL", "https://env.daytona.invalid/api")
    config = _config()
    config.execution.daytona.api_key = "dtn_saved_key"
    config.execution.daytona.api_url = "https://saved.daytona.invalid/api"

    apply_render_execution_env(config)

    assert config.execution.daytona.api_key == "dtn_saved_key"
    assert config.execution.daytona.api_url == "https://saved.daytona.invalid/api"


def test_daytona_empty_key_is_restored_from_env(monkeypatch) -> None:
    """A wiped platform env still self-heals an unconfigured Daytona key."""
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "daytona")
    monkeypatch.setenv("NANOBOT_DAYTONA_API_KEY", "dtn_env_key")
    config = _config()

    apply_render_execution_env(config)

    assert config.execution.daytona.api_key == "dtn_env_key"


def test_daytona_outbound_proxy_url_is_applied(monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "daytona")
    monkeypatch.setenv(
        "NANOBOT_DAYTONA_OUTBOUND_PROXY_URL", "http://proxy.example.test:3128"
    )
    config = _config()

    apply_render_execution_env(config)

    assert config.execution.daytona.outbound_proxy_url == "http://proxy.example.test:3128"


def test_upstash_saved_api_key_wins_over_durable_env(monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "upstash")
    monkeypatch.setenv("NANOBOT_UPSTASH_API_KEY", "box_env_key")
    config = _config()
    config.execution.upstash.api_key = "box_saved_key"

    apply_render_execution_env(config)

    assert config.execution.upstash.api_key == "box_saved_key"
