"""Run the sandbox tool contract on an administrator-configured Tenki Sandbox.

Tenki (https://tenki.cloud) provides disposable full Linux VMs for coding agents
and untrusted code. Unlike the Daytona/Runloop/Vercel backends (which speak each
vendor's REST API over ``aiohttp``) Tenki's control plane is raw gRPC, so this
backend drives the vendor's own Python SDK instead of re-implementing protobuf
framing by hand. Everything else mirrors the shared sandbox contract exactly.

SDK surface used (``tenki_sandbox.AsyncClient`` / ``AsyncSandbox``):
* ``AsyncClient(auth_token=…, base_url=…)``      → workspace-scoped client
* ``client.create(name=…, cpu_cores=…, memory_mb=…, disk_size_gb=…,
   max_duration=…, sticky=…, tags=…, metadata=…, wait=True)`` → new session
* ``client.get(session_id)``                    → reattach to a live session
* ``client.list(tags=…, include_terminated=False)`` → resolve a session by name
* ``sandbox.wait_ready(timeout)``               → block until ``RUNNING``
* ``sandbox.shell(command, timeout=…)``         → run a command through bash -lc
* ``sandbox.fs.read_text/write_text/write_bytes/read_bytes/stat/mkdir``
* ``sandbox.fs.download(remote, local)``        → stream a file to the host
* ``sandbox.extend(seconds)``                   → push the deadline out
* ``sandbox.close()``                           → terminate (permanent)

Lifecycle: user sessions map to a deterministic session name so a VM survives
agent restarts. ``max_duration`` (seconds) is the TTL — the default is one hour
and it is administrator-configurable — and Tenki's own deadline reaps the VM if
cleanup is ever missed.

Resource limits are enforced by the *workspace*, not by the SDK: this tenant's
ceiling was measured at 4096 MB of RAM, so ``memory_mb`` defaults to 4096 and a
larger value is rejected server-side with
``InvalidStateError: requested resources exceed workspace limits``. Raise the
workspace quota before raising the default.
"""

from __future__ import annotations

import base64
import hashlib
import posixpath
import re
import shlex
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from loguru import logger

_MAX_COMMAND_CHARS = 12_000
_MAX_CONTENT_CHARS = 120_000
_MAX_RESULT_CHARS = 16_000
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
_MAX_TIMEOUT = 900

# Tenki sessions run as the unprivileged ``tenki`` account (uid 1000) with
# ``/home/tenki`` as its home directory — verified live against the SDK.
WORKSPACE = "/home/tenki"

DEFAULT_API_URL = "https://api.tenki.cloud"

# Session names are used verbatim by the control plane; keep them to a DNS-label
# safe alphabet so name-derived routing never trips a server-side validator.
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")

# Bounds accepted by the SDK's own ``validate_create_resources``. The workspace
# quota (not these numbers) is what actually caps a create.
_MIN_CPU_CORES = 1
_MAX_CPU_CORES = 128
_MIN_MEMORY_MB = 128
_MAX_MEMORY_MB = 524_288
_MIN_DISK_GB = 5
_MAX_DISK_GB = 100

# How many API keys (lanes) may be configured. Each key authenticates into its
# own Tenki workspace, and every workspace carries its own independent quota —
# measured live at ``max_concurrent_jobs = 5`` active sessions — so lanes are a
# capacity dial, not just a failover list.
MAX_API_KEYS = 20

# Failures that mean "this lane's key/workspace cannot serve us", as opposed to
# "this particular request was bad". Only the former may rotate to another lane:
# rotating away from a bad CPU/RAM pair or an invalid snapshot id would hide a
# real configuration bug behind a working-looking retry.
_LANE_FATAL_MARKERS = (
    "exceed workspace limits",
    "workspace limit",
    "max_concurrent",
    "too many active",
    "no active workers",
    "vm limit exceeded",
    "quota",
    "insufficient credit",
    "out of credit",
    "billing",
    "suspended",
    "unauthorized",
    "unauthenticated",
    "permission denied",
    "forbidden",
    "invalid api key",
    "invalid token",
    "authentication",
    "api key",
)


def _is_lane_fatal(detail: str) -> bool:
    """True when *detail* says the lane itself is unusable, not the request.

    Deliberately conservative: an unrecognised failure is treated as fatal to
    the *request* (it is re-raised as-is) rather than cycling every key, so a
    genuine bug surfaces as itself instead of as "all lanes failed".
    """
    lowered = detail.lower()
    return any(marker in lowered for marker in _LANE_FATAL_MARKERS)


def _identity_workspace_id(identity: Any) -> str:
    """The workspace a lane's key authenticates into, as a plain id string.

    Read from ``who_am_i()``. This is the evidence that two keys are genuinely
    two workspaces with two independent quotas rather than one workspace counted
    twice, so it is worth the round trip.
    """
    direct = str(getattr(identity, "owner_id", "") or "")
    if direct:
        return direct
    for entry in getattr(identity, "workspaces", None) or ():
        candidate = str(getattr(entry, "id", "") or "")
        if candidate:
            return candidate
    return ""


def _usage_limit(usage: Any, key: str) -> dict[str, int] | None:
    """Pull one ``WorkspaceUsageLimit`` out of ``get_usage()`` by its key."""
    entries = usage if isinstance(usage, (list, tuple)) else [usage]
    for entry in entries:
        if str(getattr(entry, "key", "") or "") != key:
            continue
        values: dict[str, int] = {}
        for field in ("current", "max"):
            try:
                values[field] = int(getattr(entry, field))
            except (TypeError, ValueError):
                continue
        return values or None
    return None

