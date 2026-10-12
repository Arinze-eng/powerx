"""The configured main provider must stay in rotation alongside the pool.

The pool used to replace the configured provider outright, so once its lanes
were exhausted the caller saw a lane's error and the admin's own key was never
tried. These tests pin the fix: the main provider joins the pool as its last
lane, so an exhausted lane falls through to it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nanobot.config.loader import load_config
from nanobot.providers.base import LLMResponse
from nanobot.providers.factory import ADMIN_LANE_ID, make_provider
from nanobot.providers.pool_provider import PoolProvider

_MAIN_BASE = "https://main.example/v1"
_MAIN_KEY = "main-secret-key"
_MAIN_MODEL = "main-model"

_POOL_LANE = {
    "id": "lane-1",
    "baseUrl": "https://pool.example/v1",
    "apiKey": "pool-secret-key",
    "model": "pool-model",
    "label": "pool lane",
    "enabled": True,
}

_MESSAGES = [{"role": "user", "content": "hello"}]


def _config(tmp_path: Path):
    """A config shaped the way the deployment persists it."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": f"custom/{_MAIN_MODEL}"}},
                "providers": {
                    "custom": {"apiBase": _MAIN_BASE, "apiKey": _MAIN_KEY}
                },
            }
        ),
        encoding="utf-8",
    )
    return load_config(path)


def _pool_env(monkeypatch: pytest.MonkeyPatch, entries: list[dict]) -> None:
    monkeypatch.setenv("PROVIDER-POOL-JSON", json.dumps({"entries": entries}))


def test_pool_appends_the_main_provider_as_a_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _pool_env(monkeypatch, [_POOL_LANE])
    provider = make_provider(_config(tmp_path))

    assert isinstance(provider, PoolProvider)
    assert [entry["id"] for entry, _ in provider.lanes] == ["lane-1", ADMIN_LANE_ID]


def test_main_lane_is_not_added_twice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The main provider already in the pool is not duplicated as a lane."""
    _pool_env(
        monkeypatch,
        [
            dict(
                _POOL_LANE,
                id="lane-main",
                baseUrl=_MAIN_BASE,
                apiKey=_MAIN_KEY,
                model=_MAIN_MODEL,
            )
        ],
    )
    provider = make_provider(_config(tmp_path))

    assert isinstance(provider, PoolProvider)
    assert [entry["id"] for entry, _ in provider.lanes] == ["lane-main"]


def test_main_lane_answers_when_the_pool_lane_is_exhausted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reported failure: an exhausted pool lane must not be what the user sees.

    With the main provider in rotation, an out-of-credit pool lane falls through
    to the main key instead of surfacing its error.
    """
    _pool_env(monkeypatch, [_POOL_LANE])
    provider = make_provider(_config(tmp_path))
    assert isinstance(provider, PoolProvider)

    pool_entry, pool_lane = provider.lanes[0]
    main_entry, main_lane = provider.lanes[1]

    pool_lane.api_key = "pool-secret-key"

    async def _pool_chat(**_kwargs: object) -> LLMResponse:
        return LLMResponse(content=None, error_status_code=402)

    async def _main_chat(**_kwargs: object) -> LLMResponse:
        return LLMResponse(content="main-answer")

    monkeypatch.setattr(pool_lane, "chat", _pool_chat)
    monkeypatch.setattr(main_lane, "chat", _main_chat)

    assert pool_entry["id"] == "lane-1"
    assert main_entry["id"] == ADMIN_LANE_ID
    assert asyncio.run(provider.chat(_MESSAGES)).content == "main-answer"


def test_no_pool_means_the_main_provider_is_used_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PROVIDER-POOL-JSON", raising=False)
    provider = make_provider(_config(tmp_path))

    assert not isinstance(provider, PoolProvider)


