"""Tests for the TikTok -> YouTube repost automation.

Offline: no TikTok, no YouTube, no sandbox. The sandbox executor and the Google
HTTP calls are both faked, so what is pinned here is the *contract*:

* a TikTok link yields the right video id, and a bad link is refused early;
* the metadata is composed within YouTube's own limits, crediting the creator
  and linking the original;
* a video already reposted is never uploaded twice (the ledger), unless the
  caller explicitly forces it;
* the download runs in the sandbox, via yt-dlp, with the URL passed as a quoted
  variable rather than interpolated into the command;
* the upload is a two-step resumable request carrying the token in a header,
  and a quota-exceeded answer becomes a message a user can act on.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.youtube import repost
from nanobot.youtube.repost import (
    DEFAULT_TAGS,
    RepostLedger,
    TikTokRepostError,
    compose_description,
    compose_title,
    decode_info,
    download_command,
    download_in_sandbox,
    info_read_command,
    parse_tiktok_url,
    repost_tiktok,
    upload_video,
    video_body,
)

CANONICAL = "https://www.tiktok.com/@groupie_tech/video/7412345678901234567"


# ---- URL handling --------------------------------------------------------


def test_parse_canonical_url_keeps_the_id_and_author() -> None:
    target = parse_tiktok_url(CANONICAL)
    assert target.video_id == "7412345678901234567"
    assert target.author == "groupie_tech"


def test_parse_short_link() -> None:
    target = parse_tiktok_url("https://www.tiktok.com/t/ZTabc123/")
    assert target.video_id == "ZTabc123"


def test_parse_bare_domain_is_upgraded_to_https() -> None:
    target = parse_tiktok_url("tiktok.com/@a/video/7412345678901234567")
    assert target.url.startswith("https://")
    assert target.video_id == "7412345678901234567"


@pytest.mark.parametrize("bad", ["", "   ", "https://youtube.com/watch?v=abc"])
def test_bad_links_are_refused(bad: str) -> None:
    with pytest.raises(TikTokRepostError):
        parse_tiktok_url(bad)


# ---- metadata composition ------------------------------------------------


def test_title_uses_the_caption_and_is_capped() -> None:
    assert compose_title(caption="  my   clip  ", author="a", video_id="1") == "my clip"
    long = compose_title(caption="x" * 400, author="a", video_id="1")
    assert len(long) == repost.TITLE_MAX


def test_title_falls_back_to_the_author() -> None:
    assert compose_title(caption="", author="groupie_tech", video_id="1") == "TikTok by @groupie_tech"


def test_description_credits_the_creator_and_links_the_original() -> None:
    text = compose_description(caption="hello", author="groupie_tech", source_url=CANONICAL)
    assert "hello" in text
    assert "@groupie_tech on TikTok" in text
    assert CANONICAL in text
    assert len(text) <= repost.DESCRIPTION_MAX


# ---- ledger --------------------------------------------------------------


def test_ledger_remembers_and_reads_back(tmp_path: Path) -> None:
    ledger = RepostLedger(path=tmp_path / "ledger.json")
    assert ledger.get("1") is None
    import asyncio

    asyncio.run(ledger.record("1", {"youtube_video_id": "yt1", "title": "t"}))
    entry = ledger.get("1")
    assert entry is not None
    assert entry["youtube_video_id"] == "yt1"
    assert entry["reposted_at"]


def test_ledger_survives_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{not json")
    assert RepostLedger(path=path).all() == {}


# ---- download command ----------------------------------------------------


def test_download_command_passes_the_url_as_a_quoted_variable() -> None:
    command = download_command("/home/tenki/.tiktok-reposts/123")
    # The URL is never interpolated into the command text, so a link containing
    # shell metacharacters cannot turn into a second command.
    assert "$URL" in command
    assert "URL=" not in command.replace("$URL", "")
    assert "yt-dlp" in command
    assert "mp4" in command
    assert "--write-info-json" in command
    assert "/home/tenki/.tiktok-reposts/123" in command


def test_download_command_impersonates_a_browser_and_retries_once() -> None:
    """TikTok's bot wall answers a bare datacenter fetch with an "unexpected
    response" error, so the command impersonates a browser and, if that still
    fails, retries against a named TikTok API hostname."""
    command = download_command("/home/tenki/.tiktok-reposts/123")
    assert "curl_cffi" in command
    assert "--impersonate chrome" in command
    assert "api_hostname=api22-normal-c-useast2a.tiktokv.com" in command
    # Two attempts, and the second is reachable only via the fallback branch.
    assert command.count('"$URL"') == 2
    assert " || " in command


# ---- reading yt-dlp's metadata back --------------------------------------


def test_info_read_command_hides_its_program_in_base64() -> None:
    command = info_read_command("/home/tenki/.tiktok-reposts/123/video.info.json")
    assert "base64 -d" in command
    # The program travels encoded, so no quoting layer between here and the
    # sandbox can mangle it, and no shell in between ever sees a brace.
    assert "sys.argv" not in command
    assert "{" not in command
    assert "/home/tenki/.tiktok-reposts/123/video.info.json" in command


def test_info_read_command_asks_only_for_the_useful_fields() -> None:
    program = base64.b64decode(
        info_read_command("p/video.info.json").split("echo ")[1].split(" |")[0]
    ).decode()
    # A signed CDN URL per format is what makes the raw file tens of kilobytes.
    assert "http" not in program
    assert "description" in program
    assert "uploader" in program
    assert "webpage_url" in program


def test_decode_info_ignores_a_truncated_document() -> None:
    """This is the live failure that motivated the base64 extractor: a capped
    stdout cuts the JSON mid-object, so it must not be mistaken for data."""
    truncated = '{"id": "123", "formats": [{"url": "https://cdn.example/x?y=1'
    assert decode_info(truncated) == {}
    assert decode_info("") == {}
    assert decode_info("downloaded\n[exit_code=0]") == {}


def test_decode_info_finds_the_payload_among_other_output() -> None:
    payload = base64.b64encode(json.dumps({"uploader": "groupie_tech"}).encode()).decode()
    output = f"[stderr] warning\n{payload}\n[exit_code=0]"
    assert decode_info(output) == {"uploader": "groupie_tech"}


# ---- upload --------------------------------------------------------------


def test_video_body_shape() -> None:
    body = video_body(title="t" * 200, description="d", privacy="unlisted")
    assert body["snippet"]["title"] == "t" * repost.TITLE_MAX
    assert body["snippet"]["tags"] == list(DEFAULT_TAGS)
    assert body["snippet"]["categoryId"] == "22"
    assert body["status"]["privacyStatus"] == "unlisted"
    assert body["status"]["selfDeclaredMadeForKids"] is False


class _FakeResponse:
    def __init__(self, status_code: int = 200, *, headers: dict[str, str] | None = None, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self.content = b"{}" if payload is not None else b""
        self.text = text or json.dumps(payload or {})

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
    """Records the two-step resumable handshake."""

    calls: list[dict[str, Any]] = []
    init_kwargs: list[dict[str, Any]] = []
    start_response: _FakeResponse = _FakeResponse(
        headers={"Location": "https://upload.example/session"}
    )
    finish_response: _FakeResponse = _FakeResponse(payload={"id": "yt123"})
    #: Consumed in order by :meth:`put`, so a test can script a redirect chain.
    put_responses: list[_FakeResponse] = []

    def __init__(self, **kwargs: Any) -> None:
        _FakeClient.init_kwargs.append(kwargs)

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_exc: Any) -> None: ...

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        _FakeClient.calls.append({"method": "post", "url": url, **kwargs})
        return _FakeClient.start_response

    async def put(self, url: str, **kwargs: Any) -> _FakeResponse:
        _FakeClient.calls.append({"method": "put", "url": url, **kwargs})
        if _FakeClient.put_responses:
            return _FakeClient.put_responses.pop(0)
        return _FakeClient.finish_response


@pytest.mark.asyncio
async def test_upload_uses_a_resumable_handshake_and_keeps_the_token_in_a_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakeClient.calls = []
    _FakeClient.start_response = _FakeResponse(headers={"Location": "https://upload.example/session"})
    _FakeClient.finish_response = _FakeResponse(payload={"id": "yt123"})
    monkeypatch.setattr(repost.httpx, "AsyncClient", _FakeClient)

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video-bytes")

    video_id = await upload_video("tok", video, title="T", description="D")

    assert video_id == "yt123"
    start, finish = _FakeClient.calls
    assert start["method"] == "post"
    assert "uploadType=resumable" in start["url"] or start["params"]["uploadType"] == "resumable"
    assert start["headers"]["Authorization"] == "Bearer tok"
    assert start["json"]["snippet"]["title"] == "T"
    assert finish["method"] == "put"
    assert finish["url"] == "https://upload.example/session"
    assert finish["content"] == b"video-bytes"
    assert finish["headers"]["Authorization"] == "Bearer tok"
    # The token is a header, never part of the URL that goes into logs.
    assert "tok" not in finish["url"]


@pytest.mark.asyncio
async def test_upload_quota_error_is_reported_as_quota(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakeClient.calls = []
    _FakeClient.start_response = _FakeResponse(
        status_code=403,
        payload={
            "error": {
                "message": "The request cannot be completed because you have exceeded your quota.",
                "errors": [{"reason": "quotaExceeded"}],
            }
        },
    )
    monkeypatch.setattr(repost.httpx, "AsyncClient", _FakeClient)

    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")
    with pytest.raises(TikTokRepostError) as caught:
        await upload_video("tok", video, title="T", description="D")
    assert caught.value.status == 429
    assert "quota" in caught.value.message.lower()


@pytest.mark.asyncio
async def test_missing_upload_session_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakeClient.calls = []
    _FakeClient.start_response = _FakeResponse(headers={})
    monkeypatch.setattr(repost.httpx, "AsyncClient", _FakeClient)

    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")
    with pytest.raises(TikTokRepostError) as caught:
        await upload_video("tok", video, title="T", description="D")
    assert caught.value.status == 502


@pytest.mark.asyncio
async def test_upload_never_auto_follows_a_redirect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A chased 308 would re-POST the JSON body, which Google rejects — so the
    client must be built with redirects off."""
    _FakeClient.calls = []
    _FakeClient.init_kwargs = []
    _FakeClient.put_responses = []
    _FakeClient.start_response = _FakeResponse(headers={"Location": "https://upload.example/session"})
    _FakeClient.finish_response = _FakeResponse(payload={"id": "yt123"})
    monkeypatch.setattr(repost.httpx, "AsyncClient", _FakeClient)

    video = tmp_path / "video.mp4"
    video.write_bytes(b"bytes")
    await upload_video("tok", video, title="T", description="D")

    assert _FakeClient.init_kwargs
    assert _FakeClient.init_kwargs[0]["follow_redirects"] is False


