"""The delivery contract: one link, permanent, and it downloads on tap.

Reported live (2026-09-30, from the deployed service while a user worked in a
sandbox): *"The link is not working use onlyfiles"*. Two separate defects
produced links that could not work, and both are covered here.

1. ``NANOBOT_API_PUBLIC_URL`` is this deployment's public address and carries a
   path (``https://<host>/admin``). ``gateway_base_url`` used it verbatim, so the
   permanent artifact link became ``https://<host>/admin/f/<id>`` — the SPA
   catch-all answers that with its own HTML page and HTTP 200, so the link
   "opened" but never delivered the file. The gateway's routes live at the ORIGIN
   root, so only the origin may be used.
2. The link that actually works was never shown to the model. Every sandbox
   backend offered the raw ``/dl/`` token (~2h lifetime) as the "Direct-download
   link" and the onlyfiles *page* URL — an HTML viewer — as the "Permanent ...
   fallback", while the durable ``/f/<id>`` link was dropped entirely.

Offline: no network is touched.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest

from nanobot.agent.tools import novita_sandbox
from nanobot.utils import file_share
from nanobot.utils.file_share import artifact_delivery_text
from nanobot.utils.onlyfiles import gateway_base_url, gateway_download_url, permanent_download_url

PAGE = "https://onlyfiles.com/ABC123def/app-debug.apk"
RAW = "https://onlyfiles.com/dl/1790755869.f9f6b984/ABC123def/app-debug.apk"
RAW_2H = "1790755869"


def _clear_gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("POWERX_PUBLIC_URL", "NANOBOT_API_PUBLIC_URL", "API_SERVER_URL"):
        monkeypatch.delenv(var, raising=False)


# ---- defect 1: a base URL with a path must not leak into the link ---------


def test_gateway_base_url_drops_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact production value that produced /admin/f/<id>."""
    monkeypatch.setenv("NANOBOT_API_PUBLIC_URL", "https://host.code.run/admin")
    assert gateway_base_url() == "https://host.code.run"
    # The bug in one assertion: this used to be https://host.code.run/admin/f/...
    assert permanent_download_url(PAGE) == "https://host.code.run/f/ABC123def"


def test_gateway_base_url_keeps_a_port_and_drops_a_deep_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POWERX_PUBLIC_URL", "https://host.example.com:8443/admin/ui/")
    assert gateway_base_url() == "https://host.example.com:8443"


def test_gateway_base_url_ignores_a_non_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A placeholder must never be concatenated into a link."""
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("NANOBOT_API_PUBLIC_URL", "your-server-here")
    assert gateway_base_url() == ""
    assert gateway_download_url(PAGE) == ""


def test_gateway_base_url_prefers_the_first_configured_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POWERX_PUBLIC_URL", "https://powerx.example.com/admin")
    monkeypatch.setenv("NANOBOT_API_PUBLIC_URL", "https://other.example.com")
    assert gateway_base_url() == "https://powerx.example.com"


def test_gateway_download_url_is_empty_without_a_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_gateway_env(monkeypatch)
    assert gateway_download_url(PAGE) == ""
    # The page form is permanent but renders a viewer; keep it only as a fallback.
    assert permanent_download_url(PAGE) == PAGE


# ---- defect 2: the model must be handed the link that works --------------


def test_delivery_prefers_the_gateway_link_over_the_expiring_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POWERX_PUBLIC_URL", "https://gateway.example.com/admin")
    shared = {
        "url": "https://gateway.example.com/f/ABC123def",
        "page_url": PAGE,
        "download_url": RAW,
        "gateway_url": "https://gateway.example.com/f/ABC123def",
        "host": "onlyfiles",
    }
    text = artifact_delivery_text(shared, "/tmp/downloads/app-debug.apk")
    assert "https://gateway.example.com/f/ABC123def" in text
    # The expiring token and the HTML viewer page must NOT be offered at all —
    # the model copies the first URL it sees, and both of these are dead ends.
    assert "/dl/" not in text
    assert PAGE not in text
    assert text.index("https://gateway.example.com/f/ABC123def") < text.index("Give the user")


def test_delivery_uses_the_raw_token_only_without_a_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_gateway_env(monkeypatch)
    shared = {
        "url": PAGE,
        "page_url": PAGE,
        "download_url": RAW,
        "gateway_url": "",
        "host": "onlyfiles",
    }
    text = artifact_delivery_text(shared, "/tmp/downloads/app-debug.apk")
    # No permanent download link exists, so the fresh token is the working link...
    assert RAW in text
    assert "expires in about two hours" in text
    # ...and it must come before the viewer page, which is only a long-term fallback.
    assert text.index(RAW) < text.index(PAGE)


def test_delivery_catbox_url_is_permanent_and_direct() -> None:
    link = "https://files.catbox.moe/abcd1234.zip"
    shared = file_share._normalize_catbox(link)
    text = artifact_delivery_text(shared, "/tmp/downloads/big.zip")
    assert link in text
    assert text.count(link) == 1
    assert "permanent, tap to download" in text
    assert "catbox" in text


def test_normalize_onlyfiles_carries_the_gateway_separately() -> None:
    out = file_share._normalize_onlyfiles(
        {
            "url": "https://gateway.example.com/f/ABC123def",
            "page_url": PAGE,
            "download_url": RAW,
            "gateway_url": "https://gateway.example.com/f/ABC123def",
        }
    )
    # The delivery needs to tell "permanent download" from "permanent viewer page".
    assert out["gateway_url"] == "https://gateway.example.com/f/ABC123def"
    assert out["page_url"] == PAGE
    assert out["download_url"] == RAW


def test_normalize_onlyfiles_without_a_gateway_leaves_it_empty() -> None:
    out = file_share._normalize_onlyfiles({"url": PAGE, "page_url": PAGE, "download_url": RAW})
    assert out["gateway_url"] == ""
    assert file_share._normalize_catbox("https://files.catbox.moe/x.bin")["gateway_url"] == ""


def test_delivery_text_never_mentions_the_local_path_as_a_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model must give a URL, not a host path it also cannot read."""
    monkeypatch.setenv("POWERX_PUBLIC_URL", "https://gateway.example.com")
    shared = file_share._normalize_onlyfiles(
        {
            "url": "https://gateway.example.com/f/ABC123def",
            "page_url": PAGE,
            "download_url": RAW,
            "gateway_url": "https://gateway.example.com/f/ABC123def",
        }
    )
    text = artifact_delivery_text(shared, "/home/user/.nanobot/artifacts/x.apk")
    link_lines = [ln for ln in text.splitlines() if ln.startswith("http")]
    assert link_lines == ["https://gateway.example.com/f/ABC123def"]


