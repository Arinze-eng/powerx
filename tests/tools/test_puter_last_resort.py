"""Puter is the last resort, and a tool that is absent cannot be chosen.

Cloudinary is the primary image path. These tests pin the ordering down at the
only place that can enforce it: tool registration. While any other image
provider has credentials, the Puter tools do not appear, so the model has no
way to reach them; when nothing else is configured, they come back.
"""

from __future__ import annotations

from nanobot.agent.tools.puter_image_tools import (
    PuterEditImageTool,
    PuterGenerateImageTool,
)
from nanobot.config.schema import ProviderConfig
from nanobot.supabase_auth import SupabaseAuth


class Ctx:
    """The slice of ToolContext these tools read."""

    def __init__(self, providers: dict[str, ProviderConfig]) -> None:
        self.image_generation_provider_configs = providers


def _supabase_configured(monkeypatch) -> None:
    monkeypatch.setattr(SupabaseAuth, "configured", property(lambda self: True))


def test_puter_is_withheld_while_cloudinary_can_answer(monkeypatch) -> None:
    _supabase_configured(monkeypatch)
    ctx = Ctx({"cloudinary": ProviderConfig(api_key="cloudinary://k:s@cloud-a")})

    assert PuterGenerateImageTool.enabled(ctx) is False  # type: ignore[arg-type]
    assert PuterEditImageTool.enabled(ctx) is False  # type: ignore[arg-type]


def test_puter_is_withheld_while_any_other_provider_can_answer(monkeypatch) -> None:
    _supabase_configured(monkeypatch)
    ctx = Ctx({"openai": ProviderConfig(api_key="sk-x")})

    assert PuterGenerateImageTool.enabled(ctx) is False  # type: ignore[arg-type]


def test_puter_returns_when_nothing_else_is_configured(monkeypatch) -> None:
    _supabase_configured(monkeypatch)
    monkeypatch.delenv("CLOUDINARY_ACCOUNTS", raising=False)
    monkeypatch.delenv("CLOUDINARY_URL", raising=False)

    ctx = Ctx({"cloudinary": ProviderConfig(api_key=None), "openai": ProviderConfig()})

    assert PuterGenerateImageTool.enabled(ctx) is True  # type: ignore[arg-type]
    assert PuterEditImageTool.enabled(ctx) is True  # type: ignore[arg-type]


def test_puter_is_not_offered_at_all_without_a_supabase_deployment(monkeypatch) -> None:
    monkeypatch.setattr(SupabaseAuth, "configured", property(lambda self: False))

    ctx = Ctx({})

    assert PuterGenerateImageTool.enabled(ctx) is False  # type: ignore[arg-type]
    assert PuterEditImageTool.enabled(ctx) is False  # type: ignore[arg-type]


def test_the_descriptions_call_puter_the_last_resort() -> None:
    assert "last-resort" in PuterGenerateImageTool().description.lower()
    assert "last-resort" in PuterEditImageTool().description.lower()
    assert "generate_image" in PuterEditImageTool().description
