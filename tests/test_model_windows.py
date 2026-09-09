"""Automatic context-window resolution — stops silent token over-spend.

The money leak this fixes: config default window is 200k, but admin-loaded
OpenAI-compatible models are often 8k–128k. If the governor trusts 200k it never
compacts, so every turn re-sends (and pays for) the whole history. Resolution
must be automatic yet never override a deliberate admin choice.
"""

from __future__ import annotations

import pytest

from nanobot.utils.model_windows import (
    CONFIG_DEFAULT_WINDOW,
    WindowResolver,
    auto_tune_runtime,
    derive_max_output,
    resolve_context_window,
)


class TestNameHeuristics:
    @pytest.mark.parametrize(
        "model_id,expected",
        [
            ("openai/gpt-4o-mini", 128_000),
            ("gpt-4.1-nano", 128_000),
            ("deepseek/deepseek-chat", 64_000),
            ("qwen/qwen2.5-coder-32b-instruct", 32_000),
            ("meta-llama/llama-3.1-70b-instruct", 128_000),
            ("anthropic/claude-3-5-sonnet", 200_000),
            ("mistralai/mistral-large", 128_000),
            ("google/gemini-1.5-flash", 200_000),
            ("x-ai/grok-2", 128_000),
        ],
    )
    def test_known_models(self, model_id, expected) -> None:
        assert resolve_context_window(model_id, CONFIG_DEFAULT_WINDOW) == expected

    def test_unknown_preserves_configured(self) -> None:
        # Unknown model with no metadata must NOT be force-shrunk (that would
        # trigger needless compaction on a genuinely-large-window model). We keep
        # whatever the caller already had. Protection for unknown models is an
        # explicit operator choice via POWERX_CONTEXT_WINDOW_TOKENS.
        assert resolve_context_window("vendor/weird-finetune-9000", CONFIG_DEFAULT_WINDOW) == CONFIG_DEFAULT_WINDOW
        assert resolve_context_window("vendor/weird-finetune-9000", 48_000) == 48_000


class TestPrecedence:
    def test_explicit_admin_value_respected(self) -> None:
        # A non-default configured window always wins over heuristics.
        assert resolve_context_window("openai/gpt-4o", 30_000) == 30_000

    def test_default_config_enables_auto(self) -> None:
        # The untouched default is the ONLY value that unlocks detection.
        assert resolve_context_window("deepseek/deepseek-chat", CONFIG_DEFAULT_WINDOW) == 64_000

    def test_env_override_beats_all(self, monkeypatch) -> None:
        monkeypatch.setenv("POWERX_CONTEXT_WINDOW_TOKENS", "12345")
        assert resolve_context_window("openai/gpt-4o", 30_000) == 12345

    def test_invalid_env_ignored(self, monkeypatch) -> None:
        monkeypatch.setenv("POWERX_CONTEXT_WINDOW_TOKENS", "not-a-number")
        assert resolve_context_window("deepseek/deepseek-chat", CONFIG_DEFAULT_WINDOW) == 64_000

    def test_allow_auto_false_keeps_configured(self) -> None:
        assert resolve_context_window("openai/gpt-4o", 50_000, allow_auto=False) == 50_000

    def test_empty_model_id_safe(self) -> None:
        assert resolve_context_window("", CONFIG_DEFAULT_WINDOW) > 0


class TestOutputDerivation:
    def test_bounds(self) -> None:
        assert derive_max_output(8_000, None) >= 1024
        assert derive_max_output(200_000, None) <= 8192
        # small window -> proportionally smaller output cap
        assert derive_max_output(8_000, None) < derive_max_output(128_000, None)


class TestResolverCache:
    def test_caches_per_model(self) -> None:
        res = WindowResolver()
        first = res.resolve("deepseek/deepseek-chat", CONFIG_DEFAULT_WINDOW)
        second = res.resolve("deepseek/deepseek-chat", CONFIG_DEFAULT_WINDOW)
        assert first == second == 64_000
        assert len(res._cache) == 1


class TestAutoTuneRuntime:
    class _Gen:
        def __init__(self, max_tokens):
            self.max_tokens = max_tokens

    class _Snap:
        def __init__(self, window, out):
            self.context_window_tokens = window
            self.generation = self._G(out)

        class _G:
            def __init__(self, out):
                self.max_tokens = out

    def test_tunes_small_window_model(self) -> None:
        snap = self._Snap(CONFIG_DEFAULT_WINDOW, 8192)
        window, out = auto_tune_runtime("openai/gpt-4o-mini", snap)
        assert window == 128_000
        assert out <= 8192 and out >= 1024

    def test_respects_custom_output(self) -> None:
        snap = self._Snap(CONFIG_DEFAULT_WINDOW, 2048)
        _, out = auto_tune_runtime("openai/gpt-4o", snap)
        assert out == 2048  # admin-chosen output kept

    def test_never_raises_on_garbage(self) -> None:
        class Weird:
            context_window_tokens = None
            generation = None

        window, out = auto_tune_runtime("mystery/model", Weird())
        assert isinstance(window, int) and window > 0
        assert isinstance(out, int) and out > 0