# ---- the Novita backend must publish through onlyfiles too --------------


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeSession:
    """Minimal stand-in so the signed-URL fetch can be tested offline."""

    def __init__(self, status: int, body: bytes) -> None:
        self._status = status
        self._body = body

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        _FakeSession.last_url = url
        return _FakeResponse(self._status, self._body)


def _install_fake_http(monkeypatch: pytest.MonkeyPatch, status: int, body: bytes) -> dict:
    captured: dict = {}

    def _session(*args: Any, **kwargs: Any) -> _FakeSession:
        return _FakeSession(status, body)

    monkeypatch.setattr(aiohttp, "ClientSession", _session)

    async def _publish(data: bytes, *, filename: str, **kwargs: Any) -> dict:
        captured["data"] = data
        captured["filename"] = filename
        return {
            "url": "https://gateway.example.com/f/zzz",
            "page_url": "https://onlyfiles.com/zzz/" + filename,
            "download_url": "https://onlyfiles.com/dl/1.a/zzz/" + filename,
            "gateway_url": "https://gateway.example.com/f/zzz",
            "host": "onlyfiles",
        }

    monkeypatch.setattr(novita_sandbox, "upload_shared_artifact_bytes", _publish)
    return captured


async def test_novita_signed_url_is_republished_instead_of_handed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5-minute signed URL must never reach the user."""
    captured = _install_fake_http(monkeypatch, 200, b"apk-bytes")
    shared = await novita_sandbox._publish_signed_artifact(
        "https://sandbox.example.com/signed?sig=abc",
        filename="app-debug.apk",
    )
    assert _FakeSession.last_url == "https://sandbox.example.com/signed?sig=abc"
    assert captured["data"] == b"apk-bytes"
    assert captured["filename"] == "app-debug.apk"
    text = artifact_delivery_text(shared, "/workspace/app-debug.apk")
    assert "https://gateway.example.com/f/zzz" in text
    assert "expires in 5 minutes" not in text
    assert "sandbox.example.com" not in text


async def test_novita_publish_rejects_an_empty_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_http(monkeypatch, 200, b"")
    with pytest.raises(file_share.FileShareError):
        await novita_sandbox._publish_signed_artifact("https://x/y", filename="a.apk")


async def test_novita_publish_reports_a_failed_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_http(monkeypatch, 403, b"denied")
    with pytest.raises(file_share.FileShareError):
        await novita_sandbox._publish_signed_artifact("https://x/y", filename="a.apk")


async def test_novita_publish_rejects_a_missing_url() -> None:
    with pytest.raises(file_share.FileShareError):
        await novita_sandbox._publish_signed_artifact("", filename="a.apk")
