from __future__ import annotations

import pytest

import nanobot.supabase_admin as supabase_admin


def test_broadcast_announcement_deduplicates_chats_and_reports_failures(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    calls: list[tuple[str, str]] = []

    def request(method: str, path: str, *, json=None, params=None):
        calls.append((method, path))
        if method == "GET" and path == "/rest/v1/telegram_accounts":
            return [{"chat_id": "101"}, {"chat_id": "101"}, {"chat_id": "202"}, {"chat_id": None}]
        return None

    sent: list[tuple[int, str, str]] = []

    def send(token: str, chat_id: int, text: str) -> bool:
        sent.append((chat_id, token, text))
        return chat_id == 101

    monkeypatch.setattr(supabase_admin, "_request", request)
    monkeypatch.setattr(supabase_admin, "_send_telegram_announcement", send)

    result = supabase_admin.broadcast_announcement("Maintenance", "Back soon")

    assert result == {
        "ok": True,
        "action": "announcement",
        "total": 2,
        "sent": 1,
        "failed": 1,
        "completed_at": result["completed_at"],
    }
    assert [chat_id for chat_id, _, _ in sent] == [101, 202]
    assert all(token == "test-token" for _, token, _ in sent)
    assert all(text == "Maintenance\n\nBack soon" for _, _, text in sent)
    assert calls == [
        ("POST", "/rest/v1/announcements"),
        ("GET", "/rest/v1/telegram_accounts"),
    ]


def test_broadcast_announcement_requires_telegram_configuration(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(supabase_admin.SupabaseAdminError, match="not configured"):
        supabase_admin.broadcast_announcement("Title", "Message")


def test_latest_active_announcement_none_when_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(supabase_admin, "configured", lambda: False)
    assert supabase_admin.latest_active_announcement() is None


def test_latest_active_announcement_returns_newest_active_row(monkeypatch) -> None:
    monkeypatch.setattr(supabase_admin, "configured", lambda: True)
    seen: dict = {}

    def request(method: str, path: str, *, json=None, params=None):
        seen["method"] = method
        seen["path"] = path
        seen["params"] = params
        return [
            {
                "id": "a1",
                "title": "Maintenance",
                "message": "Down at 9pm",
                "created_at": "2026-09-13T00:00:00Z",
            }
        ]

    monkeypatch.setattr(supabase_admin, "_request", request)
    result = supabase_admin.latest_active_announcement()
    assert result == {
        "id": "a1",
        "title": "Maintenance",
        "message": "Down at 9pm",
        "created_at": "2026-09-13T00:00:00Z",
    }
    assert seen["method"] == "GET"
    assert seen["path"] == "/rest/v1/announcements"
    # only active rows, newest first, one row
    assert seen["params"]["is_active"] == "eq.true"
    assert seen["params"]["order"] == "created_at.desc"
    assert seen["params"]["limit"] == "1"


def test_latest_active_announcement_empty_and_error_degrade_to_none(monkeypatch) -> None:
    monkeypatch.setattr(supabase_admin, "configured", lambda: True)
    monkeypatch.setattr(supabase_admin, "_request", lambda *a, **k: [])
    assert supabase_admin.latest_active_announcement() is None

    def boom(*a, **k):
        raise supabase_admin.SupabaseAdminError("table missing")

    monkeypatch.setattr(supabase_admin, "_request", boom)
    assert supabase_admin.latest_active_announcement() is None


def test_latest_active_announcement_skips_blank_message(monkeypatch) -> None:
    monkeypatch.setattr(supabase_admin, "configured", lambda: True)
    monkeypatch.setattr(
        supabase_admin,
        "_request",
        lambda *a, **k: [{"id": "x", "title": "Hi", "message": "   ", "created_at": None}],
    )
    assert supabase_admin.latest_active_announcement() is None
