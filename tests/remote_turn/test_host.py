"""The host side reads the administrator's selection -- and refuses when it cannot.

The refusal is the interesting half. A sandbox handed one credential from an
admin pool would run on one lane of it: a different model, a different cost, and
nothing in the log to say so. Sending the turn back to the host is the correct
answer, so it is tested as deliberately as the happy path.
"""

from __future__ import annotations

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


def test_an_admin_pool_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Several lanes, one credential. Moving the turn would silently pick a lane.
    monkeypatch.setattr(
        "nanobot.providers.factory.make_pool_provider",
        lambda config, *, preset: object(),
    )

    assert provider_credentials(_config()) is None


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
