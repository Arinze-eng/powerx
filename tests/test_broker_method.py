"""Tests for the Brokers method: storage, settings API and the trade tool.

The Brokers method is the user-owned counterpart to the MT5/Wine sandbox: a
user stores their own account number, password and server, and ``broker_trade``
drives that account. What these tests protect:

* a password never leaves the server, in any action or any error;
* one user's accounts are never served to another;
* the feature gate is checked per call, not only at startup;
* the general-task switch does not refuse a trading turn.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from nanobot.access_policy import (
    AccessDecision,
    AccessPolicy,
    message_is_trading,
    reset_access_policy,
    turn_is_trading,
)


class FakeStore:
    """An in-memory stand-in for :class:`BrokerCredentialStore`."""

    enabled = True

    def __init__(self) -> None:
        self.rows: dict[str, list[dict]] = {}

    async def store_credentials(
        self, *, user_key, login, password, server="", broker="", make_default=False
    ):
        rows = self.rows.setdefault(user_key, [])
        for row in rows:
            if row["login"] == login and row["server"] == server:
                row.update(
                    {"broker": broker, "password": password, "is_default": make_default}
                )
                break
        else:
            rows.append(
                {
                    "id": len(rows) + 1,
                    "broker": broker,
                    "server": server,
                    "login": login,
                    "password": password,
                    "is_default": make_default,
                    "updated_at": "2026-10-15T00:00:00+00:00",
                }
            )
        if make_default:
            await self.set_default(user_key, login=login, server=server)
        return {"login": login, "server": server}

    async def list_credentials(self, user_key: str):
        return [
            {key: value for key, value in row.items() if key != "password"}
            for row in self.rows.get(user_key, [])
        ]

    async def get_credentials(self, user_key: str, *, login="", server=""):
        for row in self.rows.get(user_key, []):
            if login and row["login"] != login:
                continue
            if server and row["server"] != server:
                continue
            return {
                "broker": row["broker"],
                "server": row["server"],
                "login": row["login"],
                "password": row["password"],
            }
        return None

    async def set_default(self, user_key: str, *, login: str, server: str = ""):
        for row in self.rows.get(user_key, []):
            row["is_default"] = row["login"] == login

    async def delete_credentials(self, user_key: str, *, login="", server=""):
        rows = self.rows.get(user_key, [])
        self.rows[user_key] = [
            row
            for row in rows
            if not (
                (not login or row["login"] == login)
                and (not server or row["server"] == server)
            )
        ]
        return True


@pytest.fixture()
def store() -> FakeStore:
    return FakeStore()


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- settings API


def test_save_then_list_hides_the_password(store):
    from nanobot.webui.brokers_api import brokers_action_async

    saved = _run(
        brokers_action_async(
            "save",
            user_id="supabase:u1",
            payload={
                "login": "477199408",
                "password": "Miracle123@",
                "server": "Exness-MT5Trial9",
                "broker": "Exness",
            },
            store=store,
        )
    )
    assert saved["count"] == 1
    account = saved["accounts"][0]
    assert account["login"] == "477199408"
    assert account["has_password"] is True
    # The password value appears nowhere in the payload, in any spelling, and
    # no field carries it -- only the boolean that says one is stored.
    assert "Miracle123@" not in json.dumps(saved)
    assert "password" not in account
    assert account["has_password"] is True


def test_save_requires_a_server(store):
    from nanobot.webui.brokers_api import BrokerSettingsError, brokers_action_async

    with pytest.raises(BrokerSettingsError) as excinfo:
        _run(
            brokers_action_async(
                "save",
                user_id="supabase:u1",
                payload={"login": "1", "password": "p", "server": ""},
                store=store,
            )
        )
    assert "server" in str(excinfo.value).lower()


def test_no_identity_is_refused(store):
    from nanobot.webui.brokers_api import BrokerSettingsError, brokers_action_async

    with pytest.raises(BrokerSettingsError) as excinfo:
        _run(brokers_action_async("list", user_id="", store=store))
    assert excinfo.value.status == 401


def test_one_users_accounts_are_not_anothers(store):
    from nanobot.webui.brokers_api import brokers_action_async

    _run(
        brokers_action_async(
            "save",
            user_id="supabase:u1",
            payload={"login": "1", "password": "a", "server": "Exness-MT5Trial9"},
            store=store,
        )
    )
    other = _run(brokers_action_async("list", user_id="supabase:u2", store=store))
    assert other["count"] == 0


def test_delete_removes_the_account(store):
    from nanobot.webui.brokers_api import brokers_action_async

    _run(
        brokers_action_async(
            "save",
            user_id="supabase:u1",
            payload={"login": "1", "password": "a", "server": "Deriv-Demo"},
            store=store,
        )
    )
    after = _run(
        brokers_action_async(
            "delete",
            user_id="supabase:u1",
            payload={"login": "1", "server": "Deriv-Demo"},
            store=store,
        )
    )
    assert after["count"] == 0
    assert after["last_action"] == "deleted"


def test_default_flag_is_exclusive(store):
    from nanobot.webui.brokers_api import brokers_action_async

    for login in ("1", "2"):
        _run(
            brokers_action_async(
                "save",
                user_id="supabase:u1",
                payload={
                    "login": login,
                    "password": "a",
                    "server": "Exness-MT5Trial9",
                    "is_default": True,
                },
                store=store,
            )
        )
    accounts = _run(
        brokers_action_async("list", user_id="supabase:u1", store=store)
    )["accounts"]
    assert sum(1 for account in accounts if account["is_default"]) == 1
    assert next(a for a in accounts if a["is_default"])["login"] == "2"


def test_test_action_reports_the_build_for_a_server(store):
    from nanobot.webui.brokers_api import brokers_action_async

    result = _run(
        brokers_action_async(
            "test",
            user_id="supabase:u1",
            payload={"server": "Deriv-Demo"},
            store=store,
        )
    )
    assert result["ok"] is True
    assert result["profile"]["recognised"] is True


def test_forbidden_capabilities_are_refused_by_the_admin_switch():
    from nanobot.access_policy import configure_access_policy

    policy = AccessPolicy(config=None, path=None)
    configure_access_policy(policy)
    try:
        decision = policy.brokers()
        assert isinstance(decision, AccessDecision)
    finally:
        reset_access_policy()


# ------------------------------------------------------------- trade tool


def test_broker_trade_refuses_when_the_switch_is_off(monkeypatch):
    from nanobot.agent.tools import broker_trade as module

    pytest.importorskip("loguru")

    policy = AccessPolicy(config=None, path=None)
    monkeypatch.setattr(policy, "brokers", lambda *a, **k: AccessDecision(
        feature="brokers", allowed=False, source="global", reason="Brokers are off."
    ))
    monkeypatch.setattr(module, "access_policy", lambda: policy)

    tool = module.BrokerTradeTool()
    result = asyncio.run(tool.execute(action="account"))
    assert getattr(result, "is_error", False) is True
    assert "brokers_disabled" in str(result)


def test_broker_trade_refuses_without_stored_credentials(monkeypatch):
    from nanobot.agent.tools import broker_trade as module

    policy = AccessPolicy(config=None, path=None)
    monkeypatch.setattr(
        module,
        "access_policy",
        lambda: policy,
    )
    tool = module.BrokerTradeTool()
    monkeypatch.setattr(
        module.BrokerTradeTool,
        "_current_user_key",
        staticmethod(lambda: "supabase:u1"),
    )

    class Disabled:
        enabled = False

    monkeypatch.setattr(
        "nanobot.trading.broker_credentials.BrokerCredentialStore",
        lambda: Disabled(),
    )
    result = asyncio.run(tool.execute(action="account"))
    assert "broker_not_connected" in str(result)


def test_broker_trade_rejects_an_unknown_action(monkeypatch):
    from nanobot.agent.tools import broker_trade as module

    policy = AccessPolicy(config=None, path=None)
    monkeypatch.setattr(module, "access_policy", lambda: policy)
    tool = module.BrokerTradeTool()
    result = asyncio.run(tool.execute(action="launch_missiles"))
    assert "Unknown action" in str(result)


# ------------------------------------------------------- trading exemption


def test_turn_is_trading_from_metadata():
    assert turn_is_trading({"turn_kind": "trading"}) is True
    assert turn_is_trading({"broker_turn": True}) is True
    assert turn_is_trading({}) is False


def test_turn_is_trading_from_the_opening_message():
    messages = [{"role": "user", "content": "buy 0.1 lots of XAUUSD"}]
    assert turn_is_trading({}, messages) is True
    messages = [{"role": "user", "content": "write me a python script"}]
    assert turn_is_trading({}, messages) is False


def test_message_is_trading_matches_the_broker_examples():
    assert message_is_trading("connect my exness broker, account 477199408")
    assert message_is_trading("what is my account balance")
    assert message_is_trading("close the position with ticket 123")


def test_message_is_trading_ignores_ordinary_prose():
    # The bug the first version of this heuristic shipped: a blanket
    # "six letters" rule matched "please" and "create", so nearly every turn
    # was exempted from the switch it was written to respect.
    for text in (
        "please summarise this document",
        "create a landing page for my coffee shop",
        "thanks, that works",
        "fix the error in my script",
    ):
        assert message_is_trading(text) is False, text


# --------------------------------------------------- storage statement shape


class _RecordingResponse:
    def __init__(self, status_code: int = 200, payload: object = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = b"" if payload is None else b"[]"
        self.text = ""

    def json(self):
        return self._payload


class _RecordingClient:
    """Stands in for ``httpx.AsyncClient``: records statements, answers canned rows.

    A save is several statements in a row against Supabase, and how many of them
    there are is what the user waits on, so the sequence is asserted directly
    rather than inferred from a fake store that cannot show it.
    """

    instances: list["_RecordingClient"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.calls: list[dict] = []
        self.is_closed = False
        self.kwargs = kwargs
        _RecordingClient.instances.append(self)

    async def request(self, method, url, *, headers=None, params=None, json=None):
        self.calls.append(
            {"method": method, "url": url, "params": dict(params or {}), "body": json}
        )
        if method == "PATCH":
            return _RecordingResponse(204)
        if method == "POST":
            return _RecordingResponse(
                201,
                [
                    {
                        "id": 7,
                        "user_key": (json or {}).get("user_key"),
                        "broker": "",
                        "server": (json or {}).get("server"),
                        "login": (json or {}).get("login"),
                        "is_default": bool((json or {}).get("is_default")),
                    }
                ],
            )
        return _RecordingResponse(
            200,
            [
                {
                    "id": 7,
                    "broker": "",
                    "server": "Deriv-Demo",
                    "login": "41261482",
                    "is_default": True,
                    "updated_at": "2026-10-16T00:00:00+00:00",
                }
            ],
        )


@pytest.fixture()
def recording_store(monkeypatch):
    """A real store whose HTTP client records what it was asked to do."""
    import base64

    import httpx

    from nanobot.trading import broker_credentials as module

    _RecordingClient.instances = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setenv(
        "SUPABASE_TOKEN_ENCRYPTION_KEY",
        base64.b64encode(b"0" * 32).decode(),
    )
    return module.BrokerCredentialStore()


def test_a_default_save_clears_the_flag_before_it_writes(recording_store):
    """One PATCH, then the write. Four statements became three, and no window
    exists in which two accounts are both the default."""
    store = recording_store
    _run(
        store.store_credentials(
            user_key="u1",
            login="41261482",
            password="secret",
            server="Deriv-Demo",
            make_default=True,
        )
    )
    calls = _RecordingClient.instances[0].calls
    assert [call["method"] for call in calls] == ["PATCH", "POST"]
    assert calls[0]["params"] == {"user_key": "eq.u1"}
    assert calls[0]["body"] == {"is_default": False}
    assert calls[1]["body"]["is_default"] is True
    assert calls[1]["params"]["on_conflict"] == "user_key,login,server"


def test_a_plain_save_is_one_statement(recording_store):
    store = recording_store
    _run(
        store.store_credentials(
            user_key="u1",
            login="41261482",
            password="secret",
            server="Deriv-Demo",
        )
    )
    calls = _RecordingClient.instances[0].calls
    assert [call["method"] for call in calls] == ["POST"]


def test_a_save_reuses_one_http_client(recording_store):
    """The TLS handshake is paid once per store, not once per statement.

    Two saves on the gateway's one long-lived loop, which is the shape the
    deployment runs: one client, and so one handshake for both.
    """
    store = recording_store

    async def save_twice():
        for login in ("1", "2"):
            await store.store_credentials(
                user_key="u1",
                login=login,
                password="secret",
                server="Deriv-Demo",
            )

    _run(save_twice())
    assert len(_RecordingClient.instances) == 1
    assert len(_RecordingClient.instances[0].calls) == 2


def test_a_store_survives_a_second_event_loop(recording_store):
    """Tests and scripts drive the store from a new ``asyncio.run`` each time;
    a client carried across loops would fail with "Event loop is closed"."""
    store = recording_store
    for _ in range(2):
        _run(
            store.store_credentials(
                user_key="u1",
                login="41261482",
                password="secret",
                server="Deriv-Demo",
            )
        )
    assert len(_RecordingClient.instances) == 2


def test_the_brokers_pane_keeps_one_store(monkeypatch):
    from nanobot.trading import broker_credentials as module
    from nanobot.webui import brokers_api

    monkeypatch.setattr(module, "_SHARED_STORE", None)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    assert brokers_api._store() is brokers_api._store()
    # The pane and the trade tool resolve the same instance, so a trade pays no
    # second TLS handshake to reach the row the pane just read.
    assert brokers_api._store() is module.shared_store()


def test_the_trade_tool_keeps_the_context_it_was_created_with():
    """The loader instantiates every tool through ``create(ctx)``, and the base
    implementation is ``cls()`` -- which drops the context. Without an override
    ``getattr(self, "_ctx", None)`` was always None, ``_sandbox_tool(None)``
    always returned None, and broker_trade answered "No execution sandbox is
    configured" on every deployment, sandbox up or not."""
    from types import SimpleNamespace

    from nanobot.agent.tools.broker_trade import BrokerTradeTool, _sandbox_tool

    class _Sandbox:
        name = "novita_sandbox"

    class _Registry:
        def get(self, name):
            return _Sandbox() if name == "novita_sandbox" else None

    ctx = SimpleNamespace(tool_registry=_Registry())
    tool = BrokerTradeTool.create(ctx)
    assert tool._ctx is ctx
    assert _sandbox_tool(tool._ctx) is not None
    # The default create() would not have carried it: this is the regression.
    assert _sandbox_tool(getattr(BrokerTradeTool(), "_ctx", None)) is None


