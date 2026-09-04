#!/usr/bin/env python3
"""Back up & restore WebUI chat history through Supabase ``system_settings``.

Why
---
Render's disk is ephemeral: every redeploy creates a fresh container whose
``~/.nanobot`` data dir is wiped. The WebUI chat history users see lives there
(two pieces):

  * ``webui/*.jsonl``  — the display transcripts (the actual messages).
  * ``sessions/websocket_*.jsonl`` — the session metadata that records each
    chat's owner (Supabase user id) together with its title / preview / scope.

If only the transcripts are restored, the sidebar still loses every chat because
the session metadata (including the ``_webui_owner_user_id`` tag) is gone. This
script gives BOTH pieces a durable, **per-user** home in Supabase so each user's
chat history and ownership survive redeploys unchanged.

How
---
It reuses the existing ``system_settings`` key->value table (no DDL / migrations
needed) as an object store:

    key   = "chatbackup:" + "<sessions|webui>/" + relative path
    value = the file's text content (JSONL / JSON transcripts & snapshots)

* ``--backup``   walks the local data dir and upserts every chat/session file
                 that changed since the last sync.
* ``--loop N``   repeats ``--backup`` every N seconds (sidecar) so the latest
                 chat state is pushed continuously.
* ``--restore``  pulls all ``chatbackup:`` rows back down and writes them into
                 the local data dir (merging; a definitely-later local file is
                 kept in favour of a stale cloud copy). Run once at boot before
                 the app starts.

Only chat-critical text files are synced (``.jsonl`` / ``.json``). Big
media/attachment binaries are skipped to keep rows small.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

KEY_PREFIX = "chatbackup:"
# Only these extensions carry chat history (transcripts, thread snapshots,
# session metadata).
SYNC_EXTENSIONS: frozenset[str] = frozenset({".jsonl", ".json"})
# Sub-directories that never hold chat history.
SKIP_DIRS: frozenset[str] = frozenset(
    {"media", "logs", "cache", "tmp", "attachments", "uploads"},
)
# Rows above this many bytes are skipped (shouldn't happen for text chat files).
MAX_SYNC_BYTES = 20 * 1024 * 1024
# Relative sub-dirs under the runtime data dir that hold chat history.
SYNC_DIRS: tuple[str, ...] = ("webui", "sessions")


class ChatSync:
    def __init__(self, *, data_dir: str, url: str, service_key: str) -> None:
        self.data_dir = Path(data_dir).expanduser()
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
                if isinstance(value, str):
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
        """Yield ``(rel_path, file_path)`` for chat history files to sync.

        ``rel_path`` always begins with a managed sub-dir (``webui/`` or
        ``sessions/``) so it maps to a stable ``chatbackup:`` key.
        """
        for sub in SYNC_DIRS:
            base_dir = self.data_dir / sub
            if not base_dir.is_dir():
                continue
            for base, dirs, files in os.walk(base_dir):
                base_path = Path(base)
                rel_dir = base_path.relative_to(self.data_dir)
                dirs[:] = [d for d in dirs if d and d not in SKIP_DIRS]
                for name in files:
                    ext = Path(name).suffix.lower()
                    if ext not in SYNC_EXTENSIONS:
                        continue
                    file_path = base_path / name
                    rel_path = str(rel_dir / name)
                    try:
                        if file_path.stat().st_size > MAX_SYNC_BYTES:
                            continue
                    except OSError:
                        continue
                    yield rel_path, file_path

    @staticmethod
    def _legacy_key(rel_path: str) -> str:
        """Map a cloud key to its canonical namespaced key.

        Older versions stored webui transcript keys without a sub-dir prefix
        (e.g. ``websocket_xxx.jsonl``). Map those bare names back into the
        ``webui/`` namespace so restore writes to the right location and backup
        can dedupe/migrate them.
        """
        if rel_path.startswith(("webui/", "sessions/")):
            return rel_path
        return f"webui/{rel_path}"

    # -- backup ------------------------------------------------------------

    def backup(self, *, quiet: bool = False) -> int:
        """Upload changed chat files. **Non-destructive**: only upserts changed
        local files and never deletes cloud rows. Returns number of rows written.

        Cloud cleanup (removing rows for sessions the user deleted) is left to
        the explicit ``--prune`` flag so a fresh container or a partial restore
        can never destroy a user's persisted history.
        """
        if not self.data_dir.is_dir():
            return 0
        local: dict[str, str] = {}
        for rel_path, file_path in self._iter_chat_files():
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            local[rel_path] = content

        cloud = self._fetch_all()
        # Build a namespaced view of the cloud rows (migrating legacy bare
        # webui keys like ``websocket_x.jsonl`` → ``webui/websocket_x.jsonl``).
        named_cloud: dict[str, str] = {}
        legacy_map: dict[str, str] = {}
        for rel_path, content in cloud.items():
            mapped = self._legacy_key(rel_path)
            if ("/" not in rel_path) or not rel_path.startswith(("webui/", "sessions/")):
                legacy_map[mapped] = rel_path
                named_cloud.setdefault(mapped, content)
            else:
                named_cloud[rel_path] = content

        written = 0
        for rel_path, content in local.items():
            if named_cloud.get(rel_path) == content:
                continue  # already backed up under a namespaced key
            # Legacy bare row already holds the same bytes → nothing to do,
            # and we'll promote the legacy row below.
            legacy = legacy_map.get(rel_path)
            if legacy and cloud.get(legacy) == content:
                continue
            self._upsert(rel_path, content)
            written += 1

        # Promote legacy bare rows to their namespaced twin once the current
        # content exists under the namespaced key, then remove the legacy row.
        # This is a safe one-time migration (the twin row now owns the data).
        for rel_path, legacy in legacy_map.items():
            if rel_path in local and named_cloud.get(rel_path) == local.get(rel_path):
                try:
                    self._delete(legacy)
                    written += 0  # cleanup, not a new write
                except Exception:
                    pass

        if not quiet:
            print(
                f"[chat-sync] backup: {written} updated, {len(local) - written} unchanged "
                f"(non-destructive; no rows removed)"
            )
        return written

    def prune(self) -> int:
        """Remove cloud rows for sessions/files that no longer exist locally.

        Destructive — only run when the local dir is authoritative (e.g. after
        a full restore, or a deliberate session-delete). Returns rows removed.
        """
        if not self.data_dir.is_dir():
            return 0
        local_names: set[str] = {
            rel
            for rel, _ in self._iter_chat_files()
        }
        cloud = self._fetch_all()
        named: dict[str, str] = {}
        for rel_path, content in cloud.items():
            mapped = self._legacy_key(rel_path)
            named.setdefault(mapped, content)
        removed = 0
        for rel_path in named:
            if rel_path.startswith(("webui/", "sessions/")) and rel_path not in local_names:
                # Only remove rows we confidently manage in a known namespace.
                self._delete(rel_path)
                removed += 1
        if removed:
            print(f"[chat-sync] prune: removed {removed} stale row(s)")
        return removed

    # -- restore -----------------------------------------------------------

    def restore(self) -> int:
        """Pull chat files from Supabase back to disk. Returns count restored."""
        cloud = self._fetch_all()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        restored = 0
        for rel_path, content in cloud.items():
            # Legacy bare keys are webui files; map them into the webui dir.
            target_rel = self._legacy_key(rel_path)
            # Guard against path traversal from stored keys.
            target = self.data_dir.joinpath(target_rel).resolve()
            try:
                target.relative_to(self.data_dir.resolve())
            except ValueError:
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
                        help="Runtime data dir containing webui/ sessions/ chat folders")
    parser.add_argument("--url", default=os.getenv("SUPABASE_URL", ""))
    parser.add_argument("--service-key", default=os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""))
    parser.add_argument("--backup", action="store_true", help="Upload changed chat files")
    parser.add_argument("--restore", action="store_true", help="Pull chat files from Supabase")
    parser.add_argument("--prune", action="store_true",
                        help="Remove cloud rows for sessions deleted locally (destructive)")
    parser.add_argument("--loop", type=int, default=0,
                        help="Repeat --backup every N seconds (0 = run once)")
    args = parser.parse_args()

    url = args.url or os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = args.service_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        print("[chat-sync] SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set; skipping", file=sys.stderr)
        return 0

    sync = ChatSync(data_dir=args.dir, url=url, service_key=key)

    if args.restore:
        sync.restore()
        return 0

    if args.prune:
        sync.prune()
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