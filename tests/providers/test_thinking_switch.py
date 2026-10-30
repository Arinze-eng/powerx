"""Tests for the thinking on/off switch (POWERX_THINKING) and Nemotron toggle.

Speed profile "fast" sends reasoning_effort="none". Before this change nothing
on the wire turned thinking off for NVIDIA Nemotron, so the model kept
thinking on every step. These tests pin the new behaviour.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import PROVIDERS

NEMOTRON = "nvidia/nemotron-3-ultra-550b-a55b"
PLAIN = "some-vendor/plain-chat-model"


def _spec(name: str):
    return {s.name: s for s in PROVIDERS}[name]


def _provider(model: str, spec_name: str = "nvidia") -> OpenAICompatProvider:
    return OpenAICompatProvider(
        api_key="nvapi-test",
        default_model=model,
        spec=_spec(spec_name),
    )


def _messages() -> list[dict[str, Any]]:
    return [{"role": "user", "content": "hi"}]


def _build(provider: OpenAICompatProvider, effort: str | None) -> dict[str, Any]:
    return provider._build_kwargs(
        _messages(), None, None, 1024, 0.7, effort, None,
    )


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("POWERX_THINKING", raising=False)
    monkeypatch.delenv("POWERX_THINKING_WIRE", raising=False)


def test_nemotron_fast_profile_turns_thinking_off():
    kwargs = _build(_provider(NEMOTRON), "none")
    assert kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_nemotron_medium_effort_turns_thinking_on():
    kwargs = _build(_provider(NEMOTRON), "medium")
    assert kwargs["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}


def test_env_off_overrides_a_thinking_effort():
    import os

    os.environ["POWERX_THINKING"] = "off"
    try:
        kwargs = _build(_provider(NEMOTRON), "high")
    finally:
        del os.environ["POWERX_THINKING"]
    assert kwargs["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_env_on_overrides_no_effort_for_nemotron(monkeypatch):
    monkeypatch.setenv("POWERX_THINKING", "on")
    kwargs = _build(_provider(NEMOTRON), None)
    assert kwargs["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}


def test_env_off_reaches_a_model_with_no_builtin_toggle(monkeypatch):
    monkeypatch.setenv("POWERX_THINKING", "off")
    kwargs = _build(_provider(PLAIN, spec_name="custom"), "medium")
    assert kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_env_wire_override_picks_another_style(monkeypatch):
    monkeypatch.setenv("POWERX_THINKING", "off")
    monkeypatch.setenv("POWERX_THINKING_WIRE", "enable_thinking")
    kwargs = _build(_provider(PLAIN, spec_name="custom"), "medium")
    assert kwargs["extra_body"] == {"enable_thinking": False}


def test_unknown_wire_style_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("POWERX_THINKING", "off")
    monkeypatch.setenv("POWERX_THINKING_WIRE", "not-a-style")
    kwargs = _build(_provider(PLAIN, spec_name="custom"), "medium")
    assert kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_unknown_mode_value_is_treated_as_auto(monkeypatch):
    monkeypatch.setenv("POWERX_THINKING", "turbo")
    kwargs = _build(_provider(NEMOTRON), "none")
    assert kwargs["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    kwargs = _build(_provider(NEMOTRON), "medium")
    assert kwargs["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}


def test_auto_mode_sends_nothing_for_a_plain_model_without_effort():
    kwargs = _build(_provider(PLAIN, spec_name="custom"), None)
    assert "extra_body" not in kwargs or "chat_template_kwargs" not in kwargs.get(
        "extra_body", {}
    )


def test_auto_mode_sends_nothing_for_plain_model_with_effort():
    kwargs = _build(_provider(PLAIN, spec_name="custom"), "none")
    assert "chat_template_kwargs" not in kwargs.get("extra_body", {})


def test_kimi_always_thinking_is_not_disabled_by_env_off(monkeypatch):
    monkeypatch.setenv("POWERX_THINKING", "off")
    kwargs = _build(_provider("kimi-k2.6-thinking", spec_name="moonshot"), "none")
    thinking = kwargs.get("extra_body", {}).get("thinking")
    assert thinking != {"type": "disabled"}