# ------------------------------------- one identity, two spellings of one key


def test_a_web_turn_key_and_the_pane_key_reach_the_same_row():
    """The regression that made a saved account untradeable.

    Settings -> Brokers stores the bare Supabase user id; ``broker_trade``
    resolves the same user through ``turn_user_key``, which namespaces it as
    ``supabase:<uuid>``. Written verbatim those are two different rows, so the
    tool answered ``broker_not_connected`` for a user looking at the account on
    screen. Both spellings must fold onto one row.
    """
    from nanobot.trading.broker_credentials import credential_user_key

    assert credential_user_key("supabase:c9c56243-7cd3-4fa5-b91d-736ee4364b16") == (
        "c9c56243-7cd3-4fa5-b91d-736ee4364b16"
    )
    assert credential_user_key("c9c56243-7cd3-4fa5-b91d-736ee4364b16") == (
        "c9c56243-7cd3-4fa5-b91d-736ee4364b16"
    )
    # A Telegram key is a different namespace and is left alone.
    assert credential_user_key("telegram:12345") == "telegram:12345"
    assert credential_user_key("") == ""
    assert credential_user_key("  supabase:u1  ") == "u1"


def test_a_save_under_the_prefixed_key_is_readable_under_the_bare_one(recording_store):
    store = recording_store
    _run(
        store.store_credentials(
            user_key="supabase:u1",
            login="41261482",
            password="secret",
            server="Deriv-Demo",
        )
    )
    post = _RecordingClient.instances[0].calls[-1]
    assert post["body"]["user_key"] == "u1"

    # A fresh event loop per ``_run`` means a fresh client (see
    # test_a_store_survives_a_second_event_loop), so the read is on the last one.
    _run(store.list_credentials("u1"))
    read = _RecordingClient.instances[-1].calls[-1]
    assert read["params"]["user_key"] == "eq.u1"
    assert read["params"]["select"].startswith("id,broker,server,login,is_default,updated_at")