def test_provider_signature_tracks_pool_lane_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pool edit must change the provider signature.

    The signature is how the runtime resolver decides whether a rebuilt provider
    replaces the live one. With the pool left out of it, disabling the lane with
    the rejected key in the admin panel was treated as "nothing changed": the
    gateway kept rotating over the lanes it froze at container start, so that
    lane's 401 kept being the answer until the next restart.
    """
    from nanobot.providers.factory import provider_signature

    config = _config(tmp_path)

    # Baseline: no pool configured at all. The environment is shared between
    # tests, so the variable has to be removed rather than assumed absent.
    monkeypatch.delenv("PROVIDER-POOL-JSON", raising=False)
    without_pool = provider_signature(config)

    _pool_env(monkeypatch, [_POOL_LANE])
    with_lane = provider_signature(config)
    assert with_lane != without_pool

    _pool_env(monkeypatch, [dict(_POOL_LANE, enabled=False)])
    disabled = provider_signature(config)
    assert disabled != with_lane

    # Back to the same state: the signature is stable, so unrelated refreshes
    # still leave the live provider untouched.
    _pool_env(monkeypatch, [_POOL_LANE])
    assert provider_signature(config) == with_lane

    monkeypatch.setenv("PROVIDER-POOL-DISABLED", "1")
    assert provider_signature(config) != with_lane


def _config_with_prompt_cache(tmp_path: Path, cache_mode: str) -> object:
    path = tmp_path / f"config-{cache_mode}.json"
    path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": f"custom/{_MAIN_MODEL}"}},
                "providers": {
                    "custom": {
                        "apiBase": _MAIN_BASE,
                        "apiKey": _MAIN_KEY,
                        "promptCache": cache_mode,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return load_config(path)


def test_pool_lanes_inherit_the_admin_prompt_cache_choice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The admin page's cache choice must reach the lanes that serve traffic.

    It is saved to ``providers.custom.prompt_cache`` and only the last-resort
    admin lane was built from that provider; the pool's own lanes were built
    with no ``prompt_cache`` at all and fell back to the default mode. On a
    pool-backed deployment those lanes answer every request, so the setting was
    dead exactly where it mattered.
    """
    _pool_env(monkeypatch, [_POOL_LANE])
    provider = make_provider(_config_with_prompt_cache(tmp_path, "markers"))

    assert isinstance(provider, PoolProvider)
    pool_entry, pool_lane = provider.lanes[0]
    assert pool_entry["id"] == "lane-1"
    assert pool_lane._cache_markers_for("pool-model") is True


def test_pool_lanes_leave_markers_off_when_the_admin_chose_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The control: with no saved mode the lane stays at the safe default."""
    _pool_env(monkeypatch, [_POOL_LANE])
    provider = make_provider(_config_with_prompt_cache(tmp_path, "auto"))

    assert isinstance(provider, PoolProvider)
    _, pool_lane = provider.lanes[0]
    assert pool_lane._cache_markers_for("pool-model") is False


def _config_with_effort(tmp_path: Path, effort: str) -> object:
    path = tmp_path / f"config-effort-{effort}.json"
    path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": f"custom/{_MAIN_MODEL}", "reasoningEffort": effort}},
                "providers": {"custom": {"apiBase": _MAIN_BASE, "apiKey": _MAIN_KEY}},
            }
        ),
        encoding="utf-8",
    )
    return load_config(path)


def test_pool_carries_the_admin_reasoning_effort_to_a_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The provider page's effort control has to reach a pool lane's call.

    The runner reads the effort off the runtime's generation settings and passes
    it to ``provider.chat``, so a pool that dropped it would leave the lanes
    thinking at the endpoint default while the admin page reported the saved
    value as live.
    """
    _pool_env(monkeypatch, [_POOL_LANE])
    provider = make_provider(_config_with_effort(tmp_path, "none"))

    assert isinstance(provider, PoolProvider)
    assert provider.generation.reasoning_effort == "none"

    _, pool_lane = provider.lanes[0]
    seen: dict[str, object] = {}

    async def _chat(**kwargs: object) -> LLMResponse:
        seen.update(kwargs)
        return LLMResponse(content="ok")

    monkeypatch.setattr(pool_lane, "chat", _chat)
    response = asyncio.run(
        provider.chat(_MESSAGES, reasoning_effort=provider.generation.reasoning_effort)
    )

    assert response.content == "ok"
    assert seen["reasoning_effort"] == "none"
