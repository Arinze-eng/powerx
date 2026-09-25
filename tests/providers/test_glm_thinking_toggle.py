"""Tests for GLM thinking-mode toggling.

Zhipu's GLM toggles reasoning with the same wire shape as DeepSeek --
``{"thinking": {"type": "enabled"|"disabled"}}`` -- which is exactly what the
``thinking_type`` style in ``_THINKING_STYLE_MAP`` already builds. The ``zhipu``
ProviderSpec did not declare a style and GLM was not in the model map, so the
shape was never sent.

Measured consequence before the fix, for ``glm-4.6`` on provider ``zhipu``:

    reasoning_effort="none" -> NOTHING SENT  (thinking stayed ON)
    reasoning_effort="low"  -> {"reasoning_effort": "low"}  (ignored by Zhipu)

``"none"`` is the value that suppresses the top-level ``reasoning_effort``
kwarg, and with no ``extra_body`` toggle built there was nothing left to carry
the intent -- so the one value that should turn thinking off was the one that
silently sent nothing. This made a "fast"/"cheap" profile a no-op on the exact
model it was meant to speed up.

Sources:
  https://docs.z.ai/guides/capabilities/thinking-mode
  https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode

GLM-5.3 and GLM-5.3-FLASH force thinking and REJECT ``type: disabled`` with an
error, so they must receive no toggle at all -- and GLM-4 and earlier have no
thinking mode and would reject the field too.
"""

from __future__ import annotations

import pytest

from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import PROVIDERS

_MESSAGES = [{"role": "user", "content": "hi"}]


def _provider() -> OpenAICompatProvider:
    spec = {s.name: s for s in PROVIDERS}["zhipu"]
    return OpenAICompatProvider(api_key="sk-test", spec=spec, default_model="glm-4.6")


def _wire(model: str, effort: str | None) -> dict[str, object]:
    """The reasoning-relevant part of the request body for one call."""
    kwargs = _provider()._build_kwargs(_MESSAGES, None, model, 4096, 0.1, effort, None)
    return {k: kwargs[k] for k in ("reasoning_effort", "extra_body") if k in kwargs}


# --- the fix: a togglable GLM actually gets the toggle ----------------------


@pytest.mark.parametrize("model", ["glm-4.6", "glm-4.5", "glm-4.5-air", "glm-4.7"])
def test_a_togglable_glm_is_told_to_stop_thinking(model: str) -> None:
    """The headline lever of a speed profile: reasoning explicitly off."""
    assert _wire(model, "none")["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.parametrize("model", ["glm-4.6", "glm-4.5", "glm-4.7"])
def test_a_togglable_glm_is_told_to_think_when_effort_is_asked_for(model: str) -> None:
    wire = _wire(model, "high")
    assert wire["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "reasoning_effort" not in wire or wire["reasoning_effort"] == "high"


def test_the_prefix_rule_covers_a_glm_that_does_not_exist_yet() -> None:
    """A rolling release must not need a code change to get its toggle."""
    assert _wire("glm-4.7", "none")["extra_body"] == {"thinking": {"type": "disabled"}}


def test_glm_47_plus_is_togglable_without_a_slug_list_entry() -> None:
    """Guards the prefix rule against being replaced by a stale exact list."""
    assert _wire("glm-4.7-flash", "none").get("extra_body") == {
        "thinking": {"type": "disabled"}
    }


# --- what must NOT be sent --------------------------------------------------


@pytest.mark.parametrize("model", ["glm-5.3", "glm-5.3-flash"])
def test_a_forced_thinking_glm_is_never_sent_the_disabled_toggle(model: str) -> None:
    """These reject `disabled` with an error, so sending nothing is the safe answer."""
    assert "extra_body" not in _wire(model, "none")


@pytest.mark.parametrize("model", ["glm-4-plus", "glm-4-flash", "glm-4-air"])
def test_a_glm_with_no_thinking_mode_is_not_sent_the_field(model: str) -> None:
    """GLM-4 and earlier would reject a parameter they do not have."""
    assert "extra_body" not in _wire(model, "none")
    assert "extra_body" not in _wire(model, "high")


@pytest.mark.parametrize("model", ["glm-4.6", "glm-5.3", "glm-4-plus"])
def test_an_unset_effort_still_preserves_the_provider_default(model: str) -> None:
    """Omitting the setting must leave each provider's own default alone."""
    assert _wire(model, None) == {}


# --- and the resolution itself ---------------------------------------------


def test_the_style_map_is_unchanged_for_models_that_already_worked() -> None:
    """This change is additive: DeepSeek and Kimi keep their existing style."""
    deepseek = OpenAICompatProvider(
        api_key="sk-test",
        spec={s.name: s for s in PROVIDERS}["deepseek"],
        default_model="deepseek-v4-pro",
    )
    kwargs = deepseek._build_kwargs(_MESSAGES, None, "deepseek-v4-pro", 4096, 0.1, "none", None)
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
