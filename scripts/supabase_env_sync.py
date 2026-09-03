#!/usr/bin/env python3
"""Bootstrap runtime environment variables from Supabase ``system_settings``.

Purpose
-------
The Render service is configured purely from environment variables. If Render's
environment is ever wiped (new service, redeploy from scratch, credentials lost),
the app would fail to boot even though the Supabase project still holds all the
runtime secrets. This small bootstrap loads every runtime environment variable
from the Supabase ``system_settings`` table (key -> value) so the app only needs
``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY`` to be present. As long as those
two credentials are set (they are plain values already committed in
``render.yaml``), every other secret is recovered from Supabase.

Behaviour
---------
* Reads every row from ``system_settings``.
* For each known runtime key that is NOT already present in the current process
  environment, it exports the value.
* Optionally (``--seed``) it writes the current process environment back into
  ``system_settings`` so an operator can snapshot a working Render config into
  Supabase exactly once (idempotent: existing keys are left untouched unless a
  value changed).
* Never fails the boot on a transient Supabase error: if Supabase is unreachable
  it prints a warning and exits 0 so the process continues with whatever env it
  already has.

Usage
-----
Called from ``entrypoint.sh`` before the app starts:

    python3 scripts/supabase_env_sync.py --config-prefix nanobot_

A one-time seed while the Render env is healthy:

    python3 scripts/supabase_env_sync.py --seed
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from typing import Any

import httpx

# Every runtime env var the container relies on. These are the ones we snapshot
# into Supabase (--seed) and recover on boot (default). Add any new secret that
# must survive a Render env wipe.
RUNTIME_KEYS: tuple[str, ...] = (
    # LLM / provider
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "NOVITA_API_KEY",
    # Telegram
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_ORIGIN",
    "TELEGRAM_WEBHOOK_PATH",
    "TELEGRAM_WEBHOOK_SECRET",
    "TELEGRAM_WEBHOOK_URL",
    # Auth / gateway
    "ADMIN_PASSWORD",
    "ADMIN_USERNAME",
    "NANOBOT_WEB_TOKEN",
    "SUPABASE_GATEWAY_KEY",
    "SUPABASE_ANON_KEY",
    "SUPABASE_TOKEN_ENCRYPTION_KEY",
    "SUPABASE_TOKEN_ENCRYPTION_KEY_PREVIOUS",
    "SUPABASE_TOKEN_ENCRYPTION_KEY_OLD",
    "SUPABASE_WEBUI_AUTH",
    "SUPABASE_AUTH_ENABLED",
    # UniAbuja DBQ
    "UNIABUJA_DBQ_URL",
    "UNIABUJA_DBQ_KEY",
    # WebUI upstream (used by the reverse-proxy wiring)
    "WEBUI_UPSTREAM",
    "WEBUI_WS_UPSTREAM",
)

# Storage prefix for runtime config keys. Using a prefix avoids clashing with
# app-owned settings that already live in system_settings (credit_usage_rate,
# wormgpt_api_key, ...).
PREFIX = "nanobot_"


class SupabaseEnvSync:
    """Snapshot / recover runtime env vars through Supabase system_settings."""

    def __init__(self) -> None:
        self.url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        self.service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        self.enabled = bool(self.url and self.service_key)

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def _fetch(self) -> dict[str, str]:
        """Return the ``{prefixed_key: value}`` map currently in system_settings."""
        if not self.enabled:
            return {}
        try:
            with httpx.Client(timeout=15.0, follow_redirects=False) as client:
                resp = client.get(
                    f"{self.url}/rest/v1/system_settings",
                    params={"limit": "5000"},
                    headers=self._headers(),
                )
            if not resp.is_success:
                print(
                    f"[supabase-env-sync] warning: fetch failed HTTP {resp.status_code}",
                    file=sys.stderr,
                )
                return {}
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            print(
                f"[supabase-env-sync] warning: could not read Supabase settings: {type(exc).__name__}",
                file=sys.stderr,
            )
            return {}
        out: dict[str, str] = {}
        if not isinstance(payload, list):
            return out
        for row in payload:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "")
            value = row.get("value")
            if not key.startswith(PREFIX):
                continue
            if isinstance(value, str):
                out[key] = value
            elif isinstance(value, (int, float, bool)):
                out[key] = str(value)
            elif isinstance(value, dict):
                # Some settings are JSON objects; store their compact JSON.
                try:
                    import json

                    out[key] = json.dumps(value, ensure_ascii=False)
                except Exception:
                    pass
        return out

    def apply(self) -> int:
        """Export missing runtime env vars from Supabase. Returns count applied."""
        stored = self._fetch()
        applied = 0
        for key in RUNTIME_KEYS:
            if os.getenv(key):
                continue  # already set in the process environment
            value = stored.get(PREFIX + key)
            if value is None:
                continue
            os.environ[key] = value
            applied += 1
        if applied:
            print(f"[supabase-env-sync] exported {applied} runtime env var(s) from Supabase")
        return applied

    def seed(self) -> int:
        """Snapshot current env into Supabase (idempotent). Returns count written."""
        if not self.enabled:
            print("[supabase-env-sync] Supabase credentials missing; not seeding", file=sys.stderr)
            return 0
        existing = self._fetch()
        written = 0
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            for key in RUNTIME_KEYS:
                value = os.getenv(key, "").strip()
                if not value:
                    continue
                prefixed = PREFIX + key
                if existing.get(prefixed) == value:
                    continue  # already recorded with the same value
                body = {"key": prefixed, "value": value}
                try:
                    # Upsert: insert or update by key.
                    resp = client.post(
                        f"{self.url}/rest/v1/system_settings",
                        params={"on_conflict": "key"},
                        headers=self._headers(),
                        json=body,
                    )
                    if resp.is_success:
                        written += 1
                except httpx.HTTPError as exc:
                    print(
                        f"[supabase-env-sync] warning: seed {prefixed} failed: {type(exc).__name__}",
                        file=sys.stderr,
                    )
        print(f"[supabase-env-sync] seeded {written} runtime env var(s) into Supabase")
        return written

    def emit_shell(self) -> None:
        """Write shell ``export KEY='value'`` lines to stdout.

        The entrypoint sources this output so the exported variables reach the
        launched nanobot process (a child Python env never propagates to its
        parent shell). Values are shell-quoted with ``shlex.quote``.
        """
        stored = self._fetch()
        for key in RUNTIME_KEYS:
            if os.getenv(key):
                continue
            value = stored.get(PREFIX + key)
            if value is None:
                continue
            print(f"export {key}={shlex.quote(value)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover runtime env vars from Supabase")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Snapshot the current process environment into Supabase system_settings",
    )
    parser.add_argument(
        "--emit-shell",
        action="store_true",
        help="Print safe `export KEY='value'` lines for the entrypoint to source",
    )
    args = parser.parse_args()
    sync = SupabaseEnvSync()
    if not sync.enabled:
        print("[supabase-env-sync] SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set; skipping", file=sys.stderr)
        return 0
    if args.seed:
        sync.seed()
        return 0
    if args.emit_shell:
        sync.emit_shell()
        return 0
    sync.apply()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())