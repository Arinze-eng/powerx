"""Tests for the alpaca_commands module."""

from __future__ import annotations

import pytest

from nanobot.trading.alpaca_commands import is_alpaca_command


def test_is_alpaca_connect_command():
    assert is_alpaca_command("/alpaca connect") is True
    assert is_alpaca_command("/alpaca connect API_KEY SECRET_KEY") is True


def test_is_alpaca_disconnect_command():
    assert is_alpaca_command("/alpaca disconnect") is True


def test_is_alpaca_status_command():
    assert is_alpaca_command("/alpaca status") is True


def test_is_not_alpaca_command():
    assert is_alpaca_command("/trade buy AAPL") is False
    assert is_alpaca_command("hello world") is False
    assert is_alpaca_command("/signin") is False


def test_alpaca_command_case_insensitive():
    assert is_alpaca_command("/Alpaca Connect") is True
    assert is_alpaca_command("/ALPACA CONNECT") is True
