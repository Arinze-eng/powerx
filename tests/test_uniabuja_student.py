from __future__ import annotations

import json

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.uniabuja_student import UniAbujaStudentTool

_USER_ID = "6f3c50bd-47fe-4320-9e49-2ceca2e29b2f"
_TELEGRAM_ID = "7757072055"


def _ctx(*, linked: bool = True) -> RequestContext:
    metadata: dict[str, object] = {}
    if linked:
        metadata["supabase_user_id"] = _USER_ID
    return RequestContext(
        channel="telegram",
        chat_id="123",
        sender_id=f"{_TELEGRAM_ID}|student_user",
        session_key="telegram:123",
        metadata=metadata,
    )


def _mock_request(monkeypatch, *, status=None, rows=None):
    calls: list[tuple[str, str, dict[str, str] | None, object]] = []

    def request(method, path, *, params=None, body=None):
        calls.append((method, path, params, body))
        if path.endswith("/telegram_accounts"):
            return [{"telegram_user_id": int(_TELEGRAM_ID), "agentx_user_id": _USER_ID, "auth_email": "student@example.com"}]
        if path.endswith("/rpc/uniabuja_get_access_status"):
            return status or {"read_access": True, "write_access": True, "transcript_access": True}
        if path.endswith("/admin_audit_log"):
            return None
        return rows if rows is not None else []

    monkeypatch.setattr("nanobot.agent.tools.uniabuja_student._request", request)
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_student._configured", lambda: True)
    return calls


@pytest.mark.asyncio
async def test_requires_linked_authenticated_telegram_account(monkeypatch) -> None:
    _mock_request(monkeypatch)
    with request_context(_ctx(linked=False)):
        result = await UniAbujaStudentTool().execute(action="status")
    assert isinstance(result, ToolResult)
    assert "Sign in" in str(result)


@pytest.mark.asyncio
async def test_query_is_gated_by_admin_read_switch(monkeypatch) -> None:
    _mock_request(monkeypatch, status={"read_access": False, "write_access": True, "transcript_access": True})
    with request_context(_ctx()):
        result = await UniAbujaStudentTool().execute(action="query", resource="knowledge")
    assert isinstance(result, ToolResult)
    assert "read access is disabled" in str(result)


@pytest.mark.asyncio
async def test_query_returns_only_non_sensitive_knowledge(monkeypatch) -> None:
    calls = _mock_request(monkeypatch, rows=[{"id": "1", "title": "Guide", "content": "Safe", "is_sensitive": False}])
    with request_context(_ctx()):
        result = await UniAbujaStudentTool().execute(action="query", resource="knowledge")
    assert not isinstance(result, ToolResult)
    payload = json.loads(str(result))
    assert payload["rows"][0]["is_sensitive"] is False
    query_calls = [call for call in calls if call[1].endswith("/training_data")]
    assert query_calls[0][2]["is_sensitive"] == "eq.false"


@pytest.mark.asyncio
async def test_submit_question_is_gated_by_admin_write_switch(monkeypatch) -> None:
    _mock_request(monkeypatch, status={"read_access": True, "write_access": False, "transcript_access": True})
    with request_context(_ctx()):
        result = await UniAbujaStudentTool().execute(action="submit_question", question="Hello")
    assert isinstance(result, ToolResult)
    assert "write access is disabled" in str(result)


@pytest.mark.asyncio
async def test_submit_question_uses_current_user_owner_fields(monkeypatch) -> None:
    calls = _mock_request(monkeypatch, rows=[{"id": 3, "user_id": _USER_ID, "message": "Hello"}])
    with request_context(_ctx()):
        result = await UniAbujaStudentTool().execute(action="submit_question", question="Hello")
    assert not isinstance(result, ToolResult)
    write_calls = [call for call in calls if call[1].endswith("/user_questions") and call[0] == "POST"]
    assert len(write_calls) == 1
    body = write_calls[0][3]
    assert body == {
        "user_id": _USER_ID,
        "title": "Telegram UniAbuja question",
        "message": "Hello",
        "category": "telegram",
        "created_by": _USER_ID,
    }


@pytest.mark.asyncio
async def test_transcript_is_gated_and_scoped_to_sender(monkeypatch) -> None:
    calls = _mock_request(monkeypatch, rows=[])
    with request_context(_ctx()):
        result = await UniAbujaStudentTool().execute(action="transcript")
    assert not isinstance(result, ToolResult)
    transcript_calls = [call for call in calls if call[1].endswith("/telegram_question_history")]
    assert transcript_calls[0][2]["telegram_user_id"] == f"eq.{_TELEGRAM_ID}"
