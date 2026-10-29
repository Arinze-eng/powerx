"""Cloudflare R2 object storage for chat attachments.

R2 speaks the S3 API, so this wraps boto3. Configuration comes from the
environment:

- ``R2_ACCESS_KEY_ID`` and ``R2_SECRET_ACCESS_KEY``: API token credentials.
- ``R2_BUCKET``: bucket name.
- ``R2_ACCOUNT_ID`` (optional): defaults to the powerx Cloudflare account.
- ``R2_ENDPOINT_URL`` (optional): overrides the derived endpoint.

When credentials or the bucket are missing, :func:`get_r2_storage` returns
``None`` and callers keep working on local files only.
"""

from __future__ import annotations

import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any

from loguru import logger

DEFAULT_R2_ACCOUNT_ID = "ce347acbc0364bc9580eec21ea0ce627"
ATTACHMENT_PREFIX = "attachments"
PRESIGN_EXPIRES_SECONDS = 3600

_KEY_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _endpoint_url() -> str:
    explicit = _env("R2_ENDPOINT_URL")
    if explicit:
        return explicit
    account = _env("R2_ACCOUNT_ID") or DEFAULT_R2_ACCOUNT_ID
    return f"https://{account}.r2.cloudflarestorage.com"


def attachment_key(local_name: str, uploaded_on: date | None = None) -> str:
    """Object key for a chat attachment: ``attachments/<YYYY-MM-DD>/<local name>``.

    ``local_name`` already carries a random uuid prefix (see the upload code),
    so keys never collide and are not guessable from the filename alone.
    """
    day = (uploaded_on or datetime.now(UTC).date()).isoformat()
    name = _KEY_SAFE_RE.sub("_", local_name).strip("._")[:160] or "attachment.bin"
    return f"{ATTACHMENT_PREFIX}/{day}/{name}"


class R2Storage:
    """Thin wrapper over a boto3 S3 client pointed at an R2 bucket."""

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self.bucket = bucket

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        """Store ``data`` under ``key`` and return the key."""
        extra: dict[str, str] = {}
        if content_type:
            extra["ContentType"] = content_type
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        return key

    def get_bytes(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def presign_get(self, key: str, expires: int = PRESIGN_EXPIRES_SECONDS) -> str:
        """Return a time-limited download URL for ``key``."""
        url: str = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )
        return url


def _build_storage() -> R2Storage | None:
    access_key = _env("R2_ACCESS_KEY_ID")
    secret_key = _env("R2_SECRET_ACCESS_KEY")
    bucket = _env("R2_BUCKET")
    if not (access_key and secret_key and bucket):
        return None
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=_endpoint_url(),
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )
    return R2Storage(client, bucket)


@lru_cache(maxsize=1)
def get_r2_storage() -> R2Storage | None:
    """Return the shared R2 storage, or ``None`` when it is not configured."""
    try:
        storage = _build_storage()
    except Exception as exc:  # noqa: BLE001 - mirroring must never break ingest
        logger.warning("R2 storage unavailable; attachments stay local only: {}", exc)
        return None
    if storage is None:
        logger.debug("R2 storage not configured; attachments stay local only")
    return storage


PUBLIC_ARTIFACT_PREFIX = "artifacts"


class R2PublicUnavailableError(RuntimeError):
    """Raised when R2 or its public base URL is not configured."""


def public_base_url() -> str:
    """Public r2.dev (or custom domain) base URL for the bucket, no trailing slash."""
    return _env("R2_PUBLIC_BASE_URL").rstrip("/")


def public_artifact_key(local_name: str, uploaded_on: date | None = None) -> str:
    """Key for a file the agent produced: ``artifacts/<YYYY-MM-DD>/<name>``.

    The bucket is public, so the name carries a random uuid to keep the link
    hard to guess.
    """
    day = (uploaded_on or datetime.now(UTC).date()).isoformat()
    name = _KEY_SAFE_RE.sub("_", local_name).strip("._")[:160] or "artifact.bin"
    return f"{PUBLIC_ARTIFACT_PREFIX}/{day}/{uuid.uuid4().hex[:12]}_{name}"


def upload_public_artifact(data: bytes, filename: str, content_type: str | None = None) -> str:
    """Write a produced file to R2 and return its permanent public URL.

    Raises :class:`R2PublicUnavailableError` when R2 or ``R2_PUBLIC_BASE_URL``
    is not set, and lets upload errors propagate to the caller.
    """
    storage = get_r2_storage()
    base = public_base_url()
    if storage is None or not base:
        raise R2PublicUnavailableError("R2 public storage is not configured")
    key = public_artifact_key(filename)
    storage.put_bytes(key, data, content_type)
    return f"{base}/{key}"


_UPLOAD_WORKERS = 4
_upload_pool: ThreadPoolExecutor | None = None


def _upload_one(storage: R2Storage, key: str, data: bytes, content_type: str | None) -> None:
    try:
        storage.put_bytes(key, data, content_type)
    except Exception:
        logger.exception("R2 upload failed for {}", key)


def schedule_attachment_upload(
    data: bytes,
    local_name: str,
    content_type: str | None = None,
) -> str | None:
    """Copy an inbound attachment to R2 in the background.

    Returns the object key that will be written, or ``None`` when R2 is not
    configured. The upload runs on a small thread pool so the request that
    received the file never waits on the network; the local copy stays in
    place for the current turn. Failures are logged, not raised.
    """
    global _upload_pool
    try:
        storage = get_r2_storage()
        if storage is None:
            return None
        if _upload_pool is None:
            _upload_pool = ThreadPoolExecutor(
                max_workers=_UPLOAD_WORKERS, thread_name_prefix="r2-upload"
            )
        key = attachment_key(local_name)
        _upload_pool.submit(_upload_one, storage, key, data, content_type)
        return key
    except Exception as exc:  # noqa: BLE001 - the local copy is what the turn needs
        logger.warning("R2 mirror skipped for {}: {}", local_name, exc)
        return None


def reset_r2_storage_for_tests() -> None:
    get_r2_storage.cache_clear()
