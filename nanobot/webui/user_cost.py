"""Per-user cost meter for the WebUI overview surface.

WHY THIS IS NOT ``token_usage``.

``nanobot.webui.token_usage`` counts tokens for the workspace as a whole: its rows
carry a ``source`` (user / api / cron / dream / system) and no identity at all, so
the numbers it shows are a property of the deployment rather than of the person
reading them. A user-facing meter has the opposite requirement — each person must
see their own calls and nobody else's — so identity is the key here, and the two
stores cannot be the same file.

WHAT IS COUNTED, AND WHY THESE NAMES.

``api_calls`` is the number of requests that reached the configured model. It is
the expensive one and everything else on the meter is local work done between
those requests, which is exactly the ratio the cost discipline lives on: one
model call should drive many commands. ``commands`` and ``pages`` are decided by
the same classifier the live feed uses (:mod:`nanobot.agent.activity`), so a
shell command a user watched run is a command in their meter and not a second,
differently-computed thing that disagrees with the feed next to it. ``files`` is
the file-edit tracker's own path list, which is what the thread's file rows are
built from. ``steps`` is every tool call and is kept as the denominator, so no
call can leave the meter by falling outside the four names.

A broker trade is neither a command nor a page, so it is deliberately counted in
``steps`` only rather than folded into ``commands`` to make a number look fuller.

WHAT THIS STORE CANNOT SAY.

It counts what the gateway saw. A turn that was stopped before its run hook
flushed (a billing stop) contributes nothing, and a call that never left this
process is not in it. Absent is not zero: :func:`user_cost_payload` answers
``metered: false`` when it has no identity to read rather than reporting a row of
zeros for a user it could not name.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

from nanobot.config.paths import get_webui_dir

USER_COST_SCHEMA_VERSION = 1
_MAX_STATE_FILE_BYTES = 1024 * 1024
_MAX_USERS_RETAINED = 5000
_MAX_DAYS_PER_USER = 400
_MAX_USER_ID_CHARS = 128
#: The counters, in the order the meter prints them. ``steps`` last because it is
#: the denominator and reads as one.
COUNTER_KEYS = ("turns", "api_calls", "commands", "pages", "files", "steps")
#: Which surface a turn arrived on. Kept so a user can tell a WebUI chat from an
#: API key, and so an unnamed scope cannot silently merge into a named one.
_SCOPE_KEYS = ("webui", "api", "telegram", "cli", "other")
_WRITE_LOCK = threading.Lock()


def user_cost_state_path() -> Path:
    return get_webui_dir() / "user-cost.json"


def default_user_cost_state() -> dict[str, Any]:
    return {
        "schema_version": USER_COST_SCHEMA_VERSION,
        "users": {},
        "updated_at": None,
    }


def clean_user_id(user_id: str | None) -> str:
    """Normalize a user id, or return ``""`` when there is nothing to key on.

    Accepts only the shape a Supabase id actually has. A stray value with a
    newline or a path separator in it would become a JSON key nothing else can
    look up, and a store keyed by an unlookupable id is worse than no store.
    """
    value = (user_id or "").strip()
    if not value or len(value) > _MAX_USER_ID_CHARS:
        return ""
    if not all(char.isalnum() or char in "-_.:" for char in value):
        return ""
    return value


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _zone(timezone_name: str | None) -> timezone | ZoneInfo:
    if not timezone_name:
        return timezone.utc
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _local_day(now: datetime | None = None, *, timezone_name: str | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_zone(timezone_name)).date().isoformat()


def _clean_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _clean_scope(value: str | None) -> str:
    return value if value in _SCOPE_KEYS else "other"


def _empty_counters() -> dict[str, int]:
    return {key: 0 for key in COUNTER_KEYS}


def _normalize_counters(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return _empty_counters()
    row = cast(dict[str, Any], raw)
    return {key: _clean_int(row.get(key)) for key in COUNTER_KEYS}


def _add_counters(target: dict[str, int], delta: Mapping[str, int]) -> None:
    for key in COUNTER_KEYS:
        target[key] = _clean_int(target.get(key)) + _clean_int(delta.get(key))


def _is_zero(counters: Mapping[str, int]) -> bool:
    return all(_clean_int(counters.get(key)) == 0 for key in COUNTER_KEYS)


def _normalize_scopes(raw: Any) -> dict[str, dict[str, int]]:
    scopes: dict[str, dict[str, int]] = {}
    if not isinstance(raw, dict):
        return scopes
    for scope, value in cast(dict[Any, Any], raw).items():
        counters = _normalize_counters(value)
        if _is_zero(counters):
            continue
        key = _clean_scope(str(scope))
        existing = scopes.setdefault(key, _empty_counters())
        _add_counters(existing, counters)
    return scopes


def _normalize_user_row(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    row = cast(dict[str, Any], raw)
    totals = _normalize_counters(row.get("totals"))
    days: dict[str, dict[str, int]] = {}
    raw_days = row.get("days")
    if isinstance(raw_days, dict):
        for date, value in sorted(cast(dict[Any, Any], raw_days).items())[-_MAX_DAYS_PER_USER:]:
            if not isinstance(date, str) or len(date) != 10:
                continue
            try:
                datetime.fromisoformat(date)
            except ValueError:
                # A hand-edited or foreign day key that is not a real date would
                # otherwise reach user_cost_payload's date arithmetic and fail
                # every settings request until the file was fixed by hand; drop
                # it like any other malformed row.
                continue
            counters = _normalize_counters(value)
            if _is_zero(counters):
                continue
            days[date] = counters
    scopes = _normalize_scopes(row.get("scopes"))
    if _is_zero(totals) and not days and not scopes:
        return None
    first_seen = row.get("first_seen")
    updated_at = row.get("updated_at")
    return {
        "first_seen": first_seen if isinstance(first_seen, str) else None,
        "updated_at": updated_at if isinstance(updated_at, str) else None,
        "totals": totals,
        "days": days,
        "scopes": scopes,
    }


def normalize_user_cost_state(raw: Any) -> dict[str, Any]:
    state = default_user_cost_state()
    if not isinstance(raw, dict):
        return state
    users_raw = cast(dict[str, Any], raw).get("users")
    if not isinstance(users_raw, dict):
        return state

    users: dict[str, Any] = {}
    for user_id, row_value in sorted(cast(dict[Any, Any], users_raw).items()):
        key = clean_user_id(str(user_id))
        if not key:
            continue
        row = _normalize_user_row(row_value)
        if row is not None:
            users[key] = row
    if len(users) > _MAX_USERS_RETAINED:
        ordered = sorted(
            users.items(),
            key=lambda item: (item[1].get("updated_at") or "", item[0]),
        )
        users = dict(ordered[-_MAX_USERS_RETAINED:])
    state["users"] = users
    updated_at = cast(dict[str, Any], raw).get("updated_at")
    state["updated_at"] = updated_at if isinstance(updated_at, str) else None
    return state


def read_user_cost_state() -> dict[str, Any]:
    path = user_cost_state_path()
    if not path.is_file():
        return default_user_cost_state()
    try:
        if path.stat().st_size > _MAX_STATE_FILE_BYTES:
            logger.warning("user cost state too large, ignoring: {}", path)
            return default_user_cost_state()
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("read user cost state failed {}: {}", path, e)
        return default_user_cost_state()
    return normalize_user_cost_state(raw)


def write_user_cost_state(raw: dict[str, Any]) -> dict[str, Any]:
    state = normalize_user_cost_state(raw)
    state["updated_at"] = _utc_now_iso()
    encoded = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    if len(encoded) > _MAX_STATE_FILE_BYTES:
        raise ValueError("user cost state is too large")

    path = user_cost_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "wb") as f:
        f.write(encoded)
        f.write(b"\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return state
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return state


def record_user_cost(
    user_id: str | None,
    counters: Mapping[str, Any] | None,
    *,
    scope: str = "webui",
    timezone_name: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Add one turn's counters to *user_id*'s row.

    Does nothing without a usable user id, and nothing for an all-zero delta, so
    a turn that did nothing at all cannot create a row that reads as a user who
    exists. Never raises on a malformed delta: a meter that can break a turn is
    worse than a meter that is one row behind.
    """
    key = clean_user_id(user_id)
    delta = _normalize_counters(counters)
    if not key or _is_zero(delta):
        return read_user_cost_state()

    day = _local_day(now, timezone_name=timezone_name)
    scope_key = _clean_scope(scope)
    now_iso = _utc_now_iso()

    with _WRITE_LOCK:
        state = read_user_cost_state()
        users = cast(dict[str, dict[str, Any]], state["users"])
        row = users.get(key)
        if row is None:
            row = {
                "first_seen": now_iso,
                "updated_at": now_iso,
                "totals": _empty_counters(),
                "days": {},
                "scopes": {},
            }
        _add_counters(cast(dict[str, int], row["totals"]), delta)

        days = cast(dict[str, dict[str, int]], row["days"])
        day_row = days.setdefault(day, _empty_counters())
        _add_counters(day_row, delta)
        if len(days) > _MAX_DAYS_PER_USER:
            row["days"] = {date: days[date] for date in sorted(days)[-_MAX_DAYS_PER_USER:]}

        scopes = cast(dict[str, dict[str, int]], row["scopes"])
        scope_row = scopes.setdefault(scope_key, _empty_counters())
        _add_counters(scope_row, delta)

        row["updated_at"] = now_iso
        users[key] = row
        return write_user_cost_state(state)


