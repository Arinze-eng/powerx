"""Host-side fetch relay for network-restricted sandbox backends.

Why this exists
---------------
Daytona restricts outbound egress by *organization tier*. On Tier 1/Tier 2 the
API refuses every sandbox-level network override::

    POST /sandbox {"domainAllowList": "..."}      -> HTTP 400
    POST /sandbox/{id}/network-settings           -> HTTP 400
    "Network access is restricted and cannot be overridden at the sandbox level."

Measured behaviour of such a sandbox (verified live, see
``docs/daytona-network-access.md``): package registries, GitHub, model APIs and
the other "essential services" are reachable, while arbitrary hosts are cut off.
Egress is enforced by an SNI-routing proxy: TCP connects to any IP succeed, but
TLS is reset whenever the presented SNI is not allowlisted, and plain HTTP to a
non-allowlisted host is answered with 403 by an inspecting proxy. IPv6, alternate
ports, CONNECT tunnelling through allowlisted hosts, and public URL-relay
services were all confirmed blocked. No allow list can widen that set, because
the organization policy overrides sandbox-level settings.

The workaround that does work is to invert the direction of the request: PowerX
already runs on infrastructure with unrestricted egress, so the *host* performs
the fetch and then pushes the bytes into the sandbox through the toolbox files
API. The sandbox never needs to dial the origin host.

Security
--------
Because the host has unrestricted egress, the host-side fetch is the real
enforcement point and is hardened accordingly:

* HTTPS only by default (``allow_http`` opts in for legacy mirrors).
* The destination host must pass the same ``fetch_allow_hosts`` allow list the
  sandbox tool used, so behaviour is predictable and shared.
* The resolved address must be public: loopback, private, link-local,
  multicast, reserved and cloud metadata endpoints are rejected, and every
  redirect hop is re-validated. This blocks SSRF against internal services.
* The download is size-capped and streamed so a hostile origin cannot exhaust
  memory.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlparse

import aiohttp
from loguru import logger

__all__ = [
    "RelayError",
    "RelayPolicy",
    "RelayResult",
    "HostRelayFetcher",
    "validate_relay_url",
]

# Default ceiling for a relayed download. Large enough for wheels, tarballs and
# model configs; small enough that a hostile origin cannot fill the sandbox disk.
DEFAULT_RELAY_MAX_BYTES = 256 * 1024 * 1024

# Redirects are followed manually so every hop can be re-validated.
_MAX_REDIRECTS = 5

# Hostnames that must never be reachable through the relay even if a wildcard
# allow list is configured.
_BLOCKED_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.google.com",
        "instance-data",
    }
)


class RelayError(RuntimeError):
    """Raised when a host-side relay fetch cannot be completed safely."""


@dataclass(frozen=True)
class RelayPolicy:
    """Enforcement rules applied to every host-side fetch."""

    allow_hosts: frozenset[str] = field(default_factory=frozenset)
    allow_http: bool = False
    max_bytes: int = DEFAULT_RELAY_MAX_BYTES
    allow_private: bool = False

    def host_allowed(self, host: str) -> bool:
        """Mirror of the sandbox fetch allow list semantics."""
        host = (host or "").lower().strip()
        if not host:
            return False
        if "*" in self.allow_hosts:
            return True
        if host in self.allow_hosts:
            return True
        return any(
            host.endswith("." + pattern[2:])
            for pattern in self.allow_hosts
            if pattern.startswith("*.")
        )


@dataclass
class RelayResult:
    """Outcome of a successful relay fetch."""

    url: str
    final_url: str
    status: int
    content_type: str
    data: bytes
    truncated: bool = False

    @property
    def size(self) -> int:
        return len(self.data)


def _is_public_address(raw: str) -> bool:
    """True when *raw* is a globally routable unicast address."""
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_private or addr.is_link_local:
        return False
    if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return False
    # 100.64.0.0/10 carrier-grade NAT and IPv4-mapped IPv6 handled explicitly.
    if isinstance(addr, ipaddress.IPv4Address) and addr in ipaddress.ip_network("100.64.0.0/10"):
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return _is_public_address(str(addr.ipv4_mapped))
    return True


async def _resolve_host(host: str, port: int) -> list[str]:
    """Resolve *host* to every address it currently maps to."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RelayError(f"could not resolve {host!r}: {exc}") from None
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0] not in addresses:
            addresses.append(sockaddr[0])
    return addresses


