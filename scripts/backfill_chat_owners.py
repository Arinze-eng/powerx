#!/usr/bin/env python3
"""Backfill per-user chat ownership for the WebUI (post cross-user-leak fix).

Why
---
Per-user chat isolation was failing because the vast majority of WebUI chats
had **no recorded ``_webui_owner_user_id``** in their session metadata, and the
old isolation logic treated "unowned" chats as public (readable by / shown to
every authenticated user). That is how users saw each other's chats.

The runtime fix (fail-closed gates + claim-on-first-activity) stops new leaks,
but historic chats recorded before owner-stamping existed still have no owner.
This script stamps owners onto those legacy chats where an owner can be derived,
by reconciling each WebUI transcript against its **core session metadata file**
(``sessions/<dir>/<base64>.jsonl``), which reliably records ``metadata["_webui
_owner_user_id"]`` for chats that flowed through ``persist_scope()``.

Chats whose owner truly cannot be determined are left unowned. With the
fail-closed runtime they are now hidden from every user (safe) and become
reclaimable by their real owner via the next message to that chat
(claim-on-first-activity). The script prints a report so an operator can see
exactly how many chats were assigned vs left unowned.

Usage (run in the deployed container, as the nanobot user):
    NANOBOT_DATA_DIR=/home/nanobot/.nanobot python3 scripts/backfill_chat_owners.py --apply
    NANOBOT_DATA_DIR=/home/nanobot/.nanobot python3 scripts/backfill_chat_owners.py          # dry-run

Safety
------
* Only *adds* an owner when the chat currently has none and the derived owner
  is a real Supabase user id (a non-empty string that is a plausible UUID).
* Never overwrites an existing owner.
* Dry-run by default; use ``--apply`` to write.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

WEBUI_SESSION_OWNER_KEY = "_webui_owner_user_id"


def _iter_core_websocket_sessions(data_dir: Path):
    """Yield (chat_id, metadata_dict) for every websocket core session file."""
    sessions_dir = data_dir / "sessions"
    if not sessions_dir.is_dir():
        return
    for base, _dirs, files in os.walk(sessions_dir):
        for name in files:
            if not name.endswith(".jsonl") or name.startswith("."):
                continue
            file_path = Path(base) / name
            try:
                first = file_path.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
                record = json.loads(first)
            except Exception:
                continue
            if not isinstance(record, dict) or record.get("_type") != "metadata":
                continue
            key = record.get("key") or ""
            if not key.startswith("websocket:"):
                continue
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            yield key.split(":", 1)[1], metadata


def _unowned_transcript_chat_ids(data_dir: Path) -> list[str]:
    """Return websocket chat ids that appear in the webui transcript dir."""
    webui_dir = data_dir / "webui"
    if not webui_dir.is_dir():
        return []
    chat_ids: set[str] = set()
    for path in webui_dir.glob("websocket_*.jsonl"):
        stem = path.stem
        if stem.startswith("websocket_"):
            chat_ids.add(stem[len("websocket_"):])
    return sorted(chat_ids)


def _plausible_user_id(value: str) -> bool:
    value = (value or "").strip()
    # Supabase user ids are UUID v4. Accept the standard 8-4-4-4-12 form.
    import re

    return bool(re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        value,
    ))


def _ownership_map(data_dir: Path) -> dict[str, str]:
    """chat_id -> derived_owner from core session metadata."""
    out: dict[str, str] = {}
    for chat_id, metadata in _iter_core_websocket_sessions(data_dir):
        owner = (metadata.get(WEBUI_SESSION_OWNER_KEY) or "").strip()
        if _plausible_user_id(owner):
            out.setdefault(chat_id, owner)
    return out


def _session_metadata_path(data_dir: Path, chat_id: str) -> Path | None:
    """Find the core session file for a websocket chat id (base64 filename)."""
    encoded = base64.b64encode(f"websocket:{chat_id}".encode()).decode()
    for base, _dirs, _files in os.walk(data_dir / "sessions"):
        candidate = Path(base) / f"{encoded}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def _set_owner(data_dir: Path, chat_id: str, owner: str) -> None:
    """Stamp the owner onto the chat's core session metadata file (~/.nanobot/sessions)."""
    path = _session_metadata_path(data_dir, chat_id)
    if path is None:
        # No core file: create one in the top-level sessions dir so the owner
        # tag persists (the sidebar index reads it via session_owner_user_id).
        sessions_dir = data_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        encoded = base64.b64encode(f"websocket:{chat_id}".encode()).decode()
        path = sessions_dir / f"{encoded}.jsonl"
        # Initialize a minimal metadata record if the file does not exist.
        if not path.exists():
            now = ""
            path.write_text(
                json.dumps(
                    {
                        "_type": "metadata",
                        "key": f"websocket:{chat_id}",
                        "created_at": now,
                        "updated_at": now,
                        "metadata": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return
    if not lines or not lines[0].strip():
        return
    try:
        record = json.loads(lines[0])
    except json.JSONDecodeError:
        return
    if not isinstance(record, dict):
        return
    metadata = record.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
        record["metadata"] = metadata
    metadata[WEBUI_SESSION_OWNER_KEY] = owner
    lines[0] = json.dumps(record, ensure_ascii=False)
    try:
        path.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill WebUI chat owners")
    parser.add_argument(
        "--dir",
        default=os.path.join(os.path.expanduser("~"), ".nanobot"),
        help="Runtime data dir containing sessions/ and webui/ (default: ~/.nanobot)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write owners. Without it a dry-run report is printed.",
    )
    args = parser.parse_args()

    data_dir = Path(args.dir).expanduser()
    owner_map = _ownership_map(data_dir)
    transcript_ids = _unowned_transcript_chat_ids(data_dir)

    assigned = 0
    remaining = []
    for chat_id in transcript_ids:
        owner = owner_map.get(chat_id)
        if not owner:
            remaining.append(chat_id)
            continue
        # Check whether it is already owned (no-op if so).
        metadata_path = _session_metadata_path(data_dir, chat_id)
        already = ""
        if metadata_path is not None:
            try:
                first = metadata_path.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
                rec = json.loads(first)
                meta = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
                already = (meta.get(WEBUI_SESSION_OWNER_KEY) or "").strip()
            except Exception:
                already = ""
        if already == owner:
            continue
        if args.apply:
            _set_owner(data_dir, chat_id, owner)
        assigned += 1

    print(f"[backfill]\n  data_dir            = {data_dir}")
    print(f"  webui chats scanned = {len(transcript_ids)}")
    print(f"  owner derivable     = {assigned} ({'WROTE' if args.apply else 'would write'})")
    print(f"  left unowned        = {len(remaining)} (hidden until claimed)")
    for cid in remaining[:20]:
        print(f"    - {cid}")
    if len(remaining) > 20:
        print(f"    ... and {len(remaining) - 20} more")
    if not args.apply:
        print("\n  Re-run with --apply to write the derivable owners.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())