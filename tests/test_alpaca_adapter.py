"""Tests for the Alpaca adapter."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from nanobot.trading.alpaca_adapter import AlpacaCredentials, AlpacaError


def test_credentials_dataclass():
    creds = AlpacaCredentials(
        api_key="PKVUBWO7D6ZR6USUZNCB2NDKTC",
        secret_key="98bLCqXj9W3uWgptqjn1QLgUX64JnCLNkYXC7QQhnGXd",
        base_url="https://paper-api.alpaca.markets",
    )
    assert creds.api_key == "PKVUBWO7D6ZR6USUZNCB2NDKTC"
    assert creds.secret_key == "98bLCqXj9W3uWgptqjn1QLgUX64JnCLNkYXC7QQhnGXd"
    assert creds.base_url == "https://paper-api.alpaca.markets"


def test_adapter_raises_without_credentials():
    # Clear env to ensure no fallback
    with patch.dict(os.environ, {"ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": ""}, clear=False):
        with pytest.raises(AlpacaError, match="not configured"):
            from nanobot.trading.alpaca_adapter import AlpacaExecutionAdapter

            AlpacaExecutionAdapter()


def test_adapter_raises_without_alpaca_py():
    creds = AlpacaCredentials(api_key="test", secret_key="test")
    with patch("nanobot.trading.alpaca_adapter.HAS_ALPACA", False):
        with pytest.raises(AlpacaError, match="alpaca-py is not installed"):
            from nanobot.trading.alpaca_adapter import AlpacaExecutionAdapter

            AlpacaExecutionAdapter(creds)
