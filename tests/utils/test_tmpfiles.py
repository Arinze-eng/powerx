import json

import pytest

from nanobot.utils import tmpfiles


class _FakeResponse:
    status = 201

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def text(self):
        return json.dumps(
            {
                "status": "success",
                "data": {"url": "https://tmpfiles.org/12345/test.jpg"},
            }
        )


class _FakeSession:
    def __init__(self, *, timeout):
        self.timeout = timeout
        self.url = None
        self.form = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def post(self, url, *, data):
        self.url = url
        self.form = data
        return _FakeResponse()


@pytest.mark.asyncio
async def test_upload_bytes_uses_tmpfiles_and_returns_direct_download_url(monkeypatch):
    sessions = []

    def fake_session(*, timeout):
        session = _FakeSession(timeout=timeout)
        sessions.append(session)
        return session

    monkeypatch.setattr(tmpfiles.aiohttp, "ClientSession", fake_session)
    result = await tmpfiles.upload_bytes(b"safe test bytes", filename="test.jpg", content_type="image/jpeg")

    assert result == {
        "url": "https://tmpfiles.org/12345/test.jpg",
        "download_url": "https://tmpfiles.org/dl/12345/test.jpg",
    }
    assert sessions[0].url == tmpfiles.TMPFILES_UPLOAD_URL
    assert sessions[0].form is not None


def test_tmpfiles_rejects_non_tmpfiles_urls():
    with pytest.raises(tmpfiles.TmpfilesError):
        tmpfiles._public_url("https://example.com/file.jpg")


@pytest.mark.asyncio
async def test_upload_path_rejects_empty_file(tmp_path):
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    with pytest.raises(tmpfiles.TmpfilesError, match="empty"):
        await tmpfiles.upload_path(empty)


def test_tmpfiles_slug_download_link_uses_public_page_url():
    page_url = "https://tmpfiles.org/w6w9GPTSHr7R/test.jpg"
    assert tmpfiles._download_url(page_url) == page_url
