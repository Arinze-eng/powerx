"""Regression tests: a stopped/paused cloud sandbox must be restarted, never recreated.

The "files disappear during compilation" bug: Daytona treated ``stopped``/
``archived`` as terminal states, so once a sandbox auto-stopped (inactivity
timeout) the next tool call created a brand-new sandbox from the base snapshot
and silently wiped the user's entire workspace. Novita dropped paused-sandbox
handles and recreated from template; Upstash never restarted stopped boxes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from nanobot.agent.tools import daytona_backend
from nanobot.agent.tools import novita_sandbox as ns
from nanobot.agent.tools.daytona_backend import DaytonaExecutionBackend
from nanobot.agent.tools.upstash_backend import UpstashExecutionBackend
from nanobot.config.schema import DaytonaExecutionConfig


# --------------------------------------------------------------------------- #
# Daytona                                                                     #
# --------------------------------------------------------------------------- #
class _Response:
    def __init__(self, status: int = 200, payload: Any = None) -> None:
        self.status = status
        self._payload = payload

    async def text(self) -> str:
        return json.dumps(self._payload or {})

    async def read(self) -> bytes:
        return json.dumps(self._payload or {}).encode("utf-8")


class _RequestCtx:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> _Response:
        return self._response

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakeSession:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> _RequestCtx:
        self.calls.append((method, url, kwargs))
        return _RequestCtx(self._handler(method, url, kwargs))


async def test_daytona_restarts_stopped_sandbox_instead_of_recreating() -> None:
    """An auto-stopped sandbox keeps its disk: ensure_sandbox must START it, never create a fresh one."""
    starts: list[str] = []
    created: list[str] = []
    polls: list[str] = []

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "GET" and url.endswith("/sandbox/px-stopped"):
            polls.append("by-name")
            return _Response(200, {"id": "sbx-1", "state": "stopped", "toolboxProxyUrl": ""})
        if method == "GET" and url.endswith("/sandbox/sbx-1"):
            polls.append("by-id")
            # After the start call the same sandbox reports ready with a toolbox URL.
            if polls.count("by-id") >= 2:
                return _Response(200, {"id": "sbx-1", "state": "started", "toolboxProxyUrl": "https://tb/sbx-1"})
            return _Response(200, {"id": "sbx-1", "state": "stopped", "toolboxProxyUrl": ""})
        if method == "POST" and url.endswith("/sandbox/sbx-1/start"):
            starts.append(url)
            return _Response(200, {})
        if method == "POST" and url.rstrip("/").endswith("/sandbox"):
            created.append(url)
            return _Response(200, {"id": "sbx-new", "state": "started", "toolboxProxyUrl": "https://tb/sbx-new"})
        return _Response(404, {"error": f"unexpected {method} {url}"})

    backend = DaytonaExecutionBackend(DaytonaExecutionConfig(api_key="dtn_test"), sandbox_name="px-stopped")
    sandbox_id = await backend.ensure_sandbox(_FakeSession(handler))

    assert sandbox_id == "sbx-1"
    assert len(starts) == 1, "the stopped sandbox must be started exactly once"
    assert not created, "a stopped sandbox must NEVER be recreated (that wipes the workspace)"


async def test_daytona_stopped_sandbox_is_not_terminal() -> None:
    assert "stopped" not in daytona_backend._TERMINAL_STATES
    assert "archived" not in daytona_backend._TERMINAL_STATES
    assert daytona_backend._RESUMABLE_STATES == {"stopped", "archived"}


async def test_daytona_ensure_sandbox_resumes_archived_sandbox() -> None:
    """Archived sandboxes also keep their disk: they must be started, not replaced."""
    starts: list[str] = []
    created: list[str] = []

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        if method == "GET" and url.endswith("/sandbox/px-archived"):
            return _Response(200, {"id": "sbx-2", "state": "archived", "toolboxProxyUrl": ""})
        if method == "GET" and url.endswith("/sandbox/sbx-2"):
            if starts:
                return _Response(200, {"id": "sbx-2", "state": "started", "toolboxProxyUrl": "https://tb/sbx-2"})
            return _Response(200, {"id": "sbx-2", "state": "archived", "toolboxProxyUrl": ""})
        if method == "POST" and url.endswith("/sandbox/sbx-2/start"):
            starts.append(url)
            return _Response(200, {})
        if method == "POST" and url.rstrip("/").endswith("/sandbox"):
            created.append(url)
            return _Response(200, {"id": "sbx-new", "state": "started", "toolboxProxyUrl": "https://tb/sbx-new"})
        return _Response(404, {"error": f"unexpected {method} {url}"})

    backend = DaytonaExecutionBackend(DaytonaExecutionConfig(api_key="dtn_test"), sandbox_name="px-archived")
    sandbox_id = await backend.ensure_sandbox(_FakeSession(handler))

    assert sandbox_id == "sbx-2"
    assert len(starts) == 1
    assert not created


# --------------------------------------------------------------------------- #
# Upstash                                                                     #
# --------------------------------------------------------------------------- #
def _upstash_config() -> SimpleNamespace:
    return SimpleNamespace(
        api_key="box_test",
        base_url="https://us-east-1.box.upstash.com",
        runtime="python",
        size="small",
        ttl_s=3600,
    )


async def test_upstash_restarts_stopped_box_instead_of_recreating(monkeypatch) -> None:
    """A stopped box is restarted (filesystem intact) rather than replaced by a new one."""
    backend = UpstashExecutionBackend(_upstash_config(), box_name="px-test")
    requests: list[tuple[str, str]] = []
    restarted = {"done": False}

    async def fake_request(session: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        requests.append((method, path))
        if method == "POST":
            restarted["done"] = True
            return {}
        if not restarted["done"]:
            return {"status": "stopped"}
        return {"status": "running"}

    monkeypatch.setattr(backend, "_request", fake_request)
    await backend.wait_ready(None, "box-x", timeout=10)

    assert ("POST", "/v2/box/box-x/restart") in requests
    assert ("POST", "/v2/box/box-x/start") not in requests, "restart succeeds, no start fallback needed"
    # The same box id is polled to readiness — no create/recreate was involved.
    assert all("/v2/box/box-x" in path or path == "/v2/box" for _m, path in requests)


# --------------------------------------------------------------------------- #
# Novita                                                                      #
# --------------------------------------------------------------------------- #
async def test_novita_resumes_paused_sandbox_instead_of_recreating(tmp_path, monkeypatch) -> None:
    """A paused (timed-out) Novita sandbox has its files intact: resume it, never create fresh."""
    tool = ns.NovitaSandboxTool()
    store = ns._SandboxStore(index_path=tmp_path / "idx.json")
    monkeypatch.setattr(ns, "_STORE", store)

    class FakeBox:
        id = "nsbx-1"

        def __init__(self) -> None:
            self._running = False

        def is_running(self) -> bool:
            return self._running

        def resume(self) -> None:
            self._running = True

    box = FakeBox()
    store.set("sess-key", box, template="tpl-ok")

    created: list[dict[str, Any]] = []

    class FakeSandboxApi:
        def connect(self, sandbox_id: str) -> FakeBox:
            raise AssertionError("a live stored handle should be reused, not reconnected")

        def create(self, *args: Any, **kwargs: Any) -> FakeBox:
            created.append(kwargs)
            return FakeBox()

    fake_client = SimpleNamespace(sandbox=FakeSandboxApi())
    monkeypatch.setattr(tool, "_resolve_template", lambda client: "tpl-ok")
    monkeypatch.setattr(tool, "_client", lambda: fake_client)

    result = tool._get_or_create("sess-key")

    assert result is box
    assert not created, "a paused sandbox with matching sizing must be resumed, never recreated"
