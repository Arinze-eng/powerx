#!/usr/bin/env python3
"""Print the resolved cron store path, for the entrypoint's durability audit.

Kept separate from the sync scripts so the entrypoint can ask the *application's
own* code where cron will live, instead of duplicating path logic in shell. If
the import fails the entrypoint just skips the audit; it must never be able to
break boot.
"""
from __future__ import annotations

import sys


def main() -> int:
    try:
        from nanobot.config.paths import get_cron_store_path

        print(get_cron_store_path())
        return 0
    except Exception as exc:  # noqa: BLE001 - any failure is non-fatal here
        print(f"print_cron_store: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())