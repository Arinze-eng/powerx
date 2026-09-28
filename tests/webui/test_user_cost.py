"""The per-user cost meter: isolation, honest absence, and day bucketing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from nanobot.webui import user_cost
from nanobot.webui.user_cost import (
    COUNTER_KEYS,
    clean_user_id,
    record_user_cost,
    user_cost_payload,
    user_cost_state_path,
)


@pytest.fixture(autouse=True)
def _webui_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(user_cost, "get_webui_dir", lambda: tmp_path / "webui")
    return tmp_path / "webui"


def _counters(**overrides: int) -> dict[str, int]:
    row = {key: 0 for key in COUNTER_KEYS}
    row.update(overrides)
    return row


def test_a_users_meter_shows_only_that_users_numbers() -> None:
    """The read is by key, so one user's rows cannot surface in another's meter.

    This is the whole reason the store is keyed by identity instead of reusing
    the workspace-scoped token-usage file, which has no identity column at all.
    """
    record_user_cost("user-AAA", _counters(turns=2, api_calls=3, commands=40), scope="webui")
    record_user_cost("user-BBB", _counters(turns=1, api_calls=9, commands=1), scope="api")

    mine = user_cost_payload("user-AAA", timezone_name="UTC")
    theirs = user_cost_payload("user-BBB", timezone_name="UTC")

    assert mine["totals"]["api_calls"] == 3
    assert mine["totals"]["commands"] == 40
    assert theirs["totals"]["api_calls"] == 9
    assert theirs["totals"]["commands"] == 1
    assert set(mine["scopes"]) == {"webui"}
    assert set(theirs["scopes"]) == {"api"}


def test_an_unnamed_caller_gets_an_unmetered_payload_not_zeros() -> None:
    """"No meter for you" and "a meter reading zero" must not look the same.

    A row of zeros for a caller we could not name reads as "this user has spent
    nothing", which is a claim we cannot make.
    """
    record_user_cost("user-AAA", _counters(turns=1, api_calls=5), scope="webui")

    for unnamed in ("", None, "   "):
        payload = user_cost_payload(unnamed, timezone_name="UTC")
        assert payload["metered"] is False
        assert payload["totals"]["api_calls"] == 0
        assert payload["days"] == []
        assert payload["first_seen"] is None


def test_a_named_user_with_no_history_is_metered_and_empty() -> None:
    """Metred-but-empty is a different answer again, and has to be available."""
    payload = user_cost_payload("user-NEW", timezone_name="UTC")

    assert payload["metered"] is True
    assert payload["totals"]["api_calls"] == 0
    assert payload["efficiency"]["commands_per_api_call"] is None


def test_counters_accumulate_across_turns_and_days() -> None:
    """Two turns land on one row; a new day opens its own bucket."""
    day_one = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    day_two = day_one + timedelta(days=1)

    record_user_cost(
        "user-AAA", _counters(turns=1, api_calls=2, commands=10), scope="webui", now=day_one
    )
    record_user_cost(
        "user-AAA", _counters(turns=1, api_calls=1, commands=5, files=2), scope="webui", now=day_two
    )

    payload = user_cost_payload("user-AAA", timezone_name="UTC", now=day_two)
    assert payload["totals"]["turns"] == 2
    assert payload["totals"]["api_calls"] == 3
    assert payload["totals"]["commands"] == 15
    assert [row["date"] for row in payload["days"]] == ["2026-06-01", "2026-06-02"]
    assert payload["days"][1]["files"] == 2
    assert payload["today"] == dict(_counters(turns=1, api_calls=1, commands=5, files=2))


def test_the_day_bucket_follows_the_configured_timezone_not_utc() -> None:
    """A user in UTC+9 must not have their evening filed under tomorrow."""
    # 2026-06-02T16:30Z is 2026-06-03T01:30 in Asia/Tokyo.
    moment = datetime(2026, 6, 2, 16, 30, tzinfo=timezone.utc)

    record_user_cost(
        "user-AAA", _counters(turns=1), scope="webui", now=moment, timezone_name="Asia/Tokyo"
    )

    tokyo = user_cost.read_user_cost_state()["users"]["user-AAA"]["days"]

    record_user_cost(
        "user-AAA", _counters(turns=1), scope="webui", now=moment, timezone_name="UTC"
    )
    both = user_cost.read_user_cost_state()["users"]["user-AAA"]["days"]

    assert list(tokyo) == ["2026-06-03"]
    assert sorted(both) == ["2026-06-02", "2026-06-03"]


def test_the_ratio_is_absent_rather_than_invented_when_no_call_was_made() -> None:
    """One model call driving many commands is the point; zero calls is not a ratio.

    Returning 0.0 or infinity would print a number that means nothing, so the
    meter reports ``None`` and lets the surface stay silent.
    """
    record_user_cost(
        "user-AAA", _counters(turns=1, api_calls=0, commands=12, pages=3, files=1), scope="webui"
    )
    payload = user_cost_payload("user-AAA", timezone_name="UTC")
    assert payload["efficiency"]["commands_per_api_call"] is None
    assert payload["efficiency"]["local_steps_per_api_call"] is None

    record_user_cost("user-AAA", _counters(api_calls=2), scope="webui")
    payload = user_cost_payload("user-AAA", timezone_name="UTC")
    assert payload["efficiency"]["commands_per_api_call"] == 6.0
    assert payload["efficiency"]["local_steps_per_api_call"] == 8.0


def test_an_all_zero_turn_creates_no_row() -> None:
    """A turn that did nothing must not invent a user in the store."""
    record_user_cost("user-AAA", _counters(), scope="webui")

    assert user_cost_state_path() == user_cost_state_path()  # path is stable
    assert user_cost_payload("user-AAA", timezone_name="UTC")["first_seen"] is None


def test_a_malformed_stored_day_key_cannot_break_every_settings_read() -> None:
    """Hand-edited day keys are dropped instead of reaching the payload's math.

    A 10-character key that is not a real date survives a naive read and then
    blows up date arithmetic, failing every /api/settings request until the file
    is fixed by hand.
    """
    state_dir = user_cost_state_path().parent
    state_dir.mkdir(parents=True, exist_ok=True)
    user_cost_state_path().write_text(
        json.dumps(
            {
                "users": {
                    "user-AAA": {
                        "totals": {"api_calls": 4, "turns": 1},
                        "days": {
                            "not-a-dat3": {"api_calls": 7},
                            "2026-13-01": {"api_calls": 9},
                            "2026-06-02": {"api_calls": 5},
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    payload = user_cost_payload(
        "user-AAA",
        timezone_name="UTC",
        now=datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc),
    )

    assert [row["date"] for row in payload["days"]] == ["2026-06-02"]
    assert payload["days"][0]["api_calls"] == 5


def test_an_id_that_cannot_be_looked_up_is_refused() -> None:
    """A key nothing can look up is worse than no key: the row would be lost.

    Whitespace, path separators and over-long values are rejected outright so
    the store cannot grow rows that no read will ever find.
    """
    assert clean_user_id("  8f14e45f-ceea-6671-9b1b-6c1c1c1c1c1c ") == (
        "8f14e45f-ceea-6671-9b1b-6c1c1c1c1c1c"
    )
    assert clean_user_id("../../etc/passwd") == ""
    assert clean_user_id("user\nAAA") == ""
    assert clean_user_id("x" * 200) == ""
    assert clean_user_id(None) == ""

    record_user_cost("../../etc/passwd", _counters(turns=1, api_calls=1))
    assert user_cost_payload("../../etc/passwd", timezone_name="UTC")["metered"] is False
    assert not user_cost_state_path().exists()


def test_the_window_totals_and_the_lifetime_totals_differ() -> None:
    """``totals`` is lifetime; ``window`` is the last N days, and they must not alias."""
    old = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    recent = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    record_user_cost("user-AAA", _counters(api_calls=50), scope="webui", now=old)
    record_user_cost("user-AAA", _counters(api_calls=4), scope="webui", now=recent)

    payload = user_cost_payload("user-AAA", days=30, timezone_name="UTC", now=recent)

    assert payload["totals"]["api_calls"] == 54
    assert payload["window"]["api_calls"] == 4
    assert payload["window_days"] == 30


def test_a_float_or_negative_delta_cannot_corrupt_the_row() -> None:
    """Counters arrive from a hook; they are cleaned, not trusted."""
    record_user_cost(
        "user-AAA",
        {"turns": 1.7, "api_calls": -9, "commands": "12", "pages": None, "files": object()},
        scope="webui",
    )

    totals = user_cost_payload("user-AAA", timezone_name="UTC")["totals"]
    assert totals["api_calls"] == 0
    assert totals["commands"] == 12
    assert totals["files"] == 0
    assert totals["turns"] == 1


def test_the_store_is_written_atomically_and_leaves_no_temp_file() -> None:
    record_user_cost("user-AAA", _counters(turns=1, api_calls=1), scope="webui")

    assert user_cost_state_path().is_file()
    assert not user_cost_state_path().with_suffix(".json.tmp").exists()
