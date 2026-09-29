"""Run the sandbox tool contract on an administrator-configured Freestyle VM.

Freestyle (https://dash.freestyle.sh) boots full Linux VMs — hardware
virtualisation, not containers — and exposes them over one flat HTTPS REST API.
This backend talks to that API directly with ``aiohttp`` (no extra SDK
dependency), so a deployment gets the provider without a new requirement.

The endpoint surface used, verified live against the API:

* ``POST   {base}/v5/vms``                         → boot a VM (``slug``,
                                                      ``firewall``, ``snapshotId``,
                                                      ``maxRunTotalSeconds`` …)
* ``GET    {base}/v5/vms?slug=``                   → find this session's VM
* ``GET    {base}/v5/vms/{id}``                    → state + provisioned resources
* ``POST   {base}/v5/vms/{id}/start``              → resume a paused/stopped VM
* ``POST   {base}/v5/vms/{id}/snapshot``           → baselined disk snapshot
* ``POST   {base}/v5/vms/{id}/exec-await``         → run a command (≤300 s each)
* ``GET    {base}/v5/vms/{id}/fs/read?path=``      → read a file
* ``PUT    {base}/v5/vms/{id}/fs/write?path=``     → write a file
* ``DELETE {base}/v5/vms/{id}``                    → destroy the VM

Two provider facts shape everything below:

* **A single exec is capped at 300 s** (``timeoutMs`` 1–300000). Anything longer
  is launched detached inside the guest with its exit status written to a file,
  and this backend polls for it — so a 15-minute MT5 + Wine install is one
  ``run()`` call rather than a timeout.
* **Rotation is per worked API key, and a key is only extra capacity when it
  belongs to a different account.** New sessions are round-robined across the
  keys and an existing session stays pinned to the account whose disk holds its
  files, because a VM is addressed by a deterministic slug that is unique only
  *within* an account — rotating a live session would build a second, empty VM
  under the same slug elsewhere. Two keys on the SAME account buy nothing: they
  see the same VM list and share one quota. MEASURED live: the two keys
  configured for this deployment are one account (a VM created with the first
  was visible to, and deletable by, the second), which ``describe_lanes``
  surfaces as two lanes reporting the identical VM list. Check the admin Test
  button's per-lane counts before assuming rotation added headroom.
"""

from __future__ import annotations

import asyncio
import base64
import json
import posixpath
import re
import shlex
import time
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Callable
from urllib.parse import quote

import aiohttp

from loguru import logger

_MAX_COMMAND_CHARS = 12_000
_MAX_CONTENT_CHARS = 32 * 1024 * 1024
_MAX_RESULT_CHARS = 16_000
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
# The tool contract allows 900 s; a single guest exec allows 300 s. Longer
# requests go through the detached-run path below.
_MAX_TIMEOUT = 900
#: Provider ceiling for one ``exec-await`` call (``timeoutMs`` max 300000).
_EXEC_AWAIT_CEILING = 300
#: Leave a margin under the ceiling so the guest never kills the wrapper that
#: is writing the result file.
_EXEC_AWAIT_SYNC_BUDGET = 295
#: How often the detached-run poller checks for the exit-status file.
_DETACHED_POLL_SECONDS = 2.0

#: Freestyle VMs run as uid 1000 (``ubuntu``) in the stock image, so the
#: workspace lives in that account's home where no ``sudo`` is needed.
WORKSPACE = "/home/ubuntu/workspace"

DEFAULT_API_URL = "https://api.freestyle.sh"

#: Rotating a session's lane is only safe while the workspace that holds its
#: disk can be named, so a lane index is stored beside every session id.
MAX_API_KEYS = 20

#: Slug-safe names, because the slug is part of the provider's URL and its
#: uniqueness constraint is per account.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$")

#: Failures that mean "this lane's account cannot serve us", as opposed to
#: "this request was bad". Only the former may rotate to another lane: rotating
#: away from a bad CPU/memory pair or a malformed snapshot id would hide a real
#: configuration bug behind a working-looking retry.
_LANE_FATAL_MARKERS = (
    "limit_exceeded",
    "limit exceeded",
    "plan's limit",
    "capacity",
    "quota",
    "conflict",
    "invalid or unknown api key",
    "invalid_api_key",
    "unauthorized",
    "too many",
)

#: VM states that mean "this VM is not usable and must be recreated".
_DEAD_STATES = {"stopped_terminated", "deleted", "terminated", "error", "failed"}

#: Fetch allow-list used when the administrator sets none.
DEFAULT_FETCH_ALLOW_HOSTS = "onlyfiles.com,gofile.io,pypi.org,files.pythonhosted.org,github.com,raw.githubusercontent.com,deb.debian.org,archive.ubuntu.com,security.ubuntu.com,dl.winehq.org"


class FreestyleError(RuntimeError):
    """A Freestyle operation failed in a way the caller should surface."""


class FreestyleFileNotFoundError(FreestyleError):
    """The requested guest path does not exist."""


# ---------------------------------------------------------------------------
# Validators. Each one either returns an accepted value or raises ValueError;
# the admin save path turns that into a 400 with the message, which is how a
# typo'd key is caught at save time instead of at task time.
# ---------------------------------------------------------------------------


