"""Tests for the AlpacaTradeTool agent tool.

These verify the tool is auto-discovered and that credential resolution
works from environment variables without crashing. This protects against
the regression where ``_get_context()`` called a non-existent
``ToolContext.current()`` and broke every account/positions/buy/sell/close
action for the LLM.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from nanobot.agent.tools.alpaca_trade import AlpacaTradeTool
from nanobot.agent.tools.loader import ToolLoader


def test_alpaca_tool_auto_discovered():
    loader = ToolLoader()
    classes = loader.discover()
    names = {c.__name__ for c in classes}
    assert "AlpacaTradeTool" in names


def test_tool_has_expected_actions():
    tool = AlpacaTradeTool()
    schema = tool.parameters
    assert schema["type"] == "object"
    actions = schema["properties"]["action"]["enum"]
    assert set(actions) == {
        "analyze", "backtest", "buy", "sell", "positions", "account", "close",
    }


def test_get_credentials_from_env():
    with patch.dict(
        os.environ,
        {
            "ALPACA_API_KEY": "PK-test-key-abc",
            "ALPACA_SECRET_KEY": "secret-value",
            "ALPACA_BASE_URL": "https://paper-api.alpaca.markets",
        },
        clear=False,
    ):
        tool = AlpacaTradeTool()
        creds = tool._get_credentials()
        assert creds is not None
        assert creds["api_key"] == "PK-test-key-abc"
        assert creds["secret_key"] == "secret-value"
        assert creds["base_url"] == "https://paper-api.alpaca.markets"


def test_get_credentials_none_without_env():
    with patch.dict(
        os.environ,
        {"ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": ""},
        clear=False,
    ):
        tool = AlpacaTradeTool()
        creds = tool._get_credentials()
        assert creds is None


def test_unknown_action_returns_error():
    tool = AlpacaTradeTool()
    result = asyncio.run(tool.execute(action="not-a-real-action"))
    assert result.is_error is True