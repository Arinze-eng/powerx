"""Unit tests for ``scripts/supabase_cron_sync.py``.

These prove the cron-persistence layer behaves safely:

* backup uploads a changed jobs.json and skips an unchanged one.
* restore recovers the store when the local file is missing.
* restore does NOT clobber an active, non-empty local store.
* restore refuses invalid JSON from the cloud.
* these failures degrade to a warning (never crash the app boot).

The Supabase REST calls are faked through a stub httpx.Client so the tests
run offline and fast, while still covering the real method bodies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.supabase_cron_sync import (
    KEY_JOBS,
    KEY_META,
    MAX_SYNC_BYTES,
    CronSync,
)


class FakeClient:
    """Minimal httpx.Client stand-in backed by an in-memory dict."""

    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = store if store is not None else {}
        self.posts: list[tuple[str, str, str]] = []  # (key, value)

    def get(self, url: str, *, params: dict[str, Any] | None = None,
            headers: dict[str, Any] | None = None):
        key_filter = params.get("key") if params else None
        if key_filter and key_filter.startswith("eq."):
            key = key_filter[3:]
            value = self.store.get(key)
            body = [{"value": value}] if value is not None else []
        else:  # return everything
            body = [{"key": k, "value": v} for k, v in self.store.items()]
        return _Resp(200, body)

    def post(self, url: str, *, params: dict[str, Any] | None = None,
             json: dict[str, Any] | None = None,
             headers: dict[str, Any] | None = None):
        assert json is not None and json["key"] and isinstance(json["value"], str)
        self.store[json["key"]] = json["value"]
        self.posts.append((json["key"], json["value"]))
        return _Resp(201, [json])


class _Resp:
    def __init__(self, status_code: int, payload: list[dict[str, Any]]) -> None:
        self.status_code = status_code

        class _J:
            def json(self):
                return payload

        self._j = _J()

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self._j.json()


def _sync(tmp_path: Path, store: FakeClient | None = None):
    svc = CronSync(cron_store=str(tmp_path / "cron" / "jobs.json"),
                   url="https://unit.test", service_key="k")
    fake = store or FakeClient()
    svc._client = fake  # type: ignore[assignment]
    return svc, fake


def _write_jobs(tmp_path: Path, text: str) -> Path:
    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(text, encoding="utf-8")
    return store_path


def test_backup_uploads_changed_jobs(tmp_path: Path) -> None:
    svc, fake = _sync(tmp_path)
    _write_jobs(tmp_path, '{"version":1,"jobs":[]}')
    assert svc.backup() == 1
    assert fake.store.get(KEY_JOBS) == '{"version":1,"jobs":[]}'
    assert KEY_META in fake.store
    meta = json.loads(fake.store[KEY_META])
    assert meta["sha256"]


def test_backup_skips_unchanged_jobs(tmp_path: Path) -> None:
    svc, fake = _sync(tmp_path)
    store_path = _write_jobs(tmp_path, '{"version":1,"jobs":[]}')
    svc.backup(quiet=True)
    writes_before = len(fake.posts)
    # Re-write identical content -> no second upload.
    store_path.write_text('{"version":1,"jobs":[]}', encoding="utf-8")
    assert svc.backup(quiet=True) == 0
    assert len(fake.posts) == writes_before


def test_replace_upserts_after_change(tmp_path: Path) -> None:
    svc, fake = _sync(tmp_path)
    _write_jobs(tmp_path, '{"version":1,"jobs":[]}')
    svc.backup(quiet=True)
    _write_jobs(tmp_path, '{"version":1,"jobs":[{"id":"a"}]}')
    assert svc.backup(quiet=True) == 1
    assert fake.store[KEY_JOBS] == '{"version":1,"jobs":[{"id":"a"}]}'


def test_restore_recovers_store_when_local_missing(tmp_path: Path) -> None:
    svc, fake = _sync(tmp_path, FakeClient({KEY_JOBS: '{"version":1,"jobs":[{"id":"a"}]}'}))
    assert svc.restore() == 1
    store_path = tmp_path / "cron" / "jobs.json"
    assert store_path.exists()
    assert json.loads(store_path.read_text(encoding="utf-8"))["jobs"] == [{"id": "a"}]


def test_restore_does_not_clobber_fresh_local_store(tmp_path: Path) -> None:
    svc, fake = _sync(tmp_path, FakeClient({KEY_JOBS: '{"version":1,"jobs":[]}'}))
    _write_jobs(tmp_path, '{"version":1,"jobs":[{"id":"local"}]}')
    assert svc.restore() == 0  # nothing restored
    assert json.loads((tmp_path / "cron" / "jobs.json").read_text(
        encoding="utf-8"))["jobs"] == [{"id": "local"}]


def test_restore_refuses_invalid_json(tmp_path: Path) -> None:
    svc, fake = _sync(tmp_path, FakeClient({KEY_JOBS: "{not valid json"}))
    assert svc.restore() == 0
    assert not (tmp_path / "cron" / "jobs.json").exists()


def test_restore_refuses_oversized_value(tmp_path: Path) -> None:
    big = 'x' * (MAX_SYNC_BYTES + 1)
    svc, fake = _sync(tmp_path, FakeClient({KEY_JOBS: big}))
    assert svc.restore() == 0
    assert not (tmp_path / "cron" / "jobs.json").exists()


def test_missing_local_backup_is_noop(tmp_path: Path) -> None:
    svc, fake = _sync(tmp_path)
    assert not (tmp_path / "cron" / "jobs.json").exists()
    assert svc.backup() == -1
    assert KEY_JOBS not in fake.store
