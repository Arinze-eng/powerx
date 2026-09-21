"""Durable deployment credential overlay.

Render's and Northflank's service filesystem can be ephemeral, and requests may
be handled by separate service processes.  This module applies explicitly
configured server-side environment values to the in-memory execution config so
a wiped platform env self-heals.

It is credential-only by design: it never decides *which* execution backend is
used.  ``execution.backend`` is a persisted administrator setting (see
``ExecutionBackendConfig.backend_source``); the one-time seed from
``NANOBOT_EXECUTION_BACKEND`` happens at boot in ``scripts/ensure_render_config.py``.
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
    """Apply durable deployment credentials to ``config`` without picking a backend.

    This overlay fills in connection settings (VPS SSH details, Upstash/Daytona
    API keys) so a platform whose env was wiped can self-heal.  It deliberately
    never assigns ``execution.backend``: choosing *which* sandbox provider runs
    is an administrator decision, and rewriting it on every config load used to
    silently revert a saved selection (picking VPS could come back as Daytona on
    the next request).  The backend label is seeded once at boot by
    ``scripts/ensure_render_config.py`` and owned by the admin panel afterwards.

    Empty or malformed optional values are ignored, leaving the validated
    config-file value in place.  Secret values are only assigned in memory and
    are never logged or returned by this module.
    """
    execution = getattr(config, "execution", None)
    vps = getattr(execution, "vps", None)
    if execution is None or vps is None:
        return config
    upstash = getattr(execution, "upstash", None)
    daytona = getattr(execution, "daytona", None)
    runloop = getattr(execution, "runloop", None)
    vercel = getattr(execution, "vercel", None)
    # Credential-only overlay: env never changes which backend is selected.
    if (_env("NANOBOT_EXECUTION_BACKEND") or "").lower() not in {
        "novita",
        "vps",
        "upstash",
        "daytona",
        "runloop",
        "vercel",
    }:
        return config

    # Credential-only overlay. An administrator-saved value is always
    # authoritative: env vars restore what the config file leaves empty or at
    # its schema default (so a wiped platform self-heals), but they must never
    # overwrite a value the admin explicitly saved. This mirrors the Upstash
    # rule below and keeps every provider's settings handled the same way.
    def _fill(target: Any, field: str, value: str | None) -> None:
        if not value:
            return
        if str(getattr(target, field, "") or "").strip():
            return
        setattr(target, field, value)

    _fill(vps, "host", _env("NANOBOT_VPS_HOST"))
    _fill(vps, "username", _env("NANOBOT_VPS_USERNAME"))
    _fill(vps, "host_key_fingerprint", _env("NANOBOT_VPS_FINGERPRINT"))
    _fill(vps, "workspace_dir", _env("NANOBOT_VPS_WORKSPACE"))
    host_key_policy = (_env("NANOBOT_VPS_HOST_KEY_POLICY") or "").lower()
    if host_key_policy and str(getattr(vps, "host_key_policy", "") or "") == "fingerprint":
        vps.host_key_policy = host_key_policy

    port = _positive_int(_env("NANOBOT_VPS_PORT"), maximum=65535)
    timeout = _positive_int(_env("NANOBOT_VPS_TIMEOUT"), maximum=60)
    if port is not None and int(getattr(vps, "port", 22) or 22) == 22:
        vps.port = port
    if timeout is not None and int(getattr(vps, "connect_timeout", 15) or 15) == 15:
        vps.connect_timeout = timeout

    _fill(vps, "password", os.getenv("NANOBOT_VPS_PASSWORD"))
    _fill(vps, "private_key", os.getenv("NANOBOT_VPS_PRIVATE_KEY"))

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
    # Same credential-only, fill-blanks rule as VPS and Upstash: an admin-saved
    # key/endpoint wins, env restores what is missing.
    if daytona is not None:
        _fill(daytona, "api_key", _env("NANOBOT_DAYTONA_API_KEY") or _env("DAYTONA_API_KEY"))
        _fill(daytona, "api_url", _env("NANOBOT_DAYTONA_API_URL"))
        snapshot = _env("NANOBOT_DAYTONA_SNAPSHOT")
        if snapshot and str(getattr(daytona, "snapshot", "") or "") in {"", "daytona-small"}:
            daytona.snapshot = snapshot
        _fill(daytona, "domain_allow_list", _env("NANOBOT_DAYTONA_DOMAIN_ALLOW_LIST"))
        network_list = _env("NANOBOT_DAYTONA_NETWORK_ALLOW_LIST")
        if network_list and str(getattr(daytona, "network_allow_list", "") or "").strip() in {
            "",
            "0.0.0.0/0",
        }:
            daytona.network_allow_list = network_list
        _fill(daytona, "fetch_allow_hosts", _env("NANOBOT_DAYTONA_FETCH_ALLOW_HOSTS"))
        _fill(daytona, "relay_allow_hosts", _env("NANOBOT_DAYTONA_RELAY_ALLOW_HOSTS"))
        relay_enabled = _env("NANOBOT_DAYTONA_RELAY_ENABLED")
        if relay_enabled is not None and str(relay_enabled).strip().lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            daytona.relay_enabled = False
        relay_allow_http = _env("NANOBOT_DAYTONA_RELAY_ALLOW_HTTP")
        if relay_allow_http is not None and str(relay_allow_http).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            daytona.relay_allow_http = True
        relay_allow_private = _env("NANOBOT_DAYTONA_RELAY_ALLOW_PRIVATE_HOSTS")
        if relay_allow_private is not None and str(relay_allow_private).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            daytona.relay_allow_private_hosts = True
        relay_max_bytes = _positive_int(_env("NANOBOT_DAYTONA_RELAY_MAX_BYTES"), maximum=2_147_483_648)
        if relay_max_bytes is not None and relay_max_bytes >= 1_048_576:
            daytona.relay_max_bytes = relay_max_bytes
        ttl_minutes = _positive_int(_env("NANOBOT_DAYTONA_TTL_MINUTES"), maximum=43_200)
        if ttl_minutes is not None and ttl_minutes >= 5 and int(getattr(daytona, "ttl_minutes", 60) or 60) == 60:
            daytona.ttl_minutes = ttl_minutes
        _fill(daytona, "outbound_proxy_url", _env("NANOBOT_DAYTONA_OUTBOUND_PROXY_URL"))
    # Runloop overlay (used when the deployment selects the Runloop backend).
    # Same credential-only, fill-blanks rule as VPS, Upstash, and Daytona: an
    # admin-saved key/endpoint wins, env restores what is missing.
    if runloop is not None:
        _fill(runloop, "api_key", _env("NANOBOT_RUNLOOP_API_KEY") or _env("RUNLOOP_API_KEY"))
        _fill(runloop, "api_url", _env("NANOBOT_RUNLOOP_API_URL"))
        _fill(runloop, "snapshot_id", _env("NANOBOT_RUNLOOP_SNAPSHOT_ID"))
        _fill(runloop, "blueprint", _env("NANOBOT_RUNLOOP_BLUEPRINT"))
        resource_size = _env("NANOBOT_RUNLOOP_RESOURCE_SIZE")
        if resource_size and str(getattr(runloop, "resource_size", "") or "").strip() in {"", "SMALL"}:
            runloop.resource_size = resource_size.upper()
        architecture = _env("NANOBOT_RUNLOOP_ARCHITECTURE")
        if architecture and not str(getattr(runloop, "architecture", "") or "").strip():
            runloop.architecture = architecture.lower()
        keep_alive = _positive_int(_env("NANOBOT_RUNLOOP_KEEP_ALIVE"), maximum=604_800)
        if keep_alive is not None and keep_alive >= 60 and int(getattr(runloop, "keep_alive_seconds", 3600) or 3600) == 3600:
            runloop.keep_alive_seconds = keep_alive
        _fill(runloop, "fetch_allow_hosts", _env("NANOBOT_RUNLOOP_FETCH_ALLOW_HOSTS"))
    # Vercel Sandbox overlay (used when the deployment selects the Vercel
    # backend). Same credential-only, fill-blanks rule as every other provider:
    # an admin-saved token/endpoint wins, env restores what is missing.
    if vercel is not None:
        _fill(vercel, "token", _env("NANOBOT_VERCEL_TOKEN") or _env("VERCEL_TOKEN"))
        _fill(vercel, "api_url", _env("NANOBOT_VERCEL_API_URL"))
        _fill(vercel, "team_id", _env("NANOBOT_VERCEL_TEAM_ID") or _env("VERCEL_TEAM_ID"))
        _fill(vercel, "project_id", _env("NANOBOT_VERCEL_PROJECT_ID") or _env("VERCEL_PROJECT_ID"))
        runtime = _env("NANOBOT_VERCEL_RUNTIME")
        if runtime and str(getattr(vercel, "runtime", "") or "").strip() in {"", "node22"}:
            vercel.runtime = runtime
        vcpus = _positive_int(_env("NANOBOT_VERCEL_VCPUS"), maximum=8)
        if vcpus is not None and int(getattr(vercel, "vcpus", 2) or 2) == 2:
            vercel.vcpus = vcpus
        timeout_ms = _positive_int(_env("NANOBOT_VERCEL_TIMEOUT_MS"), maximum=2_700_000)
        if timeout_ms is not None and timeout_ms >= 60_000 and int(getattr(vercel, "timeout_ms", 300_000) or 300_000) == 300_000:
            vercel.timeout_ms = timeout_ms
        _fill(vercel, "fetch_allow_hosts", _env("NANOBOT_VERCEL_FETCH_ALLOW_HOSTS"))
    return config
