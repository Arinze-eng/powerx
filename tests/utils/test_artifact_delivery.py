"""The delivery contract: one link, permanent, and it is an onlyfiles.com link.

Reported live (2026-09-30, from the deployed service while a user worked in a
sandbox): *"The link is not working use onlyfiles"*, and then, once the gateway
link was working again: *"llm should stop using
https://<deployment-host>/f/<id> ... it should use onlyfiles"*. The delivery
policy is therefore: the link handed to the user is the permanent onlyfiles page
URL, and nothing else.

Verified against the live service while writing this (2026-09-30):

* ``POST https://api.onlyfiles.com/v1/upload`` with ``expire=0`` (documented at
  https://onlyfiles.com/api) returns ``https://onlyfiles.com/<id>/<name>``, and
  that page URL answers HTTP 200 and embeds a working download control.
* the raw ``/dl/<ts.nonce>/<id>/<name>`` token embedded in that page is minted
  with a timestamp exactly **300 s** ahead of the moment it was issued, and a
  fresh one is issued for every page view — so a stored raw link is dead within
  minutes, which is why it is never delivered.

Three defects produced links that could not be handed out, and all are covered
here: the gateway base URL carrying a path (``https://<host>/admin/f/<id>``, which
the SPA answered with its own HTML), the expiring raw token being offered as the
"direct download" link, and the deployment-host ``/f/<id>`` redirect being offered
at all — it resolves only while that deployment answers on that host.

Offline: no network is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from nanobot.agent.tools import novita_sandbox
from nanobot.utils import file_share, onlyfiles
from nanobot.utils.file_share import artifact_delivery_text
from nanobot.utils.onlyfiles import (
    gateway_base_url,
    gateway_download_url,
    permanent_public_url,
)

PAGE = "https://onlyfiles.com/ABC123def/app-debug.apk"
RAW = "https://onlyfiles.com/dl/1790755869.f9f6b984/ABC123def/app-debug.apk"
GATEWAY = "https://http--powerx--mxq9vl6k966n.code.run/f/w7wLwZg3IpKk"


def _clear_gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("POWERX_PUBLIC_URL", "NANOBOT_API_PUBLIC_URL", "API_SERVER_URL"):
        monkeypatch.delenv(var, raising=False)


# ---- the gateway helpers still exist, but are not the delivered link ------


def test_gateway_base_url_drops_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact production value that produced /admin/f/<id>."""
    monkeypatch.setenv("NANOBOT_API_PUBLIC_URL", "https://host.code.run/admin")
    assert gateway_base_url() == "https://host.code.run"
    assert gateway_download_url(PAGE) == "https://host.code.run/f/ABC123def"


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


# ---- the delivered link is the onlyfiles page URL, always ----------------


def test_permanent_public_url_is_the_onlyfiles_page_url_even_with_a_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user's ask: stop handing out /f/<id>, hand out onlyfiles."""
    monkeypatch.setenv("POWERX_PUBLIC_URL", "https://gateway.example.com/admin")
    assert gateway_download_url(PAGE) == "https://gateway.example.com/f/ABC123def"
    assert permanent_public_url(PAGE) == PAGE


def test_permanent_public_url_keeps_a_usable_link_for_a_foreign_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_gateway_env(monkeypatch)
    # Not an onlyfiles URL and no gateway: never return "" — the caller's own
    # URL is the best available link.
    assert permanent_public_url("https://example.com/x.apk") == "https://example.com/x.apk"


def test_normalize_onlyfiles_delivers_the_page_url_not_the_gateway() -> None:
    out = file_share._normalize_onlyfiles(
        {
            "url": GATEWAY,
            "page_url": PAGE,
            "download_url": RAW,
            "gateway_url": GATEWAY,
        }
    )
    assert out["url"] == PAGE
    assert out["page_url"] == PAGE
    assert out["download_url"] == RAW
    assert out["gateway_url"] == GATEWAY


def test_normalize_onlyfiles_never_prefers_the_expiring_token() -> None:
    out = file_share._normalize_onlyfiles(
        {"url": "", "download_url": "https://onlyfiles.com/dl/1.a/abc/file.apk"}
    )
    # Nothing permanent exists, so the token is the last resort rather than an
    # empty link — and it is still never chosen over the gateway redirect.
    assert out["url"] == "https://onlyfiles.com/dl/1.a/abc/file.apk"


