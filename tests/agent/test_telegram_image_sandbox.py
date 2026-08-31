from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.loop import AgentLoop, TurnKind
from nanobot.agent.tools.novita_sandbox import _TELEGRAM_IMAGE_SCRIPT, NovitaSandboxTool
from nanobot.bus.events import InboundMessage


@pytest.mark.asyncio
async def test_telegram_images_are_preprocessed_and_removed_from_model_media(tmp_path, monkeypatch) -> None:
    image = tmp_path / "telegram-photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nminimal")
    analyze = AsyncMock(return_value="A blue bicycle is visible. Text: RIDE SAFE.")
    monkeypatch.setattr(NovitaSandboxTool, "analyze_telegram_images", analyze)

    ctx = SimpleNamespace(
        kind=TurnKind.USER,
        session_key="telegram:123",
        msg=InboundMessage(
            channel="telegram",
            sender_id="42",
            chat_id="123",
            content="What is in this image?",
            media=[str(image)],
            metadata={"message_id": 9},
        ),
    )

    await AgentLoop._prepare_telegram_images(AgentLoop.__new__(AgentLoop), ctx)

    analyze.assert_awaited_once_with(
        [str(image)],
        "What is in this image?",
        session_key="telegram:123",
    )
    assert ctx.msg.media == []
    assert "A blue bicycle is visible." in ctx.msg.content
    assert "What is in this image?" in ctx.msg.content
    assert "WAS received" in ctx.msg.content
    assert ctx.msg.metadata["telegram_images_via_novita_sandbox"] is True


@pytest.mark.asyncio
async def test_vps_telegram_images_use_ocr_only_and_remove_local_image_reference(tmp_path, monkeypatch) -> None:
    image = tmp_path / "telegram-photo.jpg"
    image.write_bytes(b"fake-image-bytes")
    analyze = AsyncMock(return_value="Recognized text: VPS IMAGE OCR 2026")
    monkeypatch.setattr(NovitaSandboxTool, "analyze_telegram_images", analyze)
    monkeypatch.setattr(NovitaSandboxTool, "backend_name", lambda self: "vps")

    ctx = SimpleNamespace(
        kind=TurnKind.USER,
        session_key="telegram:123",
        msg=InboundMessage(
            channel="telegram",
            sender_id="42",
            chat_id="123",
            content=f"[image: {image}]\nWhat is this?",
            media=[str(image)],
            metadata={"message_id": 9},
        ),
    )

    await AgentLoop._prepare_telegram_images(AgentLoop.__new__(AgentLoop), ctx)

    analyze.assert_awaited_once_with(
        [str(image)],
        f"[image: {image}]\nWhat is this?",
        session_key="telegram:123",
    )
    assert ctx.msg.media == []
    assert "VPS IMAGE OCR 2026" in ctx.msg.content
    assert "What is this?" in ctx.msg.content
    assert "WAS received" in ctx.msg.content
    assert f"[image: {image}]" not in ctx.msg.content
    assert ctx.msg.metadata["telegram_images_execution_backend"] == "vps"
    assert ctx.msg.metadata["telegram_images_via_novita_sandbox"] is False


@pytest.mark.asyncio
async def test_non_telegram_images_keep_existing_model_media_behavior(tmp_path, monkeypatch) -> None:
    image = tmp_path / "api-photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nminimal")
    analyze = AsyncMock()
    monkeypatch.setattr(NovitaSandboxTool, "analyze_telegram_images", analyze)

    ctx = SimpleNamespace(
        kind=TurnKind.USER,
        session_key="api:123",
        msg=InboundMessage(
            channel="api",
            sender_id="42",
            chat_id="123",
            content="Inspect this image",
            media=[str(image)],
            metadata={},
        ),
    )

    await AgentLoop._prepare_telegram_images(AgentLoop.__new__(AgentLoop), ctx)

    analyze.assert_not_awaited()
    assert ctx.msg.media == [str(image)]
    assert ctx.msg.content == "Inspect this image"


@pytest.mark.asyncio
async def test_telegram_non_image_attachments_are_not_sent_to_vision_preprocessor(tmp_path, monkeypatch) -> None:
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.7")
    analyze = AsyncMock()
    monkeypatch.setattr(NovitaSandboxTool, "analyze_telegram_images", analyze)

    ctx = SimpleNamespace(
        kind=TurnKind.USER,
        session_key="telegram:123",
        msg=InboundMessage(
            channel="telegram",
            sender_id="42",
            chat_id="123",
            content="Summarize this",
            media=[str(document)],
            metadata={},
        ),
    )

    await AgentLoop._prepare_telegram_images(AgentLoop.__new__(AgentLoop), ctx)

    analyze.assert_not_awaited()
    assert ctx.msg.media == [str(document)]
    assert ctx.msg.content == "Summarize this"


