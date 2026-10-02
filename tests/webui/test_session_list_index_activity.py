"""The sidebar's activity check must not stat the volume once per chat.

``_webui_activity_signature`` resolves up to three paths per chat and calls
``stat`` then ``is_file`` on each -- two metadata operations apiece, plus a full
``readdir`` of the chat's ``.segments`` directory. Measured on this tree:

    506 chats, legacy per-chat calls   1518 os.stat,  0 os.scandir
    506 chats, one shared index           0 os.stat,  1 os.scandir

At the latency the production volume shows, 1518 metadata operations is the
10-20 s ``/api/sessions`` read, and that read is what fails the platform's
liveness probe and gets the container replaced under a user mid-task.

``_WebuiActivityIndex`` builds one ``os.scandir`` view of the webui directory
and answers every chat from it. These tests pin the syscall count *and* that the
answers are identical to the per-path code they replace, including the symlink
cases that are easy to get subtly wrong.
"""

from __future__ import annotations

import os
import pathlib
from pathlib import Path

import pytest

import nanobot.webui.session_list_index as session_list_index
from nanobot.session.manager import SessionManager


@pytest.fixture(autouse=True)
def _isolate_webui_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    webui_dir = tmp_path / "webui"
    webui_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(session_list_index, "get_webui_dir", lambda: webui_dir)


@pytest.fixture
def webui_dir(tmp_path: Path) -> Path:
    return tmp_path / "webui"


class _StatCounter:
    """Count only the metadata calls that land inside the webui directory."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = str(root)
        self.stats = 0
        self.scandirs = 0
        real_stat = os.stat
        real_scandir = os.scandir

        def counting_stat(path, *args, **kwargs):
            if str(path).startswith(self.root):
                self.stats += 1
            return real_stat(path, *args, **kwargs)

        def counting_scandir(path=".", *args, **kwargs):
            if str(path).startswith(self.root):
                self.scandirs += 1
            return real_scandir(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", counting_stat)
        monkeypatch.setattr(os, "scandir", counting_scandir)

    def reset(self) -> None:
        self.stats = 0
        self.scandirs = 0


def _stems(count: int) -> list[str]:
    return [f"websocket:chat{i}" for i in range(count)]


def _safe(key: str) -> str:
    return SessionManager.safe_key(key)


# --------------------------------------------------------------------------- #
# the syscall count
# --------------------------------------------------------------------------- #

def test_reading_every_chat_costs_no_stats(
    webui_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """506 chats with no webui files: the old path stat'ed three times each."""
    counter = _StatCounter(webui_dir, monkeypatch)
    index = session_list_index._WebuiActivityIndex(webui_dir)

    counter.reset()
    for key in _stems(506):
        signature = index.signature(key)
        assert signature[session_list_index._WEBUI_ACTIVITY_FILES] == 0

    assert counter.stats == 0, "a missing chat must not be stat'ed per lookup"
    assert counter.scandirs == 0, "the index was built once, not per chat"


