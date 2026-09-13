"""Unified memoization layer: in-memory LRU + persistent disk backing store.

This is the shared cache every other optimization layer routes through. A
tool call (or any pure computation) fingerprinted by its canonical ``(name,
args)`` pair is executed ONCE; identical calls inside the TTL window are
served straight from memory — or, in a fresh process, straight from the
persistent disk — with zero additional work and zero provider calls.

Storage contract:

* Memory tier: bounded LRU ``OrderedDict`` keyed by fingerprint.
* Disk tier: one small JSON file per fingerprint under the persistent data
  dir (``POWERX_DATA_DIR`` — Northflank mounts the volume at ``/data``), so
  a cache earned in one process is still warm after a restart or a new
  container. Every entry carries a write timestamp and expires by TTL.

Fail-open everywhere: an unserializable value, a disk error, or a disabled
flag simply means "execute the real call". Caching can never produce a wrong
answer, only miss.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.config.paths import get_persistent_data_dir

#: Feature flag: set POWERX_MEMOIZE=0 to disable the layer entirely.
_ENABLED = os.environ.get("POWERX_MEMOIZE", "1").strip().lower() not in {
    "0", "false", "no", "off",
}

#: Default time-to-live for a memoized result (seconds).
_TTL_SECONDS = int(os.environ.get("POWERX_MEMOIZE_TTL_S", "1800"))

#: Bounds so a hot loop can never balloon memory or disk.
_MAX_MEMORY_ENTRIES = 512
_MAX_DISK_ENTRIES = 2000

#: A single async ``execute(name, args)`` callable, mirroring plan_program.
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]


def memoize_enabled() -> bool:
    return _ENABLED


def canonical_fingerprint(tool_name: str, args: dict[str, Any]) -> str:
    """Stable identity for a call: sha256 of canonical ``(name, args)`` JSON.

    Keys are sorted and whitespace normalized so ``{"a": 1, "b": 2}`` and
    ``{"b": 2, "a": 1}`` are the same call.
    """
    payload = json.dumps(
        {"tool": tool_name, "args": args},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MemoCache:
    """Two-tier (memory LRU + persistent disk) result cache with TTL."""

    def __init__(
        self,
        namespace: str = "memoize",
        *,
        ttl_seconds: int | None = None,
        root: Path | None = None,
        max_memory_entries: int = _MAX_MEMORY_ENTRIES,
    ) -> None:
        self._ttl = int(ttl_seconds if ttl_seconds is not None else _TTL_SECONDS)
        self._max_memory = max(1, max_memory_entries)
        self._root = (root or get_persistent_data_dir("memoize")) / namespace
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # read-only fs: memory tier still works
            logger.debug("memo cache disk init failed: {}", exc)
        self._memory: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = asyncio.Lock()

    # -- internals ----------------------------------------------------------

    def _path(self, fingerprint: str) -> Path:
        return self._root / f"{fingerprint}.json"

    def _memory_get(self, fingerprint: str) -> Any | None:
        entry = self._memory.get(fingerprint)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > self._ttl:
            self._memory.pop(fingerprint, None)
            return None
        self._memory.move_to_end(fingerprint)
        return value

    def _memory_put(self, fingerprint: str, value: Any) -> None:
        self._memory[fingerprint] = (time.time(), value)
        self._memory.move_to_end(fingerprint)
        while len(self._memory) > self._max_memory:
            self._memory.popitem(last=False)

    def _disk_get(self, fingerprint: str) -> Any | None:
        path = self._path(fingerprint)
        try:
            if not path.is_file():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            ts = float(payload.get("ts") or 0)
            if time.time() - ts > self._ttl:
                path.unlink(missing_ok=True)
                return None
            return payload.get("value")
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("memo cache disk read failed: {}", exc)
            return None

    def _disk_put(self, fingerprint: str, value: Any) -> bool:
        try:
            self._path(fingerprint).write_text(
                json.dumps({"value": value, "ts": time.time()}, default=str),
                encoding="utf-8",
            )
            self._trim()
            return True
        except (OSError, TypeError, ValueError) as exc:
            logger.debug("memo cache disk write failed: {}", exc)
            return False

    def _trim(self) -> None:
        """Delete oldest disk entries past the cap. Bounded and cheap."""
        try:
            entries = sorted(
                self._root.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for stale in entries[_MAX_DISK_ENTRIES:]:
                stale.unlink(missing_ok=True)
        except OSError:
            pass

    # -- public API ---------------------------------------------------------

    def get(self, fingerprint: str) -> Any | None:
        """Return the cached value for a fingerprint, or None on miss/expiry."""
        value = self._memory_get(fingerprint)
        if value is not None:
            return value
        value = self._disk_get(fingerprint)
        if value is not None:
            self._memory_put(fingerprint, value)
        return value

    def put(self, fingerprint: str, value: Any) -> bool:
        """Store a value in both tiers. Never raises."""
        self._memory_put(fingerprint, value)
        return self._disk_put(fingerprint, value)

    async def get_or_compute(
        self,
        fingerprint: str,
        compute: Callable[[], Awaitable[Any]],
    ) -> tuple[Any, bool]:
        """Singleflight compute: concurrent callers await ONE execution."""
        cached = self.get(fingerprint)
        if cached is not None:
            return cached, True
        async with self._lock:
            cached = self.get(fingerprint)  # re-check inside the lock
            if cached is not None:
                return cached, True
            value = await compute()
            self.put(fingerprint, value)
            return value, False


def memoizing_executor(
    execute: ToolExecutor,
    cache: MemoCache | None = None,
    *,
    cacheable: Callable[[str], bool] | None = None,
) -> ToolExecutor:
    """Wrap a ToolExecutor so identical calls hit the memo cache.

    ``cacheable(tool_name)`` lets callers opt volatile tools out (anything
    whose result changes per call). Unknown tools are cached by default;
    a cache read/write failure degrades to the real call.
    """
    store = cache or MemoCache()
    is_cacheable = cacheable or (lambda _name: True)

    async def wrapped(tool_name: str, args: dict[str, Any]) -> Any:
        if not is_cacheable(tool_name):
            return await execute(tool_name, args)
        fingerprint = canonical_fingerprint(tool_name, args)
        value, hit = await store.get_or_compute(
            fingerprint, lambda: execute(tool_name, args)
        )
        if hit:
            logger.debug("memo hit for {} ({})", tool_name, fingerprint[:12])
        return value

    return wrapped
