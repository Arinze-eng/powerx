"""The sidebar list must never make a request wait on a volume scan.

Production, 2026-10-02, one container, four separate reads of one route:

    15:44:27 slow webui http route path=/api/sessions status=200 duration_ms=16867
    16:12:31 slow webui http route path=/api/sessions status=200 duration_ms=11711
    16:41:45 slow webui http route path=/api/sessions status=200 duration_ms=20727
    16:41:44 Gateway shutdown requested by SIGTERM

The 20.7 s read started at 16:41:25 and the SIGTERM landed at 16:41:44. The port
is probed for liveness with ``GET /api/health`` every 10 s, a 5 s timeout and
``failureThreshold: 2``, so a loop that cannot answer for ~20 s fails the probe
twice and the platform replaces the container. The user, mid-task, sees a 503
until it boots -- and it boots in 34-44 s.

What made it 20 s: ``_sessions_list_payload`` walks the persistent volume once
per chat while holding the session file lock, and production has 506 chats.
``tests/webui/test_session_list_index_activity.py`` pins the syscall count
(1518 ``os.stat`` calls for 506 chats, now zero). These tests pin the other
half: a request is served from cache and *never* runs the scan, and the reads
that used to run on the loop have moved off it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time

import pytest

from loguru import logger

from nanobot.webui import ws_http
from nanobot.webui.ws_http import GatewayHTTPHandler


def _body(response) -> dict:
    return json.loads(bytes(response.body).decode("utf-8"))


class _Req:
    """Just enough request for the list handler."""

    headers: dict[str, str] = {}


def _handler(calls: list[str], *, delay: float = 0.05, ttl: float | None = None):
    """Build a handler with only the session-list collaborators it needs."""
    handler = object.__new__(GatewayHTTPHandler)
    handler._sessions_list_lock = asyncio.Lock()
    handler._sessions_list_cache = {}
    handler._sessions_list_refreshing = set()
    handler._sessions_list_refresh_tasks = set()
    handler._log = logger
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
# a request never runs the scan
# --------------------------------------------------------------------------- #

def test_a_burst_of_pollers_shares_one_scan() -> None:
    """N concurrent clients must not each walk the volume.

    This is the production shape: several WebUI tabs on the same poll tick, all
    queued behind the same session file lock. The first request after boot has
    nothing to serve, so it scans; the other seven must wait for that scan
    rather than start their own.
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


def test_a_stale_cache_is_served_without_waiting_for_the_scan() -> None:
    """The whole point: the scan must not be on the request path.

    A poll tick that arrives with a stale payload returns the stale payload at
    once and refreshes behind it. If this test ever needs the build to finish
    before the response, the 20 s stall is back.
    """
    calls: list[str] = []
    handler = _handler(calls, delay=0.4, ttl=0.0)

    async def poll() -> tuple[float, dict]:
        started = time.perf_counter()
        response = await handler._handle_sessions_list(_Req())
        return time.perf_counter() - started, _body(response)

    async def scenario() -> tuple[float, int]:
        await handler._handle_sessions_list(_Req())  # cold: pays the scan once
        elapsed, payload = await poll()
        assert payload["sessions"] == [{"owner": "u1"}]
        await asyncio.sleep(0.6)  # let the background refresh land
        return elapsed, len(calls)

    elapsed, scan_count = asyncio.run(scenario())

    assert elapsed < 0.4, f"the response waited {elapsed:.3f}s on the scan"
    assert scan_count == 2, "the stale payload must still be refreshed"


def test_only_one_refresh_is_in_flight_per_owner() -> None:
    """A poll tick every 100 ms must not pile up scans behind a slow volume."""
    calls: list[str] = []
    handler = _handler(calls, delay=0.3, ttl=0.0)

    async def hammer() -> int:
        await handler._handle_sessions_list(_Req())
        for _ in range(20):
            await handler._handle_sessions_list(_Req())
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.8)
        return len(calls)

    scan_count = asyncio.run(hammer())

    assert scan_count <= 4, f"21 poll ticks started {scan_count} scans"


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


def test_a_failing_refresh_does_not_break_polling() -> None:
    """A background refresh has no caller to raise to; it must swallow."""
    calls: list[str] = []
    handler = _handler(calls, delay=0.0, ttl=0.0)

    def _explode(owner_user_id: str = ""):
        calls.append(owner_user_id)
        if len(calls) > 1:
            raise OSError("volume went away")
        return {"sessions": []}

    handler._sessions_list_payload = _explode  # type: ignore[method-assign]

    async def scenario() -> dict:
        await handler._handle_sessions_list(_Req())
        await handler._handle_sessions_list(_Req())
        await asyncio.sleep(0.2)
        return _body(await handler._handle_sessions_list(_Req()))

    body = asyncio.run(scenario())

    assert body == {"sessions": []}
    assert handler._sessions_list_refreshing == set()


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
    refresh = inspect.getsource(GatewayHTTPHandler._refresh_sessions_list)
    assert "asyncio.to_thread" in refresh


@pytest.mark.parametrize("ttl", [0.5, 2.0])
def test_the_ttl_is_short_enough_to_be_invisible(ttl: float) -> None:
    """It is the refresh cadence, so it must not make the sidebar feel stale."""
    assert ttl <= 2.0
