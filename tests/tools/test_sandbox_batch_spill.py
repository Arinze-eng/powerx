"""Tests for disk-first sandbox_batch results (batch_spill).

These cover the economics claim: a batch's returned context must stay roughly
constant per operation regardless of how much each command prints, and a failed
command must be visible as FAILED rather than silently reported as ok.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nanobot.agent.tools.batch_spill import (
    SPILL_DIRNAME,
    BatchSpillStore,
    build_digest,
    extract_exit_code,
    looks_like_failure,
)
from nanobot.agent.tools.sandbox_batch import SandboxBatchTool


class FakeSandbox:
    def __init__(self, handler=None):
        self.calls: list[dict] = []
        self._handler = handler or (lambda kwargs: f"out:{kwargs.get('action')}")

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        result = self._handler(kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return result


def _tool_with(handler=None, workspace=None) -> tuple[SandboxBatchTool, FakeSandbox]:
    tool = SandboxBatchTool(workspace=workspace)
    fake = FakeSandbox(handler)
    tool._sandbox = fake
    return tool, fake


# --------------------------------------------------------------------------
# unit: helpers
# --------------------------------------------------------------------------
def test_extract_exit_code_reads_last_marker() -> None:
    assert extract_exit_code("hello\n[exit=0]") == 0
    assert extract_exit_code("a\n[exit=1]\nb\n[exit=7]") == 7
    assert extract_exit_code("no marker here") is None


def test_nonzero_exit_is_a_failure_even_without_an_error_word() -> None:
    # This is the exact shape that used to be reported as "ok".
    assert looks_like_failure("run", "Traceback omitted\n[exit=2]", 2)
    assert not looks_like_failure("run", "all good\n[exit=0]", 0)


def test_read_ops_are_not_flagged_by_content_words() -> None:
    # A source file legitimately containing "error" must not read as a failure.
    body = 'raise ValueError("error")\n[12 lines total]'
    assert not looks_like_failure("read", body, None)


def test_digest_is_bounded_and_points_at_the_log() -> None:
    huge = ("some log line\n" * 50_000) + "boom: assertion error\n[exit=1]"
    digest = build_digest("run", huge, ".nanobot/batch/r/op-0003.txt")
    assert len(digest) <= 400
    assert "[FAILED]" in digest
    assert "exit=1" in digest
    assert "assertion error" in digest.lower()
    assert ".nanobot/batch/r/op-0003.txt" in digest


def test_digest_for_success_keeps_one_line() -> None:
    digest = build_digest("run", "tests passed\n[exit=0]", ".nanobot/batch/r/op-0000.txt")
    assert digest.startswith("[ok] run")
    assert "tests passed" in digest


# --------------------------------------------------------------------------
# store behaviour
# --------------------------------------------------------------------------
def test_store_writes_files_and_manifest(tmp_path: Path) -> None:
    store = BatchSpillStore(tmp_path)
    run = store.begin_run(2)
    assert run is not None
    digest = store.spill(run, 0, "run", "line one\nline two\n[exit=0]")
    assert digest is not None
    target = tmp_path / run.relative_path(0)
    assert target.exists()
    assert "line two" in target.read_text()
    store.finish_run(run, failures=0)
    manifest = json.loads((run.dir / "manifest.json").read_text())
    assert manifest["operations"] == 2
    assert manifest["failures"] == 0
    assert manifest["entries"][0]["chars"] > 0


def test_store_unavailable_without_workspace() -> None:
    store = BatchSpillStore(None)
    assert store.available is False
    assert store.begin_run(1) is None


def test_store_survives_unwritable_workspace(tmp_path: Path) -> None:
    # A regular file where the spill root directory should be created makes every
    # mkdir under it fail; begin_run must degrade to inline output, not raise.
    blocked = tmp_path / "ro"
    blocked.mkdir()
    (blocked / ".nanobot").write_text("not a directory")
    store = BatchSpillStore(blocked)
    assert store.available  # availability is about having a workspace path
    assert store.begin_run(1) is None


# --------------------------------------------------------------------------
# integration: the whole point of the feature
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_large_verbose_batch_stays_small_in_context(tmp_path: Path) -> None:
    """200 ops printing 5KB each must return far less than 1MB of text."""
    noisy = "x" * 5_000

    def handler(_kwargs):
        return noisy + "\n[exit=0]"

    tool, fake = _tool_with(handler, workspace=tmp_path)
    report = await tool.execute(
        operations=[{"action": "run", "command": "build"}] * 200,
        stop_on_error=False,
    )
    assert len(fake.calls) == 200
    # Legacy inline rendering would have blown past the 24k budget and degraded
    # later ops to useless status lines; digests keep every op individually legible.
    assert len(report) < 60_000, f"report too large: {len(report)}"
    assert report.count("[ok] run") == 200
    # Full output really is on disk, so nothing was lost — only deferred.
    logs = list((tmp_path / SPILL_DIRNAME).glob("*/op-*.txt"))
    assert len(logs) == 200
    assert all(len(p.read_text()) > 5_000 for p in logs[:5])


@pytest.mark.asyncio
async def test_every_op_keeps_detail_no_budget_degradation(tmp_path: Path) -> None:
    """The old code dropped detail past ~8 verbose ops; digests must not."""
    tool, _ = _tool_with(lambda k: "y" * 9_000 + "\n[exit=0]", workspace=tmp_path)
    report = await tool.execute(
        operations=[{"action": "run", "command": "c"}] * 40,
        stop_on_error=False,
    )
    assert "detail omitted" not in report
    assert "details omitted to stay within budget" not in report
    assert report.count("[ok] run") == 40


@pytest.mark.asyncio
async def test_failing_command_is_reported_as_failure(tmp_path: Path) -> None:
    """A run exiting non-zero without raising must count as a failure."""
    tool, _ = _tool_with(lambda k: "compile exploded\n[exit=1]", workspace=tmp_path)
    report = await tool.execute(operations=[{"action": "run", "command": "make"}])
    assert "1 failure(s)" in report
    assert "[FAILED]" in report
    assert "compile exploded" in report


@pytest.mark.asyncio
async def test_allow_failure_opts_out_of_exit_code_check(tmp_path: Path) -> None:
    tool, _ = _tool_with(lambda k: "probe said no\n[exit=127]", workspace=tmp_path)
    report = await tool.execute(
        operations=[{"action": "run", "command": "which nope", "allow_failure": True}]
    )
    assert "0 failure(s)" in report


@pytest.mark.asyncio
async def test_stop_on_error_halts_at_first_nonzero_exit(tmp_path: Path) -> None:
    seen: list[int] = []

    def handler(_kwargs):
        seen.append(len(seen))
        return "kaboom\n[exit=1]" if len(seen) == 1 else "fine\n[exit=0]"

    tool, _ = _tool_with(handler, workspace=tmp_path)
    report = await tool.execute(
        operations=[{"action": "run", "command": "step"}] * 5, stop_on_error=True
    )
    assert len(seen) == 1, "batch should halt immediately after the failing op"
    assert "halted early on error" in report


@pytest.mark.asyncio
async def test_digest_names_the_failed_op_for_follow_up(tmp_path: Path) -> None:
    def handler(kwargs):
        idx = kwargs.get("_idx")
        return "boom: TypeError here\n[exit=1]" if idx == 2 else "clean\n[exit=0]"

    calls: list[dict] = []

    async def counting(**kwargs):
        kwargs["_idx"] = len(calls)
        calls.append(kwargs)
        return handler(kwargs)

    tool = SandboxBatchTool(workspace=tmp_path)
    fake = FakeSandbox()
    fake.execute = counting  # type: ignore[method-assign]
    tool._sandbox = fake

    report = await tool.execute(
        operations=[{"action": "run", "command": "t"}] * 4, stop_on_error=False
    )
    failed_lines = [ln for ln in report.splitlines() if "[FAILED]" in ln]
    assert len(failed_lines) == 1
    assert "op 2" in failed_lines[0]
    # The hint tells the model where to look next turn, without wasting a call.
    assert "full logs under" in report


@pytest.mark.asyncio
async def test_without_workspace_falls_back_to_legacy_inline(tmp_path: Path) -> None:
    """No workspace => unchanged pre-feature behaviour, byte for byte."""
    tool, _ = _tool_with(lambda k: "short output\n[exit=0]", workspace=None)
    report = await tool.execute(
        operations=[
            {"action": "write", "path": "/workspace/a.py", "content": "x=1"},
            {"action": "run", "command": "python a.py"},
        ]
    )
    assert "[sandbox_batch: 2 operation(s), 0 failure(s)]" in report
    assert "[op 0 write → ok]" in report
    assert "[op 1 run → ok]" in report
    assert "full:" not in report  # no digests in legacy mode


@pytest.mark.asyncio
async def test_composite_ops_also_spill(tmp_path: Path, monkeypatch) -> None:
    """Composite handlers return long reports; those should spill too."""
    import nanobot.agent.tools.sandbox_batch as sb

    tool, _ = _tool_with(lambda k: "unused\n[exit=0]", workspace=tmp_path)
    long_report = "deploy log\n" + ("step done\n" * 3_000)

    async def fake_deploy(sandbox, op):
        return long_report

    monkeypatched = dict(sb._COMPOSITE_HANDLERS, deploy=fake_deploy)
    monkeypatch.setattr(sb, "_COMPOSITE_HANDLERS", monkeypatched)
    report = await tool.execute(
        operations=[{"action": "deploy", "project_dir": "/workspace/app"}]
    )
    assert "[ok] deploy" in report
    assert len(report) < 2_000, "composite report should be a digest, not the log"
    spilled = list((tmp_path / SPILL_DIRNAME).glob("*/op-0000.txt"))
    assert spilled and len(spilled[0].read_text()) > 30_000
