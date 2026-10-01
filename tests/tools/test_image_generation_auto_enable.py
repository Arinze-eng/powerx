"""``generate_image`` registers itself whenever an image provider can answer.

The regression these pin: ``ImageGenerationToolConfig.enabled`` defaulted to
``False`` and nothing on a Cloudinary-only deployment ever set it, so the tool
was never registered — the model had no Cloudinary tool for an image edit at
all and every edit fell through to Pillow/OpenCV inside a sandbox. The provider
layer was never at fault: Cloudinary leads the default order and is the only
image provider with credentials there.

So "unset" must not mean "off". It means "ask the providers", and an explicit
``true``/``false`` from an operator still wins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.image_generation import (
    ImageGenerationTool,
    ImageGenerationToolConfig,
)
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.loader import load_config, save_config
from nanobot.config.schema import Config, ProviderConfig
from nanobot.providers.image_generation import image_gen_provider_configs

CLOUDINARY_URL = "cloudinary://781896334133766:secret@duabcpp9o"


@pytest.fixture
def cloudinary_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDINARY_ACCOUNTS", CLOUDINARY_URL)


@pytest.fixture
def no_image_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """No image provider has credentials, and none is pinned in the config."""
    for name in (
        "CLOUDINARY_ACCOUNTS",
        "CLOUDINARY_URL",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "AIHUBMIX_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _providers(config: Config | None = None) -> dict[str, ProviderConfig]:
    return image_gen_provider_configs(config or Config())


# --------------------------------------------------------------------------
# The resolution rule
# --------------------------------------------------------------------------


def test_absent_setting_resolves_true_when_cloudinary_is_configured(
    cloudinary_env: None,
) -> None:
    cfg = ImageGenerationToolConfig()
    assert cfg.enabled is None
    assert cfg.resolves_enabled(_providers()) is True


def test_absent_setting_resolves_false_with_no_provider(no_image_keys: None) -> None:
    cfg = ImageGenerationToolConfig()
    assert cfg.resolves_enabled(_providers()) is False


def test_explicit_false_wins_over_a_configured_provider(cloudinary_env: None) -> None:
    cfg = ImageGenerationToolConfig(enabled=False)
    assert cfg.resolves_enabled(_providers()) is False


def test_explicit_true_wins_with_no_provider(no_image_keys: None) -> None:
    """An operator who insists gets the tool; it is their call to make."""
    cfg = ImageGenerationToolConfig(enabled=True)
    assert cfg.resolves_enabled(_providers()) is True


def test_null_from_a_saved_config_still_means_auto(cloudinary_env: None) -> None:
    """``save_config`` dumps every field, so a round-trip writes ``enabled: null``.

    If ``null`` were read as "off" the auto-enable would survive exactly one
    settings save and then switch itself off — the same silent-disappearance
    bug in a new costume.
    """
    cfg = ImageGenerationToolConfig.model_validate({"enabled": None})
    assert cfg.resolves_enabled(_providers()) is True


# --------------------------------------------------------------------------
# The loader's answer
# --------------------------------------------------------------------------


def test_enabled_is_true_when_the_config_omits_the_section(cloudinary_env: None) -> None:
    ctx = ToolContext(config=Config().tools, workspace="/tmp")
    assert ImageGenerationTool.enabled(ctx) is True


def test_enabled_is_false_when_the_config_says_false(cloudinary_env: None) -> None:
    tools = Config().tools.model_copy(update={})
    tools.image_generation = ImageGenerationToolConfig(enabled=False)
    ctx = ToolContext(config=tools, workspace="/tmp")
    assert ImageGenerationTool.enabled(ctx) is False


def test_enabled_is_false_with_no_provider_configured(no_image_keys: None) -> None:
    ctx = ToolContext(config=Config().tools, workspace="/tmp")
    assert ImageGenerationTool.enabled(ctx) is False


def test_credentials_in_the_context_alone_are_enough(no_image_keys: None) -> None:
    """Without CLOUDINARY_ACCOUNTS in the environment, the key on the provider
    config is what makes Cloudinary usable — and that is the dict the context
    hands the tool, so an unset ``enabled`` has to consult it."""
    tools = Config().tools
    providers = dict(_providers())
    providers["cloudinary"] = ProviderConfig(api_key=CLOUDINARY_URL)

    assert ImageGenerationTool.enabled(ToolContext(config=tools, workspace="/tmp")) is False
    assert (
        ImageGenerationTool.enabled(
            ToolContext(config=tools, workspace="/tmp", image_generation_provider_configs=providers)
        )
        is True
    )


def test_loader_registers_generate_image_from_an_omitting_config(
    cloudinary_env: None,
    tmp_path: Path,
) -> None:
    """The end-to-end claim: a config with no ``tools.image_generation`` yields
    a registered ``generate_image``."""
    ctx = ToolContext(
        config=Config().tools,
        workspace=str(tmp_path),
        image_generation_provider_configs=_providers(),
    )
    registry = ToolRegistry()
    registered = ToolLoader(test_classes=[ImageGenerationTool]).load(ctx, registry)
    assert "generate_image" in registered
    assert registry.get("generate_image") is not None


def test_loader_withholds_generate_image_with_no_provider(
    no_image_keys: None,
    tmp_path: Path,
) -> None:
    ctx = ToolContext(
        config=Config().tools,
        workspace=str(tmp_path),
        image_generation_provider_configs={},
    )
    registry = ToolRegistry()
    registered = ToolLoader(test_classes=[ImageGenerationTool]).load(ctx, registry)
    assert "generate_image" not in registered


def test_generate_image_is_discoverable() -> None:
    names = {cls.__name__ for cls in ToolLoader().discover()}
    assert "ImageGenerationTool" in names


# --------------------------------------------------------------------------
# Survives a real save/reload, the way the webui settings route does it
# --------------------------------------------------------------------------


def test_auto_survives_a_settings_save_and_reload(
    cloudinary_env: None,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    assert "image_generation" not in config.tools.model_fields_set
    save_config(config, config_path)

    reloaded = load_config(config_path)
    assert reloaded.tools.image_generation.resolves_enabled(_providers()) is True


def test_an_explicit_false_survives_a_settings_save_and_reload(
    cloudinary_env: None,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.tools.image_generation.enabled = False
    save_config(config, config_path)

    reloaded = load_config(config_path)
    assert reloaded.tools.image_generation.enabled is False
    assert reloaded.tools.image_generation.resolves_enabled(_providers()) is False


# --------------------------------------------------------------------------
# The tool's own description: an image edit is its job, not the sandbox's
# --------------------------------------------------------------------------


def test_description_claims_text_and_transformation_edits(tmp_path: Path) -> None:
    tool = ImageGenerationTool(
        workspace=tmp_path,
        config=ImageGenerationToolConfig(),
        provider_configs=_providers(),
    )
    description = tool.description
    assert "Changing text that is already in a picture" in description
    assert "reference_images" in description
    assert "Never answer such a request with a refusal" in description
