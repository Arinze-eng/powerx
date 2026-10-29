"""Tests for the FastSaver client and the host-side link hook in the media tool.

The FastSaver API is faked with a stub ``urlopen``, so these tests need no network
and spend no credits. They pin three things:

1. A refusal in the response body (``ok: false``) is an error, even on HTTP 200.
2. With no key, or a non-YouTube link, the client does nothing and returns None.
3. The model cannot pass its own ``fastsaver_*`` values into the sandbox command.
"""
from __future__ import annotations

import asyncio
import io
import json
from typing import Any
from unittest.mock import patch

import pytest

from nanobot.utils import fastsaver

YT = "https://www.youtube.com/watch?v=9lY-1J8LPS0"


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(payload: dict[str, Any], status: int = 200):
    captured: dict[str, Any] = {}

    def _open(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        resp = _Resp(json.dumps(payload).encode("utf-8"))
        resp.status = status
        return resp

    return _open, captured


def test_no_key_returns_none_without_calling_api(monkeypatch):
    monkeypatch.delenv("FASTSAVER_API_KEY", raising=False)
    with patch("urllib.request.urlopen") as opened:
        assert fastsaver.request_download(YT) is None
        opened.assert_not_called()


def test_non_youtube_link_returns_none(monkeypatch):
    monkeypatch.setenv("FASTSAVER_API_KEY", "test-key")
    with patch("urllib.request.urlopen") as opened:
        assert fastsaver.request_download("https://vimeo.com/123") is None
        opened.assert_not_called()


def test_ok_true_returns_link_and_sends_key(monkeypatch):
    monkeypatch.setenv("FASTSAVER_API_KEY", "test-key")
    opener, seen = _fake_urlopen({
        "ok": True,
        "download_url": "https://cdn.example.com/file.mp4",
        "filename": "clip.mp4",
        "video_id": "9lY-1J8LPS0",
        "duration": 42,
    })
    with patch("urllib.request.urlopen", side_effect=opener):
        link = fastsaver.request_download(YT, quality="720")
    assert link is not None
    assert link["download_url"] == "https://cdn.example.com/file.mp4"
    assert link["format"] == "720p"
    assert link["credits"] == 15
    assert link["duration"] == 42.0
    assert seen["url"] == "https://api.fastsaver.io/v1/youtube/download"
    assert seen["body"] == {"url": YT, "format": "720p"}
    headers = {k.lower(): v for k, v in seen["headers"].items()}
    assert headers["x-api-key"] == "test-key"


def test_ok_false_on_http_200_raises(monkeypatch):
    monkeypatch.setenv("FASTSAVER_API_KEY", "test-key")
    opener, _ = _fake_urlopen({"ok": False, "error": "video is private"}, status=200)
    with patch("urllib.request.urlopen", side_effect=opener):
        with pytest.raises(fastsaver.FastSaverError, match="video is private"):
            fastsaver.request_download(YT)


def test_never_requests_the_25_credit_formats(monkeypatch):
    # 1440p and 2160p cost 25 credits each; the tool never asks for them.
    for quality in ("best", "1080", "1440", "2160", "nonsense", ""):
        assert fastsaver.format_for(quality, False) not in ("1440p", "2160p")
    assert fastsaver.format_for("1080", True) == "mp3"


def test_standard_download_costs_15_credits(monkeypatch):
    monkeypatch.setenv("FASTSAVER_API_KEY", "test-key")
    opener, _ = _fake_urlopen({"ok": True, "download_url": "https://cdn.example.com/x.mp4"})
    with patch("urllib.request.urlopen", side_effect=opener):
        link = fastsaver.request_download(YT, quality="1080")
    assert link is not None
    assert link["format"] == "1080p"
    assert link["credits"] == 15


def _load_media_tool():
    # Import the tool module without needing a running sandbox.
    from nanobot.agent.tools import media

    return media


def test_model_supplied_fastsaver_values_are_stripped(monkeypatch):
    media = _load_media_tool()
    monkeypatch.delenv("FASTSAVER_API_KEY", raising=False)
    kwargs = {
        "url": YT,
        "via": "auto",
        "fastsaver_url": "https://evil.example.com/steal",
        "fastsaver_filename": "x",
        "fastsaver_duration": 1,
    }
    out = asyncio.run(media._with_fastsaver_link(kwargs))
    assert not any(k.startswith("fastsaver_") for k in out)


def test_host_link_is_attached_only_for_auto_youtube(monkeypatch):
    media = _load_media_tool()
    monkeypatch.setenv("FASTSAVER_API_KEY", "test-key")
    link = {
        "download_url": "https://cdn.example.com/file.mp4",
        "filename": "clip.mp4",
        "duration": 42.0,
        "format": "1080p",
        "credits": 15,
    }
    with patch.object(media.fastsaver, "request_download", return_value=link) as req:
        out = asyncio.run(media._with_fastsaver_link({"url": YT, "via": "auto", "quality": "1080"}))
        assert out["fastsaver_url"] == "https://cdn.example.com/file.mp4"
        assert req.call_count == 1

        req.reset_mock()
        out = asyncio.run(media._with_fastsaver_link({"url": YT, "via": "yt-dlp"}))
        assert "fastsaver_url" not in out
        req.assert_not_called()


def test_refusal_falls_back_without_link(monkeypatch):
    media = _load_media_tool()
    monkeypatch.setenv("FASTSAVER_API_KEY", "test-key")
    with patch.object(
        media.fastsaver, "request_download",
        side_effect=fastsaver.FastSaverError("out of credits"),
    ):
        out = asyncio.run(media._with_fastsaver_link({"url": YT, "via": "auto"}))
    assert "fastsaver_url" not in out
    assert out["url"] == YT
