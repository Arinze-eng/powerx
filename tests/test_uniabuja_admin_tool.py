from __future__ import annotations

import json

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.uniabuja_admin import UniAbujaAdminTool
from nanobot.runtime_context import (
    RUNTIME_CONTEXT_INPUT_META,
    RuntimeContextBlock,
    wrap_runtime_context_lines,
)

_ADMIN_ID = "6f3c50bd-47fe-4320-9e49-2ceca2e29b2f"
_ADMIN_TELEGRAM_ID = "7757072055"


def _ctx(*, verified: bool = True, channel: str = "telegram") -> RequestContext:
    metadata: dict[str, object] = {}
    if verified:
        metadata[RUNTIME_CONTEXT_INPUT_META] = [
            RuntimeContextBlock(
                source="telegram_verified_admin",
                content=wrap_runtime_context_lines(
                    [
                        "The current Telegram sender is the verified Minis Bot administrator.",
                        "Verified administrator account: allisonarinze@gmail.com",
                    ]
                ),
            )
        ]
    return RequestContext(
        channel=channel,
        chat_id="123",
        sender_id=f"{_ADMIN_TELEGRAM_ID}|allisonarinze",
        session_key="telegram:123",
        metadata=metadata,
    )


def _mock_request(monkeypatch, *, status=None, rows=None, calls=None):
    calls = calls if calls is not None else []

    def request(method, path, *, params=None, body=None):
        calls.append((method, path, params, body))
        if path.endswith("/telegram_accounts"):
            return [{"agentx_user_id": _ADMIN_ID, "auth_email": "allisonarinze@gmail.com"}]
        if path.endswith("/rpc/uniabuja_get_access_status"):
            return status or {
                "is_admin": True,
                "read_access": True,
                "write_access": True,
                "transcript_access": True,
                "updated_at": "now",
            }
        if path.endswith("/admin_audit_log"):
            return None
        return rows if rows is not None else []

    monkeypatch.setattr("nanobot.agent.tools.uniabuja_admin._request", request)
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_admin._configured", lambda: True)
    return calls


@pytest.mark.asyncio
async def test_denies_without_verified_telegram_admin_context(monkeypatch) -> None:
    tool = UniAbujaAdminTool()
    with request_context(_ctx(verified=False)):
        result = await tool.execute(action="list_tables")
    assert result.is_error is True
    assert "Verified administrator authorization is required" in str(result)


@pytest.mark.asyncio
async def test_denies_verified_context_from_non_telegram_channel(monkeypatch) -> None:
    tool = UniAbujaAdminTool()
    with request_context(_ctx(channel="websocket")):
        result = await tool.execute(action="list_tables")
    assert result.is_error is True
    assert "only to the verified Telegram administrator" in str(result)


@pytest.mark.asyncio
async def test_reads_allowlisted_table_and_audits(monkeypatch) -> None:
    calls = _mock_request(
        monkeypatch,
        rows=[{"id": True, "ai_read_access": True, "remote_exec_access": False}],
    )
    tool = UniAbujaAdminTool()
    with request_context(_ctx()):
        result = await tool.execute(
            action="read",
            table="uniabuja_ai_access_controls",
            columns="id,ai_read_access,remote_exec_access",
            filters="{}",
        )
    assert not isinstance(result, ToolResult)
    payload = json.loads(str(result))
    assert payload["rows"] == [{"id": True, "ai_read_access": True, "remote_exec_access": False}]
    assert any(path.endswith("/admin_audit_log") for _, path, _, _ in calls)


@pytest.mark.asyncio
async def test_rejects_secret_tables_and_unknown_filter_columns(monkeypatch) -> None:
    _mock_request(monkeypatch)
    tool = UniAbujaAdminTool()
    with request_context(_ctx()):
        secret_result = await tool.execute(action="read", table="novita_admin_secrets", filters="{}")
        filter_result = await tool.execute(
            action="read",
            table="profiles",
            filters='{"unknown_column":"x"}',
        )
    assert secret_result.is_error is True
    assert "not in the protected UniAbuja allowlist" in str(secret_result)
    assert filter_result.is_error is True
    assert "Filter column is not allowed" in str(filter_result)


@pytest.mark.asyncio
async def test_write_is_blocked_when_database_switch_is_off(monkeypatch) -> None:
    _mock_request(
        monkeypatch,
        status={"is_admin": True, "read_access": True, "write_access": False, "transcript_access": True},
    )
    tool = UniAbujaAdminTool()
    with request_context(_ctx()):
        result = await tool.execute(
            action="write",
            table="training_data",
            mode="insert",
            record=json.dumps({"title": "T", "content": "C", "is_sensitive": False}),
        )
    assert result.is_error is True
    assert "write access is disabled" in str(result)


@pytest.mark.asyncio
async def test_write_uses_only_approved_fields_and_admin_owner(monkeypatch) -> None:
    calls = _mock_request(monkeypatch, rows=[{"id": "new-id"}])
    tool = UniAbujaAdminTool()
    with request_context(_ctx()):
        result = await tool.execute(
            action="write",
            table="admin_agent_policy_memory",
            mode="insert",
            record=json.dumps({
                "scope_type": "telegram",
                "scope_key": "admin",
                "title": "Helpful policy",
                "instruction": "Be concise.",
                "version": 1,
                "is_active": True,
                "metadata": {"note": "safe"},
            }),
        )
    assert not isinstance(result, ToolResult)
    write_calls = [call for call in calls if call[1].endswith("/admin_agent_policy_memory")]
    assert len(write_calls) == 1
    body = write_calls[0][3]
    assert body["admin_id"] == _ADMIN_ID
    assert "id" not in body
    assert any(path.endswith("/admin_audit_log") for _, path, _, _ in calls)


@pytest.mark.asyncio
async def test_set_access_cannot_enable_remote_exec(monkeypatch) -> None:
    _mock_request(monkeypatch)
    tool = UniAbujaAdminTool()
    with request_context(_ctx()):
        result = await tool.execute(
            action="set_access",
            read_access="true",
            write_access="true",
            transcript_access="true",
            remote_exec_access="true",
        )
    assert result.is_error is True
    assert "cannot be enabled" in str(result)
