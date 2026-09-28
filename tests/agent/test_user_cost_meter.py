"""The per-turn hook that files a turn into one user's own cost meter."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from nanobot.agent.hook import AgentHookContext, AgentTurnHookContext
from nanobot.agent.hooks import user_cost_meter
from nanobot.agent.hooks.user_cost_meter import (
    UserCostMeterHook,
    count_tool_call,
    create_user_cost_meter_hook,
)
from nanobot.providers.base import ToolCallRequest
from nanobot.webui import user_cost
from nanobot.webui.user_cost import user_cost_payload


@pytest.fixture(autouse=True)
def _webui_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(user_cost, "get_webui_dir", lambda: tmp_path / "webui")
    # The hook imports the recorder by name at module load, so the store it
    # writes to is reached through the same patched dir either way.
    return tmp_path / "webui"


class _Tool:
    """Stand-in for a write tool; the resolver prefers its own ``_resolve_write``."""

    def __init__(self, root) -> None:
        self._root = root

    def _resolve_write(self, raw: str):
        return (self._root / raw).resolve()


def _call(name: str, arguments: dict | None = None, call_id: str = "c1") -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments or {})


def _meter(user_id: str = "user-AAA"):
    return user_cost_payload(user_id, timezone_name="UTC")


def test_a_command_a_page_and_a_search_are_not_the_same_thing() -> None:
    """Classification is the feed's, so the meter cannot disagree with it.

    A shell command is a command, a navigation is a page, and a lookup that is
    neither lands in ``steps`` alone — which is what keeps ``steps`` an honest
    total instead of a second name for commands.
    """
    assert count_tool_call("exec", {"command": "ls -la"}) == {
        "commands": 1,
        "pages": 0,
        "files": 0,
        "steps": 1,
    }
    assert count_tool_call("browser", {"url": "https://example.com"}) == {
        "commands": 0,
        "pages": 1,
        "files": 0,
        "steps": 1,
    }
    assert count_tool_call("search_web", {"query": "anything"}) == {
        "commands": 0,
        "pages": 0,
        "files": 0,
        "steps": 1,
    }


def test_an_unregistered_tool_that_announces_a_command_is_a_command() -> None:
    """A tool added tomorrow must not vanish from the meter.

    Classification gates on the arguments the call actually carries, not on a
    list of tool names that would need updating in step with the registry.
    """
    assert count_tool_call("some_new_runner", {"shell_command": "make"})["commands"] == 1


@pytest.mark.asyncio
async def test_one_turn_files_api_calls_commands_pages_and_files_together(tmp_path) -> None:
    hook = UserCostMeterHook(user_id="user-AAA", scope="webui", workspace=tmp_path)
    tool = _Tool(tmp_path)

    await hook.after_iteration(
        AgentHookContext(iteration=0, messages=[], usage={"llm_calls": 1})
    )
    for call, params in (
        (_call("exec", {"command": "ls"}, "c1"), {"command": "ls"}),
        (_call("exec", {"command": "pwd"}, "c2"), {"command": "pwd"}),
        (_call("browser", {"url": "https://a.example"}, "c3"), {"url": "https://a.example"}),
        (_call("search_web", {"query": "x"}, "c4"), {"query": "x"}),
    ):
        await hook.after_execute_tool(
            AgentHookContext(iteration=0, messages=[]), call, tool, params, None
        )
    await hook.after_execute_tool(
        AgentHookContext(iteration=0, messages=[]),
        _call("write_file", {"path": "report.md"}, "c5"),
        tool,
        {"path": "report.md"},
        None,
    )
    await hook.on_finally(SimpleNamespace(usage={"llm_calls": 3}, stop_reason="completed"))

    totals = _meter()["totals"]
    assert totals["turns"] == 1
    assert totals["api_calls"] == 3
    assert totals["commands"] == 2
    assert totals["pages"] == 1
    assert totals["files"] == 1
    assert totals["steps"] == 5


@pytest.mark.asyncio
async def test_the_same_file_written_twice_is_one_file(tmp_path) -> None:
    """Deduped per turn, which is how the thread's file rows fold them too."""
    hook = UserCostMeterHook(user_id="user-AAA", scope="webui", workspace=tmp_path)
    tool = _Tool(tmp_path)
    context = AgentHookContext(iteration=0, messages=[])

    for call_id in ("c1", "c2"):
        await hook.after_execute_tool(
            context,
            _call("write_file", {"path": "same.md"}, call_id),
            tool,
            {"path": "same.md"},
            None,
        )
    await hook.on_finally(SimpleNamespace(usage={"llm_calls": 1}, stop_reason="completed"))

    assert _meter()["totals"]["files"] == 1