def test_the_tool_finds_an_account_the_pane_saved(monkeypatch):
    """End to end across the two entry points, at the identity boundary."""
    from nanobot.agent.tools.broker_trade import BrokerTradeTool
    from nanobot.trading import broker_credentials as module

    saved: list[dict] = []

    class _Store:
        enabled = True

        async def get_credentials(self, user_key, *, login="", server=""):
            saved.append({"user_key": user_key, "login": login})
            if module.credential_user_key(user_key) != "u1":
                return None
            return {"login": "41261482", "password": "secret", "server": "Deriv-Demo"}

    monkeypatch.setattr(module, "shared_store", lambda: _Store())
    monkeypatch.setattr(BrokerTradeTool, "_current_user_key", staticmethod(lambda: "supabase:u1"))

    tool = BrokerTradeTool()
    found = _run(tool._account_credentials())
    assert found == {"login": "41261482", "password": "secret", "server": "Deriv-Demo"}
    # The tool still speaks the access-policy shape; only the store folds it.
    assert saved == [{"user_key": "supabase:u1", "login": ""}]


# --------------------------------------------- the connection state on the row


def test_connection_outcome_reads_ok_not_just_that_the_command_ran():
    from nanobot.agent.tools.broker_trade import _connection_outcome

    assert _connection_outcome({"ok": True, "account_login": 41261482}) == ("connected", "")
    status, message = _connection_outcome(
        {"ok": False, "hint": "Terminal is up but has no account."}
    )
    assert status == "failed"
    assert "no account" in message
    # `failure` is the terminal's own diagnosis and outranks the generic hint.
    assert _connection_outcome({"ok": False, "failure": "server mismatch", "hint": "x"}) == (
        "failed",
        "server mismatch",
    )


