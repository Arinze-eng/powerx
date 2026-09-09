"""GET /api/version — public deployment identity (proves which build is live).

The whole point of this endpoint is answering "did my push actually deploy?"
without guessing. It must be unauthenticated, JSON, and report the running
commit + which zero-call cost layers are enabled.
"""

from __future__ import annotations

import json

from nanobot.webui.ws_http import _deployment_identity


class TestDeploymentIdentity:
    def test_shape(self) -> None:
        data = _deployment_identity()
        assert data["app"] == "powerx"
        assert isinstance(data["git_sha"], str) and data["git_sha"]
        assert set(data["cost_layers"]) == {
            "plan_cache",
            "tool_middleware",
            "deterministic_router",
        }
        # All three default-on in a normal build.
        assert all(v is True for v in data["cost_layers"].values())

    def test_json_serializable(self) -> None:
        json.dumps(_deployment_identity())  # must not raise

    def test_env_sha_preferred(self, monkeypatch) -> None:
        monkeypatch.setenv("GIT_SHA", "deadbeef")
        assert _deployment_identity()["git_sha"] == "deadbeef"

    def test_layers_reflect_env_kill_switches(self, monkeypatch) -> None:
        monkeypatch.setenv("POWERX_PLAN_CACHE", "0")
        monkeypatch.setenv("POWERX_TOOL_MIDDLEWARE", "off")
        layers = _deployment_identity()["cost_layers"]
        assert layers["plan_cache"] is False
        assert layers["tool_middleware"] is False
