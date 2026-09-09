from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.agent.hook import AgentHook, AgentHookContext, AgentTurnHookContext
from nanobot.agent.hooks import supabase_credit
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.supabase_auth import SupabaseAuthError


class FakeSupabase:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, str], str, int]] = []
        self.failure: Exception | None = None

    async def charge_step(self, account: dict[str, str], task_ref: str, step_no: int) -> dict[str, object]:
        self.calls.append((account, task_ref, step_no))
        if self.failure is not None:
            raise self.failure
        return {"success": True, "balance": 10}


@pytest.mark.asyncio
async def test_credit_hook_charges_each_telegram_iteration(monkeypatch) -> None:
    fake = FakeSupabase()
    monkeypatch.setattr(supabase_credit, "SupabaseAuth", lambda: fake)
    hook = supabase_credit.SupabaseCreditHook(AgentTurnHookContext(
        channel="telegram",
        chat_id="42",
        message_id="7",
        session_key="telegram:42",
        metadata={"supabase_user_id": "user-1"},
    ))
    await hook.before_iteration(AgentHookContext(iteration=0, messages=[]))
    await hook.before_iteration(AgentHookContext(iteration=1, messages=[]))
    assert fake.calls == [
        ({"agentx_user_id": "user-1"}, "nanobot:telegram:42:7", 1),
        ({"agentx_user_id": "user-1"}, "nanobot:telegram:42:7", 2),
    ]


@pytest.mark.asyncio
async def test_credit_hook_is_inert_for_non_telegram_turns(monkeypatch) -> None:
    fake = FakeSupabase()
    monkeypatch.setattr(supabase_credit, "SupabaseAuth", lambda: fake)
    hook = supabase_credit.create_supabase_credit_hook(AgentTurnHookContext(channel="websocket"))
    assert hook is None


@pytest.mark.asyncio
async def test_credit_hook_fails_closed_when_balance_is_insufficient(monkeypatch) -> None:
    fake = FakeSupabase()
    fake.failure = SupabaseAuthError("Insufficient credits")
    monkeypatch.setattr(supabase_credit, "SupabaseAuth", lambda: fake)
    hook = supabase_credit.SupabaseCreditHook(AgentTurnHookContext(
        channel="telegram",
        chat_id="42",
        message_id="7",
        session_key="telegram:42",
        metadata={"supabase_user_id": "user-1"},
    ))
    with pytest.raises(supabase_credit.CreditExhaustedError, match="Please add credit"):
        await hook.before_iteration(AgentHookContext(iteration=0, messages=[]))


@pytest.mark.asyncio
async def test_credit_hook_rejects_unlinked_telegram_turn(monkeypatch) -> None:
    fake = FakeSupabase()
    monkeypatch.setattr(supabase_credit, "SupabaseAuth", lambda: fake)
    hook = supabase_credit.SupabaseCreditHook(AgentTurnHookContext(
        channel="telegram",
        chat_id="42",
        message_id="7",
        session_key="telegram:42",
        metadata={},
    ))
    with pytest.raises(supabase_credit.CreditExhaustedError, match="/signup or /signin"):
        await hook.before_iteration(AgentHookContext(iteration=0, messages=[]))


class _RejectingCreditHook(AgentHook):
    def __init__(self) -> None:
        super().__init__(reraise=True)

    async def before_iteration(self, context: AgentHookContext) -> None:
        raise supabase_credit.CreditExhaustedError("Please add credit before using the agent again.")


@pytest.mark.asyncio
async def test_runner_returns_exhausted_credit_without_calling_provider() -> None:
    result = await AgentRunner().run(AgentRunSpec(
        initial_messages=[],
        tools=ToolRegistry(),
        runtime=SimpleNamespace(
            provider=object(),
            model="test-model",
            context_window_tokens=1000,
            generation=SimpleNamespace(max_tokens=100),
        ),
        max_iterations=1,
        max_tool_result_chars=1000,
        hook=_RejectingCreditHook(),
    ))
    assert result.stop_reason == "credit_exhausted"
    assert result.error == "Please add credit before using the agent again."
    assert result.final_content == result.error


# ---------------------------------------------------------------------------
# Cost meter (B): per-turn spend + zero-call savings must be observable.
# ---------------------------------------------------------------------------


def _capture_cost_lines(monkeypatch):
    """Route loguru 'cost'-bound records into a list for assertions."""
    from loguru import logger

    captured: list[str] = []

    def _sink(message):  # message is a str-like record
        captured.append(str(message))

    sink_id = logger.add(_sink, filter=lambda r: r["extra"].get("cost") is True)
    yield_ = None  # placeholder to keep flake quiet
    return captured, lambda: logger.remove(sink_id)


class TestCostMeter:
    def test_llm_turn_reports_calls(self, monkeypatch) -> None:
        captured, remove = _capture_cost_lines(monkeypatch)
        try:
            supabase_credit.log_cost_meter(
                channel="telegram",
                session_key="tg:123",
                charged_steps=3,
                usage={"prompt_tokens": 100, "completion_tokens": 40},
                stop_reason="completed",
            )
        finally:
            remove()
        line = "\n".join(captured)
        assert "COST_METER" in line
        assert "llm_calls=3" in line
        assert "served_by=llm" in line

    def test_plan_replay_reports_zero_calls(self, monkeypatch) -> None:
        captured, remove = _capture_cost_lines(monkeypatch)
        try:
            supabase_credit.log_cost_meter(
                channel="api",
                session_key="api:key",
                charged_steps=0,
                usage={"plan_replayed": 1},
                stop_reason="completed",
            )
        finally:
            remove()
        line = "\n".join(captured)
        assert "llm_calls=0" in line
        assert "served_by=plan_cache" in line

    def test_middleware_and_router_attributed(self, monkeypatch) -> None:
        for marker, expected in [
            ("middleware_formatted", "tool_middleware"),
            ("deterministic", "deterministic_router"),
            ("replay_cache", "replay_cache"),
        ]:
            captured, remove = _capture_cost_lines(monkeypatch)
            try:
                supabase_credit.log_cost_meter(
                    channel="telegram",
                    session_key="s",
                    charged_steps=0,
                    usage={marker: 1},
                    stop_reason="completed",
                )
            finally:
                remove()
            assert f"served_by={expected}" in "\n".join(captured), marker

    def test_meter_never_raises_on_bad_input(self, monkeypatch) -> None:
        captured, remove = _capture_cost_lines(monkeypatch)
        try:
            # Garbage usage must not blow up a paid turn.
            supabase_credit.log_cost_meter(
                channel="telegram",
                session_key=None,
                charged_steps=None,  # type: ignore[arg-type]
                usage=None,
                stop_reason=None,
            )
        finally:
            remove()
        # Still emits something sane (treats missing counts as 0).
        assert "COST_METER" in "\n".join(captured)
