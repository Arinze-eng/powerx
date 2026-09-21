"""Resilience tests for the Upstash Box backend.

Covers the three failure modes behind the "Upstash isn't responding / hangs the
whole system / box limit" report:

1. a wedged endpoint that stretches every command (hard per-call deadline),
2. a control plane that is simply not answering (circuit breaker),
3. an exhausted box quota (dead-box reaping + retry), plus the box-count
   explosion caused by a per-session archive box.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiohttp
import pytest

from nanobot.agent.tools import upstash_backend as ub
from nanobot.agent.tools.upstash_backend import UpstashError, UpstashExecutionBackend


def _config(**overrides):
    values = {
        "api_key": "box_test",
        "base_url": "https://us-east-1.box.upstash.com",
        "runtime": "python",
        "size": "small",
        "ttl_s": 3600,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _clean_breaker():
    ub._BREAKER.clear()
    yield
    ub._BREAKER.clear()


class _Resp:
    def __init__(self, status: int, payload: str) -> None:
        self.status = status
        self._payload = payload

    async def text(self) -> str:
        return self._payload

    async def read(self) -> bytes:
        return self._payload.encode()


class _CM:
    def __init__(self, resp: _Resp | Exception) -> None:
        self._resp = resp

    async def __aenter__(self):
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp

    async def __aexit__(self, *exc) -> bool:
        return False


class _Session:
    """Fake aiohttp session: a script of responses/exceptions, plus a call log."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append((method, url))
        item = self.script.pop(0) if self.script else _Resp(200, "{}")
        return _CM(item if isinstance(item, Exception) else item)


# --------------------------------------------------------------------------- #
# 1. Circuit breaker                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_transport_failures_open_the_breaker_and_later_calls_fail_fast():
    session = _Session([aiohttp.ClientError("boom")] * 5)
    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")

    for _ in range(ub._BREAKER_THRESHOLD):
        with pytest.raises(UpstashError):
            await backend._request(session, "GET", "/v2/box")
    assert len(session.calls) == ub._BREAKER_THRESHOLD

    # The breaker is open: the next call refuses WITHOUT touching the network,
    # which is what stops a wedged endpoint from stalling every later command.
    with pytest.raises(UpstashError) as exc:
        await backend._request(session, "GET", "/v2/box")
    assert "not responding" in str(exc.value)
    assert len(session.calls) == ub._BREAKER_THRESHOLD


@pytest.mark.asyncio
async def test_breaker_ignores_http_status_errors_and_resets_on_success():
    # A 500 means the control plane IS answering — it must never trip the breaker.
    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")
    for _ in range(ub._BREAKER_THRESHOLD + 1):
        with pytest.raises(UpstashError):
            await backend._request(_Session([_Resp(500, '{"error": "Failed to read file"}')]), "GET", "/v2/box")
    assert ub.breaker_retry_in(backend.base_url) == 0.0

    # One success clears accumulated transport failures.
    session = _Session([aiohttp.ClientError("x"), aiohttp.ClientError("x"), _Resp(200, "{}")])
    for _ in range(2):
        with pytest.raises(UpstashError):
            await backend._request(session, "GET", "/v2/box")
    assert ub._breaker_entry(backend.base_url)["fails"] == 2
    # A single successful response clears the accumulated transport failures.
    assert await backend._request(session, "GET", "/v2/box") == {}
    assert ub.breaker_retry_in(backend.base_url) == 0.0
    assert ub._breaker_entry(backend.base_url)["fails"] == 0


@pytest.mark.asyncio
async def test_timeout_is_typed_and_counted_by_the_breaker():
    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")
    with pytest.raises(UpstashError) as exc:
        await backend._request(_Session([asyncio.TimeoutError()]), "GET", "/v2/box")
    assert "timed out" in str(exc.value)
    assert ub._breaker_entry(backend.base_url)["fails"] == 1


# --------------------------------------------------------------------------- #
# 2. One shared archive box (box-quota preservation)                         #
# --------------------------------------------------------------------------- #


