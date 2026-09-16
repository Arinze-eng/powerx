"""Run the sandbox tool contract on an administrator-configured Daytona sandbox.

Daytona (https://daytona.io) provides isolated cloud sandboxes with shell +
filesystem APIs and configurable network firewall / domain allowlists. This
backend communicates directly with Daytona's REST API over HTTPS (``aiohttp``
only — no extra SDK dependency), mirroring the official ``daytona`` SDK:

Platform API (api_url, e.g. ``https://app.daytona.io/api`` or ``https://api.daytona.io``):
* ``POST   {api}/sandbox``             → create a sandbox (snapshot, allowlists, name)
* ``GET    {api}/sandbox/{idOrName}``   → sandbox metadata (state, toolboxProxyUrl)
* ``DELETE {api}/sandbox/{idOrName}``   → permanently kill the sandbox

Toolbox API (per-sandbox ``toolboxProxyUrl`` returned in sandbox metadata):
* ``POST   {toolbox}/process/execute``  → body ``{"command": cmd, "timeout": t}``
                                          returns ``{"exitCode": 0, "result": "..."}``
* ``GET    {toolbox}/files/download?path=`` → download file (raw bytes)
* ``POST   {toolbox}/files?path=``     → upload file (multipart field "file" or raw)
* ``DELETE {api}/sandbox/{idOrName}``   → permanently kill the sandbox

Lifecycle: user sessions map to a deterministic sandbox name. Explicit release
deletes the sandbox immediately; a wall-clock TTL backstop automatically reaps
it server-side if cleanup is missed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import posixpath
import re
import shlex
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse, urlsplit

import aiohttp
from loguru import logger

from nanobot.agent.tools.daytona_relay import (
    RelayError,
    RelayPolicy,
    RelayResult,
    build_policy,
    relay_fetch_into_backend,
)

_MAX_COMMAND_CHARS = 12_000
_MAX_CONTENT_CHARS = 120_000
_MAX_RESULT_CHARS = 16_000
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
_MAX_TIMEOUT = 900

# Daytona sandboxes (Ubuntu-based official snapshots) use /home/daytona as default.
WORKSPACE = "/home/daytona"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

# Default allowed hosts for sandbox fetch_url (expanded for common registries and mirrors).
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

# Broad default domain allowlist used when no custom allowlist is provided.
# Daytona sandboxes need an explicit domain allowlist to reach arbitrary hosts
# (an open CIDR alone only unlocks essential services).
# HARD LIMIT: Daytona accepts at most 100 domains per allow list (HTTP 400 above
# that), so this list is capped at 99 to leave headroom for user overrides.
DEFAULT_DOMAIN_ALLOW_LIST: str = (
    # Package registries (12)
    "pypi.org,*.pypi.org,files.pythonhosted.org,"
    "registry.npmjs.org,npmjs.org,proxy.golang.org,golang.org,"
    "crates.io,static.crates.io,rubygems.org,repo1.maven.org,packagist.org,"
    # Runtimes, SDKs & CDNs (14)
    "nodejs.org,deb.nodesource.com,"
    "registry.yarnpkg.com,yarnpkg.com,bun.sh,"
    "dl.google.com,storage.googleapis.com,"
    "cdn.jsdelivr.net,unpkg.com,esm.sh,"
    "packages.microsoft.com,apt.llvm.org,ppa.launchpad.net,launchpad.net,"
    # GitHub / repos (6)
    "github.com,codeload.github.com,*.githubusercontent.com,ghcr.io,gitlab.com,*.gitlab.com,"
    # AI providers (30)
    "openai.com,*.openai.com,oaiusercontent.com,*.oaiusercontent.com,"
    "anthropic.com,*.anthropic.com,"
    "googleapis.com,*.googleapis.com,ai.google.dev,gemini.google.com,"
    "deepseek.com,*.deepseek.com,openrouter.ai,groq.com,*.groq.com,"
    "mistral.ai,*.mistral.ai,x.ai,api.x.ai,together.ai,api.together.xyz,"
    "fireworks.ai,perplexity.ai,*.perplexity.ai,cohere.com,api.cohere.com,"
    "huggingface.co,*.huggingface.co,hf.co,"
    # Search & knowledge (9)
    "google.com,*.google.com,gstatic.com,*.gstatic.com,"
    "duckduckgo.com,*.duckduckgo.com,bing.com,*.bing.com,"
    "wikipedia.org,"
    # Distro & container mirrors (8)
    "archive.ubuntu.com,security.ubuntu.com,*.ubuntu.com,deb.debian.org,*.debian.org,"
    "docker.io,registry-1.docker.io,quay.io,"
    # Diagnostics & platform (7)
    "daytona.io,*.daytona.io,example.com,httpbin.org,api.ipify.org,ifconfig.me,ipinfo.io,"
    # Messaging (6)
    "api.telegram.org,*.telegram.org,discord.com,*.discord.com,slack.com,*.slack.com,"
    # File drops (8)
    "onlyfiles.com,gofile.io,*.gofile.io,filebin.net,0x0.st,transfer.sh,bashupload.com,temp.sh"
)

# Daytona's documented cap for a sandbox domain allow list.
MAX_DOMAIN_ALLOW_LIST_ENTRIES = 100
# Daytona's documented cap for a sandbox CIDR allow list. This is far smaller
# than the domain cap and crossing it is a hard API 400, not a truncation.
MAX_NETWORK_ALLOW_LIST_ENTRIES = 10

# Daytona organizations are billed by tier, and Tier 1/Tier 2 orgs are
# network-restricted at the ORGANIZATION level: the API refuses any sandbox-level
# domain/CIDR allow list with HTTP 400
#   "Network access is restricted and cannot be overridden at the sandbox level."
# Sandboxes may still reach Daytona's essential services (package registries,
# GitHub, model/LLM endpoints), but no allow list can widen that set at these
# tiers. The marker is cached per process once Daytona tells us so, because
# otherwise every single sandbox creation pays a guaranteed-failed create first.
_network_override_restricted = False


def _is_network_override_rejection(detail: str) -> bool:
    """True when Daytona says sandbox-level network policy cannot be set."""
    text = str(detail or "").lower()
    return "network access is restricted" in text or (
        "cannot be overridden" in text and "network" in text
    )


def daytona_network_override_restricted() -> bool:
    """Whether Daytona has told us this org cannot set sandbox network policy."""
    return _network_override_restricted

# States in which a Daytona sandbox is ready for toolbox commands.
_READY_STATES = {"started", "running", "healthy", "ready", "active"}
# States from which a sandbox can be STARTED again with its filesystem intact
# (Daytona auto-stops sandboxes after ``autoStopInterval`` of inactivity and
# archives them after longer inactivity; both preserve the disk). These must
# NEVER be treated as terminal: doing so makes ensure_sandbox create a fresh
# sandbox from the snapshot and silently wipe the user's whole workspace
# mid-task (the "files disappear during compilation" bug).
_RESUMABLE_STATES = {"stopped", "archived"}
_TERMINAL_STATES = {"deleted", "deleting", "error", "failed"}


class DaytonaError(RuntimeError):
    """Raised when the Daytona API rejects a request."""


class DaytonaFileNotFoundError(DaytonaError):
    """Raised when a remote file does not exist."""


_MISSING_FILE_HINTS = (
    "failed to read file",
    "no such file",
    "not found",
    "does not exist",
    "is a directory",
    "cannot find the file",
)


def _looks_like_missing_file(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(hint in lowered for hint in _MISSING_FILE_HINTS)


def validate_daytona_api_key(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 256 or any(ord(char) < 0x21 or ord(char) == 0x7F for char in value):
        raise ValueError("Daytona API key must be a single-line token of at most 256 characters")
    return value


def validate_daytona_api_url(raw: str) -> str:
    value = str(raw or "").strip().rstrip("/") or "https://app.daytona.io/api"
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Daytona API URL must be a valid HTTP(S) origin such as https://app.daytona.io/api")
    return value


def validate_daytona_snapshot(raw: str) -> str:
    value = str(raw or "daytona-small").strip()
    if not value:
        return "daytona-small"
    if len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError("Daytona snapshot must be an alphanumeric identifier (e.g. daytona-small)")
    return value


def validate_daytona_domain_allow_list(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    # Comma-separated list of hostnames/wildcards (e.g. "*.pypi.org,example.com")
    parts = [p.strip() for p in value.split(",") if p.strip()]
    for p in parts:
        if not re.fullmatch(r"(\*\.)?[a-zA-Z0-9][-a-zA-Z0-9.]*[a-zA-Z0-9]", p) and p != "*":
            raise ValueError(f"Invalid domain in allowlist: {p!r}")
    return ",".join(parts)


def validate_daytona_network_allow_list(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return "0.0.0.0/0"
    # Comma-separated IPv4 CIDRs. Daytona documents a hard maximum of TEN
    # entries for networkAllowList (distinct from the 100 allowed for
    # domainAllowList) and requires every entry to carry a /prefix, so reject
    # both violations here instead of letting the API return an opaque 400.
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if len(parts) > MAX_NETWORK_ALLOW_LIST_ENTRIES:
        raise ValueError(
            f"Daytona networkAllowList supports at most {MAX_NETWORK_ALLOW_LIST_ENTRIES} "
            f"CIDR entries; got {len(parts)}"
        )
    for p in parts:
        if not re.fullmatch(r"[0-9a-fA-F.:/]+", p):
            raise ValueError(f"Invalid CIDR in network allowlist: {p!r}")
        if not _is_cidr(p):
            raise ValueError(
                f"Daytona networkAllowList entries must be IPv4 CIDR blocks with a "
                f"/prefix (for example 143.244.209.188/32); got {p!r}"
            )
    return ",".join(parts)


def _is_cidr(value: str) -> bool:
    """True when *value* is a dotted IPv4 CIDR block (``addr/prefix``)."""
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False
    return "/" in value


def validate_daytona_outbound_proxy_url(raw: str) -> str:
    """Validate a Daytona ``outboundProxyUrl`` (HTTP/HTTPS, optional auth).

    Routing sandbox egress through an operator-run proxy is the only mechanism
    Daytona accepts for widening outbound access on a network-restricted
    (Tier 1/2) organization, so it is configurable in its own right.
    """
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 512:
        raise ValueError("Daytona outbound proxy URL is too long")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("Daytona outbound proxy URL is malformed") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Daytona outbound proxy URL must be http(s)://host:port")
    return value


def validate_daytona_fetch_allow_hosts(raw: str) -> str:
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


_NETWORK_POLICY_KEYS = frozenset({"domainAllowList", "networkAllowList", "networkBlockAll"})

# Relay defaults: the curated fetch allow list doubles as the relay allow list so
# `action=fetch_url` behaves the same whether or not the sandbox can dial out.
DEFAULT_RELAY_ALLOW_HOSTS: tuple[str, ...] = DEFAULT_FETCH_ALLOW_HOSTS


def daytona_sandbox_name(session_key: str) -> str:
    """Deterministic, valid sandbox name for a session so sandboxes survive restarts."""
    slug = re.sub(r"[^a-z0-9-]", "-", session_key.lower()).strip("-")[:32] or "session"
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:10]
    name = f"px-{slug}-{digest}".strip("-").replace("--", "-")
    return name[:48] if _NAME_RE.fullmatch(name) else f"px-{digest}"


def _safe_path(raw: str, root: str = WORKSPACE) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("path is required")
    root_path = PurePosixPath(root).as_posix().rstrip("/") or "/"
    # The LLM frequently asks to "list /" to see the sandbox root. From its
    # perspective that means the sandbox workspace, not the container's real
    # filesystem root. Map a bare "/" to the workspace root so it does not
    # raise. All other paths outside the workspace remain rejected (secure).
    if value == "/":
        return root_path
    candidate = value if value.startswith("/") else f"{root_path}/{value}"
    normalized = posixpath.normpath(candidate)
    if normalized != root_path and not normalized.startswith(root_path + "/"):
        raise ValueError(f"path must remain inside {root_path}")
    return normalized


def _truncate(text: str) -> str:
    return text[-_MAX_RESULT_CHARS:] if len(text) > _MAX_RESULT_CHARS else text


class DaytonaExecutionBackend:
    """Async client implementing the shared sandbox contract against Daytona."""

    def __init__(self, config: Any, *, sandbox_name: str = "powerx-session") -> None:
        self.config = config
        self.api_url = validate_daytona_api_url(str(getattr(config, "api_url", "") or ""))
        self.api_key = validate_daytona_api_key(str(getattr(config, "api_key", "") or ""))
        self.snapshot = validate_daytona_snapshot(str(getattr(config, "snapshot", "") or "daytona-small"))
        self.domain_allow_list = validate_daytona_domain_allow_list(
            str(getattr(config, "domain_allow_list", "") or "")
        )
        self.network_allow_list = validate_daytona_network_allow_list(
            str(getattr(config, "network_allow_list", "") or "0.0.0.0/0")
        )
        self.fetch_allow_hosts = validate_daytona_fetch_allow_hosts(
            str(getattr(config, "fetch_allow_hosts", "") or "")
        )
        # Operator-run HTTP(S) egress proxy. On a network-restricted Daytona
        # organization this is the only lever that widens outbound access, so it
        # is applied even after the allow-list path has been dropped.
        self.outbound_proxy_url = validate_daytona_outbound_proxy_url(
            str(getattr(config, "outbound_proxy_url", "") or "")
        )
        self._fetch_hosts: set[str] = (
            {h.strip() for h in self.fetch_allow_hosts.split(",") if h.strip()}
            if self.fetch_allow_hosts
            else set(DEFAULT_FETCH_ALLOW_HOSTS)
        )
        # Host-side relay. A Tier 1/Tier 2 Daytona organization blocks arbitrary
        # egress no matter what allow list PowerX sends, so destinations outside
        # the essential-services set can never be dialled from inside the
        # sandbox. Instead the PowerX host (unrestricted egress) performs the
        # fetch and injects the bytes over the toolbox files API. Set
        # relay_enabled=False to force the old direct-from-sandbox behaviour.
        self.relay_enabled = bool(getattr(config, "relay_enabled", True))
        self.relay_allow_http = bool(getattr(config, "relay_allow_http", False))
        self.relay_allow_private_hosts = bool(getattr(config, "relay_allow_private_hosts", False))
        relay_hosts = str(getattr(config, "relay_allow_hosts", "") or "").strip()
        self.relay_policy: RelayPolicy = build_policy(
            relay_hosts if relay_hosts else sorted(self._fetch_hosts),
            allow_http=self.relay_allow_http,
            max_bytes=int(getattr(config, "relay_max_bytes", 0) or 0) or 268_435_456,
            allow_private=self.relay_allow_private_hosts,
        )
        self.ttl_minutes = max(5, min(int(getattr(config, "ttl_minutes", 60) or 60), 43_200))
        self.auto_stop_minutes = max(0, min(int(getattr(config, "auto_stop_minutes", 0) or 0), 10_080))
        self.sandbox_name = sandbox_name if _NAME_RE.fullmatch(sandbox_name) else "powerx-session"
        self.workspace = WORKSPACE
        self.last_sandbox_id: str = ""
        self._toolbox_url: str = ""
        # "Perfect sandbox" persistence (mirrors the Upstash archive-box
        # design): finished tasks snapshot the workspace into a dedicated
        # archive sandbox; a freshly created sandbox restores that snapshot so
        # files survive task end, agent restarts, and TTL reaping.
        self.persist_workspace = bool(getattr(config, "persist_workspace", True))

    def _network_policy_body(self) -> dict[str, Any]:
        """Build the sandbox-level network keys to send on create.

        Daytona's rules (https://www.daytona.io/docs/en/network-limits):

        * ``domainAllowList`` (max 100 entries) and ``networkAllowList``
          (max 10 CIDRs) are MUTUALLY EXCLUSIVE - never send both.
        * Setting either one is RESTRICTIVE: it replaces Daytona's default
          policy, so essential services are not granted on top of it.
        * A network-restricted organization (Tier 1/2) rejects both outright;
          once Daytona has told us that, stop sending an allow list so every
          sandbox creation does not begin with a guaranteed-failed request.

        ``outboundProxyUrl`` is orthogonal and is always sent when configured.
        """
        body: dict[str, Any] = {}
        if self.outbound_proxy_url:
            body["outboundProxyUrl"] = self.outbound_proxy_url
        if _network_override_restricted:
            return body

        domain_list = self.domain_allow_list
        if domain_list == "*":
            # "*" is the administrator's "allow everything" choice. Daytona
            # expresses open egress as an IPv4 CIDR allow list, and the two
            # keys are mutually exclusive, so send the CIDR and nothing else.
            return {"networkAllowList": "0.0.0.0/0"}
        if not domain_list:
            # Nothing configured: ship the curated registry/API allow list so
            # installs and common integrations work out of the box.
            domain_list = DEFAULT_DOMAIN_ALLOW_LIST
        entries = [p.strip() for p in domain_list.split(",") if p.strip()]
        if len(entries) > MAX_DOMAIN_ALLOW_LIST_ENTRIES:
            logger.warning(
                "Daytona domain allow list has {} entries; truncating to the documented "
                "maximum of {}",
                len(entries),
                MAX_DOMAIN_ALLOW_LIST_ENTRIES,
            )
            entries = entries[:MAX_DOMAIN_ALLOW_LIST_ENTRIES]
        body["domainAllowList"] = ",".join(entries)
        return body

    # ------------------------------------------------------------------ HTTP

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise DaytonaError("Daytona API key is not configured")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    async def _platform_request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: int = 120,
    ) -> Any:
        url = f"{self.api_url}{path}"
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(),
                json=body,
                timeout=aiohttp.ClientTimeout(total=timeout + 30),
            ) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text) if text else {}
                except ValueError:
                    data = {"raw": text}
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    # Daytona returns {"statusCode": 400, "error": "Bad Request", "message": "Total disk limit exceeded..."}
                    # Prefer the informative 'message' over the generic 'error' status phrase.
                    msg = data.get("message")
                    err = data.get("error")
                    detail = str(msg or err or text)[:300]
                    raise DaytonaError(f"{method} {path} failed with HTTP {resp.status}: {detail}")
                return data
        except aiohttp.ClientError as exc:
            raise DaytonaError(f"Daytona platform transport error: {type(exc).__name__}") from None

    async def _toolbox_request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        data: Any | None = None,
        params: dict[str, str] | None = None,
        timeout: int = 120,
        raw_response: bool = False,
    ) -> Any:
        if not self._toolbox_url:
            raise DaytonaError("Toolbox URL is not resolved (sandbox not ready)")
        url = f"{self._toolbox_url}{path}"
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(),
                json=body,
                data=data,
                params=params,
                timeout=aiohttp.ClientTimeout(total=timeout + 30),
            ) as resp:
                if raw_response:
                    payload_bytes = await resp.read()
                    if resp.status == 404:
                        raise DaytonaFileNotFoundError(f"{path}: file not found")
                    if resp.status >= 400:
                        raise DaytonaError(
                            f"Toolbox {method} {path} failed with HTTP {resp.status}: "
                            f"{payload_bytes[:200].decode('utf-8', 'replace')}"
                        )
                    return payload_bytes
                text = await resp.text()
                try:
                    res_data = json.loads(text) if text else {}
                except ValueError:
                    res_data = {"raw": text}
                if resp.status == 404 or (resp.status == 500 and _looks_like_missing_file(text)):
                    raise DaytonaFileNotFoundError(f"{method} {path}: file not found ({text[:200]})")
                if resp.status >= 400:
                    detail = str(res_data.get("error") or res_data.get("message") or text)[:300]
                    raise DaytonaError(f"Toolbox {method} {path} failed with HTTP {resp.status}: {detail}")
                return res_data
        except aiohttp.ClientError as exc:
            raise DaytonaError(f"Daytona toolbox transport error: {type(exc).__name__}") from None

    # --------------------------------------------------------------- lifecycle

    async def _start_sandbox(self, session: aiohttp.ClientSession, sandbox_id_or_name: str) -> None:
        """Start a stopped/archived sandbox, PRESERVING its filesystem.

        Daytona keeps the sandbox disk when it auto-stops (autoStopInterval)
        or archives it, so starting the existing sandbox restores the user's
        workspace exactly as it was. Recreating instead wipes everything.
        """
        try:
            await self._platform_request(
                session, "POST", f"/sandbox/{quote(sandbox_id_or_name, safe='')}/start", timeout=90
            )
        except DaytonaError as exc:
            detail = str(exc)
            if "409" not in detail and "already" not in detail.lower():
                raise

    async def find_sandbox(self, session: aiohttp.ClientSession) -> dict[str, Any] | None:
        """Fetch sandbox metadata by name if it exists and is not terminated."""
        data = await self._platform_request(session, "GET", f"/sandbox/{quote(self.sandbox_name, safe='')}", timeout=30)
        if not isinstance(data, dict):
            return None
        state = str(data.get("state") or "").lower()
        if state in _TERMINAL_STATES:
            # Clean up the terminal sandbox so its name and disk are freed server-side.
            target = str(data.get("id") or self.sandbox_name)
            try:
                await self._platform_request(session, "DELETE", f"/sandbox/{quote(target, safe='')}", timeout=30)
            except Exception:
                pass
            return None
        return data

    async def wait_ready(self, session: aiohttp.ClientSession, sandbox_id_or_name: str, timeout: int = 180) -> dict[str, Any]:
        """Poll until the sandbox is in a started/ready state and toolboxProxyUrl is present."""
        deadline = asyncio.get_running_loop().time() + timeout
        started_once = False
        while True:
            data = await self._platform_request(
                session, "GET", f"/sandbox/{quote(sandbox_id_or_name, safe='')}", timeout=30
            )
            if isinstance(data, dict):
                state = str(data.get("state") or "").lower()
                toolbox = str(data.get("toolboxProxyUrl") or data.get("toolbox_proxy_url") or "").rstrip("/")
                if (state in _READY_STATES or state == "started") and toolbox:
                    # The Daytona toolbox proxy routes requests per sandbox:
                    # `{toolboxProxyUrl}/{sandboxId}`. The sandbox ID must be
                    # part of the base URL so the proxy can identify the target
                    # container and validate the Bearer token (without the ID
                    # the proxy rejects with 401 "Bearer token is invalid").
                    real_id = str(data.get("id") or sandbox_id_or_name)
                    if real_id and not toolbox.rstrip("/").endswith(f"/{real_id}"):
                        self._toolbox_url = f"{toolbox}/{real_id}"
                    else:
                        self._toolbox_url = toolbox
                    return data
                # A stopped/archived sandbox still owns its disk: start it and
                # keep polling instead of giving up (which used to cascade into
                # a fresh create that wiped the workspace).
                if state in _RESUMABLE_STATES and not started_once:
                    started_once = True
                    await self._start_sandbox(session, sandbox_id_or_name)
                    if asyncio.get_running_loop().time() >= deadline:
                        deadline = asyncio.get_running_loop().time() + 90
                    continue
                if state in _TERMINAL_STATES:
                    reason = data.get("errorReason") or state
                    raise DaytonaError(f"Daytona sandbox entered failed state: {reason}")
            if asyncio.get_running_loop().time() >= deadline:
                raise DaytonaError(f"Daytona sandbox {sandbox_id_or_name} did not become ready in time")
            await asyncio.sleep(2)

    async def ensure_sandbox(self, session: aiohttp.ClientSession) -> str:
        """Get or create the session's Daytona sandbox and resolve its toolbox endpoint."""
        if self.last_sandbox_id:
            try:
                data = await self._platform_request(
                    session, "GET", f"/sandbox/{quote(self.last_sandbox_id, safe='')}", timeout=30
                )
                if isinstance(data, dict):
                    state = str(data.get("state") or "").lower()
                    if state not in _TERMINAL_STATES:
                        # Resumable states (stopped/archived) are handled inside
                        # wait_ready, which starts the sandbox and keeps its disk.
                        ready = await self.wait_ready(session, self.last_sandbox_id, timeout=90)
                        self.last_sandbox_id = str(ready.get("id") or self.last_sandbox_id)
                        return self.last_sandbox_id
            except DaytonaError:
                pass
            self.last_sandbox_id = ""
            self._toolbox_url = ""

        existing = await self.find_sandbox(session)
        if existing is not None:
            sandbox_id = str(existing.get("id") or self.sandbox_name)
            try:
                ready = await self.wait_ready(session, sandbox_id, timeout=90)
                self.last_sandbox_id = str(ready.get("id") or sandbox_id)
                return self.last_sandbox_id
            except DaytonaError:
                # A pre-existing sandbox that will not become ready (stuck /
                # broken) is unusable and would fail every operation forever.
                # Reclaim it so a fresh sandbox is created instead of letting
                # the session stay wedged. Workspace loss for this one broken
                # sandbox is preferable to a hard outage on every tool call.
                logger.warning(
                    "reclaiming stuck Daytona sandbox {} that did not become ready",
                    sandbox_id,
                )
                with suppress(Exception):
                    await self._platform_request(
                        session, "DELETE", f"/sandbox/{quote(sandbox_id, safe='')}", timeout=30
                    )
                self.last_sandbox_id = ""

        # Create fresh sandbox
        body: dict[str, Any] = {
            "name": self.sandbox_name,
            "snapshot": self.snapshot,
            "labels": {"app": "powerx", "managed-by": "nanobot"},
            "ttlMinutes": self.ttl_minutes,
        }
        # Network egress policy. Daytona applies these ONLY to newly created
        # sandboxes, and on a Tier 1/2 organization the API rejects them outright
        # ("Network access is restricted and cannot be overridden at the sandbox
        # level"), so the policy is best-effort: a rejection is remembered and the
        # sandbox is created with Daytona's org default instead of failing.
        network_body = self._network_policy_body()
        body.update(network_body)

        if self.auto_stop_minutes > 0:
            body["autoStopInterval"] = self.auto_stop_minutes

        try:
            created = await self._platform_request(session, "POST", "/sandbox", body=body, timeout=90)
        except DaytonaError as exc:
            err_msg = str(exc).lower()
            if _is_network_override_rejection(err_msg) and network_body:
                # Org-level restriction wins: remember it for this process and
                # recreate without any sandbox-level network override so the
                # user still gets a working (essential-services-only) sandbox.
                global _network_override_restricted  # noqa: PLW0603
                _network_override_restricted = True
                logger.warning(
                    "Daytona org is network-restricted at the organization level; "
                    "dropping sandbox network policy {} and creating without it. "
                    "Only Daytona essential services (package registries, GitHub, "
                    "model endpoints) are reachable. Configure "
                    "daytona.outbound_proxy_url to widen egress, or raise the org tier.",
                    sorted(network_body),
                )
                body = {k: v for k, v in body.items() if k not in _NETWORK_POLICY_KEYS}
                created = await self._platform_request(session, "POST", "/sandbox", body=body, timeout=90)
            elif "disk limit" in err_msg or "limit exceeded" in err_msg or "400" in err_msg:
                # Total disk limit exceeded (30GiB cap) or Bad Request on create:
                # reap stopped/orphaned sandboxes from earlier sessions and retry.
                reclaimed = await self.reclaim_orphaned_sandboxes(session)
                if reclaimed > 0:
                    created = await self._platform_request(session, "POST", "/sandbox", body=body, timeout=90)
                else:
                    raise
            elif "409" in err_msg or "already exists" in err_msg:
                # Name conflict: sandbox exists server-side; fetch and wait ready
                existing_meta = await self._platform_request(session, "GET", f"/sandbox/{quote(self.sandbox_name, safe='')}", timeout=30)
                if isinstance(existing_meta, dict):
                    created = existing_meta
                else:
                    raise
            else:
                raise

        if not isinstance(created, dict):
            raise DaytonaError("Daytona sandbox create returned invalid response")
        sandbox_id = str(created.get("id") or self.sandbox_name)
        ready = await self.wait_ready(session, sandbox_id, timeout=180)
        self.last_sandbox_id = str(ready.get("id") or sandbox_id)
        if self.persist_workspace:
            # A brand-new sandbox is empty. Restore the last workspace snapshot
            # so a session's files survive TTL reaping / recreation instead of
            # every restart starting from a wiped workspace.
            try:
                await self.restore_workspace()
            except Exception:
                pass  # best-effort: a missing/unreadable snapshot is not fatal
        return self.last_sandbox_id

    async def reclaim_orphaned_sandboxes(
        self, session: aiohttp.ClientSession, keep_names: set[str] | None = None
    ) -> int:
        """Reap stopped or terminal sandboxes to free up organization disk quota.

        Daytona's 30 GiB account limit counts all stopped sandboxes. When sandboxes
        accumulate over sessions, this removes inactive ones so new sandboxes can
        be created without manual intervention.
        """
        keep = set(keep_names or ())
        keep.add(self.sandbox_name)
        keep.add(self._archive_sandbox_name())
        deleted_count = 0
        try:
            raw = await self._platform_request(session, "GET", "/sandbox", timeout=30)
            items = (raw.get("items") if isinstance(raw, dict) else raw) if isinstance(raw, (dict, list)) else []
            if not isinstance(items, list):
                return 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                sid = str(item.get("id") or "")
                state = str(item.get("state") or "").lower()
                labels = item.get("labels") or {}
                is_px = name.startswith("px-") or (isinstance(labels, dict) and labels.get("app") == "powerx")
                if not is_px or name in keep:
                    continue
                if state in (_RESUMABLE_STATES | _TERMINAL_STATES | {"stopped", "archived", "error", "failed"}):
                    try:
                        target = sid or name
                        await self._platform_request(session, "DELETE", f"/sandbox/{quote(target, safe='')}", timeout=30)
                        deleted_count += 1
                    except Exception:
                        pass
        except Exception:
            pass
        return deleted_count

    async def delete_sandbox(self, sandbox_id_or_name: str) -> None:
        """Permanently delete a Daytona sandbox."""
        async with aiohttp.ClientSession() as session:
            await self._platform_request(
                session, "DELETE", f"/sandbox/{quote(sandbox_id_or_name, safe='')}", timeout=60
            )

    # ------------------------------------------------------------------ exec

    async def _exec(self, session: aiohttp.ClientSession, command: str, timeout: int) -> dict[str, Any]:
        result = await self._toolbox_request(
            session,
            "POST",
            "/process/execute",
            body={"command": command, "timeout": min(timeout, _MAX_TIMEOUT)},
            timeout=min(timeout, _MAX_TIMEOUT) + 30,
        )
        return result if isinstance(result, dict) else {"result": str(result)}

    @staticmethod
    def _render(result: dict[str, Any]) -> str:
        output = str(result.get("result") or result.get("output") or result.get("stdout") or "")
        code = result.get("exitCode") if "exitCode" in result else result.get("exit_code")
        text = output
        if code is not None and code != 0:
            text += f"\n[exit_code={code}]"
        return _truncate(text) or "(no output)"

    async def run(self, command: str, *, timeout: int = 120) -> str:
        command = str(command or "").strip()
        if not command:
            raise ValueError("command is required")
        if len(command) > _MAX_COMMAND_CHARS:
            raise ValueError(f"command exceeds {_MAX_COMMAND_CHARS} characters")
        async with aiohttp.ClientSession() as session:
            await self.ensure_sandbox(session)
            result = await self._exec(session, command, timeout)
        return self._render(result)

    async def read(self, path: str) -> str:
        target = _safe_path(path, self.workspace)
        async with aiohttp.ClientSession() as session:
            await self.ensure_sandbox(session)
            try:
                raw_bytes = await self._toolbox_request(
                    session,
                    "GET",
                    "/files/download",
                    params={"path": target},
                    timeout=90,
                    raw_response=True,
                )
                if isinstance(raw_bytes, (bytes, bytearray)):
                    try:
                        return _truncate(raw_bytes.decode("utf-8"))
                    except UnicodeDecodeError:
                        return _truncate(base64.b64encode(raw_bytes).decode("ascii"))
            except DaytonaFileNotFoundError:
                return ""
            except DaytonaError:
                pass
        # Fallback via base64 exec
        b64 = await self.run(f"base64 {shlex.quote(target)}")
        return _truncate(b64)

    async def write(self, path: str, content: str) -> None:
        target = _safe_path(path, self.workspace)
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError(f"content exceeds {_MAX_CONTENT_CHARS} characters")
        await self.write_bytes(target, content.encode("utf-8"))

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = _safe_path(path, self.workspace)
        if len(data) > _MAX_UPLOAD_BYTES:
            raise ValueError("file exceeds 200 MiB")
        parent = posixpath.dirname(target)
        async with aiohttp.ClientSession() as session:
            await self.ensure_sandbox(session)
            if parent and parent != "/":
                await self._exec(session, f"mkdir -p {shlex.quote(parent)}", 60)
            form = aiohttp.FormData()
            form.add_field("file", data, filename=posixpath.basename(target))
            try:
                await self._toolbox_request(
                    session,
                    "POST",
                    "/files",
                    params={"path": target},
                    data=form,
                    timeout=180,
                )
            except DaytonaError:
                # Fallback: base64 write via exec
                b64 = base64.b64encode(data).decode("ascii")
                await self._exec(session, f"echo {shlex.quote(b64)} | base64 -d > {shlex.quote(target)}", 90)

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
        """Fetch *url* into the sandbox workspace.

        Order of operations:

        1. **Host relay** (default). PowerX fetches the URL on the host — which
           has unrestricted egress — and writes the bytes into the sandbox. This
           is the only path that reaches arbitrary hosts on a network-restricted
           (Tier 1/Tier 2) Daytona organization, where the sandbox firewall
           permits essential services only.
        2. **Direct sandbox curl** (fallback). Used when the relay is disabled or
           declined the URL, preserving the previous behaviour for unrestricted
           organizations and for straightforward registry/mirror downloads.
        """
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        allowed = parsed.scheme in ("https", "http") and self._is_host_allowed(host)
        dest = _safe_path(dest_path, self.workspace)

        relay_error: str = ""
        if self.relay_enabled:
            try:
                written, result = await relay_fetch_into_backend(
                    self,
                    url,
                    dest,
                    timeout=timeout,
                    policy=self.relay_policy,
                )
                note = " (truncated at the relay size cap)" if result.truncated else ""
                logger.info(
                    "relayed {} -> {} ({} bytes{})",
                    url,
                    written,
                    result.size,
                    note,
                )
                return written
            except RelayError as exc:
                relay_error = str(exc)
                # A policy refusal is authoritative: fall through to the sandbox
                # curl only when the host allow list would have permitted it.
                if not allowed:
                    raise ValueError(relay_error) from None
                logger.warning("host relay declined {} ({}); trying sandbox curl", url, relay_error)

        if not allowed:
            raise ValueError(
                f"URL host {host!r} is not in the allowed fetch hosts list. "
                "Add it to the Daytona fetch_allow_hosts setting or set NANOBOT_DAYTONA_FETCH_ALLOW_HOSTS."
            )
        command = (
            f"mkdir -p {shlex.quote(posixpath.dirname(dest))} && "
            f"curl -fsSL --max-time {int(timeout)} -o {shlex.quote(dest)} {shlex.quote(url)} && "
            f"stat -c %s {shlex.quote(dest)}"
        )
        out = await self.run(command, timeout=min(timeout + 30, _MAX_TIMEOUT))
        if "[exit_code=" in out and "exit_code=0" not in out:
            raise DaytonaError(f"remote fetch failed: {out[:300]}")
        return dest

    # ------------------------------------------------------------------ relay

    @property
    def relay_spool_dir(self) -> str:
        """Sandbox-side directory the relay bridge watches for requests."""
        return f"{self.workspace}/.px-relay"

    def relay_status(self) -> dict[str, Any]:
        """Diagnostics for the relay, surfaced through reset/health checks."""
        return {
            "relay_enabled": self.relay_enabled,
            "relay_allow_hosts": sorted(self.relay_policy.allow_hosts)[:20],
            "relay_allow_http": self.relay_policy.allow_http,
            "relay_max_bytes": self.relay_policy.max_bytes,
            "network_override_restricted": daytona_network_override_restricted(),
            "outbound_proxy_configured": bool(self.outbound_proxy_url),
        }

    async def run_with_relay(self, command: str, *, timeout: int = 120) -> str:
        """Run a command directly.

        Commands that dial arbitrary hosts (``curl``/``wget`` to a
        non-essential host, ``git clone`` from an arbitrary origin) still cannot
        egress from a Tier 1/Tier 2 sandbox, because the organization firewall
        rejects every sandbox-level allow list. Use :meth:`fetch_url` for those
        downloads: the PowerX host performs the fetch and injects the bytes, so
        the sandbox never needs to dial out. This method therefore just runs the
        command, and exists so callers have a single documented entry point.
        """
        _ = self.relay_spool_dir
        return await self.run(command, timeout=timeout)

    async def download(self, remote_path: str, local_path: Any) -> Any:
        """Download a sandbox file to a local path."""
        target = _safe_path(remote_path, self.workspace)
        destination = Path(str(local_path)).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with aiohttp.ClientSession() as session:
            await self.ensure_sandbox(session)
            try:
                raw_bytes = await self._toolbox_request(
                    session,
                    "GET",
                    "/files/download",
                    params={"path": target},
                    timeout=120,
                    raw_response=True,
                )
                if isinstance(raw_bytes, (bytes, bytearray)):
                    destination.write_bytes(raw_bytes)
                    return destination
            except (DaytonaFileNotFoundError, DaytonaError):
                pass
        # Fallback via base64 exec
        encoded = await self.run(f"base64 {shlex.quote(target)}", timeout=120)
        payload = re.sub(r"\n\[exit_code=\d+\]\s*$", "", encoded).strip()
        try:
            raw = base64.b64decode(payload, validate=False)
        except Exception:
            raise DaytonaError("failed to decode downloaded artifact") from None
        destination.write_bytes(raw)
        return destination

    async def install_packages(self, packages: list[str], *, timeout: int = 600) -> str:
        cleaned = [item for item in packages if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.:@~=-]{0,127}", item)]
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

    async def test_connection(self) -> dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            sandbox_id = await self.ensure_sandbox(session)
            result = await self._exec(session, "uname -a", 60)
        exit_code = result.get("exitCode") if "exitCode" in result else result.get("exit_code")
        return {
            "ok": exit_code in (0, None),
            "backend": "daytona",
            "sandbox_id": sandbox_id,
            "platform": str(result.get("result") or result.get("output") or "").strip()[:200],
        }

    async def reset(self, sandbox_id: str | None = None) -> None:
        """Delete the sandbox immediately (explicit wipe / opt-out cleanup)."""
        async with aiohttp.ClientSession() as session:
            target = sandbox_id or ""
            if not target:
                existing = await self.find_sandbox(session)
                target = str((existing or {}).get("id") or self.sandbox_name)
            if target:
                await self._platform_request(session, "DELETE", f"/sandbox/{quote(target, safe='')}", timeout=60)
            # Reclaim any leftover stopped sandboxes to keep the 30 GiB quota free.
            await self.reclaim_orphaned_sandboxes(session)

    # ---------------------------------------------------------------- persist

    _SNAPSHOT_STAGED = ".px-snapshot.tgz"
    _RESTORE_STAGED = ".px-restore.tgz"
    _SNAPSHOT_MAX_BYTES = 150 * 1024 * 1024

    def _archive_sandbox_name(self) -> str:
        """Dedicated long-lived sandbox that stores workspace snapshots."""
        digest = hashlib.sha256(f"archive:{self.sandbox_name}".encode("utf-8")).hexdigest()[:10]
        return f"px-archive-{digest}"

    def _archive_backend(self) -> "DaytonaExecutionBackend":
        archive = DaytonaExecutionBackend(self.config, sandbox_name=self._archive_sandbox_name())
        # Snapshots must outlive the ephemeral session sandboxes, so the
        # archive sandbox uses the longest TTL the config schema accepts and
        # never stops early. It must never snapshot itself (infinite recurse).
        archive.ttl_minutes = 43_200
        archive.auto_stop_minutes = 0
        archive.persist_workspace = False
        return archive

    async def _download_bytes(self, session: aiohttp.ClientSession, path: str) -> bytes | None:
        """Read one file's raw bytes from the sandbox toolbox (None if missing)."""
        target = _safe_path(path, self.workspace)
        try:
            raw = await self._toolbox_request(
                session,
                "GET",
                "/files/download",
                params={"path": target},
                timeout=240,
                raw_response=True,
            )
        except (DaytonaFileNotFoundError, DaytonaError):
            return None
        return bytes(raw) if isinstance(raw, (bytes, bytearray)) else None

    async def snapshot_workspace(self) -> bool:
        """Tar the workspace and store the archive in the dedicated archive sandbox.

        Called instead of sandbox deletion when persistence is enabled, so
        writes and reads made during a task survive task end, agent restarts,
        and TTL reaping (the snapshot is restored whenever a fresh sandbox is
        created). Returns True when a snapshot was stored.
        """
        if not self.persist_workspace:
            return False
        staged = f"{self.workspace}/{self._SNAPSHOT_STAGED}"
        async with aiohttp.ClientSession() as session:
            await self.ensure_sandbox(session)
            await self._exec(
                session,
                f"rm -f {shlex.quote(staged)} && tar czf {shlex.quote(staged)} "
                f"-C {shlex.quote(self.workspace)} "
                f"--exclude=./{self._SNAPSHOT_STAGED} --exclude=./{self._RESTORE_STAGED} . "
                "2>/dev/null || true",
                300,
            )
            data = await self._download_bytes(session, staged)
            if not data or len(data) > self._SNAPSHOT_MAX_BYTES:
                return False
            archive = self._archive_backend()
            await archive.ensure_sandbox(session)
            await archive.write_bytes(f"{archive.workspace}/snapshots/{self.sandbox_name}.tgz", data)
        return True

    async def restore_workspace(self) -> bool:
        """Restore the last workspace snapshot from the archive sandbox (best effort)."""
        if not self.persist_workspace:
            return False
        try:
            archive = self._archive_backend()
            async with aiohttp.ClientSession() as session:
                await archive.ensure_sandbox(session)
                data = await archive._download_bytes(
                    session,
                    f"{archive.workspace}/snapshots/{self.sandbox_name}.tgz",
                )
                if not data or len(data) > self._SNAPSHOT_MAX_BYTES:
                    return False
                await self.ensure_sandbox(session)
                staged = f"{self.workspace}/{self._RESTORE_STAGED}"
                await self.write_bytes(staged, data)
                await self._exec(
                    session,
                    f"tar xzf {shlex.quote(staged)} -C {shlex.quote(self.workspace)} "
                    f"2>/dev/null || true; rm -f {shlex.quote(staged)}",
                    300,
                )
        except DaytonaError:
            return False
        return True
