"""Durable replay cache: an identical task answered once costs ZERO provider
calls the next time.

Manus-style cost discipline means ``sandbox_batch`` collapses an N-op task
into ONE provider round-trip. But a *repeated* task - the same coding job
re-requested, a cron retry, a user re-sending the same build instruction -
still pays that one call again. This cache fingerprints the task text and
stores the completed answer; an exact repeat is served straight from disk
with no provider call and no token spend.

Safety guards:
* Only tasks above a minimum text length are cached (chatter is never cached).
* Entries expire after a TTL (default 6h) so stale results never linger.
* The cache is per-workspace and only consulted when the feature is enabled
  (POWERX_REPLAY_CACHE=1 default) and the run opts in via the spec flag.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.paths import get_runtime_subdir

_REPLAY_ENABLED = os.environ.get("POWERX_REPLAY_CACHE", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
_REPLAY_TTL_SECONDS = int(os.environ.get("POWERX_REPLAY_CACHE_TTL_S", str(6 * 3600)))
#: Below this many chars a "task" is chatter (hello, thanks, one-liners) and
#: must never be cached or replayed - replaying "hi" would be absurd.
_MIN_TASK_CHARS = 30
#: Fingerprint is computed over the last N chars of the joined user text so a
#: growing conversation prefix never changes the identity of the task.
_MAX_TASK_CHARS = 4_000
#: Keep at most this many entries on disk (each file is tiny; bounds disk use).
_MAX_ENTRIES = 500


def replay_cache_enabled() -> bool:
    return _REPLAY_ENABLED


def _normalize(text: str) -> str:
    """Collapse whitespace so cosmetic edits do not change task identity."""
    return re.sub(r"\s+", " ", text or "").strip()


def task_fingerprint_text(messages: list[dict[str, Any]], max_chars: int = _MAX_TASK_CHARS) -> str:
    """The exact text used for fingerprinting: joined user messages, tail-only.

    Only ``role == "user"`` content participates, so system prompts, tool
    results and assistant chatter never affect the identity of the task.
    """
    parts: list[str] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(_normalize(content))
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(_normalize(block["text"]))
    joined = " ".join(parts)
    if len(joined) <= _MAX_TASK_CHARS:
        return joined
    return joined[-_MAX_TASK_CHARS:]


class TaskReplayCache:
    """Fingerprint -> completed-answer store under the runtime data root.

    Stored as ``{final_content, ts}`` JSON files so a later run (even in a new
    process) can replay a task it already completed with zero provider calls.
    """

    def __init__(self, workspace: str | Path | None = None) -> None:
        self._enabled = replay_cache_enabled()
        root = get_runtime_subdir("replay_cache")
        if workspace:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(workspace))[:80]
            root = root / safe
        root.mkdir(parents=True, exist_ok=True)
        self._root = root

    # -- internals ----------------------------------------------------------

    def _fingerprint(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _path(self, fingerprint: str) -> Path:
        return self._root / f"{fingerprint}.json"

    # -- public API ---------------------------------------------------------

    def get(self, task_text: str) -> str | None:
        """Return the cached final answer for an identical task, or None."""
        if not self._enabled:
            return None
        fingerprint = self._fingerprint(task_text)
        path = self._path(fingerprint)
        try:
            if not path.is_file():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            ts = float(payload.get("ts") or 0)
            if time.time() - ts > _REPLAY_TTL_SECONDS:
                path.unlink(missing_ok=True)
                return None
            final_content = payload.get("final_content")
            if not final_content or not isinstance(final_content, str):
                return None
            logger.info("replay cache hit for task {} ({:.0f}s old)", fingerprint[:12], time.time() - ts)
            return final_content
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("replay cache read failed: {}", exc)
            return None

    def put(self, task_text: str, final_content: str | None) -> bool:
        """Store the completed answer for a task (only meaningful tasks)."""
        if not self._enabled:
            return False
        if not final_content or not final_content.strip():
            return False
        if len(task_text) < _MIN_TASK_CHARS:
            return False
        fingerprint = self._fingerprint(task_text)
        try:
            path = self._path(fingerprint)
            path.write_text(
                json.dumps({"final_content": final_content, "ts": time.time()}),
                encoding="utf-8",
            )
            self._trim()
            logger.info("replay cache stored for task {}", fingerprint[:12])
            return True
        except OSError as exc:
            logger.debug("replay cache write failed: {}", exc)
            return False

    def _trim(self) -> None:
        """Delete oldest entries past the cap. Bounded and cheap."""
        try:
            entries = sorted(self._root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for stale in entries[_MAX_ENTRIES:]:
                stale.unlink(missing_ok=True)
        except OSError:
            pass


def make_replay_cache(workspace: str | Path | None = None) -> TaskReplayCache | None:
    """Construct the cache when the feature is enabled, else None."""
    if not replay_cache_enabled():
        return None
    return TaskReplayCache(workspace)
