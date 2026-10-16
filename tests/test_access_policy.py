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


# ------------------------------------------------------- the admin page's route
#
# The switches are only real if the admin page can read and write them. These
# cases drive ``admin_route`` -- the exact entry point the admin WebSocket's
# mutation channel and the dashboard's own fetches land on -- so a broken route
# cannot pass while the policy underneath is healthy.


def _admin_request(path, *, payload=None, password="nethunter", basic=True):
    import base64

    headers = {}
    if basic:
        encoded = base64.b64encode(f"admin:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
    request = SimpleNamespace(path=path, headers=headers)
    if payload is not None:
        request._nanobot_webui_mutation_payload = payload
    return request


def _call_admin(monkeypatch, tmp_path, path, payload=None, *, password="nethunter", basic=True):
    import json

    from nanobot import admin_registry

    monkeypatch.setenv("ADMIN_PASSWORD", password)
    monkeypatch.setattr(admin_registry, "_config_path", lambda: tmp_path / "config.json")
    refreshes: list[int] = []
    response = admin_registry.admin_route(
        _admin_request(path, payload=payload, password=password, basic=basic),
        path,
        issue_admin_token=lambda: "tok",
        ws_path="/",
        refresh_runtime_config=lambda: refreshes.append(1),
    )
    assert response is not None, path
    raw = bytes(response.body).decode()
    try:
        body = json.loads(raw)
    except ValueError:
        # The auth failure answers a plain-text body, not JSON.
        body = raw
    return response.status_code, body, refreshes


def test_admin_access_route_requires_a_password(monkeypatch, tmp_path):
    status, body, _ = _call_admin(
        monkeypatch, tmp_path, "/api/admin/access-settings", basic=False
    )
    assert status == 401, body


def test_admin_access_switches_read_write_and_read_back(monkeypatch, tmp_path):
    """Every switch round-trips: the save is the read the page renders next."""
    status, body, refreshes = _call_admin(
        monkeypatch, tmp_path, "/api/admin/access-settings"
    )
    assert status == 200, body
    assert body["global"] == {
        "general_task_enabled": True,
        "mt5_trading_enabled": True,
        "brokers_enabled": True,
        "default_broker": "",
        "default_server": "",
    }

    status, body, refreshes = _call_admin(
        monkeypatch,
        tmp_path,
        "/api/admin/access-settings",
        {
            "action": "admin.access.save",
            "generalTaskEnabled": False,
            "mt5TradingEnabled": False,
            "brokersEnabled": True,
            "defaultBroker": "Exness",
            "defaultServer": "Exness-MT5Trial9",
        },
    )
    assert status == 200, body
    assert body["global"] == {
        "general_task_enabled": False,
        "mt5_trading_enabled": False,
        "brokers_enabled": True,
        "default_broker": "exness",
        "default_server": "Exness-MT5Trial9",
    }
    assert body["features"]["general_task"]["allowed"] is False
    assert body["features"]["mt5_trading"]["allowed"] is False
    assert body["features"]["brokers"]["allowed"] is True
    assert refreshes == [1]

    # A fresh read of the same route must show what was just saved, or the
    # page snaps back to the old value on reload and the switch looks dead.
    status, body, _ = _call_admin(monkeypatch, tmp_path, "/api/admin/access-settings")
    assert status == 200, body
    assert body["global"]["general_task_enabled"] is False
    assert body["global"]["default_broker"] == "exness"


def test_admin_access_switch_write_is_partial(monkeypatch, tmp_path):
    """A page that does not render a switch cannot silently reset it."""
    _call_admin(
        monkeypatch,
        tmp_path,
        "/api/admin/access-settings",
        {"mt5TradingEnabled": False},
    )
    status, body, _ = _call_admin(monkeypatch, tmp_path, "/api/admin/access-settings")
    assert status == 200, body
    assert body["global"]["mt5_trading_enabled"] is False
    assert body["global"]["general_task_enabled"] is True
    assert body["global"]["brokers_enabled"] is True


def test_admin_access_override_sets_and_clears(monkeypatch, tmp_path):
    status, body, _ = _call_admin(
        monkeypatch,
        tmp_path,
        "/api/admin/access-overrides",
        {
            "action": "admin.access.override",
            "userKey": "supabase:user-1",
            "generalTask": True,
            "brokers": False,
        },
    )
    assert status == 200, body
    assert body["overrides"]["supabase:user-1"] == {"general_task": True, "brokers": False}

    status, body, _ = _call_admin(
        monkeypatch,
        tmp_path,
        "/api/admin/access-overrides",
        {"action": "admin.access.override", "userKey": "supabase:user-1", "clear": True},
    )
    assert status == 200, body
    assert body["overrides"] == {}


def test_admin_access_override_needs_a_key(monkeypatch, tmp_path):
    status, body, _ = _call_admin(
        monkeypatch,
        tmp_path,
        "/api/admin/access-overrides",
        {"action": "admin.access.override", "clear": True},
    )
    assert status == 400, body
