"""Thinking/reasoning controls must reach every OpenAI-compatible path.

The GLM toggle fix landed in ``_model_thinking_style``, which is keyed on the
model slug alone. That is deliberate and it is what makes the fix apply to
paths that never touch the ``zhipu`` registry entry:

* **admin/custom OpenAI-compatible providers** -- created by
  :func:`create_dynamic_spec` from ``ProviderConfig.thinking_style``, with no
  keywords and no model list of their own;
* **the provider pool** -- up to 40 admin lanes, each its own base URL, key and
  model, wrapped by :class:`PoolProvider`.

Both were audited rather than assumed. The pool already threads
``on_thinking_delta`` and ``reasoning_effort`` into every lane's
``chat_stream``, so it needed no change; the tests below pin that, because a
silent regression there would only show up as a UI that stops narrating
reasoning on pooled deployments.

A custom provider serving a model whose slug we cannot recognise still gets the
escape hatch: declare ``thinking_style`` in the provider config and it is
appended to the style list unconditionally.
"""

from __future__ import annotations

import pytest

from nanobot.providers.base import LLMResponse
from nanobot.providers.openai_compat_provider import (
    OpenAICompatProvider,
    _model_thinking_style,
    _thinking_styles_for,
)
from nanobot.providers.pool_provider import PoolProvider
from nanobot.providers.registry import PROVIDERS, create_dynamic_spec

_MESSAGES = [{"role": "user", "content": "hi"}]


def _wire(spec, model: str, effort: str | None) -> dict[str, object]:
    provider = OpenAICompatProvider(api_key="sk-test", spec=spec, default_model=model)
    kwargs = provider._build_kwargs(_MESSAGES, None, model, 4096, 0.1, effort, None)
    return {k: kwargs[k] for k in ("reasoning_effort", "extra_body") if k in kwargs}


# --- admin / custom OpenAI-compatible providers ----------------------------


def test_a_custom_provider_got_the_glm_toggle_with_no_configuration_at_all() -> None:
    """The fix must not depend on the admin knowing to set ``thinking_style``.

    ``create_dynamic_spec`` is what a custom provider in ``providers`` config
    becomes. It carries no keywords and an empty ``thinking_style``, so before
    the prefix rule this request sent nothing at all -- ``reasoning_effort``
    "none" suppresses the top-level kwarg and there was no ``extra_body``
    toggle to carry the intent.
    """
    spec = create_dynamic_spec("my-gateway")
    assert spec.thinking_style == ""
    assert _wire(spec, "glm-4.6", "none")["extra_body"] == {
        "thinking": {"type": "disabled"}
    }


def test_a_custom_provider_declaring_a_style_still_reaches_the_wire() -> None:
    """The documented escape hatch for a model slug we cannot recognise."""
    spec = create_dynamic_spec("my-gateway", thinking_style="thinking_type")
    assert _wire(spec, "some-inhouse-model", "none")["extra_body"] == {
        "thinking": {"type": "disabled"}
    }
    assert _wire(spec, "some-inhouse-model", "high")["extra_body"] == {
        "thinking": {"type": "enabled"}
    }


def test_a_custom_provider_with_no_style_and_an_unknown_model_sends_no_toggle() -> None:
    """The boundary, stated so it is not mistaken for a regression.

    A slug we do not know, on a provider that declared no style, gets nothing:
    guessing a wire shape for an unknown endpoint is how you turn a working
    request into a 400.
    """
    spec = create_dynamic_spec("my-gateway")
    wire = _wire(spec, "some-inhouse-model", "none")
    assert "extra_body" not in wire
    assert "reasoning_effort" not in wire


def test_the_glm_prefix_and_a_declared_style_do_not_both_fire() -> None:
    """One toggle per request, not two of the same shape."""
    spec = create_dynamic_spec("my-gateway", thinking_style="thinking_type")
    assert _thinking_styles_for(spec, "glm-4.6") == ["thinking_type"]
    assert _model_thinking_style("glm-4.6") == "thinking_type"


def test_a_forced_thinking_glm_on_a_custom_provider_sends_nothing() -> None:
    """GLM-5.3 rejects ``disabled``; an unset style would send "enabled"."""
    spec = create_dynamic_spec("my-gateway")
    assert _wire(spec, "glm-5.3", "none").get("extra_body") is None


def test_the_builtin_zhipu_spec_agrees_with_the_slug_rule() -> None:
    """The registry entry and the slug rule must not drift apart."""
    zhipu = {s.name: s for s in PROVIDERS}["zhipu"]
    assert _wire(zhipu, "glm-4.6", "none")["extra_body"] == {
        "thinking": {"type": "disabled"}
    }


# --- the provider pool ----------------------------------------------------


class _Lane:
    """Minimal lane that records the callbacks and kwargs it was handed."""

    def __init__(self) -> None:
        self.thinking: list[str] = []
        self.content: list[str] = []
        self.reasoning_effort: object = "<unset>"

    async def chat_stream(self, **kwargs) -> LLMResponse:
        self.reasoning_effort = kwargs.get("reasoning_effort", "<absent>")
        on_thinking = kwargs.get("on_thinking_delta")
        on_content = kwargs.get("on_content_delta")
        if on_thinking is not None:
            await on_thinking("We")
            await on_thinking("igh.")
        if on_content is not None:
            await on_content("42")
        return LLMResponse(content="42", tool_calls=[], usage={})


@pytest.mark.asyncio
async def test_the_pool_forwards_thinking_deltas_from_its_lane() -> None:
    """A pooled deployment must narrate reasoning exactly like a direct one.

    The pool is the default on admin-managed deployments, so a dropped
    ``on_thinking_delta`` here would look like "GLM stopped streaming its
    thinking" while the provider itself was fine.
    """
    lane = _Lane()
    pool = PoolProvider(lanes=[({"id": "lane-1", "model": "glm-4.6"}, lane)])

    async def on_content_delta(delta: str) -> None:
        lane.content.append(delta)

    async def on_thinking_delta(delta: str) -> None:
        lane.thinking.append(delta)

    response = await pool.chat_stream(
        messages=_MESSAGES,
        model=None,
        on_content_delta=on_content_delta,
        on_thinking_delta=on_thinking_delta,
    )

    assert response.content == "42"
    assert lane.thinking == ["We", "igh."]
    assert lane.content == ["42"]


@pytest.mark.asyncio
async def test_the_pool_forwards_a_forced_speed_profile_to_the_lane() -> None:
    """"Force slow models faster" must survive the pool wrapper."""
    lane = _Lane()
    pool = PoolProvider(lanes=[({"id": "lane-1", "model": "glm-4.6"}, lane)])

    await pool.chat_stream(messages=_MESSAGES, model=None, reasoning_effort="none")

    assert lane.reasoning_effort == "none"