def test_delivery_hands_over_the_onlyfiles_link_and_hides_the_alternatives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact production shape: a gateway was configured AND a token minted."""
    monkeypatch.setenv("POWERX_PUBLIC_URL", "https://http--powerx--mxq9vl6k966n.code.run/admin")
    shared = file_share._normalize_onlyfiles(
        {
            "url": GATEWAY,
            "page_url": PAGE,
            "download_url": RAW,
            "gateway_url": GATEWAY,
        }
    )
    text = artifact_delivery_text(shared, "/tmp/downloads/app-debug.apk")

    assert text.count(PAGE) == 1
    # The deployment-host link must not appear at all...
    assert "code.run" not in text
    assert "/f/w7wLwZg3IpKk" not in text
    # ...nor the token that dies in five minutes.
    assert "/dl/" not in text
    assert "expires in about two hours" not in text
    assert text.index(PAGE) < text.index("Give the user")


def test_delivery_text_forbids_substitutes_in_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """The model is told explicitly which links are banned, not just which is right."""
    _clear_gateway_env(monkeypatch)
    shared = file_share._normalize_onlyfiles({"url": PAGE, "page_url": PAGE, "download_url": RAW})
    text = artifact_delivery_text(shared, "/tmp/downloads/app-debug.apk")
    assert "gateway /f/ link" in text
    assert "signed URL" in text


def test_delivery_text_never_mentions_the_local_path_as_a_link() -> None:
    """The model must give a URL, not a host path it also cannot read."""
    shared = file_share._normalize_onlyfiles(
        {"url": GATEWAY, "page_url": PAGE, "download_url": RAW, "gateway_url": GATEWAY}
    )
    text = artifact_delivery_text(shared, "/home/user/.nanobot/artifacts/x.apk")
    link_lines = [ln for ln in text.splitlines() if ln.startswith("http")]
    assert link_lines == [PAGE]


def test_delivery_without_any_link_still_explains_itself() -> None:
    text = artifact_delivery_text({"host": "onlyfiles"}, "/tmp/x.bin")
    assert "Download link" not in text
    assert "Give the user THIS link" in text


def test_delivery_catbox_url_is_permanent_and_direct() -> None:
    link = "https://files.catbox.moe/abcd1234.zip"
    shared = file_share._normalize_catbox(link)
    text = artifact_delivery_text(shared, "/tmp/downloads/big.zip")
    assert link in text
    assert text.count(link) == 1
    assert "permanent, valid forever" in text
    assert "catbox" in text
    assert shared["gateway_url"] == ""


# ---- the delivery link is what gets remembered ---------------------------


def test_remember_artifact_stores_the_onlyfiles_page_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onlyfiles, "get_persistent_data_dir", lambda _ns=None: tmp_path)
    file_share.remember_artifact(
        {"url": GATEWAY, "page_url": PAGE, "gateway_url": GATEWAY},
        filename="a.apk",
        description="demo",
    )
    hits = onlyfiles.ArtifactLinkMemory(root=tmp_path).search("demo")
    assert hits and hits[0]["url"] == PAGE


# ---- Tenki and Freestyle deliveries use the same contract ----------------


class _FakeDownloadBackend:
    """Stands in for a Tenki/Freestyle backend: only ``download`` is exercised."""

    last_session_id = ""

    def __init__(self, tmp_path: Path) -> None:
        self._tmp = tmp_path
        self.downloaded: list[tuple[str, str]] = []

    async def download(self, remote_path: str, destination: Any) -> Any:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"apk-bytes")
        self.downloaded.append((remote_path, str(target)))
        return str(target)


def _wire_delivery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Publish through onlyfiles' real normalization, with no network call."""
    captured: dict[str, Any] = {}

    async def _publish(path: Any, **kwargs: Any) -> dict[str, Any]:
        captured["path"] = str(path)
        captured["data"] = Path(path).read_bytes()
        return file_share._normalize_onlyfiles(
            {"url": GATEWAY, "page_url": PAGE, "download_url": RAW, "gateway_url": GATEWAY}
        )

    monkeypatch.setattr(novita_sandbox, "upload_shared_artifact", _publish)
    monkeypatch.setattr(
        novita_sandbox.NovitaSandboxTool,
        "_artifact_destination",
        staticmethod(lambda remote_path: tmp_path / "artifacts" / Path(remote_path).name),
    )
    return captured


