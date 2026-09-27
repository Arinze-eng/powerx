"""Prompt-cache policy: mode resolution, marker safety and the unmarked retry.

The behaviour under test was measured against live endpoints:

* an OpenAI-compatible gateway (Kyma/qwen3.7-flash) served a repeated 3.9k
  prefix from cache and reported ``cached_tokens`` - i.e. ``auto`` is enough;
* another (the configured gemini-proxy) returned HTTP 200 for a marked request
  while dropping the marked block entirely - i.e. ``markers`` must stay opt-in
  and a hard refusal must not fail the turn.

"""

from __future__ import annotations

import asyncio

import pytest

from nanobot.providers.prompt_cache import (
    cache_hit_pct,
    cache_marker_rejection,
    markers_allowed,
    normalize_cache_mode,
    resolve_cache_mode,
    strip_cache_markers,
)


class _Spec:
    def __init__(self, name: str = "custom", caching: bool = False) -> None:
        self.name = name
        self.supports_prompt_caching = caching


class _FakeBadRequest(Exception):
    status_code = 400


# --- mode resolution -------------------------------------------------------


def test_normalize_accepts_known_modes_and_aliases():
    assert normalize_cache_mode("AUTO") == "auto"
    assert normalize_cache_mode("markers") == "markers"
    assert normalize_cache_mode("off") == "off"
    assert normalize_cache_mode("true") == "markers"
    assert normalize_cache_mode("implicit") == "auto"
    assert normalize_cache_mode("") is None
    assert normalize_cache_mode(None) is None
    assert normalize_cache_mode("banana") is None


def test_configured_mode_wins_over_everything():
    mode = resolve_cache_mode(
        configured="off",
        spec=_Spec(caching=True),
        environ={"POWERX_PROMPT_CACHE": "markers"},
    )
    assert mode == "off"


def test_env_mode_applies_when_nothing_is_saved():
    assert resolve_cache_mode(environ={"POWERX_PROMPT_CACHE": "markers"}) == "markers"
    assert resolve_cache_mode(environ={"POWERX_PROMPT_CACHE": "off"}) == "off"


def test_legacy_force_markers_env_still_means_markers():
    assert resolve_cache_mode(environ={"POWERX_FORCE_CACHE_MARKERS": "1"}) == "markers"


def test_cache_capable_spec_defaults_to_markers_and_plain_spec_to_auto():
    assert resolve_cache_mode(spec=_Spec(name="anthropic", caching=True)) == "markers"
    assert resolve_cache_mode(spec=_Spec(name="custom")) == "auto"


def test_markers_allowed_only_for_an_explicit_choice_or_claude():
    assert markers_allowed("off", is_claude=True, spec=_Spec(caching=True)) is False
    assert markers_allowed("markers", is_claude=False, spec=_Spec()) is True
    # ``auto`` keeps the historical Claude-only behaviour.
    assert markers_allowed("auto", is_claude=True, spec=_Spec(caching=True)) is True
    assert markers_allowed("auto", is_claude=True, spec=_Spec()) is False
    assert markers_allowed("auto", is_claude=False, spec=_Spec(caching=True)) is False


# --- marker rejection / stripping -----------------------------------------


def test_rejection_detected_for_a_refusal_that_names_cache_control():
    assert cache_marker_rejection(_FakeBadRequest("unsupported parameter: cache_control")) is True


def test_rejection_is_not_claimed_for_unrelated_errors():
    assert cache_marker_rejection(_FakeBadRequest("model not found")) is False
    assert cache_marker_rejection(RuntimeError("connection reset")) is False


def test_strip_removes_markers_from_messages_and_tools():
    kwargs = {
        "messages": [
            {
                "role": "system",
                "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}],
            },
            {"role": "user", "content": "hello"},
        ],
        "tools": [{"type": "function", "cache_control": {"type": "ephemeral"}, "function": {}}],
    }
    stripped, removed = strip_cache_markers(kwargs)
    assert removed == 2
    assert "cache_control" not in stripped["messages"][0]["content"][0]
    assert "cache_control" not in stripped["tools"][0]
    # The original stays untouched so the caller can retry with it if needed.
    assert "cache_control" in kwargs["messages"][0]["content"][0]


def test_strip_reports_zero_when_there_is_nothing_to_strip():
    _, removed = strip_cache_markers({"messages": [{"role": "user", "content": "hi"}]})
    assert removed == 0


def test_cache_hit_pct_is_bounded_and_safe():
    assert cache_hit_pct(1000, 900) == 90.0
    assert cache_hit_pct(1000, 0) == 0.0
    assert cache_hit_pct(0, 10) == 0.0
    assert cache_hit_pct(100, 500) == 100.0


# --- provider integration --------------------------------------------------


class _Completions:
    def __init__(self, failures: int) -> None:
        self.calls: list[dict] = []
        self._failures = failures

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self._failures:
            raise _FakeBadRequest("invalid request: cache_control is not supported")
        return {"ok": True}


class _Client:
    def __init__(self, failures: int = 0) -> None:
        self.chat = type("_Chat", (), {"completions": _Completions(failures)})()


def _provider(spec: _Spec, configured: str | None = None):
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider

    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
    provider._spec = spec
    provider._prompt_cache = configured
    provider._cache_marker_disabled = False
    provider.api_base = "https://example.test/v1"
    return provider


def test_provider_adds_markers_for_claude_under_auto():
    provider = _provider(_Spec(name="anthropic", caching=True))
    assert provider._cache_markers_for("claude-3-5-sonnet") is True


def test_provider_does_not_mark_a_plain_openai_compatible_endpoint():
    provider = _provider(_Spec(name="custom"))
    assert provider._cache_markers_for("qwen3.7-flash") is False


def test_provider_marks_a_plain_endpoint_only_when_asked():
    provider = _provider(_Spec(name="custom"), configured="markers")
    assert provider._cache_markers_for("qwen3.7-flash") is True


def test_refused_markers_are_retried_without_them_and_stay_off():
    provider = _provider(_Spec(name="custom"), configured="markers")
    client = _Client(failures=1)
    kwargs = {
        "messages": [
            {
                "role": "system",
                "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}],
            }
        ],
        "tools": [{"type": "function", "cache_control": {"type": "ephemeral"}, "function": {}}],
    }

    result = asyncio.run(provider._create_chat_with_cache_fallback(client, kwargs))

    assert result == {"ok": True}
    completions = client.chat.completions
    assert len(completions.calls) == 2
    assert "cache_control" in completions.calls[0]["messages"][0]["content"][0]
    assert "cache_control" not in completions.calls[1]["messages"][0]["content"][0]
    assert provider._cache_marker_disabled is True
    # Now that markers are known to be refused, later requests skip them.
    assert provider._cache_markers_for("qwen3.7-flash") is False


def test_an_unrelated_failure_is_not_swallowed():
    provider = _provider(_Spec(name="custom"), configured="markers")
    client = _Client(failures=1)

    class _Opaque(Exception):
        status_code = 500

    async def _boom(**kwargs):
        raise _Opaque("upstream exploded")

    client.chat.completions.create = _boom  # type: ignore[assignment]
    with pytest.raises(_Opaque):
        asyncio.run(provider._create_chat_with_cache_fallback(client, {"messages": []}))


def test_no_retry_when_there_were_no_markers_to_strip():
    provider = _provider(_Spec(name="custom"))
    client = _Client(failures=1)
    with pytest.raises(_FakeBadRequest):
        asyncio.run(
            provider._create_chat_with_cache_fallback(
                client, {"messages": [{"role": "user", "content": "hi"}]}
            )
        )
    assert len(client.chat.completions.calls) == 1
