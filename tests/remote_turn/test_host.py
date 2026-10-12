"""The host side reads the administrator's selection -- and refuses when it cannot.

The refusal is the interesting half. A sandbox handed *nothing* to run on -- no
pool, and no model that matches a configured provider -- would have to guess, so
sending the turn back to the host is the correct answer, and it is tested as
deliberately as the happy path.

A pool is the opposite case: it is not one credential but several, so what has to
be tested is that the *whole* lane set travels. A sandbox given one lane of a
pool, or given the saved entries without the host's own fallback lane, would run
a different rotation from the host's and nothing in the log would say so.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from nanobot.config.schema import Config
from nanobot.remote_turn import host as host_module
from nanobot.remote_turn.host import active_config, build_turn_request, provider_credentials


@pytest.fixture(autouse=True)
def _clear_config_cache() -> None:
    host_module._config_cache = None


def _config(**providers: Any) -> Config:
    config = Config()
    config.agents.defaults.provider = "deepseek"
    config.agents.defaults.model = "deepseek-chat"
    config.agents.defaults.model_preset = None
    for name, payload in providers.items():
        setattr(config.providers, name, payload)
    return config


def test_the_active_selection_is_read_from_the_config() -> None:
    from nanobot.config.schema import ProviderConfig

    config = _config(deepseek=ProviderConfig(api_key="sk-deep", api_base="https://api.deep.test"))

    credentials = provider_credentials(config)

    assert credentials is not None
    assert credentials.provider == "deepseek"
    assert credentials.model == "deepseek-chat"
    assert credentials.api_key == "sk-deep"
    assert credentials.api_base == "https://api.deep.test"


_LANES = [
    {"id": "a", "baseUrl": "https://one.test/v1", "apiKey": "k1", "model": "m", "enabled": True},
    {"id": "b", "baseUrl": "https://two.test/v1", "apiKey": "k2", "model": "m", "enabled": True},
]


def test_an_admin_pool_is_carried_rather_than_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Several lanes, no single provider. The turn still moves -- the pool goes
    # with it, so the sandbox rotates over the same endpoints the host would.
    monkeypatch.setattr(host_module, "active_pool", lambda _config: list(_LANES))

    request = build_turn_request(
        _config(), session_key="k", messages=[{"role": "user", "content": "hi"}]
    )

    assert request is not None
    assert request.pool == _LANES


def test_the_carried_pool_includes_the_hosts_own_fallback_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nanobot.config.schema import ProviderConfig
    from nanobot.provider_pool import POOL_ENV_VAR

    monkeypatch.setenv(POOL_ENV_VAR, json.dumps({"entries": _LANES}))
    monkeypatch.setenv("PROVIDER_POOL_PATH", str(tmp_path / "provider_pool.json"))

    carried = host_module.active_pool(_config(deepseek=ProviderConfig(api_key="sk-deep")))

    # The saved entries, then the main provider as the pool's last-resort lane --
    # the same two-part lane set the host's own ``make_provider`` rotates over.
    assert [entry["id"] for entry in carried] == ["a", "b", "admin-main"]
    assert carried[-1]["apiKey"] == "sk-deep"


def test_a_pool_is_not_needed_when_a_single_provider_is_identified() -> None:
    from nanobot.config.schema import ProviderConfig

    credentials = provider_credentials(_config(deepseek=ProviderConfig(api_key="sk-deep")))

    assert credentials is not None
    assert credentials.provider == "deepseek"


def test_nothing_to_run_on_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    # No pool and no identified provider: there is genuinely nothing the sandbox
    # could call, so the turn stays on the host.
    monkeypatch.setattr(host_module, "active_pool", lambda _config: [])
    config = _config()
    config.agents.defaults.provider = "auto"
    monkeypatch.setattr(type(config), "get_provider_name", lambda self, *a, **k: "auto")

    assert provider_credentials(config) is None


def test_a_request_is_built_from_the_transcript() -> None:
    from nanobot.config.schema import ProviderConfig

    request = build_turn_request(
        _config(deepseek=ProviderConfig(api_key="sk-deep")),
        session_key="telegram:9",
        messages=[{"role": "user", "content": "hi"}],
        channel="telegram",
        chat_id="9",
        max_iterations=5,
        turn_budget_s=60.0,
    )

    assert request is not None
    assert request.session_key == "telegram:9"
    assert request.channel == "telegram"
    assert request.max_iterations == 5
    assert request.turn_budget_s == 60.0
    assert request.credentials.api_key == "sk-deep"


def test_a_turn_with_no_transcript_is_not_dispatched() -> None:
    assert (
        build_turn_request(_config(), session_key="k", messages=[]) is None
    )


def test_active_config_is_re_read_when_the_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The admin page mutates the config under a running process; a cached read
    # would keep dispatching turns on the model that was active at boot.
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    calls: list[int] = []

    def _load() -> Config:
        calls.append(1)
        return _config()

    monkeypatch.setattr("nanobot.config.loader.get_config_path", lambda: path)
    monkeypatch.setattr("nanobot.config.loader.load_config", _load)

    first = active_config()
    assert active_config() is first
    assert len(calls) == 1

    # An explicit mtime: the test is about the cache noticing a change, not about
    # how finely the filesystem timestamps two writes in the same tick.
    path.write_text('{"a": 1}', encoding="utf-8")
    changed = path.stat().st_mtime_ns + 1_000_000
    os.utime(path, ns=(changed, changed))

    assert active_config() is not first
    assert len(calls) == 2


def test_a_missing_config_file_is_still_a_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nanobot.config.loader.get_config_path", lambda: tmp_path / "absent.json")

    assert active_config() is not None