# States from which a session can still be used. Anything else means the VM is
# gone and must be recreated (Tenki has no resume-from-terminated path).
_DEAD_STATES = frozenset({"TERMINATED", "TERMINATING", "FAILED", "ERROR"})
_TERMINAL_STATES = _DEAD_STATES

# Default allowed hosts for ``fetch_url`` (kept in sync with the Daytona/Runloop
# backends so every cloud provider behaves identically for the agent).
DEFAULT_FETCH_ALLOW_HOSTS: tuple[str, ...] = (
    "onlyfiles.com",
    "gofile.io",
    "*.gofile.io",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
    "files.pythonhosted.org",
    "pypi.org",
    "registry.npmjs.org",
    "filebin.net",
    "0x0.st",
    "transfer.sh",
    "bashupload.com",
    "temp.sh",
    "pixeldrain.com",
    "*.pixeldrain.com",
    "catbox.moe",
    "litterbox.catbox.moe",
    "file.io",
    "cdn.jsdelivr.net",
    "unpkg.com",
    "esm.sh",
    "dl.google.com",
    "storage.googleapis.com",
    "registry.yarnpkg.com",
    "nodejs.org",
)


class TenkiError(RuntimeError):
    """Any non-recoverable Tenki platform or session failure."""


class TenkiFileNotFoundError(TenkiError):
    """The requested path does not exist in the session."""


def _load_async_client() -> Any:
    """Import the Tenki SDK lazily so the tool stays importable without it."""
    try:
        from tenki import AsyncClient  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on the deployment image
        raise TenkiError(
            "The Tenki SDK is not installed. Install it with `pip install tenki` "
            "in the nanobot environment, then retry."
        ) from exc
    return AsyncClient


def is_sdk_available() -> bool:
    """True when the Tenki SDK can be imported (used by the admin test path)."""
    try:
        _load_async_client()
    except TenkiError:
        return False
    return True


def validate_tenki_api_key(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 256 or not re.fullmatch(r"[A-Za-z0-9_\-\.]+", value):
        raise ValueError("Tenki API key format is invalid")
    if not value.startswith("tk_"):
        raise ValueError("Tenki API key must start with tk_")
    return value


def parse_tenki_api_keys(raw: Any) -> list[str]:
    """Split configured keys into ordered, de-duplicated lanes.

    Accepts a list/tuple or a single string separated by commas, semicolons,
    newlines or spaces, so an operator can paste keys into one deployment
    variable in whatever shape is convenient. Order is preserved because lane
    order is what the round-robin cursor walks; duplicates are dropped so a
    repeated key cannot masquerade as extra capacity.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        parts = [str(item or "") for item in raw]
    else:
        parts = re.split(r"[,;\s]+", str(raw or ""))
    keys: list[str] = []
    for part in parts:
        candidate = part.strip().strip("\"'").strip()
        if candidate and candidate not in keys:
            keys.append(candidate)
    return keys


def validate_tenki_api_keys(raw: Any) -> list[str]:
    """Validate every lane key, preserving order and dropping duplicates."""
    keys = parse_tenki_api_keys(raw)
    if len(keys) > MAX_API_KEYS:
        raise ValueError(f"Tenki supports at most {MAX_API_KEYS} API keys")
    for key in keys:
        validate_tenki_api_key(key)
    return keys


class TenkiRotationState:
    """In-memory default for the round-robin cursor and parked lanes.

    The sandbox tool injects a disk-backed implementation so rotation survives a
    restart; this one exists so the backend is usable (and testable) on its own.
    """

    def __init__(self) -> None:
        self._cursor = 0
        self._parked: dict[int, float] = {}

    def next_lane(self, count: int, *, skip: set[int] | None = None) -> int:
        """Return the next lane to try for a NEW session, advancing the cursor.

        Parked lanes are moved to the back rather than removed: when every lane
        is parked the rotation still returns a full order, because a stale
        cooldown must never become "no lane was tried at all".
        """
        if count <= 0:
            return 0
        skip = set(skip or set()) | set(self._parked)
        start = self._cursor % count
        self._cursor = (self._cursor + 1) % count
        order = [(start + i) % count for i in range(count)]
        healthy = [lane for lane in order if lane not in skip]
        return (healthy + order)[0]

    def park(self, lane: int, *, seconds: float = 900.0) -> None:
        """Stop offering *lane* first for a while after it failed on us."""
        self._parked[int(lane)] = time.monotonic() + float(seconds)

    def parked(self) -> set[int]:
        now = time.monotonic()
        self._parked = {lane: until for lane, until in self._parked.items() if until > now}
        return set(self._parked)

    def clear(self) -> None:
        self._parked.clear()


def validate_tenki_api_url(raw: str) -> str:
    value = str(raw or "").strip().rstrip("/") or DEFAULT_API_URL
    if len(value) > 253:
        raise ValueError("Tenki API URL is too long")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(
            "Tenki API URL must be an https URL (for example https://api.tenki.cloud)"
        )
    return value


def _validate_int(raw: Any, *, field: str, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Tenki {field} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"Tenki {field} must be between {minimum} and {maximum}")
    return value


def validate_tenki_cpu_cores(raw: Any) -> int:
    return _validate_int(
        raw, field="CPU cores", minimum=_MIN_CPU_CORES, maximum=_MAX_CPU_CORES
    )


def validate_tenki_memory_mb(raw: Any) -> int:
    value = _validate_int(
        raw, field="memory (MB)", minimum=_MIN_MEMORY_MB, maximum=_MAX_MEMORY_MB
    )
    if value % 2 != 0:
        raise ValueError("Tenki memory (MB) must be an even number of megabytes")
    return value


def validate_tenki_disk_size_gb(raw: Any) -> int:
    """0 means "let Tenki pick the source's default disk"; otherwise 5-100 GB."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Tenki disk size must be an integer number of GB") from None
    if value == 0:
        return 0
    if not _MIN_DISK_GB <= value <= _MAX_DISK_GB:
        raise ValueError(
            f"Tenki disk size must be 0 (provider default) or between "
            f"{_MIN_DISK_GB} and {_MAX_DISK_GB} GB"
        )
    return value


