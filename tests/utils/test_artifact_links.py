"""Tests for artifact-link durability and the permanent download gateway.

Two live bugs motivated these:

1. ``upload_bytes`` sent the caller's filename straight to onlyfiles, which
   rejects stems shorter than two characters with HTTP 422 ``ERROR_FILE_INVALID``
   (verified live: ``t.txt`` fails, ``ab.txt`` succeeds). Agent-generated
   artifacts are often short-named, so the upload failed and no link was produced.
2. The onlyfiles *page* URL is permanent but serves an HTML viewer, while the raw
   ``/dl/`` URL downloads but its token expires in ~2h. Handing out either one
   broke copy-paste: the user either got a web page or a dead link. ``/f/{id}``
   is a permanent redirector that mints a fresh raw token per request.

These tests are offline and must not touch the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.utils import file_share
from nanobot.utils.onlyfiles import (
    ArtifactLinkMemory,
    gateway_base_url,
    onlyfiles_file_id,
    permanent_download_url,
    safe_upload_filename,
)

# ---- filename sanitisation (the HTTP 422 fix) -----------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("APP-debug.apk", "APP-debug.apk"),
        # A 1-char stem is rejected by onlyfiles; it must be widened.
        ("t.txt", "file-t.txt"),
        ("a.txt", "file-a.txt"),
        ("x.bin", "file-x.bin"),
        # Spaces and punctuation are not safe in a filename.
        ("my file.txt", "my-file.txt"),
        ("2026!!.txt", "2026.txt"),
        # No extension at all is rejected; one is added.
        ("noext", "noext.bin"),
        ("", "powerx-file.bin"),
    ],
)
def test_safe_upload_filename_produces_acceptable_names(given: str, expected: str) -> None:
    assert safe_upload_filename(given) == expected


def test_safe_upload_filename_never_returns_short_stem() -> None:
    # Regression guard: every output must satisfy onlyfiles' >=2 char stem rule,
    # including pathological inputs.
    for given in ("a", "a.", ".x", "-", "..", "  ", "1", "!!", "a" * 300 + ".txt"):
        name = safe_upload_filename(given)
        assert "." in name, f"{given!r} produced extensionless {name!r}"
        stem = name.rsplit(".", 1)[0]
        assert len(stem) >= 2, f"{given!r} produced short stem {stem!r}"


def test_safe_upload_filename_strips_directory_components() -> None:
    assert safe_upload_filename("/etc/passwd") == "passwd.bin"
    assert safe_upload_filename("../../secret.txt") == "secret.txt"


# ---- permanent link construction -----------------------------------------


def test_onlyfiles_file_id_parses_page_and_dl_forms() -> None:
    assert onlyfiles_file_id("https://onlyfiles.com/ABC123def/file.apk") == "ABC123def"
    assert (
        onlyfiles_file_id("https://onlyfiles.com/dl/1789627686.abc123/ABC123def/file.apk")
        == "ABC123def"
    )
    assert onlyfiles_file_id("https://example.com/ABC123def/file.apk") == ""
    assert onlyfiles_file_id("https://onlyfiles.com/") == ""
    assert onlyfiles_file_id("") == ""


def test_permanent_download_url_uses_gateway_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POWERX_PUBLIC_URL", "https://gateway.example.com/")
    page = "https://onlyfiles.com/ABC123def/file.apk"
    # The gateway form is stable and downloads, unlike the HTML page URL.
    assert permanent_download_url(page) == "https://gateway.example.com/f/ABC123def"


def test_permanent_download_url_falls_back_to_page_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("POWERX_PUBLIC_URL", "NANOBOT_API_PUBLIC_URL", "API_SERVER_URL"):
        monkeypatch.delenv(var, raising=False)
    page = "https://onlyfiles.com/ABC123def/file.apk"
    assert gateway_base_url() == ""
    # Without a gateway the page URL is still the permanent, shareable form.
    assert permanent_download_url(page) == page


# ---- artifact link memory -------------------------------------------------


def test_artifact_memory_survives_a_new_instance(tmp_path: Path) -> None:
    """The point of the store: recall after the sandbox/process is gone."""
    first = ArtifactLinkMemory(root=tmp_path)
    first.remember(
        name="app-debug.apk",
        url="https://gateway.example.com/f/abc123",
        page_url="https://onlyfiles.com/abc123/app-debug.apk",
        description="debug build",
        kind="apk",
    )
    # A fresh instance reads from disk, exactly like a restarted process.
    second = ArtifactLinkMemory(root=tmp_path)
    hits = second.search("apk")
    assert len(hits) == 1
    assert hits[0]["name"] == "app-debug.apk"
    assert hits[0]["url"] == "https://gateway.example.com/f/abc123"
    assert hits[0]["description"] == "debug build"


def test_artifact_memory_stores_no_binary_payload(tmp_path: Path) -> None:
    """Only text metadata is persisted, so the disk cannot fill with artifacts."""
    memory = ArtifactLinkMemory(root=tmp_path)
    memory.remember(name="big.zip", url="https://gateway.example.com/f/x1", size=90_000_000)
    raw = (tmp_path / "links.json").read_text(encoding="utf-8")
    assert len(raw) < 1024
    assert "90000000" in raw  # size is a fact worth keeping
    records = json.loads(raw)
    assert set(records[0]) <= {"name", "description", "url", "page_url", "kind", "ts", "size"}


def test_artifact_memory_replaces_duplicate_instead_of_appending(tmp_path: Path) -> None:
    memory = ArtifactLinkMemory(root=tmp_path)
    for _ in range(3):
        memory.remember(name="same.apk", url="https://gateway.example.com/f/same")
    assert len(ArtifactLinkMemory(root=tmp_path).search()) == 1


def test_artifact_memory_search_matches_multiple_terms(tmp_path: Path) -> None:
    memory = ArtifactLinkMemory(root=tmp_path)
    memory.remember(name="release.apk", url="https://g/f/1", description="signed release build")
    memory.remember(name="debug.apk", url="https://g/f/2", description="debug build")
    names = [r["name"] for r in memory.search("signed release")]
    assert names == ["release.apk"]


def test_artifact_memory_tolerates_corrupt_file(tmp_path: Path) -> None:
    (tmp_path / "links.json").write_text("not json at all", encoding="utf-8")
    assert ArtifactLinkMemory(root=tmp_path).search() == []


def test_artifact_memory_prunes_to_max_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ArtifactLinkMemory, "MAX_RECORDS", 3)
    memory = ArtifactLinkMemory(root=tmp_path)
    for index in range(6):
        memory.remember(name=f"f{index}.bin", url=f"https://g/f/{index}")
    records = ArtifactLinkMemory(root=tmp_path).search(limit=100)
    assert len(records) == 3
    # Newest are kept.
    assert records[0]["name"] == "f5.bin"


# ---- file_share contract --------------------------------------------------


def test_normalize_onlyfiles_hands_out_the_permanent_link() -> None:
    """`url` must be paste-and-download, never the expiring raw token."""
    out = file_share._normalize_onlyfiles(
        {
            "url": "https://gateway.example.com/f/abc123",
            "page_url": "https://onlyfiles.com/abc123/file.apk",
            "download_url": "https://onlyfiles.com/dl/1789627686.abc/abc123/file.apk",
        }
    )
    assert out["url"] == "https://gateway.example.com/f/abc123"
    assert out["page_url"] == "https://onlyfiles.com/abc123/file.apk"
    # The expiring token is exposed separately, for immediate use only.
    assert out["download_url"].startswith("https://onlyfiles.com/dl/")


def test_normalize_onlyfiles_never_prefers_expiring_token() -> None:
    out = file_share._normalize_onlyfiles(
        {
            "url": "",
            "download_url": "https://onlyfiles.com/dl/1.a/abc/file.apk",
        }
    )
    assert out["url"] == "https://onlyfiles.com/dl/1.a/abc/file.apk"  # last resort only


def test_remember_artifact_persists_via_persistent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nanobot.utils import onlyfiles

    monkeypatch.setattr(onlyfiles, "get_persistent_data_dir", lambda _ns=None: tmp_path)
    file_share.remember_artifact(
        {"url": "https://gateway.example.com/f/zzz", "page_url": "https://onlyfiles.com/zzz/a.apk"},
        filename="a.apk",
        description="demo",
    )
    hits = onlyfiles.ArtifactLinkMemory(root=tmp_path).search("demo")
    assert hits and hits[0]["url"] == "https://gateway.example.com/f/zzz"


def test_remember_artifact_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memory is best-effort: a failure must not break a delivery."""
    from nanobot.utils import onlyfiles

    def _boom() -> object:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(onlyfiles, "artifact_memory", _boom)
    file_share.remember_artifact({"url": "https://g/f/1"}, filename="a.bin")