def test_tesseract_reader_script_recognizes_text(tmp_path) -> None:
    import json
    import subprocess
    import sys

    from PIL import Image, ImageDraw, ImageFont

    image = tmp_path / "ocr-text.png"
    canvas = Image.new("RGB", (1000, 260), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 72)
    draw.text((40, 80), "POWERX OCR 123", fill="black", font=font)
    canvas.save(image)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([str(image)]))
    script = tmp_path / "reader.py"
    script.write_text(_TELEGRAM_IMAGE_SCRIPT)

    result = subprocess.run(
        [sys.executable, str(script), str(manifest)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    content = payload["content"]
    assert "Tesseract OCR confidence:" in content
    assert "Recognized text:" in content
    assert "POWERX" in content
    assert "123" in content
    assert "api.novita.ai" not in script.read_text()


@pytest.mark.asyncio
async def test_analyzer_uploads_and_executes_tesseract_reader_end_to_end(tmp_path, monkeypatch) -> None:
    import json
    import shlex
    import subprocess
    import sys

    from PIL import Image, ImageDraw, ImageFont

    image = tmp_path / "photo.jpg"
    canvas = Image.new("RGB", (1000, 260), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 72)
    draw.text((40, 80), "SANDBOX TELEGRAM 456", fill="black", font=font)
    canvas.save(image)

    class FakeFiles:
        def __init__(self) -> None:
            self.files: dict[str, bytes] = {}

        def write(self, path: str, content: str | bytes) -> None:
            self.files[path] = content.encode() if isinstance(content, str) else content

    class FakeCommands:
        def __init__(self, files: FakeFiles) -> None:
            self.files = files

        def run(self, command: str, **kwargs):
            del kwargs
            if command.startswith("mkdir -p"):
                return SimpleNamespace(stdout="", stderr="", exit_code=0)
            if command.startswith("rm -f"):
                for path in shlex.split(command)[2:]:
                    self.files.files.pop(path, None)
                return SimpleNamespace(stdout="", stderr="", exit_code=0)
            tokens = shlex.split(command)
            script_path, manifest_path = tokens[-2:]
            import tempfile
            with tempfile.TemporaryDirectory() as workspace:
                root = __import__("pathlib").Path(workspace)
                for remote_path, data in self.files.files.items():
                    local_path = root / remote_path.removeprefix("/workspace/")
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    local_path.write_bytes(data)
                manifest = json.loads((root / manifest_path.removeprefix("/workspace/")).read_text())
                local_manifest = root / "manifest.json"
                local_manifest.write_text(json.dumps([
                    str(root / path.removeprefix("/workspace/")) for path in manifest
                ]))
                local_script = root / script_path.removeprefix("/workspace/")
                result = subprocess.run(
                    [sys.executable, str(local_script), str(local_manifest)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return SimpleNamespace(
                    stdout=result.stdout,
                    stderr=result.stderr,
                    exit_code=result.returncode,
                )

    class FakeSandbox:
        def __init__(self) -> None:
            self.files = FakeFiles()
            self.commands = FakeCommands(self.files)

    sandbox = FakeSandbox()
    monkeypatch.setenv("NOVITA_API_KEY", "test-key")
    monkeypatch.setattr(NovitaSandboxTool, "_get_or_create", lambda self, key: sandbox)

    result = await NovitaSandboxTool().analyze_telegram_images(
        [str(image)],
        "Read this image",
        session_key="telegram:e2e",
    )

    assert "Tesseract OCR confidence:" in result
    assert "Recognized text:" in result
    assert "SANDBOX" in result
    assert "456" in result
    assert "api.novita.ai" not in result
    assert not any("telegram-images" in path for path in sandbox.files.files)


def test_tesseract_reader_handles_multiple_images(tmp_path) -> None:
    import json
    import subprocess
    import sys

    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 64)
    images = []
    for name, text in (("first.png", "FIRST 111"), ("second.png", "SECOND 222")):
        image = Image.new("RGB", (900, 220), "white")
        ImageDraw.Draw(image).text((40, 65), text, fill="black", font=font)
        path = tmp_path / name
        image.save(path)
        images.append(path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([str(path) for path in images]))
    script = tmp_path / "reader.py"
    script.write_text(_TELEGRAM_IMAGE_SCRIPT)

    result = subprocess.run(
        [sys.executable, str(script), str(manifest)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    content = json.loads(result.stdout)["content"]
    assert "first.png" in content and "FIRST" in content and "111" in content
    assert "second.png" in content and "SECOND" in content and "222" in content


def test_tesseract_reader_handles_corrupt_image_without_crashing(tmp_path) -> None:
    import json
    import subprocess
    import sys

    image = tmp_path / "corrupt.png"
    image.write_bytes(b"not-an-image")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([str(image)]))
    script = tmp_path / "reader.py"
    script.write_text(_TELEGRAM_IMAGE_SCRIPT)

    result = subprocess.run(
        [sys.executable, str(script), str(manifest)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    content = json.loads(result.stdout)["content"]
    assert "corrupt.png" in content
    assert "could not read this image" in content


def test_tesseract_reader_reports_no_text_for_blank_image(tmp_path) -> None:
    import json
    import subprocess
    import sys

    from PIL import Image

    image = tmp_path / "blank.png"
    Image.new("RGB", (800, 400), "white").save(image)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([str(image)]))
    script = tmp_path / "reader.py"
    script.write_text(_TELEGRAM_IMAGE_SCRIPT)

    result = subprocess.run(
        [sys.executable, str(script), str(manifest)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    content = json.loads(result.stdout)["content"]
    assert "Tesseract detected no readable text." in content


def test_tesseract_reader_recognizes_faint_low_contrast_text(tmp_path) -> None:
    """Improved preprocessing must rescue faint/low-contrast text from 'no text'."""
    import json
    import subprocess
    import sys

    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1200, 400), "#f5f5f5")
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 54)
    ImageDraw.Draw(image).text((50, 160), "RETURNABLE ITEM", fill="#777777", font=font)
    image_path = tmp_path / "faint.png"
    image.save(image_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([str(image_path)]))
    script = tmp_path / "reader.py"
    script.write_text(_TELEGRAM_IMAGE_SCRIPT)

    result = subprocess.run(
        [sys.executable, str(script), str(manifest)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    content = json.loads(result.stdout)["content"]
    assert "Recognized text:" in content
    assert "RETURNABLE ITEM" in content


def test_tesseract_reader_recognizes_text_image_without_installing_dependencies(tmp_path) -> None:
    import json
    import os
    import subprocess
    import sys

    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1000, 300), "white")
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 64)
    ImageDraw.Draw(image).text((40, 90), "VPS OCR 123", fill="black", font=font)
    image_path = tmp_path / "text.png"
    image.save(image_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([str(image_path)]))
    script = tmp_path / "reader.py"
    script.write_text(_TELEGRAM_IMAGE_SCRIPT)
    env = dict(os.environ)
    env["NANOBOT_OCR_ALLOW_INSTALL"] = "0"
    env["NANOBOT_OCR_ALLOW_PILLOW_INSTALL"] = "0"

    result = subprocess.run(
        [sys.executable, str(script), str(manifest)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    content = json.loads(result.stdout)["content"]
    assert "VPS OCR 123" in content


def test_tesseract_reader_handles_missing_binary_gracefully(tmp_path) -> None:
    import json
    import os
    import subprocess
    import sys

    from PIL import Image

    image = tmp_path / "no-binary.png"
    Image.new("RGB", (200, 100), "white").save(image)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([str(image)]))
    script = tmp_path / "reader.py"
    script.write_text(_TELEGRAM_IMAGE_SCRIPT)
    env = dict(os.environ)
    env["PATH"] = "/definitely/no/tesseract"

    result = subprocess.run(
        [sys.executable, str(script), str(manifest)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    content = json.loads(result.stdout)["content"]
    assert "Tesseract is unavailable on this execution backend" in content


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", ["/workspace", "/srv/nanobot"])
async def test_vps_ocr_uploads_telegram_image_bytes_and_returns_result(
    tmp_path,
    monkeypatch,
    workspace: str,
) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"telegram-image-bytes")
    execution = SimpleNamespace(
        backend="vps",
        vps=SimpleNamespace(host="vps.example.test", workspace_dir=workspace),
    )
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))
    tmpfile_uploads: list[tuple[str, bytes]] = []
    uploads: list[tuple[str, str, bytes]] = []
    commands: list[str] = []

    async def fake_run(self, command: str, *, timeout: int = 120, cwd: str | None = None) -> str:
        del self, timeout, cwd
        commands.append(command)
        if "command -v tesseract" in command:
            return "READY"
        if command.startswith("env "):
            return '{"content":"File: photo.jpg\\nRecognized text:\\nVPS OCR OK"}'
        return ""

    async def fake_write(self, path: str, content: str) -> None:
        del path, content

    async def fake_tmpfile_upload(data: bytes, *, filename: str, content_type: str | None = None) -> dict[str, str]:
        del content_type
        tmpfile_uploads.append((filename, data))
        return {
            "url": "https://tmpfiles.org/12345/photo.jpg",
            "download_url": "https://tmpfiles.org/dl/12345/photo.jpg",
        }

    async def fake_upload(self, source: str, remote_path: str, data: bytes) -> None:
        del self
        uploads.append((source, remote_path, data))

    monkeypatch.setattr("nanobot.agent.tools.novita_sandbox.upload_tmpfile_bytes", fake_tmpfile_upload)
    monkeypatch.setattr("nanobot.agent.tools.novita_sandbox.VPSExecutionBackend.run", fake_run)
    monkeypatch.setattr("nanobot.agent.tools.novita_sandbox.VPSExecutionBackend.write", fake_write)
    monkeypatch.setattr("nanobot.agent.tools.novita_sandbox.VPSExecutionBackend.upload", fake_upload)

    result = await NovitaSandboxTool().analyze_telegram_images(
        [str(image)],
        "Read this image",
        session_key="telegram:vps-ocr",
    )

    assert "VPS OCR OK" in result
    # tmpfiles.org relay is skipped in VPS mode — SFTP delivers
    # the bytes directly.
    assert tmpfile_uploads == []
    assert uploads == [("telegram", uploads[0][1], b"telegram-image-bytes")]
    assert uploads[0][1].startswith(f"{workspace}/telegram-images/")
    assert any(
        "NANOBOT_OCR_ALLOW_INSTALL=0" in command
        and "NANOBOT_OCR_ALLOW_PILLOW_INSTALL=0" in command
        for command in commands
    )


@pytest.mark.asyncio
async def test_vps_attachment_accepts_active_config_media_root_when_env_root_differs(tmp_path, monkeypatch) -> None:
    canonical_root = tmp_path / "canonical-data"
    env_root = tmp_path / "different-data"
    document = canonical_root / "media" / "telegram" / "report.pdf"
    document.parent.mkdir(parents=True)
    document.write_bytes(b"%PDF-1.7")
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(env_root))
    monkeypatch.setattr("nanobot.agent.tools.novita_sandbox.get_data_dir", lambda: canonical_root)
    execution = SimpleNamespace(
        backend="vps",
        vps=SimpleNamespace(host="vps.example.test", workspace_dir="/workspace"),
    )
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))
    tmpfile_upload = AsyncMock(return_value={
        "url": "https://tmpfiles.org/12345/report.pdf",
        "download_url": "https://tmpfiles.org/dl/12345/report.pdf",
    })
    upload = AsyncMock()
    monkeypatch.setattr("nanobot.agent.tools.novita_sandbox.upload_tmpfile_path", tmpfile_upload)
    monkeypatch.setattr("nanobot.agent.tools.novita_sandbox.VPSExecutionBackend.upload", upload)
    staged = await NovitaSandboxTool().stage_telegram_attachments(
        [str(document)],
        session_key="telegram:canonical-root",
    )
    assert staged and staged[0][0] == str(document.resolve())
    # tmpfiles.org relay is skipped — SFTP upload is used directly.
    tmpfile_upload.assert_not_awaited()
    upload.assert_awaited_once()


