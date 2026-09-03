"""Puter image generation & editing tools for the WebUI/WebSocket channel.

These tools let a user generate or edit an image using **natural language** in
the WebUI chat — no slash-command required. The agent's LLM decides to call one
of these tools when the user asks to "make an image", "create a picture",
"edit this photo", etc. They mirror the Telegram bot's ``/image`` and
``/image edit`` commands: they charge one credit step and round-trip to the
same Supabase-hosted Puter generation/editing edges that Telegram uses.

Design notes
------------
- Enabled only for Supabase-backed WebUI turns (``channel`` in
  ``{"websocket", "webui"}``) where the authenticated user id is present in
  the turn metadata.
- The per-iteration Supabase credit hook already charges agent steps; these
  tools charge the **same additional per-generation step** that the Telegram
  image commands charge, so image usage is priced identically across channels.
- Generated/edited images are persisted under the media root as artifacts and
  returned to the LLM (via ``generated_image_tool_result``), which then
  delivers them to the chat with the ``message`` tool's ``media`` parameter —
  the exact same flow as the built-in ``generate_image`` tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.agent.tools.schema import (
    ArraySchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.config.paths import get_media_dir
from nanobot.supabase_auth import SupabaseAuth
from nanobot.utils.artifacts import (
    ArtifactError,
    generated_image_tool_result,
    store_generated_image_artifact,
)


def _supabase() -> SupabaseAuth:
    return SupabaseAuth()


def _webui_user_id() -> str | None:
    """Return the authenticated Supabase user id on a WebUI/WebSocket turn."""
    ctx = current_request_context()
    if ctx is None:
        return None
    if ctx.channel not in {"websocket", "webui"}:
        return None
    metadata = ctx.metadata or {}
    user_id = metadata.get("supabase_user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        return None
    return user_id.strip()


def _save_image(data_url: str, *, mime: str, prompt: str, kind: str) -> dict[str, Any]:
    """Persist a returned Puter image under the media root as an artifact."""
    if not data_url:
        raise ArtifactError("Puter returned an empty image payload")
    artifact = store_generated_image_artifact(
        data_url,
        prompt=prompt,
        model=kind,  # e.g. "generate_image" / "edit_image"
        source_images=None,
        save_dir="puter",
        provider="puter",
    )
    return artifact


@tool_parameters(
    tool_parameters_schema(
        prompt=StringSchema(
            "Natural-language description of the image to generate. Include subject, style, "
            "composition, colors, and any constraints.",
            min_length=1,
        ),
        model=StringSchema("Optional Puter image model id. Omit for the default model."),
        required=["prompt"],
    )
)
class PuterGenerateImageTool(Tool):
    """Generate an image through the Supabase/Puter image service (credit-gated)."""

    _plugin_discoverable = True
    _scopes = {"core"}

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        # Only expose on Supabase-backed deployments (Render gateway).
        return SupabaseAuth().configured

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls()

    @property
    def name(self) -> str:
        return "generate_puter_image"

    @property
    def description(self) -> str:
        return (
            "Generate a brand-new image using the AI image service. Use this when the user "
            "asks to create, make, draw, or generate an image or picture. "
            "It charges the user a small amount of credit. Returns artifact paths; deliver "
            "them to the user with the message tool's media parameter. "
            "If the account has no credit, tell the user to buy credits and stop."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        prompt: str,
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        user_id = _webui_user_id()
        if not user_id:
            return ToolResult.error(
                "Error: You must be signed in to generate images. Please sign in and try again."
            )
        prompt = (prompt or "").strip()
        if not prompt:
            return ToolResult.error("Error: Please describe the image you want to generate.")

        auth = _supabase()
        try:
            await auth.charge_step(
                {"agentx_user_id": user_id},
                f"webui:puter-generate:{user_id}",
                1,
            )
        except Exception as exc:
            logger.debug("puter image step charge failed: {}", exc)
            return ToolResult.error(
                "Error: Image generation is not available because your credit is exhausted or "
                "could not be charged. Please buy credits or try again later."
            )

        try:
            result = await auth.puter_generate(
                {"agentx_user_id": user_id},
                "generate_image",
                prompt,
                model=(model or "").strip(),
            )
            artifact = _save_image(
                str(result.get("data_uri") or ""),
                mime=str(result.get("mime") or ""),
                prompt=prompt,
                kind="generate_image",
            )
            return generated_image_tool_result([artifact])
        except Exception as exc:
            logger.warning("puter image generation failed: {}", exc)
            return ToolResult.error(
                f"Error: Image generation failed ({str(exc)[:300]}). Please try again."
            )


@tool_parameters(
    tool_parameters_schema(
        prompt=StringSchema(
            "Natural-language instruction describing what to change in the image(s).",
            min_length=1,
        ),
        image_paths=ArraySchema(
            StringSchema("Local path of an attached image or a previously generated image artifact."),
            description="The image(s) to edit. Use the path of the user's attached image or a prior generated artifact path.",
        ),
        model=StringSchema("Optional Puter image model id. Omit for the default model."),
        required=["prompt"],
    )
)
class PuterEditImageTool(Tool):
    """Edit one or more images through the Supabase/Puter image service (credit-gated)."""

    _plugin_discoverable = True
    _scopes = {"core"}

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return SupabaseAuth().configured

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls()

    @property
    def name(self) -> str:
        return "edit_puter_image"

    @property
    def description(self) -> str:
        return (
            "Edit an existing image (change, enhance, restyle, remove/object, etc.) using the "
            "AI image service. Use this when the user attaches an image and asks to edit, "
            "change, or retouch it. It charges a small amount of credit. Returns artifact paths; "
            "deliver them to the user with the message tool's media parameter. "
            "If the account has no credit, tell the user to buy credits and stop."
        )

    @property
    def exclusive(self) -> bool:
        return True

    def _resolve_paths(self, values: list[str] | None) -> list[str]:
        """Resolve image paths relative to the workspace / media root."""
        if not values:
            return []
        media_root = get_media_dir().resolve()
        resolved: list[str] = []
        for value in values[:3]:
            value = (value or "").strip()
            if not value:
                continue
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                try:
                    candidate = (media_root / candidate).resolve()
                except OSError:
                    continue
            if candidate.is_file():
                resolved.append(str(candidate))
        return resolved

    async def execute(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        prompt: str,
        image_paths: list[str] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        user_id = _webui_user_id()
        if not user_id:
            return ToolResult.error(
                "Error: You must be signed in to edit images. Please sign in and try again."
            )
        prompt = (prompt or "").strip()
        if not prompt:
            return ToolResult.error("Error: Please describe how to edit the image.")

        paths = self._resolve_paths(image_paths)
        if not paths:
            return ToolResult.error(
                "Error: No editable image found. Please attach an image and ask to edit it."
            )

        try:
            input_images = [image_path_to_puter_data_uri(p) for p in paths]
        except Exception as exc:
            return ToolResult.error(
                f"Error: Could not read an attached image ({str(exc)[:200]})."
            )

        auth = _supabase()
        try:
            await auth.charge_step(
                {"agentx_user_id": user_id},
                f"webui:puter-edit:{user_id}",
                1,
            )
        except Exception as exc:
            logger.debug("puter edit step charge failed: {}", exc)
            return ToolResult.error(
                "Error: Image editing is not available because your credit is exhausted or "
                "could not be charged. Please buy credits or try again later."
            )

        try:
            result = await auth.puter_edit_image(
                {"agentx_user_id": user_id},
                prompt,
                input_images,
                model=(model or "").strip(),
            )
            artifact = _save_image(
                str(result.get("data_uri") or ""),
                mime=str(result.get("mime") or ""),
                prompt=prompt,
                kind="edit_image",
            )
            return generated_image_tool_result([artifact])
        except Exception as exc:
            logger.warning("puter image edit failed: {}", exc)
            return ToolResult.error(
                f"Error: Image editing failed ({str(exc)[:300]}). Please try again."
            )


def image_path_to_puter_data_uri(path: str) -> str:
    """Convert a local image file into a ``data:image/...;base64,...`` URI.

    Bounded to 12 MB to match Telegram's Puter image-edit limit and to keep
    the payload inside the WebSocket/media size limits.
    """
    import base64

    from nanobot.utils.helpers import detect_image_mime

    image_path = Path(path).expanduser().resolve()
    if not image_path.is_file():
        raise ArtifactError("The image is no longer available")
    raw = image_path.read_bytes()
    if not raw or len(raw) > 12 * 1024 * 1024:
        raise ArtifactError("Image edits accept images up to 12 MB")
    mime = detect_image_mime(raw)
    if not mime or not mime.startswith("image/"):
        raise ArtifactError("Please attach a supported image (PNG, JPEG, GIF, or WebP)")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


__all__ = [
    "PuterGenerateImageTool",
    "PuterEditImageTool",
]