def validate_tenki_max_duration_seconds(raw: Any) -> int:
    """"The session TTL in seconds. One hour by default."""
    return _validate_int(
        raw, field="max duration (seconds)", minimum=60, maximum=604_800
    )


def validate_tenki_image(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 512 or not re.fullmatch(r"[A-Za-z0-9_\-\./:@]+", value):
        raise ValueError("Tenki image reference format is invalid")
    return value


def validate_tenki_snapshot_id(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9_\-\.]+", value):
        raise ValueError("Tenki snapshot ID format is invalid")
    return value


def validate_tenki_tag(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return ""
    if len(value) > 64 or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value):
        raise ValueError("Tenki tag format is invalid")
    return value


def validate_tenki_fetch_allow_hosts(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value == "*":
        return "*"
    parts = [p.strip() for p in value.split(",") if p.strip()]
    for p in parts:
        if p != "*" and not re.fullmatch(r"(\*\.)?[a-zA-Z0-9][-a-zA-Z0-9.]*[a-zA-Z0-9]", p):
            raise ValueError(f"Invalid host in fetch allowlist: {p!r}")
    return ",".join(parts)


def tenki_sandbox_name(session_key: str) -> str:
    """Deterministic, valid session name for a session so VMs survive restarts."""
    slug = re.sub(r"[^a-z0-9-]", "-", (session_key or "").lower()).strip("-")[:32] or "session"
    digest = hashlib.sha256((session_key or "session").encode("utf-8")).hexdigest()[:10]
    name = f"px-{slug}-{digest}".strip("-")
    while "--" in name:
        name = name.replace("--", "-")
    return name[:48] if _NAME_RE.fullmatch(name) else f"px-{digest}"


def _safe_path(raw: str, root: str = WORKSPACE) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("path is required")
    root_path = PurePosixPath(root).as_posix().rstrip("/") or "/"
    # The LLM frequently asks to "list /" to see the sandbox root. From its
    # perspective that means the sandbox workspace, not the VM's real filesystem
    # root. Map a bare "/" to the workspace root so it does not raise. All other
    # paths outside the workspace remain rejected (secure).
    if value == "/":
        return root_path
    candidate = value if value.startswith("/") else f"{root_path}/{value}"
    normalized = posixpath.normpath(candidate)
    if normalized != root_path and not normalized.startswith(root_path + "/"):
        raise ValueError(f"path must remain inside {root_path}")
    return normalized


def _truncate(text: str) -> str:
    return text[-_MAX_RESULT_CHARS:] if len(text) > _MAX_RESULT_CHARS else text


def _looks_like_missing_file(detail: str) -> bool:
    lowered = detail.lower()
    return "not found" in lowered or "no such file" in lowered or "does not exist" in lowered


class TenkiExecutionBackend:
    """Async client implementing the shared sandbox contract against Tenki."""

    def __init__(
        self,
        config: Any,
        *,
        sandbox_name: str = "powerx-session",
        lane_index: int | None = None,
        rotation: Any | None = None,
        on_lane_pinned: Any | None = None,
    ) -> None:
        self.config = config
        self.api_url = validate_tenki_api_url(str(getattr(config, "api_url", "") or ""))
        # Lane list. The plural setting wins; the legacy single key stays as a
        # fallback so a config saved before rotation existed keeps working.
        lanes = validate_tenki_api_keys(getattr(config, "api_keys", None) or "")
        if not lanes:
            legacy = validate_tenki_api_key(str(getattr(config, "api_key", "") or ""))
            lanes = [legacy] if legacy else []
        self.api_keys = lanes
        # The first lane is the primary, so anything reading ``api_key`` sees the
        # value it always did.
        self.api_key = lanes[0] if lanes else ""
        # Lane this backend works against. ``None`` means "not decided yet": a
        # brand-new session is assigned one by round-robin, while an existing
        # session is PINNED to the lane its disk actually lives in.
        self.lane_index = lane_index if lane_index is None else int(lane_index)
        self.rotation = rotation if rotation is not None else TenkiRotationState()
        # Called with a lane number the moment a session is created or adopted,
        # so the caller can persist it and pin the next backend instance to the
        # workspace that actually holds that session's disk.
        self.on_lane_pinned = on_lane_pinned
        self.image = validate_tenki_image(str(getattr(config, "image", "") or ""))
        self.snapshot_id = validate_tenki_snapshot_id(
            str(getattr(config, "snapshot_id", "") or "")
        )
        self.cpu_cores = validate_tenki_cpu_cores(
            getattr(config, "cpu_cores", 2) or 2
        )
        self.memory_mb = validate_tenki_memory_mb(
            getattr(config, "memory_mb", 4096) or 4096
        )
        self.disk_size_gb = validate_tenki_disk_size_gb(
            getattr(config, "disk_size_gb", 0) or 0
        )
        # The session TTL. One hour by default and administrator-configurable;
        # Tenki's own deadline is the backstop that reaps an abandoned VM.
        self.max_duration_seconds = validate_tenki_max_duration_seconds(
            getattr(config, "max_duration_seconds", 3600) or 3600
        )
        self.tag = validate_tenki_tag(str(getattr(config, "tag", "") or "powerx")) or "powerx"
        self.fetch_allow_hosts = validate_tenki_fetch_allow_hosts(
            str(getattr(config, "fetch_allow_hosts", "") or "")
        )
        self._fetch_hosts: set[str] = (
            {h.strip() for h in self.fetch_allow_hosts.split(",") if h.strip()}
            if self.fetch_allow_hosts
            else set(DEFAULT_FETCH_ALLOW_HOSTS)
        )
        self.sandbox_name = (
            sandbox_name if _NAME_RE.fullmatch(sandbox_name) else "powerx-session"
        )
        self.workspace = WORKSPACE
        self.last_session_id: str = ""
        # "Perfect sandbox" persistence (mirrors the Upstash/Daytona/Runloop
        # design): with persistence on, a finished task leaves the VM alive and
        # its disk intact, so files survive across tasks, agent restarts and
        # platform redeploys; the VM is reattached by name and only its max
        # duration ever reaps it. With persistence off, a finished task
        # terminates the VM instead.
        self.persist_workspace = bool(getattr(config, "persist_workspace", True))

    # ------------------------------------------------------------- lifecycle

    def _lane_count(self) -> int:
        return len(self.api_keys)

    def _new_client(self, lane: int | None = None) -> Any:
        """Build a client for *lane* (default: this backend's lane).

        Each lane is a different API key authenticating into a different Tenki
        workspace, so a client is always bound to exactly one workspace.
        """
        if not self.api_keys:
            raise TenkiError("Tenki API key is not configured")
        index = self.lane_index if lane is None else int(lane)
        if index is None or not 0 <= index < len(self.api_keys):
            index = 0
        client_cls = _load_async_client()
        try:
            return client_cls(auth_token=self.api_keys[index], base_url=self.api_url)
        except Exception as exc:  # noqa: BLE001 - SDK raises its own error types
            raise TenkiError(f"could not create the Tenki client: {_detail(exc)}") from None

    # ------------------------------------------------------------ lane choice

    def _pin(self, lane: int) -> None:
        """Record the lane this session lives in, and tell the store about it.

        A session's disk exists in exactly one workspace, so once a lane is
        known every later operation must go there: rotating a live session would
        look for its files in a workspace that has never seen them.
        """
        index = max(0, int(lane))
        self.lane_index = index
        if self.api_keys and index < len(self.api_keys):
            self.api_key = self.api_keys[index]
        notify = self.on_lane_pinned
        if notify is not None:
            with suppress(Exception):
                notify(index)

    def _pick_lane(self, skip: set[int] | None = None) -> int:
        """Choose the lane for a brand-new session (round-robin, parked last)."""
        count = self._lane_count()
        if count <= 1:
            return 0
        return int(self.rotation.next_lane(count, skip=skip))

    def _park_lane(self, lane: int, reason: str) -> None:
        """Stop offering *lane* first for a while after it failed on us."""
        with suppress(Exception):
            self.rotation.park(int(lane))
        logger.warning("Tenki lane {} parked after a lane-fatal failure: {}", lane, reason)

    async def _lane_facts(self, lane: int) -> dict[str, Any]:
        """Workspace identity and remaining session headroom for one lane.

        Both calls are best effort: the admin Test button must still show the
        lanes it could read rather than failing whole.
        """
        facts: dict[str, Any] = {}
        client = self._new_client(lane)
        try:
            with suppress(Exception):
                facts["workspace_id"] = _identity_workspace_id(await client.who_am_i())
            with suppress(Exception):
                limit = _usage_limit(await client.get_usage(), "max_concurrent_jobs")
                if limit is not None:
                    facts["active_sessions"] = limit.get("current")
                    facts["session_limit"] = limit.get("max")
            return facts
        finally:
            with suppress(Exception):
                await client.close()

    async def describe_lanes(self) -> list[dict[str, Any]]:
        """One row per configured lane: which workspace, and how full it is.

        This is the evidence that rotation spreads load over *distinct*
        workspaces: a repeated key would report the same workspace id twice and
        buy no extra capacity however many times it were listed.
        """
        rows: list[dict[str, Any]] = []
        for lane in range(self._lane_count()):
            row: dict[str, Any] = {"lane": lane}
            try:
                row.update(await self._lane_facts(lane))
            except Exception as exc:  # noqa: BLE001 - one bad key must not hide the rest
                row["error"] = _detail(exc)
            rows.append(row)
        return rows

    @staticmethod
    def _state(sandbox: Any) -> str:
        try:
            info = sandbox.info
        except Exception:  # noqa: BLE001
            return ""
        return str(getattr(info, "state", "") or getattr(sandbox, "state", "") or "").upper()

    def _usable(self, sandbox: Any) -> bool:
        return self._state(sandbox) not in _DEAD_STATES

    @staticmethod
    def _disk_gb(sandbox: Any) -> int:
        """The disk size this session was CREATED with, or 0 when unreported."""
        try:
            info = sandbox.info
        except Exception:  # noqa: BLE001
            return 0
        try:
            return int(getattr(info, "disk_size_gb", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _undersized(self, sandbox: Any) -> bool:
        """Whether this VM's disk is smaller than this deployment now asks for.

        Reports False whenever either size is unknown, so an SDK that omits
        ``disk_size_gb`` can never cause a working VM to be torn down.

        Tenki fixes a session's disk at create time — ``AsyncSandbox.update``
        carries name/tags/sticky/TTL and nothing else, and only *volumes* can be
        resized — so a session born before the setting was raised stays at the
        old size for the whole of its life. Measured live: every session created
        while the setting was unset came up ``disk=5``, which is below the ~6 GiB
        an MT5 + Wine install needs, and every later install in those sessions
        died on a full disk (the installer's own preflight is what surfaced it).
        """
        if not self.disk_size_gb:
            return False
        current = self._disk_gb(sandbox)
        return bool(current) and current < self.disk_size_gb

    async def _reclaim_undersized(self, sandbox: Any) -> None:
        """Terminate a VM provisioned below the configured disk size.

        Recreating is the only way to pick the new size up, and the old VM's
        files are already unusable — a full 5 GiB disk holds a half-installed
        Wine, not work worth preserving.

        Termination must SUCCEED before the caller creates a replacement: a
        session is resolved by its deterministic name, so leaving the undersized
        VM alive would put two VMs under one name and the next operation could
        attach to the old, still-too-small one all over again.
        """
        session_id = str(getattr(sandbox, "id", self.sandbox_name))
        logger.warning(
            "recreating Tenki session {}: it was created with a {} GB disk but {} GB "
            "is now configured, and Tenki cannot grow an existing session's disk",
            session_id,
            self._disk_gb(sandbox),
            self.disk_size_gb,
        )
        try:
            await sandbox.close()
        except Exception as exc:  # noqa: BLE001
            raise TenkiError(
                f"Tenki session {session_id} has a {self._disk_gb(sandbox)} GB disk but "
                f"{self.disk_size_gb} GB is configured, and it could not be terminated "
                f"to be replaced at the new size: {_detail(exc)}"
            ) from None
        self.last_session_id = ""

    async def _ready(self, sandbox: Any, *, timeout: float = 240) -> Any:
        """Block until the session is ``RUNNING`` (a fresh VM can take a while)."""
        if self._state(sandbox) == "RUNNING":
            return sandbox
        try:
            await sandbox.wait_ready(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - SDK raises WaitReadyFailedError et al.
            if self._state(sandbox) in _DEAD_STATES:
                raise TenkiError(
                    f"Tenki session {sandbox.id} entered terminal state: {_detail(exc)}"
                ) from None
            raise TenkiError(
                f"Tenki session {sandbox.id} did not become ready: {_detail(exc)}"
            ) from None
        return sandbox

    async def _find_session(self, client: Any) -> Any | None:
        """Resolve this session's VM by its deterministic name + tag."""
        for kwargs in ({"tags": [self.tag]}, {}):
            try:
                sessions = await client.list(include_terminated=False, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Tenki list failed: {}", _detail(exc))
                continue
            for item in sessions or []:
                try:
                    if str(item.name or "") != self.sandbox_name:
                        continue
                except Exception:  # noqa: BLE001
                    continue
                if self._usable(item):
                    return item
        return None

    async def _resolve_in_lane(
        self, client: Any, session_id: str | None = None
    ) -> Any | None:
        """Resolve an ALREADY EXISTING session inside ONE lane's workspace.

        Never creates. The client is bound to a single workspace, so a miss here
        means the session is not in this lane — not that it needs making.
        """
        target = session_id or self.last_session_id
        if target:
            sandbox: Any | None = None
            try:
                sandbox = await client.get(target)
            except Exception as exc:  # noqa: BLE001 - a stale id is expected
                logger.debug("Tenki get({}) failed: {}", target, _detail(exc))
            if sandbox is not None and self._usable(sandbox):
                await self._ready(sandbox, timeout=180)
                self.last_session_id = str(sandbox.id)
                return sandbox
            self.last_session_id = ""

        existing = await self._find_session(client)
        if existing is None:
            return None
        try:
            await self._ready(existing, timeout=180)
        except TenkiError:
            # A pre-existing VM that will not come up would fail every operation
            # forever; terminate it so a fresh one is created instead of leaving
            # the session wedged.
            logger.warning(
                "reclaiming stuck Tenki session {} that did not become ready",
                getattr(existing, "id", self.sandbox_name),
            )
            with suppress(Exception):
                await existing.close()
            return None
        self.last_session_id = str(existing.id)
        return existing

    async def _attach_only(
        self, session_id: str | None = None
    ) -> tuple[Any | None, Any | None]:
        """Resolve an ALREADY EXISTING session without ever creating one.

        Task-end lifecycle hooks (keep-alive, reset) must never spin up a fresh
        VM just to touch it, so they resolve rather than ensure. A pinned
        backend looks only in its own lane; an unpinned one searches every lane
        and pins whichever one holds the session, because the VM's disk exists
        in exactly one workspace and adopting it elsewhere would silently lose
        the files written earlier.

        Returns ``(sandbox, client)``. The client that holds the session is
        handed back STILL OPEN, because a sandbox handle is only usable while
        the client that produced it is alive; the caller closes it when the
        operation is over. Every client that found nothing is closed here.
        """
        if not self._lane_count():
            return None, None
        lanes = (
            [self.lane_index]
            if self.lane_index is not None
            else list(range(self._lane_count()))
        )
        for lane in lanes:
            client = self._new_client(int(lane))
            found: Any | None = None
            try:
                found = await self._resolve_in_lane(client, session_id)
            except Exception as exc:  # noqa: BLE001 - a dead lane is not fatal here
                logger.debug("Tenki lane {} could not be searched: {}", lane, _detail(exc))
                found = None
            if found is not None:
                self.last_session_id = str(found.id)
                if self.lane_index is None:
                    self._pin(int(lane))
                return found, client
            with suppress(Exception):
                await client.close()
        return None, None

    async def _ensure_session(self) -> tuple[Any, Any]:
        """Get or create this session's Tenki Sandbox, pinned or rotating.

        Returns ``(sandbox, client)`` with the client LEFT OPEN: the sandbox
        handle is only usable while the client that produced it is alive, so the
        caller owns closing it once the operation has finished.

        Pinned (``lane_index`` set): resolve inside that one workspace and, at
        worst, recreate there. Rotation is never consulted — a recreated VM must
        land back in the workspace that holds the session's disk.

        Unpinned: adopt the session wherever it already lives, otherwise this is
        a brand-new session and a lane is chosen round-robin.

        A session that exists but was created with less disk than is configured
        now is terminated and rebuilt rather than attached to: Tenki cannot
        resize a session, so reusing it would fail every heavy install for as
        long as it lives. Because that VM's files are discarded with it, its
        lane pin is dropped too and the replacement rotates like any new session.
        """
        sandbox, client = await self._attach_only()
        if sandbox is not None and client is not None:
            if not self._undersized(sandbox):
                return sandbox, client
            await self._reclaim_undersized(sandbox)
            with suppress(Exception):
                await client.close()
            self.lane_index = None
        if self.lane_index is not None:
            return await self._create_in_lane(self.lane_index)
        return await self._create_with_rotation()

    async def _create_in_lane(self, lane: int) -> tuple[Any, Any]:
        """Create this session's VM in *lane*, its pinned workspace.

        Returns the VM with its client still open, for the caller to close.
        """
        client = self._new_client(lane)
        try:
            sandbox = await self._create_session(client)
        except TenkiError as exc:
            with suppress(Exception):
                await client.close()
            detail = str(exc)
            if _is_lane_fatal(detail):
                raise TenkiError(
                    f"Tenki lane {lane} cannot create this session's VM ({detail}). "
                    "The session is pinned to that workspace because its files "
                    "live there, so it will not be recreated elsewhere — free a "
                    "session on that workspace, or reset the session to start a "
                    "new one on another lane."
                ) from None
            raise
        except BaseException:
            with suppress(Exception):
                await client.close()
            raise
        self.last_session_id = str(sandbox.id)
        self._pin(lane)
        return sandbox, client

    async def _create_with_rotation(self) -> tuple[Any, Any]:
        """Create a brand-new session, round-robining lanes until one accepts it.

        Every attempt runs on a fresh client built from that lane's own key, so
        a failure in one workspace cannot poison the next. Only a lane-fatal
        failure rotates: a bad CPU/RAM combination or a rejected snapshot id is
        re-raised as itself, because cycling the keys would hide a real
        configuration bug behind a retry that happens to be less picky.

        Returns the VM with the winning lane's client still open.
        """
        count = self._lane_count()
        if not count:
            raise TenkiError("Tenki API key is not configured")
        tried: set[int] = set()
        skip: set[int] = set()
        last = ""
        for _ in range(count):
            lane = self._pick_lane(skip)
            if lane in tried:
                break
            tried.add(lane)
            client = self._new_client(lane)
            try:
                sandbox = await self._create_session(client)
            except TenkiError as exc:
                with suppress(Exception):
                    await client.close()
                last = str(exc)
                if not _is_lane_fatal(last):
                    raise
                self._park_lane(lane, last)
                skip.add(lane)
                continue
            except BaseException:
                with suppress(Exception):
                    await client.close()
                raise
            self.last_session_id = str(sandbox.id)
            self._pin(lane)
            return sandbox, client
        raise TenkiError(
            f"no Tenki lane could accept a new session (tried {len(tried)} of "
            f"{count}): {last}"
        )

    async def _create_session(self, client: Any) -> Any:
        kwargs: dict[str, Any] = {
            "name": self.sandbox_name,
            "cpu_cores": self.cpu_cores,
            "memory_mb": self.memory_mb,
            # ``sticky`` must be False for ``max_duration`` to be honoured:
            # sticky takes precedence over the duration and would make the VM
            # immortal, so the TTL would silently stop applying.
            "sticky": False,
            "max_duration": self.max_duration_seconds,
            "tags": [self.tag],
            "metadata": {"app": "powerx", "managed-by": "nanobot"},
            "wait": True,
        }
        if self.disk_size_gb:
            kwargs["disk_size_gb"] = self.disk_size_gb
        # Only one base image source may be supplied: an operator snapshot (exact
        # baselined state) wins over a raw image reference.
        if self.snapshot_id:
            kwargs["snapshot_id"] = self.snapshot_id
        elif self.image:
            kwargs["image"] = self.image
        try:
            return await client.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            message = _detail(exc)
            if "exceed workspace limits" in message.lower():
                raise TenkiError(
                    f"Tenki rejected the requested resources ({self.cpu_cores} vCPU / "
                    f"{self.memory_mb} MB): {message}. Lower the Tenki CPU/RAM settings "
                    "in the admin panel or raise the workspace quota."
                ) from None
            raise TenkiError(f"could not create a Tenki session: {message}") from None

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        """One operation = one SDK client, so the gRPC channel always matches
        the running event loop and a wedged channel can never be reused.

        Which lane that client is built for is decided inside
        ``_ensure_session``: a pinned session is resolved in its own workspace,
        while a brand-new one rotates. That call hands back the OPEN client that
        owns the session, and this is where it is closed — the sandbox stays
        usable for exactly as long as its channel is.
        """
        sandbox, client = await self._ensure_session()
        try:
            yield sandbox
        finally:
            with suppress(Exception):
                await client.close()

    async def describe(self, session_id: str | None = None) -> dict[str, Any]:
        """Return live session facts (used by the admin Test button and tests)."""
        async with self._session() as sandbox:
            info = getattr(sandbox, "info", None)
            facts: dict[str, Any] = {
                "session_id": str(sandbox.id),
                "name": str(getattr(info, "name", "") or ""),
                "state": self._state(sandbox),
                "cpu_cores": getattr(info, "cpu_cores", None),
                "memory_mb": getattr(info, "memory_mb", None),
                "disk_size_gb": getattr(info, "disk_size_gb", None),
                "timeout_at": getattr(info, "timeout_at", None),
                "workspace": self.workspace,
                # Which lane this session actually lives in. With one key it is
                # always 0; with several it is the one round-robin assigned.
                "lane_index": self.lane_index,
                "lane_count": self._lane_count(),
            }
        lane = self.lane_index if self.lane_index is not None else 0
        with suppress(Exception):
            facts.update(await self._lane_facts(lane))
        return facts

    async def keep_alive(self, session_id: str | None = None) -> None:
        """Push the session deadline out by one full TTL window.

        Never creates a VM: with persistence on this runs at task end, where
        spinning up a fresh session would be pure waste. A missing or already
        dead session is a silent no-op.
        """
        sandbox, client = await self._attach_only(session_id)
        if sandbox is None or client is None:
            return
        try:
            await sandbox.extend(self.max_duration_seconds)
            self.last_session_id = str(sandbox.id)
        finally:
            with suppress(Exception):
                await client.close()

    # ------------------------------------------------------------------ exec

    @staticmethod
    def _render(result: Any) -> str:
        stdout = str(getattr(result, "stdout_text", "") or "")
        stderr = str(getattr(result, "stderr_text", "") or "")
        code = getattr(result, "exit_code", None)
        text = stdout
        if stderr:
            text += f"\n[stderr]\n{stderr}"
        if getattr(result, "timed_out", False):
            text += "\n[timed_out=true]"
        if code is not None and int(code) != 0:
            text += f"\n[exit_code={code}]"
        return _truncate(text) or "(no output)"

    async def run(self, command: str, *, timeout: int = 120) -> str:
        command = str(command or "").strip()
        if not command:
            raise ValueError("command is required")
        if len(command) > _MAX_COMMAND_CHARS:
            raise ValueError(f"command exceeds {_MAX_COMMAND_CHARS} characters")
        timeout = max(1, min(int(timeout), _MAX_TIMEOUT))
        async with self._session() as sandbox:
            try:
                result = await sandbox.shell(command, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                raise TenkiError(_detail(exc)) from None
        return self._render(result)

    async def read(self, path: str) -> str:
        target = _safe_path(path, self.workspace)
        async with self._session() as sandbox:
            try:
                raw = await sandbox.fs.read_bytes(target)
            except Exception as exc:  # noqa: BLE001
                if _looks_like_missing_file(_detail(exc)):
                    return ""
                # Fall back to a shell read for paths the file API declines.
                try:
                    return await self._read_via_exec(sandbox, target)
                except Exception:  # noqa: BLE001
                    return ""
        if raw is None:
            return ""
        try:
            return _truncate(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return _truncate(base64.b64encode(raw).decode("ascii"))

    async def _read_via_exec(self, sandbox: Any, target: str) -> str:
        result = await sandbox.shell(f"base64 {shlex.quote(target)}", timeout=90)
        payload = _strip_trailers(str(getattr(result, "stdout_text", "") or ""))
        try:
            raw = base64.b64decode(payload, validate=False)
        except Exception:  # noqa: BLE001
            return payload
        try:
            return _truncate(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return _truncate(payload)

    async def write(self, path: str, content: str) -> None:
        target = _safe_path(path, self.workspace)
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError(f"content exceeds {_MAX_CONTENT_CHARS} characters")
        async with self._session() as sandbox:
            parent = posixpath.dirname(target)
            if parent and parent != "/":
                with suppress(Exception):
                    await sandbox.fs.mkdir(parent, recursive=True)
            try:
                await sandbox.fs.write_text(target, content)
            except Exception as exc:  # noqa: BLE001
                raise TenkiError(_detail(exc)) from None

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = _safe_path(path, self.workspace)
        if len(data) > _MAX_UPLOAD_BYTES:
            raise ValueError("file exceeds 200 MiB")
        async with self._session() as sandbox:
            parent = posixpath.dirname(target)
            if parent and parent != "/":
                with suppress(Exception):
                    await sandbox.fs.mkdir(parent, recursive=True)
            try:
                await sandbox.fs.write_bytes(target, data)
            except Exception as exc:  # noqa: BLE001
                # Fallback: base64 through the exec plane for payloads the file
                # API rejects.
                b64 = base64.b64encode(data).decode("ascii")
                result = await sandbox.shell(
                    f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(target)}",
                    timeout=180,
                )
                if int(getattr(result, "exit_code", 0) or 0) != 0:
                    raise TenkiError(_detail(exc)) from None

    async def list(self, path: str) -> str:
        target = _safe_path(path or self.workspace, self.workspace)
        listing_root = target if target == self.workspace else (posixpath.dirname(target) or self.workspace)
        command = f"find {shlex.quote(listing_root)} -maxdepth 2 -printf '%y %p\\n' 2>/dev/null | head -200"
        return await self.run(command, timeout=60)

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
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        allowed = parsed.scheme in ("https", "http") and self._is_host_allowed(host)
        if not allowed:
            raise ValueError(
                f"URL host {host!r} is not in the allowed fetch hosts list. "
                "Add it to the Tenki fetch_allow_hosts setting."
            )
        dest = _safe_path(dest_path, self.workspace)
        command = (
            f"mkdir -p {shlex.quote(posixpath.dirname(dest))} && "
            f"curl -fsSL --max-time {int(timeout)} -o {shlex.quote(dest)} {shlex.quote(url)} && "
            f"stat -c %s {shlex.quote(dest)}"
        )
        out = await self.run(command, timeout=min(timeout + 30, _MAX_TIMEOUT))
        if "[exit_code=" in out and "[exit_code=0]" not in out:
            raise TenkiError(f"remote fetch failed: {out[:300]}")
        return dest

    async def download(self, remote_path: str, local_path: Any) -> Any:
        """Download a session file to a local path."""
        target = _safe_path(remote_path, self.workspace)
        destination = Path(str(local_path)).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with self._session() as sandbox:
            # The stat is best-effort (a missing file is reported by the
            # download itself), but the size guard must NOT be swallowed: keep
            # it outside the suppress so an oversized file fails loudly.
            size = 0
            with suppress(Exception):
                stat = await sandbox.fs.stat(target)
                size = int(getattr(stat, "size", 0) or 0)
            if size > _MAX_DOWNLOAD_BYTES:
                raise TenkiError(
                    f"file exceeds the {_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MiB download limit"
                )
            try:
                await sandbox.fs.download(target, str(destination))
                return destination
            except Exception as exc:  # noqa: BLE001
                if _looks_like_missing_file(_detail(exc)):
                    raise TenkiFileNotFoundError(_detail(exc)) from None
            # Fallback via base64 exec.
            payload = await self._read_via_exec(sandbox, target)
        try:
            destination.write_bytes(base64.b64decode(payload, validate=False))
        except Exception:  # noqa: BLE001
            raise TenkiError("failed to decode downloaded artifact") from None
        return destination

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

    async def _disk_report(self, sandbox: Any) -> dict[str, Any]:
        """Live free space inside the session, for the admin Test button.

        Provisioned size alone does not tell an operator whether an install will
        fit — a correctly sized session can still be full — so this reads the
        filesystem the workspace actually sits on. Best effort: a session that
        cannot answer must not fail the connection test.
        """
        report: dict[str, Any] = {"disk_size_gb": self._disk_gb(sandbox)}
        try:
            result = await sandbox.shell(
                f"df -Pk {shlex.quote(self.workspace)}", timeout=60
            )
        except Exception as exc:  # noqa: BLE001
            report["disk_error"] = _detail(exc)
            return report
        report.update(_parse_df_kb(getattr(result, "stdout_text", "")))
        if "total_mb" not in report:
            report["disk_error"] = "df produced no parsable line"
        return report

    async def test_connection(self) -> dict[str, Any]:
        async with self._session() as sandbox:
            result = await sandbox.shell("uname -a", timeout=90)
            disk = await self._disk_report(sandbox)
            info = getattr(sandbox, "info", None)
            timeout_at = getattr(info, "timeout_at", None)
            session_id = str(sandbox.id)
            state = self._state(sandbox)
        payload: dict[str, Any] = {
            "ok": int(getattr(result, "exit_code", 0) or 0) == 0,
            "backend": "tenki",
            "session_id": session_id,
            "platform": str(getattr(result, "stdout_text", "") or "").strip()[:200],
            "state": state,
            "cpu_cores": getattr(info, "cpu_cores", None),
            "memory_mb": getattr(info, "memory_mb", None),
            # Live headroom next to the provisioned size: "20 GB configured" is
            # not the same claim as "20 GB free where the install will land".
            **disk,
            # ``SandboxInfo`` exposes the deadline, not the configured TTL, so the
            # landing TTL is verified through ``timeout_at``.
            "timeout_at": timeout_at.isoformat() if hasattr(timeout_at, "isoformat") else str(timeout_at or ""),
            "lane_index": self.lane_index,
            "lane_count": self._lane_count(),
        }
        # Which workspace served this test and how much session headroom its
        # quota has left — the numbers that show rotation is buying capacity.
        lane = self.lane_index if self.lane_index is not None else 0
        with suppress(Exception):
            payload.update(await self._lane_facts(lane))
        return payload

    async def reset(self, session_id: str | None = None) -> None:
        """Terminate the session permanently (explicit wipe / persistence opt-out).

        Only an existing VM is terminated — reset must never create one.
        """
        sandbox, client = await self._attach_only(session_id)
        try:
            if sandbox is not None:
                with suppress(Exception):
                    await sandbox.close()
            self.last_session_id = ""
        finally:
            with suppress(Exception):
                await client.close()


def _detail(exc: BaseException) -> str:
    """One-line, always-non-empty description of an SDK error."""
    text = " ".join(str(exc).split())
    return text[:400] or type(exc).__name__


def _strip_trailers(text: str) -> str:
    return re.sub(r"\n\[(?:stderr|exit_code)=\d+\]\s*$", "", text).strip()


def _parse_df_kb(text: str) -> dict[str, int]:
    """Pull total/free megabytes out of ``df -Pk`` output.

    POSIX ``-P`` fixes the column order to ``Filesystem 1024-blocks Used
    Available Capacity Mounted-on``, so the fields are positional. The LAST
    parsable line wins, which is also the correct answer when ``df`` prints its
    header. Anything unexpected yields ``{}``: this is a diagnostic readout, and
    a filesystem the parser has never seen must not fail the Test button.
    """
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


__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_FETCH_ALLOW_HOSTS",
    "WORKSPACE",
    "TenkiError",
    "TenkiExecutionBackend",
    "TenkiFileNotFoundError",
    "is_sdk_available",
    "tenki_sandbox_name",
    "MAX_API_KEYS",
    "TenkiRotationState",
    "parse_tenki_api_keys",
    "validate_tenki_api_key",
    "validate_tenki_api_keys",
    "validate_tenki_api_url",
    "validate_tenki_cpu_cores",
    "validate_tenki_disk_size_gb",
    "validate_tenki_fetch_allow_hosts",
    "validate_tenki_image",
    "validate_tenki_max_duration_seconds",
    "validate_tenki_memory_mb",
    "validate_tenki_snapshot_id",
    "validate_tenki_tag",
]
