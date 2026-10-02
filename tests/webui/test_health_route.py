"""GET /api/health — the container health probe target.

The endpoint exists so a platform liveness probe can reach a real, code-backed
signal on the *exposed* port. The gateway's own ``/health`` listener binds
127.0.0.1 on the internal port, so it is unreachable from Northflank's probe;
everything the probe can see on the public port used to fall through to the
WebUI SPA, which answers 200 for any unknown path and therefore cannot fail.

These tests pin the two properties that make the endpoint useful as a probe:
it is reachable without auth, and it answers 503 (not 200) when memory pressure
is critical — a probe that can never fail is not a probe.
"""

from __future__ import annotations

import asyncio
import json

import nanobot.utils.memory_guard as memory_guard
from nanobot.webui.ws_http import GatewayHTTPHandler


def _uninitialised_handler() -> GatewayHTTPHandler:
    """Build the handler without __init__: the health path needs no services."""
    return object.__new__(GatewayHTTPHandler)


def _dispatch(path: str):
    """Route a path through the real dispatcher, bypassing only __init__."""
    return asyncio.run(_uninitialised_handler()._dispatch_resolved(None, None, path))


class TestHealthRoute:
    def test_route_is_wired_and_needs_no_services(self) -> None:
        # Reaching the handler at all proves the route is registered ahead of the
        # /api/ 404 fallback and needs no auth, session manager, or config.
        response = _dispatch("/api/health")
        assert response is not None
        assert response.status_code == 200

    def test_ok_pressure_answers_200(self, monkeypatch) -> None:
        monkeypatch.setattr(
            memory_guard,
            "memory_snapshot",
            lambda: {"pressure": "ok", "pct": 59.1, "used_mb": 288.7, "limit_mb": 488.3},
        )
        response = _dispatch("/api/health")
        payload = json.loads(response.body)
        assert response.status_code == 200
        assert payload["status"] == "ok"
        assert payload["pressure"] == "ok"

    def test_no_cgroup_limit_answers_200(self, monkeypatch) -> None:
        # A dev box (or any host without a cgroup limit) grades "unknown", and
        # "unknown" must not be read as unhealthy — that would restart a
        # perfectly fine container because the environment is unfamiliar.
        monkeypatch.setattr(
            memory_guard,
            "memory_snapshot",
            lambda: {"pressure": "unknown", "pct": None, "limit_mb": None},
        )
        response = _dispatch("/api/health")
        assert response.status_code == 200
        assert json.loads(response.body)["status"] == "ok"

    def test_critical_pressure_answers_503(self, monkeypatch) -> None:
        monkeypatch.setattr(
            memory_guard,
            "memory_snapshot",
            lambda: {
                "pressure": "critical",
                "pct": 93.4,
                "used_mb": 456.1,
                "limit_mb": 488.3,
                "rss_mb": 470.2,
                "cgroup_used_mb": 488.0,
            },
        )
        response = _dispatch("/api/health")
        payload = json.loads(response.body)
        # A 5xx is what makes Northflank terminate and replace the container.
        assert response.status_code == 503
        assert payload["status"] == "unhealthy"
        assert payload["pressure"] == "critical"
        assert payload["pct"] == 93.4

    def test_warn_pressure_still_answers_200(self, monkeypatch) -> None:
        # warn is headroom being spent, not a death sentence: restarting here
        # would kill in-flight turns that would otherwise finish.
        monkeypatch.setattr(
            memory_guard,
            "memory_snapshot",
            lambda: {"pressure": "warn", "pct": 84.0, "used_mb": 410.0, "limit_mb": 488.3},
        )
        response = _dispatch("/api/health")
        assert response.status_code == 200
        assert json.loads(response.body)["status"] == "ok"

    def test_telemetry_failure_fails_open(self, monkeypatch) -> None:
        # A telemetry bug must not restart the container. Fail open, report
        # unknown, keep serving.
        def _boom() -> dict:
            raise RuntimeError("cgroup unreadable")

        monkeypatch.setattr(memory_guard, "memory_snapshot", _boom)
        response = _dispatch("/api/health")
        assert response.status_code == 200
        assert json.loads(response.body) == {"status": "ok", "pressure": "unknown"}

    def test_reports_real_snapshot_fields(self) -> None:
        payload = json.loads(_dispatch("/api/health").body)
        # These are the fields worth having in the probe response when someone
        # is trying to work out why a container was restarted.
        for key in ("pressure", "pct", "used_mb", "limit_mb", "rss_mb", "cgroup_mb"):
            assert key in payload
