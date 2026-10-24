from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop, TurnKind
from nanobot.agent.tools.novita_sandbox import NovitaSandboxTool
from nanobot.bus.events import InboundMessage
from nanobot.providers.registry import model_accepts_images

PNG = b"\x89PNG\r\n\x1a\nminimal"


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-opus-4-5",
        "openai/gpt-4o",
        "gemini-2.5-flash",
        "deepseek-v4-flash-vision-exp",
        "qwen2.5-vl-7b",
    ],
)
def test_known_multimodal_models_accept_images(model: str) -> None:
    assert model_accepts_images(model) is True


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "gemma-3", "deepseek-chat", "", None])
def test_text_only_or_unknown_models_do_not_accept_images(model: str | None) -> None:
    assert model_accepts_images(model) is False


def _loop_for_model(model: str | None) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.sessions = SimpleNamespace(get_cached=lambda _key: None)
    loop.runtime_resolver = SimpleNamespace(runtime=SimpleNamespace(model=model))
    return loop


def _ctx(image) -> SimpleNamespace:
    return SimpleNamespace(
        kind=TurnKind.USER,
        session_key="websocket:abc",
        session=None,
        msg=InboundMessage(
            channel="websocket",
            sender_id="u",
            chat_id="abc",
            content="Remove the background from this picture",
            media=[str(image)],
            metadata={"message_id": 1},
        ),
    )


@pytest.mark.asyncio
async def test_vision_model_keeps_pixels_and_gets_ocr_text(tmp_path, monkeypatch) -> None:
    image = tmp_path / "photo.png"
    image.write_bytes(PNG)
    monkeypatch.setattr(
        NovitaSandboxTool,
        "analyze_telegram_images",
        AsyncMock(return_value="Text: SALE 50%"),
    )
    monkeypatch.setattr(NovitaSandboxTool, "backend_name", lambda self: "vps")
    ctx = _ctx(image)

    await _loop_for_model("anthropic/claude-opus-4-5")._prepare_sandbox_images(ctx)

    assert ctx.msg.media == [str(image)]
    assert "SALE 50%" in ctx.msg.content
    assert str(image) in ctx.msg.content
    assert ctx.msg.metadata["images_execution_backend"] == "vps"


@pytest.mark.asyncio
async def test_text_only_model_still_gets_ocr_only(tmp_path, monkeypatch) -> None:
    image = tmp_path / "photo.png"
    image.write_bytes(PNG)
    monkeypatch.setattr(
        NovitaSandboxTool,
        "analyze_telegram_images",
        AsyncMock(return_value="Text: SALE 50%"),
    )
    monkeypatch.setattr(NovitaSandboxTool, "backend_name", lambda self: "vps")
    ctx = _ctx(image)

    await _loop_for_model("deepseek-v4-flash")._prepare_sandbox_images(ctx)

    assert ctx.msg.media == []
    assert "SALE 50%" in ctx.msg.content


def test_vision_turn_builds_an_image_block_for_the_provider(tmp_path) -> None:
    image = tmp_path / "photo.png"
    image.write_bytes(PNG)

    content = ContextBuilder.build_user_content(
        ContextBuilder.__new__(ContextBuilder),
        "Remove the background",
        image_paths=[str(image)],
    )

    assert isinstance(content, list)
    assert any(
        isinstance(block, dict)
        and block.get("type") == "image_url"
        and str(block.get("image_url", {}).get("url", "")).startswith("data:image/png;base64,")
        for block in content
    )
