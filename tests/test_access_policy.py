"""The administrator's access switches: resolution, overrides, and enforcement.

Every case here is a decision the product makes about a paying user, so the two
failure modes are tested explicitly: a switch that is off must actually refuse,
and a switch that is on (or an account that has been excepted) must not.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.access_policy import (
    AccessDeniedError,
    AccessPolicy,
    turn_is_trading,
    turn_user_key,
)
from nanobot.agent.hook import AgentHookContext, AgentTurnHookContext
from nanobot.agent.hooks.access_gate import AccessGateHook, create_access_gate_hook
from nanobot.config.schema import AccessConfig


def _policy(tmp_path, **access) -> AccessPolicy:
    return AccessPolicy(
        config=SimpleNamespace(access=AccessConfig(**access)),
        path=tmp_path / "user_access_overrides.json",
    )


def _turn(**metadata) -> AgentTurnHookContext:
    return AgentTurnHookContext(
        channel="websocket", chat_id="direct", metadata=metadata
    )


# --------------------------------------------------------------------- switches


def test_defaults_allow_everything(tmp_path):
    policy = _policy(tmp_path)
    assert policy.general_task("supabase:u").allowed
    assert policy.mt5_trading().allowed
    assert policy.brokers("supabase:u").allowed


def test_general_task_switch_refuses_with_a_reason(tmp_path):
    policy = _policy(tmp_path, general_task_enabled=False)
    decision = policy.general_task("supabase:u")
    assert decision.allowed is False
    assert decision.source == "global"
    assert "switched off" in decision.reason


def test_mt5_switch_is_absolute_and_ignores_overrides(tmp_path):
    """The MT5 kill switch exists to retire the method, so nothing may undo it."""
    policy = _policy(tmp_path, mt5_trading_enabled=False)
    policy.set_override("supabase:u", mt5_trading=True)
    assert policy.mt5_trading("supabase:u").allowed is False


def test_brokers_switch_refuses(tmp_path):
    policy = _policy(tmp_path, brokers_enabled=False)
    assert policy.brokers("supabase:u").allowed is False


# --------------------------------------------------------------------- overrides


def test_override_allows_one_account_while_the_global_switch_is_off(tmp_path):
    policy = _policy(tmp_path, general_task_enabled=False)
    policy.set_override("supabase:vip", general_task=True)
    assert policy.general_task("supabase:vip").allowed is True
    assert policy.general_task("supabase:vip").source == "user"
    # Everyone else is still refused.
    assert policy.general_task("supabase:other").allowed is False


def test_override_denies_one_account_while_the_global_switch_is_on(tmp_path):
    policy = _policy(tmp_path)
    policy.set_override("supabase:abuser", general_task=False)
    assert policy.general_task("supabase:abuser").allowed is False
    assert policy.general_task("supabase:someone-else").allowed is True


def test_override_survives_a_reload_from_disk(tmp_path):
    path = tmp_path / "user_access_overrides.json"
    first = _policy(tmp_path, general_task_enabled=False)
    first.set_override("telegram:12345", general_task=True)
    reloaded = AccessPolicy(
        config=SimpleNamespace(access=AccessConfig(general_task_enabled=False)),
        path=path,
    )
    assert reloaded.general_task("telegram:12345").allowed is True


def test_clearing_an_override_returns_the_account_to_the_global_switch(tmp_path):
    policy = _policy(tmp_path, general_task_enabled=False)
    policy.set_override("supabase:u", general_task=True)
    assert policy.general_task("supabase:u").allowed is True
    policy.set_override("supabase:u", clear=True)
    assert policy.general_task("supabase:u").allowed is False
    assert policy.overrides() == {}


def test_override_requires_a_key(tmp_path):
    policy = _policy(tmp_path)
    with pytest.raises(ValueError):
        policy.set_override("  ")


def test_corrupt_override_file_is_ignored_not_fatal(tmp_path):
    """A half-written file must not decide who may work."""
    path = tmp_path / "user_access_overrides.json"
    path.write_text("{not json", encoding="utf-8")
    policy = _policy(tmp_path, general_task_enabled=False)
    assert policy.overrides() == {}
    assert policy.general_task("supabase:u").allowed is False


# ----------------------------------------------------------------------- keys


def test_turn_user_key_prefers_the_supabase_identity():
    assert turn_user_key(
        channel="websocket", chat_id="direct", metadata={"supabase_user_id": "u1"}
    ) == "supabase:u1"


def test_turn_user_key_falls_back_to_the_telegram_chat_id():
    assert turn_user_key(channel="telegram", chat_id="4242") == "telegram:4242"


def test_anonymous_turn_has_no_key():
    assert turn_user_key(channel="cli", chat_id="direct") == ""


# ------------------------------------------------------------------------ gate


def _ctx() -> AgentHookContext:
    return AgentHookContext(iteration=1, messages=[])


@pytest.mark.asyncio
async def test_gate_refuses_a_general_turn_when_the_switch_is_off(tmp_path, monkeypatch):
    policy = _policy(tmp_path, general_task_enabled=False)
    monkeypatch.setattr("nanobot.access_policy._policy", policy)
    hook = AccessGateHook(_turn(supabase_user_id="u1"))
    with pytest.raises(AccessDeniedError):
        await hook.before_iteration(_ctx())


@pytest.mark.asyncio
async def test_gate_lets_an_excepted_account_through(tmp_path, monkeypatch):
    policy = _policy(tmp_path, general_task_enabled=False)
    policy.set_override("supabase:u1", general_task=True)
    monkeypatch.setattr("nanobot.access_policy._policy", policy)
    hook = AccessGateHook(_turn(supabase_user_id="u1"))
    await hook.before_iteration(_ctx())


@pytest.mark.asyncio
async def test_gate_never_refuses_a_trading_turn(tmp_path, monkeypatch):
    """The switch sells a trading-only product; it must not lock the buyer out."""
    policy = _policy(tmp_path, general_task_enabled=False)
    monkeypatch.setattr("nanobot.access_policy._policy", policy)
    hook = AccessGateHook(_turn(supabase_user_id="u1", turn_kind="trading"))
    await hook.before_iteration(_ctx())


@pytest.mark.asyncio
async def test_gate_checks_once_per_turn(tmp_path, monkeypatch):
    policy = _policy(tmp_path, general_task_enabled=True)
    calls = {"n": 0}
    real = policy.general_task

    def counting(key=""):
        calls["n"] += 1
        return real(key)

    monkeypatch.setattr(policy, "general_task", counting)
    monkeypatch.setattr("nanobot.access_policy._policy", policy)
    hook = AccessGateHook(_turn(supabase_user_id="u1"))
    await hook.before_iteration(_ctx())
    await hook.before_iteration(_ctx())
    await hook.before_iteration(_ctx())
    assert calls["n"] == 1


def test_factory_skips_anonymous_turns():
    """No identity means no override can name the account; the CLI is untouched."""
    assert create_access_gate_hook(_turn()) is None
    assert create_access_gate_hook(_turn(supabase_user_id="u1")) is not None


def test_trading_turn_detection():
    assert turn_is_trading({"turn_kind": "trading"}) is True
    assert turn_is_trading({"turn_kind": "broker"}) is True
    assert turn_is_trading({"broker_turn": True}) is True
    assert turn_is_trading({"turn_kind": "general"}) is False
    assert turn_is_trading({}) is False
    assert turn_is_trading(None) is False