def validate_freestyle_api_key(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,255}", value):
        raise ValueError("Freestyle API key has an unexpected shape")
    return value


def parse_freestyle_api_keys(raw: Any) -> list[str]:
    """Split configured keys into ordered, de-duplicated rotation lanes.

    Order is preserved because the order is what the round-robin cursor walks;
    duplicates are dropped so a repeated key cannot masquerade as extra quota.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        parts = [str(item or "") for item in raw]
    else:
        parts = re.split(r"[\s,;]+", str(raw))
    keys: list[str] = []
    for part in parts:
        value = part.strip().strip("\"'")
        if value and value not in keys:
            keys.append(value)
    return keys


def validate_freestyle_api_keys(raw: Any) -> list[str]:
    keys = parse_freestyle_api_keys(raw)
    if len(keys) > MAX_API_KEYS:
        raise ValueError(f"Freestyle supports at most {MAX_API_KEYS} API keys")
    for key in keys:
        validate_freestyle_api_key(key)
    return keys


def validate_freestyle_api_url(raw: str) -> str:
    value = str(raw or "").strip().rstrip("/")
    if not value:
        return DEFAULT_API_URL
    if not value.startswith("https://"):
        raise ValueError("Freestyle API URL must be an https URL (for example https://api.freestyle.sh)")
    return value


def validate_freestyle_snapshot_id(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value):
        raise ValueError("Freestyle snapshot id has an unexpected shape")
    return value


def validate_freestyle_cpu_cores(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Freestyle CPU cores must be a whole number") from None
    if not 1 <= value <= 64:
        raise ValueError("Freestyle CPU cores must be between 1 and 64")
    return value


def validate_freestyle_memory_mb(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Freestyle memory must be a whole number of megabytes") from None
    if not 512 <= value <= 65_536:
        raise ValueError("Freestyle memory must be between 512 and 65536 MB")
    if value % 2:
        raise ValueError("Freestyle memory must be an even number of megabytes")
    return value


def validate_freestyle_disk_size_gb(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Freestyle disk size must be a whole number of GB") from None
    if not 0 <= value <= 500:
        raise ValueError("Freestyle disk size must be between 0 and 500 GB")
    return value


def validate_freestyle_max_duration_seconds(raw: Any) -> int:
    """Total lifetime budget for one VM, in seconds.

    Mapped onto the provider's ``maxRunTotalSeconds``: once the budget is spent
    the VM is paused and every later start is refused, so a sandbox cannot run
    forever however the agent behaves. -1 is rejected on purpose — the whole
    point of the setting is that there is a ceiling.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Freestyle session TTL must be a whole number of seconds") from None
    if not 60 <= value <= 2_592_000:
        raise ValueError("Freestyle session TTL must be between 60 and 2592000 seconds")
    return value


def validate_freestyle_tag(raw: str) -> str:
    value = str(raw or "").strip() or "powerx"
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", value):
        raise ValueError("Freestyle tag must be 1-60 chars of letters, digits, dot, dash or underscore")
    return value


