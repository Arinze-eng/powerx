#!/usr/bin/env python3
"""Back up & restore WebUI chat history through Supabase ``system_settings``.

Why
---
Render's disk is ephemeral: every redeploy/redeploy creates a fresh container
whose ``~/.nanobot`` data dir is wiped. Chat transcripts (the JSONL display
history users see in the WebUI) live there, so an update on Render destroys each
user's chat history. This script gives the chat history a durable home in
Supabase (which already hosts the app's users), so history survives reboots.

How
---
It reuses the existing ``system_settings`` key->value table (no DDL / migrations
needed) as an object store for chat files:

    key   = "chatbackup:" + relative file path (under the webui dir)
    value = the file's text content (JSONL / JSON transcripts & thread snapshots)

* ``--backup``   walks the local WebUI data dir and upserts every chat file that
                 changed since the last sync.
* ``--loop N``   repeats ``--backup`` every N seconds (run as an in-container
                 sidecar so the latest chat state is pushed continuously; even
                 if a redeploy kills the container, history is preserved up to
                 the last sync window).
* ``--restore``  pulls all ``chatbackup:`` rows back down and writes them into
                 the local WebUI dir (merging; a definitely-later local file is
                 kept in favour of a stale cloud copy). Run once at boot before
                 the app starts.

Only chat-critical text files under the WebUI dir are synced (``.jsonl`` /
``.json``). Big media/attachment binaries are skipped to keep rows small.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

KEY_PREFIX = "chatbackup:"
# Only these extensions carry chat history (transcripts, thread snapshots).
SYNC_EXTENSIONS: frozenset[str] = frozenset({".jsonl", ".json"})
# Sub-directories under the webui dir that never hold chat history.
SKIP_DIRS: frozenset[str] = frozenset({"media", "logs", "cache", "tmp", "attachments", "uploads"})
# Rows above this many bytes are skipped (shouldn't happen for text chat files).
MAX_SYNC_BYTES = 20 * 1024 * 1024


class ChatSync:
    def __init__(self, *, webui_dir: str, url: str, service_key: str) -> None:
        self.webui_dir = Path(webui_dir).expanduser()
        self.url = url.rstrip("/")
        self.service_key = service_key
        self._client = httpx.Client(timeout=60.0, follow_redirects=False)

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }

    # -- cloud helpers -----------------------------------------------------

    def _fetch_all(self) -> dict[str, str]:
        """Return ``{rel_path: content}`` for every chatbackup row."""
        page = 0
        out: dict[str, str] = {}
        while True:
            resp = self._client.get(
                f"{self.url}/rest/v1/system_settings",
                params={
                    "select": "key,value,updated_at",
                    "key": f"like.{KEY_PREFIX}*",
                    "limit": "1000",
                    "offset": str(page * 1000),
                },
                headers=self._headers(),
            )
            if not resp.is_success:
                break
            rows = resp.json()
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("key") or "")
                value = row.get("value")
                if not key.startswith(KEY_PREFIX):
                    continue
                if isinstance(value, (str,)):
                    out[key[len(KEY_PREFIX):]] = value
                elif value is not None:
                    out[key[len(KEY_PREFIX):]] = str(value)
            if len(rows) < 1000:
                break
            page += 1
        return out

    def _upsert(self, rel_path: str, content: str) -> None:
        self._client.post(
            f"{self.url}/rest/v1/system_settings",
            params={"on_conflict": "key"},
            headers=self._headers(),
            json={"key": KEY_PREFIX + rel_path, "value": content},
        )

    def _delete(self, rel_path: str) -> None:
        self._client.delete(
            f"{self.url}/rest/v1/system_settings",
            params={"key": f"eq.{KEY_PREFIX}{rel_path}"},
            headers=self._headers(),
        )

    # -- local file helpers ------------------------------------------------

    def _iter_chat_files(self):
        """Yield ``(rel_path, file_path)`` for chat history files to sync."""
        if not self.webui_dir.is_dir():
            return
        for base, dirs, files in os.walk(self.webui_dir):
            base_path = Path(base)
            rel_dir = base_path.relative_to(self.webui_dir)
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                ext = Path(name).suffix.lower()
                if ext not in SYNC_EXTENSIONS:
                    continue
                file_path = base_path / name
                rel_path = str(rel_dir / name) if str(rel_dir) != "." else name
                try:
                    if file_path.stat().st_size > MAX_SYNC_BYTES:
                        continue
                except OSError:
                    continue
                yield rel_path, file_path

    # -- backup ------------------------------------------------------------

    def backup(self, *, quiet: bool = False) -> int:
        """Upload changed chat files. Returns number of rows written/removed."""
        if not self.webui_dir.is_dir():
            return 0
        local: dict[str, str] = {}
        for rel_path, file_path in self._iter_chat_files():
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            local[rel_path] = content
        cloud = self._fetch_all()
        written = 0
        removed = 0
        for rel_path, content in local.items():
            if cloud.get(rel_path) == content:
                continue  # unchanged
            self._upsert(rel_path, content)
            written += 1
        # Drop cloud rows whose local file no longer exists
        # (session deleted by the user).
        for rel_path in list(cloud.keys()):
            if rel_path not in local:
                self._delete(rel_path)
                removed += 1
        if not quiet:
            print(
                f"[chat-sync] backup: {written} updated, {removed} removed, "
                f"{len(local) - written} unchanged"
            )
        return written + removed

    # -- restore -----------------------------------------------------------

    def restore(self) -> int:
        """Pull chat files from Supabase back to disk. Returns count restored."""
        cloud = self._fetch_all()
        self.webui_dir.mkdir(parents=True, exist_ok=True)
        restored = 0
        for rel_path, content in cloud.items():
            # Guard against path traversal from stored keys.
            target = self.webui_dir.joinpath(rel_path).resolve()
            if not str(target).startswith(str(self.webui_dir.resolve())):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # Merge: a definitely-later local file wins over the cloud copy.
            if target.exists():
                try:
                    if target.stat().st_mtime > time.time() - 3600:
                        continue  # freshly written locally; keep it
                except OSError:
                    pass
            try:
                target.write_text(content, encoding="utf-8")
                restored += 1
            except OSError:
                continue
        if restored:
            print(f"[chat-sync] restore: wrote {restored} chat file(s) from Supabase")
        return restored


def _enabled() -> tuple[str, str] | None:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        return None
    return url, key


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist WebUI chat history to Supabase")
    parser.add_argument("--dir", default=os.path.join(os.path.expanduser("~"), ".nanobot"),
                        help="Runtime data dir containing the webui/ chat folder")
    parser.add_argument("--url", default=os.getenv("SUPABASE_URL", ""))
    parser.add_argument("--service-key", default=os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""))
    parser.add_argument("--backup", action="store_true", help="Upload changed chat files")
    parser.add_argument("--restore", action="store_true", help="Pull chat files from Supabase")
    parser.add_argument("--loop", type=int, default=0,
                        help="Repeat --backup every N seconds (0 = run once)")
    args = parser.parse_args()

    url = args.url or os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = args.service_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        print("[chat-sync] SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set; skipping", file=sys.stderr)
        return 0

    sync = ChatSync(webui_dir=os.path.join(args.dir, "webui"), url=url, service_key=key)

    if args.restore:
        sync.restore()
        return 0

    if args.loop:
        print(f"[chat-sync] backup loop every {args.loop}s (pid {os.getpid()})")
        first = True
        while True:
            try:
                sync.backup(quiet=not first)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                print(f"[chat-sync] backup error: {type(exc).__name__}", file=sys.stderr)
            first = False
            time.sleep(args.loop)
    else:
        sync.backup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())