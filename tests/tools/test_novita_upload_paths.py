"""``novita_sandbox action=upload`` must survive a missing destination.

The session behind the report "the image is not accessible from the sandbox or
Cloudinary because it resides in a temporary media directory that the tools
cannot reach directly" had, in the same turn, called

    novita_sandbox(action="upload",
                   source="/home/nanobot/.nanobot/media/websocket/034aa76fd1a9.jpg")

with no ``path``. Every backend branch read the destination as
``str(kwargs.get("path") or "")`` and handed the empty string to the backend,
whose path guard raised::

    ValueError: path is required
      File ".../novita_sandbox.py", line 3842, in _execute_freestyle_inner
        await backend.write_bytes(path, ...)
      File ".../freestyle_backend.py", line 342, in _safe_path
        raise ValueError("path is required")

Nothing caught it, so the raise escaped the tool: the file never landed in the
sandbox, and the model — with no file to point at and no error it could act on
— narrated the media directory as unreachable. Cloudinary was configured the
whole time and both of its entry points read a local path off this host, so the
attachment was never unreachable.

These tests pin the three things that fix that: an omitted destination defaults
instead of crashing, a real failure comes back as an actionable error result,
and the Cloudinary tools really do accept the local path the model was told it
could not use.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.novita_sandbox import (
    NovitaSandboxTool,
    _upload_destination,
)

SOURCE_PATH = "nanobot/agent/tools/novita_sandbox.py"


class _Recorder:
    """A writer that records what the staging helper hands it."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.error = error

    async def __call__(self, target: str, data: bytes) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append((target, data))


@pytest.fixture()
def media_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A user attachment, exactly where the live process keeps one."""
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(tmp_path))
    media = tmp_path / "media" / "websocket"
    media.mkdir(parents=True)
    attachment = media / "034aa76fd1a9.jpg"
    attachment.write_bytes(b"\xff\xd8\xff\xe0 jpeg bytes")
    return attachment


def test_an_omitted_destination_defaults_under_the_workspace() -> None:
    destination = _upload_destination(Path("/tmp/034aa76fd1a9.jpg"), None)
    assert destination == "/workspace/uploads/034aa76fd1a9.jpg"
    # An empty string is what `kwargs.get("path") or ""` used to produce.
    assert _upload_destination(Path("/tmp/034aa76fd1a9.jpg"), "  ") == destination


def test_an_explicit_destination_is_kept() -> None:
    assert _upload_destination(Path("/tmp/a.jpg"), "/workspace/in/a.jpg") == "/workspace/in/a.jpg"


def test_a_filename_is_sanitised_before_it_becomes_a_path() -> None:
    destination = _upload_destination(Path("/tmp/../evil name;rm -rf/.jpg"), None)
    assert destination == "/workspace/uploads/.jpg"
    assert re.fullmatch(r"/workspace/uploads/[A-Za-z0-9._-]{1,120}", destination)


def test_upload_without_a_destination_no_longer_crashes_the_turn(media_file: Path) -> None:
    """The exact call from the log: source given, path omitted."""
    writer = _Recorder()
    tool = NovitaSandboxTool()

    staged = asyncio.run(tool._stage_upload({"source": str(media_file)}, writer, label="a sandbox"))

    assert not isinstance(staged, ToolResult), staged
    source, destination = staged
    assert source == media_file
    assert destination == f"/workspace/uploads/{media_file.name}"
    assert writer.calls == [(destination, media_file.read_bytes())]


def test_a_backend_that_rejects_an_empty_path_is_reported_not_raised(media_file: Path) -> None:
    """The crash that started the story, in the shape the log shows it."""
    writer = _Recorder(ValueError("path is required"))
    tool = NovitaSandboxTool()

    result = asyncio.run(
        tool._stage_upload({"source": str(media_file)}, writer, label="the Freestyle VM")
    )

    assert isinstance(result, ToolResult) and result.is_error
    # The model must be told the local file is fine, or it repeats the false
    # "the tools cannot reach the media directory" claim.
    assert str(media_file) in result
    assert "readable local file" in result
    assert "path is required" in result


def test_upload_without_a_source_is_an_error_result_not_a_raise() -> None:
    writer = _Recorder()
    tool = NovitaSandboxTool()

    result = asyncio.run(tool._stage_upload({"path": "/workspace/x.jpg"}, writer, label="a sandbox"))

    assert isinstance(result, ToolResult) and result.is_error
    assert "source" in result
    assert writer.calls == []


def test_an_attachment_outside_the_media_directory_is_still_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(tmp_path / "data"))
    outside = tmp_path / "elsewhere.jpg"
    outside.write_bytes(b"x")
    tool = NovitaSandboxTool()

    result = asyncio.run(
        tool._stage_upload({"source": str(outside)}, _Recorder(), label="a sandbox")
    )

    assert isinstance(result, ToolResult) and result.is_error
    assert "media/data directory" in result


def test_every_upload_branch_routes_through_the_shared_helper() -> None:
    """Guard the 8 duplicated branches the crash lived in.

    The same ``source = Path(...)`` / ``path = str(kwargs.get("path") or "")``
    block was copy-pasted per backend, so the missing-destination bug existed
    eight times over (and once more in the direct-sandbox path). They now all
    delegate, which is what makes one fix cover every backend.
    """
    source_text = Path(SOURCE_PATH).read_text()
    # 8 backend branches: vps (via _prepare_upload) + daytona, vercel, runloop,
    # tenki, freestyle, upstash and the direct Novita sandbox (_stage_upload).
    assert source_text.count('if action == "upload":') == 8
    assert source_text.count("await self._stage_upload(") == 8
    assert source_text.count("await self._prepare_upload(") == 1
    assert source_text.count('if action in {"upload", "fetch_url"}:') == 1
    # No upload branch may hand a body of its own to the backend any more: the
    # `write` action keeps its own `path = str(kwargs.get("path") or "")` line,
    # so the guard is pinned to the `write_bytes` shape the crash had.
    assert source_text.count('path = str(kwargs.get("path") or "")\n                    await backend.write_bytes') == 0
    assert "def _upload_destination(" in source_text


def test_the_cloudinary_video_tool_accepts_the_local_path_it_was_said_to_lose(
    media_file: Path,
) -> None:
    """The claim was false: a local media path is exactly what the tool wants.

    ``_resolve_media`` returns a readable local file for a path, so the turn
    that reported the attachment as unreadable had a perfectly usable input.
    """
    from nanobot.agent.tools.cloudinary_video import _resolve_media, _resource_type

    resolved = asyncio.run(_resolve_media(str(media_file)))

    assert resolved == media_file
    assert _resource_type(resolved) == "image"


def test_the_success_message_says_the_local_path_still_works() -> None:
    from nanobot.agent.tools.novita_sandbox import _UPLOAD_NOTE

    assert "local file remains readable" in _UPLOAD_NOTE
    assert "generate_image" in _UPLOAD_NOTE
    assert "cloudinary_video_edit" in _UPLOAD_NOTE