def test_archive_box_is_shared_across_sessions():
    first = UpstashExecutionBackend(_config(), box_name="px-telegram-aaa")._archive_backend()
    second = UpstashExecutionBackend(_config(), box_name="px-webui-bbb")._archive_backend()
    # Same archive box for both sessions: a per-session archive box doubled the
    # account's box usage and exhausted the quota.
    assert first.box_name == second.box_name == "px-archive-shared"
    assert first.box_name.startswith("px-archive-")
    assert first.persist_workspace is False
    assert first.ttl_s == 86_400
    base = UpstashExecutionBackend(_config(), box_name="px-telegram-aaa")
    assert base._archive_backend().box_name != base.box_name
    # Snapshots stay namespaced per session even though the box is shared.
    assert base.workspace == first.workspace


# --------------------------------------------------------------------------- #
# 3. Box quota exhaustion                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_box_limit_reaps_dead_boxes_then_retries_create():
    limit = _Resp(400, '{"error": "box limit exceeded for this account"}')
    listing = _Resp(
        200,
        '{"boxes": ['
        '{"id": "dead-1", "name": "px-old-1", "status": "deleted"},'
        '{"id": "dead-2", "name": "px-old-2", "status": "error"},'
        '{"id": "live-1", "name": "px-live", "status": "running"}]}',
    )
    created = _Resp(200, '{"id": "box-new"}')
    session = _Session([limit, listing, _Resp(200, "{}"), _Resp(200, "{}"), created])
    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")

    box_id = await backend._create_box(session, {"runtime": "python"})
    assert box_id == "box-new"
    deletes = [url for method, url in session.calls if method == "DELETE"]
    # Only the terminal-status powerx boxes are reaped; the running one is kept.
    assert deletes == ["https://us-east-1.box.upstash.com/v2/box/dead-1",
                       "https://us-east-1.box.upstash.com/v2/box/dead-2"]


@pytest.mark.asyncio
async def test_box_limit_without_prunable_boxes_raises_actionable_error():
    limit = _Resp(400, '{"error": "box limit exceeded for this account"}')
    listing = _Resp(200, '{"boxes": [{"id": "live-1", "name": "px-live", "status": "running"}]}')
    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")
    with pytest.raises(UpstashError) as exc:
        await backend._create_box(_Session([limit, listing]), {"runtime": "python"})
    message = str(exc.value)
    assert "no free box slots" in message
    assert "Upstash dashboard" in message


# --------------------------------------------------------------------------- #
# 4. Hard per-call deadline                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_abandons_a_wedged_call_instead_of_hanging(monkeypatch):
    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")

    async def wedged_ensure_box(session):  # noqa: ANN001
        await asyncio.sleep(30)

    class _FakeClientSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc) -> bool:
            return False

    monkeypatch.setattr(backend, "ensure_box", wedged_ensure_box)
    monkeypatch.setattr(ub.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ub, "_ENSURE_FULL_BUDGET", 1)
    monkeypatch.setattr(ub, "_TRANSPORT_SLACK", 1)

    with pytest.raises(UpstashError) as exc:
        await asyncio.wait_for(backend.run("echo hi", timeout=1), timeout=10)
    assert "hard budget" in str(exc.value)


@pytest.mark.asyncio
async def test_wait_ready_has_an_absolute_ceiling(monkeypatch):
    """A box stuck in a resumable status can never poll past its ceiling."""
    backend = UpstashExecutionBackend(_config(), box_name="px-test-1")
    polls = {"n": 0}

    async def stopped_forever(session, method, path, **kwargs):  # noqa: ANN001
        polls["n"] += 1
        return {"status": "stopped"}

    async def noop_restart(session, box_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(backend, "_request", stopped_forever)
    monkeypatch.setattr(backend, "_restart_box", noop_restart)
    monkeypatch.setattr(ub, "_WAIT_READY_BUDGET", 2)
    monkeypatch.setattr(ub, "_WAIT_READY_HARD_MARGIN", 1)
    with pytest.raises(UpstashError) as exc:
        await backend.wait_ready(object(), "box-1", timeout=2)
    assert "was not ready in time" in str(exc.value)
    assert polls["n"] < 40
