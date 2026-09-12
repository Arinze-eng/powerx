from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from nanobot.webui.attachment_ingress import (
    extract_data_url_mime,
    extract_remote_file_url,
    resolve_remote_direct_url,
    store_inbound_attachments,
)
from nanobot.webui.ingress_policy import AttachmentIngressLimits


def _data_url(mime: str, payload: bytes) -> str:
    encoded = base64.b64encode(payload).decode()
    return f"data:{mime};base64,{encoded}"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("data:image/png;base64,AAAA", "image/png"),
        ("data:IMAGE/JPEG;charset=utf-8;base64,AAAA", "image/jpeg"),
        ("data:video/webm;codecs=vp9;base64,AAAA", "video/webm"),
        ("data:text/plain;base64,AAAA", "text/plain"),
        ("data:image/svg+xml;base64,AAAA", "image/svg+xml"),
        ("data:image/png,AAAA", None),
        ("data:;base64,AAAA", None),
        ("https://example.invalid/image.png", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_data_url_mime_normalizes_only_base64_data_urls(
    url: Any,
    expected: str | None,
) -> None:
    assert extract_data_url_mime(url) == expected


def test_store_inbound_document_preserves_safe_name(tmp_path: Path) -> None:
    paths, rejection = store_inbound_attachments(
        [
            {
                "data_url": _data_url("text/csv", b"name,value\nnanobot,1"),
                "name": "report.csv",
            },
        ],
        media_dir=tmp_path,
        logger=MagicMock(),
    )

    assert rejection is None
    assert len(paths) == 1
    saved = Path(paths[0])
    assert saved.parent == tmp_path
    assert saved.name.endswith("_report.csv")
    assert saved.read_bytes() == b"name,value\nnanobot,1"


def test_invalid_batch_removes_files_already_persisted(tmp_path: Path) -> None:
    paths, rejection = store_inbound_attachments(
        [
            {"data_url": _data_url("image/png", b"valid-first-item")},
            {"data_url": _data_url("image/svg+xml", b"<svg/>")},
        ],
        media_dir=tmp_path,
        logger=MagicMock(),
    )

    assert paths == []
    assert rejection == "mime"
    assert list(tmp_path.iterdir()) == []


def test_invalid_base64_cannot_create_an_empty_attachment(tmp_path: Path) -> None:
    paths, rejection = store_inbound_attachments(
        [{"data_url": "data:text/plain;base64,@@@@", "name": "empty.txt"}],
        media_dir=tmp_path,
        logger=MagicMock(),
    )

    assert paths == []
    assert rejection == "decode"
    assert list(tmp_path.iterdir()) == []


def test_single_file_limit_is_attachment_policy_not_transport(tmp_path: Path) -> None:
    paths, rejection = store_inbound_attachments(
        [{"data_url": _data_url("text/plain", b"12345"), "name": "large.txt"}],
        media_dir=tmp_path,
        logger=MagicMock(),
        limits=AttachmentIngressLimits(max_file_bytes=4, max_total_bytes=20),
    )

    assert paths == []
    assert rejection == "size"
    assert list(tmp_path.iterdir()) == []


def test_total_attachment_policy_rolls_back_the_batch(tmp_path: Path) -> None:
    paths, rejection = store_inbound_attachments(
        [
            {"data_url": _data_url("text/plain", b"1234"), "name": "one.txt"},
            {"data_url": _data_url("text/plain", b"5678"), "name": "two.txt"},
        ],
        media_dir=tmp_path,
        logger=MagicMock(),
        limits=AttachmentIngressLimits(max_file_bytes=4, max_total_bytes=6),
    )

    assert paths == []
    assert rejection == "total_size"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "mime",
    [
        "application/zip",
        "application/vnd.android.package-archive",
        "application/x-tar",
        "application/gzip",
        "application/x-7z-compressed",
        "application/octet-stream",
    ],
)
def test_binary_archive_files_are_accepted(tmp_path: Path, mime: str) -> None:
    """Any binary/archive file (zip, apk, tar, …) uploads instead of being
    rejected as an unsupported type."""
    paths, rejection = store_inbound_attachments(
        [
            {
                "data_url": _data_url(mime, b"\x50\x4b\x03\x04 some-blob"),
                "name": f"bundle.{mime.split('/')[-1]}",
            },
        ],
        media_dir=tmp_path,
        logger=MagicMock(),
    )

    assert rejection is None
    assert len(paths) == 1
    saved = Path(paths[0])
    assert saved.read_bytes() == b"\x50\x4b\x03\x04 some-blob"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://tmpfiles.org/1234567/report.pdf", "https://tmpfiles.org/1234567/report.pdf"),
        ("https://tmpfiles.org/dl/1234567/report.pdf", "https://tmpfiles.org/dl/1234567/report.pdf"),
        ("https://TMPFILES.ORG/123/report.pdf", "https://TMPFILES.ORG/123/report.pdf"),
        ("http://tmpfiles.org/123/report.pdf", ""),
        ("https://evil.example/123/report.pdf", ""),
        ("https://tmpfiles.org.evil.test/123/report.pdf", ""),
        ("https://tmpfiles.org/", ""),
        ("https://tmpfiles.org", ""),
        ("", ""),
        (123, ""),
        (None, None),
    ],
)
def test_extract_remote_file_url_allows_only_tmpfiles_https(
    url: Any,
    expected: str | None,
) -> None:
    assert extract_remote_file_url({"url": url}) == expected


