#!/usr/bin/env python3
"""Back up & restore nanobot scheduled cron jobs through Supabase ``system_settings``.

Why
---
Render's disk is ephemeral: every redeploy/redeploy creates a fresh container
whose ``~/.nanobot`` data dir is wiped. On the free plan there is no persistent
disk, so the nanobot cron store (``workspace/cron/jobs.json``, which holds every
user-scheduled reminder / assignment / recurring task) is destroyed on every
restart. Chat history and runtime env vars are already made durable through
Supabase (``supabase_chat_sync.py`` / ``supabase_env_sync.py``); scheduled cron
jobs were the one piece left unprotected — a registered job could silently stop
firing because its store vanished.

This script gives the cron store the same durable home in Supabase (which
already hosts the app's users, chat history and runtime secrets) so scheduled
jobs survive redeploys.

How
---
It reuses the existing ``system_settings`` key->value table (no DDL / migrations
needed) as an object store:

    key   = "cronbackup:jobs.json"   value = the raw jobs.json text
    key   = "cronbackup:meta"        value = JSON {"lastSyncMs":..., "sha256":...}

* ``--backup``  uploads the local ``jobs.json`` to Supabase (skipped when the
                local file is unchanged since the last sync, i.e. the stored
                sha256 matches).
* ``--restore`` pulls ``cronbackup:jobs.json`` back down and writes it to the
                local store *only if the local store is missing/empty* — so a
                freshly started container recovers its schedules, while an
                active container with real local jobs is never clobbered.
* ``--loop N``  repeats ``--backup`` every N seconds (run as an in-container
                sidecar so any newly scheduled job is pushed continuously; even
                if a redeploy kills the container, the schedule is preserved up
                to the last sync window).

Never fails the boot on a transient Supabase error: restores print a warning and
exit 0 so the app starts with whatever is on disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import httpx

KEY_JOBS = "cronbackup:jobs.json"
KEY_META = "cronbackup:meta"
# Rows above this many bytes are considered corrupt and are refused (guard
# against a pathological value; a real jobs.json is usually a few KB).
MAX_SYNC_BYTES = 2 * 1024 * 1024


class CronSync:
    def __init__(self, *, cron_store: str, url: str, service_key: str) -> None:
        self.cron_store = Path(cron_store).expanduser()
        self.url = url.rstrip("/")
        self.service_key = service_key
        self._client = httpx.Client(timeout=20.0, follow_redirects=False)

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    # -- cloud helpers -----------------------------------------------------

    def _fetch(self, key: str) -> str | None:
        resp = self._client.get(
            f"{self.url}/rest/v1/system_settings",
            params={"key": f"eq.{key}", "select": "value"},
            headers=self._headers(),
        )
        if not resp.is_success:
            return None
        payload = resp.json()
        if not isinstance(payload, list) or not payload:
            return None
        value = payload[0].get("value")
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        return None

    def _upsert(self, key: str, value: str) -> bool:
        resp = self._client.post(
            f"{self.url}/rest/v1/system_settings",
            params={"on_conflict": "key"},
            headers=self._headers(),
            json={"key": key, "value": value},
        )
        return resp.is_success

    # -- backup ------------------------------------------------------------

    def backup(self, *, quiet: bool = False) -> int:
        """Upload the local jobs.json to Supabase if it changed. Returns 1 on
        write, 0 if unchanged, -1 if nothing usable is on disk."""
        store = self.cron_store
        if not store.is_file():
            return -1
        try:
            if store.stat().st_size > MAX_SYNC_BYTES:
                print(f"[cron-sync] warning: {store} too large; skipping backup",
                      file=sys.stderr)
                return -1
            content = store.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[cron-sync] warning: could not read {store}: {type(exc).__name__}",
                  file=sys.stderr)
            return -1

        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        # Only write when the cloud copy differs from the local file.
        cloud = self._fetch(KEY_JOBS)
        if cloud == content:
            if not quiet:
                print("[cron-sync] backup: jobs.json unchanged")
            return 0

        ok_jobs = self._upsert(KEY_JOBS, content)
        if not ok_jobs:
            print("[cron-sync] warning: jobs.json upload failed", file=sys.stderr)
            return -1
        meta = json.dumps({"lastSyncMs": int(time.time() * 1000), "sha256": sha},
                          ensure_ascii=False)
        self._upsert(KEY_META, meta)
        if not quiet:
            print(f"[cron-sync] backup: uploaded jobs.json ({len(content)} bytes)")
        return 1

    # -- restore -----------------------------------------------------------

    def restore(self) -> int:
        """Pull jobs.json from Supabase to disk only when the local store is
        absent/empty. Returns 1 when restored, 0 when nothing needed."""
        store = self.cron_store
        # Do not clobber an active, non-empty local store.
        if store.is_file():
            try:
                if store.stat().st_size > 0 and store.read_text(
                    encoding="utf-8"
                ).strip():
                    return 0
            except OSError:
                pass

        cloud = self._fetch(KEY_JOBS)
        if cloud is None or not cloud.strip():
            return 0
        if len(cloud) > MAX_SYNC_BYTES:
            print("[cron-sync] warning: cloud jobs.json too large; refusing restore",
                  file=sys.stderr)
            return 0
        try:
            json.loads(cloud)  # sanity: only write valid JSON
        except ValueError:
            print("[cron-sync] warning: cloud jobs.json is not valid JSON; refusing restore",
                  file=sys.stderr)
            return 0

        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(cloud, encoding="utf-8")
        print(f"[cron-sync] restore: recovered {len(cloud)} bytes of scheduled jobs")
        return 1


def main() -> int:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    data_dir = os.getenv("NANOBOT_DATA_DIR", "").strip() or os.path.join(
        os.path.expanduser("~"), ".nanobot"
    )
    workspace = os.getenv("NANOBOT_WORKSPACE_DIR", "").strip()
    cron_store = workspace or os.path.join(data_dir, "workspace", "cron", "jobs.json")

    parser = argparse.ArgumentParser(description="Persist nanobot cron jobs to Supabase")
    parser.add_argument("--store", default=cron_store,
                        help="Path to the cron jobs.json store")
    parser.add_argument("--url", default=url)
    parser.add_argument("--service-key", default=service_key)
    parser.add_argument("--backup", action="store_true", help="Upload changed jobs")
    parser.add_argument("--restore", action="store_true", help="Pull jobs from Supabase")
    parser.add_argument("--loop", type=int, default=0,
                        help="Repeat --backup every N seconds (0 = run once)")
    args = parser.parse_args()

    url = args.url or os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = args.service_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        print("[cron-sync] SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set; skipping",
              file=sys.stderr)
        return 0

    sync = CronSync(cron_store=args.store, url=url, service_key=key)

    if args.restore:
        try:
            sync.restore()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"[cron-sync] restore warning: {type(exc).__name__}", file=sys.stderr)
        return 0

    if args.loop:
        print(f"[cron-sync] backup loop every {args.loop}s (pid {os.getpid()})", flush=True)
        first = True
        while True:
            try:
                sync.backup(quiet=not first)
            except (httpx.HTTPError, ValueError) as exc:
                print(f"[cron-sync] backup error: {type(exc).__name__}", file=sys.stderr)
            first = False
            time.sleep(args.loop)
    else:
        sync.backup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
