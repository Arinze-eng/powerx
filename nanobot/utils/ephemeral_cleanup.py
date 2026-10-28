"""Bounded cleanup for PowerX-owned ephemeral task scratch space."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_DEFAULT_WORK_DIR = "/tmp/powerx-work"
_KEEP_FILE = ".keep"


def ephemeral_work_dir() -> Path:
    """Return the dedicated scratch directory without creating arbitrary paths."""
    raw = (os.environ.get("POWERX_EPHEMERAL_WORK_DIR") or _DEFAULT_WORK_DIR).strip()
    path = Path(raw).expanduser()
    try:
        path.relative_to(Path("/tmp"))
    except ValueError:
        return Path(_DEFAULT_WORK_DIR)
    if (
        not path.is_absolute()
        or path in {Path("/"), Path("/tmp"), Path.home()}
        or path.parent != Path("/tmp")
        or not path.name.startswith("powerx-work")
    ):
        return Path(_DEFAULT_WORK_DIR)
    return path


def cleanup_ephemeral_work() -> int:
    """Delete children of the dedicated scratch directory and return the count.

    This intentionally refuses to remove the directory itself or anything outside
    it. It never calls sync/drop_caches: page-cache reclaim is a kernel/platform
    concern, not a safe per-task application operation.
    """
    root = ephemeral_work_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / _KEEP_FILE).touch(exist_ok=True)
    except OSError:
        return 0

    removed = 0
    for child in tuple(root.iterdir()):
        if child.name == _KEEP_FILE:
            continue
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        except OSError:
            # Cleanup is best effort and must never turn a completed task into a
            # failed task because another process still owns a temporary file.
            continue
    return removed


__all__ = ["cleanup_ephemeral_work", "ephemeral_work_dir"]
