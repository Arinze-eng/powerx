# ruff: noqa: N803  -- fake S3 methods mirror boto3 keyword names (Bucket, Key, ...)
"""Tests for Cloudflare R2 attachment storage (boto3 S3 client, faked here)."""

from __future__ import annotations

import base64
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from nanobot.utils import r2_storage
from nanobot.utils.r2_storage import (
    DEFAULT_R2_ACCOUNT_ID,
    R2Storage,
    attachment_key,
    get_r2_storage,
    reset_r2_storage_for_tests,
    schedule_attachment_upload,
)


class FakeS3:
    def __init__(self, fail: bool = False) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.fail = fail

    def put_object(self, Bucket: str, Key: str, Body: bytes, **extra: Any) -> None:
        if self.fail:
            raise RuntimeError("network down")
        self.objects[(Bucket, Key)] = {"Body": Body, **extra}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        import io

        return {"Body": io.BytesIO(self.objects[(Bucket, Key)]["Body"])}

    def delete_object(self, Bucket: str, Key: str) -> None:
        self.objects.pop((Bucket, Key), None)

    def generate_presigned_url(self, op: str, Params: dict[str, Any], ExpiresIn: int) -> str:
        return f"https://signed.example/{Params['Key']}?expires={ExpiresIn}"


class InlinePool:
    """Runs submitted work immediately so tests can assert on the result."""

    def submit(self, fn: Any, *args: Any) -> None:
        fn(*args)


@pytest.fixture(autouse=True)
def _reset_storage(monkeypatch):
    for name in (
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET",
        "R2_ENDPOINT_URL",
        "R2_ACCOUNT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(r2_storage, "_upload_pool", None)
    reset_r2_storage_for_tests()
    yield
    reset_r2_storage_for_tests()


def _configure(monkeypatch, client: FakeS3) -> None:
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "cdnai")
    monkeypatch.setattr(r2_storage, "_build_storage", lambda: R2Storage(client, "cdnai"))
    reset_r2_storage_for_tests()


def test_not_configured_returns_none() -> None:
    assert get_r2_storage() is None


def test_attachment_key_is_dated_and_keeps_local_name() -> None:
    key = attachment_key("abc123def456_report final.pdf", uploaded_on=date(2026, 5, 1))
    assert key == "attachments/2026-05-01/abc123def456_report_final.pdf"


def test_attachment_key_defaults_name_when_empty() -> None:
    key = attachment_key("...", uploaded_on=date(2026, 5, 1))
    assert key == "attachments/2026-05-01/attachment.bin"


def test_put_get_delete_roundtrip() -> None:
    client = FakeS3()
    storage = R2Storage(client, "cdnai")
    assert storage.put_bytes("attachments/x.txt", b"hello", "text/plain") == "attachments/x.txt"
    assert client.objects[("cdnai", "attachments/x.txt")]["ContentType"] == "text/plain"
    assert storage.get_bytes("attachments/x.txt") == b"hello"
    storage.delete("attachments/x.txt")
    assert ("cdnai", "attachments/x.txt") not in client.objects


def test_presign_get_uses_expiry() -> None:
    storage = R2Storage(FakeS3(), "cdnai")
    assert storage.presign_get("k", expires=60) == "https://signed.example/k?expires=60"


def test_endpoint_defaults_to_powerx_account(monkeypatch) -> None:
    monkeypatch.delenv("R2_ENDPOINT_URL", raising=False)
    assert r2_storage._endpoint_url() == f"https://{DEFAULT_R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    monkeypatch.setenv("R2_ENDPOINT_URL", "https://example.test")
    assert r2_storage._endpoint_url() == "https://example.test"


def test_schedule_upload_is_noop_when_unconfigured() -> None:
    assert schedule_attachment_upload(b"data", "a.txt", "text/plain") is None


def test_schedule_upload_writes_object(monkeypatch) -> None:
    client = FakeS3()
    _configure(monkeypatch, client)
    monkeypatch.setattr(r2_storage, "_upload_pool", InlinePool())

    key = schedule_attachment_upload(b"payload", "abc_photo.png", "image/png")

    assert key is not None and key.endswith("/abc_photo.png")
    assert client.objects[("cdnai", key)]["Body"] == b"payload"
    assert client.objects[("cdnai", key)]["ContentType"] == "image/png"


def test_upload_failure_is_logged_not_raised(monkeypatch) -> None:
    _configure(monkeypatch, FakeS3(fail=True))
    monkeypatch.setattr(r2_storage, "_upload_pool", InlinePool())
    assert schedule_attachment_upload(b"x", "a.txt") is not None


def test_base64_upload_reaches_r2(monkeypatch, tmp_path) -> None:
    """save_base64_data_url mirrors the decoded bytes to R2 under the local name."""
    from nanobot.utils import media_decode

    client = FakeS3()
    _configure(monkeypatch, client)
    monkeypatch.setattr(r2_storage, "_upload_pool", InlinePool())

    raw = b"\x89PNG-test"
    url = "data:image/png;base64," + base64.b64encode(raw).decode()
    saved = media_decode.save_base64_data_url(url, tmp_path)

    assert saved is not None
    local_name = Path(saved).name
    keys = [k for (_b, k) in client.objects]
    assert len(keys) == 1
    assert keys[0].endswith("/" + local_name)
    assert client.objects[("cdnai", keys[0])]["Body"] == raw


def test_public_artifact_key_is_dated_and_unguessable() -> None:
    a = r2_storage.public_artifact_key("report.pdf", uploaded_on=date(2026, 5, 1))
    b = r2_storage.public_artifact_key("report.pdf", uploaded_on=date(2026, 5, 1))
    assert a.startswith("artifacts/2026-05-01/") and a.endswith("_report.pdf")
    assert a != b


def test_upload_public_artifact_returns_public_url(monkeypatch) -> None:
    client = FakeS3()
    _configure(monkeypatch, client)
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", "https://pub-test.r2.dev/")

    url = r2_storage.upload_public_artifact(b"pdf-bytes", "out.pdf", "application/pdf")

    key = url.removeprefix("https://pub-test.r2.dev/")
    assert key.startswith("artifacts/")
    assert client.objects[("cdnai", key)]["Body"] == b"pdf-bytes"


def test_upload_public_artifact_requires_public_url(monkeypatch) -> None:
    _configure(monkeypatch, FakeS3())
    monkeypatch.delenv("R2_PUBLIC_BASE_URL", raising=False)
    with pytest.raises(r2_storage.R2PublicUnavailableError):
        r2_storage.upload_public_artifact(b"x", "a.txt")


def test_mirror_failure_never_breaks_inbound_save(tmp_path, monkeypatch) -> None:
    """A missing boto3 (optional extra) must not reject the user's image."""
    import base64

    from nanobot.utils import r2_storage
    from nanobot.utils.media_decode import save_base64_data_url

    def boom(*_a, **_k):
        raise ModuleNotFoundError("No module named 'boto3'")

    monkeypatch.setattr(r2_storage, "_build_storage", boom)
    r2_storage.reset_r2_storage_for_tests()
    try:
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 16).decode()
        saved = save_base64_data_url(f"data:image/png;base64,{png}", tmp_path)
        assert saved is not None
        assert Path(saved).is_file()
    finally:
        r2_storage.reset_r2_storage_for_tests()
