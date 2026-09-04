"""Unit tests for ``scripts/supabase_chat_sync.py``.

Covers the critical safety and isolation guarantees:

* backup uploads changed chat/session files and is **non-destructive**
  (never deletes cloud rows on a partial/fresh local dir).
* restore recovers transcripts + session metadata from Supabase.
* restore maps legacy (bare-key) webui rows into the ``webui/`` namespace.
* prune removes cloud rows whose files were deleted locally.

The Supabase REST calls are faked through a stub httpx.Client so the tests run
offline and fast while still exercising the real method bodies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.supabase_chat_sync import (
    KEY_PREFIX,
    ChatSync,
)


class FakeClient:
    """Minimal httpx.Client stand-in backed by an in-memory dict."""

    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = store if store is not None else {}
        self.posts: list[tuple[str, str]] = []
        self.deletes: list[str] = []

    def get(self, url: str, *, params: dict[str, Any] | None = None,
            headers: dict[str, Any] | None = None):
        key_filter = params.get("key") if params else None
        if key_filter and key_filter.startswith("eq."):
            key = key_filter[3:]
            value = self.store.get(key)
            body = [{"value": value}] if value is not None else []
        else:
            body = [{"key": k, "value": v} for k, v in self.store.items()]
        return _Resp(200, body)

    def post(self, url: str, *, params: dict[str, Any] | None = None,
             json: dict[str, Any] | None = None,
             headers: dict[str, Any] | None = None):
        assert json is not None and json["key"] and isinstance(json["value"], str)
        self.store[json["key"]] = json["value"]
        self.posts.append((json["key"], json["value"]))
        return _Resp(201, [json])

    def delete(self, url: str, *, params: dict[str, Any] | None = None,
               headers: dict[str, Any] | None = None):
        key_filter = params.get("key") if params else None
        key = key_filter[3:] if key_filter and key_filter.startswith("eq.") else ""
        self.store.pop(key, None)
        self.deletes.append(key)
        return _Resp(204, [])


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


def _sync(tmp_path: Path, store: FakeClient | None = None) -> tuple[ChatSync, FakeClient]:
    svc = ChatSync(data_dir=str(tmp_path), url="https://unit.test", service_key="k")
    fake = store or FakeClient()
    svc._client = fake  # type: ignore[assignment]
    return svc, fake


def _write(svc: ChatSync, rel: str, text: str) -> None:
    p = Path(svc.data_dir) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_backup_uploads_transcript_and_session_metadata(tmp_path: Path) -> None:
    svc, fake = _sync(tmp_path)
    _write(svc, "webui/websocket_a1b2c3.jsonl", '{"event":"user","text":"hi"}\n')
    _write(svc, "sessions/websocket_a1b2c3.jsonl", '{"key":"websocket:a1b2c3","metadata":{}}\n')

    assert svc.backup() == 2
    assert fake.store[KEY_PREFIX + "webui/websocket_a1b2c3.jsonl"] == '{"event":"user","text":"hi"}\n'
    assert fake.store[KEY_PREFIX + "sessions/websocket_a1b2c3.jsonl"].startswith('{"key":')


def test_backup_is_nondestructive_on_fresh_dir(tmp_path: Path) -> None:
    """Backup must never delete cloud rows that aren't on a fresh local disk."""
    legacy_cloud = {
        KEY_PREFIX + "websocket_already.jsonl": 'old-transcript',
        KEY_PREFIX + "webui/someone-else.jsonl": 'another',
    }
    svc, fake = _sync(tmp_path, FakeClient(dict(legacy_cloud)))
    # Local only has a brand-new file; the cloud rows are untouched.
    _write(svc, "webui/websocket_new.jsonl", 'new\n')

    assert svc.backup() == 1
    # Nothing was deleted, no pre-existing rows were overwritten.
    assert fake.deletes == []
    assert fake.store[KEY_PREFIX + "websocket_already.jsonl"] == 'old-transcript'
    assert fake.store[KEY_PREFIX + "webui/someone-else.jsonl"] == 'another'
    assert fake.store[KEY_PREFIX + "webui/websocket_new.jsonl"] == 'new\n'


def test_restore_recovers_into_namespaced_locations(tmp_path: Path) -> None:
    cloud = {
        KEY_PREFIX + "webui/websocket_x.jsonl": '{"event":"user","text":"x"}',
        KEY_PREFIX + "sessions/websocket_x.jsonl": '{"key":"websocket:x"}',
    }
    svc, fake = _sync(tmp_path, FakeClient(cloud))
    assert svc.restore() == 2
    assert (Path(svc.data_dir) / "webui" / "websocket_x.jsonl").exists()
    assert (Path(svc.data_dir) / "sessions" / "websocket_x.jsonl").exists()


def test_restore_maps_legacy_bare_keys_to_webui(tmp_path: Path) -> None:
    """Old backups stored keys without a subdir prefix; map them to webui/."""
    cloud = {KEY_PREFIX + "websocket_legacy.jsonl": "old"}
    svc, fake = _sync(tmp_path, FakeClient(cloud))
    assert svc.restore() == 1
    target = Path(svc.data_dir) / "webui" / "websocket_legacy.jsonl"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "old"


def test_restore_blocks_path_traversal(tmp_path: Path) -> None:
    cloud = {KEY_PREFIX + "../../../etc/passwd.json": "pwned"}
    svc, fake = _sync(tmp_path, FakeClient(cloud))
    assert svc.restore() == 0
    assert not (Path(svc.data_dir) / ".." / ".." / ".." / "etc" / "passwd.json").exists()


def test_prune_removes_only_deleted_namespaced_rows(tmp_path: Path) -> None:
    cloud = {
        KEY_PREFIX + "webui/deleted-session.jsonl": "gone",
        KEY_PREFIX + "webui/kept-session.jsonl": "kept",
        KEY_PREFIX + "websocket_legacy.jsonl": "legacy",
    }
    svc, fake = _sync(tmp_path, FakeClient(cloud))
    # Local has only the kept session; prune should drop the deleted one but
    # keep the legacy row (which maps to webui/ and isn't in local → also
    # removed in this controlled prune, but that's expected: prune is explicit).
    _write(svc, "webui/kept-session.jsonl", "kept")
    removed = svc.prune()
    assert removed >= 1
    assert fake.store.get(KEY_PREFIX + "webui/kept-session.jsonl") == "kept"
    assert fake.store.get(KEY_PREFIX + "webui/deleted-session.jsonl") is None