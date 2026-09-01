"""Tests for the GoFile share-resolution and download helpers."""

from __future__ import annotations

import pytest

from nanobot.utils import gofile


class _FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, content_type=None):
        return self.payload


class _FakeSession:
    def __init__(self, *, timeout, post_payload, get_payload):
        self.timeout = timeout
        self.post_url = None
        self.get_url = None
        self.post_count = 0
        self.get_count = 0
        self._post_payload = post_payload
        self._get_payload = get_payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def post(self, url, headers=None, **kwargs):
        self.post_count += 1
        self.post_url = url
        return _FakeResponse(self._post_payload)

    def get(self, url, headers=None, **kwargs):
        self.get_count += 1
        self.get_url = url
        return _FakeResponse(self._get_payload)


def test_is_gofile_url_accepts_share_and_cdn_hosts():
    assert gofile.is_gofile_url("https://gofile.io/d/1h7drnFe")
    assert gofile.is_gofile_url("https://cdn.gofile.io/dl/file.bin")
    assert gofile.is_gofile_url("https://content5328.gofile.io/download/file.bin")
    # Host detection is scheme-agnostic; scheme validation happens in the callers.
    assert gofile.is_gofile_url("http://gofile.io/d/abc123")
    assert not gofile.is_gofile_url("https://example.com/d/abc123")


def test_extract_gofile_code_parses_share_link():
    assert gofile.extract_gofile_code("https://gofile.io/d/1h7drnFe") == "1h7drnFe"
    assert gofile.extract_gofile_code("gofile.io/d/abc123") == "abc123"
    assert gofile.extract_gofile_code("https://gofile.io/d/_aB9-7xYz23") == "_aB9-7xYz23"


def test_extract_gofile_code_rejects_invalid_links():
    with pytest.raises(gofile.GoFileError, match="GoFile link must look like"):
        gofile.extract_gofile_code("https://gofile.io/random/page")
    with pytest.raises(gofile.GoFileError, match="invalid"):
        gofile.extract_gofile_code("https://gofile.io/d/short")


def test_website_token_is_deterministic_sha256():
    first = gofile._website_token("account-token", 0)
    second = gofile._website_token("account-token", 0)
    assert first == second
    assert len(first) == 64
    # A different offset changes the token input (-> different hash), which is
    # what GoFile uses to tolerate clock drift between retries.
    assert gofile._website_token("account-token", 0) != gofile._website_token("account-token", -1)


@pytest.mark.asyncio
async def test_resolve_gofile_download_creates_guest_and_returns_direct_links(monkeypatch):
    session = _FakeSession(
        timeout=object(),
        post_payload={"status": "ok", "data": {"token": "guest-token-123"}},
        get_payload={
            "status": "ok",
            "data": {
                "children": {
                    "file1": {
                        "type": "file",
                        "name": "report.pdf",
                        "link": "https://cdn.gofile.io/dl/report.pdf",
                        "size": 12345,
                    }
                }
            },
        },
    )

    def fake_session(*, timeout):
        session.timeout = timeout
        return session

    monkeypatch.setattr(gofile.aiohttp, "ClientSession", fake_session)
    items = await gofile.resolve_gofile_download(
        "https://gofile.io/d/1h7drnFe", timeout_seconds=90
    )

    assert session.post_count == 1
    assert session.get_count == 1
    assert session.post_url == f"{gofile.GOFILE_API}/accounts"
    assert items == [
        {
            "name": "report.pdf",
            "link": "https://cdn.gofile.io/dl/report.pdf",
            "size": "12345",
            "token": "guest-token-123",
        }
    ]


def test_gofile_file_headers_use_accounttoken_cookie_and_range():
    headers = gofile.gofile_file_headers("guest-token-123")
    assert headers["Cookie"] == "accountToken=guest-token-123"
    assert headers["Range"] == "bytes=0-"
    assert headers["Referer"] == "https://gofile.io/"
    assert "X-Website-Token" in headers
    assert len(headers["X-Website-Token"]) == 64
    assert headers["User-Agent"] == gofile.USER_AGENT


@pytest.mark.asyncio
async def test_resolve_gofile_download_retries_then_raises_on_persistent_rate_limit(monkeypatch):
    sleep_count = {"n": 0}

    class RateLimitSession(_FakeSession):
        def get(self, url, headers=None, **kwargs):
            self.get_count += 1
            self.get_url = url
            # Both of the two token-offset retries hit the rate limit.
            return _FakeResponse({"status": "error-rateLimit"})

    session = RateLimitSession(
        timeout=object(),
        post_payload={"status": "ok", "data": {"token": "guest-token-123"}},
        get_payload={"status": "ok", "data": {}},
    )

    def fake_session(*, timeout):
        session.timeout = timeout
        return session

    async def fake_sleep(_seconds):
        sleep_count["n"] += 1

    monkeypatch.setattr(gofile.aiohttp, "ClientSession", fake_session)
    monkeypatch.setattr(gofile.asyncio, "sleep", fake_sleep)
    with pytest.raises(gofile.GoFileError, match="error-rateLimit"):
        await gofile.resolve_gofile_download("https://gofile.io/d/1h7drnFe")
    # Two API attempts (offset 0 and -1), with a rate-limit backoff sleep after each.
    assert session.get_count == 2
    assert sleep_count["n"] == 2


@pytest.mark.asyncio
async def test_resolve_gofile_download_rejects_host_without_files(monkeypatch):
    session = _FakeSession(
        timeout=object(),
        post_payload={"status": "ok", "data": {"token": "guest-token-123"}},
        get_payload={"status": "ok", "data": {"children": {}}},
    )

    def fake_session(*, timeout):
        session.timeout = timeout
        return session

    monkeypatch.setattr(gofile.aiohttp, "ClientSession", fake_session)
    with pytest.raises(gofile.GoFileError, match="no downloadable files"):
        await gofile.resolve_gofile_download("https://gofile.io/d/1h7drnFe")