@pytest.mark.asyncio
async def test_vps_confirmed_attachment_is_staged_before_model_turn(tmp_path, monkeypatch) -> None:
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.7")
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(tmp_path))
    execution = SimpleNamespace(
        backend="vps",
        vps=SimpleNamespace(host="vps.example.test", workspace_dir="/workspace"),
    )
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))
    tmpfile_upload = AsyncMock(return_value={
        "url": "https://tmpfiles.org/12345/report.pdf",
        "download_url": "https://tmpfiles.org/dl/12345/report.pdf",
    })
    upload = AsyncMock()
    monkeypatch.setattr("nanobot.agent.tools.novita_sandbox.upload_tmpfile_path", tmpfile_upload)
    monkeypatch.setattr("nanobot.agent.tools.novita_sandbox.VPSExecutionBackend.upload", upload)

    staged = await NovitaSandboxTool().stage_telegram_attachments(
        [str(document)],
        session_key="telegram:staging",
    )

    assert staged and staged[0][0] == str(document.resolve())
    assert staged[0][1].startswith("/workspace/telegram-attachments/")
    # tmpfiles.org relay is skipped in VPS mode.
    tmpfile_upload.assert_not_awaited()
    upload.assert_awaited_once()


@pytest.mark.asyncio
async def test_vps_image_turn_removes_original_image_after_ocr(tmp_path, monkeypatch) -> None:
    image = tmp_path / "telegram-photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nminimal")
    execution = SimpleNamespace(
        backend="vps",
        vps=SimpleNamespace(host="vps.example.test", workspace_dir="/workspace"),
    )
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))
    stage = AsyncMock(return_value=[(str(image), "/workspace/telegram-attachments/photo.png")])
    analyze = AsyncMock(return_value="Recognized text: VPS IMAGE 123")
    monkeypatch.setattr(NovitaSandboxTool, "stage_telegram_attachments", stage)
    monkeypatch.setattr(NovitaSandboxTool, "analyze_telegram_images", analyze)

    ctx = SimpleNamespace(
        kind=TurnKind.USER,
        session_key="telegram:123",
        msg=InboundMessage(
            channel="telegram",
            sender_id="42",
            chat_id="123",
            content="Read this image",
            media=[str(image)],
            metadata={},
        ),
    )
    loop = AgentLoop.__new__(AgentLoop)

    await loop._stage_vps_telegram_attachments(ctx)
    await loop._prepare_telegram_images(ctx)

    stage.assert_not_awaited()
    analyze.assert_awaited_once()
    assert ctx.msg.media == []
    assert "/workspace/telegram-attachments/photo.png" not in ctx.msg.content
    assert str(image) not in ctx.msg.content
    assert "VPS IMAGE 123" in ctx.msg.content
    assert ctx.msg.metadata["telegram_images_execution_backend"] == "vps"


