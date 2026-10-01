"""Cloudinary video editing: upload once, then edit by asking for a new URL.

Cloudinary edits video on **delivery**, so an edit is one transform, not a
re-encode on our host and not a re-upload of the source:

* ``trim`` keeps a span of the clip (``so_``/``eo_``),
* ``crop``/``resize`` re-frames it,
* ``transcode`` changes container, quality or frame rate,
* ``poster`` pulls a still out of the clip at a timestamp,
* ``concat`` lays a second clip onto the end of the first.

The generative side is separate and is deliberately not promised here:
Cloudinary's ``image_to_video`` endpoint answers ``MG_00014 not found`` on
product environments without the video add-on (all three Free-plan accounts
tested did), so this tool reports that plainly instead of pretending the clip
was animated. What does work everywhere is the transformation engine above, and
that is what the tool leans on.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.schema import (
    IntegerSchema,
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.config.paths import get_media_dir
from nanobot.utils.artifacts import ArtifactError
from nanobot.utils.remote_media import (
    RemoteMediaError,
    is_remote_reference,
    materialize_reference,
)

#: A delivery render can take a while for a clip that was never rendered before.
_RENDER_TIMEOUT_S = 180.0
_MAX_MEDIA_BYTES = 200 * 1024 * 1024

_ACTIONS = ("trim", "crop", "transcode", "poster", "concat", "describe", "animate")

_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
_EXT_BY_FORMAT = {
    "mp4": "mp4",
    "webm": "webm",
    "mkv": "mkv",
    "mov": "mov",
    "gif": "gif",
    "jpg": "jpg",
    "jpeg": "jpg",
    "png": "png",
}


def _resolve_media_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.is_file():
        raise ArtifactError(f"file not found: {value}")
    return path


async def _resolve_media(value: str) -> Path:
    """A local file for a source that may be a URL instead of a path.

    A user-uploaded file attachment arrives as an onlyfiles.com URL, not as a
    file on this host (see :mod:`nanobot.utils.remote_media`), so a link is
    fetched down before the upload path runs. Without this, editing a video the
    user just attached fails with ``file not found: https://onlyfiles.com/...``.
    """
    if is_remote_reference(value):
        try:
            return await materialize_reference(value)
        except RemoteMediaError as exc:
            raise ArtifactError(f"could not fetch media {value}: {exc}") from exc
    return _resolve_media_path(value)


def _resource_type(path: Path) -> str:
    return "video" if path.suffix.lower() in _VIDEO_SUFFIXES else "image"


def _store(path: Path, data: bytes, *, prompt: str, model: str) -> dict[str, Any]:
    """Persist a rendered edit under the media root, as an artifact.

    Kept local to this tool on purpose: the image artifact store only accepts
    image data URLs, and a video edit is neither an image nor a data URL.
    """
    now = datetime.now().astimezone()
    artifact_id = f"vid_{uuid.uuid4().hex[:12]}"
    day_dir = get_media_dir() / "generated" / "cloudinary-video" / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix or ".mp4"
    media_path = day_dir / f"{artifact_id}{suffix}"
    media_path.write_bytes(data)
    metadata: dict[str, Any] = {
        "id": artifact_id,
        "path": str(media_path),
        "mime": "video/mp4" if suffix == ".mp4" else f"application/octet-stream",
        "bytes": len(data),
        "prompt": prompt,
        "model": model,
        "provider": "cloudinary",
        "created_at": now.isoformat(),
    }
    (day_dir / f"{artifact_id}.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metadata


def _media_tool_result(artifacts: list[dict[str, Any]]) -> str:
    """The compact structured result exposed to the LLM, matching images."""
    return json.dumps(
        {
            "artifacts": artifacts,
            "next_step": (
                "Call the message tool with these artifact paths in the media parameter to "
                "deliver the video to the user. Keep raw paths internal unless the user asks "
                "for debug details."
            ),
        },
        ensure_ascii=False,
    )


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            description="The edit to perform: trim, crop, transcode, poster, concat, describe or animate.",
            min_length=1,
        ),
        source=StringSchema(
            description=(
                "Local path of the video (or still image for animate) to edit. An https:// "
                "link to a file the user uploaded works too — it is downloaded first."
            )
        ),
        start=NumberSchema(
            description=(
                "Start offset in seconds. For trim it is where the kept span begins; for "
                "poster it is the timestamp of the frame to grab."
            ),
        ),
        end=NumberSchema(description="End offset in seconds. For trim it is where the kept span ends."),
        width=IntegerSchema(description="Output width in pixels."),
        height=IntegerSchema(description="Output height in pixels."),
        crop=StringSchema(description="Crop mode, e.g. scale, fill, fit, limit, pad."),
        format=StringSchema(description="Output container or image format, e.g. mp4, webm, gif, jpg."),
        fps=IntegerSchema(description="Output frame rate."),
        quality=StringSchema(description="Output quality, e.g. auto, good, best, 70."),
        second_clip=StringSchema(
            description=(
                "Path or https:// link of the clip to append, for the concat action."
            )
        ),
        prompt=StringSchema(description="Motion description, for the animate action."),
        public_id=StringSchema(description="Optional Cloudinary public id to store the uploaded asset under."),
        required=["action"],
    )
)
class CloudinaryVideoEditTool(Tool):
    """Edit video with Cloudinary's delivery transformations."""

    _plugin_discoverable = True
    _scopes = {"core"}

    @classmethod
    def _api_key(cls, ctx: ToolContext) -> str | None:
        providers = getattr(ctx, "image_generation_provider_configs", None) or {}
        provider = providers.get("cloudinary")
        key = getattr(provider, "api_key", None)
        return key if isinstance(key, str) and key.strip() else None

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        from nanobot.providers.cloudinary import configured

        return configured(cls._api_key(ctx))

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(api_key=cls._api_key(ctx))

    def __init__(self, *, api_key: str | None = None) -> None:
        self.api_key = api_key

    @property
    def name(self) -> str:
        return "cloudinary_video_edit"

    @property
    def description(self) -> str:
        return (
            "Edit a video by storing it in Cloudinary and rendering a transformation of it. "
            "This is the FIRST tool for a video edit — call it before media_sandbox/ffmpeg "
            "for trims, crops, resizes, format changes, joins and poster frames, including "
            "when the clip is already inside the sandbox (publish it with the sandbox tool's "
            "download_url and pass the resulting link as source). "
            "Actions: trim (keep a start/end span), crop (re-frame or resize), transcode "
            "(change format, fps or quality), poster (pull a still frame), concat (append a "
            "second clip), describe (report the stored asset), animate (generative "
            "image-to-video, only where the account has that add-on). Returns artifact paths; "
            "deliver them with the message tool's media parameter."
        )

    def _client(self) -> Any:
        from nanobot.providers.cloudinary import POOL, CloudinaryClient

        return CloudinaryClient(pool=POOL, config_api_key=self.api_key, timeout=_RENDER_TIMEOUT_S)

    async def _render(self, client: Any, url: str, dest: Path, *, prompt: str, model: str) -> dict[str, Any]:
        data = await asyncio.wait_for(
            client.download(url, max_bytes=_MAX_MEDIA_BYTES), timeout=_RENDER_TIMEOUT_S
        )
        if not data:
            raise ArtifactError("Cloudinary returned an empty render")
        return _store(dest, data, prompt=prompt, model=model)

    async def execute(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        action: str,
        source: str | None = None,
        *,
        start: float | None = None,
        end: float | None = None,
        width: int | None = None,
        height: int | None = None,
        crop: str | None = None,
        format: str | None = None,  # noqa: A002 - the field name the model sees
        fps: int | None = None,
        quality: str | None = None,
        second_clip: str | None = None,
        prompt: str | None = None,
        public_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        from nanobot.providers.cloudinary import (
            CloudinaryError,
            splice_transformation,
            transformation_segment,
            with_transformation,
        )

        action = (action or "").strip().lower()
        if action not in _ACTIONS:
            return ToolResult.error(
                f"Error: unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}."
            )
        if not source:
            return ToolResult.error(f"Error: the '{action}' action needs a 'source' media file.")

        client = self._client()
        try:
            if action == "animate":
                return await self._animate(client, prompt=prompt, source=source, public_id=public_id)

            path = await _resolve_media(str(source))
            asset = await client.upload(
                path.read_bytes(),
                resource_type=_resource_type(path),
                public_id=public_id or None,
            )
            if action == "describe":
                return _media_tool_result(
                    [
                        {
                            "id": asset.asset_id or asset.public_id,
                            "remote": asset.secure_url,
                            "resource_type": asset.resource_type,
                            "width": asset.width,
                            "height": asset.height,
                            "bytes": asset.bytes,
                            "source": str(path),
                        }
                    ]
                )

            target_ext = _EXT_BY_FORMAT.get((format or "").strip().lower()) or (
                "jpg" if action == "poster" else (path.suffix.lstrip(".").lower() or "mp4")
            )

            if action == "poster":
                transformation = transformation_segment(
                    start=start if start is not None else 1.0,
                    width=width,
                    height=height,
                    crop=crop or "scale",
                    quality=quality,
                )
            elif action == "concat":
                if not second_clip:
                    return ToolResult.error("Error: concat needs 'second_clip'.")
                other = await _resolve_media(str(second_clip))
                # Same cloud as the base: a splice combines two assets, and the
                # delivery URL cannot reach across product environments.
                other_asset = await client.upload(
                    other.read_bytes(), resource_type="video", cloud_name=asset.cloud_name
                )
                transformation = splice_transformation(
                    public_id=other_asset.public_id,
                    resource_type="video",
                    start=start,
                    end=end,
                    ext=other_asset.format or other.suffix.lstrip(".") or "mp4",
                )
            else:
                transformation = transformation_segment(
                    start=start,
                    end=end,
                    width=width,
                    height=height,
                    crop=crop,
                    quality=quality,
                    format=format,
                    fps=fps,
                )
                if not transformation:
                    return ToolResult.error(
                        "Error: nothing to change - give at least one of start, end, width, "
                        "height, crop, format, fps or quality."
                    )

            url = with_transformation(asset.secure_url, transformation, ext=target_ext)
            dest = Path(f"{path.stem}-{action}.{target_ext}")
            artifact = await self._render(
                client,
                url,
                dest,
                prompt=f"cloudinary {action} on {path.name}",
                model=transformation,
            )
            return _media_tool_result([artifact])
        except asyncio.TimeoutError:
            return ToolResult.error(
                "Error: Cloudinary did not finish rendering the edit in time. Try a shorter "
                "span or a smaller size."
            )
        except CloudinaryError as exc:
            if getattr(exc, "status", None) == 404:
                return ToolResult.error(
                    "Error: Cloudinary does not offer that operation on this account "
                    "(the add-on is not enabled). Trims, crops, transcodes and posters are "
                    "rendered by the transformation engine and work on any plan."
                )
            return ToolResult.error(f"Error: Cloudinary rejected the edit: {exc}")
        except (ArtifactError, OSError, ValueError) as exc:
            return ToolResult.error(f"Error: video editing failed: {exc}")

    async def _animate(
        self, client: Any, *, prompt: str | None, source: str, public_id: str | None
    ) -> str:
        """Generative image-to-video, only where the account has the add-on."""
        from nanobot.providers.cloudinary import CloudinaryError

        path = await _resolve_media(str(source))
        prompt = prompt or "subtle natural motion, cinematic"
        try:
            # image_to_video animates a *still*, and a video handed to it is
            # rejected as "Invalid image file". When the source is a clip, the
            # frame to animate is the one the transformation engine takes out of
            # it - so the caller can point at either and get the same result.
            if _resource_type(path) == "video":
                from nanobot.providers.cloudinary import with_transformation

                clip = await client.upload(path.read_bytes(), resource_type="video")
                frame_url = with_transformation(clip.secure_url, "so_0", ext="jpg")
                still = await client.upload(
                    await client.download(frame_url, max_bytes=32 * 1024 * 1024),
                    resource_type="image",
                )
            else:
                still = await client.upload(path.read_bytes(), resource_type="image")
            generation = await client.image_to_video(
                prompt, [still.secure_url], public_id=public_id or None
            )
        except CloudinaryError as exc:
            logger.warning("cloudinary image_to_video failed: {}", exc)
            return ToolResult.error(
                "Error: Cloudinary's image-to-video add-on is not available on this account "
                f"({exc}). Trims, crops, transcodes and posters still work on any plan."
            )
        if not generation.assets:
            return ToolResult.error("Error: Cloudinary returned no clip.")
        artifact = await self._render(
            client,
            generation.assets[0].secure_url,
            Path(f"{path.stem}-animated.mp4"),
            prompt=prompt,
            model="cloudinary image_to_video",
        )
        return _media_tool_result([artifact])


__all__ = ["CloudinaryVideoEditTool"]
