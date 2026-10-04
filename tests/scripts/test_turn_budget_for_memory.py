"""The turn budget must be capped by the container's memory, not by a constant.

A turn holds its whole message list in RAM for its entire life. The runner
already knows how to spill an oversized result to disk and hand the model a path
to read it back -- but that spill only engages above ``maxToolResultChars``, and
the deployed value was 131072. Ordinary results therefore stayed resident, the
list grew, and the container died mid-turn at 99.5% of its cgroup charge while
its own grade reported ``ok`` at 65% -- because the grade reads anonymous memory
and the ceiling enforces the whole charge.

The deployment has a 6 GB NVMe volume and a 512 MB memory plan, so the scarce
resource is RAM and the abundant one is disk. These tests pin that the boot
reconciler spends the abundant one.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ensure_render_config.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("ensure_render_config", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return _load_module()


def _config(chars: int, iterations: int) -> dict:
    return {"agents": {"defaults": {
        "maxToolResultChars": chars,
        "maxToolIterations": iterations,
    }}}


def test_small_container_gets_a_small_result_budget(module, monkeypatch) -> None:
    monkeypatch.setattr(module, "_container_memory_limit_mb", lambda: 488.3)
    data = _config(131_072, 120)

    assert module._ensure_turn_budget_for_memory(data) is True
    assert data["agents"]["defaults"]["maxToolResultChars"] == 16_384
    assert data["agents"]["defaults"]["maxToolIterations"] == 40


def test_large_container_keeps_its_configured_budget(module, monkeypatch) -> None:
    """A host that really has the memory must not be downgraded to the floor."""
    monkeypatch.setattr(module, "_container_memory_limit_mb", lambda: 4096.0)
    data = _config(131_072, 120)

    module._ensure_turn_budget_for_memory(data)

    assert data["agents"]["defaults"]["maxToolResultChars"] == 65_536
    assert data["agents"]["defaults"]["maxToolIterations"] == 120


def test_budget_is_only_lowered_never_raised(module, monkeypatch) -> None:
    """An operator who chose a tighter cap than ours keeps it."""
    monkeypatch.setattr(module, "_container_memory_limit_mb", lambda: 488.3)
    data = _config(8_192, 10)

    assert module._ensure_turn_budget_for_memory(data) is False
    assert data["agents"]["defaults"] == {"maxToolResultChars": 8_192, "maxToolIterations": 10}


def test_unknown_memory_leaves_the_config_alone(module, monkeypatch) -> None:
    """No limit must mean no opinion, not a silent downgrade to the floor."""
    monkeypatch.setattr(module, "_container_memory_limit_mb", lambda: None)
    data = _config(131_072, 120)

    assert module._ensure_turn_budget_for_memory(data) is False
    assert data["agents"]["defaults"]["maxToolResultChars"] == 131_072


def test_missing_sections_do_not_raise(module, monkeypatch) -> None:
    monkeypatch.setattr(module, "_container_memory_limit_mb", lambda: 488.3)

    assert module._ensure_turn_budget_for_memory({}) is False
    assert module._ensure_turn_budget_for_memory({"agents": {}}) is False
    assert module._ensure_turn_budget_for_memory({"agents": {"defaults": "junk"}}) is False


def test_v2_max_sentinel_is_no_limit(module, monkeypatch) -> None:
    """``memory.max`` literally contains the string "max" when unbounded."""
    target = module.Path("/sys/fs/cgroup/memory.max")

    class _FakePath:
        def __init__(self, body):
            self._body = body

        def read_text(self):
            return self._body

    monkeypatch.setattr(
        module, "Path",
        lambda p: _FakePath("max\n") if str(p) == str(target) else _FakePath(""),
    )

    assert module._container_memory_limit_mb() is None


def test_v2_byte_value_is_converted_to_mb(module, monkeypatch) -> None:
    target = module.Path("/sys/fs/cgroup/memory.max")

    class _FakePath:
        def __init__(self, body):
            self._body = body

        def read_text(self):
            return self._body

    monkeypatch.setattr(
        module, "Path",
        lambda p: _FakePath(str(512 * 1024 * 1024))
        if str(p) == str(target) else _FakePath(""),
    )

    assert module._container_memory_limit_mb() == pytest.approx(512.0)


def test_deployed_template_is_already_within_the_small_budget(module) -> None:
    """Fresh installs must not start at the value that killed the container."""
    repo = Path(__file__).resolve().parents[2]
    data = json.loads((repo / "render-config.json").read_text())
    defaults = data["agents"]["defaults"]

    assert defaults["maxToolResultChars"] <= 16_384
    assert defaults["maxToolIterations"] <= 40
