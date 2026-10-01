"""Image generation tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from loguru import logger
from pydantic import Field

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.schema import (
    ArraySchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.bus.events import (
    INBOUND_META_RUNTIME_CONTROL,
    RUNTIME_CONTROL_ACK,
    RUNTIME_CONTROL_IMAGE_GENERATION_RELOAD,
    InboundMessage,
)
from nanobot.bus.queue import MessageBus
from nanobot.config.paths import get_media_dir
from nanobot.config_base import Base
from nanobot.providers.image_generation import (
    ImageGenerationError,
    ImageGenerationProvider,
    get_image_gen_provider,
    image_gen_provider_configs,
    image_provider_chain,
)
from nanobot.security.workspace_access import current_tool_workspace
from nanobot.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path
from nanobot.utils.artifacts import (
    ArtifactError,
    generated_image_tool_result,
    store_generated_image_artifact,
)
from nanobot.utils.helpers import detect_image_mime
from nanobot.utils.remote_media import (
    RemoteMediaError,
    is_remote_reference,
    materialize_reference,
)

if TYPE_CHECKING:
    from nanobot.agent.tools.context import ToolContext
    from nanobot.config.schema import ProviderConfig


class ImageGenerationToolConfig(Base):
    """Image generation tool configuration."""
    enabled: bool = False
    #: Cloudinary leads by default: it is the one provider here that holds
    #: several credentials at once, so it is the one that keeps answering after
    #: a single key's monthly allowance runs out. An operator who pins another
    #: provider still gets it first - see ``ImageGenerationTool._provider_chain``.
    provider: str = "cloudinary"
    model: str = "openai/gpt-5.4-image-2"
    default_aspect_ratio: str = "1:1"
    default_image_size: str = "1K"
    max_images_per_turn: int = Field(default=4, ge=1, le=8)
    save_dir: str = "generated"


@tool_parameters(
    tool_parameters_schema(
        prompt=StringSchema(
            "Detailed image generation or edit prompt. Include style, subject, composition, colors, and constraints.",
            min_length=1,
        ),
        reference_images=ArraySchema(
            StringSchema(
                "Local path of an existing image artifact or user-provided image to use as an edit "
                "reference. An https:// link to a user-uploaded file works too — it is downloaded "
                "automatically, so never tell the user to re-upload a file that is already attached."
            ),
            description="Optional image references: local paths, or https:// links to uploaded files.",
        ),
        aspect_ratio=StringSchema(
            "Optional output aspect ratio, e.g. 1:1, 16:9, 9:16, 4:3.",
        ),
        image_size=StringSchema(
            "Optional output size hint supported by the configured provider, e.g. 1K, 2K, 4K, or 1024x1024.",
        ),
        count=IntegerSchema(
            description="Number of images to generate in this turn.",
            minimum=1,
            maximum=8,
        ),
        required=["prompt"],
    )
)
class ImageGenerationTool(Tool):
    """Generate persistent image artifacts through the configured image provider."""

    config_key = "image_generation"

    @classmethod
    def config_cls(cls):
        return ImageGenerationToolConfig

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return ctx.config.image_generation.enabled

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(
            workspace=ctx.workspace,
            config=ctx.config.image_generation,
            provider_configs=ctx.image_generation_provider_configs,
        )

    def __init__(
        self,
        *,
        workspace: str | Path,
        config: ImageGenerationToolConfig,
        provider_config: ProviderConfig | None = None,
        provider_configs: dict[str, ProviderConfig] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser()
        self.config = config
        self.provider_configs = dict(provider_configs or {})
        if provider_config is not None and "openrouter" not in self.provider_configs:
            self.provider_configs["openrouter"] = provider_config

    @property
    def name(self) -> str:
        return "generate_image"

    @property
    def description(self) -> str:
        return (
            "Generate or edit images and store them as persistent artifacts. "
            "Returns artifact ids and local paths. For edits, pass prior generated image paths "
            "or user image paths as reference_images."
        )

    def _provider_config(self) -> ProviderConfig | None:
        return self.provider_configs.get(self.config.provider)

    def _provider_client(self) -> ImageGenerationProvider | None:
        """The client for the configured provider alone.

        The request itself walks the fallback chain; this is the single-provider
        view, kept for callers and tests that want to inspect one client.
        """
        return self._client_for(self.config.provider)

    def _missing_key_message(self) -> str:
        """What to say when no provider is usable: name the one that was asked for."""
        cls = get_image_gen_provider(self.config.provider)
        message = getattr(cls, "missing_key_message", "") if cls is not None else ""
        return str(message or "").strip()

    def _provider_chain(self) -> list[str]:
        """The providers to try for this request, best first.

        The configured provider leads when it can serve a request, Cloudinary
        follows whenever it is configured, and the rest of the default order
        comes after. Providers with no credentials are dropped, so a stale
        setting from an earlier configuration cannot stop the agent from using
        the keys that are actually present.
        """
        return image_provider_chain(self.config.provider, self.provider_configs)

    def _client_for(self, name: str) -> ImageGenerationProvider | None:
        provider = self.provider_configs.get(name)
        cls = get_image_gen_provider(name)
        if cls is None:
            return None
        kwargs: dict[str, Any] = {
            "api_key": provider.api_key if provider and isinstance(provider.api_key, str) else None,
            "api_base": provider.api_base if provider and isinstance(provider.api_base, str) else None,
            "extra_headers": provider.extra_headers
            if provider and isinstance(provider.extra_headers, dict) else None,
            "extra_body": provider.extra_body
            if provider and isinstance(provider.extra_body, dict) else None,
            "proxy": provider.proxy if provider and isinstance(provider.proxy, str) else None,
        }
        return cls(**kwargs)

    def _resolve_reference_image(self, value: str) -> str:
        access = current_tool_workspace(self.workspace, restrict_to_workspace=True)
        workspace = access.project_path or self.workspace
        try:
            resolved = resolve_allowed_path(
                value,
                workspace=workspace,
                allowed_root=access.allowed_root,
                extra_allowed_roots=[get_media_dir()] if access.allowed_root is not None else None,
                strict=True,
            )
        except WorkspaceBoundaryError as exc:
            raise ImageGenerationError(
                "reference_images must be inside the workspace or nanobot media directory"
            ) from exc
        except OSError as exc:
            raise ImageGenerationError(f"reference image not found: {value}") from exc
        if not resolved.is_file():
            raise ImageGenerationError(f"reference image is not a file: {value}")
        raw = resolved.read_bytes()
        if detect_image_mime(raw) is None:
            raise ImageGenerationError(f"unsupported reference image: {value}")
        return str(resolved)

    async def _resolve_reference_images(self, values: list[str] | None) -> list[str]:
        """Resolve every reference to a readable local image path.

        A browser file attachment reaches the agent as an onlyfiles.com URL, not
        as a file on this host, so a URL reference is fetched down into the media
        directory first — otherwise the user is told to upload a file they just
        uploaded. See :mod:`nanobot.utils.remote_media`.
        """
        if not values:
            return []
        resolved: list[str] = []
        for value in values:
            if not value:
                continue
            if is_remote_reference(value):
                # Validate the downloaded copy exactly like a local reference,
                # so a link that served a viewer page is refused here rather
                # than handed to a provider as an "image".
                local = await self._materialize_reference(value)
                resolved.append(self._resolve_reference_image(str(local)))
                continue
            resolved.append(self._resolve_reference_image(value))
        return resolved

    async def _materialize_reference(self, url: str) -> Path:
        """Fetch a remote reference onto disk, or fail with a usable message."""
        try:
            return await materialize_reference(url)
        except RemoteMediaError as exc:
            raise ImageGenerationError(
                f"could not fetch reference image {url}: {exc}"
            ) from exc
        except OSError as exc:
            raise ImageGenerationError(
                f"could not store reference image {url}: {exc}"
            ) from exc

    async def execute(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        prompt: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        count: int | None = None,
        **kwargs: Any,
    ) -> str:
        requested = count or 1
        if requested > self.config.max_images_per_turn:
            return ToolResult.error(
                "Error: count exceeds tools.imageGeneration.maxImagesPerTurn "
                f"({self.config.max_images_per_turn})"
            )

        try:
            refs = await self._resolve_reference_images(reference_images)
        except (ArtifactError, ImageGenerationError, OSError) as exc:
            return ToolResult.error(f"Error: {exc}")

        chain = self._provider_chain()
        if not chain:
            # Name the provider the operator asked for and what it is missing,
            # rather than a generic complaint that hides the fix.
            message = self._missing_key_message()
            if message:
                return ToolResult.error(f"Error: {message}")
            return ToolResult.error(
                "Error: no image provider is configured. Add Cloudinary credentials "
                "(CLOUDINARY_ACCOUNTS, or the Cloudinary provider in settings) or another "
                f"image provider's API key. Asked for {self.config.provider!r}."
            )

        # Walk the chain rather than betting on one provider: a key that has run
        # out of monthly allowance, a rate limit or a revoked key is a reason to
        # ask the next provider, not a reason to fail the turn. The error that
        # comes back names every provider that was tried, so a real problem is
        # reported as itself instead of as "no provider worked".
        attempted: list[str] = []
        for name in chain:
            client = self._client_for(name)
            if client is None:
                continue
            attempted.append(name)
            artifacts: list[dict[str, Any]] = []
            try:
                while len(artifacts) < requested:
                    response = await client.generate(
                        prompt=prompt,
                        model=self.config.model,
                        reference_images=refs,
                        aspect_ratio=aspect_ratio or self.config.default_aspect_ratio,
                        image_size=image_size or self.config.default_image_size,
                    )
                    for image_data_url in response.images:
                        artifact = store_generated_image_artifact(
                            image_data_url,
                            prompt=prompt,
                            model=self.config.model,
                            source_images=refs,
                            save_dir=self.config.save_dir,
                            provider=name,
                        )
                        artifacts.append(artifact)
                        if len(artifacts) >= requested:
                            break
            except (ArtifactError, ImageGenerationError, OSError) as exc:
                logger.warning("image generation via {} failed: {}", name, exc)
                if artifacts:
                    # Something was produced before the failure: what the user
                    # can already see beats a clean error.
                    break
                continue
            except Exception as exc:  # noqa: BLE001 - one provider must not end the turn
                logger.warning("image generation via {} raised: {}", name, exc)
                if artifacts:
                    break
                continue
            if artifacts:
                return generated_image_tool_result(artifacts)
            logger.warning("image generation via {} returned no image", name)

        return ToolResult.error(
            "Error: image generation failed. Tried: "
            + (", ".join(attempted) or "nothing (no provider is configured)")
            + "."
        )


async def reload_image_generation_tool(state: Any, registry: ToolRegistry) -> dict[str, Any]:
    """Apply the persisted image configuration to the running agent."""
    try:
        from nanobot.config.loader import load_config, resolve_config_env_vars

        config = resolve_config_env_vars(load_config())
        tool_config = config.tools.image_generation
        provider_configs = image_gen_provider_configs(config)
    except Exception as exc:
        logger.warning("Image generation hot reload could not read config: {}", exc)
        return {
            "ok": False,
            "message": "Could not reload image generation config.",
            "requires_restart": True,
            "error": str(exc),
        }

    next_tool = (
        ImageGenerationTool(  # pyright: ignore[reportAbstractUsage]
            workspace=state.workspace,
            config=tool_config,
            provider_configs=provider_configs,
        )
        if tool_config.enabled
        else None
    )

    state.tools_config.image_generation = tool_config
    state._image_generation_provider_configs = provider_configs
    if next_tool is not None:
        registry.register(next_tool)
    else:
        registry.unregister("generate_image")

    logger.info(
        "Image generation config reloaded: enabled={} provider={} model={}",
        tool_config.enabled,
        tool_config.provider,
        tool_config.model,
    )
    return {
        "ok": True,
        "message": "Image generation settings applied without restarting nanobot.",
        "enabled": tool_config.enabled,
        "provider": tool_config.provider,
        "model": tool_config.model,
        # The providers that will actually be tried, in order, so an operator
        # can see whether the keys they added are part of the run.
        "providers": image_provider_chain(tool_config.provider, provider_configs),
        "requires_restart": False,
    }


async def request_image_generation_reload(
    bus: MessageBus,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Ask the running agent loop to refresh its image generation tool."""
    loop = asyncio.get_running_loop()
    ack: asyncio.Future[dict[str, Any]] = loop.create_future()
    await bus.publish_inbound(
        InboundMessage(
            channel="system",
            sender_id="webui-settings",
            chat_id="runtime",
            content=RUNTIME_CONTROL_IMAGE_GENERATION_RELOAD,
            metadata={
                INBOUND_META_RUNTIME_CONTROL: RUNTIME_CONTROL_IMAGE_GENERATION_RELOAD,
                RUNTIME_CONTROL_ACK: ack,
            },
        )
    )
    try:
        result = await asyncio.wait_for(ack, timeout=timeout)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "message": "Image generation hot reload timed out.",
            "requires_restart": True,
        }
    if not isinstance(cast(object, result), dict):
        return {
            "ok": False,
            "message": "Image generation hot reload returned an unexpected response.",
            "requires_restart": True,
        }
    return result


async def handle_runtime_control(
    state: Any,
    msg: InboundMessage,
    registry: ToolRegistry,
) -> bool:
    """Handle an in-process image generation reload request."""
    metadata = msg.metadata
    if metadata.get(INBOUND_META_RUNTIME_CONTROL) != RUNTIME_CONTROL_IMAGE_GENERATION_RELOAD:
        return False

    ack = metadata.get(RUNTIME_CONTROL_ACK)
    try:
        result = await reload_image_generation_tool(state, registry)
    except Exception as exc:
        logger.exception("Image generation hot reload failed")
        result = {
            "ok": False,
            "message": "Image generation hot reload failed.",
            "requires_restart": True,
            "error": str(exc),
        }
    if isinstance(ack, asyncio.Future) and not ack.done():
        cast(asyncio.Future[Any], ack).set_result(result)
    return True
