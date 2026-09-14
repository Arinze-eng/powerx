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
    if backend not in {"novita", "vps", "upstash", "daytona"}:
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
    upstash = getattr(execution, "upstash", None)
    upstash_configured = bool(str(getattr(upstash, "api_key", "") or "").strip()) if upstash is not None else False
    daytona = getattr(execution, "daytona", None)
    daytona_configured = bool(str(getattr(daytona, "api_key", "") or "").strip()) if daytona is not None else False
    # explicit_novita guards an admin's deliberate switch BACK to novita; its
    # marker is another backend's key saved on disk (admin saves persist the
    # env-overlaid key). Env-provided keys must NOT count here, or the durable
    # env default could never restore a backend on a fresh instance.
    explicit_novita = configured_backend == "novita" and (vps_configured or upstash_configured or daytona_configured)
    explicit_vps = configured_backend == "vps" and vps_configured
    # A saved Daytona/Upstash admin choice is authoritative on its own. The API
    # key may live in the deployment env (synced from Supabase at boot) rather
    # than in the saved config file, so requiring a key on disk would let the
    # durable env default stomp the admin's selection on every config load and
    # silently fall back to the initial sandbox.
    explicit_daytona = configured_backend == "daytona"
    explicit_upstash = configured_backend == "upstash"
    if not (explicit_novita or explicit_vps or explicit_daytona or explicit_upstash):
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

    # Upstash Box overlay (used when the deployment selects the Upstash backend).
    # An admin-saved value is authoritative: env vars are durable defaults that
    # fill in what the config file leaves at its default/empty, but they must
    # NEVER overwrite a value the admin explicitly saved. Overwriting on every
    # config load silently reverted admin key/endpoint changes and made the box
    # fail with stale credentials that read as "the endpoint has changed".
    if upstash is not None:
        api_key = _env("NANOBOT_UPSTASH_API_KEY") or _env("UPSTASH_BOX_API_KEY")
        if api_key and not str(getattr(upstash, "api_key", "") or "").strip():
            upstash.api_key = api_key
        base_url = _env("NANOBOT_UPSTASH_BASE_URL")
        if base_url and str(getattr(upstash, "base_url", "") or "").rstrip("/") in {
            "",
            "https://us-east-1.box.upstash.com",
        }:
            upstash.base_url = base_url
        runtime = _env("NANOBOT_UPSTASH_RUNTIME")
        if runtime and str(getattr(upstash, "runtime", "") or "").strip() in {"", "python"}:
            upstash.runtime = runtime
        size = _env("NANOBOT_UPSTASH_SIZE")
        if size and str(getattr(upstash, "size", "") or "").strip() in {"", "small"}:
            upstash.size = size
        ttl = _positive_int(_env("NANOBOT_UPSTASH_TTL"), maximum=86_400)
        if ttl is not None and (int(getattr(upstash, "ttl_s", 0) or 0)) == 3600:
            upstash.ttl_s = ttl

    # Daytona overlay (used when the deployment selects the Daytona backend).
    if daytona is not None:
        api_key = _env("NANOBOT_DAYTONA_API_KEY") or _env("DAYTONA_API_KEY")
        if api_key:
            daytona.api_key = api_key
        api_url = _env("NANOBOT_DAYTONA_API_URL")
        if api_url:
            daytona.api_url = api_url
        snapshot = _env("NANOBOT_DAYTONA_SNAPSHOT")
        if snapshot:
            daytona.snapshot = snapshot
        domain_list = _env("NANOBOT_DAYTONA_DOMAIN_ALLOW_LIST")
        if domain_list:
            daytona.domain_allow_list = domain_list
        network_list = _env("NANOBOT_DAYTONA_NETWORK_ALLOW_LIST")
        if network_list:
            daytona.network_allow_list = network_list
        fetch_hosts = _env("NANOBOT_DAYTONA_FETCH_ALLOW_HOSTS")
        if fetch_hosts:
            daytona.fetch_allow_hosts = fetch_hosts
        ttl_minutes = _positive_int(_env("NANOBOT_DAYTONA_TTL_MINUTES"), maximum=43_200)
        if ttl_minutes is not None and ttl_minutes >= 5:
            daytona.ttl_minutes = ttl_minutes
    return config
