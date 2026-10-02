"""Backend timezone detection for automatic agent defaults."""

import os
from zoneinfo import ZoneInfo

from tzlocal import get_localzone_name

#: The zone this deployment interprets wall-clock time in: cron expressions,
#: naive ISO timestamps, and the day boundaries in usage/summaries.
#:
#: This matters because a schedule is stored as a *wall-clock* expression plus a
#: zone. The container this runs in reports UTC while the owner works in West
#: Africa Time (UTC+1), so every schedule that did not carry an explicit zone —
#: and every one silently stamped with the old hard-coded "UTC" default — fired
#: an hour late. The zone is a deployment decision, not something the image can
#: detect, so it is pinned here and overridable with NANOBOT_DEFAULT_TIMEZONE.
DEFAULT_TIMEZONE = (os.environ.get("NANOBOT_DEFAULT_TIMEZONE") or "").strip() or "Africa/Lagos"

#: Lower-cased on purpose: these are compared against hand-typed values from
#: stored cron jobs and config files, not against canonical IANA spelling.
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


def is_utc_alias(timezone: str | None) -> bool:
    """Return whether *timezone* names UTC (or an equivalent alias)."""
    return isinstance(timezone, str) and timezone.strip().lower() in _UTC_ALIASES


def detect_system_timezone() -> str:
    """Return the host's IANA timezone, falling back safely to UTC."""
    try:
        timezone = get_localzone_name()
        ZoneInfo(timezone)
    except Exception:
        return "UTC"
    return "UTC" if is_utc_alias(timezone) else timezone


def resolve_default_timezone() -> str:
    """Return the zone this deployment should interpret wall-clock time in.

    A host that reports UTC is reporting "no opinion" — both because that is the
    tzlocal fallback and because every container on this platform is UTC — so
    the deployment default (Africa/Lagos) is used instead of silently adopting
    UTC. A host with a real, non-UTC zone still wins.
    """
    detected = detect_system_timezone()
    return DEFAULT_TIMEZONE if is_utc_alias(detected) else detected
