"""Immutable execution settings for one LLM turn."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

from nanobot.providers.base import GenerationSettings, LLMProvider

if TYPE_CHECKING:
    from nanobot.providers.factory import ProviderSnapshot


@dataclass(frozen=True, slots=True)
class LLMRuntime:
    """One captured provider/model configuration used for an entire execution.

    The provider itself is stateful, but all mutable selection and generation
    values are copied into this frozen value.  Consumers must use these fields
    instead of consulting ``provider.generation`` after admission.
    """

    provider: LLMProvider
    model: str
    generation: GenerationSettings
    context_window_tokens: int
    model_preset: str | None = None
    snapshot_signature: tuple[object, ...] | None = None

    @classmethod
    def capture(
        cls,
        provider: LLMProvider,
        model: str,
        *,
        context_window_tokens: int,
        model_preset: str | None = None,
        snapshot_signature: tuple[object, ...] | None = None,
    ) -> LLMRuntime:
        """Capture provider defaults without retaining mutable generation state."""
        defaults = GenerationSettings()
        generation = getattr(provider, "generation", defaults)
        configured_max_tokens = getattr(generation, "max_tokens", defaults.max_tokens)
        # Auto-derive the TRUE context window for admin-loaded models instead of
        # trusting the 200k config default. This is what makes history compaction
        # actually fire on small-window models, stopping long chats from silently
        # re-sending (and paying for) their whole history every turn. Honours any
        # deliberate admin override; only fills in when the value is untouched.
        from nanobot.utils.model_windows import auto_tune_runtime

        snapshot_for_tuning = SimpleNamespace(
            context_window_tokens=context_window_tokens,
            generation=SimpleNamespace(max_tokens=configured_max_tokens),
        )
        tuned_window, tuned_max_output = auto_tune_runtime(model, snapshot_for_tuning)
        return cls(
            provider=provider,
            model=model,
            generation=GenerationSettings(
                temperature=getattr(generation, "temperature", defaults.temperature),
                max_tokens=tuned_max_output,
                reasoning_effort=getattr(
                    generation,
                    "reasoning_effort",
                    defaults.reasoning_effort,
                ),
            ),
            context_window_tokens=tuned_window,
            model_preset=model_preset,
            snapshot_signature=snapshot_signature,
        )

    def with_generation_overrides(
        self,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> LLMRuntime:
        """Return a derived runtime for explicit per-run generation overrides."""
        generation = self.generation
        return replace(
            self,
            generation=GenerationSettings(
                temperature=(
                    generation.temperature if temperature is None else temperature
                ),
                max_tokens=generation.max_tokens if max_tokens is None else max_tokens,
                reasoning_effort=(
                    generation.reasoning_effort
                    if reasoning_effort is None
                    else reasoning_effort
                ),
            ),
        )


def runtime_from_provider_snapshot(
    snapshot: ProviderSnapshot,
) -> LLMRuntime:
    """Convert a provider factory snapshot into the canonical runtime value."""
    if snapshot.generation is not None:
        return LLMRuntime(
            provider=snapshot.provider,
            model=snapshot.model,
            generation=snapshot.generation,
            context_window_tokens=snapshot.context_window_tokens,
            model_preset=snapshot.model_preset,
            snapshot_signature=snapshot.signature,
        )
    return LLMRuntime.capture(
        snapshot.provider,
        snapshot.model,
        context_window_tokens=snapshot.context_window_tokens,
        model_preset=snapshot.model_preset,
        snapshot_signature=snapshot.signature,
    )
