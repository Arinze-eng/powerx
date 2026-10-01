"""Tests for turning a remote media reference into a local file."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from nanobot.utils import remote_media
from nanobot.utils.remote_media import (
    RemoteMediaError,
    fetch_remote_bytes,
    is_remote_reference,
    looks_like_html,
    materialize_reference,
    reference_filename,
)

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdacd\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)
PAGE_URL = "https://onlyfiles.com/abc123/photo.png"
DIRECT_URL = "https://onlyfiles.com/dl/1699999999.nonce/abc123/photo.png"


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Route remote_media's pinned transport through an httpx MockTransport."""
    monkeypatch.setattr(
        remote_media,
        "PinnedDNSAsyncTransport",
        lambda **_kwargs: httpx.MockTransport(handler),
    )


# --- is_remote_reference -------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://onlyfiles.com/a/b.png", True),
        ("http://example.com/a.png", True),
        ("HTTPS://ONLYFILES.COM/a.png", True),
        ("  https://onlyfiles.com/a.png  ", True),
        ("/workspace/ref.png", False),
        ("ref.png", False),
        ("", False),
        (None, False),
        (123, False),
    ],
)
def test_is_remote_reference(value: object, expected: bool) -> None:
    assert is_remote_reference(value) is expected


# --- reference_filename --------------------------------------------------


def test_reference_filename_keeps_a_readable_basename_and_dedupes() -> None:
    first = reference_filename("https://onlyfiles.com/1/a/photo", ".png")
    second = reference_filename("https://onlyfiles.com/2/b/photo", ".png")
    assert first.endswith("-photo.png")
    assert second.endswith("-photo.png")
    # Same basename, different URL: the URL digest in the stem keeps them apart.
    assert first != second


def test_reference_filename_does_not_double_the_extension() -> None:
    assert reference_filename("https://onlyfiles.com/1/a/photo.png", ".png") == (
        reference_filename("https://onlyfiles.com/1/a/photo.png", ".png")
    )
    assert not reference_filename("https://onlyfiles.com/1/a/photo.png", ".png").endswith(
        ".png.png"
    )


def test_reference_filename_sanitizes_a_hostile_basename() -> None:
    name = reference_filename("https://onlyfiles.com/1/a/..%2f..%2fetc%2fpasswd", ".bin")
    assert "/" not in name
    assert name.endswith(".bin")


# --- fetch_remote_bytes --------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_remote_bytes_returns_body_and_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"]
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG_BYTES)

    _install_transport(monkeypatch, handler)
    raw, content_type = await fetch_remote_bytes(DIRECT_URL)
    assert raw == PNG_BYTES
    assert content_type == "image/png"


@pytest.mark.asyncio
async def test_fetch_remote_bytes_follows_a_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/dl/"):
            return httpx.Response(302, headers={"location": "/real/photo.png"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG_BYTES)

    _install_transport(monkeypatch, handler)
    raw, _ = await fetch_remote_bytes(DIRECT_URL)
    assert raw == PNG_BYTES


@pytest.mark.asyncio
async def test_fetch_remote_bytes_rejects_a_redirect_without_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_transport(monkeypatch, lambda _request: httpx.Response(302))
    with pytest.raises(RemoteMediaError, match="redirected without a location"):
        await fetch_remote_bytes(DIRECT_URL)


@pytest.mark.asyncio
async def test_fetch_remote_bytes_rejects_an_oversized_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_transport(
        monkeypatch,
        lambda _request: httpx.Response(200, content=b"x" * 64),
    )
    with pytest.raises(RemoteMediaError, match="larger than"):
        await fetch_remote_bytes(DIRECT_URL, max_bytes=16)


@pytest.mark.asyncio
async def test_fetch_remote_bytes_reports_an_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, lambda _request: httpx.Response(404))
    with pytest.raises(RemoteMediaError, match="HTTP 404"):
        await fetch_remote_bytes(DIRECT_URL)


@pytest.mark.asyncio
async def test_fetch_remote_bytes_rejects_an_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, lambda _request: httpx.Response(200, content=b""))
    with pytest.raises(RemoteMediaError, match="empty"):
        await fetch_remote_bytes(DIRECT_URL)