def _window_totals(days: Mapping[str, Mapping[str, int]], start: str, end: str) -> dict[str, int]:
    totals = _empty_counters()
    for date, counters in days.items():
        if start <= date <= end:
            _add_counters(totals, counters)
    return totals


def user_cost_payload(
    user_id: str | None,
    *,
    days: int = 30,
    timezone_name: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the requesting user's own meter.

    Read by key, so this function has no way to return another user's row: an id
    it does not recognise comes back as the empty payload below and not as the
    nearest match. ``metered`` states whether there was an identity to read at
    all, because "no meter for you" and "a meter reading zero" are different
    things and only one of them is true for a caller we could not name.
    """
    empty = _empty_counters()
    payload: dict[str, Any] = {
        "metered": False,
        "days": [],
        "today": dict(empty),
        "totals": dict(empty),
        "window": dict(empty),
        "window_days": 0,
        "scopes": {},
        "efficiency": {"commands_per_api_call": None, "local_steps_per_api_call": None},
        "first_seen": None,
        "updated_at": None,
    }
    key = clean_user_id(user_id)
    if not key:
        return payload

    state = read_user_cost_state()
    row = cast(dict[str, dict[str, Any]], state["users"]).get(key)
    if row is None:
        payload["metered"] = True
        return payload

    today = datetime.fromisoformat(_local_day(now, timezone_name=timezone_name)).date()
    window_days = max(1, int(days))
    start = today - timedelta(days=window_days - 1)
    stored_days = cast(dict[str, dict[str, int]], row["days"])
    day_rows = [
        {"date": date, **counters}
        for date, counters in sorted(stored_days.items())
        if start.isoformat() <= date <= today.isoformat()
    ]
    totals = _normalize_counters(row["totals"])
    window = _window_totals(stored_days, start.isoformat(), today.isoformat())

    api_calls = _clean_int(totals["api_calls"])
    # None rather than 0 or infinity when nothing hit the model: a ratio against
    # zero calls is not a number, and printing one would invent a figure.
    commands_per_call = (
        round(_clean_int(totals["commands"]) / api_calls, 2) if api_calls > 0 else None
    )
    local_steps = _clean_int(totals["commands"]) + _clean_int(totals["pages"]) + _clean_int(
        totals["files"]
    )
    local_per_call = round(local_steps / api_calls, 2) if api_calls > 0 else None

    payload.update(
        {
            "metered": True,
            "days": day_rows,
            "today": dict(stored_days.get(today.isoformat()) or empty),
            "totals": totals,
            "window": window,
            "window_days": window_days,
            "scopes": {
                scope: _normalize_counters(counters)
                for scope, counters in cast(dict[str, dict[str, int]], row["scopes"]).items()
            },
            "efficiency": {
                "commands_per_api_call": commands_per_call,
                "local_steps_per_api_call": local_per_call,
            },
            "first_seen": row.get("first_seen"),
            "updated_at": row.get("updated_at"),
        }
    )
    return payload


__all__ = [
    "COUNTER_KEYS",
    "USER_COST_SCHEMA_VERSION",
    "clean_user_id",
    "default_user_cost_state",
    "normalize_user_cost_state",
    "read_user_cost_state",
    "record_user_cost",
    "user_cost_payload",
    "user_cost_state_path",
    "write_user_cost_state",
]
