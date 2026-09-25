"""What the Live screen's activity feed is allowed to print, and how it decides.

Two things are asserted here, and they fail in opposite directions.

**Coverage.** The feed must show every general task, not only an MT5 one: a shell
command, a browsing step, a media download. Its first version classified calls
against a tool-name set kept in the WebUI, and that set went stale silently — the
general case produced no row at all, which on a panel whose whole job is to say
what the agent is doing is indistinguishable from "the agent did nothing". The
classification now lives here, next to the tools, and the tests below pin the
families it covers, including the URL-shaped parameters an audit found
unregistered.

**Restraint.** A tool event is broadcast to every WebUI client and written into
the chat transcript, so anything left in ``arguments`` is durable and public. The
bounding tests pin that a secret key never survives and that a long value is
capped, while every tool the feed does not render is passed through untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nanobot.agent.hook import AgentHookContext  # noqa: F401 - imported first on purpose
from nanobot.agent import activity
from nanobot.providers.base import ToolCallRequest
from nanobot.utils.progress_events import (
    build_tool_event_finish_payloads,
    build_tool_event_start_payload,
)


# --- classification: which call makes which row --------------------------------


def test_a_shell_command_is_a_command_row() -> None:
    assert activity.classify_kind("exec", {"command": "ls -la"}) == activity.KIND_COMMAND


def test_a_reading_of_a_page_the_agent_reached_is_a_nav_row_for_every_tool_that_takes_a_url() -> None:
    """The four URL parameters an audit found unregistered must all classify.

    Each of these was invisible to the feed before: the tool was not in any set
    the WebUI held, so a browsing step produced no row. They are named here
    individually rather than as a family so that dropping one from
    ``URL_ARGUMENTS`` fails a test instead of quietly emptying part of the feed.
    """
    cases = [
        ("browser", {"action": "navigate", "url": "https://a.example.com"}),
        ("human_browser", {"action": "navigate", "url": "https://a.example.com"}),
        ("web_fetch", {"url": "https://a.example.com"}),
        ("media_sandbox", {"action": "download", "url": "https://a.example.com/x.mp4"}),
        ("novita_sandbox", {"url": "https://a.example.com/f.py"}),
        ("web_dev", {"url": "http://localhost:5173"}),
    ]
    for name, arguments in cases:
        assert activity.classify_kind(name, arguments) == activity.KIND_NAV, name


def test_a_browsing_tool_is_a_nav_row_even_when_it_names_no_destination() -> None:
    """A click or a keystroke is still the agent browsing, and worth a row."""
    assert activity.classify_kind("browser", {"action": "click", "target": "#go"}) == activity.KIND_NAV
    assert activity.classify_kind("browser", {"action": "screenshot"}) == activity.KIND_NAV


def test_a_selector_is_not_a_destination() -> None:
    """``target`` is a CSS selector on both browser tools, never an address.

    Treating it as one put a selector in the destination slot of every click, so
    the empty-value case is pinned too: a call that sends ``url: None`` is not a
    navigation just because the key was present.
    """
    assert "target" not in activity.URL_ARGUMENTS
    assert activity.classify_kind("browser", {"action": "click", "url": None}) == activity.KIND_NAV
    assert "url" not in activity._present_keys({"url": None, "action": "click"})
    assert "url" not in activity._present_keys({"url": ""})


def test_an_mt5_action_that_moves_the_book_is_a_trade_row() -> None:
    for action in sorted(activity.MT5_TRADING_ACTIONS):
        assert activity.classify_kind("mt5_sandbox", {"action": action}) == activity.KIND_TRADE, action


def test_a_read_only_mt5_action_is_not_a_trade_row() -> None:
    for action in ["quote", "positions", "account", "candles", "history", "status", "doctor"]:
        assert activity.classify_kind("mt5_sandbox", {"action": action}) == activity.KIND_SANDBOX, action


def test_an_mt5_installer_url_does_not_turn_an_install_into_browsing() -> None:
    """MT5 is classified by its action, never by its arguments.

    ``mt5 install`` takes a download URL, and a download is not the agent
    browsing; labelling it ``nav`` would put a broker installer in the panel's
    browsing colour.
    """
    kind = activity.classify_kind(
        "mt5_sandbox",
        {"action": "install", "broker_installer_url": "https://download.mql5.com/x.exe"},
    )
    assert kind == activity.KIND_SANDBOX
    kind = activity.classify_kind(
        "mt5_sandbox", {"action": "install", "page_urls": ["https://broker.example.com"]}
    )
    assert kind == activity.KIND_SANDBOX


def test_a_sandbox_tool_without_a_command_or_a_url_is_a_sandbox_row() -> None:
    assert activity.classify_kind("novita_sandbox", {"action": "install"}) == activity.KIND_SANDBOX
    assert activity.classify_kind("long_task", {"title": "backtest"}) == activity.KIND_SANDBOX
    assert activity.classify_kind("build_artifact", {"action": "build"}) == activity.KIND_SANDBOX


def test_a_tool_the_feed_has_never_heard_of_still_reports_its_command() -> None:
    """The next shell-ish tool must not need a WebUI redeploy to appear.

    This is the whole reason classification moved host-side: a tool added
    tomorrow with a ``command`` parameter renders today.
    """
    assert activity.classify_kind("some_new_backend", {"command": "whoami"}) == activity.KIND_COMMAND


def test_a_call_that_touches_neither_a_machine_nor_a_page_gets_no_row() -> None:
    for name, arguments in [
        ("web_search", {"query": "eurusd"}),
        ("filesystem", {"path": "/tmp/x"}),
        ("message", {"text": "hi"}),
        ("", {"command": "ls"}),
    ]:
        assert activity.classify_kind(name, arguments) is None, name


def test_the_feed_families_are_exported_for_a_client_that_wants_to_show_them() -> None:
    names = activity.feed_tool_names()
    assert "exec" in names
    assert "browser" in names
    assert "mt5_sandbox" in names
    assert "web_search" not in names


# --- the privacy boundary ------------------------------------------------------


def test_a_secret_key_never_survives_into_a_tool_event() -> None:
    bounded = activity.tool_event_arguments(
        "browser",
        {
            "action": "type",
            "url": "https://a.example.com/login",
            "password": "hunter2",
            "api_key": "sk-live-123",
            "headers": {"Authorization": "Bearer zz", "X-Ok": "1"},
        },
    )
    assert "password" not in bounded
    assert "api_key" not in bounded
    assert "Authorization" not in bounded["headers"]
    assert bounded["headers"]["X-Ok"] == "1"
    assert bounded["url"] == "https://a.example.com/login"


def test_a_command_is_kept_because_showing_it_is_the_whole_point() -> None:
    """The boundary drops secret *keys*, not secret-looking substrings.

    A command is heuristically unfilterable — every command looks suspicious — and
    a panel that hid the command would be the static screen again with more
    chrome. The residual risk is stated here rather than papered over: an agent
    that inlines a credential into a command still publishes it.
    """
    bounded = activity.tool_event_arguments("exec", {"command": "export TOKEN=abc123; ls"})
    assert bounded["command"] == "export TOKEN=abc123; ls"


def test_a_long_value_is_capped_and_the_row_stays_usable() -> None:
    bounded = activity.tool_event_arguments("exec", {"command": "x" * 5000})
    assert len(bounded["command"]) == activity.MAX_ARGUMENT_CHARS
    assert bounded["command"].endswith("…")


def test_a_deeply_parameterised_tool_is_capped_by_key_count() -> None:
    arguments = {f"param_{i}": i for i in range(60)}
    bounded = activity.tool_event_arguments("exec", arguments)
    assert len(bounded) <= activity.MAX_ARGUMENT_KEYS


def test_the_identifying_arguments_are_kept_ahead_of_the_incidental_ones() -> None:
    """When the cap bites, the keys that name the call are the ones that survive."""
    arguments = {"zzz": "1", "command": "ls", "aaa": "2"}
    bounded = activity.tool_event_arguments("exec", arguments)
    assert list(bounded)[0] == "command"


def test_a_plain_tool_is_passed_through_untouched() -> None:
    """The transcript renders arguments for its own reasons.

    Rewriting a tool this module does not understand would silently edit somebody
    else's feature, so a search that carries an ``api_key`` keeps it.
    """
    arguments = {"query": "x", "api_key": "k" * 900}
    assert activity.tool_event_arguments("web_search", arguments) == arguments


def test_an_unknown_tool_that_carries_a_command_is_still_bounded() -> None:
    """The tool nobody has registered must not be the one hole in the boundary.

    A row is printed for it, so its arguments are broadcast; leaving them
    unbounded because its name is unfamiliar would be exactly backwards.
    """
    bounded = activity.tool_event_arguments(
        "some_new_backend", {"command": "deploy", "token": "abc"}
    )
    assert "token" not in bounded


def test_the_boundary_can_be_opened_for_an_operator_debugging_a_tool(monkeypatch: Any) -> None:
    monkeypatch.setenv(activity.ACTIVITY_ARGUMENTS_ENV, "0")
    arguments = {"command": "ls", "password": "hunter2"}
    assert activity.tool_event_arguments("exec", arguments) == arguments
    assert activity.activity_arguments_enabled() is False


# --- outcomes ------------------------------------------------------------------


def test_a_guard_saying_no_is_refused_and_not_a_failure() -> None:
    """The distinction the panel colours on.

    An agent that tried to trade with live trading switched off produces a result
    that reads like a failure and is a working safety control. Colouring that red
    next to a real broker rejection trains an operator to ignore red.
    """
    gate = "MT5 live trading is switched off. Set MT5_ALLOW_TRADING=1 to enable it."
    assert activity.classify_outcome("error", gate) == activity.OUTCOME_REFUSED
    assert activity.classify_outcome("error", "path guard: outside the workspace") == (
        activity.OUTCOME_REFUSED
    )
    assert activity.classify_outcome("error", "no execution sandbox is configured") == (
        activity.OUTCOME_REFUSED
    )


def test_a_real_rejection_is_an_error() -> None:
    assert activity.classify_outcome("error", "retcode 10027: invalid stops") == (
        activity.OUTCOME_ERROR
    )
    assert activity.classify_outcome("error", "connection reset by peer") == activity.OUTCOME_ERROR


def test_a_successful_call_is_ok_whatever_its_result_says() -> None:
    """A success cannot have been prevented, so it is never a refusal."""
    assert activity.classify_outcome("end", None) == activity.OUTCOME_OK
    assert activity.classify_outcome("end", "permission denied") == activity.OUTCOME_OK
    assert activity.classify_outcome("ok", "") == activity.OUTCOME_OK


# --- the wire contract the WebUI reads -----------------------------------------


@dataclass
class _HookContext:
    tool_calls: list[ToolCallRequest]
    tool_results: list[Any]
    tool_events: list[dict[str, str]]


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments)


def test_a_frame_carries_the_kind_the_client_renders() -> None:
    """The client renders ``kind`` and never matches on a tool name.

    If this field stops being populated the panel does not error, it silently
    empties — which is indistinguishable from "the agent did nothing", the exact
    defect the feed exists to fix.
    """
    start = build_tool_event_start_payload(
        _call("c1", "browser", {"action": "navigate", "url": "https://a.example.com"})
    )
    assert start["kind"] == activity.KIND_NAV
    assert start["outcome"] is None

    finishes = build_tool_event_finish_payloads(
        _HookContext(
            tool_calls=[_call("c1", "browser", {"action": "navigate", "url": "https://a.example.com"})],
            tool_results=["ok"],
            tool_events=[{"status": "ok"}],
        )  # type: ignore[arg-type]
    )
    assert finishes[0]["kind"] == activity.KIND_NAV
    assert finishes[0]["outcome"] == activity.OUTCOME_OK


def test_a_refusal_reaches_the_client_as_a_refusal() -> None:
    call = _call("c2", "mt5_sandbox", {"action": "order", "symbol": "EURUSD"})
    finishes = build_tool_event_finish_payloads(
        _HookContext(
            tool_calls=[call],
            tool_results=["MT5 live trading is switched off. Set MT5_ALLOW_TRADING=1"],
            tool_events=[{"status": "error"}],
        )  # type: ignore[arg-type]
    )
    assert finishes[0]["phase"] == "error"
    assert finishes[0]["outcome"] == activity.OUTCOME_REFUSED
    assert finishes[0]["kind"] == activity.KIND_TRADE


def test_a_call_the_feed_will_not_render_still_gets_a_frame_with_no_kind() -> None:
    """``kind: None`` is a fact the producer states, not one the client infers."""
    start = build_tool_event_start_payload(_call("c3", "web_search", {"query": "x"}))
    assert start["kind"] is None
    assert start["arguments"] == {"query": "x"}
