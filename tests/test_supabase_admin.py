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
