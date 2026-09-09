"""Three cost-reduction layers: history spend cap (#2), fuzzy plan match (#4),
message coalescing (#5). Each proven in isolation without live providers.

#2 — long chats must NOT resend (and pay for) the whole window every turn; the
     replay budget is now a fraction of the context window, not "all that fits".
#4 — a task phrased slightly differently than a stored plan still replays free
     instead of falling back to the model, as long as variables line up.
#5 — rapid same-session messages merge into ONE turn (one model call, not N).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from nanobot.agent.plan_cache import (
    PlanCache,
    StoredPlan,
    normalize_task,
    replayable_for,
    substitute_variables,
    variables_compatible,
    _template_similarity,
)


# ---------------------------------------------------------------------------
# #2 — replay token budget = spend ceiling, not fit ceiling
# ---------------------------------------------------------------------------


class _Gen:
    def __init__(self, max_tokens):
        self.max_tokens = max_tokens


@dataclass
class _Runtime:
    context_window_tokens: int
    generation: Any


class TestReplayBudgetCap:
    def test_caps_large_window_to_fraction(self, monkeypatch) -> None:
        from nanobot.agent.loop import AgentLoop

        monkeypatch.delenv("POWERX_REPLAY_BUDGET_RATIO", raising=False)
        rt = _Runtime(context_window_tokens=128_000, generation=_Gen(8192))
        budget = AgentLoop._replay_token_budget(rt)
        # Default ratio 0.35 => ~44800, far below the old ~119k fit-based value.
        assert budget <= int(128_000 * 0.35) + 10
        assert budget < 128_000 // 2  # definitely trimmed vs "all that fits"

    def test_small_window_still_respects_fit(self, monkeypatch) -> None:
        from nanobot.agent.loop import AgentLoop

        monkeypatch.delenv("POWERX_REPLAY_BUDGET_RATIO", raising=False)
        rt = _Runtime(context_window_tokens=8_000, generation=_Gen(4_096))
        budget = AgentLoop._replay_token_budget(rt)
        # fit limit (8000-4096-1024=2880) is smaller than spend (2800); take min.
        assert budget <= 2880
        assert budget >= 128

    def test_ratio_env_override(self, monkeypatch) -> None:
        from nanobot.agent.loop import AgentLoop

        monkeypatch.setenv("POWERX_REPLAY_BUDGET_RATIO", "0.8")
        rt = _Runtime(context_window_tokens=100_000, generation=_Gen(2_000))
        budget = AgentLoop._replay_token_budget(rt)
        assert budget == int(100_000 * 0.8)  # spend cap 80k < fit 97976

    def test_ratio_ge_one_disables_cap(self, monkeypatch) -> None:
        from nanobot.agent.loop import AgentLoop

        monkeypatch.setenv("POWERX_REPLAY_BUDGET_RATIO", "1.5")
        rt = _Runtime(context_window_tokens=128_000, generation=_Gen(8192))
        budget = AgentLoop._replay_token_budget(rt)
        # ratio>=1 means spend cap exceeds window; fit governs (~118784).
        assert budget > 100_000

    def test_invalid_ratio_uses_default(self, monkeypatch) -> None:
        from nanobot.agent.loop import AgentLoop

        monkeypatch.setenv("POWERX_REPLAY_BUDGET_RATIO", "banana")
        assert AgentLoop._replay_budget_ratio() == 0.35

    def test_zero_window_returns_zero(self) -> None:
        from nanobot.agent.loop import AgentLoop

        rt = _Runtime(context_window_tokens=0, generation=_Gen(1024))
        assert AgentLoop._replay_token_budget(rt) == 0


# ---------------------------------------------------------------------------
# #4 — fuzzy plan matching
# ---------------------------------------------------------------------------


class TestTemplateSimilarity:
    def test_identical_is_one(self) -> None:
        assert _template_similarity("compile project folder", "compile project folder") == 1.0

    def test_filler_words_dont_break_match(self) -> None:
        a = normalize_task("please compile the project in folder 7 now")
        b = normalize_task("compile project folder 7")
        assert a is not None and b is not None
        # After stopword removal both reduce to {compile, project, folder, <num>}
        sim = _template_similarity(a.template, b.template)
        assert sim >= 0.72

    def test_unrelated_tasks_low_similarity(self) -> None:
        a = normalize_task("compile the rust project please")
        b = normalize_task("send an email to the finance team")
        assert a is not None and b is not None
        assert _template_similarity(a.template, b.template) < 0.5

    def test_stopwords_only_both_empty(self) -> None:
        assert _template_similarity("the a", "an the") == 1.0


class TestFuzzyLookup:
    def test_exact_hit_fast_path(self, tmp_path) -> None:
        cache = PlanCache(tmp_path)
        norm = normalize_task("process data file 7 rows please")
        assert norm is not None
        cache.put(norm, [{"name": "exec", "arguments": {"command": "run 7"}}])
        got = cache.get_fuzzy(norm)
        assert got is not None
        assert got.steps[0]["arguments"]["command"] == "run 7"

    def test_fuzzy_hit_despite_different_wording(self, tmp_path) -> None:
        cache = PlanCache(tmp_path)
        learned = normalize_task("please process the data file 7 rows now")
        assert learned is not None
        cache.put(learned, [{"name": "exec", "arguments": {"command": "process 7"}}])
        # Different phrasing, SAME variable count (one number) -> should fuzzy match.
        query = normalize_task("process data file 9 rows")
        assert query is not None
        got = cache.get_fuzzy(query)
        assert got is not None, "fuzzy matcher should find the near-duplicate plan"
        # Substitution maps the new variable onto the recorded command.
        assert variables_compatible(query, got)
        steps = substitute_variables(got.steps, got.variables, query.variables)
        assert steps[0]["arguments"]["command"] == "process 9"

    def test_no_false_positive_on_unrelated(self, tmp_path) -> None:
        cache = PlanCache(tmp_path)
        learned = normalize_task("compile the rust project binary")
        assert learned is not None
        cache.put(learned, [{"name": "exec", "arguments": {"command": "cargo build"}}])
        query = normalize_task("translate this sentence into french")
        assert query is not None
        assert cache.get_fuzzy(query) is None  # similarity too low -> no replay

    def test_variable_count_mismatch_rejected(self, tmp_path) -> None:
        cache = PlanCache(tmp_path)
        learned = normalize_task("add numbers 3 and 5 together")
        assert learned is not None
        cache.put(learned, [{"name": "exec", "arguments": {"c": "3+5"}}])
        # Same template words but THREE variables now -> substitution unsafe.
        query = normalize_task("add numbers 3 and 5 and 8 together")
        assert query is not None
        if query.template != learned.template:
            # If templates differ, fuzzy may or may not match; but compatibility
            # check must reject unequal variable counts regardless.
            pass
        got = cache.get_fuzzy(query)
        # Either no match, or matched-but-incompatible (runner then falls back).
        assert got is None or not variables_compatible(query, got)


# ---------------------------------------------------------------------------
# #5 — message coalescing
# ---------------------------------------------------------------------------


@dataclass
class FakeMsg:
    content: str
    channel: str = "telegram"
    chat_id: str = "1"
    session_key_override: str | None = None
    media: list[str] = field(default_factory=list)
    is_user_input: bool = True
    kind: str = "user"
    sender_id: str = "user"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        return self.session_key_override or f"{self.channel}:{self.chat_id}"


class FakeBus:
    def __init__(self, messages: list[FakeMsg]) -> None:
        self._q: asyncio.Queue = asyncio.Queue()
        for m in messages:
            self._q.put_nowait(m)

    async def consume_inbound(self) -> FakeMsg:
        return await self._q.get()

    async def publish_inbound(self, msg: FakeMsg) -> None:
        await self._q.put(msg)

    def remaining(self) -> list[FakeMsg]:
        out = []
        while not self._q.empty():
            out.append(self._q.get_nowait())
        return out


class FakeCommands:
    def is_dispatchable_command(self, raw: str) -> bool:
        return raw.startswith("/")


class _Coalescer:
    """Binds just the coalesce helpers so we can unit-test them."""

    _unified_session = False

    def __init__(self, bus, commands) -> None:
        self.bus = bus
        self.commands = commands

    _effective_session_key = __import__("nanobot.agent.loop", fromlist=["AgentLoop"]).AgentLoop._effective_session_key
    _coalesce_window_seconds = staticmethod(__import__("nanobot.agent.loop", fromlist=["AgentLoop"]).AgentLoop._coalesce_window_seconds)
    _coalesce_same_session = __import__("nanobot.agent.loop", fromlist=["AgentLoop"]).AgentLoop._coalesce_same_session


class TestCoalescing:
    def test_merges_rapid_same_session_messages(self, monkeypatch) -> None:
        monkeypatch.setenv("POWERX_MESSAGE_COALESCE_MS", "150")
        msgs = [FakeMsg("do X"), FakeMsg("also Y"), FakeMsg("thanks")]
        bus = FakeBus(msgs)
        c = _Coalescer(bus, FakeCommands())
        result = asyncio.run(c._coalesce_same_session(msgs[0], "telegram:1"))
        assert "do X" in result.content
        assert "also Y" in result.content
        assert "thanks" in result.content
        assert bus.remaining() == []  # all consumed

    def test_stops_at_other_session_and_preserves_order(self, monkeypatch) -> None:
        monkeypatch.setenv("POWERX_MESSAGE_COALESCE_MS", "150")
        mine = FakeMsg("my first", chat_id="1")
        other = FakeMsg("someone else", chat_id="2")
        bus = FakeBus([other])
        c = _Coalescer(bus, FakeCommands())
        result = asyncio.run(c._coalesce_same_session(mine, "telegram:1"))
        # Stops merging at the foreign message; it is re-published untouched.
        assert "someone else" not in result.content
        left = bus.remaining()
        assert [m.content for m in left] == ["someone else"]


    def test_does_not_merge_commands(self, monkeypatch) -> None:
        monkeypatch.setenv("POWERX_MESSAGE_COALESCE_MS", "150")
        msgs = [FakeMsg("hello there"), FakeMsg("/stop")]
        bus = FakeBus(msgs)
        c = _Coalescer(bus, FakeCommands())
        result = asyncio.run(c._coalesce_same_session(msgs[0], "telegram:1"))
        assert "/stop" not in result.content
        # /stop was pulled then deferred back onto the bus.
        assert any(m.content == "/stop" for m in bus.remaining())

    def test_disabled_window_returns_first(self, monkeypatch) -> None:
        monkeypatch.setenv("POWERX_MESSAGE_COALESCE_MS", "0")
        msgs = [FakeMsg("a"), FakeMsg("b")]
        bus = FakeBus(msgs)
        c = _Coalescer(bus, FakeCommands())
        result = asyncio.run(c._coalesce_same_session(msgs[0], "telegram:1"))
        assert result is msgs[0]
        assert len(bus.remaining()) == 2  # untouched

    def test_single_message_noop(self, monkeypatch) -> None:
        monkeypatch.setenv("POWERX_MESSAGE_COALESCE_MS", "80")
        only = FakeMsg("just one thing here")
        bus = FakeBus([])  # nothing after it
        c = _Coalescer(bus, FakeCommands())
        result = asyncio.run(c._coalesce_same_session(only, "telegram:1"))
        assert result is only
