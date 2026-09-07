#!/usr/bin/env python3
"""Build/publish a custom Novita sandbox template with increased RAM.

The built-in ``base`` / ``browser-chromium`` templates default to ~1 GB of
memory (the novita-sandbox SDK's ``memory_mb`` default is 1024). Heavy tasks
such as rebuilding an APK with apktool OOM inside those sandboxes. This script
publishes a clone of a built-in template configured with more CPU/RAM so every
sandbox spawned from it gets the larger allocation.

Usage:
    NOVITA_API_KEY=sk_... python scripts/build_novita_template.py \
        --source base --alias powerx-base-4g --cpu 2 --memory-mb 4096

The resulting alias is wired into the agent via the ``NOVITA_SANDBOX_TEMPLATE``
environment variable (see nanobot/agent/tools/novita_sandbox.py).
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="base", help="Built-in template to clone (default: base)")
    parser.add_argument("--alias", default="powerx-base-4g", help="Alias for the new template")
    parser.add_argument("--cpu", type=int, default=2, help="vCPU count per sandbox")
    parser.add_argument("--memory-mb", type=int, default=4096, help="Memory in MB per sandbox")
    args = parser.parse_args()

    api_key = os.getenv("NOVITA_API_KEY", "").strip()
    if not api_key:
        print("ERROR: NOVITA_API_KEY is not set", file=sys.stderr)
        return 2

    try:
        from novita_sandbox import Novita
    except ImportError:
        print("ERROR: novita-sandbox package is not installed", file=sys.stderr)
        return 2

    n = Novita(api_key=api_key)

    def log(entry):
        msg = getattr(entry, "message", None) or str(entry)
        print(f"[build] {msg}")

    # Idempotency: skip if the alias already exists.
    try:
        if n.template.alias_exists(args.alias):
            print(f"Template alias '{args.alias}' already exists — skipping build.")
            return 0
    except Exception as exc:  # pragma: no cover - defensive
        print(f"WARN: alias_exists check failed ({exc}); continuing.", file=sys.stderr)

    print(
        f"Building template '{args.alias}' from '{args.source}' "
        f"(cpu={args.cpu}, memory={args.memory_mb}MB)..."
    )
    info = n.template.build(
        n.template.from_template(args.source),
        alias=args.alias,
        cpu_count=args.cpu,
        memory_mb=args.memory_mb,
        on_build_logs=log,
    )
    template_id = getattr(info, "template_id", None) or getattr(info, "templateID", None)
    print(f"DONE. alias='{args.alias}' template_id={template_id}")
    print(f"Set NOVITA_SANDBOX_TEMPLATE={args.alias} in your deployment env to use it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
