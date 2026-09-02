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
        creds = asyncio.run(tool._get_credentials())
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
        creds = asyncio.run(tool._get_credentials())
        assert creds is None


def test_get_credentials_prefers_per_user_over_env():
    """A resolved Telegram user gets ONLY their stored credentials — never the
    shared server env keys. This enforces per-user isolation and makes
    /alpaca disconnect effective."""
    from unittest.mock import AsyncMock, MagicMock, patch

    fake_store = MagicMock()
    fake_store.enabled = True
    fake_store.get_credentials = AsyncMock(
        return_value={
            "api_key": "PK-user-key-111",
            "secret_key": "user-secret",
            "base_url": "https://paper-api.alpaca.markets",
        }
    )

    with (
        patch.dict(
            os.environ,
            {
                "ALPACA_API_KEY": "PK-server-key-999",
                "ALPACA_SECRET_KEY": "server-secret",
            },
            clear=False,
        ),
        patch(
            "nanobot.trading.alpaca_credentials.AlpacaCredentialStore",
            return_value=fake_store,
        ),
        patch(
            "nanobot.agent.tools.alpaca_trade.AlpacaTradeTool._current_telegram_user_id",
            return_value=12345,
        ),
    ):
        tool = AlpacaTradeTool()
        creds = asyncio.run(tool._get_credentials())
        assert creds["api_key"] == "PK-user-key-111"
        assert creds["api_key"] != "PK-server-key-999"


def test_get_credentials_none_when_user_disconnected_even_with_env():
    """If a user has disconnected (no stored row), the tool returns None even
    when server env keys are present — wiring the /alpaca disconnect behavior."""
    from unittest.mock import AsyncMock, MagicMock, patch

    fake_store = MagicMock()
    fake_store.enabled = True
    fake_store.get_credentials = AsyncMock(return_value=None)

    with (
        patch.dict(
            os.environ,
            {
                "ALPACA_API_KEY": "PK-server-key-999",
                "ALPACA_SECRET_KEY": "server-secret",
            },
            clear=False,
        ),
        patch(
            "nanobot.trading.alpaca_credentials.AlpacaCredentialStore",
            return_value=fake_store,
        ),
        patch(
            "nanobot.agent.tools.alpaca_trade.AlpacaTradeTool._current_telegram_user_id",
            return_value=12345,
        ),
    ):
        tool = AlpacaTradeTool()
        creds = asyncio.run(tool._get_credentials())
        assert creds is None


def test_tool_registers_via_real_loader():
    """The tool must register through ToolLoader.load, the path the agent runtime
    uses. enabled() is a classmethod, so this also guards against the previous
    bug where an instance-method enabled() raised TypeError and the tool was
    silently dropped from the agent's tool set."""
    from unittest.mock import MagicMock

    from nanobot.agent.tools.context import ToolContext
    from nanobot.agent.tools.registry import ToolRegistry

    mock_config = MagicMock()
    mock_config.exec.enable = True
    mock_config.web.enable = True
    mock_config.image_generation.enabled = False
    mock_config.my.enable = False

    ctx = ToolContext(
        config=mock_config,
        workspace="/tmp",
        bus=MagicMock(),
        subagent_manager=MagicMock(),
        cron_service=MagicMock(),
        timezone="UTC",
    )
    registry = ToolRegistry()
    ToolLoader().load(ctx, registry)
    assert registry.has("alpaca_trade")


def test_unknown_action_returns_error():
    tool = AlpacaTradeTool()
    result = asyncio.run(tool.execute(action="not-a-real-action"))
    assert result.is_error is True