"""Fetching an image to disk, and the document path it unlocks.

An image returned as content blocks can be looked at and nothing more. Saving it
is what lets a fetched picture reach a PDF, a DOCX or a deck.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.tools import web as web_module
from nanobot.agent.tools.web import WebFetchTool

#: A real 1x1 transparent PNG, so the bytes written are a decodable image and
#: not just filler that happens to carry an image content-type.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAF"
    "AAH/q842iQAAAABJRU5ErkJggg=="
)


class _Response:
    def __init__(self, content: bytes, ctype: str) -> None:
        self.content = content
        self.headers = {"content-type": ctype}
        self.status_code = 200
        self.text = ""

    def raise_for_status(self) -> None:
        return None

    async def aread(self) -> bytes:
        return self.content


class _Stream:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the image preflight at a canned PNG response."""
    monkeypatch.chdir(tmp_path)

    async def fake_stream(_client, url, headers=None):  # noqa: ANN001, ARG001
        return _Response(PNG, "image/png"), _Stream(), None, False

    monkeypatch.setattr(web_module, "_stream_with_safe_redirects", fake_stream)
    return tmp_path


def _fetch(tool: WebFetchTool, **kwargs: Any) -> dict[str, Any]:
    result = asyncio.run(tool.execute("https://example.com/logo.png", **kwargs))
    return json.loads(str(result))


def test_an_image_without_save_to_is_returned_as_a_picture(workspace: Path) -> None:
    """Unchanged behaviour: no save_to, no file, the image itself."""
    tool = WebFetchTool(workspace=workspace)

    result = asyncio.run(tool.execute("https://example.com/logo.png"))

    assert isinstance(result, list)
    assert result[0]["type"] == "image_url"
    assert not list(workspace.glob("**/*.png"))


def test_save_to_writes_the_image_and_returns_a_path(workspace: Path) -> None:
    tool = WebFetchTool(workspace=workspace)

    payload = _fetch(tool, save_to="assets/logo.png")

    written = workspace / "assets" / "logo.png"
    assert written.read_bytes() == PNG
    assert payload["saved_to"] == str(written)
    assert payload["bytes"] == len(PNG)
    assert payload["content_type"] == "image/png"
    assert "add_picture" in payload["note"]


def test_save_to_accepts_the_camel_case_spelling(workspace: Path) -> None:
    """The harness may pass saveTo, exactly as it does for maxChars."""
    tool = WebFetchTool(workspace=workspace)

    asyncio.run(tool.execute("https://example.com/logo.png", **{"saveTo": "a/b.png"}))

    assert (workspace / "a" / "b.png").read_bytes() == PNG


def test_save_to_cannot_escape_the_workspace(workspace: Path) -> None:
    tool = WebFetchTool(workspace=workspace)

    payload = _fetch(tool, save_to="../../outside.png")

    assert "inside the agent workspace" in payload["error"]
    assert not (workspace.parent.parent / "outside.png").exists()


def test_an_absolute_path_outside_the_workspace_is_refused(workspace: Path) -> None:
    tool = WebFetchTool(workspace=workspace)

    payload = _fetch(tool, save_to="/tmp/definitely-outside.png")

    assert "inside the agent workspace" in payload["error"]


def test_an_oversized_image_is_refused_rather_than_written(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(WebFetchTool, "_MAX_SAVED_IMAGE_BYTES", 4)
    tool = WebFetchTool(workspace=workspace)

    payload = _fetch(tool, save_to="big.png")

    assert "above the 4 byte limit" in payload["error"]
    assert not (workspace / "big.png").exists()
