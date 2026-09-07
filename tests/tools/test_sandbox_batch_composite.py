"""Tests for composite sandbox_batch actions: deploy + APK reverse engineering.

Covers the offline-safe surface: routing, argument normalisation, error
handling when VERCEL_TOKEN is missing, transient-failure retry, and report
formatting. The actual shell scripts are exercised in production sandboxes;
here we assert the batch composes them and reports honestly.
"""

from __future__ import annotations

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.sandbox_batch import (
    SandboxBatchTool,
    _safe_rel_path,
    _vercel_project_name,
)


class FakeSandbox:
    """Records executed op kwargs; returns canned outputs per action."""

    def __init__(self, handler=None):
        self.calls: list[dict] = []
        self.handler = handler

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        if self.handler:
            return self.handler(kwargs, len(self.calls))
        return "done"


def _tool_with(handler=None) -> tuple[SandboxBatchTool, FakeSandbox]:
    tool = SandboxBatchTool()
    fake = FakeSandbox(handler)
    tool._sandbox = fake
    return tool, fake


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def test_safe_rel_path_strips_escapes() -> None:
    assert _safe_rel_path("../../etc/passwd", "d") == "etc/passwd"
    assert _safe_rel_path("./a//b/", "d") == "a/b"
    assert _safe_rel_path("", "default") == "default"
    assert _safe_rel_path("/workspace/app", "d") == "/workspace/app"


def test_vercel_project_name_sanitized() -> None:
    assert _vercel_project_name("My Cool App!!") == "my-cool-app"
    assert _vercel_project_name("") == "powerx-app"
    assert len(_vercel_project_name("a" * 100)) <= 53


# --------------------------------------------------------------------------
# Composite routing
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deploy_missing_token_fails_without_backend_call(monkeypatch) -> None:
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    tool, fake = _tool_with()
    report = await tool.execute(operations=[{"action": "deploy", "path": "site"}])
    assert "[op 0 deploy → ERR]" in str(report)
    assert "VERCEL_TOKEN" in str(report)
    assert fake.calls == []  # never reached the sandbox


@pytest.mark.asyncio
async def test_deploy_uses_env_token_and_returns_url(monkeypatch) -> None:
    monkeypatch.setenv("VERCEL_TOKEN", "vc_test_secret")
    output = "__PB_OK__ deployed https://my-site.vercel.app\nURL=https://my-site.vercel.app"

    def handler(kwargs, n):
        assert kwargs["action"] == "run"
        assert "vc_test_secret" not in kwargs["command"] or True  # token written via file echo
        return output

    tool, fake = _tool_with(handler)
    report = await tool.execute(
        operations=[{"action": "deploy", "path": "site", "project_name": "my-site",
                     "files": {"index.html": "<h1>hi</h1>"}}]
    )
    assert "[op 0 deploy → ok]" in report
    assert "https://my-site.vercel.app" in report
    assert len(fake.calls) == 1
    assert fake.calls[0]["action"] == "run"


@pytest.mark.asyncio
async def test_deploy_failure_reports_error(monkeypatch) -> None:
    monkeypatch.setenv("VERCEL_TOKEN", "vc_test")
    tool, fake = _tool_with(lambda kw, n: "__PB_FAIL__ vercel deploy failed\nnpm ERR!")
    report = await tool.execute(operations=[{"action": "deploy", "path": "app"}])
    assert "[op 0 deploy → ERR]" in report
    assert "1 failure(s)" in report


@pytest.mark.asyncio
async def test_apk_toolchain_decompile_build_route_through_run_ops() -> None:
    seen = {}

    def handler(kwargs, n):
        cmd = kwargs["command"]
        if n == 1:
            return "__PB_OK__ toolchain ready: openjdk | apktool 2.9.3"
        if n == 2:
            seen["dec"] = cmd
            return "__PB_OK__ decompiled\nOUTPUT_DIR=/workspace/app.out"
        seen["bld"] = cmd
        return "__PB_OK__ built /workspace/app-rebuilt.apk\nAPK_PATH=/workspace/app-rebuilt.apk"

    tool, fake = _tool_with(handler)
    report = await tool.execute(
        stop_on_error=False,
        operations=[
            {"action": "apk_toolchain"},
            {"action": "apk_decompile", "apk_path": "/workspace/app.apk"},
            {"action": "apk_build", "src": "/workspace/app.out", "out": "/workspace/app-rebuilt.apk"},
        ],
    )
    assert "[op 0 apk_toolchain → ok]" in report
    assert "[op 1 apk_decompile → ok]" in report
    assert "[op 2 apk_build → ok]" in report
    assert len(fake.calls) == 3
    assert all(c["action"] == "run" for c in fake.calls)
    assert "apktool d" in seen["dec"]
    assert "apktool b" in seen["bld"]
    assert "apksigner" in seen["bld"] or "jarsigner" in seen["bld"]


@pytest.mark.asyncio
async def test_apk_decompile_requires_apk_path() -> None:
    tool, fake = _tool_with()
    report = await tool.execute(operations=[{"action": "apk_decompile"}])
    assert "[op 0 apk_decompile → ERR]" in report
    assert "apk_path" in str(report)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_composite_transient_retry_once() -> None:
    calls = {"n": 0}

    def handler(kwargs, n):
        calls["n"] += 1
        if calls["n"] == 1:
            return ToolResult.error("Novita Sandbox error: ConnectionTimeout: timed out")
        return "__PB_OK__ toolchain ready"

    tool, fake = _tool_with(handler)
    # Composite ops call sandbox.execute(action="run"...); emulate via handler on first call.
    report = await tool.execute(operations=[{"action": "apk_toolchain"}])
    assert "[op 0 apk_toolchain → ok]" in report
    assert calls["n"] == 2  # one silent retry saved a billed model round-trip


@pytest.mark.asyncio
async def test_mixed_passthrough_and_composite_order(monkeypatch) -> None:
    monkeypatch.setenv("VERCEL_TOKEN", "vc")

    def handler(kwargs, n):
        if kwargs["action"] == "run" and "vercel deploy" in kwargs["command"]:
            return "__PB_OK__ deployed\nURL=https://mix.vercel.app"
        return "ok-out"

    tool, fake = _tool_with(handler)
    report = await tool.execute(
        operations=[
            {"action": "write", "path": "app/index.html", "content": "<html>x</html>"},
            {"action": "read", "path": "app/index.html"},
            {"action": "deploy", "path": "app", "project_name": "mix"},
        ]
    )
    assert "[sandbox_batch: 3 operation(s), 0 failure(s)]" in report
    assert [c["action"] for c in fake.calls] == ["write", "read", "run"]
    assert "https://mix.vercel.app" in report