@pytest.mark.asyncio
async def test_vps_mixed_turn_stages_only_non_image_attachments(tmp_path, monkeypatch) -> None:
    image = tmp_path / "telegram-photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nminimal")
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.7")
    execution = SimpleNamespace(
        backend="vps",
        vps=SimpleNamespace(host="vps.example.test", workspace_dir="/workspace"),
    )
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))
    stage = AsyncMock(return_value=[(str(document), "/workspace/telegram-attachments/report.pdf")])
    analyze = AsyncMock(return_value="Recognized text: MIXED TURN")
    monkeypatch.setattr(NovitaSandboxTool, "stage_telegram_attachments", stage)
    monkeypatch.setattr(NovitaSandboxTool, "analyze_telegram_images", analyze)

    ctx = SimpleNamespace(
        kind=TurnKind.USER,
        session_key="telegram:mixed",
        msg=InboundMessage(
            channel="telegram",
            sender_id="42",
            chat_id="mixed",
            content="Process both files",
            media=[str(image), str(document)],
            metadata={},
        ),
    )

    loop = AgentLoop.__new__(AgentLoop)
    await loop._stage_vps_telegram_attachments(ctx)
    await loop._prepare_telegram_images(ctx)

    stage.assert_awaited_once_with([str(document)], session_key="telegram:mixed")
    analyze.assert_awaited_once()
    analysis_prompt = analyze.await_args.args[1]
    assert "Process both files" in analysis_prompt
    assert "/workspace/telegram-attachments/report.pdf" in analysis_prompt
    assert str(image) not in analysis_prompt
    assert "/workspace/telegram-attachments/report.pdf" in ctx.msg.content
    assert str(image) not in ctx.msg.content
    assert ctx.msg.media == []
    assert "MIXED TURN" in ctx.msg.content