@pytest.mark.asyncio
async def test_fetch_remote_bytes_rejects_a_local_path() -> None:
    with pytest.raises(RemoteMediaError, match="not a remote reference"):
        await fetch_remote_bytes("/workspace/photo.png")


@pytest.mark.asyncio
async def test_fetch_remote_bytes_reports_a_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _install_transport(monkeypatch, handler)
    with pytest.raises(RemoteMediaError, match="download failed: ConnectError"):
        await fetch_remote_bytes(DIRECT_URL)


# --- materialize_reference ----------------------------------------------


@pytest.fixture
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "media"
    monkeypatch.setattr(remote_media, "get_media_dir", lambda channel=None: root)
    return root


FOREIGN_URL = "https://cdn.example.com/a/photo.png"


@pytest.mark.asyncio
async def test_materialize_reference_writes_a_png_under_the_media_dir(
    monkeypatch: pytest.MonkeyPatch,
    media_root: Path,
) -> None:
    async def fake_fetch(url: str, **_kwargs: object) -> tuple[bytes, str]:
        assert url == FOREIGN_URL
        return PNG_BYTES, "image/png"

    monkeypatch.setattr(remote_media, "fetch_remote_bytes", fake_fetch)
    path = await materialize_reference(FOREIGN_URL)

    assert path.is_file()
    assert path.read_bytes() == PNG_BYTES
    assert path.suffix == ".png"
    # Under the media root: an allowed root for every tool that guards media paths.
    assert media_root in path.parents
    # No temp file left behind by the atomic write.
    assert list(path.parent.glob("*.part")) == []


@pytest.mark.asyncio
async def test_materialize_reference_reuses_an_existing_copy(
    monkeypatch: pytest.MonkeyPatch,
    media_root: Path,
) -> None:
    calls = 0

    async def fake_fetch(url: str, **_kwargs: object) -> tuple[bytes, str]:
        nonlocal calls
        calls += 1
        return PNG_BYTES, "image/png"

    monkeypatch.setattr(remote_media, "fetch_remote_bytes", fake_fetch)
    first = await materialize_reference(FOREIGN_URL)
    second = await materialize_reference(FOREIGN_URL)

    assert first == second
    assert calls == 1


@pytest.mark.asyncio
async def test_materialize_reference_rewrites_an_onlyfiles_page_url(
    monkeypatch: pytest.MonkeyPatch,
    media_root: Path,
) -> None:
    seen: list[str] = []

    async def fake_resolve(page_url: str, **_kwargs: object) -> str:
        seen.append(page_url)
        return DIRECT_URL

    async def fake_fetch(url: str, **_kwargs: object) -> tuple[bytes, str]:
        seen.append(url)
        return PNG_BYTES, "image/png"

    monkeypatch.setattr("nanobot.utils.onlyfiles.resolve_raw_url", fake_resolve)
    monkeypatch.setattr(remote_media, "fetch_remote_bytes", fake_fetch)

    path = await materialize_reference(PAGE_URL)

    assert seen == [PAGE_URL, DIRECT_URL]
    assert path.read_bytes() == PNG_BYTES


@pytest.mark.asyncio
async def test_materialize_reference_tries_a_stale_raw_url_before_minting_a_new_one(
    monkeypatch: pytest.MonkeyPatch,
    media_root: Path,
) -> None:
    """A fresh ``/dl/`` token needs no page view; a dead one falls back to the page."""
    stale = "https://onlyfiles.com/dl/1000000000.deadbeef/abc123/photo.png"
    seen: list[str] = []

    async def fake_resolve(page_url: str, **_kwargs: object) -> str:
        assert page_url == stale
        return DIRECT_URL

    async def fake_fetch(url: str, **_kwargs: object) -> tuple[bytes, str]:
        seen.append(url)
        if url == stale:
            raise RemoteMediaError("download failed with HTTP 404")
        return PNG_BYTES, "image/png"

    monkeypatch.setattr("nanobot.utils.onlyfiles.resolve_raw_url", fake_resolve)
    monkeypatch.setattr(remote_media, "fetch_remote_bytes", fake_fetch)

    path = await materialize_reference(stale)

    assert seen == [stale, DIRECT_URL]
    assert path.read_bytes() == PNG_BYTES


