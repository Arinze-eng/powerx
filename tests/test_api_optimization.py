"""Tests for the API-call optimization stack (planner, memoize, router,
reflection, plan_program parallel construct, persistent data dir, onlyfiles
expiry=0 + URL memory, and the optimizer facade)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nanobot.agent.agent_planner import (
    AgentPlanner,
    normalize_task,
    template_fingerprint,
)
from nanobot.agent.api_optimizer import ApiOptimizer
from nanobot.agent.memoize import MemoCache, canonical_fingerprint, memoizing_executor
from nanobot.agent.plan_program import StepOutcome, execute_plan, fan_out, parse_plan
from nanobot.agent.reflection import FailureMemory, reflect_plan, reflect_step
from nanobot.agent.tool_router import ToolRouter, default_router
from nanobot.config.paths import get_persistent_data_dir
from nanobot.utils.onlyfiles import (
    UploadedUrlMemory,
    _ONLYFILES_EXPIRY,
    upload_and_remember,
)


# ---------------------------------------------------------------------------
# Persistent data dir
# ---------------------------------------------------------------------------


def test_persistent_data_dir_env_override(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POWERX_DATA_DIR", str(tmp_path / "vol"))
    resolved = get_persistent_data_dir("memoize")
    assert resolved == tmp_path / "vol" / "memoize"
    assert resolved.is_dir()


def test_persistent_data_dir_falls_back_to_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POWERX_DATA_DIR", raising=False)
    # /data is not writable in the test sandbox, so the runtime fallback wins.
    resolved = get_persistent_data_dir()
    assert resolved.is_dir()
    assert "persistent" in str(resolved) or "powerx" in str(resolved)


# ---------------------------------------------------------------------------
# Memoization
# ---------------------------------------------------------------------------


async def test_memo_cache_put_get_and_disk_persistence(tmp_path: Path):
    cache = MemoCache(namespace="t", root=tmp_path, ttl_seconds=60)
    key = canonical_fingerprint("exec", {"command": "ls"})
    cache.put(key, {"stdout": "file.txt"})
    # A brand-new instance (fresh process simulation) reads from disk.
    cache2 = MemoCache(namespace="t", root=tmp_path, ttl_seconds=60)
    assert cache2.get(key) == {"stdout": "file.txt"}


async def test_memo_cache_ttl_expiry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import nanobot.agent.memoize as memoize_mod

    now = 1000.0
    monkeypatch.setattr(memoize_mod.time, "time", lambda: now)
    cache = MemoCache(namespace="ttl", root=tmp_path, ttl_seconds=10)
    key = "k"
    cache.put(key, "v")
    now += 11
    assert cache.get(key) is None
    now += 0  # unchanged
    cache.put(key, "v2")
    now += 5
    assert cache.get(key) == "v2"


async def test_memoizing_executor_counts_real_executions(tmp_path: Path):
    calls = {"n": 0}

    async def execute(name, args):
        calls["n"] += 1
        return f"ran {name} {args['i']}"

    wrapped = memoizing_executor(execute, MemoCache(namespace="x", root=tmp_path))
    assert await wrapped("exec", {"i": 1}) == "ran exec 1"
    assert await wrapped("exec", {"i": 1}) == "ran exec 1"  # memo hit
    assert await wrapped("exec", {"i": 2}) == "ran exec 2"  # different args
    assert calls["n"] == 2


async def test_memoizing_executor_respects_cacheable(tmp_path: Path):
    calls = {"n": 0}

    async def execute(name, args):
        calls["n"] += 1
        return "now"

    wrapped = memoizing_executor(
        execute,
        MemoCache(namespace="v", root=tmp_path),
        cacheable=lambda name: name != "date",
    )
    await wrapped("date", {})
    await wrapped("date", {})
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Agentic planner + plan memory
# ---------------------------------------------------------------------------


def test_normalize_task_collapses_variable_bits():
    a = template_fingerprint("run the test suite 3 times")
    b = template_fingerprint("Run the test suite 7 times")
    assert a == b  # numbers collapse to one template
    assert template_fingerprint("deploy the website") != a


def test_planner_remember_recall_roundtrip(tmp_path: Path):
    planner = AgentPlanner(root=tmp_path / "plan_memory")
    plan = {"steps": [{"id": "s", "tool": "exec", "args": {"command": "ls"}}]}
    assert planner.recall("list the project files") is None
    assert planner.remember("list the project files", plan, success=True)
    # Case/whitespace-only variation recalls the same learned plan.
    assert planner.recall("LIST  the project files") == plan


def test_planner_learned_bad_plan_is_skipped(tmp_path: Path):
    planner = AgentPlanner(root=tmp_path / "plan_memory")
    plan = {"steps": []}
    planner.remember("convert the file", plan, success=False, error="boom")
    planner.remember("convert the file", plan, success=False, error="boom")
    planner.remember("convert the file", plan, success=False, error="boom")
    assert planner.recall("convert the file") is None  # net failures >= 2


async def test_plan_and_execute_composes_once_then_replays(tmp_path: Path):
    planner = AgentPlanner(root=tmp_path / "plan_memory")
    composed = {"n": 0}

    async def compose(task_text):
        composed["n"] += 1
        return {
            "steps": [{"id": "out", "tool": "exec", "args": {"command": "echo hi"}}],
            "output": "done $out",
        }

    async def execute(name, args):
        return "hi"

    first = await planner.plan_and_execute(
        "show me the build status 1", compose, execute, run_plan=execute_plan
    )
    assert first is not None
    result, plan = first
    assert composed["n"] == 1
    assert result.final == "done hi"

    second = await planner.plan_and_execute(
        "show me the build status 9", compose, execute, run_plan=execute_plan
    )
    assert second is not None
    assert composed["n"] == 1  # replayed from plan memory: ZERO compose calls


# ---------------------------------------------------------------------------
# Tool router
# ---------------------------------------------------------------------------


def test_default_router_matches_routine_ask():
    router = default_router()
    routed = router.route("ls -la")
    assert routed is not None
    name, args = routed
    assert name == "exec"
    assert args["args"]["command"] == "ls -la"


def test_default_router_read_file():
    router = default_router()
    routed = router.route("read file README.md")
    assert routed is not None
    name, args = routed
    assert name == "read_file"
    assert args["args"]["path"] == "README.md"


def test_router_fails_open_on_ambiguous_ask():
    router = ToolRouter()
    assert router.route("please refactor the whole auth module and add tests") is None
    assert router.route("") is None
    assert router.route("x" * 500) is None


def test_router_builder_failure_is_a_miss():
    def _boom(_match):
        raise ValueError("builder bug")

    router = ToolRouter()
    router.register("boom", (r"^crash$",), _boom)
    assert router.route("crash") is None


# ---------------------------------------------------------------------------
# Reflection
# ---------------------------------------------------------------------------


async def test_reflect_step_success_first_try(tmp_path: Path):
    calls = {"n": 0}

    async def execute(name, args):
        calls["n"] += 1
        return "all good"

    verdict = await reflect_step("exec", {"c": 1}, execute, failures=FailureMemory(root=tmp_path))
    assert verdict.ok and verdict.attempts == 1 and not verdict.retried
    assert calls["n"] == 1


async def test_reflect_step_retries_once_and_recovers(tmp_path: Path):
    seq = ["Error: sandbox cold start", "ok output"]

    async def execute(name, args):
        return seq.pop(0)

    memory = FailureMemory(root=tmp_path)
    verdict = await reflect_step("exec", {"c": 2}, execute, failures=memory)
    assert verdict.ok and verdict.retried and verdict.attempts == 2
    assert not memory.known_bad("exec", {"c": 2})  # recovered -> cleared


async def test_reflect_step_records_and_then_skips_retry(tmp_path: Path):
    async def execute(name, args):
        return "Error: exit_code=1"

    memory = FailureMemory(root=tmp_path)
    for _ in range(4):
        verdict = await reflect_step("exec", {"bad": 1}, execute, failures=memory)
    assert memory.known_bad("exec", {"bad": 1})
    # known-bad: single attempt only now
    calls = {"n": 0}

    async def counting(name, args):
        calls["n"] += 1
        return "Error: exit_code=1"

    verdict = await reflect_step("exec", {"bad": 1}, counting, failures=memory)
    assert not verdict.ok and calls["n"] == 1


async def test_reflect_plan_recovers_failed_steps(tmp_path: Path):
    async def execute(name, args):
        return "recovered"

    outcomes = [
        StepOutcome(name="exec", arguments={"a": 1}, result="fine", ok=True),
        StepOutcome(name="exec", arguments={"a": 2}, result="Error: transient", ok=False),
    ]
    summary = await reflect_plan(outcomes, execute, failures=FailureMemory(root=tmp_path))
    assert summary["ok"] and summary["recovered"] == 1 and summary["still_failed"] == 0
    assert outcomes[1].ok is True


# ---------------------------------------------------------------------------
# plan_program parallel construct (LLM compiler)
# ---------------------------------------------------------------------------


async def test_parallel_steps_run_concurrently():
    started = asyncio.Event()
    release = asyncio.Event()
    entered = {"a": False, "b": False}

    async def execute(name, args):
        entered[args["tag"]] = True
        if all(entered.values()):
            started.set()
        await asyncio.wait_for(release.wait(), timeout=2)
        return f"done-{args['tag']}"

    plan = {
        "steps": [
            {
                "parallel": [
                    {"id": "x", "tool": "exec", "args": {"tag": "a"}},
                    {"id": "y", "tool": "exec", "args": {"tag": "b"}},
                ]
            },
            {"id": "summary", "tool": "exec", "args": {"tag": "a"}},
        ],
        "output": "$x and $y",
    }

    async def releaser():
        await asyncio.wait_for(started.wait(), timeout=2)
        release.set()

    runner = asyncio.ensure_future(releaser())
    result = await execute_plan(parse_plan(plan), execute)
    await runner
    assert not result.failed
    # Both branches overlapped: the summary step references results published
    # by BOTH parallel branches.
    assert result.final == "done-a and done-b"
    assert result.executed_steps == 3


async def test_fan_out_preserves_order_and_captures_errors():
    async def execute(name, args):
        if args.get("boom"):
            raise RuntimeError("kaput")
        await asyncio.sleep(0.001 * (3 - args["i"]))
        return f"r{args['i']}"

    outcomes = await fan_out(
        [("exec", {"i": 1}), ("exec", {"boom": True, "i": 2}), ("exec", {"i": 3})], execute
    )
    assert [o.result for o in outcomes] == ["r1", "Error: kaput", "r3"]
    assert [o.ok for o in outcomes] == [True, False, True]


# ---------------------------------------------------------------------------
# Optimizer facade
# ---------------------------------------------------------------------------


async def test_optimizer_full_pipeline_replays_without_compose(tmp_path: Path):
    optimizer = ApiOptimizer(
        planner=AgentPlanner(root=tmp_path / "plan_memory"),
        memo=MemoCache(namespace="opt", root=tmp_path),
        failures=FailureMemory(root=tmp_path / "reflection"),
    )
    composed = {"n": 0}
    executed = {"n": 0}

    async def compose(task_text):
        composed["n"] += 1
        return {
            "steps": [{"id": "s", "tool": "exec", "args": {"command": "make build"}}],
            "output": "built",
        }

    async def execute(name, args):
        executed["n"] += 1
        return "ok"

    first = await optimizer.optimize("build the project 1 for me", compose, execute)
    assert first is not None and first["reflection"]["ok"]
    assert composed["n"] == 1 and executed["n"] == 1

    # Structurally identical repeat: plan replays AND the tool call is
    # memoized, so compose AND execute both stay at zero extra calls.
    second = await optimizer.optimize("build the project 2 for me", compose, execute)
    assert second is not None
    assert composed["n"] == 1
    assert executed["n"] == 1  # memo hit instead of a second real execution


async def test_optimizer_run_parallel(tmp_path: Path):
    optimizer = ApiOptimizer(
        planner=AgentPlanner(root=tmp_path / "plan_memory"),
        memo=MemoCache(namespace="opt2", root=tmp_path),
        failures=FailureMemory(root=tmp_path / "reflection"),
    )

    async def execute(name, args):
        await asyncio.sleep(0.01)
        return f"v{args['i']}"

    outcomes = await optimizer.run_parallel(
        [("exec", {"i": 1}), ("exec", {"i": 2})], execute
    )
    assert [o.result for o in outcomes] == ["v1", "v2"]


# ---------------------------------------------------------------------------
# Onlyfiles: expiry=0 + persistent URL memory
# ---------------------------------------------------------------------------


def test_onlyfiles_expiry_is_zero():
    assert _ONLYFILES_EXPIRY == 0


async def test_upload_bytes_sends_expiry_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import nanobot.utils.onlyfiles as onlyfiles_mod

    captured: dict = {}

    class FakeResponse:
        status = 200

        async def text(self):
            return json.dumps(
                {
                    "status": True,
                    "data": {
                        "file": {
                            "url": {"full": "https://onlyfiles.com/abc/report.pdf", "short": "https://onlyfiles.com/abc"}
                        }
                    },
                }
            )

    class FakePostCtx:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, data=None):
            captured["url"] = url
            captured["form"] = data
            return FakePostCtx()

    monkeypatch.setattr(onlyfiles_mod.aiohttp, "ClientSession", FakeSession)
    result = await onlyfiles_mod.upload_bytes(b"payload", filename="report.pdf")
    assert result["url"] == "https://onlyfiles.com/abc/report.pdf"
    assert captured["url"] == "https://api.onlyfiles.com/v1/upload"
    fields = str(getattr(captured["form"], "_fields", captured["form"]))
    assert "expire" in fields and "'0'" in fields  # expire=0: never expires per API docs


async def test_file_info_parses_metadata_and_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import nanobot.utils.onlyfiles as onlyfiles_mod

    class FakeResponse:
        def __init__(self, status, body):
            self.status = status
            self._body = body

        async def text(self):
            return self._body

    class FakeGetCtx:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def __init__(self, resp, **kw):
            self._resp = resp

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url):
            captured["url"] = url
            return FakeGetCtx(self._resp)

    captured: dict = {}
    ok = FakeResponse(
        200,
        json.dumps({"status": True, "data": {"file": {"id": "abc", "name": "report.pdf"}}}),
    )
    monkeypatch.setattr(onlyfiles_mod.aiohttp, "ClientSession", lambda *a, **kw: FakeSession(ok))
    info = await onlyfiles_mod.file_info("abc")
    assert captured["url"] == "https://api.onlyfiles.com/v1/file/abc/info"
    assert info == {"file": {"id": "abc", "name": "report.pdf"}}

    gone = FakeResponse(404, json.dumps({"status": False, "error": {"message": "not found", "type": "api", "code": 8}}))
    monkeypatch.setattr(onlyfiles_mod.aiohttp, "ClientSession", lambda *a, **kw: FakeSession(gone))
    assert await onlyfiles_mod.file_info("abc") is None

    with pytest.raises(onlyfiles_mod.OnlyFilesError):
        await onlyfiles_mod.file_info("")


async def test_upload_and_remember_reuses_url_without_reupload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import nanobot.utils.onlyfiles as onlyfiles_mod

    uploads = {"n": 0}

    async def fake_upload_path(path, **kw):
        uploads["n"] += 1
        return {"url": "https://onlyfiles.com/x/data.csv", "download_url": "https://onlyfiles.com/x/data.csv"}

    monkeypatch.setattr(onlyfiles_mod, "upload_path", fake_upload_path)
    src = tmp_path / "data.csv"
    src.write_bytes(b"a,b\n1,2\n")

    memory = UploadedUrlMemory(root=tmp_path / "onlyfiles")
    first = await upload_and_remember(src, url_memory=memory)
    second = await upload_and_remember(src, url_memory=memory)
    assert first == second
    assert uploads["n"] == 1  # second call served from persistent URL memory

    # A brand-new memory instance over the same root still remembers it.
    fresh = UploadedUrlMemory(root=tmp_path / "onlyfiles")
    assert fresh.lookup(b"a,b\n1,2\n") == first
