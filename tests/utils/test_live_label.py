"""A tool that is WATCHING something must be able to say what it last saw.

The runner already narrates a blocked tool every few seconds, but the narration
was generic ("still running (24s)"), which cannot distinguish watching a market
from waiting on a download. This store is the channel a watching tool publishes
its newest observation into so the progress line can carry it.
"""

from __future__ import annotations

from nanobot.utils.live_label import (
    DEFAULT_LIVE_LABEL_MAX_AGE_S,
    MAX_LIVE_LABEL_CHARS,
    clear_live_label,
    live_label_for,
    live_labels,
    publish_live_label,
)


def setup_function() -> None:
    for name in list(live_labels(max_age_s=None)):
        clear_live_label(name)


def test_a_published_label_comes_back():
    assert publish_live_label("mt5_sandbox", "EYES ON: #777 EURUSD buy, bid 1.1419")
    assert (
        live_label_for("mt5_sandbox")
        == "EYES ON: #777 EURUSD buy, bid 1.1419"
    )


def test_publishing_returns_what_landed_so_the_caller_can_log_it():
    landed = publish_live_label("mt5_sandbox", "  bid   1.1419\n +0.42R  ")
    # Whitespace is collapsed: the label is rendered on ONE line next to the tool
    # name, so a newline in it would silently truncate the interesting part.
    assert landed == "bid 1.1419 +0.42R"
    assert live_label_for("mt5_sandbox") == landed


def test_nothing_usable_is_never_stored():
    """An empty publication must not erase or fake an observation."""
    publish_live_label("mt5_sandbox", "guard live, 35 ticks scanned")
    for junk in ("", "   ", "\n\t", None, 17):
        assert publish_live_label("mt5_sandbox", junk) is None
    assert live_label_for("mt5_sandbox") == "guard live, 35 ticks scanned"
    for bad_name in ("", None, 5):
        assert publish_live_label(bad_name, "x") is None


def test_a_long_label_is_trimmed_rather_than_dropped():
    long_text = "EYES ON: " + "bid 1.1419, " * 40
    landed = publish_live_label("mt5_sandbox", long_text)
    assert landed is not None
    assert len(landed) == MAX_LIVE_LABEL_CHARS
    assert landed.endswith("…")
    assert landed.startswith("EYES ON: bid 1.1419")


def test_an_old_label_is_refused_so_a_dead_tool_stops_narrating():
    publish_live_label("mt5_sandbox", "bid 1.1419", at=1000.0)
    assert live_label_for("mt5_sandbox", now=1005.0) == "bid 1.1419"
    # Staleness is the whole point: a crashed or hung tool must not leave a
    # frozen observation on screen looking like a live reading.
    assert live_label_for("mt5_sandbox", now=1000.0 + DEFAULT_LIVE_LABEL_MAX_AGE_S + 1) is None
    # Comfortably longer than the 8 s heartbeat, so a healthy label is never
    # dropped between beats.
    assert DEFAULT_LIVE_LABEL_MAX_AGE_S > 8.0


def test_staleness_is_measured_on_the_monotonic_clock():
    """A system clock change must not make a fresh label look old, or a dead one alive."""
    import time

    publish_live_label("mt5_sandbox", f"bid 1.1419 at {time.monotonic()}")
    assert live_label_for("mt5_sandbox") is not None


def test_clearing_forgets_the_label():
    publish_live_label("mt5_sandbox", "bid 1.1419")
    clear_live_label("mt5_sandbox")
    assert live_label_for("mt5_sandbox") is None
    # Clearing is idempotent and tolerant of junk: it is called from a finally.
    clear_live_label("mt5_sandbox")
    clear_live_label(None)


def test_two_tools_keep_their_own_labels():
    publish_live_label("mt5_sandbox", "watching XAUUSD")
    publish_live_label("bash", "tail -f")
    assert live_labels() == {"mt5_sandbox": "watching XAUUSD", "bash": "tail -f"}
    # The last publisher for a NAME wins: a label is a status line, not a ledger.
    publish_live_label("mt5_sandbox", "watching EURUSD")
    assert live_label_for("mt5_sandbox") == "watching EURUSD"


def test_an_unknown_name_has_no_label():
    assert live_label_for("never-published") is None
    assert live_label_for(None) is None