@pytest.mark.parametrize("backend", ["tenki", "freestyle"])
async def test_tenki_and_freestyle_download_url_publish_the_onlyfiles_link(
    backend: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The user asked for this by name: a finished file from Tenki/Freestyle."""
    monkeypatch.setenv("POWERX_PUBLIC_URL", GATEWAY.rsplit("/f/", 1)[0] + "/admin")
    fake = _FakeDownloadBackend(tmp_path)
    monkeypatch.setattr(
        novita_sandbox.NovitaSandboxTool, f"_{backend}_backend", lambda self, config, key: fake
    )
    captured = _wire_delivery(monkeypatch, tmp_path)

    tool = novita_sandbox.NovitaSandboxTool()
    inner = getattr(tool, f"_execute_{backend}_inner")
    result = await inner("download_url", {"path": "app-debug.apk"}, object(), "session-1")
    text = result if isinstance(result, str) else result.content

    assert fake.downloaded and fake.downloaded[0][0] == "app-debug.apk"
    assert captured["data"] == b"apk-bytes"
    assert PAGE in text
    assert "code.run" not in text
    assert "/dl/" not in text


# ---- the Novita signed-URL path must republish, not hand out -------------


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def text(self) -> str:
        return self._body.decode("utf-8", "replace")

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
        return file_share._normalize_onlyfiles(
            {
                "url": GATEWAY,
                "page_url": "https://onlyfiles.com/zzz/" + filename,
                "download_url": "https://onlyfiles.com/dl/1.a/zzz/" + filename,
                "gateway_url": GATEWAY,
            }
        )

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
    assert "https://onlyfiles.com/zzz/app-debug.apk" in text
    assert "code.run" not in text
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


# ---- the model-facing instructions carry the same rule -------------------

_ROOT = Path(__file__).resolve().parents[2]


def test_sandbox_workspace_template_teaches_the_onlyfiles_rule() -> None:
    """The prompt the model reads before any sandbox task states the policy."""
    template = (
        _ROOT / "nanobot" / "templates" / "agent" / "sandbox_workspace.md"
    ).read_text(encoding="utf-8")
    assert "Delivering a finished file" in template
    assert "onlyfiles.com" in template
    assert "NEVER hand over a `<this-deployment-host>/f/<id>` link" in template
    assert '"action":"download_url"' in template


def test_sandbox_build_skill_teaches_the_onlyfiles_rule() -> None:
    skill = (
        _ROOT / "nanobot" / "skills" / "sandbox-build-environment" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "Delivering the finished file" in skill
    assert "onlyfiles.com/api" in skill
    assert "expire=0" in skill
    assert "/f/<id>" in skill


def test_sandbox_tool_description_teaches_the_onlyfiles_rule() -> None:
    description = novita_sandbox.NovitaSandboxTool().description
    assert "onlyfiles.com" in description
    assert "never substitute another one" in description
    assert "not a gateway /f/ link" in description


def test_durable_artifact_section_replays_onlyfiles_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old records carry the deployment link; the prompt must not replay it."""
    from nanobot.agent import context as context_mod

    class _Memory:
        def search(self, limit: int = 10) -> list[dict[str, Any]]:
            return [
                {
                    "name": "app-debug.apk",
                    "url": GATEWAY,
                    "page_url": PAGE,
                    "kind": "apk",
                    "description": "debug build",
                }
            ]

    monkeypatch.setattr(onlyfiles, "artifact_memory", lambda: _Memory())
    holder = SimpleNamespace(_DURABLE_ARTIFACT_LIMIT=12)
    section = context_mod.ContextBuilder._build_durable_artifacts_section(holder)

    assert PAGE in section
    assert GATEWAY not in section
    assert "never replace it with a gateway /f/ link" in section


# ---- the documented upload contract itself -------------------------------


async def test_upload_bytes_publishes_the_page_url_as_the_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``url`` (the link handed to the user) is onlyfiles' own page URL."""
    body = json.dumps(
        {
            "status": True,
            "data": {
                "file": {
                    "url": {"full": PAGE, "short": "https://onlyfiles.com/ABC123def"},
                    "metadata": {"id": "ABC123def", "name": "app-debug.apk"},
                }
            },
        }
    ).encode()

    class _PostSession:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_PostSession":
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        def post(self, url: str, data: Any = None) -> _FakeResponse:
            _PostSession.last_url = url
            return _FakeResponse(200, body)

    monkeypatch.setattr(onlyfiles.aiohttp, "ClientSession", lambda *a, **kw: _PostSession())

    async def _no_resolve(page_url: str, **kwargs: Any) -> str:
        return RAW

    monkeypatch.setattr(onlyfiles, "resolve_raw_url", _no_resolve)
    out = await onlyfiles.upload_bytes(b"apk-bytes", filename="app-debug.apk")

    assert _PostSession.last_url == onlyfiles.ONLYFILES_UPLOAD_URL
    assert out["url"] == PAGE
    assert out["page_url"] == PAGE
    assert out["download_url"] == RAW