@pytest.mark.asyncio
async def test_upload_follows_one_upload_session_redirect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Google can answer the PUT with a 308 to a googleusercontent URL; the same
    bytes and the same token must go there."""
    _FakeClient.calls = []
    _FakeClient.init_kwargs = []
    _FakeClient.start_response = _FakeResponse(headers={"Location": "https://upload.example/session"})
    _FakeClient.put_responses = [
        _FakeResponse(status_code=308, headers={"Location": "https://gc.example/final"}),
        _FakeResponse(payload={"id": "yt999"}),
    ]
    _FakeClient.finish_response = _FakeResponse(payload={"id": "yt999"})
    monkeypatch.setattr(repost.httpx, "AsyncClient", _FakeClient)

    video = tmp_path / "video.mp4"
    video.write_bytes(b"bytes")
    try:
        video_id = await upload_video("tok", video, title="T", description="D")
    finally:
        _FakeClient.put_responses = []

    assert video_id == "yt999"
    puts = [call for call in _FakeClient.calls if call["method"] == "put"]
    assert [call["url"] for call in puts] == [
        "https://upload.example/session",
        "https://gc.example/final",
    ]
    assert all(call["content"] == b"bytes" for call in puts)
    assert all(call["headers"]["Authorization"] == "Bearer tok" for call in puts)


@pytest.mark.asyncio
async def test_a_redirect_loop_is_not_chased_forever(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Three redirects, each to a new URL, must stop and report — not loop."""
    _FakeClient.calls = []
    _FakeClient.init_kwargs = []
    _FakeClient.start_response = _FakeResponse(headers={"Location": "https://upload.example/0"})
    _FakeClient.put_responses = [
        _FakeResponse(status_code=308, headers={"Location": f"https://upload.example/{n}"})
        for n in (1, 2, 3, 4)
    ]
    monkeypatch.setattr(repost.httpx, "AsyncClient", _FakeClient)

    video = tmp_path / "video.mp4"
    video.write_bytes(b"bytes")
    try:
        with pytest.raises(TikTokRepostError):
            await upload_video("tok", video, title="T", description="D")
    finally:
        _FakeClient.put_responses = []

    puts = [call for call in _FakeClient.calls if call["method"] == "put"]
    assert len(puts) == 3


