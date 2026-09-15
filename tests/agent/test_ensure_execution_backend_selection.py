"""Boot-time seeding of ``execution.backend`` from the deployment environment.

The backend label used to be re-derived on every config load, which let a
durable platform variable silently revert an administrator's saved selection.
Seeding now happens once, at boot, and only while nobody has chosen explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ensure_render_config import ensure_render_defaults


def _write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _clear_backend_env(monkeypatch) -> None:
    monkeypatch.delenv("NANOBOT_EXECUTION_BACKEND", raising=False)


def test_fresh_config_seeds_backend_from_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "daytona")
    path = _write_config(tmp_path, {"tools": {"file": {"enable": True}}})

    assert ensure_render_defaults(path) is True

    saved = _read(path)
    assert saved["execution"]["backend"] == "daytona"
    assert saved["execution"]["backend_source"] == "env"


def test_env_matches_saved_label_without_provenance(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "daytona")
    path = _write_config(
        tmp_path,
        {"execution": {"backend": "daytona", "backend_source": "default"}},
    )

    ensure_render_defaults(path)

    saved = _read(path)
    assert saved["execution"]["backend"] == "daytona"
    # A label that was never attributed is stamped so later deploys changing the
    # env var cannot move it behind the operator's back.
    assert saved["execution"]["backend_source"] == "env"


def test_admin_selection_is_never_reverted_at_boot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "daytona")
    path = _write_config(
        tmp_path,
        {"execution": {"backend": "vps", "backend_source": "admin"}},
    )

    ensure_render_defaults(path)

    saved = _read(path)
    assert saved["execution"]["backend"] == "vps"
    assert saved["execution"]["backend_source"] == "admin"


def test_legacy_config_with_saved_non_default_backend_is_left_alone(
    tmp_path, monkeypatch
) -> None:
    """Upgrading must not switch a live deployment's provider.

    Configs written before ``backend_source`` existed carry a real backend label
    but no provenance; a non-default label is attributed to the administrator.
    """
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "upstash")
    path = _write_config(tmp_path, {"execution": {"backend": "vps"}})

    ensure_render_defaults(path)

    saved = _read(path)
    assert saved["execution"]["backend"] == "vps"
    assert saved["execution"]["backend_source"] == "admin"


def test_env_change_reapplies_while_source_is_env(tmp_path, monkeypatch) -> None:
    """A deployment still owned by the env keeps following it across redeploys."""
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "upstash")
    path = _write_config(
        tmp_path,
        {"execution": {"backend": "daytona", "backend_source": "env"}},
    )

    ensure_render_defaults(path)

    saved = _read(path)
    assert saved["execution"]["backend"] == "upstash"
    assert saved["execution"]["backend_source"] == "env"


def test_noop_without_env_and_noop_on_default_label(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NANOBOT_EXECUTION_BACKEND", raising=False)
    path = _write_config(tmp_path, {"execution": {"backend": "novita"}})
    assert _read(path) == json.loads(path.read_text(encoding="utf-8"))
    ensure_render_defaults(path)
    assert "backend_source" not in _read(path).get("execution", {})

    # An invalid env value is ignored rather than written to disk.
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "bogus")
    ensure_render_defaults(path)
    assert _read(path)["execution"]["backend"] == "novita"
