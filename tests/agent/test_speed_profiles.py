"""A speed profile is a force, so the tests pin what it overrides and what it must not.

The contract has two halves and both matter. Forced, it must reach the model
call -- a knob that is set and ignored is worse than no knob. Unforced, it must
be the identity mapping, because the default deployment's generation settings
are not this module's to change.
"""

from __future__ import annotations

import pytest

from nanobot.agent import speed


@pytest.fixture(autouse=True)
def _no_ambient_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test inherits a profile another test, or the shell, left behind."""
    monkeypatch.delenv(speed.ENV_VAR, raising=False)


_GENERATION = {"temperature": 0.1, "max_tokens": 8192, "reasoning_effort": None}


def test_with_nothing_forced_the_generation_is_untouched() -> None:
    """The unforced deployment must be byte-identical to its configuration."""
    assert speed.resolve_profile() is None
    assert speed.apply_to_generation(_GENERATION) == _GENERATION


def test_the_environment_variable_forces_a_profile_for_the_whole_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """How an operator forces it, including for a model with no per-request config."""
    monkeypatch.setenv(speed.ENV_VAR, "fast")

    assert speed.resolve_profile() == "fast"
    tuned = speed.apply_to_generation(_GENERATION)

    assert tuned["reasoning_effort"] == "none"
    assert tuned["max_tokens"] == 2_048
    # A profile changes the budget, not the creativity setting it knows nothing about.
    assert tuned["temperature"] == _GENERATION["temperature"]


def test_a_forced_profile_does_not_leak_past_its_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """One fast turn must not make every later turn fast."""
    monkeypatch.setenv(speed.ENV_VAR, "balanced")

    with speed.force_profile("fast"):
        assert speed.resolve_profile() == "fast"
        assert speed.apply_to_generation(_GENERATION)["max_tokens"] == 2_048

    assert speed.resolve_profile() == "balanced"
    assert speed.apply_to_generation(_GENERATION)["max_tokens"] == 4_096


def test_nesting_restores_the_outer_force(monkeypatch: pytest.MonkeyPatch) -> None:
    with speed.force_profile("fast"):
        with speed.force_profile("balanced"):
            assert speed.resolve_profile() == "balanced"
        assert speed.resolve_profile() == "fast"


def test_an_explicit_argument_beats_both_ambient_forces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(speed.ENV_VAR, "fast")

    with speed.force_profile("fast"):
        assert speed.resolve_profile("balanced") == "balanced"


def test_a_typo_in_the_environment_variable_is_no_profile_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A performance knob must never be the thing that takes the agent down."""
    monkeypatch.setenv(speed.ENV_VAR, "turb0")

    assert speed.resolve_profile() is None
    assert speed.apply_to_generation(_GENERATION) == _GENERATION


def test_a_profile_never_invents_a_key_the_caller_did_not_have() -> None:
    """Providers differ in what they accept; the profile must not widen the call."""
    with speed.force_profile("fast"):
        tuned = speed.apply_to_generation({"temperature": 0.2})

    assert tuned == {"temperature": 0.2}


def test_a_profile_overrides_a_provider_default_rather_than_deferring_to_it() -> None:
    """`reasoning_effort=None` is the provider default -- exactly what a force replaces."""
    assert _GENERATION["reasoning_effort"] is None

    with speed.force_profile("fast"):
        assert speed.apply_to_generation(_GENERATION)["reasoning_effort"] == "none"


def test_every_named_profile_is_describable() -> None:
    """The model has to be able to say which one is on."""
    for name in speed.known_profiles():
        described = speed.describe(name)
        assert name in described or name == speed.PROFILE_NONE


def test_the_unforced_description_names_the_switch_to_set() -> None:
    assert speed.ENV_VAR in speed.describe()