# ---- download, inside the sandbox ----------------------------------------


class _FakeBackend:
    def __init__(self, *, listing: str = "video.mp4", info: dict[str, Any] | None = None) -> None:
        self.commands: list[str] = []
        self.downloads: list[tuple[str, str]] = []
        self._listing = listing
        self._info = info if info is not None else {"description": "cap", "uploader": "u"}

    async def run(self, command: str, *, timeout: int = 0) -> str:
        self.commands.append(command)
        if "base64 -d" in command:
            # Faithful to the real extractor: one base64 line of chosen fields.
            payload = base64.b64encode(json.dumps(self._info).encode()).decode()
            return f"{payload}\n[exit_code=0]"
        if ".info.json" in command:
            return json.dumps(self._info) + "\n[exit_code=0]"
        if "ls -1" in command:
            return f"{self._listing}\n[exit_code=0]"
        return "downloaded\n[exit_code=0]"

    async def download(self, remote: str, local: str) -> None:
        self.downloads.append((remote, local))
        Path(local).write_bytes(b"video-bytes")


class _FakeExecutor:
    name = "tenki"
    available = True

    def __init__(self, backend: _FakeBackend) -> None:
        self.backend = backend


@pytest.mark.asyncio
async def test_download_runs_in_the_sandbox_and_copies_the_file_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    executor = _FakeExecutor(backend)

    async def _resolve() -> Any:
        return executor

    async def _root() -> str:
        return "/home/tenki"

    monkeypatch.setattr(
        "nanobot.agent.tools.workspace_bridge.resolve_remote_executor", _resolve
    )
    monkeypatch.setattr("nanobot.agent.tools.workspace_bridge.remote_workspace_root", _root)

    local, info = await download_in_sandbox(parse_tiktok_url(CANONICAL))
    try:
        assert local.is_file()
        assert local.read_bytes() == b"video-bytes"
        assert info.get("uploader") == "u"
        assert backend.downloads[0][0].endswith("/video.mp4")
        assert "yt-dlp" in backend.commands[0]
        assert "/home/tenki/.tiktok-reposts/7412345678901234567" in backend.commands[0]
    finally:
        repost._cleanup(local.parent)


