"""Disk-first spill storage for ``sandbox_batch`` operation results.

Why this exists
---------------
The agent loop charges one LLM round-trip per iteration, so ``sandbox_batch``
lets the model express many operations in a single call.  But that only pays off
while the *combined result* stays small enough to be cheap to read.  With the
old inline design, every operation's stdout was rendered into the tool result,
which meant:

* the 24k aggregate budget was exhausted after a few verbose commands, and
* once exhausted, later operations degraded to status-only lines whose first
  line is usually meaningless (a blank line, an echo header), so the model lost
  the ability to tell pass from fail — and had to spend *another* round-trip
  asking for details.

Spilling changes the economics: full output goes to disk under the workspace and
the model receives a fixed-size, high-signal digest instead.  Cost per operation
becomes O(1) rather than O(output size), which is what makes very large batches
(500+ ops) viable.  The model reads the real log only when a digest says FAILED.

Layout::

    <workspace>/.nanobot/batch/<run_id>/manifest.json   # run metadata
    <workspace>/.nanobot/batch/<run_id>/op-0007.txt     # full stdout/stderr

Retention is best-effort: runs older than ``DEFAULT_RETENTION_SECONDS`` are
pruned whenever a new run starts, so long-lived workspaces do not accumulate
logs without bound.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Directory (relative to the sandbox workspace) holding all spilled logs.
SPILL_DIRNAME = ".nanobot/batch"

#: How long spilled runs survive before a later run prunes them (~12 hours).
DEFAULT_RETENTION_SECONDS = 12 * 3600

#: Cap on the number of trailing lines quoted inside a digest.
_DIGEST_TAIL_LINES = 4

#: Hard cap on digest characters, independent of operation count.
_MAX_DIGEST_CHARS = 400

#: Composite ops that already render their own verdict line; re-labelling them
#: with [ok]/[FAILED] would duplicate the signal in the digest.
_SELF_DESCRIBING_ACTIONS = frozenset({"retry_until", "foreach", "await"})

_ERROR_MARKERS = (
    "error",
    "failed",
    "failure",
    "exception",
    "traceback",
    "cannot",
    "not found",
    "assert",
    "panic",
    "denied",
    "fatal",
)

_EXIT_RE = re.compile(r"\[exit=(\d+)\]")


@dataclass(slots=True)
class SpillRun:
    """One ``sandbox_batch`` invocation writing its logs to disk."""

    root: Path
    run_id: str
    dir: Path
    created_at: float = field(default_factory=time.time)
    entries: list[dict[str, Any]] = field(default_factory=list)

    def path_for(self, index: int) -> Path:
        return self.dir / f"op-{index:04d}.txt"

    def relative_path(self, index: int) -> str:
        return f"{SPILL_DIRNAME}/{self.run_id}/op-{index:04d}.txt"


class BatchSpillStore:
    """Writes operation output to disk and returns compact digests."""

    def __init__(self, workspace: str | Path | None) -> None:
        self.workspace = Path(workspace) if workspace else None

    @property
    def available(self) -> bool:
        return self.workspace is not None

    # -- run lifecycle -----------------------------------------------------
    def begin_run(self, op_count: int) -> SpillRun | None:
        """Create a spill directory for a batch. Returns None when unusable."""
        if self.workspace is None:
            return None
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 100000:05d}"
        base = self.workspace / SPILL_DIRNAME
        try:
            base.mkdir(parents=True, exist_ok=True)
            self._prune(base)
            run_dir = base / run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            run = SpillRun(root=self.workspace, run_id=run_id, dir=run_dir)
            (run_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "started_at": run.created_at,
                        "operations": op_count,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            return run
        except OSError:
            # Disk trouble must never break a batch; fall back to inline output.
            return None

    def finish_run(self, run: SpillRun, failures: int) -> None:
        """Record the outcome so a later turn can find interesting logs."""
        try:
            manifest = run.dir / "manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["finished_at"] = time.time()
            data["failures"] = failures
            data["entries"] = run.entries
            manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def _prune(self, base: Path) -> None:
        cutoff = time.time() - DEFAULT_RETENTION_SECONDS
        try:
            candidates = sorted(p for p in base.iterdir() if p.is_dir())
        except OSError:
            return
        for old in candidates[:-10]:  # always keep the most recent runs
            try:
                if old.stat().st_mtime < cutoff:
                    for f in old.iterdir():
                        f.unlink(missing_ok=True)
                    old.rmdir()
            except OSError:
                continue

    # -- per-op ------------------------------------------------------------
    def spill(self, run: SpillRun, index: int, action: str, body: str) -> str | None:
        """Write ``body`` to disk; return a compact digest, or None on failure."""
        try:
            target = run.path_for(index)
            target.write_text(body, encoding="utf-8")
        except OSError:
            return None
        digest = build_digest(action, body, run.relative_path(index))
        run.entries.append(
            {"index": index, "action": action, "path": run.relative_path(index), "chars": len(body)}
        )
        return digest


def extract_exit_code(text: str) -> int | None:
    """Pull the backend's trailing ``[exit=N]`` marker out of command output."""
    matches = _EXIT_RE.findall(text)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except (TypeError, ValueError):
        return None