def test_extract_remote_file_url_absent_key_is_none() -> None:
    assert extract_remote_file_url({"data_url": _data_url("image/png", b"x")}) is None


def test_store_inbound_tmpfiles_url_persists_nothing(tmp_path: Path) -> None:
    """Browser-uploaded file attachments arrive as tmpfiles.org URLs: the
    gateway must not write any bytes to disk — the URL itself is the record."""
    url = "https://tmpfiles.org/1234567/report.pdf"
    paths, rejection = store_inbound_attachments(
        [{"url": url, "name": "report.pdf"}],
        media_dir=tmp_path,
        logger=MagicMock(),
    )

    assert rejection is None
    assert paths == [url]
    assert list(tmp_path.iterdir()) == []


def test_store_inbound_mixed_batch_keeps_local_and_remote(tmp_path: Path) -> None:
    url = "https://tmpfiles.org/42/bundle.apk"
    paths, rejection = store_inbound_attachments(
        [
            {"url": url, "name": "bundle.apk"},
            {"data_url": _data_url("image/png", b"png-bytes"), "name": "shot.png"},
        ],
        media_dir=tmp_path,
        logger=MagicMock(),
    )

    assert rejection is None
    assert len(paths) == 2
    assert paths[0] == url
    assert Path(paths[1]).exists()


def test_store_inbound_rejects_untrusted_url_host(tmp_path: Path) -> None:
    paths, rejection = store_inbound_attachments(
        [{"url": "https://evil.example/123/mal.pdf", "name": "mal.pdf"}],
        media_dir=tmp_path,
        logger=MagicMock(),
    )

    assert paths == []
    assert rejection == "malformed"


# --- resolve_remote_direct_url -------------------------------------------
#
# tmpfiles.org page URLs (/<slug>/<file>) serve an HTML viewer; the raw bytes
# live under /dl/<nonce>/<slug>/<file>, and the nonce is minted per page view
# and embedded in that HTML. The agent must receive the /dl/ form so it can
# download with a single GET instead of scraping the viewer page.

_PAGE_URL = "https://tmpfiles.org/wewRPwJIvaJ9/report.pdf"
_DL_PATH = "/dl/1789235400.3535ca35b2436ac2/wewRPwJIvaJ9/report.pdf"


def _client_returning(html: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))
    )


@pytest.mark.asyncio
async def test_page_url_resolves_to_embedded_dl_link(monkeypatch: pytest.MonkeyPatch) -> None:
    html = f'<a href="https://tmpfiles.org{_DL_PATH}">download</a>'
    monkeypatch.setattr(
        "nanobot.webui.attachment_ingress.httpx.AsyncClient",
        lambda **kwargs: _client_returning(html),
    )

    resolved = await resolve_remote_direct_url(_PAGE_URL)

    assert resolved == f"https://tmpfiles.org{_DL_PATH}"


@pytest.mark.asyncio
async def test_already_direct_url_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**kwargs):  # pragma: no cover - must never be used
        raise AssertionError("must not fetch an already-direct URL")

    monkeypatch.setattr(
        "nanobot.webui.attachment_ingress.httpx.AsyncClient", _boom
    )

    direct = f"https://tmpfiles.org{_DL_PATH}"
    assert await resolve_remote_direct_url(direct) == direct


@pytest.mark.asyncio
async def test_non_tmpfiles_host_is_left_untouched() -> None:
    url = "https://example.com/file.pdf"
    assert await resolve_remote_direct_url(url) == url


@pytest.mark.asyncio
async def test_fetch_failure_falls_back_to_original(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        "nanobot.webui.attachment_ingress.httpx.AsyncClient",
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    assert await resolve_remote_direct_url(_PAGE_URL) == _PAGE_URL


@pytest.mark.asyncio
async def test_missing_dl_link_falls_back_to_original(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nanobot.webui.attachment_ingress.httpx.AsyncClient",
        lambda **kwargs: _client_returning("<html><body>no link here</body></html>"),
    )

    assert await resolve_remote_direct_url(_PAGE_URL) == _PAGE_URL
