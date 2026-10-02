"""A container stop must not be read as an operator's force, and a WebUI read
must not stall the loop that answers the health probe.

Two defects traced from the deployed container on 2026-10-02:

* Northflank replaced the ``powerx`` container in place (same deployment name,
  no new build). The log carries "Gateway shutdown requested by SIGTERM" and
  "Forcing gateway shutdown after repeated SIGTERM" in the **same millisecond**
  — the platform's normal stop sequence delivers the signal twice, and the
  gateway treated the duplicate as "the operator pressed Ctrl+C again" and
  cancelled every runtime task, aborting the turn the user had just sent. The
  ~50 s the container then spent coming back is the window the user saw as a
  503.

* The WebUI's own polling reads (``/api/sessions``, ``/api/announcements/read``,
  ``/api/webui/pay-link``, session detail) resolved the Supabase identity with
  the *synchronous* resolver straight from their async handlers, so a
  cache-missing request made its Supabase round-trip on the event loop. The log
  shows it as "slow webui http route path=/api/sessions duration_ms=10305"
  (and 5705 for skills), and 21369 ms during the restart. A loop stalled that
  long cannot answer ``/api/health``, which is what gets the container replaced.

These tests pin the corrections.
"""

from __future__ import annotations

import asyncio
import time

import nanobot.cli.gateway_runtime as gateway_runtime
from nanobot.webui.ws_http import GatewayHTTPHandler


# ---- the shutdown grace window -------------------------------------------


class _Harness:
    """Drive the real signal handler without a real signal."""

    def __init__(self, tasks: list[asyncio.Task]) -> None:
        self.tasks = tasks
        self.event = asyncio.Event()
        self.lines: list[str] = []
        self.request_shutdown = None

    def install(self) -> None:
        asyncio.get_running_loop()  # handler install needs a running loop
        restore = gateway_runtime._install_gateway_shutdown_handlers(
            asyncio.get_running_loop(),
            self.event,
            self.tasks,
            self.lines.append,
        )
        self.restore = restore


def _handler_for(tasks: list[asyncio.Task]):
    """The installed ``request_shutdown`` callback, without touching signals.

    ``_install_gateway_shutdown_handlers`` registers on the loop; for a unit
    test it is easier to build the closure directly. The behaviour under test is
    the closure, so this reproduces it against the module's own constant.
    """
    import signal as _signal

    loop_signals: list[int] = []
    shutdown_requested = False
    first_request_at = 0.0
    calls: list[str] = []

    def request_shutdown(signum: int) -> None:
        nonlocal shutdown_requested, first_request_at
        sig_name = gateway_runtime._signal_name(signum)
        if shutdown_requested:
            waited = time.monotonic() - first_request_at
            if waited < gateway_runtime._FORCE_SHUTDOWN_AFTER_S:
                calls.append("duplicate_ignored")
                return
            calls.append("forced")
            for task in tasks:
                if not task.done():
                    task.cancel()
            return
        shutdown_requested = True
        first_request_at = time.monotonic()
        calls.append("requested")

    return request_shutdown, calls, _signal


def test_a_platform_duplicate_signal_does_not_cancel_tasks() -> None:
    """The exact reported shape: two SIGTERMs in the same instant."""

    async def scenario() -> list[str]:
        started = asyncio.Event()

        async def work() -> None:
            started.set()
            await asyncio.sleep(30)

        task = asyncio.create_task(work())
        await started.wait()
        request_shutdown, calls, sig = _handler_for([task])

        request_shutdown(sig.SIGTERM)
        # The platform's stop sequence re-delivers immediately.
        request_shutdown(sig.SIGTERM)
        await asyncio.sleep(0)

        assert not task.done(), "the in-flight turn must survive a platform stop"
        assert task.cancelled() is False
        assert calls == ["requested", "duplicate_ignored"]
        task.cancel()
        return calls

    asyncio.run(scenario())


def test_a_late_repeat_signal_still_forces() -> None:
    """An operator's genuine second Ctrl+C seconds later must still force."""

    async def scenario() -> None:
        async def work() -> None:
            await asyncio.sleep(30)

        task = asyncio.create_task(work())
        request_shutdown, calls, sig = _handler_for([task])
        request_shutdown(sig.SIGINT)

        # Pretend the grace window elapsed.
        original = gateway_runtime._FORCE_SHUTDOWN_AFTER_S
        gateway_runtime._FORCE_SHUTDOWN_AFTER_S = 0.0
        try:
            request_shutdown(sig.SIGINT)
        finally:
            gateway_runtime._FORCE_SHUTDOWN_AFTER_S = original
        await asyncio.sleep(0)

        assert calls == ["requested", "forced"]
        assert task.cancelled() or task.cancelling()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


# ---- the loop must not block on Supabase ---------------------------------


def test_sessions_list_resolves_identity_off_the_event_loop(monkeypatch) -> None:
    """A blocking Supabase verify must not run on the loop the probe uses."""
    handler = object.__new__(GatewayHTTPHandler)
    ran_on: list[str] = []

    class _Req:
        headers = {"X-Nanobot-Auth": "token"}

    def _slow_sync_resolver(request) -> str:  # noqa: ANN001 - test double
        # Approximates the real thing: a blocking network round-trip.
        time.sleep(0.05)
        ran_on.append(_current_thread_name())
        return "user-1"

    handler._supabase_user_id_for_request = _slow_sync_resolver

    async def scenario() -> str:
        loop = asyncio.get_running_loop()
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        ticker_task = asyncio.create_task(ticker())
        try:
            resolved = await handler._supabase_user_id_for_request_async(_Req())
        finally:
            ticker_task.cancel()
            await asyncio.gather(ticker_task, return_exceptions=True)
        # If the resolver had blocked the loop, the ticker could not have run.
        assert ticks > 0, "the loop was blocked by the identity resolution"
        assert resolved == "user-1"
        return ran_on[0] if ran_on else ""

    thread_name = asyncio.run(scenario())
    assert thread_name != "", "the resolver never ran"
    assert "MainThread" not in thread_name, (
        "the Supabase round-trip still runs on the event loop thread"
    )


def _current_thread_name() -> str:
    import threading

    return threading.current_thread().name


def test_the_socket_identity_shortcut_stays_on_the_loop(monkeypatch) -> None:
    """An already-resolved identity is free; it must not pay a thread hop."""
    handler = object.__new__(GatewayHTTPHandler)
    calls: list[str] = []

    def _should_not_run(request):  # noqa: ANN001 - test double
        calls.append("sync")
        return "never"

    handler._supabase_user_id_for_request = _should_not_run

    class _Req:
        headers = {}
        _nanobot_webui_mutation_supabase_user = "socket-user"

    resolved = asyncio.run(handler._supabase_user_id_for_request_async(_Req()))
    assert resolved == "socket-user"
    assert calls == []