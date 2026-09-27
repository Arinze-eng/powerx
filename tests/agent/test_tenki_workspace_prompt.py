"""A rotation-only Tenki config must still render the sandbox orientation map.

Reported live as "when running mt5 and wine in tenki it stays too long". The
sandbox itself was fine — a cold Wine + MT5 + bridge install measured 138 s on
Tenki, a warm re-install 5 s. What was broken was the PROMPT: the system-prompt
builder gated its Tenki arm on the singular ``api_key``, which rotation
deliberately leaves empty, so it returned "" and dropped the whole sandbox
orientation section — 10,648 characters, and with it the entire MT5/Wine
playbook that tells the model an install takes ~2 minutes, that ``status`` is
polled to ``stage="done"``, and that it must NOT re-run ``install``.

That is the same defect Stage C fixed at three tool-level sites; this is the
fourth site, in the prompt builder, and it is the one the model actually reads.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.context import ContextBuilder, _tenki_keys_configured
from nanobot.config.schema import ExecutionBackendConfig, TenkiExecutionConfig

LANES = [
    "tk_adxSo0Z8p7TlzYZaIYvJIcP63fXD25RvpiawYQ39Lpf",
    "tk_juNBSrLkn530veHfi3FxuNCtX6udOKrPJ3IE6Li2ela",
    "tk_LI2rBqp93mG3UB2whcgXowcNN43Ulywe0hpTDp3o6lH",
]


def _section(monkeypatch: pytest.MonkeyPatch, **execution: Any) -> str:
    """Render the section against a pinned execution config."""
    holder = SimpleNamespace(execution=SimpleNamespace(**execution))

    import nanobot.config.loader as loader_mod
    import nanobot.execution_env as env_mod

    monkeypatch.setattr(env_mod, "apply_render_execution_env", lambda _cfg: holder)
    monkeypatch.setattr(loader_mod, "load_config", lambda *_a, **_k: holder)
    return ContextBuilder._build_sandbox_workspace_section()


def _tenki(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"backend": "tenki", "tenki": SimpleNamespace(**fields)}
    return base


# --------------------------------------------------------------- the predicate


@pytest.mark.parametrize(
    ("tenki", "expected"),
    [
        ({}, False),
        ({"api_key": ""}, False),
        ({"api_keys": []}, False),
        ({"api_key": "   "}, False),
        ({"api_keys": ["  ", ""], "api_key": ""}, False),
        ({"api_key": "tk_single"}, True),
        ({"api_keys": LANES}, True),
        ({"api_keys": [LANES[2]], "api_key": ""}, True),
    ],
)
def test_the_prompt_gate_accepts_either_key_form(
    tenki: dict[str, Any], expected: bool
) -> None:
    assert _tenki_keys_configured(SimpleNamespace(**tenki)) is expected


def test_the_prompt_gate_treats_a_missing_config_as_unconfigured() -> None:
    assert _tenki_keys_configured(None) is False


# --------------------------------------------------------------- the section


def test_a_rotation_only_config_still_renders_the_workspace_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: three lanes and no singular key must NOT return ""."""
    section = _section(monkeypatch, **_tenki(api_key="", api_keys=list(LANES)))
    assert section, "rotation-only Tenki config dropped the orientation section"
    assert "/home/tenki" in section


def test_rotation_only_matches_the_single_key_section_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotation must be a no-op: it only changes WHICH workspace, not the prompt."""
    rotated = _section(monkeypatch, **_tenki(api_key="", api_keys=list(LANES)))
    single = _section(monkeypatch, **_tenki(api_key=LANES[0], api_keys=[]))
    assert rotated == single


def test_the_section_carries_the_mt5_playbook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards against a fix that restores length but not the content that matters.

    Each assertion is a rule the model needs to avoid the "it stays too long"
    behaviour: the install has a known duration, it is polled, it is not re-run.
    """
    section = _section(monkeypatch, **_tenki(api_key="", api_keys=list(LANES)))
    lowered = section.lower()
    assert "mt5_sandbox" in lowered
    assert "wine" in lowered
    assert "~2 min" in lowered or "2 min" in lowered
    assert 'stage="done"' in section
    assert "do not re-run install" in lowered
    assert "mt5-trading" in lowered


def test_a_tenki_config_with_no_key_at_all_still_returns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate must stay a gate — it is not a blanket "always render"."""
    assert _section(monkeypatch, **_tenki(api_key="", api_keys=[])) == ""


def test_the_fix_does_not_loosen_the_other_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other backend still needs its own credential, unchanged."""
    for backend, field in (
        ("runloop", "api_key"),
        ("daytona", "api_key"),
        ("upstash", "api_key"),
    ):
        empty = _section(
            monkeypatch, backend=backend, **{backend: SimpleNamespace(**{field: ""})}
        )
        assert empty == "", f"{backend} rendered a section with no credential"


def test_the_real_config_objects_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the actual pydantic models, not just SimpleNamespace stand-ins."""
    execution = ExecutionBackendConfig(
        backend="tenki",
        tenki=TenkiExecutionConfig(api_key="", api_keys=list(LANES)),
    )
    holder = SimpleNamespace(execution=execution)

    import nanobot.config.loader as loader_mod
    import nanobot.execution_env as env_mod

    monkeypatch.setattr(env_mod, "apply_render_execution_env", lambda _cfg: holder)
    monkeypatch.setattr(loader_mod, "load_config", lambda *_a, **_k: holder)

    section = ContextBuilder._build_sandbox_workspace_section()
    assert section
    assert "/home/tenki" in section