@pytest.mark.asyncio
async def test_download_still_gets_metadata_when_the_sandbox_has_no_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The base64 extractor needs python3 on the sandbox PATH; a plain bounded
    read has to cover the box that has none."""

    class _NoPython(_FakeBackend):
        async def run(self, command: str, *, timeout: int = 0) -> str:
            self.commands.append(command)
            if "base64 -d" in command:
                return "python3: command not found\n[exit_code=127]"
            if ".info.json" in command:
                return json.dumps({"description": "cap", "uploader": "u"}) + "\n[exit_code=0]"
            if "ls -1" in command:
                return f"{self._listing}\n[exit_code=0]"
            return "downloaded\n[exit_code=0]"

    executor = _FakeExecutor(_NoPython())

    async def _resolve() -> Any:
        return executor

    async def _root() -> str:
        return "/home/tenki"

    monkeypatch.setattr(
        "nanobot.agent.tools.workspace_bridge.resolve_remote_executor", _resolve
    )
    monkeypatch.setattr("nanobot.agent.tools.workspace_bridge.remote_workspace_root", _root)

    local, info = await download_in_sandbox(parse_tiktok_url(CANONICAL))
    try:
        assert info.get("uploader") == "u"
    finally:
        repost._cleanup(local.parent)


@pytest.mark.asyncio
async def test_download_reports_a_failed_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Failing(_FakeBackend):
        async def run(self, command: str, *, timeout: int = 0) -> str:
            return "ERROR: Unsupported URL\n[exit_code=1]"

    async def _resolve() -> Any:
        return _FakeExecutor(_Failing())

    async def _root() -> str:
        return "/home/tenki"

    monkeypatch.setattr(
        "nanobot.agent.tools.workspace_bridge.resolve_remote_executor", _resolve
    )
    monkeypatch.setattr("nanobot.agent.tools.workspace_bridge.remote_workspace_root", _root)

    with pytest.raises(TikTokRepostError) as caught:
        await download_in_sandbox(parse_tiktok_url(CANONICAL))
    assert caught.value.status == 502


@pytest.mark.asyncio
async def test_no_sandbox_is_a_clear_message(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Off(_FakeExecutor):
        available = False

    async def _resolve() -> Any:
        return _Off(_FakeBackend())

    monkeypatch.setattr(
        "nanobot.agent.tools.workspace_bridge.resolve_remote_executor", _resolve
    )
    with pytest.raises(TikTokRepostError) as caught:
        await download_in_sandbox(parse_tiktok_url(CANONICAL))
    assert caught.value.status == 503
    assert "sandbox" in caught.value.message.lower()


@pytest.mark.asyncio
async def test_a_sandbox_that_cannot_export_files_is_a_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Novita SDK shape has no backend.download; that must read as advice,
    not as an AttributeError."""

    class _Native:
        name = "novita"
        available = True
        backend = None

    async def _resolve() -> Any:
        return _Native()

    monkeypatch.setattr(
        "nanobot.agent.tools.workspace_bridge.resolve_remote_executor", _resolve
    )
    with pytest.raises(TikTokRepostError) as caught:
        await download_in_sandbox(parse_tiktok_url(CANONICAL))
    assert caught.value.status == 503
    assert "settings" in caught.value.message.lower()


