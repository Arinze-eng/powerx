"""Rewrite stored cron jobs that were pinned to UTC onto the deployment zone.

A cron expression is wall-clock time in a named zone, and the zone is stored
with the job. Every job created through the agent before this deployment had a
timezone at all was stamped with the tool's hard-coded ``"UTC"`` default, and
every job created by hand with the zone left blank stored ``tz: null`` — which
the scheduler then read in the *container's* zone, also UTC. For an owner at
UTC+1 both cases fired exactly one hour late, which is indistinguishable from
"the scheduler is wrong" and was reported as such.

Fresh jobs are fixed at the source (``agents.defaults.timezone`` is pinned to
Africa/Lagos and the scheduler now falls back to it). This script fixes the jobs
that are already on the durable volume, because nothing else rewrites an
existing store: the scheduler recomputes ``nextRunAtMs`` from each job's stored
zone, so a wrong zone stays wrong forever.

Only jobs whose zone is missing or a UTC alias are touched. A job that names a
real zone (``Asia/Kuwait``, ``America/New_York``) meant it and is left alone.

Idempotent: once every job carries a real zone there is nothing to rewrite, so
it is safe to run on every boot. ``nextRunAtMs`` is deliberately reset to null —
``CronService.start()`` recomputes it for every enabled job from the corrected
zone, and leaving a stale timestamp in place is how a job fires at the old hour
one last time.

Usage::

    python3 scripts/migrate_cron_timezone.py [jobs.json]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_DEFAULT_TIMEZONE = (
    os.environ.get("NANOBOT_DEFAULT_TIMEZONE") or ""
).strip() or "Africa/Lagos"

#: Lower-cased: compared against what is actually in the store, which has held
#: hand-typed spellings like " utc " and "etc/gmt".
_UTC_ALIASES = frozenset(
    {
        "etc/gmt",
        "etc/utc",
        "gmt",
        "gmt0",
        "greenwich",
        "uct",
        "universal",
        "utc",
        "zulu",
    }
)


def _is_utc_alias(timezone: object) -> bool:
    return isinstance(timezone, str) and timezone.strip().lower() in _UTC_ALIASES


def default_store_path() -> Path:
    """Return the durable cron store path, mirroring nanobot.config.paths."""
    override = (os.environ.get("POWERX_DATA_DIR") or "").strip()
    if override:
        return Path(override).expanduser() / "cron" / "jobs.json"
    candidate = Path("/data")
    if candidate.is_dir() and os.access(candidate, os.W_OK):
        return candidate / "powerx" / "cron" / "jobs.json"
    return Path.home() / ".nanobot" / "persistent" / "cron" / "jobs.json"


def migrate(store_path: Path, target_timezone: str = _DEFAULT_TIMEZONE) -> int:
    """Repoint every UTC-or-unset cron job at *target_timezone*.

    Returns the number of jobs rewritten. Never raises on a missing or
    unreadable store — the caller runs at boot and must not block startup.
    """
    try:
        raw = store_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[cron-tz] {store_path} is not valid JSON — leaving it untouched")
        return 0
    if not isinstance(data, dict):
        return 0
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return 0

    changed = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        schedule = job.get("schedule")
        if not isinstance(schedule, dict) or schedule.get("kind") != "cron":
            continue
        tz = schedule.get("tz")
        if tz is not None and not _is_utc_alias(tz):
            continue
        schedule["tz"] = target_timezone
        state = job.get("state")
        if isinstance(state, dict):
            # Recomputed by CronService.start() from the corrected zone.
            state["nextRunAtMs"] = None
        changed += 1
        print(
            f"[cron-tz] job {job.get('id')!r} ({job.get('name')!r}): "
            f"{tz or 'no timezone'} -> {target_timezone}"
        )

    if not changed:
        return 0

    temporary = store_path.with_suffix(store_path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(store_path)
    except OSError as exc:
        print(f"[cron-tz] could not rewrite {store_path}: {exc}")
        try:
            temporary.unlink()
        except OSError:
            pass
        return 0
    print(f"[cron-tz] repointed {changed} job(s) to {target_timezone}")
    return changed


def main(argv: list[str]) -> int:
    store_path = Path(argv[1]).expanduser() if len(argv) > 1 else default_store_path()
    migrate(store_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
