from __future__ import annotations

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.uniabuja_transcript import UniAbujaTranscriptTool

_ADMIN_ID = "6f3c50bd-47fe-4320-9e49-2ceca2e29b2f"
_ADMIN_TELEGRAM_ID = "7757072055"
_STUDENT_ID = "235eb41f-ea04-408c-9481-42f95c536c26"
_STUDENT_TELEGRAM_ID = "7381268208"


def _ctx(sender_id: str, *, supabase_id: str | None = None) -> RequestContext:
    metadata = {"supabase_user_id": supabase_id} if supabase_id else {}
    return RequestContext(
        channel="telegram",
        chat_id="123",
        sender_id=sender_id,
        session_key="telegram:123",
        metadata=metadata,
    )


def _admin(monkeypatch, *, status=None, sql_result=None):
    calls: list[tuple[str, object]] = []

    def request(sql: str):
        calls.append(("sql", sql))
        return sql_result if sql_result is not None else []

    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._configured", lambda: True)
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._verified_admin_context", lambda: (_ctx(f"{_ADMIN_TELEGRAM_ID}|admin"), _ADMIN_ID))
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._access_status", lambda: status or {"read_access": True, "write_access": True})
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._sql", request)
    return calls


@pytest.mark.asyncio
async def test_admin_student_lookup_uses_txt_schema_query(monkeypatch) -> None:
    calls = _admin(monkeypatch, sql_result=[{"regno": "22/205EEE/172"}])
    with request_context(_ctx(f"{_ADMIN_TELEGRAM_ID}|admin")):
        result = await UniAbujaTranscriptTool().execute(
            action="student_lookup",
            regno="22/205EEE/172",
            session="2025/2026",
        )
    assert not isinstance(result, ToolResult)
    query = calls[0][1]
    assert "FROM studenttb" in query
    assert "FROM regtb" in query
    assert "22/205EEE/172" in query


@pytest.mark.asyncio
async def test_student_lookup_is_scoped_to_authenticated_portal_email(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._configured", lambda: True)
    monkeypatch.setattr(
        "nanobot.agent.tools.uniabuja_transcript._verified_admin_context",
        lambda: (_ for _ in ()).throw(RuntimeError("not admin")),
    )
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._student_identity", lambda: (_ctx(f"{_STUDENT_TELEGRAM_ID}|student", supabase_id=_STUDENT_ID), _STUDENT_ID))
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._status", lambda: {"read_access": True, "write_access": False, "transcript_access": True})
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._student_auth_email", lambda ctx: "student@example.com")

    def sql(query: str):
        calls.append(query)
        return [{"regno": "22/205EEE/172"}]

    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._sql", sql)
    with request_context(_ctx(f"{_STUDENT_TELEGRAM_ID}|student", supabase_id=_STUDENT_ID)):
        result = await UniAbujaTranscriptTool().execute(action="student_lookup", regno="22/999EEE/999")
    assert not isinstance(result, ToolResult)
    assert len(calls) == 1
    assert "student@example.com" in calls[0]
    assert "22/999EEE/999" not in calls[0]


@pytest.mark.asyncio
async def test_student_score_check_is_blocked_when_read_switch_is_off(monkeypatch) -> None:
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._configured", lambda: True)
    monkeypatch.setattr(
        "nanobot.agent.tools.uniabuja_transcript._verified_admin_context",
        lambda: (_ for _ in ()).throw(RuntimeError("not admin")),
    )
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._student_identity", lambda: (_ctx(f"{_STUDENT_TELEGRAM_ID}|student", supabase_id=_STUDENT_ID), _STUDENT_ID))
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._status", lambda: {"read_access": False, "write_access": False, "transcript_access": False})
    with request_context(_ctx(f"{_STUDENT_TELEGRAM_ID}|student", supabase_id=_STUDENT_ID)):
        result = await UniAbujaTranscriptTool().execute(action="score_check", course="FEG412", session="2025/2026", semester="1st")
    assert isinstance(result, ToolResult)
    assert "read access is disabled" in str(result)


@pytest.mark.asyncio
async def test_admin_score_write_is_dry_run_without_confirmation(monkeypatch) -> None:
    calls = _admin(monkeypatch, status={"read_access": True, "write_access": True})
    with request_context(_ctx(f"{_ADMIN_TELEGRAM_ID}|admin")):
        result = await UniAbujaTranscriptTool().execute(
            action="score_update",
            regno="22/205EEE/172",
            course="FEG412",
            session="2025/2026",
            semester="1st",
            ca="20",
            exam="20",
            operator="ACA_ADMIN",
            confirm="false",
        )
    assert not isinstance(result, ToolResult)
    assert "DRY RUN" in str(result)
    assert calls == []


@pytest.mark.asyncio
async def test_non_admin_cannot_use_schema_or_write(monkeypatch) -> None:
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._configured", lambda: True)
    monkeypatch.setattr(
        "nanobot.agent.tools.uniabuja_transcript._verified_admin_context",
        lambda: (_ for _ in ()).throw(RuntimeError("not admin")),
    )
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._student_identity", lambda: (_ctx(f"{_STUDENT_TELEGRAM_ID}|student", supabase_id=_STUDENT_ID), _STUDENT_ID))
    monkeypatch.setattr("nanobot.agent.tools.uniabuja_transcript._status", lambda: {"read_access": True, "write_access": True, "transcript_access": True})
    with request_context(_ctx(f"{_STUDENT_TELEGRAM_ID}|student", supabase_id=_STUDENT_ID)):
        result = await UniAbujaTranscriptTool().execute(action="schema", table="studenttb")
    assert isinstance(result, ToolResult)
    assert "requires the verified administrator" in str(result)
