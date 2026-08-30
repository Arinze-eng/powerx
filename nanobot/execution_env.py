"""Durable Render execution-settings overlay.

Render's free service filesystem is ephemeral and requests may be handled by
separate service processes.  This module applies explicitly configured
server-side environment values to the in-memory execution config.  It is
intentionally a no-op unless ``NANOBOT_EXECUTION_BACKEND`` is set, so local
config files and the Novita default remain unchanged.
"""

from __future__ import annotations

import os
from typing import Any


def _env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value is not None else None


def _positive_int(value: str | None, *, maximum: int) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if 1 <= parsed <= maximum else None


def apply_render_execution_env(config: Any) -> Any:
    """Apply optional durable Render execution settings to ``config``.

    The overlay is activated only when the backend variable explicitly equals
    ``novita`` or ``vps``.  Empty or malformed optional values are ignored,
    leaving the validated config-file value in place.  Secret values are only
    assigned in memory and are never logged or returned by this module.
    """
    backend = (_env("NANOBOT_EXECUTION_BACKEND") or "").lower()
    if backend not in {"novita", "vps"}:
        return config
    execution = getattr(config, "execution", None)
    vps = getattr(execution, "vps", None)
    if execution is None or vps is None:
        return config
    configured_backend = str(getattr(execution, "backend", "") or "").lower()
    # A saved admin choice is authoritative once the config contains VPS
    # details. This lets the admin switch back to Novita without the old
    # durable VPS default overriding the choice on every request. On a fresh
    # Render instance the template has no VPS details, so the durable VPS
    # environment still restores the selected backend as intended.
    vps_configured = any(
        str(getattr(vps, field, "") or "").strip()
        for field in ("host", "username", "password", "private_key")
    )
    explicit_novita = configured_backend == "novita" and vps_configured
    if not explicit_novita:
        execution.backend = backend

    values = {
        "host": _env("NANOBOT_VPS_HOST"),
        "username": _env("NANOBOT_VPS_USERNAME"),
        "host_key_fingerprint": _env("NANOBOT_VPS_FINGERPRINT"),
        "host_key_policy": (_env("NANOBOT_VPS_HOST_KEY_POLICY") or "").lower() or None,
        "workspace_dir": _env("NANOBOT_VPS_WORKSPACE"),
    }
    for field, value in values.items():
        if value is not None:
            setattr(vps, field, value)

    port = _positive_int(_env("NANOBOT_VPS_PORT"), maximum=65535)
    timeout = _positive_int(_env("NANOBOT_VPS_TIMEOUT"), maximum=60)
    if port is not None:
        vps.port = port
    if timeout is not None:
        vps.connect_timeout = timeout

    password = os.getenv("NANOBOT_VPS_PASSWORD")
    private_key = os.getenv("NANOBOT_VPS_PRIVATE_KEY")
    if password:
        vps.password = password
    if private_key:
        vps.private_key = private_key
    return config