def validate_freestyle_fetch_allow_hosts(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    for entry in value.split(","):
        host = entry.strip()
        if not re.fullmatch(r"(\*\.)?[A-Za-z0-9.-]{1,253}", host):
            raise ValueError(f"'{host}' is not a valid host pattern for the Freestyle fetch allow list")
    return value


class FreestyleRotationState:
    """In-memory default for the round-robin cursor and parked lanes.

    The sandbox tool injects the disk-backed store instead; this exists so the
    backend is usable (and testable) on its own.
    """

    def __init__(self) -> None:
        self._cursor = 0
        self._parked: dict[int, float] = {}

    def next_lane(self, count: int, *, skip: set[int] | None = None) -> int:
        if count <= 0:
            return 0
        blocked = set(skip or set()) | set(self._parked)
        start = self._cursor % count
        self._cursor = (self._cursor + 1) % count
        order = [(start + i) % count for i in range(count)]
        healthy = [lane for lane in order if lane not in blocked]
        return (healthy + order)[0]

    def park(self, lane: int, *, seconds: float = 900.0) -> None:
        self._parked[int(lane)] = time.monotonic() + float(seconds)

    def clear(self) -> None:
        self._parked.clear()

    def parked(self) -> set[int]:
        now = time.monotonic()
        self._parked = {lane: until for lane, until in self._parked.items() if until > now}
        return set(self._parked)


# ---------------------------------------------------------------------------
# Small shared helpers (the same contract tenki_backend implements).
# ---------------------------------------------------------------------------


def _truncate(text: str) -> str:
    return text[-_MAX_RESULT_CHARS:] if len(text) > _MAX_RESULT_CHARS else text


def _safe_path(raw: str, root: str = WORKSPACE) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("path is required")
    root_path = PurePosixPath(root).as_posix().rstrip("/") or "/"
    # The LLM frequently asks to "list /" to see the sandbox root. From its
    # perspective that means the workspace, not the VM's real filesystem root.
    # A bare "/" maps to the workspace; everything else outside stays rejected.
    if value == "/":
        return root_path
    candidate = value if value.startswith("/") else f"{root_path}/{value}"
    normalized = posixpath.normpath(candidate)
    if normalized != root_path and not normalized.startswith(root_path + "/"):
        raise ValueError("path must remain inside the workspace")
    return normalized


def _looks_like_missing_file(detail: str) -> bool:
    lowered = detail.lower()
    return "not found" in lowered or "no such file" in lowered or "does not exist" in lowered


def _detail(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text[:400] or type(exc).__name__


def _parse_df_kb(text: str) -> dict[str, int]:
    """Pull total/free megabytes out of ``df -Pk`` output (POSIX column order)."""
    for line in reversed(str(text or "").splitlines()):
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            total_kb = int(fields[1])
            free_kb = int(fields[3])
        except ValueError:
            continue
        return {"total_mb": total_kb // 1024, "free_mb": free_kb // 1024}
    return {}


def _is_lane_fatal(detail: str) -> bool:
    lowered = str(detail or "").lower()
    return any(marker in lowered for marker in _LANE_FATAL_MARKERS)


def freestyle_sandbox_name(session_key: str) -> str:
    """Deterministic, slug-safe VM slug for one session key.

    The slug must be unique within the account and is the only handle that
    survives a restart, so it is derived from the session key rather than
    generated: the next operation finds the same VM by name.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(session_key or "").lower()).strip("-")
    cleaned = cleaned[:40] or "session"
    return f"px-fs-{cleaned}"


class FreestyleExecutionBackend:
    """Async client implementing the shared sandbox contract against Freestyle."""

    def __init__(
        self,
        config: Any,
        *,
        sandbox_name: str = "powerx-session",
        lane_index: int | None = None,
        rotation: Any | None = None,
        on_lane_pinned: Callable[[int], None] | None = None,
    ) -> None:
        self.config = config
        self.api_url = validate_freestyle_api_url(str(getattr(config, "api_url", "") or ""))
        lanes = validate_freestyle_api_keys(getattr(config, "api_keys", None) or "")
        if not lanes:
            legacy = validate_freestyle_api_key(str(getattr(config, "api_key", "") or ""))
            lanes = [legacy] if legacy else []
        self.api_keys = lanes
        # The first lane is the primary, so anything reading ``api_key`` sees the
        # value it always did.
        self.api_key = lanes[0] if lanes else ""
        # ``None`` means "not decided yet": a brand-new session is assigned a
        # lane by round-robin, an existing session is PINNED to the lane whose
        # account holds its disk.
        self.lane_index = lane_index if lane_index is None else int(lane_index)
        self.rotation = rotation if rotation is not None else FreestyleRotationState()
        self.on_lane_pinned = on_lane_pinned
        self.snapshot_id = validate_freestyle_snapshot_id(
            str(getattr(config, "snapshot_id", "") or "")
        )
        self.cpu_cores = validate_freestyle_cpu_cores(getattr(config, "cpu_cores", 4) or 4)
        self.memory_mb = validate_freestyle_memory_mb(getattr(config, "memory_mb", 8192) or 8192)
        self.disk_size_gb = validate_freestyle_disk_size_gb(
            getattr(config, "disk_size_gb", 0) or 0
        )
        # Total run budget. Freestyle pauses the VM when it is spent and refuses
        # every later start, which is the provider's own backstop against a
        # sandbox that never dies.
        self.max_duration_seconds = validate_freestyle_max_duration_seconds(
            getattr(config, "max_duration_seconds", 3600) or 3600
        )
        self.tag = validate_freestyle_tag(str(getattr(config, "tag", "") or "powerx"))
        self.fetch_allow_hosts = validate_freestyle_fetch_allow_hosts(
            str(getattr(config, "fetch_allow_hosts", "") or "")
        )
        # Split on commas in BOTH branches: ``set("a,b")`` would iterate the
        # string and yield one entry per character, silently allowing nothing.
        allow_source = self.fetch_allow_hosts or DEFAULT_FETCH_ALLOW_HOSTS
        self._fetch_hosts: set[str] = {
            host.strip() for host in allow_source.split(",") if host.strip()
        }
        cleaned = str(sandbox_name or "")
        self.sandbox_name = cleaned if _NAME_RE.fullmatch(cleaned) else "powerx-session"
        self.workspace = WORKSPACE
        self.last_session_id: str = ""
        self.persist_workspace = bool(getattr(config, "persist_workspace", True))
        # The VM this backend most recently resolved, kept so one operation can
        # run several commands without re-resolving it each time.
        self._vm: dict[str, Any] | None = None

    # ------------------------------------------------------------- lane logic

    def _lane_count(self) -> int:
        return len(self.api_keys)

    def _key_for_lane(self, lane: int | None = None) -> str:
        if not self.api_keys:
            raise FreestyleError("Freestyle API key is not configured")
        index = self.lane_index if lane is None else int(lane)
        if index is None or not 0 <= index < len(self.api_keys):
            index = 0
        return self.api_keys[index]

    def _pin(self, lane: int) -> None:
        """Record the lane this session lives in, and persist it via the store.

        A VM's disk exists in exactly one account, so once a lane is known every
        later operation must go there.
        """
        index = max(0, int(lane))
        self.lane_index = index
        if self.api_keys and index < len(self.api_keys):
            self.api_key = self.api_keys[index]
        notify = self.on_lane_pinned
        if notify is not None:
            try:
                notify(index)
            except Exception:  # noqa: BLE001 - persistence is best effort
                logger.debug("could not persist the Freestyle lane pin")

    def _pick_lane(self, skip: set[int] | None = None) -> int:
        count = self._lane_count()
        if count <= 1:
            return 0
        return int(self.rotation.next_lane(count, skip=skip))

    def _park_lane(self, lane: int, reason: str) -> None:
        try:
            self.rotation.park(int(lane))
        except Exception:  # noqa: BLE001
            pass
        logger.warning("Freestyle lane {} parked after a lane-fatal failure: {}", lane, reason)

    # ------------------------------------------------------------- HTTP plane

    def _headers(self, lane: int | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key_for_lane(lane)}",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        lane: int | None = None,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        data: bytes | None = None,
        expect: tuple[int, ...] = (200, 201, 204),
        timeout: float = 120,
        raw: bool = False,
    ) -> Any:
        """One API call on one lane's key. Non-expected statuses raise.

        ``raw=True`` returns the response bytes, which is how ``fs/read``
        answers. Everything else returns decoded JSON (or ``{}`` when a
        successful response carries no body, as a DELETE and the writes do).
        """
        url = f"{self.api_url}{path}"
        headers = self._headers(lane)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        elif data is not None:
            headers["Content-Type"] = "application/octet-stream"
        try:
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))
        except Exception as exc:  # noqa: BLE001
            raise FreestyleError(f"could not start the HTTP client: {_detail(exc)}") from None
        try:
            async with session:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    data=data,
                ) as response:
                    status = int(response.status)
                    if raw and status in (200, 206):
                        return await response.read()
                    body = await response.text()
                    if status not in expect:
                        raise FreestyleError(
                            f"Freestyle {method} {path} returned {status}: {body[:400]}"
                        )
                    if not body.strip():
                        return {}
                    try:
                        return json.loads(body)
                    except ValueError:
                        return {"raw": body}
        except FreestyleError:
            raise
        except aiohttp.ClientError as exc:
            raise FreestyleError(f"Freestyle request failed: {_detail(exc)}") from None
        except asyncio.TimeoutError:
            raise FreestyleError(f"Freestyle {method} {path} timed out after {timeout:.0f}s") from None

    # --------------------------------------------------------- VM lifecycle

    async def _find_vm(self, lane: int | None = None) -> dict[str, Any] | None:
        """Resolve this session's VM by its deterministic slug, in one account."""
        payload = await self._request(
            "GET", "/v5/vms", lane=lane, params={"slug": self.sandbox_name}, timeout=60
        )
        for item in (payload or {}).get("vms") or []:
            if str(item.get("slug") or "") == self.sandbox_name:
                return dict(item)
        return None

    async def _state_is_dead(self, vm: dict[str, Any]) -> bool:
        return str(vm.get("state") or "").lower() in _DEAD_STATES

    async def _wait_running(
        self, vm: dict[str, Any], *, lane: int | None = None, timeout: float = 120
    ) -> dict[str, Any]:
        """Block until the VM answers ``running``.

        Freestyle restores from a snapshot, so a boot is usually sub-second; a
        cold first boot in a fresh account is the slow case, which is why there
        is a real budget here rather than one poll.
        """
        deadline = time.monotonic() + max(5.0, float(timeout))
        current = vm
        while True:
            if str(current.get("state") or "").lower() == "running":
                return current
            if await self._state_is_dead(current):
                raise FreestyleError(
                    f"Freestyle VM {current.get('id')} entered terminal state "
                    f"{current.get('state')!r}"
                )
            if time.monotonic() >= deadline:
                return current
            await asyncio.sleep(1.0)
            fetched = await self._request(
                "GET", f"/v5/vms/{current['id']}", lane=lane, timeout=60
            )
            current = dict(fetched or current)

    async def _create_vm(self, lane: int) -> dict[str, Any]:
        """Boot this session's VM in *lane*'s account."""
        rules: list[dict[str, Any]] = [
            # Outbound Internet: a VM gets nothing implicitly, and every install
            # (apt, pip, the MT5 bootstrap) needs egress.
            {"action": "allow", "source": {}, "destination": {"cidr": "0.0.0.0/0"}},
            {"action": "allow", "source": {}, "destination": {"cidr": "::/0"}},
        ]
        body: dict[str, Any] = {
            "slug": self.sandbox_name,
            "displayName": f"{self.tag}:{self.sandbox_name}"[:120],
            "firewall": {"rules": rules},
            "metadata": {"powerx": self.tag[:60]},
            # The provider's own ceiling on total run time: once the budget is
            # spent the VM pauses and every later start is refused, so an
            # abandoned sandbox cannot bill forever.
            "maxRunTotalSeconds": int(self.max_duration_seconds),
            # Never delete an *unused* VM on us: persistence is what lets the
            # next task reattach to the same disk. Reset is what destroys it.
            "autoDeleteSeconds": -1,
        }
        if self.snapshot_id:
            body["snapshotId"] = self.snapshot_id
        try:
            created = await self._request("POST", "/v5/vms", lane=lane, json_body=body, timeout=180)
        except FreestyleError as exc:
            detail = _detail(exc)
            if "conflict" in detail.lower():
                # The slug is taken: a previous VM still holds it. Adopt it
                # rather than fighting for the name.
                existing = await self._find_vm(lane)
                if existing is not None:
                    return existing
            raise
        return dict(created or {})

    async def _ensure_vm(self) -> tuple[dict[str, Any], int]:
        """Get or create this session's VM, pinned or rotating.

        Pinned (``lane_index`` set): resolve inside that one account and, at
        worst, recreate there. Rotation is never consulted — a recreated VM must
        land back in the account that holds the session's disk.
        """
        if not self._lane_count():
            raise FreestyleError("Freestyle API key is not configured")
        if self.lane_index is not None:
            lane = int(self.lane_index)
            vm = await self._find_vm(lane)
            if vm is not None and not await self._state_is_dead(vm):
                vm = await self._resume_if_needed(vm, lane)
                self.last_session_id = str(vm.get("id") or "")
                self._vm = vm
                return vm, lane
            vm = await self._create_vm(lane)
            vm = await self._wait_running(vm, lane=lane)
            self.last_session_id = str(vm.get("id") or "")
            self._pin(lane)
            self._vm = vm
            return vm, lane
        return await self._create_with_rotation()

    async def _resume_if_needed(self, vm: dict[str, Any], lane: int) -> dict[str, Any]:
        """Bring a paused/stopped VM back up. A running VM is left alone."""
        state = str(vm.get("state") or "").lower()
        if state == "running":
            return vm
        if state in ("starting", "pausing"):
            return await self._wait_running(vm, lane=lane)
        try:
            started = await self._request(
                "POST", f"/v5/vms/{vm['id']}/start", lane=lane, timeout=120
            )
            vm = dict(started or vm)
        except FreestyleError as exc:
            detail = _detail(exc)
            # A spent total-run budget is a 409 by design: the ceiling did its
            # job. Report it as such instead of looping on restarts.
            if "409" in detail or "budget" in detail.lower():
                raise FreestyleError(
                    f"Freestyle VM {vm.get('id')} has exhausted its "
                    f"{self.max_duration_seconds}s run budget and cannot be started again: "
                    f"{detail}. Reset the session to start a new VM."
                ) from None
            raise
        return await self._wait_running(vm, lane=lane)

    async def _create_with_rotation(self) -> tuple[dict[str, Any], int]:
        """Create a brand-new session, round-robining lanes until one accepts it."""
        count = self._lane_count()
        tried: set[int] = set()
        skip: set[int] = set()
        last = ""
        for _ in range(count):
            lane = self._pick_lane(skip)
            if lane in tried:
                break
            tried.add(lane)
            try:
                vm = await self._create_vm(lane)
            except FreestyleError as exc:
                last = _detail(exc)
                if not _is_lane_fatal(last):
                    raise
                self._park_lane(lane, last)
                skip.add(lane)
                continue
            vm = await self._wait_running(vm, lane=lane)
            self.last_session_id = str(vm.get("id") or "")
            self._pin(lane)
            self._vm = vm
            return vm, lane
        raise FreestyleError(
            f"no Freestyle lane could accept a new session (tried {len(tried)} of "
            f"{count}): {last}"
        )

    # ------------------------------------------------------------ exec plane

    async def _exec(self, command: str, *, lane: int, vm_id: str, timeout: int) -> dict[str, Any]:
        """One ``exec-await`` call, inside the provider's 300 s ceiling."""
        budget_ms = int(max(1, min(timeout, _EXEC_AWAIT_SYNC_BUDGET)) * 1000)
        return await self._request(
            "POST",
            f"/v5/vms/{vm_id}/exec-await",
            lane=lane,
            json_body={"command": command, "timeoutMs": budget_ms},
            timeout=budget_ms / 1000 + 30,
        )

    async def _exec_detached(self, command: str, *, lane: int, vm_id: str, timeout: int) -> dict[str, Any]:
        """Run a command longer than one ``exec-await`` allows.

        The guest keeps running a detached process when the exec request
        returns, so the command is launched with its exit status written to a
        marker file; this polls that file. The status file is written last and
        atomically (``mv``), so a partially-written status is never read as a
        result — the same failure mode a truncating redirect would create.
        """
        stamp = f"{int(time.time() * 1000)}-{id(command) % 100000}"
        log_path = f"/tmp/px-fs-run-{stamp}.log"
        code_path = f"/tmp/px-fs-run-{stamp}.code"
        wrapper = (
            f"cd {shlex.quote(self.workspace)} 2>/dev/null; "
            f"{{ {command} ; }} > {shlex.quote(log_path)} 2>&1; "
            f"rc=$?; printf '%s' \"$rc\" > {shlex.quote(code_path)}.part; "
            f"mv {shlex.quote(code_path)}.part {shlex.quote(code_path)}"
        )
        pid_payload = await self._exec(
            f"setsid sh -c {shlex.quote(wrapper)} >/dev/null 2>&1 & echo started",
            lane=lane,
            vm_id=vm_id,
            timeout=30,
        )
        if "started" not in str(pid_payload.get("stdout") or ""):
            # Fall back to a plain run rather than pretending it was detached.
            return await self._exec(command, lane=lane, vm_id=vm_id, timeout=timeout)
        deadline = time.monotonic() + max(5.0, float(timeout))
        while time.monotonic() < deadline:
            await asyncio.sleep(_DETACHED_POLL_SECONDS)
            probe = await self._exec(
                f"cat {shlex.quote(code_path)} 2>/dev/null || true",
                lane=lane,
                vm_id=vm_id,
                timeout=30,
            )
            code_text = str(probe.get("stdout") or "").strip()
            if code_text:
                log = await self._exec(
                    f"tail -c {_MAX_RESULT_CHARS} {shlex.quote(log_path)} 2>/dev/null || true",
                    lane=lane,
                    vm_id=vm_id,
                    timeout=60,
                )
                try:
                    code = int(code_text.splitlines()[-1])
                except (ValueError, IndexError):
                    code = 0
                await self._exec(
                    f"rm -f {shlex.quote(log_path)} {shlex.quote(code_path)}",
                    lane=lane,
                    vm_id=vm_id,
                    timeout=30,
                )
                return {"stdout": str(log.get("stdout") or ""), "stderr": "", "statusCode": code}
        return {
            "stdout": "",
            "stderr": f"command exceeded the {timeout}s budget",
            "statusCode": 124,
            "timedOut": True,
        }

    @staticmethod
    def _render(payload: dict[str, Any]) -> str:
        stdout = str(payload.get("stdout") or "")
        stderr = str(payload.get("stderr") or "")
        code = payload.get("statusCode")
        text = stdout
        if stderr:
            text += f"\n[stderr]\n{stderr}"
        if payload.get("timedOut"):
            text += "\n[timed_out=true]"
        if code is not None:
            # The marker is the ONLY way a caller can tell success from failure:
            # ``run`` never raises for a non-zero exit, and
            # ``workspace_bridge._exit_code`` reads the trailing marker to decide
            # whether a command succeeded. Emitting it only on failure made every
            # successful command look like a failure to the bridge, which is what
            # stopped the live-screen pump from ever fetching a frame. This is the
            # same contract ``vps_backend._output`` has always had.
            text += f"\n[exit_code={code}]"
        return _truncate(text) or "(no output)"

    async def run(self, command: str, *, timeout: int = 120) -> str:
        command = str(command or "").strip()
        if not command:
            raise ValueError("command is required")
        if len(command) > _MAX_COMMAND_CHARS:
            raise ValueError(f"command exceeds {_MAX_COMMAND_CHARS} characters")
        timeout = max(1, min(int(timeout), _MAX_TIMEOUT))
        vm, lane = await self._ensure_vm()
        vm_id = str(vm.get("id") or "")
        if timeout > _EXEC_AWAIT_SYNC_BUDGET:
            payload = await self._exec_detached(command, lane=lane, vm_id=vm_id, timeout=timeout)
        else:
            payload = await self._exec(command, lane=lane, vm_id=vm_id, timeout=timeout)
        return self._render(payload)

    # ------------------------------------------------------------ file plane

    async def read(self, path: str) -> str:
        target = _safe_path(path, self.workspace)
        vm, lane = await self._ensure_vm()
        vm_id = str(vm.get("id") or "")
        try:
            raw = await self._request(
                "GET",
                f"/v5/vms/{vm_id}/fs/read",
                lane=lane,
                params={"path": target},
                raw=True,
                timeout=120,
            )
        except FreestyleError as exc:
            if _looks_like_missing_file(_detail(exc)):
                return ""
            # Fall back to a shell read for paths the file API declines.
            with_suppress = await self._exec(
                f"base64 {shlex.quote(target)} 2>/dev/null", lane=lane, vm_id=vm_id, timeout=90
            )
            payload = str(with_suppress.get("stdout") or "").strip()
            if not payload:
                return ""
            try:
                raw = base64.b64decode(payload, validate=False)
            except Exception:  # noqa: BLE001
                return _truncate(payload)
        if not raw:
            return ""
        try:
            return _truncate(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return _truncate(base64.b64encode(raw).decode("ascii"))

    async def _mkdir(self, lane: int, vm_id: str, target: str) -> None:
        parent = posixpath.dirname(target)
        if not parent or parent == "/":
            return
        await self._exec(
            f"mkdir -p {shlex.quote(parent)}", lane=lane, vm_id=vm_id, timeout=60
        )

    async def write(self, path: str, content: str) -> None:
        target = _safe_path(path, self.workspace)
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError(f"content exceeds {_MAX_CONTENT_CHARS} characters")
        vm, lane = await self._ensure_vm()
        vm_id = str(vm.get("id") or "")
        await self._mkdir(lane, vm_id, target)
        await self._request(
            "PUT",
            f"/v5/vms/{vm_id}/fs/write",
            lane=lane,
            params={"path": target},
            data=content.encode("utf-8"),
            timeout=180,
        )

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = _safe_path(path, self.workspace)
        if len(data) > _MAX_UPLOAD_BYTES:
            raise ValueError("file exceeds 200 MiB")
        vm, lane = await self._ensure_vm()
        vm_id = str(vm.get("id") or "")
        await self._mkdir(lane, vm_id, target)
        try:
            # Raw bytes on the same endpoint: the JSON form is capped at 32 MiB.
            await self._request(
                "PUT",
                f"/v5/vms/{vm_id}/fs/write",
                lane=lane,
                params={"path": target},
                data=data,
                timeout=300,
            )
        except FreestyleError as exc:
            # Fallback through the exec plane for payloads the file API rejects.
            b64 = base64.b64encode(data).decode("ascii")
            result = await self._exec(
                f"mkdir -p {shlex.quote(posixpath.dirname(target) or '/tmp')}; "
                f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(target)}",
                lane=lane,
                vm_id=vm_id,
                timeout=_EXEC_AWAIT_SYNC_BUDGET,
            )
            if int(result.get("statusCode") or 0) != 0:
                raise FreestyleError(_detail(exc)) from None

    async def list(self, path: str) -> str:
        target = _safe_path(path or self.workspace, self.workspace)
        listing_root = target if target == self.workspace else (posixpath.dirname(target) or self.workspace)
        command = (
            f"find {shlex.quote(listing_root)} -maxdepth 2 -printf '%y %p\\n' 2>/dev/null | head -200"
        )
        return await self.run(command, timeout=60)

    async def upload(self, local_path: Any, remote_path: str) -> str:
        """Copy a local file into the VM."""
        source = Path(str(local_path)).expanduser()
        if not source.is_file():
            raise FreestyleFileNotFoundError(f"local file not found: {source}")
        payload = source.read_bytes()
        target = _safe_path(remote_path, self.workspace)
        await self.write_bytes(target, payload)
        return target

    async def download(self, remote_path: str, local_path: Any) -> Any:
        """Download a VM file to a local path."""
        target = _safe_path(remote_path, self.workspace)
        destination = Path(str(local_path)).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        vm, lane = await self._ensure_vm()
        vm_id = str(vm.get("id") or "")
        try:
            raw = await self._request(
                "GET",
                f"/v5/vms/{vm_id}/fs/read",
                lane=lane,
                params={"path": target},
                raw=True,
                timeout=300,
            )
        except FreestyleError as exc:
            if _looks_like_missing_file(_detail(exc)):
                raise FreestyleFileNotFoundError(_detail(exc)) from None
            raise
        data = bytes(raw or b"")
        if len(data) > _MAX_DOWNLOAD_BYTES:
            raise FreestyleError(
                f"file exceeds the {_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MiB download limit"
            )
        destination.write_bytes(data)
        return destination

    def _is_host_allowed(self, host: str) -> bool:
        if "*" in self._fetch_hosts:
            return True
        if host in self._fetch_hosts:
            return True
        return any(
            host.endswith("." + pattern[2:])
            for pattern in self._fetch_hosts
            if pattern.startswith("*.")
        )

    async def fetch_url(self, url: str, dest_path: str, *, timeout: int = 150) -> str:
        """Fetch an allowed HTTPS URL *inside* the VM, not on the agent host."""
        from urllib.parse import urlparse

        parsed = urlparse(str(url or "").strip())
        if parsed.scheme != "https":
            raise ValueError("only https URLs can be fetched")
        host = (parsed.hostname or "").lower()
        if not host or not self._is_host_allowed(host):
            raise ValueError(f"host '{host}' is not in the Freestyle fetch allow list")
        target = _safe_path(dest_path or f"downloads/{posixpath.basename(parsed.path) or 'file'}")
        command = (
            f"mkdir -p {shlex.quote(posixpath.dirname(target))} && "
            f"curl -fsSL --retry 2 --connect-timeout 20 -o {shlex.quote(target)}.part {shlex.quote(url)} && "
            f"mv {shlex.quote(target)}.part {shlex.quote(target)} && "
            f"ls -l {shlex.quote(target)}"
        )
        return await self.run(command, timeout=timeout)

    async def install_packages(self, packages: list[str], *, timeout: int = 600) -> str:
        cleaned = [
            item
            for item in packages
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.:@~=-]{0,127}", item)
        ]
        if not cleaned:
            raise ValueError("no valid package names supplied")
        quoted = " ".join(shlex.quote(item) for item in cleaned)
        command = (
            "export DEBIAN_FRONTEND=noninteractive; "
            "if command -v sudo >/dev/null 2>&1; then SUDO='sudo -n'; else SUDO=''; fi; "
            "if command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "
            + quoted
            + "; elif command -v apk >/dev/null 2>&1; then $SUDO apk add --no-cache "
            + quoted
            + "; elif command -v dnf >/dev/null 2>&1; then $SUDO dnf install -y "
            + quoted
            + "; else echo 'no supported package manager found' >&2; exit 127; fi"
        )
        return await self.run(command, timeout=min(timeout, _MAX_TIMEOUT))

    async def keep_alive(self, session_id: str | None = None) -> None:
        """Renew the session: start the VM if it has paused or stopped.

        Freestyle pauses an idle VM but never deletes it, so "keep alive" here
        means "make sure it is running for the next operation" rather than
        "extend a deadline".
        """
        lane = self.lane_index
        vm: dict[str, Any] | None = None
        if session_id:
            for candidate in range(self._lane_count()):
                try:
                    fetched = await self._request(
                        "GET", f"/v5/vms/{session_id}", lane=lane if lane is not None else candidate
                    )
                    vm = dict(fetched or {})
                    break
                except FreestyleError:
                    continue
        if vm is None:
            try:
                vm = await self._find_vm(lane)
            except FreestyleError:
                vm = None
        if vm is None or await self._state_is_dead(vm):
            return
        with_suppress = str(vm.get("state") or "").lower()
        if with_suppress == "running":
            return
        try:
            await self._resume_if_needed(vm, lane if lane is not None else 0)
        except FreestyleError as exc:  # noqa: BLE001 - best effort by contract
            logger.warning("could not keep the Freestyle VM alive: {}", _detail(exc))

    async def snapshot_workspace(self, name: str | None = None) -> str:
        """Baseline the VM's current disk/memory as a snapshot, and return its id."""
        vm, lane = await self._ensure_vm()
        vm_id = str(vm.get("id") or "")
        body = {"displayName": (name or f"{self.tag}-snapshot")[:120]}
        created = await self._request(
            "POST", f"/v5/vms/{vm_id}/snapshot", lane=lane, json_body=body, timeout=300
        )
        payload = dict(created or {})
        return str(payload.get("id") or payload.get("snapshotId") or "")

    async def reset(self, session_id: str | None = None) -> None:
        """Destroy the session's VM permanently (explicit wipe).

        Only an existing VM is destroyed — reset must never create one.
        """
        lane = self.lane_index
        vm: dict[str, Any] | None = None
        if session_id:
            for candidate in range(max(1, self._lane_count())):
                try:
                    fetched = await self._request(
                        "GET", f"/v5/vms/{session_id}", lane=lane if lane is not None else candidate
                    )
                    vm = dict(fetched or {})
                    break
                except FreestyleError:
                    continue
        if vm is None and lane is not None:
            try:
                vm = await self._find_vm(lane)
            except FreestyleError:
                vm = None
        if vm is not None:
            try:
                await self._request(
                    "DELETE", f"/v5/vms/{vm['id']}", lane=lane, timeout=120, expect=(200, 202, 204, 404)
                )
            except FreestyleError as exc:  # noqa: BLE001
                logger.warning("could not destroy the Freestyle VM: {}", _detail(exc))
        self.last_session_id = ""
        self._vm = None

    # ------------------------------------------------------------- diagnostics

    async def _lane_facts(self, lane: int) -> dict[str, Any]:
        """Account-level headroom for one lane, for the admin Test button.

        Best effort: the Test button must still show the lanes it could read.
        """
        facts: dict[str, Any] = {}
        try:
            listing = await self._request("GET", "/v5/vms", lane=lane, timeout=60)
            payload = dict(listing or {})
            facts["vm_count"] = payload.get("totalCount")
            facts["running_vms"] = payload.get("runningCount")
        except Exception as exc:  # noqa: BLE001
            facts["error"] = _detail(exc)
        return facts

    async def describe_lanes(self) -> list[dict[str, Any]]:
        """One row per configured lane: which account, and what it is running.

        This is the evidence that rotation spreads load over *distinct*
        accounts: a repeated key would report the same VM count twice and buy no
        extra capacity however many times it were listed.
        """
        rows: list[dict[str, Any]] = []
        for lane in range(self._lane_count()):
            row: dict[str, Any] = {"lane": lane}
            row.update(await self._lane_facts(lane))
            rows.append(row)
        return rows

    async def test_connection(self) -> dict[str, Any]:
        vm, lane = await self._ensure_vm()
        vm_id = str(vm.get("id") or "")
        probe = await self._exec(
            "uname -a; free -m | head -2; nproc; df -Pk / | tail -1",
            lane=lane,
            vm_id=vm_id,
            timeout=60,
        )
        resources = dict(vm.get("resources") or {})
        payload: dict[str, Any] = {
            "ok": int(probe.get("statusCode") or 0) == 0,
            "backend": "freestyle",
            "session_id": vm_id,
            "platform": str(probe.get("stdout") or "").splitlines()[:1] and
                        str(probe.get("stdout") or "").splitlines()[0][:200],
            "state": str(vm.get("state") or ""),
            "cpu_cores": resources.get("cpu"),
            "memory_mb": resources.get("memory"),
            "disk_size_gb": resources.get("storage", 0) // 1024 if resources.get("storage") else 0,
            "lane_index": lane,
            "lane_count": self._lane_count(),
        }
        payload.update(_parse_df_kb(str(probe.get("stdout") or "")))
        return payload


__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_FETCH_ALLOW_HOSTS",
    "WORKSPACE",
    "FreestyleError",
    "FreestyleExecutionBackend",
    "FreestyleFileNotFoundError",
    "FreestyleRotationState",
    "MAX_API_KEYS",
    "freestyle_sandbox_name",
    "parse_freestyle_api_keys",
    "validate_freestyle_api_key",
    "validate_freestyle_api_keys",
    "validate_freestyle_api_url",
    "validate_freestyle_cpu_cores",
    "validate_freestyle_disk_size_gb",
    "validate_freestyle_fetch_allow_hosts",
    "validate_freestyle_max_duration_seconds",
    "validate_freestyle_memory_mb",
    "validate_freestyle_snapshot_id",
    "validate_freestyle_tag",
]