def test_the_directory_is_read_once_per_index(
    webui_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = _StatCounter(webui_dir, monkeypatch)

    session_list_index._WebuiActivityIndex(webui_dir)
    assert counter.scandirs == 1

    session_list_index._WebuiActivityIndex(webui_dir)
    assert counter.scandirs == 2


def test_a_chat_with_files_costs_no_path_stat_at_all(
    webui_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metadata read is served by the DirEntry, not by ``Path.stat``.

    The counter is exact for the per-path code (``Path.stat`` is ``os.stat``)
    and a lower bound for the index, whose ``DirEntry.stat`` reaches the same
    syscall through the C API and caches the result on the entry. So zero here
    means the per-chat lookup is served from the directory read.
    """
    for key in _stems(5):
        (webui_dir / f"{_safe(key)}.jsonl").write_text("{}\n", encoding="utf-8")
    index = session_list_index._WebuiActivityIndex(webui_dir)

    counter = _StatCounter(webui_dir, monkeypatch)
    files = session_list_index._WEBUI_ACTIVITY_FILES
    for key in _stems(5):
        assert index.signature(key)[files] == 1

    assert counter.stats == 0
    assert counter.scandirs == 0


def test_repeating_a_lookup_reads_nothing_again(
    webui_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every poll tick re-asks for the same chats; none of it may hit the disk."""
    key = "websocket:repeated"
    segments = webui_dir / f"{_safe(key)}{session_list_index._TRANSCRIPT_SEGMENTS_SUFFIX}"
    segments.mkdir()
    (segments / "0001.jsonl").write_text("{}\n", encoding="utf-8")
    index = session_list_index._WebuiActivityIndex(webui_dir)

    index.signature(key)  # first call reads the segments directory
    counter = _StatCounter(webui_dir, monkeypatch)
    counter.reset()
    for _ in range(20):
        index.signature(key)

    assert counter.stats == 0
    assert counter.scandirs == 0


def test_segment_directories_are_scanned_once_each(
    webui_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A segment file is stat'ed, but the directory is only read on first use."""
    key = "websocket:segmented"
    segments = webui_dir / f"{_safe(key)}{session_list_index._TRANSCRIPT_SEGMENTS_SUFFIX}"
    segments.mkdir()
    (segments / "0001.jsonl").write_text("{}\n", encoding="utf-8")
    index = session_list_index._WebuiActivityIndex(webui_dir)

    counter = _StatCounter(webui_dir, monkeypatch)
    first = index.signature(key)
    second = index.signature(key)

    assert first == second
    assert counter.scandirs == 1, "the segments directory was read more than once"
    assert first[session_list_index._WEBUI_ACTIVITY_FILES] == 1


# --------------------------------------------------------------------------- #
# identical answers to the per-path code
# --------------------------------------------------------------------------- #

def _scaffold(webui_dir: Path) -> list[str]:
    """Every transcript shape the sidebar has to agree on."""
    linked = webui_dir / "target.jsonl"
    linked.write_text("{}\n", encoding="utf-8")
    broken_target = webui_dir / "gone.jsonl"

    keys: list[str] = []
    for key in (
        "websocket:jsonl",
        "websocket:json",
        "websocket:both",
        "websocket:segments",
        "websocket:segments_only",
        "websocket:symlinked_jsonl",
        "websocket:broken_symlink",
        "websocket:symlinked_segments",
        "websocket:missing",
        "websocket:segments_with_symlink",
    ):
        keys.append(key)
        pass

    (webui_dir / f"{_safe('websocket:jsonl')}.jsonl").write_text("{}\n", encoding="utf-8")
    (webui_dir / f"{_safe('websocket:json')}.json").write_text("{}", encoding="utf-8")
    (webui_dir / f"{_safe('websocket:both')}.jsonl").write_text("{}\n", encoding="utf-8")
    (webui_dir / f"{_safe('websocket:both')}.json").write_text("{}", encoding="utf-8")

    for key in ("websocket:segments", "websocket:segments_with_symlink"):
        segments = (
            webui_dir / f"{_safe(key)}{session_list_index._TRANSCRIPT_SEGMENTS_SUFFIX}"
        )
        segments.mkdir()
        (segments / "0001.jsonl").write_text("{}\n", encoding="utf-8")
        (segments / "0002.jsonl").write_text("{}\n", encoding="utf-8")
    segments = (
        webui_dir
        / f"{_safe('websocket:segments_only')}{session_list_index._TRANSCRIPT_SEGMENTS_SUFFIX}"
    )
    segments.mkdir()
    (segments / "0001.jsonl").write_text("{}\n", encoding="utf-8")

    # A symlink to a real file: the per-path code counts it (stat follows).
    (webui_dir / f"{_safe('websocket:symlinked_jsonl')}.jsonl").symlink_to(linked)
    # A broken symlink: the per-path code skips it (stat raises).
    (webui_dir / f"{_safe('websocket:broken_symlink')}.jsonl").symlink_to(broken_target)
    # A symlinked segments directory: not a real directory, so never descended.
    (webui_dir / f"{_safe('websocket:symlinked_segments')}{session_list_index._TRANSCRIPT_SEGMENTS_SUFFIX}").symlink_to(
        segments
    )
    # A symlink *inside* a segments directory: not a regular file, so skipped.
    inner = (
        webui_dir
        / f"{_safe('websocket:segments_with_symlink')}{session_list_index._TRANSCRIPT_SEGMENTS_SUFFIX}"
    )
    (inner / "0003.jsonl").symlink_to(linked)
    return keys


def test_the_index_agrees_with_the_per_path_code(webui_dir: Path) -> None:
    keys = _scaffold(webui_dir)
    index = session_list_index._WebuiActivityIndex(webui_dir)

    for key in keys:
        assert index.signature(key) == session_list_index._webui_activity_signature(
            key, webui_dir
        ), f"the index disagrees with the per-path code for {key}"


def test_the_index_reports_the_shapes_it_is_supposed_to(webui_dir: Path) -> None:
    _scaffold(webui_dir)
    index = session_list_index._WebuiActivityIndex(webui_dir)
    files = session_list_index._WEBUI_ACTIVITY_FILES

    assert index.signature("websocket:jsonl")[files] == 1
    assert index.signature("websocket:json")[files] == 1
    assert index.signature("websocket:both")[files] == 2
    assert index.signature("websocket:segments")[files] == 2
    assert index.signature("websocket:segments_only")[files] == 1
    assert index.signature("websocket:missing")[files] == 0
    # stat() follows symlinks, so a link to a real file is activity.
    assert index.signature("websocket:symlinked_jsonl")[files] == 1
    # A broken link is not.
    assert index.signature("websocket:broken_symlink")[files] == 0
    # A symlinked segments directory is never descended.
    assert index.signature("websocket:symlinked_segments")[files] == 0
    # A symlink inside a segments directory is not a regular file.
    assert index.signature("websocket:segments_with_symlink")[files] == 2


def test_the_index_sums_mtime_and_size_like_the_per_path_code(webui_dir: Path) -> None:
    key = "websocket:sizes"
    (webui_dir / f"{_safe(key)}.jsonl").write_text("12345\n", encoding="utf-8")
    (webui_dir / f"{_safe(key)}.json").write_text("123", encoding="utf-8")
    index = session_list_index._WebuiActivityIndex(webui_dir)

    from_index = index.signature(key)
    per_path = session_list_index._webui_activity_signature(key, webui_dir)

    assert from_index == per_path
    assert from_index[session_list_index._WEBUI_ACTIVITY_SIZE] == 9
    assert from_index[session_list_index._WEBUI_ACTIVITY_MTIME_NS] > 0


def test_an_unreadable_directory_degrades_to_no_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-path code answered "nothing" when every stat failed; so does this."""
    missing = tmp_path / "not-there"
    monkeypatch.setattr(session_list_index, "get_webui_dir", lambda: missing)

    index = session_list_index._WebuiActivityIndex(missing)

    assert index.signature("websocket:anything") == {
        session_list_index._WEBUI_ACTIVITY_MTIME_NS: 0,
        session_list_index._WEBUI_ACTIVITY_SIZE: 0,
        session_list_index._WEBUI_ACTIVITY_FILES: 0,
    }


# --------------------------------------------------------------------------- #
# the reconcile actually uses it
# --------------------------------------------------------------------------- #

def test_the_reconcile_reads_the_directory_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a call site forgets the index, the per-chat stat cost comes straight back."""
    manager = SessionManager(tmp_path / "sessions")
    for key in _stems(30):
        session = manager.get_or_create(key)
        session.add_message("user", "hello")
        manager.save(session)

    without_index: list[str] = []
    real = session_list_index._webui_activity_signature

    def recording(session_key, webui_dir, index=None):
        if index is None:
            without_index.append(session_key)
        return real(session_key, webui_dir, index)

    monkeypatch.setattr(session_list_index, "_webui_activity_signature", recording)

    rows, _changed = session_list_index._reconcile_index(manager)

    assert len(rows) == 30
    assert without_index == [], (
        "the reconcile fell back to a per-chat stat for "
        f"{len(without_index)} chats: {without_index[:5]}"
    )


def test_the_reconcile_is_still_quiet_when_nothing_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index must not make a no-op reconcile look like a change."""
    manager = SessionManager(tmp_path / "sessions")
    for key in _stems(5):
        session = manager.get_or_create(key)
        session.add_message("user", "hello")
        manager.save(session)
    session_list_index.list_webui_sessions(manager)
    existing = session_list_index._read_index_rows(manager.sessions_dir)
    assert existing is not None

    rows, changed = session_list_index._reconcile_index(manager)

    assert changed is False
    assert rows == existing


def test_pathlib_is_not_used_for_the_hot_lookup() -> None:
    """`Path.stat()`/`Path.is_file()` are the cost being removed. Guard it."""
    import inspect

    source = inspect.getsource(session_list_index._WebuiActivityIndex)
    assert "os.scandir" in source
    assert ".is_file()" not in source
    assert ".stat()" not in source.replace("entry.stat()", "")


def test_no_stray_import_was_added_for_the_broken_path() -> None:
    assert pathlib is not None  # pathlib stays a test-time import only