@pytest.mark.asyncio
async def test_materialize_reference_rejects_a_viewer_page(
    monkeypatch: pytest.MonkeyPatch,
    media_root: Path,
) -> None:
    """Downloading the HTML viewer silently would hand the provider a web page."""

    async def fake_resolve(page_url: str, **_kwargs: object) -> str:
        return page_url

    async def fake_fetch(url: str, **_kwargs: object) -> tuple[bytes, str]:
        return b"<!DOCTYPE html><html>onlyfiles viewer</html>", "text/html"

    monkeypatch.setattr("nanobot.utils.onlyfiles.resolve_raw_url", fake_resolve)
    monkeypatch.setattr(remote_media, "fetch_remote_bytes", fake_fetch)

    with pytest.raises(RemoteMediaError, match="served a web page"):
        await materialize_reference(PAGE_URL)
    assert list(media_root.rglob("*")) == []


@pytest.mark.asyncio
async def test_materialize_reference_leaves_a_foreign_host_alone(
    monkeypatch: pytest.MonkeyPatch,
    media_root: Path,
) -> None:
    called = False

    async def fake_resolve(page_url: str, **_kwargs: object) -> str:
        nonlocal called
        called = True
        return page_url

    async def fake_fetch(url: str, **_kwargs: object) -> tuple[bytes, str]:
        assert url == FOREIGN_URL
        return PNG_BYTES, "image/png"

    monkeypatch.setattr("nanobot.utils.onlyfiles.resolve_raw_url", fake_resolve)
    monkeypatch.setattr(remote_media, "fetch_remote_bytes", fake_fetch)

    path = await materialize_reference(FOREIGN_URL)

    assert called is False
    assert path.read_bytes() == PNG_BYTES


@pytest.mark.asyncio
async def test_materialize_reference_falls_back_to_the_declared_suffix(
    monkeypatch: pytest.MonkeyPatch,
    media_root: Path,
) -> None:
    async def fake_fetch(url: str, **_kwargs: object) -> tuple[bytes, str]:
        return b"not-an-image", "video/mp4; charset=binary"

    monkeypatch.setattr(remote_media, "fetch_remote_bytes", fake_fetch)
    path = await materialize_reference(FOREIGN_URL)

    assert path.suffix == ".mp4"


@pytest.mark.asyncio
async def test_materialize_reference_propagates_a_download_failure(
    monkeypatch: pytest.MonkeyPatch,
    media_root: Path,
) -> None:
    async def fake_fetch(url: str, **_kwargs: object) -> tuple[bytes, str]:
        raise RemoteMediaError("download failed with HTTP 404")

    monkeypatch.setattr(remote_media, "fetch_remote_bytes", fake_fetch)
    with pytest.raises(RemoteMediaError, match="HTTP 404"):
        await materialize_reference(FOREIGN_URL)


# --- reference_candidates -----------------------------------------------


@pytest.mark.asyncio
async def test_reference_candidates_mints_a_raw_link_for_a_page_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve(page_url: str, **_kwargs: object) -> str:
        return DIRECT_URL

    monkeypatch.setattr("nanobot.utils.onlyfiles.resolve_raw_url", fake_resolve)
    assert await remote_media.reference_candidates(PAGE_URL) == [DIRECT_URL]


@pytest.mark.asyncio
async def test_reference_candidates_keeps_the_fresh_raw_link_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve(_page_url: str, **_kwargs: object) -> str:
        return DIRECT_URL

    monkeypatch.setattr("nanobot.utils.onlyfiles.resolve_raw_url", fake_resolve)
    assert await remote_media.reference_candidates(DIRECT_URL) == [DIRECT_URL]


@pytest.mark.asyncio
async def test_reference_candidates_returns_a_foreign_url_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve(_url: str, **_kwargs: object) -> str:
        raise AssertionError("must not resolve a foreign host")

    monkeypatch.setattr("nanobot.utils.onlyfiles.resolve_raw_url", fake_resolve)
    assert await remote_media.reference_candidates(FOREIGN_URL) == [FOREIGN_URL]


# --- looks_like_html ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"<!DOCTYPE html><html><body>onlyfiles</body></html>", True),
        (b"  <html><head></head></html>", True),
        (b"<?xml version='1.0'?><rss/>", True),
        (PNG_BYTES, False),
        (b"plain text", False),
    ],
)
def test_looks_like_html(raw: bytes, expected: bool) -> None:
    assert looks_like_html(raw) is expected