@pytest.mark.asyncio
async def test_vps_staging_failure_preserves_attachment_context(tmp_path, monkeypatch) -> None:
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.7")
    execution = SimpleNamespace(
        backend="vps",
        vps=SimpleNamespace(host="vps.example.test", workspace_dir="/workspace"),
    )
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))
    stage = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(NovitaSandboxTool, "stage_telegram_attachments", stage)

    ctx = SimpleNamespace(
        kind=TurnKind.USER,
        session_key="telegram:123",
        msg=InboundMessage(
            channel="telegram",
            sender_id="42",
            chat_id="123",
            content="Summarize this",
            media=[str(document)],
            metadata={},
        ),
    )

    await AgentLoop._stage_vps_telegram_attachments(AgentLoop.__new__(AgentLoop), ctx)

    assert str(document) in ctx.msg.content
    assert "TimeoutError" in ctx.msg.content
    assert ctx.msg.metadata["telegram_vps_attachment_stage_error"] == "TimeoutError"


@pytest.mark.asyncio
async def test_missing_sandbox_key_fails_closed_without_uploading(tmp_path, monkeypatch) -> None:
    image = tmp_path / "telegram-photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nminimal")
    monkeypatch.delenv("NOVITA_API_KEY", raising=False)
    get_or_create = AsyncMock(side_effect=AssertionError("sandbox must not be created"))
    monkeypatch.setattr(NovitaSandboxTool, "_get_or_create", get_or_create)

    result = await NovitaSandboxTool().analyze_telegram_images(
        [str(image)],
        "Read this image",
        session_key="telegram:missing-key",
    )

    assert "not configured" in result
    get_or_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_analyzer_retries_once_with_fresh_sandbox_after_command_failure(tmp_path, monkeypatch) -> None:
    from PIL import Image

    image = tmp_path / "retry.png"
    Image.new("RGB", (32, 32), "white").save(image)

    class FakeFiles:
        def write(self, path: str, content: str | bytes) -> None:
            del path, content

    class FakeCommands:
        def __init__(self, fail_ocr: bool) -> None:
            self.fail_ocr = fail_ocr

        def run(self, command: str, **kwargs):
            del kwargs
            if command.startswith("rm -f") or command.startswith("mkdir -p"):
                return SimpleNamespace(stdout="", stderr="", exit_code=0)
            if self.fail_ocr:
                self.fail_ocr = False
                raise RuntimeError("stale sandbox command failed")
            return SimpleNamespace(
                stdout='{"content":"File: retry.png\\nRecognized text:\\nRETRY SUCCESS"}',
                stderr="",
                exit_code=0,
            )

    class FakeSandbox:
        def __init__(self, fail_ocr: bool) -> None:
            self.files = FakeFiles()
            self.commands = FakeCommands(fail_ocr)
            self.killed = False

        def kill(self) -> None:
            self.killed = True

    first = FakeSandbox(fail_ocr=True)
    second = FakeSandbox(fail_ocr=False)
    sandboxes = iter((first, second))

    def get_or_create(self, key):
        del self, key
        return next(sandboxes)

    monkeypatch.setattr(NovitaSandboxTool, "_get_or_create", get_or_create)
    monkeypatch.setenv("NOVITA_API_KEY", "test-key")

    result = await NovitaSandboxTool().analyze_telegram_images(
        [str(image)],
        "read this",
        session_key="telegram:retry",
    )

    assert "RETRY SUCCESS" in result
    assert first.killed is True


