"""Tests for the admin-managed provider pool storage."""

from __future__ import annotations

import json

import pytest

import nanobot.provider_pool as provider_pool


@pytest.fixture(autouse=True)
def _clear_pool_env(monkeypatch):
    monkeypatch.delenv(provider_pool.POOL_ENV_VAR, raising=False)


@pytest.fixture()
def pool_file(monkeypatch, tmp_path):
    path = tmp_path / "provider_pool.json"
    monkeypatch.setenv("PROVIDER_POOL_PATH", str(path))
    return path


def _entry(index: int) -> dict[str, str]:
    return {
        "baseUrl": f"https://host{index}.example.com/v1",
        "apiKey": f"key-{index:02d}-abcdefghij",
        "model": "gpt-4o-mini",
        "label": f"lane-{index}",
    }


def test_add_normalises_and_masks(pool_file) -> None:
    entry = provider_pool.add_entry(
        {
            "baseUrl": "https://api.example.com/v1/",
            "apiKey": "sk-secret-1234567890",
            "model": "gpt-4o-mini",
            "label": "one",
        }
    )
    assert entry["baseUrl"] == "https://api.example.com/v1"
    assert entry["id"]

    public = provider_pool.public_entries()
    assert public[0]["apiKeyMasked"] == "sk-s...7890"
    assert "sk-secret-1234567890" not in str(public)


def test_duplicate_rejected(pool_file) -> None:
    raw = {"baseUrl": "https://a.example.com", "apiKey": "k" * 20, "model": "m"}
    provider_pool.add_entry(raw)
    with pytest.raises(ValueError):
        provider_pool.add_entry(raw)


def test_cap_at_max_entries(pool_file) -> None:
    for index in range(provider_pool.MAX_POOL_ENTRIES):
        provider_pool.add_entry(_entry(index))
    assert len(provider_pool.load_pool()) == provider_pool.MAX_POOL_ENTRIES
    with pytest.raises(ValueError):
        provider_pool.add_entry(
            {"baseUrl": "https://overflow.example.com", "apiKey": "another-key-value", "model": "m"}
        )


def test_invalid_base_url_rejected(pool_file) -> None:
    with pytest.raises(ValueError):
        provider_pool.add_entry({"baseUrl": "ftp://nope", "apiKey": "k" * 12, "model": "m"})
    with pytest.raises(ValueError):
        provider_pool.add_entry({"baseUrl": "https://user:pass@host/v1", "apiKey": "k" * 12, "model": "m"})
    with pytest.raises(ValueError):
        provider_pool.add_entry({"baseUrl": "https://host/v1", "apiKey": "", "model": "m"})
    with pytest.raises(ValueError):
        provider_pool.add_entry({"baseUrl": "https://host/v1", "apiKey": "k" * 12, "model": ""})


def test_disable_remove_and_round_trip(pool_file) -> None:
    entry = provider_pool.add_entry(_entry(1))
    provider_pool.update_entry(entry["id"], {"enabled": False})
    assert provider_pool.enabled_entries() == []
    assert provider_pool.load_pool()[0]["enabled"] is False
    assert provider_pool.remove_entry(entry["id"]) is True
    assert provider_pool.load_pool() == []
    assert provider_pool.remove_entry("missing") is False


def test_save_is_atomic_no_temp_left(pool_file) -> None:
    provider_pool.add_entry(_entry(1))
    assert pool_file.exists()
    leftovers = [p.name for p in pool_file.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_env_var_is_a_durable_fallback(pool_file, monkeypatch) -> None:
    monkeypatch.setenv(provider_pool.POOL_ENV_VAR, json.dumps({"entries": [_entry(7)]}))
    entries = provider_pool.load_pool()
    assert len(entries) == 1
    assert entries[0]["label"] == "lane-7"


def test_file_cache_wins_over_env(pool_file, monkeypatch) -> None:
    provider_pool.add_entry(_entry(1))  # writes the on-disk cache
    monkeypatch.setenv(provider_pool.POOL_ENV_VAR, json.dumps({"entries": [_entry(7)]}))
    assert provider_pool.load_pool()[0]["label"] == "lane-1"


def test_malformed_env_falls_back_to_file(pool_file, monkeypatch) -> None:
    provider_pool.add_entry(_entry(1))
    monkeypatch.setenv(provider_pool.POOL_ENV_VAR, "{not json")
    assert provider_pool.load_pool()[0]["label"] == "lane-1"


def test_pool_env_json_round_trips(pool_file) -> None:
    provider_pool.add_entry(_entry(1))
    payload = json.loads(provider_pool.pool_env_json())
    assert payload["entries"][0]["label"] == "lane-1"
