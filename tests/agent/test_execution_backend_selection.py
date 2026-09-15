"""End-to-end regression: the administrator's backend selection must stick.

Reported symptom: selecting VPS in the admin panel sometimes ran tasks on
Daytona instead (same confusion with Upstash). Two defects combined to cause
it, and both are pinned here:

1. ``apply_render_execution_env`` rewrote ``execution.backend`` on every config
   load using an asymmetric "was this explicitly chosen?" heuristic that only
   protected Daytona/Upstash.
2. ``_save_execution_settings`` assigned the VPS fields unconditionally, so
   saving the shared form without re-typing the host wiped it -- which in turn
   made the VPS selection look unconfigured and get reverted.
"""

from __future__ import annotations

import json

from nanobot.config.loader import load_config
from scripts.ensure_render_config import ensure_render_defaults


def _save(admin_registry, payload: dict) -> dict:
    response = admin_registry._save_execution_settings(payload, refresh_runtime_config=None)
    return json.loads(bytes(response.body).decode())


def _vps_payload(host: str = "203.0.113.10", username: str = "root") -> dict:
    return {
        "backend": "vps",
        "host": host,
        "username": username,
        "password": "fixture-secret",
        "hostKeyFingerprint": "SHA256:examplefingerprintplaceholder",
        "hostKeyPolicy": "fingerprint",
        "workspaceDir": "/workspace",
        "port": "22",
        "connectTimeout": "15",
    }


def test_vps_selection_survives_daytona_pinned_env(tmp_path, monkeypatch) -> None:
    """The exact reported failure: pick VPS while the org env pins Daytona."""
    import nanobot.admin_registry as admin_registry

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "daytona")
    monkeypatch.setenv("NANOBOT_DAYTONA_API_KEY", "dtn_poison_key")

    body = _save(admin_registry, _vps_payload())
    assert body["ok"] is True
    assert body["backend"] == "vps"
    assert body["backendSource"] == "admin"

    # Reload the way the runtime does on every request: config file + overlay.
    config = load_config(config_path)
    assert config.execution.backend == "vps"
    assert config.execution.backend_source == "admin"


def test_saving_another_backend_does_not_wipe_vps_details(tmp_path, monkeypatch) -> None:
    """The shared form submits blank VPS fields; blanks must preserve, not clear."""
    import nanobot.admin_registry as admin_registry

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)

    _save(admin_registry, _vps_payload())

    # Admin switches to Daytona and the form posts empty VPS fields.
    body = _save(admin_registry, {"backend": "daytona", "daytonaApiKey": "dtn_other_key"})
    assert body["backend"] == "daytona"
    assert body["vps"]["host"] == "203.0.113.10"
    assert body["vps"]["username"] == "root"
    assert body["vps"]["passwordConfigured"] is True

    # Switching back to VPS still has a complete, usable target.
    body = _save(admin_registry, {"backend": "vps"})
    assert body["backend"] == "vps"
    assert body["vps"]["host"] == "203.0.113.10"


def test_selecting_vps_without_credentials_is_rejected(tmp_path, monkeypatch) -> None:
    """A backend cannot be selected into a broken state; the save fails loudly."""
    import nanobot.admin_registry as admin_registry

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)
    monkeypatch.delenv("NANOBOT_VPS_HOST", raising=False)

    response = admin_registry._save_execution_settings(
        {"backend": "vps"}, refresh_runtime_config=None
    )
    assert response.status_code == 400
    assert not config_path.exists() or load_config(config_path).execution.backend != "vps"


def test_boot_bootstrap_does_not_override_admin_selection(tmp_path, monkeypatch) -> None:
    """A restart must not move a persisted admin choice."""
    import nanobot.admin_registry as admin_registry

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)
    _save(admin_registry, _vps_payload())
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "daytona")

    ensure_render_defaults(config_path)

    assert load_config(config_path).execution.backend == "vps"


def test_all_four_backends_are_selected_verbatim(tmp_path, monkeypatch) -> None:
    """No backend is privileged: every explicit choice round-trips unchanged."""
    import nanobot.admin_registry as admin_registry

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)
    monkeypatch.setenv("NANOBOT_VPS_HOST", "203.0.113.10")
    monkeypatch.setenv("NANOBOT_VPS_USERNAME", "root")
    monkeypatch.setenv("NANOBOT_VPS_PASSWORD", "fixture-secret")
    monkeypatch.setenv("NANOBOT_UPSTASH_API_KEY", "box_fixture_key")
    monkeypatch.setenv("NANOBOT_DAYTONA_API_KEY", "dtn_fixture_key")

    for backend in ("novita", "vps", "upstash", "daytona"):
        # Each selection is made while a *different* backend is pinned in env,
        # which is the condition that used to cause the silent revert.
        for env_backend in ("novita", "vps", "upstash", "daytona"):
            monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", env_backend)
            body = _save(admin_registry, {"backend": backend})
            assert body["backend"] == backend, f"{env_backend} env poisoned {backend} save"
            assert body["backendSource"] == "admin"
            assert load_config(config_path).execution.backend == backend