@pytest.mark.asyncio
async def test_analyzer_accepts_json_after_sandbox_bootstrap_noise(tmp_path, monkeypatch) -> None:
    from PIL import Image

    image = tmp_path / "noisy-bootstrap.png"
    Image.new("RGB", (32, 32), "white").save(image)

    class FakeFiles:
        def write(self, path: str, content: str | bytes) -> None:
            del path, content

    class FakeCommands:
        def run(self, command: str, **kwargs):
            del kwargs
            if command.startswith("mkdir -p") or command.startswith("rm -f"):
                return SimpleNamespace(stdout="", stderr="", exit_code=0)
            return SimpleNamespace(
                stdout="WARNING: bootstrap output\n{\"content\":\"File: noisy-bootstrap.png\\nRecognized text:\\nNOISY JSON OK\"}",
                stderr="",
                exit_code=0,
            )

    class FakeSandbox:
        def __init__(self) -> None:
            self.files = FakeFiles()
            self.commands = FakeCommands()

    sandbox = FakeSandbox()

    def get_or_create(self, key):
        del self, key
        return sandbox

    monkeypatch.setattr(NovitaSandboxTool, "_get_or_create", get_or_create)
    monkeypatch.setenv("NOVITA_API_KEY", "test-key")

    result = await NovitaSandboxTool().analyze_telegram_images(
        [str(image)],
        "read this",
        session_key="telegram:noisy-json",
    )

    assert "NOISY JSON OK" in result


def test_tesseract_reader_exports_library_path_for_bundled_binary(tmp_path, monkeypatch) -> None:
    import json
    import os
    import subprocess
    import sys

    from PIL import Image

    extract_root = tmp_path / "ocr_extract"
    binary_dir = extract_root / "usr" / "bin"
    library_dir = extract_root / "usr" / "lib"
    binary_dir.mkdir(parents=True)
    library_dir.mkdir(parents=True)
    binary = binary_dir / "tesseract"
    binary.write_text(
        "#!/bin/sh\n"
        f"case \":${{LD_LIBRARY_PATH:-}}:\" in *:\"{library_dir}\":*) ;; "
        "*) echo 'libtesseract.so.5 not found' >&2; exit 127 ;; esac\n"
        "printf 'level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\twidth\\theight\\tconf\\ttext\\n'\n"
        "printf '5\\t1\\t1\\t1\\t1\\t1\\t0\\t0\\t100\\t40\\t95.0\\tPOWERX\\n'\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary_dir))

    image = tmp_path / "ocr-text.png"
    Image.new("RGB", (80, 40), "white").save(image)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([str(image)]), encoding="utf-8")
    script = tmp_path / "reader.py"
    script.write_text(_TELEGRAM_IMAGE_SCRIPT, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script), str(manifest)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": str(binary_dir)},
    )

    assert result.returncode == 0, result.stderr
    assert "POWERX" in json.loads(result.stdout)["content"]


def test_telegram_ocr_turn_strips_all_image_block_shapes_before_provider() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,redacted"}},
                {"type": "input_image", "image_url": "https://example.invalid/image"},
                {"type": "text", "text": "OCR result"},
            ],
        }
    ]

    AgentLoop._strip_telegram_image_blocks(messages)

    content = messages[0]["content"]
    assert all(block.get("type") != "image_url" for block in content)
    assert all(block.get("type") != "input_image" for block in content)
    assert any("raw image content was not sent" in block.get("text", "") for block in content)


# --- Direct-fetch (tgurl::) token paths: keep file bytes off Render egress ---


def test_is_image_file_recognises_tgurl_image_token() -> None:
    from nanobot.utils.document import is_image_file

    token = "tgurl::https://api.telegram.org/file/bot123/documents/photo_2235492.png::photo_2235492.png"
    assert is_image_file(token) is True
    pdf_token = "tgurl::https://api.telegram.org/file/bot123/documents/report_dc.pdf::report_dc.pdf"
    assert is_image_file(pdf_token) is False