def test_only_actions_that_prove_a_login_record_a_connection():
    from nanobot.agent.tools.broker_trade import _CONNECTION_EVIDENCE

    assert "connect" in _CONNECTION_EVIDENCE
    assert "order" in _CONNECTION_EVIDENCE
    # A working install is not a login: recording it would show an account that
    # never authorized as connected.
    assert "install" not in _CONNECTION_EVIDENCE
    assert "status" not in _CONNECTION_EVIDENCE
    assert "doctor" not in _CONNECTION_EVIDENCE


def test_recording_a_connection_writes_the_state_and_the_time(recording_store):
    store = recording_store
    ok = _run(
        store.record_connection(
            "supabase:u1",
            login="41261482",
            server="Deriv-Demo",
            status="connected",
        )
    )
    assert ok is True
    call = _RecordingClient.instances[0].calls[-1]
    assert call["method"] == "PATCH"
    assert call["params"] == {
        "user_key": "eq.u1",
        "login": "eq.41261482",
        "server": "eq.Deriv-Demo",
    }
    assert call["body"]["connection_status"] == "connected"
    assert call["body"]["connection_at"]


def test_a_failed_connection_record_never_fails_the_trade(recording_store):
    """A report about a trade is not a condition of it: a Supabase hiccup while
    writing the status must not turn a filled order into a failed tool call."""
    store = recording_store

    async def boom(*args, **kwargs):
        raise RuntimeError("supabase down")

    store._request = boom  # type: ignore[assignment]
    assert _run(store.record_connection("u1", login="1", status="connected")) is False
    assert _run(store.record_connection("u1", login="", status="connected")) is False