def looks_like_failure(action: str, body: str, exit_code: int | None) -> bool:
    """Decide whether an operation needs the model's attention."""
    if exit_code not in (None, 0):
        return True
    lowered = body.lower()
    if action == "read":
        return False
    # Only scan the tail: headers/echoes mentioning "error" are not failures.
    tail = lowered[-2000:]
    return any(marker in tail for marker in _ERROR_MARKERS)


def build_digest(action: str, body: str, rel_path: str) -> str:
    """Render a fixed-size, decision-ready summary of one operation.

    Keeps the signal a model actually needs — verdict plus the specific lines
    explaining it — while costing a constant number of tokens regardless of how
    much the command emitted.
    """
    exit_code = extract_exit_code(body)
    failed = looks_like_failure(action, body, exit_code)
    # Composite ops render their own "[SATISFIED] kind" line, so prefixing them
    # with another [ok]/[FAILED] would duplicate the verdict in the digest.
    self_describing = action in _SELF_DESCRIBING_ACTIONS
    verdict = "" if self_describing else ("FAILED" if failed else "ok")

    stripped = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
    # Drop the echoed exit marker from the quoted tail; it is reported separately.
    if stripped and _EXIT_RE.fullmatch(stripped[-1].strip()):
        stripped.pop()

    informative = [ln for ln in stripped if _informative(ln)]
    if failed:
        detail = _failure_lines(informative or stripped)
    else:
        # Prefer the last meaningful line: test runners summarise at the end.
        detail = [_strip_progress(ln) for ln in (informative[-1:] or stripped[:1])]
        detail = [ln for ln in detail if ln]

    parts = [f"{action}" if not verdict else f"[{verdict}] {action}"]
    if exit_code is not None:
        parts.append(f"exit={exit_code}")
    if detail:
        parts.append(" | ".join(detail)[:_MAX_DIGEST_CHARS // 2])
    parts.append(f"full:{rel_path} ({len(body)} chars)")
    digest = " ".join(parts)
    return digest[:_MAX_DIGEST_CHARS]


#: Lines matching this carry no information (pytest progress dots, npm spinners,
#: pip bars) and would otherwise crowd out the real summary in a digest.
_NOISE_RE = re.compile(r"^[\s.*=_#\-]*$|\.{3,}\s*$|%\s*$")


def _informative(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) < 3:
        return False
    return not _NOISE_RE.match(stripped)


def _strip_progress(line: str) -> str:
    """Remove inline progress spam ("... ... 300 passed") from a summary line.

    Test runners emit thousands of status characters on the same physical line as
    their verdict, so line-level filtering cannot drop them; the verdict survives
    by keeping only the trailing non-dot segment.
    """
    parts = [seg.strip(" .") for seg in re.split(r"\s{2,}|\.{3,}", line)]
    keep = [seg for seg in parts if len(seg.strip()) > 2]
    return keep[-1][:200] if keep else ""


def _failure_lines(lines: list[str]) -> list[str]:
    """Select the lines most likely to explain a failure.

    A plain tail is often just a test-progress dot line, so error-bearing lines
    are pulled out first and the tail is used as context.
    """
    if not lines:
        return []
    scored = [ln for ln in lines if any(m in ln.lower() for m in _ERROR_MARKERS)]
    picked = scored[-_DIGEST_TAIL_LINES:] if scored else lines[-_DIGEST_TAIL_LINES:]
    cleaned = [_strip_progress(ln) or ln[:200] for ln in picked]
    return [ln for ln in cleaned if ln]