def test_telegram_url_token_round_trip() -> None:
    from nanobot.agent.tools.novita_sandbox import (
        _decode_telegram_url_token,
        _encode_telegram_url_token,
        _is_telegram_url_token,
    )

    url = "https://api.telegram.org/file/bot123/documents/report%20dc.pdf"
    token = _encode_telegram_url_token(url, "report dc.pdf")
    assert _is_telegram_url_token(token) is True
    decoded = _decode_telegram_url_token(token)
    assert decoded == (url, "", "report_dc.pdf")


@pytest.mark.asyncio
async def test_stage_telegram_attachments_token_fetches_directly_from_telegram(monkeypatch) -> None:
    import nanobot.agent.tools.vps_backend as vps_backend_mod

    from nanobot.agent.tools.novita_sandbox import (
        _encode_telegram_url_token,
        NovitaSandboxTool,
    )

    execution = SimpleNamespace(
        backend="vps",
        vps=SimpleNamespace(host="vps.example.test", workspace_dir="/workspace"),
    )
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))
    fetched: list[tuple[str, str]] = []
    upload = AsyncMock()

    async def fake_fetch_telegram_url(self, url: str, remote_path: str) -> str:
        del self
        fetched.append((url, remote_path))
        return remote_path

    monkeypatch.setattr(
        vps_backend_mod.VPSExecutionBackend,
        "fetch_telegram_url",
        fake_fetch_telegram_url,
    )
    monkeypatch.setattr(
        vps_backend_mod.VPSExecutionBackend, "upload", upload
    )

    url = "https://api.telegram.org/file/bot123/documents/report.pdf"
    token = _encode_telegram_url_token(url, "report.pdf")
    staged = await NovitaSandboxTool().stage_telegram_attachments(
        [token],
        session_key="telegram:direct-stage",
    )

    assert len(staged) == 1
    assert staged[0][0] == url
    assert staged[0][1].startswith("/workspace/telegram-attachments/")
    assert len(fetched) == 1 and fetched[0][0] == url
    upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_analyze_telegram_images_vps_from_urls_fetches_directly(monkeypatch) -> None:
    import nanobot.agent.tools.vps_backend as vps_backend_mod

    from nanobot.agent.tools.novita_sandbox import (
        _encode_telegram_url_token,
        NovitaSandboxTool,
    )

    execution = SimpleNamespace(
        backend="vps",
        vps=SimpleNamespace(host="vps.example.test", workspace_dir="/workspace"),
    )
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))
    fetched: list[str] = []

    async def fake_run(self, command: str, *, timeout: int = 120, cwd: str | None = None) -> str:
        del self, timeout, cwd
        if "command -v tesseract" in command:
            return "READY"
        if command.startswith("env "):
            return '{"content":"File: photo.jpg\\nRecognized text:\\nDIRECT VPS OCR OK"}'
        return ""

    async def fake_write(self, path: str, content: str) -> None:
        del path, content

    async def fake_fetch_telegram_url(self, url: str, remote_path: str) -> str:
        del self
        fetched.append(remote_path)
        return remote_path

    monkeypatch.setattr(vps_backend_mod.VPSExecutionBackend, "run", fake_run)
    monkeypatch.setattr(vps_backend_mod.VPSExecutionBackend, "write", fake_write)
    monkeypatch.setattr(
        vps_backend_mod.VPSExecutionBackend, "fetch_telegram_url", fake_fetch_telegram_url
    )

    url = "https://api.telegram.org/file/bot123/photos/photo_1.jpg"
    token = _encode_telegram_url_token(url, "photo_1.jpg")
    result = await NovitaSandboxTool().analyze_telegram_images(
        [token],
        "Read this",
        session_key="telegram:direct-vps-ocr",
    )

    assert "DIRECT VPS OCR OK" in result
    assert len(fetched) == 1
    assert fetched[0].startswith("/workspace/telegram-images/")


