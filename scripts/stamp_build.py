#!/usr/bin/env python3
"""Stamp the current git commit SHA into BUILD_SHA for deployment identity.

Run this as part of your release/commit flow (or via scripts/stamp_build.sh) so
the image you deploy always reports an accurate ``GET /version`` -> git_sha even
when Northflank builds with plain ``COPY .`` and has no ``.git`` directory.

The running app prefers the GIT_SHA/COMMIT_SHA env var (set by CI/Northflank
build args) and falls back to this committed file, so either mechanism works.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def current_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


def main() -> int:
    sha = current_sha()
    if not sha:
        print("stamp_build: no git SHA available, leaving BUILD_SHA untouched", file=sys.stderr)
        return 1
    target = Path(__file__).resolve().parents[1] / "BUILD_SHA"
    target.write_text(f"{sha}\n", encoding="utf-8")
    print(f"stamp_build: wrote {sha} -> {target.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
