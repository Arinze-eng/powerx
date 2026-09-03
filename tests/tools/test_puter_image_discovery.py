"""Ensures the Puter image tools are discovered & gated by the plugin loader.

Mirrors ``test_loop_tool_context.test_loop_registers_default_tools_in_injected_registry``:
construct a real AgentLoop and assert the injected registry gained the tools.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools import puter_image_tools
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus


def _make_supabase(configured: bool):
    class Fake:
        def __init__(self) -> None:
            self.configured = configured

    return Fake


@pytest.fixture
def configured_supabase(monkeypatch) -> None:
    monkeypatch.setattr(puter_image_tools, "SupabaseAuth", _make_supabase(True))


@pytest.mark.usefixtures("configured_supabase")
def test_tools_registered_when_supabase_configured(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    registry = ToolRegistry()
    AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        tool_registry=registry,
    )
    assert registry.has("generate_puter_image")
    assert registry.has("edit_puter_image")


def test_tools_skipped_when_supabase_not_configured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(puter_image_tools, "SupabaseAuth", _make_supabase(False))
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    registry = ToolRegistry()
    AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        tool_registry=registry,
    )
    assert not registry.has("generate_puter_image")
    assert not registry.has("edit_puter_image")