@pytest.mark.asyncio
async def test_novita_direct_fetch_failure_is_isolated_and_reported_honestly(monkeypatch) -> None:
    """A failing api.telegram.org direct-fetch must not abort the batch or
    silently drop the image. It must yield an honest 'image received, bytes
    not retrievable' message instead of a bare OCR-failure string so the model
    never fabricates 'the image file could not be located'."""
    from nanobot.agent.tools.novita_sandbox import (
        _encode_telegram_url_token,
        NovitaSandboxTool,
    )

    class FakeFiles:
        def __init__(self) -> None:
            self.files: dict[str, bytes] = {}

        def write(self, path: str, content: str | bytes) -> None:
            del path
            del content

    class FakeCommands:
        def __init__(self) -> None:
            self.fetched: list[tuple[str, str]] = []

        def run(self, command: str, **kwargs):
            cwd = kwargs.get("cwd")
            if command.startswith("mkdir -p"):
                return SimpleNamespace(stdout="", stderr="", exit_code=0)
            if command.startswith("rm -f"):
                return SimpleNamespace(stdout="", stderr="", exit_code=0)
            if command.startswith("curl"):
                # Simulate a failed Telegram direct-fetch: the sandbox command
                # raises (CommandExitException in production).
                raise RuntimeError("sandbox curl failed")
            del cwd
            return SimpleNamespace(
                stdout="",
                stderr="",
                exit_code=1,
            )

    class FakeSandbox:
        def __init__(self) -> None:
            self.files = FakeFiles()
            self.commands = FakeCommands()

    sandbox = FakeSandbox()

    def get_or_create(self, key):
        del self, key
        return sandbox

    monkeypatch.setenv("NOVITA_API_KEY", "test-key")
    monkeypatch.setattr(NovitaSandboxTool, "_get_or_create", get_or_create)

    bad = _encode_telegram_url_token(
        "https://api.telegram.org/file/bot123/documents/gone.png",
        "gone.png",
    )
    result = await NovitaSandboxTool().analyze_telegram_images(
        [bad],
        "Read this",
        session_key="telegram:direct-fail",
    )

    assert "bytes from Telegram" in result
    assert "gone.png" in result
    assert "Tesseract OCR failed" not in result


@pytest.mark.asyncio
async def test_novita_direct_fetch_success_still_runs_ocr_and_populates_manifest(monkeypatch) -> None:
    """A successful direct-fetch must write the file path into the manifest and
    feed it through the OCR reader (happy path preserved)."""
    import json

    from nanobot.agent.tools.novita_sandbox import (
        _encode_telegram_url_token,
        NovitaSandboxTool,
    )

    class FakeFiles:
        def __init__(self) -> None:
            self.files: dict[str, bytes] = {}
            self.manifest: dict = {}

        def write(self, path: str, content: str | bytes) -> None:
            if path.endswith("telegram_image_manifest.json"):
                self.manifest = json.loads(content if isinstance(content, str) else content.decode())
            else:
                self.files[path] = content.encode() if isinstance(content, str) else content

    class FakeCommands:
        def __init__(self, files: FakeFiles) -> None:
            self.files = files

        def run(self, command: str, **kwargs):
            del kwargs
            if command.startswith("mkdir -p"):
                return SimpleNamespace(stdout="", stderr="", exit_code=0)
            if command.startswith("rm -f"):
                return SimpleNamespace(stdout="", stderr="", exit_code=0)
            if command.startswith("curl"):
                return SimpleNamespace(stdout="", stderr="", exit_code=0)
            # OCR script invocation
            return SimpleNamespace(
                stdout='{"content":"File: photo.png\\nRecognized text:\\nOK DIRECT"}',
                stderr="",
                exit_code=0,
            )

    class FakeSandbox:
        def __init__(self) -> None:
            self.files = FakeFiles()
            self.commands = FakeCommands(self.files)

    sandbox = FakeSandbox()

    def get_or_create(self, key):
        del self, key
        return sandbox

    monkeypatch.setenv("NOVITA_API_KEY", "test-key")
    monkeypatch.setattr(NovitaSandboxTool, "_get_or_create", get_or_create)

    token = _encode_telegram_url_token(
        "https://api.telegram.org/file/bot123/photos/photo.png",
        "photo.png",
    )
    result = await NovitaSandboxTool().analyze_telegram_images(
        [token],
        "Read this",
        session_key="telegram:direct-ok",
    )

    assert "OK DIRECT" in result
    assert len(sandbox.files.manifest) == 1
    assert sandbox.files.manifest[0].startswith("/workspace/telegram-images/")


@pytest.mark.asyncio
async def test_novita_stage_telegram_attachments_local_path_skips_relay(monkeypatch, tmp_path) -> None:
    import nanobot.agent.tools.vps_backend as vps_backend_mod

    from nanobot.agent.tools.novita_sandbox import NovitaSandboxTool

    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.7")
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(tmp_path))
    execution = SimpleNamespace(
        backend="vps",
        vps=SimpleNamespace(host="vps.example.test", workspace_dir="/workspace"),
    )
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))
    tmpfile_upload = AsyncMock()
    upload = AsyncMock()
    monkeypatch.setattr("nanobot.agent.tools.novita_sandbox.upload_tmpfile_path", tmpfile_upload)
    monkeypatch.setattr(vps_backend_mod.VPSExecutionBackend, "upload", upload)

    staged = await NovitaSandboxTool().stage_telegram_attachments(
        [str(document.resolve())],
        session_key="telegram:local-stage",
    )

    assert len(staged) == 1
    assert staged[0][1].startswith("/workspace/telegram-attachments/")
    tmpfile_upload.assert_not_awaited()
    upload.assert_awaited_once()
