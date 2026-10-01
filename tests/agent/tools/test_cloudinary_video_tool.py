"""Cloudinary video editing and the provider chain that keeps it primary.

Offline: the Cloudinary client is replaced by a fake, so these assert the URLs
and the decisions rather than any render.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.tools import cloudinary_video as vid
from nanobot.agent.tools.cloudinary_video import CloudinaryVideoEditTool
from nanobot.config.schema import Config, ProviderConfig
from nanobot.providers import cloudinary as cld
from nanobot.providers.image_generation import (
    DEFAULT_IMAGE_PROVIDER_ORDER,
    image_provider_available,
    image_provider_chain,
    primary_image_provider_available,
)

CLIP = "https://res.cloudinary.com/cloud-a/video/upload/v7/clip.mp4"


class FakeCloudinary:
    """Stands in for CloudinaryClient: records what was asked for."""

    def __init__(self, *, cloud_name: str = "cloud-a", fmt: str = "mp4") -> None:
        self.uploads: list[dict[str, Any]] = []
        self.downloaded: list[str] = []
        self.cloud_name = cloud_name
        self.fmt = fmt
        self.fail_download: int | None = None

    async def upload(
        self,
        data: bytes,
        *,
        resource_type: str = "image",
        public_id: str | None = None,
        folder: str | None = None,
        filename: str | None = None,
        cloud_name: str | None = None,
    ) -> cld.CloudinaryAsset:
        self.uploads.append(
            {
                "bytes": len(data),
                "resource_type": resource_type,
                "public_id": public_id,
                "cloud_name": cloud_name,
            }
        )
        name = public_id or f"asset-{len(self.uploads)}"
        return cld.CloudinaryAsset(
            secure_url=f"https://res.cloudinary.com/{self.cloud_name}/{resource_type}/upload/v7/{name}.{self.fmt}",
            public_id=name,
            asset_id=f"id-{len(self.uploads)}",
            resource_type=resource_type,
            format=self.fmt,
            width=640,
            height=360,
            bytes=len(data),
            cloud_name=self.cloud_name,
        )

    async def download(self, url: str, *, max_bytes: int = 0) -> bytes:
        if self.fail_download is not None:
            raise cld.CloudinaryError("nope", kind="server", status=self.fail_download)
        self.downloaded.append(url)
        return b"rendered-bytes" * 10

    async def image_to_video(self, prompt: str, refs: list[str], **kwargs: Any) -> Any:
        raise cld.CloudinaryError(
            "cloudinary 404: not found", kind="request", status=404
        )


@pytest.fixture()
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(vid, "get_media_dir", lambda *a, **k: tmp_path)
    return tmp_path


@pytest.fixture()
def clip(tmp_path: Path) -> Path:
    path = tmp_path / "sample.mp4"
    path.write_bytes(b"x" * 4096)
    return path


def _tool(monkeypatch: pytest.MonkeyPatch, fake: FakeCloudinary) -> CloudinaryVideoEditTool:
    tool = CloudinaryVideoEditTool(api_key="cloudinary://k:s@cloud-a")
    monkeypatch.setattr(tool, "_client", lambda: fake)
    return tool


# ------------------------------------------------------------------ the actions


@pytest.mark.asyncio
async def test_trim_asks_cloudinary_for_a_span_and_stores_the_render(
    monkeypatch: pytest.MonkeyPatch, media_root: Path, clip: Path
) -> None:
    fake = FakeCloudinary()
    tool = _tool(monkeypatch, fake)

    result = await tool.execute("trim", str(clip), start=1.0, end=2.5)

    assert fake.uploads[0]["resource_type"] == "video"
    assert fake.downloaded == [
        "https://res.cloudinary.com/cloud-a/video/upload/so_1,eo_2.5/v7/asset-1.mp4"
    ]
    payload = json.loads(result)
    stored = Path(payload["artifacts"][0]["path"])
    assert stored.is_file()
    assert stored.suffix == ".mp4"
    assert payload["artifacts"][0]["provider"] == "cloudinary"


@pytest.mark.asyncio
async def test_poster_pulls_a_still_out_of_the_clip(
    monkeypatch: pytest.MonkeyPatch, media_root: Path, clip: Path
) -> None:
    fake = FakeCloudinary()
    tool = _tool(monkeypatch, fake)

    result = await tool.execute("poster", str(clip), start=2.0, width=480)

    url = fake.downloaded[0]
    assert url.endswith(".jpg")
    assert "so_2" in url and "w_480" in url
    assert Path(json.loads(result)["artifacts"][0]["path"]).suffix == ".jpg"


@pytest.mark.asyncio
async def test_transcode_changes_container_and_frame_rate(
    monkeypatch: pytest.MonkeyPatch, media_root: Path, clip: Path
) -> None:
    fake = FakeCloudinary()
    tool = _tool(monkeypatch, fake)

    result = await tool.execute("transcode", str(clip), format="webm", fps=15)

    assert "fps_15" in fake.downloaded[0]
    assert "f_webm" in fake.downloaded[0]
    assert Path(json.loads(result)["artifacts"][0]["path"]).suffix == ".webm"


@pytest.mark.asyncio
async def test_concat_puts_the_second_clip_in_the_same_cloud_as_the_first(
    monkeypatch: pytest.MonkeyPatch, media_root: Path, clip: Path
) -> None:
    fake = FakeCloudinary()
    tool = _tool(monkeypatch, fake)
    second = clip.parent / "second.mp4"
    second.write_bytes(b"y" * 1024)

    await tool.execute("concat", str(clip), second_clip=str(second))

    # A layer must live where the base clip lives, or the render is a 400.
    assert fake.uploads[0]["cloud_name"] is None  # base: the pool chooses
    assert fake.uploads[1]["cloud_name"] == "cloud-a"  # layer: pinned to it
    assert "l_video:asset-2.mp4,fl_splice" in fake.downloaded[0]


@pytest.mark.asyncio
async def test_describe_reports_the_stored_asset_without_rendering(
    monkeypatch: pytest.MonkeyPatch, media_root: Path, clip: Path
) -> None:
    fake = FakeCloudinary()
    tool = _tool(monkeypatch, fake)

    result = await tool.execute("describe", str(clip))

    assert fake.downloaded == []
    assert "res.cloudinary.com" in result


@pytest.mark.asyncio
async def test_an_unknown_action_is_refused_before_anything_is_uploaded(
    monkeypatch: pytest.MonkeyPatch, media_root: Path, clip: Path
) -> None:
    fake = FakeCloudinary()
    tool = _tool(monkeypatch, fake)

    result = await tool.execute("explode", str(clip))

    assert "unknown action" in result
    assert fake.uploads == []


@pytest.mark.asyncio
async def test_an_empty_edit_is_refused_rather_than_uploading_for_nothing(
    monkeypatch: pytest.MonkeyPatch, media_root: Path, clip: Path
) -> None:
    fake = FakeCloudinary()
    tool = _tool(monkeypatch, fake)

    result = await tool.execute("crop", str(clip))

    assert "nothing to change" in result


@pytest.mark.asyncio
async def test_a_missing_file_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, media_root: Path
) -> None:
    tool = _tool(monkeypatch, FakeCloudinary())

    result = await tool.execute("trim", "/nope/missing.mp4", start=0, end=1)

    assert "file not found" in result


@pytest.mark.asyncio
async def test_animate_says_plainly_that_the_addon_is_absent(
    monkeypatch: pytest.MonkeyPatch, media_root: Path, clip: Path
) -> None:
    """image_to_video answers MG_00014 on a plan without the video add-on."""
    fake = FakeCloudinary()
    tool = _tool(monkeypatch, fake)

    result = await tool.execute("animate", str(clip), prompt="drift")

    assert "image-to-video add-on is not available" in result
    assert "posters still work" in result


@pytest.mark.asyncio
async def test_animate_takes_the_frame_from_a_clip_before_animating_it(
    monkeypatch: pytest.MonkeyPatch, media_root: Path, clip: Path
) -> None:
    """A video handed to image_to_video is rejected as an invalid image."""
    fake = FakeCloudinary()
    tool = _tool(monkeypatch, fake)

    await tool.execute("animate", str(clip), prompt="drift")

    # base clip -> poster frame -> that still is the reference image
    assert [u["resource_type"] for u in fake.uploads] == ["video", "image"]


def test_the_tool_is_only_offered_when_cloudinary_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLOUDINARY_ACCOUNTS", raising=False)
    monkeypatch.delenv("CLOUDINARY_URL", raising=False)

    class Ctx:
        image_generation_provider_configs: dict[str, ProviderConfig] = {}

    assert CloudinaryVideoEditTool.enabled(Ctx()) is False  # type: ignore[arg-type]

    class Ctx2:
        image_generation_provider_configs = {
            "cloudinary": ProviderConfig(api_key="cloudinary://k:s@cloud-a")
        }

    assert CloudinaryVideoEditTool.enabled(Ctx2()) is True  # type: ignore[arg-type]


def test_the_tool_reports_the_key_the_settings_panel_wrote() -> None:
    class Ctx:
        image_generation_provider_configs = {
            "cloudinary": ProviderConfig(api_key="cloudinary://k:s@cloud-a")
        }

    tool = CloudinaryVideoEditTool.create(Ctx())  # type: ignore[arg-type]
    assert tool.api_key == "cloudinary://k:s@cloud-a"  # type: ignore[attr-defined]


# ------------------------------------------------------- transformations by URL


def test_a_transformation_is_inserted_into_a_delivery_url() -> None:
    url = cld.with_transformation(CLIP, "so_1,eo_3", ext="mp4")
    assert url == "https://res.cloudinary.com/cloud-a/video/upload/so_1,eo_3/v7/clip.mp4"


def test_a_poster_is_the_same_url_under_a_still_extension() -> None:
    url = cld.with_transformation(CLIP, "so_2", ext="jpg")
    assert url.endswith("/so_2/v7/clip.jpg")


def test_a_video_layer_carries_its_format_or_cloudinary_refuses_it() -> None:
    assert cld.splice_transformation(public_id="b", ext="mp4") == "l_video:b.mp4,fl_splice"


def test_a_layer_for_a_still_needs_no_extension() -> None:
    assert cld.splice_transformation(public_id="logo", resource_type="image") == (
        "l_image:logo,fl_splice"
    )


# ---------------------------------------------------------------- the chain


def test_a_provider_without_credentials_is_not_available() -> None:
    providers = {"cloudinary": ProviderConfig(api_key=None)}

    assert image_provider_available("cloudinary", providers) is False
    assert image_provider_available("openai", providers) is False


def test_cloudinary_counts_as_available_when_its_accounts_are_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUDINARY_ACCOUNTS", "cloudinary://k:s@cloud-a")

    assert image_provider_available("cloudinary", {}) is True


def test_a_local_ollama_is_only_available_once_a_base_url_is_given() -> None:
    """Otherwise it would rank as a usable provider in every install."""
    assert image_provider_available("ollama", {"ollama": ProviderConfig()}) is False
    assert (
        image_provider_available("ollama", {"ollama": ProviderConfig(api_base="http://x")})
        is True
    )


def test_cloudinary_leads_the_chain_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDINARY_ACCOUNTS", "cloudinary://k:s@cloud-a")

    chain = image_provider_chain("cloudinary", {})

    assert chain[0] == "cloudinary"


def test_the_operator_choice_that_can_serve_leads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDINARY_ACCOUNTS", "cloudinary://k:s@cloud-a")
    providers = {"openai": ProviderConfig(api_key="sk-x")}

    chain = image_provider_chain("openai", providers)

    assert chain == ["openai", "cloudinary"]


def test_a_stale_choice_without_credentials_falls_through_to_the_keys_that_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved provider whose key was removed must not block image generation."""
    monkeypatch.setenv("CLOUDINARY_ACCOUNTS", "cloudinary://k:s@cloud-a")

    chain = image_provider_chain("openrouter", {"openrouter": ProviderConfig()})

    assert chain == ["cloudinary"]


def test_no_provider_at_all_yields_no_chain() -> None:
    assert image_provider_chain("cloudinary", {}) == []
    assert primary_image_provider_available({}) is False


def test_puter_is_not_part_of_the_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Puter is reached only by its own tools, which withhold themselves."""
    monkeypatch.setenv("CLOUDINARY_ACCOUNTS", "cloudinary://k:s@cloud-a")

    assert "puter" not in DEFAULT_IMAGE_PROVIDER_ORDER
    assert "puter" not in image_provider_chain("cloudinary", {})


def test_the_shipped_default_provider_is_cloudinary() -> None:
    assert Config.model_validate({}).tools.image_generation.provider == "cloudinary"
