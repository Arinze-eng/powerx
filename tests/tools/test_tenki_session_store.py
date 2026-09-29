"""The persisted half of Tenki key rotation.

A lane pin only earns its keep if it outlives the process: the whole point is
that the next chat turn — possibly in a different worker — looks for a session
in the workspace that actually holds its files. So these tests are about the
index file, and about the one operation that used to destroy it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.agent.tools.novita_sandbox import _TenkiSessionStore


def _store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _TenkiSessionStore:
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(tmp_path))
    return _TenkiSessionStore()


def _index(tmp_path: Path) -> Path:
    return tmp_path / "tenki_sessions.json"


def test_state_survives_a_reload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path)
    store.set_id("telegram:1", "sbx-1")
    store.set_lane("telegram:1", 1)
    store.set_id("telegram:2", "sbx-2")
    store.set_lane("telegram:2", 0)
    store.park(1, seconds=600)

    reloaded = _TenkiSessionStore()
    assert reloaded.sandbox_id("telegram:1") == "sbx-1"
    assert reloaded.lane("telegram:1") == 1
    assert reloaded.lane("telegram:2") == 0
    assert reloaded.parked() == {1}


def test_a_legacy_flat_map_still_loads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A deployment that predates lanes has a bare session -> id file."""
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    _index(tmp_path).write_text(
        json.dumps({"telegram:1": "sbx-1", "telegram:2": "sbx-2"}), encoding="utf-8"
    )
    store = _TenkiSessionStore()
    assert store.sandbox_id("telegram:1") == "sbx-1"
    # No lane was ever recorded for them, so they rotate freely from now on.
    assert store.lane("telegram:1") is None
    # …and the file is rewritten in the current shape on the next write.
    store.set_lane("telegram:1", 1)
    payload = json.loads(_index(tmp_path).read_text(encoding="utf-8"))
    assert payload["sessions"] == {"telegram:1": "sbx-1", "telegram:2": "sbx-2"}
    assert payload["lanes"] == {"telegram:1": 1}


def test_the_inherited_index_shape_is_not_mistaken_for_a_legacy_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: ``{"ids":…,"templates":…}`` used to load as a session called "ids"."""
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    _index(tmp_path).write_text(
        json.dumps({"ids": {"telegram:1": "sbx-1"}, "templates": {}}), encoding="utf-8"
    )
    store = _TenkiSessionStore()
    assert store.sandbox_id("telegram:1") == "sbx-1"
    assert "ids" not in store._ids


def test_remove_keeps_the_schema_and_drops_the_lane_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: ``remove`` used to overwrite the file with the parent schema.

    Every session id, lane pin, cursor and cooldown was lost on the first reset,
    so the next turn built a brand-new empty VM in an arbitrary workspace.
    """
    store = _store(monkeypatch, tmp_path)
    store.set_id("telegram:1", "sbx-1")
    store.set_lane("telegram:1", 1)
    store.set_id("telegram:2", "sbx-2")
    store.set_lane("telegram:2", 0)
    store.park(1, seconds=600)

    store.remove("telegram:1")

    payload = json.loads(_index(tmp_path).read_text(encoding="utf-8"))
    assert set(payload) == {"sessions", "lanes", "cursor", "parked"}
    assert payload["sessions"] == {"telegram:2": "sbx-2"}
    assert payload["lanes"] == {"telegram:2": 0}
    assert payload["parked"]

    reloaded = _TenkiSessionStore()
    assert reloaded.sandbox_id("telegram:1") is None
    assert reloaded.sandbox_id("telegram:2") == "sbx-2"
    assert reloaded.lane("telegram:2") == 0
    # The removed session must not keep steering new sessions at its old lane.
    assert reloaded.lane("telegram:1") is None


def test_next_lane_round_robins_and_steps_over_parked_lanes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(monkeypatch, tmp_path)
    assert [store.next_lane(3) for _ in range(6)] == [0, 1, 2, 0, 1, 2]

    store.park(1)
    assert store.parked() == {1}
    assert [store.next_lane(3) for _ in range(4)] == [0, 2, 2, 0]

    store.clear()
    assert store.parked() == set()


def test_parking_every_lane_still_offers_them_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(monkeypatch, tmp_path)
    for lane in range(3):
        store.park(lane)
    # A stale cooldown must never become "no lane was tried at all".
    assert sorted({store.next_lane(3) for _ in range(3)}) == [0, 1, 2]


def test_the_cursor_persists_across_restarts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(monkeypatch, tmp_path)
    assert store.next_lane(2) == 0

    # A restart must resume the order instead of stampeding lane 0 again.
    reloaded = _TenkiSessionStore()
    assert reloaded.next_lane(2) == 1


def test_a_single_lane_is_always_lane_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(monkeypatch, tmp_path)
    assert {store.next_lane(1) for _ in range(5)} == {0}


# --------------------------------------------------------------- Freestyle


def test_freestyle_keeps_its_own_index_beside_tenki(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression guard: the two rotations must never share a cursor or a pin.

    ``_FreestyleSessionStore`` subclasses the Tenki store to inherit the exact
    persistence semantics, so the index NAME is the only thing keeping a
    Freestyle lane pin out of the Tenki file. Sharing it would re-point a
    Freestyle session into whichever Tenki account happened to hold the lane of
    the same number.
    """
    from nanobot.agent.tools.novita_sandbox import _FreestyleSessionStore, _TenkiSessionStore

    monkeypatch.setenv("NANOBOT_DATA_DIR", str(tmp_path))
    assert _FreestyleSessionStore.INDEX_NAME == "freestyle_sessions.json"
    assert _TenkiSessionStore.INDEX_NAME == "tenki_sessions.json"

    tenki = _TenkiSessionStore()
    tenki.set_id("telegram:1", "tenki-sbx")
    tenki.set_lane("telegram:1", 1)

    freestyle = _FreestyleSessionStore()
    assert freestyle.sandbox_id("telegram:1") is None
    freestyle.set_id("telegram:1", "vm-1")
    freestyle.set_lane("telegram:1", 0)
    assert freestyle.next_lane(2) == 0

    # Each file keeps only its own lanes and cursor.
    assert sorted(p.name for p in tmp_path.glob("*_sessions.json")) == [
        "freestyle_sessions.json",
        "tenki_sessions.json",
    ]
    assert json.loads((tmp_path / "tenki_sessions.json").read_text())["lanes"] == {"telegram:1": 1}
    assert json.loads((tmp_path / "freestyle_sessions.json").read_text())["lanes"] == {
        "telegram:1": 0
    }
    assert _TenkiSessionStore().lane("telegram:1") == 1
    assert _FreestyleSessionStore().sandbox_id("telegram:1") == "vm-1"
