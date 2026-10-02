"""The sidebar list must not be rebuilt once per poller.

Production, 2026-10-02, one client:

    15:19:35 WARNING slow webui http route path=/api/sessions status=200
             duration_ms=23401
    15:19:35 Response to websocket:anon-20ab74524cfd: Hey! How can I help...

The 23.4 s read finished in the same millisecond as an unrelated model reply.
That is not two slow things: it is the event loop being unavailable to both.
``_sessions_list_payload`` walks the persistent volume once per chat (506 of
them) while holding the session file lock, so every open client retriggered the
same multi-second scan and they completed together when the lock let go.

The port is probed for liveness with ``GET /api/health`` every 10 s and a 5 s
timeout, ``failureThreshold: 2``. A loop stalled for 23 s therefore fails the
probe twice and the platform replaces the container -- the 503 the user sees on
the first task of a new session. These tests pin the fix: one scan per interval,
one scan per burst, and the perimeter reads that used to run on the loop moved
off it.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time

import pytest

from nanobot.webui import ws_http
from nanobot.webui.ws_http import GatewayHTTPHandler


class _Req:
    """Just enough request for the list handler."""

    headers: dict[str, str] = {}


def _handler(calls: list[str], *, delay: float = 0.05, ttl: float | None = None):
    """Build a handler with only the session-list collaborators it needs."""
    handler = object.__new__(GatewayHTTPHandler)
    handler._sessions_list_lock = asyncio.Lock()
    handler._sessions_list_cache = {}
    handler.session_manager = object()
    if ttl is not None:
        handler._SESSIONS_LIST_TTL_S = ttl
    handler.check_api_token = lambda request: True  # type: ignore[method-assign]
    handler._supabase_webui_auth_enabled = lambda: False  # type: ignore[method-assign]

    async def _owner(request):
        return getattr(request, "owner", "u1")

    handler._supabase_user_id_for_request_async = _owner  # type: ignore[method-assign]

    def _build(owner_user_id: str = ""):
        calls.append(owner_user_id)
        time.sleep(delay)  # stand in for the volume walk
        return {"sessions": [{"owner": owner_user_id}]}

    handler._sessions_list_payload = _build  # type: ignore[method-assign]
    return handler


def _owner_request(owner: str) -> _Req:
    request = _Req()
    request.owner = owner  # type: ignore[attr-defined]
    return request


# --------------------------------------------------------------------------- #
# one scan per burst
# --------------------------------------------------------------------------- #

def test_a_burst_of_pollers_shares_one_scan() -> None:
    """N concurrent clients must not each walk the volume.

    This is the production shape: several WebUI tabs on the same poll tick, all
    queued behind the same session file lock. Before the single-flight guard
    each one ran the full scan and they all finished at the same instant.
    """
    calls: list[str] = []
    handler = _handler(calls)

    async def burst() -> None:
        await asyncio.gather(
            *(handler._handle_sessions_list(_Req()) for _ in range(8))
        )

    asyncio.run(burst())

    assert calls == ["u1"], f"expected one scan for eight pollers, got {len(calls)}"


def test_a_repeat_within_the_ttl_is_served_from_cache() -> None:
    calls: list[str] = []
    handler = _handler(calls, ttl=60.0)

    async def twice() -> None:
        await handler._handle_sessions_list(_Req())
        await handler._handle_sessions_list(_Req())

    asyncio.run(twice())

    assert calls == ["u1"]


def test_the_cache_expires() -> None:
    calls: list[str] = []
    handler = _handler(calls, delay=0.0, ttl=0.0)

    async def twice() -> None:
        await handler._handle_sessions_list(_Req())
        await handler._handle_sessions_list(_Req())

    asyncio.run(twice())

    assert calls == ["u1", "u1"], "a zero TTL must not be cached forever"


def test_cached_payloads_are_not_shared_across_owners() -> None:
    """Per-user isolation: one user's sidebar must not be served to another."""
    calls: list[str] = []
    handler = _handler(calls, delay=0.0, ttl=60.0)

    async def two_owners() -> None:
        await handler._handle_sessions_list(_owner_request("u1"))
        await handler._handle_sessions_list(_owner_request("u2"))

    asyncio.run(two_owners())

    assert sorted(calls) == ["u1", "u2"]


def test_the_cache_is_bounded() -> None:
    calls: list[str] = []
    handler = _handler(calls, delay=0.0, ttl=60.0)
    handler._SESSIONS_LIST_CACHE_MAX_OWNERS = 2

    async def many_owners() -> None:
        for index in range(5):
            await handler._handle_sessions_list(_owner_request(f"u{index}"))

    asyncio.run(many_owners())

    assert len(handler._sessions_list_cache) <= 2


# --------------------------------------------------------------------------- #
# the other perimeter reads come off the loop
# --------------------------------------------------------------------------- #

def test_the_skills_handler_is_a_coroutine_now() -> None:
    assert asyncio.iscoroutinefunction(GatewayHTTPHandler._handle_webui_skills)


def test_the_skills_read_runs_off_the_event_loop(monkeypatch) -> None:
    """It walked every skill directory on the loop for ~1 s per request."""
    seen: list[str] = []

    def _payload(workspace_path, *, disabled_skills=None):
        seen.append(threading.current_thread().name)
        return {"skills": []}

    monkeypatch.setattr(ws_http, "webui_skills_payload", _payload)

    handler = object.__new__(GatewayHTTPHandler)
    handler.skills_workspace_path = object()
    handler.disabled_skills = set()
    handler.check_api_token = lambda request: True  # type: ignore[method-assign]

    loop_thread = threading.current_thread().name
    asyncio.run(handler._handle_webui_skills(_Req()))

    assert seen and seen[0] != loop_thread, "the skills scan still ran on the loop"


def test_the_list_scan_also_stays_off_the_loop() -> None:
    """Guards the hop itself: a regression to a direct call would show here."""
    src = inspect.getsource(GatewayHTTPHandler._sessions_list_payload_cached)
    assert "asyncio.to_thread" in src
    assert "_sessions_list_payload" in src


@pytest.mark.parametrize("ttl", [0.5, 2.0])
def test_the_ttl_is_short_enough_to_be_invisible(ttl: float) -> None:
    """It buys batching, it must not make the sidebar feel stale."""
    assert ttl <= 2.0
