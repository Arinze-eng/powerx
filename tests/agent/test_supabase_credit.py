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
        self.calls: list[tuple[dict[str, str], str, int, int]] = []
        self.failure: Exception | None = None
        self._drain_rate = 2

    async def get_drain_rate(self, account: dict[str, str]) -> int:
        return self._drain_rate

    async def charge_step(self, account: dict[str, str], task_ref: str, step_no: int, amount: int = 0) -> dict[str, object]:
        self.calls.append((account, task_ref, step_no, amount))
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
        ({"agentx_user_id": "user-1"}, "nanobot:telegram:42:7", 1, 6),
        ({"agentx_user_id": "user-1"}, "nanobot:telegram:42:7", 2, 6),
    ]


@pytest.mark.asyncio
async def test_credit_hook_is_inert_for_non_telegram_turns(monkeypatch) -> None:
    fake = FakeSupabase()
    monkeypatch.setattr(supabase_credit, "SupabaseAuth", lambda: fake)
    hook = supabase_credit.create_supabase_credit_hook(AgentTurnHookContext(channel="websocket"))
    assert hook is None


@pytest.mark.asyncio
async def test_credit_hook_caches_drain_rate_across_iterations(monkeypatch) -> None:
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
    await hook.before_iteration(AgentHookContext(iteration=2, messages=[]))
    # drain_rate should be cached after the first call
    assert hook._drain_rate == 2


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