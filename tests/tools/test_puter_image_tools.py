from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.agent.tools import puter_image_tools
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import request_context
from nanobot.agent.tools.puter_image_tools import (
    PuterEditImageTool,
    PuterGenerateImageTool,
)
from nanobot.config.loader import set_config_path


class FakeSupabase:
    configured = True

    def __init__(self) -> None:
        self.charged: list[dict[str, object]] = []
        self.generated: list[tuple[str, str, str]] = []  # (action, prompt, model)
        self.edited: list[list[object]] = []
        self.failure: Exception | None = None

    async def charge_step(self, account: dict[str, str], task_ref: str, step_no: int) -> dict[str, object]:
        self.charged.append({"account": account, "task_ref": task_ref, "step_no": step_no})
        if self.failure is not None:
            raise self.failure
        return {"success": True, "balance": 10}

    async def puter_generate(self, account: dict[str, str], action: str, prompt: str, *, model: str = "") -> dict[str, object]:
        self.generated.append((action, prompt, model))
        return {"data_uri": _tiny_png_data_url(), "mime": "image/png"}

    async def puter_edit_image(
        self,
        account: dict[str, str],
        prompt: str,
        input_images: list[str],
        *,
        model: str = "",
    ) -> dict[str, object]:
        self.edited.append([prompt, input_images, model])
        return {"data_uri": _tiny_png_data_url(), "mime": "image/png"}


def _tiny_png_data_url() -> str:
    # 1x1 transparent PNG.
    b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    return f"data:image/png;base64,{b64}"


@pytest.fixture
def fake_supabase(monkeypatch) -> FakeSupabase:
    fake = FakeSupabase()
    monkeypatch.setattr(puter_image_tools, "SupabaseAuth", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def isolated_runtime_dir(monkeypatch, tmp_path) -> None:
    # Redirect nanobot's runtime data (media, artifacts) into a temp dir so the
    # tools never write into a real ~/.nanobot when running tests.
    set_config_path(tmp_path / "instance" / "config.json")


def _bind_context(channel: str = "websocket", user_id: str | None = "user-1"):
    return request_context(
        SimpleNamespace(
            channel=channel,
            chat_id="chat-1",
            message_id="m1",
            session_key=f"{channel}:chat-1",
            metadata={} if user_id is None else {"supabase_user_id": user_id},
        )
    )


@pytest.mark.asyncio
async def test_generate_tool_requires_sign_in(fake_supabase, monkeypatch) -> None:
    tool = PuterGenerateImageTool()
    monkeypatch.chdir(".")
    with _bind_context(user_id=None):
        result = await tool.execute(prompt="a cat")
    assert isinstance(result, ToolResult) and result.is_error
    assert "sign in" in str(result).lower()
    assert fake_supabase.generated == []


@pytest.mark.asyncio
async def test_generate_tool_charges_credit_and_generates(fake_supabase, monkeypatch) -> None:
    tool = PuterGenerateImageTool()
    monkeypatch.chdir(".")
    with _bind_context():
        result = await tool.execute(prompt="a neon cat")
    # Success path returns a JSON artifact string (matches core image tool contract).
    assert isinstance(result, str)
    assert "artifacts" in result
    assert "a neon cat" in result
    # One credit step charged for the image generation.
    assert len(fake_supabase.charged) == 1
    assert fake_supabase.generated[0][1] == "a neon cat"


@pytest.mark.asyncio
async def test_generate_tool_inert_outside_webui_channel(fake_supabase, monkeypatch) -> None:
    tool = PuterGenerateImageTool()
    monkeypatch.chdir(".")
    with _bind_context(channel="telegram", user_id="user-1"):
        result = await tool.execute(prompt="a cat")
    assert isinstance(result, ToolResult) and result.is_error
    assert fake_supabase.generated == []


@pytest.mark.asyncio
async def test_generate_tool_fails_closed_on_credit_exhausted(fake_supabase, monkeypatch) -> None:
    fake_supabase.failure = Exception("Insufficient credits for this step")
    tool = PuterGenerateImageTool()
    monkeypatch.chdir(".")
    with _bind_context():
        result = await tool.execute(prompt="a cat")
    assert isinstance(result, ToolResult) and result.is_error
    assert "credit" in str(result).lower()
    assert fake_supabase.generated == []


@pytest.mark.asyncio
async def test_edit_tool_requires_an_image(fake_supabase, monkeypatch) -> None:
    tool = PuterEditImageTool()
    monkeypatch.chdir(".")
    with _bind_context():
        result = await tool.execute(prompt="make it night")
    assert isinstance(result, ToolResult) and result.is_error
    assert fake_supabase.edited == []