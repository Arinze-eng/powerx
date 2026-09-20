"""Runtime path helpers derived from the active config context."""

from __future__ import annotations

import os
from pathlib import Path

from nanobot.utils.helpers import ensure_dir


def get_config_path() -> Path:
    """Get the configuration file path (lazy import to break circular dependency).

    Delegates to ``nanobot.config.loader.get_config_path`` at call time so
    that importing this module never triggers a circular import during startup.
    """
    from nanobot.config.loader import get_config_path as _loader_get_config_path
    return _loader_get_config_path()


def get_data_dir() -> Path:
    """Return the instance-level runtime data directory."""
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """Return a named runtime subdirectory under the instance data dir."""
    return ensure_dir(get_data_dir() / name)


def get_media_dir(channel: str | None = None) -> Path:
    """Return the media directory, optionally namespaced per channel."""
    base = get_runtime_subdir("media")
    return ensure_dir(base / channel) if channel else base


def get_cron_dir() -> Path:
    """Return the cron storage directory."""
    return get_runtime_subdir("cron")


def get_logs_dir() -> Path:
    """Return the logs directory."""
    return get_runtime_subdir("logs")


def get_webui_dir() -> Path:
    """Return the directory for WebUI-only persisted display threads (JSON)."""
    return get_runtime_subdir("webui")


def get_workspace_path(workspace: str | Path | None = None) -> Path:
    """Resolve and ensure the agent workspace path."""
    path = Path(workspace).expanduser() if workspace else Path.home() / ".nanobot" / "workspace"
    return ensure_dir(path)


def is_default_workspace(workspace: str | Path | None) -> bool:
    """Return whether a workspace resolves to nanobot's default workspace path."""
    current = Path(workspace).expanduser() if workspace is not None else Path.home() / ".nanobot" / "workspace"
    default = Path.home() / ".nanobot" / "workspace"
    return current.resolve(strict=False) == default.resolve(strict=False)


def get_cli_history_path() -> Path:
    """Return the shared CLI history file path."""
    return Path.home() / ".nanobot" / "history" / "cli_history"


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return Path.home() / ".nanobot" / "sessions"


def get_persistent_data_dir(namespace: str | None = None) -> Path:
    """Return the durable storage root for caches, learned plans, and memory.

    Prefers the ``POWERX_DATA_DIR`` environment variable (Northflank mounts
    the persistent disk volume at ``/data`` by default), falls back to
    ``/data`` when it exists and is writable, and otherwise defaults to the
    instance runtime data root so nothing is ever lost silently.
    """
    override = (os.environ.get("POWERX_DATA_DIR") or "").strip()
    if override:
        base = ensure_dir(Path(override).expanduser())
    else:
        candidate = Path("/data")
        try:
            if candidate.is_dir() and os.access(candidate, os.W_OK):
                base = ensure_dir(candidate / "powerx")
            else:
                base = get_runtime_subdir("persistent")
        except OSError:
            base = get_runtime_subdir("persistent")
    return ensure_dir(base / namespace) if namespace else base


def get_cron_store_path() -> Path:
    """Return the cron job store path: ALWAYS on the durable volume.

    This used to be ``workspace_path / "cron" / "jobs.json"``. That was the
    single reason user cron jobs never fired in production: the workspace is
    ``$HOME/.nanobot/workspace``, which on Northflank is **container
    filesystem**, not the mounted 6 GB volume (the volume is at ``/data``).
    Every deploy or restart recreated the container from the image, deleting
    ``jobs.json`` along with it, so the scheduler started with an empty job
    list forever.

    It is written this way deliberately so the failure is impossible to repeat
    by accident: cron is now stored beside ``plan_memory``/``memoize``/
    ``reflection``, the same durable root every other piece of long-lived state
    already uses, and it follows ``POWERX_DATA_DIR`` like they do.

    Note the in-process ``heartbeat`` job kept firing throughout the outage
    because it is registered programmatically at boot rather than read from the
    wiped file — which is exactly why the symptom looked like "the scheduler is
    alive but my reminders never happen".
    """
    return ensure_dir(get_persistent_data_dir("cron")) / "jobs.json"
