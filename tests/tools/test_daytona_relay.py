"""Tests for the host-side fetch relay used by network-restricted backends.

The relay is the security boundary: it runs on the PowerX host, which has
unrestricted egress, so it must enforce its own allow list and refuse to touch
internal addresses. These tests cover both the policy logic and real end-to-end
fetching against a local server.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp import web

from nanobot.agent.tools.daytona_relay import (
    HostRelayFetcher,
    RelayError,
    RelayPolicy,
    build_policy,
    relay_fetch_into_backend,
    validate_relay_url,
)
from nanobot.agent.tools.daytona_relay import _is_public_address as is_public


# --------------------------------------------------------------------------- #
# Policy / allow list semantics                                                #
# --------------------------------------------------------------------------- #


def test_policy_exact_and_wildcard_matching() -> None:
    policy = build_policy("example.com,*.pythonhosted.org")
    assert policy.host_allowed("example.com") is True
    assert policy.host_allowed("EXAMPLE.COM") is True
    assert policy.host_allowed("files.pythonhosted.org") is True
    assert policy.host_allowed("a.b.pythonhosted.org") is True
    # A wildcard must not match the bare suffix itself.
    assert policy.host_allowed("pythonhosted.org") is False
    assert policy.host_allowed("evil.com") is False
    assert policy.host_allowed("") is False
    # A suffix match must be label-aligned, not a substring match.
    assert policy.host_allowed("notpythonhosted.org") is False


def test_policy_star_allows_everything() -> None:
    assert build_policy("*").host_allowed("anything.example") is True


def test_build_policy_from_iterable_and_str() -> None:
    assert build_policy(["a.com", " b.com "]).host_allowed("b.com") is True
    assert build_policy("a.com,b.com").host_allowed("a.com") is True
    assert build_policy(None).allow_hosts == frozenset()


# --------------------------------------------------------------------------- #
# URL validation                                                               #
# --------------------------------------------------------------------------- #


def test_validate_relay_url_rejects_non_https_by_default() -> None:
    policy = build_policy("example.com")
    with pytest.raises(RelayError, match="require"):
        validate_relay_url("http://example.com/x", policy)
    with pytest.raises(RelayError, match="require"):
        validate_relay_url("ftp://example.com/x", policy)
    assert validate_relay_url("https://example.com/x", policy)[1] == "example.com"


def test_validate_relay_url_allows_http_when_opted_in() -> None:
    policy = build_policy("example.com", allow_http=True)
    assert validate_relay_url("http://example.com/x", policy)[1] == "example.com"


def test_validate_relay_url_enforces_allow_list() -> None:
    policy = build_policy("example.com")
    with pytest.raises(RelayError, match="not in the allowed fetch hosts"):
        validate_relay_url("https://evil.test/x", policy)


def test_validate_relay_url_blocks_metadata_hosts_even_with_wildcard() -> None:
    policy = build_policy("*")
    with pytest.raises(RelayError, match="not fetchable"):
        validate_relay_url("https://metadata.google.internal/computeMetadata/v1/", policy)
    with pytest.raises(RelayError, match="not fetchable"):
        validate_relay_url("https://instance-data/latest/meta-data/", policy)


def test_validate_relay_url_requires_host_and_enforces_length() -> None:
    policy = build_policy("*")
    with pytest.raises(RelayError, match="no host"):
        validate_relay_url("https:///nohost", policy)
    with pytest.raises(RelayError, match="too long"):
        validate_relay_url("https://example.com/" + "a" * 5000, policy)
    with pytest.raises(RelayError, match="url is required"):
        validate_relay_url("", policy)


# --------------------------------------------------------------------------- #
# SSRF address screening                                                       #
# --------------------------------------------------------------------------- #


def test_private_and_internal_addresses_are_not_public() -> None:
    for address in (
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",  # cloud metadata (link-local)
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "224.0.0.1",  # multicast
        "::1",
        "fe80::1",
        "fc00::1",
    ):
        assert is_public(address) is False, address


def test_public_addresses_are_recognised() -> None:
    for address in ("93.184.215.14", "140.82.114.4", "8.8.8.8", "2606:4700::1111"):
        assert is_public(address) is True, address


@pytest.mark.asyncio
async def test_resolution_to_private_address_is_refused() -> None:
    """SSRF guard: a hostname that resolves to loopback must not be relayed."""
    policy = build_policy("*")  # even an open allow list must not enable SSRF
    fetcher = HostRelayFetcher(policy)
    with pytest.raises(RelayError, match="non-public address"):
        await fetcher.fetch("https://localhost:9/secret")


@pytest.mark.asyncio
async def test_private_origin_allowed_only_with_explicit_opt_in() -> None:
    """The documented escape hatch permits internal origins when configured.

    With the default policy an internal origin is refused by the SSRF guard;
    with ``allow_private`` the guard is skipped and the fetch proceeds to the
    transport layer instead.
    """
    strict = HostRelayFetcher(build_policy("*"))
    permissive = HostRelayFetcher(
        RelayPolicy(allow_hosts=frozenset({"*"}), allow_http=True, allow_private=True)
    )

    with pytest.raises(RelayError, match="non-public address"):
        await strict.fetch("https://127.0.0.1:1/secret")
    # Skipping the guard means we get a transport failure, not an SSRF refusal.
    with pytest.raises(RelayError) as excinfo:
        await permissive.fetch("http://127.0.0.1:1/secret", timeout=5)
    assert "non-public address" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# End-to-end fetching against a local origin                                   #
# --------------------------------------------------------------------------- #


async def _start_origin() -> tuple[web.AppRunner, str]:
    """A local origin with normal, redirecting, large and failing endpoints."""
    async def ok(request: web.Request) -> web.Response:
        return web.Response(body=b"hello-relay", content_type="text/plain")

    async def redirect_ok(request: web.Request) -> web.Response:
        raise web.HTTPFound("/ok")

    async def redirect_external(request: web.Request) -> web.Response:
        raise web.HTTPFound("https://evil.test/payload")

    async def big(request: web.Request) -> web.Response:
        return web.Response(body=b"x" * 5000)

    async def missing(request: web.Request) -> web.Response:
        raise web.HTTPNotFound()

    async def declared_big(request: web.Request) -> web.Response:
        return web.Response(body=b"y" * 5000, headers={"Content-Length": "5000"})

    async def chunked(request: web.Request) -> web.StreamResponse:
        """No Content-Length, so the size is only discoverable while streaming."""
        resp = web.StreamResponse()
        resp.content_type = "application/octet-stream"
        await resp.prepare(request)
        for _ in range(50):
            await resp.write(b"z" * 1000)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/ok", ok)
    app.router.add_get("/redirect-ok", redirect_ok)
    app.router.add_get("/redirect-external", redirect_external)
    app.router.add_get("/big", big)
    app.router.add_get("/missing", missing)
    app.router.add_get("/declared-big", declared_big)
    app.router.add_get("/chunked", chunked)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


def _local_policy(**kwargs: Any) -> RelayPolicy:
    return RelayPolicy(
        allow_hosts=frozenset({"127.0.0.1"}),
        allow_http=True,
        allow_private=True,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_fetch_returns_bytes_and_metadata() -> None:
    runner, base = await _start_origin()
    try:
        result = await HostRelayFetcher(_local_policy()).fetch(f"{base}/ok")
        assert result.data == b"hello-relay"
        assert result.status == 200
        assert result.truncated is False
        assert result.size == len(b"hello-relay")
        assert "text/plain" in result.content_type
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_fetch_follows_allowlisted_redirect() -> None:
    runner, base = await _start_origin()
    try:
        result = await HostRelayFetcher(_local_policy()).fetch(f"{base}/redirect-ok")
        assert result.data == b"hello-relay"
        assert result.final_url.endswith("/ok")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_fetch_refuses_redirect_to_disallowed_host() -> None:
    """Every redirect hop is re-validated, so a redirect cannot smuggle a host."""
    runner, base = await _start_origin()
    try:
        with pytest.raises(RelayError):
            await HostRelayFetcher(_local_policy()).fetch(f"{base}/redirect-external")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_fetch_truncates_undeclared_stream_at_size_cap() -> None:
    """When the origin hides its size, the stream is cut off at the cap."""
    runner, base = await _start_origin()
    try:
        result = await HostRelayFetcher(_local_policy(max_bytes=1024)).fetch(f"{base}/chunked")
        assert result.size == 1024
        assert result.truncated is True
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_fetch_rejects_oversized_declared_length() -> None:
    """A declared Content-Length over the cap fails fast, before download."""
    runner, base = await _start_origin()
    try:
        with pytest.raises(RelayError, match="exceeds the relay limit"):
            await HostRelayFetcher(_local_policy(max_bytes=1024)).fetch(f"{base}/declared-big")
        with pytest.raises(RelayError, match="exceeds the relay limit"):
            await HostRelayFetcher(_local_policy(max_bytes=1024)).fetch(f"{base}/big")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_fetch_surfaces_origin_errors() -> None:
    runner, base = await _start_origin()
    try:
        with pytest.raises(RelayError, match="HTTP 404"):
            await HostRelayFetcher(_local_policy()).fetch(f"{base}/missing")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_fetch_reports_transport_failure() -> None:
    """A closed port must surface as a RelayError, not an unhandled exception."""
    policy = RelayPolicy(allow_hosts=frozenset({"*"}), allow_http=True, allow_private=True)
    with pytest.raises(RelayError):
        await HostRelayFetcher(policy).fetch("http://127.0.0.1:1/unreachable", timeout=5)


# --------------------------------------------------------------------------- #
# Delivery into a backend                                                      #
# --------------------------------------------------------------------------- #


class _RecordingBackend:
    """Stand-in for a sandbox backend that records relayed writes."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes]] = []

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.writes.append((path, data))


@pytest.mark.asyncio
async def test_relay_fetch_into_backend_writes_bytes() -> None:
    runner, base = await _start_origin()
    try:
        backend = _RecordingBackend()
        path, result = await relay_fetch_into_backend(
            backend, f"{base}/ok", "/home/daytona/out.txt", policy=_local_policy()
        )
        assert path == "/home/daytona/out.txt"
        assert result.data == b"hello-relay"
        assert backend.writes == [("/home/daytona/out.txt", b"hello-relay")]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_fetch_times_out_on_stalled_origin() -> None:
    async def slow(request: web.Request) -> web.Response:
        await asyncio.sleep(30)
        return web.Response(body=b"late")

    app = web.Application()
    app.router.add_get("/slow", slow)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        with pytest.raises(RelayError, match="timed out"):
            await HostRelayFetcher(_local_policy()).fetch(
                f"http://127.0.0.1:{port}/slow", timeout=5
            )
    finally:
        await runner.cleanup()