def validate_relay_url(url: str, policy: RelayPolicy) -> tuple[str, str]:
    """Validate scheme, allow list and hostname shape. Returns ``(url, host)``."""
    raw = str(url or "").strip()
    if not raw:
        raise RelayError("url is required")
    if len(raw) > 4096:
        raise RelayError("url is too long")
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    allowed_schemes = {"https"} if not policy.allow_http else {"https", "http"}
    if scheme not in allowed_schemes:
        raise RelayError(
            f"relay fetch requires {'/'.join(sorted(allowed_schemes))}; got {scheme or 'no'} scheme"
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise RelayError("url has no host")
    if host in _BLOCKED_HOSTS:
        raise RelayError(f"host {host!r} is not fetchable through the relay")
    if not policy.host_allowed(host):
        raise RelayError(
            f"URL host {host!r} is not in the allowed fetch hosts list. "
            "Add it to the Daytona fetch_allow_hosts setting or set "
            "NANOBOT_DAYTONA_FETCH_ALLOW_HOSTS."
        )
    return raw, host


class HostRelayFetcher:
    """Fetch a URL on the PowerX host and return its bytes.

    The host is not network-restricted, which is exactly why this can reach
    destinations a Tier 1/Tier 2 sandbox cannot. All SSRF and size safeguards
    live here rather than in the sandbox.
    """

    def __init__(self, policy: RelayPolicy, *, user_agent: str = "PowerX-Relay/1.0") -> None:
        self.policy = policy
        self.user_agent = user_agent

    async def _assert_public(self, host: str, port: int) -> None:
        if self.policy.allow_private:
            return
        addresses = await _resolve_host(host, port)
        if not addresses:
            raise RelayError(f"could not resolve {host!r}")
        for address in addresses:
            if not _is_public_address(address):
                raise RelayError(
                    f"refusing to relay {host!r}: resolves to non-public address {address}"
                )

    async def fetch(self, url: str, *, timeout: int = 120) -> RelayResult:
        """Fetch *url*, following redirects with re-validation and a size cap."""
        current, host = validate_relay_url(url, self.policy)
        headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        budget = max(5, int(timeout))
        client_timeout = aiohttp.ClientTimeout(total=budget, sock_connect=min(20, budget))
        try:
            async with aiohttp.ClientSession(timeout=client_timeout, headers=headers) as session:
                for hop in range(_MAX_REDIRECTS + 1):
                    parsed = urlparse(current)
                    port = parsed.port or (443 if parsed.scheme == "https" else 80)
                    await self._assert_public(parsed.hostname or "", port)
                    try:
                        async with session.get(current, allow_redirects=False) as resp:
                            if resp.status in (301, 302, 303, 307, 308):
                                location = resp.headers.get("Location")
                                if not location:
                                    raise RelayError(
                                        f"redirect from {current!r} had no Location header"
                                    )
                                current = aiohttp.helpers.URL(current).join(
                                    aiohttp.helpers.URL(location)
                                ).human_repr()
                                # Re-validate the hop against the allow list too.
                                current, _ = validate_relay_url(current, self.policy)
                                if hop >= _MAX_REDIRECTS:
                                    raise RelayError("too many redirects")
                                continue
                            if resp.status >= 400:
                                raise RelayError(
                                    f"origin returned HTTP {resp.status} for {current}"
                                )
                            data, truncated = await self._read_capped(resp)
                            return RelayResult(
                                url=url,
                                final_url=current,
                                status=resp.status,
                                content_type=resp.headers.get("Content-Type", ""),
                                data=data,
                                truncated=truncated,
                            )
                    except aiohttp.ClientError as exc:
                        raise RelayError(
                            f"relay transport error reaching {current}: {type(exc).__name__}"
                        ) from None
        except asyncio.TimeoutError:
            raise RelayError(f"relay fetch timed out after {budget}s") from None
        raise RelayError("relay fetch failed")

    async def _read_capped(self, resp: aiohttp.ClientResponse) -> tuple[bytes, bool]:
        """Stream the body, stopping at the configured cap."""
        cap = self.policy.max_bytes
        declared = resp.content_length
        if declared is not None and declared > cap:
            raise RelayError(
                f"remote file is {declared} bytes which exceeds the relay limit of {cap}"
            )
        chunks: list[bytes] = []
        total = 0
        truncated = False
        async for chunk in resp.content.iter_chunked(65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > cap:
                remaining = cap - (total - len(chunk))
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                truncated = True
                logger.warning("relay download truncated at {} bytes", cap)
                break
            chunks.append(chunk)
        return b"".join(chunks), truncated


def build_policy(
    allow_hosts: Iterable[str] | str | None,
    *,
    allow_http: bool = False,
    max_bytes: int = DEFAULT_RELAY_MAX_BYTES,
    allow_private: bool = False,
) -> RelayPolicy:
    """Build a :class:`RelayPolicy` from the comma-separated config value."""
    if isinstance(allow_hosts, str):
        hosts = {part.strip() for part in allow_hosts.split(",") if part.strip()}
    else:
        hosts = {str(part).strip() for part in (allow_hosts or []) if str(part).strip()}
    return RelayPolicy(
        allow_hosts=frozenset(hosts),
        allow_http=allow_http,
        max_bytes=max(1, int(max_bytes)),
        allow_private=allow_private,
    )


async def relay_fetch_into_backend(
    backend: Any,
    url: str,
    dest_path: str,
    *,
    timeout: int = 120,
    policy: RelayPolicy,
) -> tuple[str, RelayResult]:
    """Fetch *url* on the host and write the bytes into *backend*'s sandbox.

    Returns ``(written_path, result)``. The sandbox never dials the origin host,
    so destinations blocked by the sandbox firewall are still delivered.
    """
    fetcher = HostRelayFetcher(policy)
    result = await fetcher.fetch(url, timeout=timeout)
    written = await backend.write_bytes(dest_path, result.data)
    return (written if isinstance(written, str) else dest_path), result