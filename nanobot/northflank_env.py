"""Store runtime configuration in the Northflank service environment.

The provider pool's durable home is a Northflank service *environment variable*
rather than a database table, so nothing round-trips through Supabase and no
egress is billed for it. The value survives restarts and redeploys, and the
running instance is refreshed in-process by the caller so a save applies
immediately.

Auth is a Northflank API token held in this service's own environment
(``NORTHFLANK_API_TOKEN``), alongside the project and service identifiers. When
the token is absent :func:`configured` returns ``False`` and the admin panel
keeps its in-instance copy only.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

TOKEN_VAR = "NORTHFLANK_API_TOKEN"
PROJECT_VAR = "NORTHFLANK_PROJECT_ID"
SERVICE_VAR = "NORTHFLANK_SERVICE_ID"
DEFAULT_PROJECT = "minis"
DEFAULT_SERVICE = "powerx"

API_BASE = os.getenv("NORTHFLANK_API_BASE", "https://api.northflank.com/v1").rstrip("/")

_TIMEOUT = 30.0


class NorthflankError(RuntimeError):
    """Raised when a Northflank API call cannot be completed."""


def config() -> dict[str, str] | None:
    """Return the Northflank connection settings, or None when unconfigured."""
    token = os.getenv(TOKEN_VAR, "").strip()
    if not token:
        return None
    project = os.getenv(PROJECT_VAR, "").strip() or DEFAULT_PROJECT
    service = os.getenv(SERVICE_VAR, "").strip() or DEFAULT_SERVICE
    return {"token": token, "project": project, "service": service}


def configured() -> bool:
    """Whether a Northflank API token is available to write with."""
    return config() is not None


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _request(
    method: str,
    path: str,
    cfg: dict[str, str],
    *,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{API_BASE}{path}"
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as client:
            response = client.request(
                method, url, headers=_headers(cfg["token"]), json=json_body
            )
    except httpx.HTTPError as exc:
        raise NorthflankError(f"Northflank request failed ({type(exc).__name__})") from exc
    if response.status_code >= 400:
        raise NorthflankError(f"Northflank API returned HTTP {response.status_code}")
    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError as exc:
        raise NorthflankError("Northflank returned a non-JSON response") from exc
    return payload if isinstance(payload, dict) else {}


def get_runtime_environment() -> dict[str, str]:
    """Return the service's own runtime environment variables."""
    cfg = config()
    if cfg is None:
        raise NorthflankError("Northflank API token is not configured")
    payload = _request(
        "GET",
        f"/projects/{cfg['project']}/services/{cfg['service']}/runtime-environment",
        cfg,
    )
    environment = (payload.get("data") or {}).get("runtimeEnvironment") or {}
    return {str(key): str(value) for key, value in environment.items()}


def set_env_var(name: str, value: str) -> dict[str, str]:
    """Merge *name* into the service environment and persist it on Northflank."""
    cfg = config()
    if cfg is None:
        raise NorthflankError("Northflank API token is not configured")
    current = get_runtime_environment()
    current[name] = value
    _request(
        "PATCH",
        f"/projects/{cfg['project']}/services/combined/{cfg['service']}",
        cfg,
        json_body={"runtimeEnvironment": current},
    )
    return current


def restart_service() -> None:
    """Restart the service so a changed environment takes effect."""
    cfg = config()
    if cfg is None:
        raise NorthflankError("Northflank API token is not configured")
    _request("POST", f"/projects/{cfg['project']}/services/{cfg['service']}/restart", cfg)
