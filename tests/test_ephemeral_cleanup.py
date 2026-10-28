from __future__ import annotations

import os
import shutil
from pathlib import Path

from nanobot.utils.ephemeral_cleanup import cleanup_ephemeral_work


def test_cleanup_removes_only_powerx_scratch(monkeypatch, tmp_path: Path) -> None:
    scratch = Path("/tmp") / f"powerx-work-test-{os.getpid()}"
    shutil.rmtree(scratch, ignore_errors=True)
    persistent = tmp_path / "persistent"
    scratch.mkdir()
    persistent.mkdir()
    (scratch / "build").mkdir()
    (scratch / "build" / "large.aux").write_text("x")
    (scratch / "node_modules").mkdir()
    (scratch / "node_modules" / "x").write_text("x")
    (persistent / "final.pdf").write_text("keep")
    monkeypatch.setenv("POWERX_EPHEMERAL_WORK_DIR", str(scratch))

    assert cleanup_ephemeral_work() == 2
    assert list(scratch.iterdir()) == [scratch / ".keep"]
    assert (persistent / "final.pdf").read_text() == "keep"
    shutil.rmtree(scratch, ignore_errors=True)


def test_cleanup_rejects_dangerous_roots(monkeypatch, tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("keep")
    monkeypatch.setenv("POWERX_EPHEMERAL_WORK_DIR", str(tmp_path))

    cleanup_ephemeral_work()

    assert sentinel.exists()