# ---- the whole job -------------------------------------------------------


@pytest.mark.asyncio
async def test_repost_downloads_then_uploads_and_records_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ledger = RepostLedger(path=tmp_path / "ledger.json")
    uploaded: list[dict[str, Any]] = []

    async def _download(target: repost.TikTokTarget) -> tuple[Path, dict[str, Any]]:
        local = tmp_path / "video.mp4"
        local.write_bytes(b"v")
        return local, {"description": "my caption", "uploader": "groupie_tech", "webpage_url": CANONICAL}

    async def _upload(_token: str, path: Path, **kwargs: Any) -> str:
        uploaded.append({"path": str(path), **kwargs})
        return "yt999"

    monkeypatch.setattr(repost, "download_in_sandbox", _download)
    monkeypatch.setattr(repost, "upload_video", _upload)

    result = await repost_tiktok(access_token="tok", url=CANONICAL, ledger=ledger)

    assert result["status"] == "reposted"
    assert result["youtube_url"] == "https://youtu.be/yt999"
    assert uploaded[0]["title"] == "my caption"
    assert CANONICAL in uploaded[0]["description"]
    assert ledger.get("7412345678901234567")["youtube_video_id"] == "yt999"


@pytest.mark.asyncio
async def test_a_second_repost_of_the_same_video_uploads_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ledger = RepostLedger(path=tmp_path / "ledger.json")
    calls = {"download": 0, "upload": 0}

    async def _download(_target: repost.TikTokTarget) -> tuple[Path, dict[str, Any]]:
        calls["download"] += 1
        local = tmp_path / "video.mp4"
        local.write_bytes(b"v")
        return local, {}

    async def _upload(*_args: Any, **_kwargs: Any) -> str:
        calls["upload"] += 1
        return "yt1"

    monkeypatch.setattr(repost, "download_in_sandbox", _download)
    monkeypatch.setattr(repost, "upload_video", _upload)

    first = await repost_tiktok(access_token="t", url=CANONICAL, ledger=ledger)
    second = await repost_tiktok(access_token="t", url=CANONICAL, ledger=ledger)

    assert first["status"] == "reposted"
    assert second["status"] == "already_reposted"
    assert second["youtube_video_id"] == "yt1"
    assert calls == {"download": 1, "upload": 1}

    forced = await repost_tiktok(access_token="t", url=CANONICAL, ledger=ledger, force=True)
    assert forced["status"] == "reposted"
    assert calls == {"download": 2, "upload": 2}


@pytest.mark.asyncio
async def test_bad_privacy_is_refused_before_any_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("must not reach the sandbox")

    monkeypatch.setattr(repost, "download_in_sandbox", _boom)
    with pytest.raises(TikTokRepostError):
        await repost_tiktok(
            access_token="t",
            url=CANONICAL,
            privacy="everyone",
            ledger=RepostLedger(path=tmp_path / "l.json"),
        )


def test_metadata_prefers_the_real_caption_and_webpage_url() -> None:
    caption, author, url = repost.metadata_from_info(
        {"description": "real caption", "uploader": "@someone", "webpage_url": "https://tiktok.com/x"},
        parse_tiktok_url(CANONICAL),
    )
    assert caption == "real caption"
    assert author == "someone"
    assert url == "https://tiktok.com/x"


def test_metadata_falls_back_to_the_target() -> None:
    caption, author, url = repost.metadata_from_info({}, parse_tiktok_url(CANONICAL))
    assert caption == ""
    assert author == "groupie_tech"
    assert url == CANONICAL