@pytest.mark.asyncio
async def test_a_stopped_turn_is_still_filed(tmp_path) -> None:
    """The flush hangs off ``on_finally`` because ``after_run`` is skipped there.

    A turn stopped for billing returns from the runner's own except block, so
    ``after_run`` never runs. That is exactly the turn whose spend a user is
    looking at, so the meter is written from the hook that always runs.
    """
    hook = UserCostMeterHook(user_id="user-AAA", scope="webui", workspace=tmp_path)
    await hook.after_iteration(
        AgentHookContext(iteration=0, messages=[], usage={"llm_calls": 2})
    )
    await hook.after_execute_tool(
        AgentHookContext(iteration=0, messages=[]),
        _call("exec", {"command": "ls"}),
        None,
        {"command": "ls"},
        None,
    )

    # No after_run at all: the run died on a credit stop.
    await hook.on_finally(SimpleNamespace(usage={}, stop_reason="credit_exhausted"))

    totals = _meter()["totals"]
    assert totals["api_calls"] == 2
    assert totals["commands"] == 1
    assert totals["turns"] == 1


@pytest.mark.asyncio
async def test_one_turn_is_filed_once_even_though_both_hooks_run(tmp_path) -> None:
    """``after_run`` and ``on_finally`` both fire on a normal turn."""
    hook = UserCostMeterHook(user_id="user-AAA", scope="webui", workspace=tmp_path)
    run_ctx = SimpleNamespace(usage={"llm_calls": 1}, stop_reason="completed")

    await hook.on_finally(run_ctx)
    await hook.on_finally(run_ctx)

    assert _meter()["totals"]["turns"] == 1


@pytest.mark.asyncio
async def test_a_raising_tool_call_cannot_take_down_the_turn(tmp_path) -> None:
    """Metering is observability; a broken count must not break the work."""
    hook = UserCostMeterHook(user_id="user-AAA", scope="webui", workspace=tmp_path)

    class _Exploding:
        @property
        def name(self):
            raise RuntimeError("boom")

    await hook.after_execute_tool(
        AgentHookContext(iteration=0, messages=[]),
        _Exploding(),
        None,
        {},
        None,
    )
    await hook.on_finally(SimpleNamespace(usage={"llm_calls": 1}, stop_reason="completed"))

    assert _meter()["metered"] is True


def test_a_turn_with_no_identity_creates_no_hook() -> None:
    """Keyed by a guess would file one user's work under another user's name."""
    assert (
        create_user_cost_meter_hook(
            AgentTurnHookContext(channel="websocket", chat_id="c1", metadata={})
        )
        is None
    )
    assert (
        create_user_cost_meter_hook(
            AgentTurnHookContext(channel="cli", chat_id="direct", metadata={})
        )
        is None
    )
    assert (
        create_user_cost_meter_hook(
            AgentTurnHookContext(
                channel="telegram",
                chat_id="42",
                metadata={"supabase_user_id": "user-AAA"},
            )
        )
        is None
    )


def test_webui_and_api_turns_are_metered_under_their_own_scope(monkeypatch) -> None:
    """The scope is kept so a chat and an API key are not silently merged."""
    for channel, scope in (("websocket", "webui"), ("api", "api")):
        hook = create_user_cost_meter_hook(
            AgentTurnHookContext(
                channel=channel,
                chat_id="c1",
                metadata={"supabase_user_id": "user-AAA"},
                timezone_name="Europe/Berlin",
            )
        )
        assert isinstance(hook, UserCostMeterHook)
        assert hook._scope == scope
        assert hook._user_id == "user-AAA"
        assert hook._timezone_name == "Europe/Berlin"


@pytest.mark.asyncio
async def test_the_day_bucket_uses_the_turn_timezone(tmp_path) -> None:
    """A meter that disagrees with the calendar beside it is a meter nobody trusts.

    The turn's configured zone is carried on the hook context, so a WebUI chat
    filed at 16:30 UTC lands on the user's own next day in Asia/Tokyo.
    """
    hook = create_user_cost_meter_hook(
        AgentTurnHookContext(
            channel="websocket",
            chat_id="c1",
            metadata={"supabase_user_id": "user-AAA"},
            timezone_name="Asia/Tokyo",
        )
    )
    assert isinstance(hook, UserCostMeterHook)

    moment = datetime(2026, 6, 2, 16, 30, tzinfo=timezone.utc)
    original = user_cost_meter.record_user_cost

    def _record(user_id, counters, **kwargs):
        return original(user_id, counters, now=moment, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(user_cost_meter, "record_user_cost", _record)
        await hook.on_finally(SimpleNamespace(usage={"llm_calls": 1}, stop_reason="completed"))

    stored = user_cost.read_user_cost_state()["users"]["user-AAA"]["days"]
    assert list(stored) == ["2026-06-03"]
