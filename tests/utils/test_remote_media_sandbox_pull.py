"""A sandbox path handed to a host-side media tool is pulled across.

``generate_image`` and ``cloudinary_video_edit`` run on the gateway host while
the agent's working files live inside the execution sandbox, so a path the
sandbox tool returned (``image_input.jpg``, ``/workspace/out.mp4``) resolved to
nothing and the turn reported it as "Cloudinary failed to locate the staged
file". These cover the bridge that closes that gap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.tools import cloudinary_video as vid
from nanobot.agent.tools import image_generation as gen
from nanobot.agent.tools import workspace_bridge as wb
from nanobot.config.schema import ImageGenerationToolConfig, ProviderConfig
from nanobot.utils import remote_media as rm

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdacd\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture()
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setattr(rm, "get_media_dir", lambda *_a, **_k: root)
    monkeypatch.setattr(gen, "get_media_dir", lambda *_a, **_k: root)
    return root


def _fake_sandbox(
    monkeypatch: pytest.MonkeyPatch, files: dict[str, bytes], root: str | None = "/workspace"
) -> list[str]:
    """Patch the bridge so the sandbox is a dict of path -> bytes."""
    seen: list[str] = []

    async def fake_root() -> str | None:
        return root

    async def fake_fetch(remote_path: str, **_kwargs: object) -> bytes | None:
        seen.append(remote_path)
        return files.get(remote_path)

    monkeypatch.setattr(wb, "sandbox_workspace_root", fake_root)
    monkeypatch.setattr(wb, "fetch_remote_file", fake_fetch)
    return seen


# ------------------------------------------------------- the reference test


def test_is_sandbox_reference_only_for_sandbox_shaped_values() -> None:
    assert rm.is_sandbox_reference("image_input.jpg") is True
    assert rm.is_sandbox_reference("/workspace/out.mp4") is True
    assert rm.is_sandbox_reference("https://onlyfiles.com/x.png") is False
    assert rm.is_sandbox_reference("references/local.png") is False
    assert rm.is_sandbox_reference("") is False
    assert rm.is_sandbox_reference(None) is False


def test_sandbox_filename_is_stable_and_carries_the_basename() -> None:
    name = rm.sandbox_filename("/workspace/a b/out 1.png")
    assert name.endswith("-out_1.png")
    assert name == rm.sandbox_filename("/workspace/a b/out 1.png")
    assert rm.sandbox_filename("/workspace/../other.png") != name


# ----------------------------------------------------------- materializing


@pytest.mark.asyncio
async def test_a_bare_name_is_looked_for_under_the_workspace_root(
    monkeypatch: pytest.MonkeyPatch, media_root: Path
) -> None:
    seen = _fake_sandbox(monkeypatch, {"/workspace/image_input.jpg": PNG_BYTES})

    path = await rm.materialize_sandbox_file("image_input.jpg")

    assert seen == ["/workspace/image_input.jpg"]
    assert path.read_bytes() == PNG_BYTES
    assert media_root in path.parents


@pytest.mark.asyncio
async def test_an_absolute_sandbox_path_is_pulled_as_given(
    monkeypatch: pytest.MonkeyPatch, media_root: Path
) -> None:
    seen = _fake_sandbox(monkeypatch, {"/workspace/render/out.mp4": b"video"})

    path = await rm.materialize_sandbox_file("/workspace/render/out.mp4")

    assert seen == ["/workspace/render/out.mp4"]
    assert path.read_bytes() == b"video"


@pytest.mark.asyncio
async def test_a_missing_sandbox_file_says_where_it_looked(
    monkeypatch: pytest.MonkeyPatch, media_root: Path
) -> None:
    _fake_sandbox(monkeypatch, {})

    with pytest.raises(rm.RemoteMediaError) as excinfo:
        await rm.materialize_sandbox_file("image_input.jpg")

    message = str(excinfo.value)
    assert "was not found in the execution sandbox" in message
    assert "/workspace/image_input.jpg" in message
    assert "https link" in message


@pytest.mark.asyncio
async def test_a_bare_name_without_a_sandbox_says_so(
    monkeypatch: pytest.MonkeyPatch, media_root: Path
) -> None:
    _fake_sandbox(monkeypatch, {}, root=None)

    with pytest.raises(rm.RemoteMediaError) as excinfo:
        await rm.materialize_sandbox_file("image_input.jpg")

    assert "no execution backend is configured" in str(excinfo.value)


@pytest.mark.asyncio
async def test_try_materialize_is_a_silent_miss(
    monkeypatch: pytest.MonkeyPatch, media_root: Path
) -> None:
    _fake_sandbox(monkeypatch, {})

    assert await rm.try_materialize_sandbox_file("https://onlyfiles.com/x.png") is None
    assert await rm.try_materialize_sandbox_file("/data/not-in-a-sandbox.png") is None


# --------------------------------------------------------------- the callers


@pytest.mark.asyncio
async def test_cloudinary_resolves_a_sandbox_path_to_the_pulled_copy(
    monkeypatch: pytest.MonkeyPatch, media_root: Path
) -> None:
    """The reported symptom: a bare sandbox name is not a host path."""
    _fake_sandbox(monkeypatch, {"/workspace/image_input.jpg": b"clip-bytes"})

    resolved = await vid._resolve_media("image_input.jpg")

    assert resolved.read_bytes() == b"clip-bytes"
    assert resolved.is_file()


@pytest.mark.asyncio
async def test_cloudinary_keeps_its_own_error_when_the_sandbox_has_nothing(
    monkeypatch: pytest.MonkeyPatch, media_root: Path
) -> None:
    _fake_sandbox(monkeypatch, {})

    with pytest.raises(vid.ArtifactError) as excinfo:
        await vid._resolve_media("sandbox-only-frame.jpg")

    message = str(excinfo.value)
    assert "file not found: sandbox-only-frame.jpg" in message
    assert "neither a file on this host" in message


@pytest.mark.asyncio
async def test_cloudinary_does_not_touch_the_sandbox_for_a_host_file(
    monkeypatch: pytest.MonkeyPatch, media_root: Path, tmp_path: Path
) -> None:
    clip = tmp_path / "local.mp4"
    clip.write_bytes(b"v")
    seen = _fake_sandbox(monkeypatch, {})

    assert await vid._resolve_media(str(clip)) == clip
    assert seen == []


@pytest.mark.asyncio
async def test_generate_image_resolves_a_sandbox_reference(
    monkeypatch: pytest.MonkeyPatch, media_root: Path, tmp_path: Path
) -> None:
    _fake_sandbox(monkeypatch, {"/workspace/ref-frame.png": PNG_BYTES})
    tool = gen.ImageGenerationTool(
        workspace=tmp_path / "ws",
        config=ImageGenerationToolConfig(enabled=True),
        provider_config=ProviderConfig(api_key="k"),
    )

    resolved = await tool._resolve_reference_images(["ref-frame.png"])

    assert len(resolved) == 1
    assert Path(resolved[0]).read_bytes() == PNG_BYTES


@pytest.mark.asyncio
async def test_generate_image_still_refuses_a_reference_that_is_nowhere(
    monkeypatch: pytest.MonkeyPatch, media_root: Path, tmp_path: Path
) -> None:
    _fake_sandbox(monkeypatch, {})
    tool = gen.ImageGenerationTool(
        workspace=tmp_path / "ws",
        config=ImageGenerationToolConfig(enabled=True),
        provider_config=ProviderConfig(api_key="k"),
    )

    with pytest.raises(gen.ImageGenerationError):
        await tool._resolve_reference_images(["nowhere-frame.png"])
