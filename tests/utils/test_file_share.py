import pytest

from nanobot.utils import file_share


@pytest.mark.asyncio
async def test_small_file_routes_to_tmpfiles(monkeypatch):
    calls = {}

    async def fake_tmpfiles(data, *, filename, content_type, timeout_seconds):
        calls["tmpfiles"] = (len(data), filename)
        return {"url": "https://tmpfiles.org/dl/x", "page_url": "https://tmpfiles.org/x", "host": "tmpfiles"}

    async def fake_catbox(*a, **k):  # pragma: no cover - should not be called
        raise AssertionError("catbox should not be used for small files")

    monkeypatch.setattr(file_share, "_upload_tmpfiles", fake_tmpfiles)
    monkeypatch.setattr(file_share, "_upload_catbox", fake_catbox)

    result = await file_share.upload_artifact_bytes(b"hello world", filename="a.txt")
    assert result["host"] == "tmpfiles"
    assert calls["tmpfiles"][1] == "a.txt"


@pytest.mark.asyncio
async def test_large_file_routes_to_catbox(monkeypatch):
    calls = {}

    async def fake_tmpfiles(*a, **k):  # pragma: no cover
        raise AssertionError("tmpfiles should not be used for >100MB files")

    async def fake_catbox(data, *, filename, content_type, timeout_seconds):
        calls["catbox"] = len(data)
        return {"url": "https://files.catbox.moe/big.zip", "page_url": "https://files.catbox.moe/big.zip", "host": "catbox"}

    monkeypatch.setattr(file_share, "_upload_tmpfiles", fake_tmpfiles)
    monkeypatch.setattr(file_share, "_upload_catbox", fake_catbox)

    big = b"x" * (101 * 1024 * 1024)
    result = await file_share.upload_artifact_bytes(big, filename="big.zip")
    assert result["host"] == "catbox"
    assert calls["catbox"] == len(big)


@pytest.mark.asyncio
async def test_midsize_falls_back_from_tmpfiles_to_catbox(monkeypatch):
    from nanobot.utils.tmpfiles import TmpfilesError

    async def failing_tmpfiles(*a, **k):
        raise TmpfilesError("too big")

    async def fake_catbox(data, *, filename, content_type, timeout_seconds):
        return {"url": "https://files.catbox.moe/m.bin", "page_url": "https://files.catbox.moe/m.bin", "host": "catbox"}

    monkeypatch.setattr(file_share, "_upload_tmpfiles", failing_tmpfiles)
    monkeypatch.setattr(file_share, "_upload_catbox", fake_catbox)

    mid = b"y" * (60 * 1024 * 1024)  # >50MB but <100MB
    result = await file_share.upload_artifact_bytes(mid, filename="m.bin")
    assert result["host"] == "catbox"


@pytest.mark.asyncio
async def test_empty_file_raises():
    with pytest.raises(file_share.FileShareError):
        await file_share.upload_artifact_bytes(b"", filename="empty.bin")
