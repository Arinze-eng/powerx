"""Admin-managed pool of OpenAI-compatible provider endpoints.

A pool entry is one OpenAI-compatible lane:
``{id, baseUrl, apiKey, model, label, enabled}``. The admin panel can manage up
to :data:`MAX_POOL_ENTRIES` of them; the runtime rotates across the enabled
entries so that no single key or endpoint is able to rate-limit the whole agent.

Entries are stored as JSON on disk, next to the runtime config, so they survive
restarts on the platform's persistent volume. API keys live only on the server:
:func:`public_entries` masks them before anything reaches the browser and they
are never logged.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_POOL_ENTRIES = 40

_MAX_API_BASE = 2048
_MAX_API_KEY = 4096
_MAX_MODEL_ID = 512
_MAX_LABEL = 80

_LOCK = threading.RLock()


def data_dir() -> Path:
    """Directory the runtime config lives in (also holds the pool file)."""
    configured = os.getenv("NANOBOT_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(os.getenv("HOME", ".")) / ".nanobot"


def pool_path() -> Path:
    """Path of the JSON pool file (``PROVIDER_POOL_PATH`` overrides it)."""
    override = os.getenv("PROVIDER_POOL_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return data_dir() / "provider_pool.json"


def mask_api_key(api_key: str) -> str:
    """Return a browser-safe rendering of a key (never the whole secret)."""
    value = str(api_key or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def normalize_api_base(raw: str) -> str:
    """Validate and canonicalise an OpenAI-compatible base URL."""
    value = str(raw or "").strip().rstrip("/")
    if value.endswith("/chat/completions"):
        value = value[: -len("/chat/completions")].rstrip("/")
    if len(value) > _MAX_API_BASE:
        raise ValueError("API base URL is too long")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("API base must be an http(s) URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("API base must not contain credentials or a fragment")
    if any(ord(char) < 0x20 for char in value):
        raise ValueError("API base contains invalid characters")
    return value


def _clean(raw: dict[str, Any], *, entry_id: str | None = None) -> dict[str, Any]:
    """Validate one raw entry, returning the stored shape."""
    base_url = normalize_api_base(str(raw.get("baseUrl") or raw.get("base_url") or ""))
    model = str(raw.get("model") or "").strip()[:_MAX_MODEL_ID]
    if not model:
        raise ValueError("model ID is required")
    api_key = str(raw.get("apiKey") or raw.get("api_key") or "").strip()[:_MAX_API_KEY]
    if not api_key:
        raise ValueError("API key is required")
    label = str(raw.get("label") or "").strip()[:_MAX_LABEL]
    resolved_id = entry_id or str(raw.get("id") or "").strip() or secrets.token_hex(6)
    return {
        "id": resolved_id,
        "baseUrl": base_url,
        "apiKey": api_key,
        "model": model,
        "label": label,
        "enabled": bool(raw.get("enabled", True)),
    }


def load_pool() -> list[dict[str, Any]]:
    """Return the stored entries (empty list when absent or unreadable)."""
    path = pool_path()
    with _LOCK:
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    rows = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    entries: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            entries.append(_clean(row, entry_id=str(row.get("id") or "") or None))
        except ValueError:
            continue
    return entries[:MAX_POOL_ENTRIES]


def save_pool(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Atomically persist *entries* and return the normalised list."""
    cleaned = [_clean(row, entry_id=str(row.get("id") or "") or None) for row in entries]
    cleaned = cleaned[:MAX_POOL_ENTRIES]
    path = pool_path()
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps({"entries": cleaned}, indent=2), encoding="utf-8")
        with suppress(OSError):
            os.chmod(temp, 0o600)
        os.replace(temp, path)
    return cleaned


def enabled_entries() -> list[dict[str, Any]]:
    """Enabled entries, in stored order (the pool rotation lanes)."""
    return [entry for entry in load_pool() if entry.get("enabled", True)]


def public_entries() -> list[dict[str, Any]]:
    """Entries with the API key masked, safe to hand to the browser."""
    return [
        {
            "id": entry["id"],
            "baseUrl": entry["baseUrl"],
            "apiKeyMasked": mask_api_key(entry["apiKey"]),
            "model": entry["model"],
            "label": entry["label"],
            "enabled": bool(entry.get("enabled", True)),
        }
        for entry in load_pool()
    ]


def get_entry(entry_id: str) -> dict[str, Any] | None:
    for entry in load_pool():
        if entry["id"] == entry_id:
            return entry
    return None


def _duplicate(entries: list[dict[str, Any]], candidate: dict[str, Any]) -> bool:
    return any(
        entry["baseUrl"] == candidate["baseUrl"]
        and entry["model"] == candidate["model"]
        and entry["apiKey"] == candidate["apiKey"]
        for entry in entries
    )


def add_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate, de-duplicate, and append one entry (rejects at the cap)."""
    with _LOCK:
        entries = load_pool()
        if len(entries) >= MAX_POOL_ENTRIES:
            raise ValueError(f"Provider pool is full ({MAX_POOL_ENTRIES} entries maximum)")
        candidate = _clean(raw)
        if _duplicate(entries, candidate):
            raise ValueError("That base URL, key and model already exist in the pool")
        entries.append(candidate)
        save_pool(entries)
        return candidate


def remove_entry(entry_id: str) -> bool:
    with _LOCK:
        entries = load_pool()
        remaining = [entry for entry in entries if entry["id"] != entry_id]
        if len(remaining) == len(entries):
            return False
        save_pool(remaining)
        return True


def update_entry(entry_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Update label / enabled / (optionally) base URL, key and model."""
    with _LOCK:
        entries = load_pool()
        for index, entry in enumerate(entries):
            if entry["id"] != entry_id:
                continue
            merged = dict(entry)
            if "label" in changes and changes["label"] is not None:
                merged["label"] = str(changes["label"]).strip()[:_MAX_LABEL]
            if "enabled" in changes and changes["enabled"] is not None:
                merged["enabled"] = bool(changes["enabled"])
            for field, key in (("baseUrl", "baseUrl"), ("model", "model"), ("apiKey", "apiKey")):
                value = changes.get(field)
                if value:
                    merged[key] = value
            updated = _clean(merged, entry_id=entry_id)
            others = [row for row in entries if row["id"] != entry_id]
            if _duplicate(others, updated):
                raise ValueError("That base URL, key and model already exist in the pool")
            entries[index] = updated
            save_pool(entries)
            return updated
    raise ValueError("Pool entry not found")