def test_a_row_payload_carries_the_connection_state():
    from nanobot.webui.brokers_api import _row_payload

    row = {
        "id": 6,
        "broker": "Deriv",
        "server": "Deriv-Demo",
        "login": "41261482",
        "is_default": True,
        "updated_at": "2026-10-16T05:28:35+00:00",
        "connection_status": "connected",
        "connection_message": "",
        "connection_at": "2026-10-17T07:00:00+00:00",
    }
    payload = _row_payload(row)
    assert payload["connection"]["status"] == "connected"
    assert payload["connection"]["connected"] is True
    assert payload["connection"]["at"] == "2026-10-17T07:00:00+00:00"
    # Never the password, and never the ciphertext.
    assert "password" not in json.dumps(payload).replace("has_password", "")

    # An account saved but never used is not reported as connected.
    never = _row_payload({"login": "1", "server": "Exness-MT5Trial9"})
    assert never["connection"] == {
        "status": "",
        "connected": False,
        "message": "",
        "at": "",
    }


def test_listing_survives_a_table_without_the_connection_columns(monkeypatch):
    """A deployment whose table predates the columns still has to list: the
    pane shows "not connected yet" instead of an error."""
    import base64

    import httpx

    from nanobot.trading import broker_credentials as module

    class _OldTableClient(_RecordingClient):
        """A table created before the connection columns existed."""

        async def request(self, method, url, *, headers=None, params=None, json=None):
            if "connection_status" in (params or {}).get("select", ""):
                self.calls.append({"method": method, "params": dict(params or {})})
                refusal = _RecordingResponse(400, None)
                refusal.content = b"{}"
                refusal.text = (
                    '{"code":"42703","message":"column broker_credentials.'
                    'connection_status does not exist"}'
                )
                return refusal
            return await super().request(
                method, url, headers=headers, params=params, json=json
            )

    _RecordingClient.instances = []
    monkeypatch.setattr(httpx, "AsyncClient", _OldTableClient)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setenv("SUPABASE_TOKEN_ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())

    store = module.BrokerCredentialStore()
    rows = _run(store.list_credentials("u1"))
    assert rows and rows[0]["login"] == "41261482"
    selects = [call["params"]["select"] for call in _RecordingClient.instances[0].calls]
    assert len(selects) == 2
    assert "connection_status" in selects[0]
    assert "connection_status" not in selects[1]
