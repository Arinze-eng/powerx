"""Tests for the MT5 sandbox tool (compile / log / trade via the sandbox).

These tests pin the two properties that matter most:

1. **The host is never touched.** Every action must be forwarded to an execution
   sandbox; with no sandbox configured the tool must refuse rather than run
   Wine/MT5 locally.
2. **Live trading is opt-in.** ``order`` / ``close`` / ``close_all`` are blocked
   unless ``MT5_ALLOW_TRADING`` is enabled, while ``dry_run`` still previews the
   exact command.

The sandbox is faked, so the tests are fast and need no network or Wine.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.mt5_sandbox import (
    MT5SandboxTool,
    _parse_payload,
    bootstrap_command,
    build_cli_command,
)
from nanobot.agent.tools.registry import ToolRegistry


class _FakeSandbox:
    """Minimal stand-in for the novita/vps/runloop sandbox tool."""

    name = "novita_sandbox"

    def __init__(self, response: str = '{"ok": true, "value": 1}\n[exit_code=0]') -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.response


def _ctx(tools: dict[str, Any] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.tool_registry = tools or {}
    return ctx


# --------------------------------------------------------------------------- #
# registration / gating
# --------------------------------------------------------------------------- #
def test_tool_is_discoverable_and_named():
    assert MT5SandboxTool().name == "mt5_sandbox"


def test_enabled_requires_a_sandbox():
    assert MT5SandboxTool.enabled(_ctx({"novita_sandbox": _FakeSandbox()})) is True
    assert MT5SandboxTool.enabled(_ctx({})) is False
    assert MT5SandboxTool.enabled(None) is False


def test_create_carries_context():
    ctx = _ctx({"novita_sandbox": _FakeSandbox()})
    tool = MT5SandboxTool.create(ctx)
    assert isinstance(tool, MT5SandboxTool)
    assert tool._ctx is ctx


def test_schema_exposes_all_actions():
    params = MT5SandboxTool().parameters
    actions = set(params["properties"]["action"]["enum"])
    assert {"install", "start", "login", "compile", "logs", "experts"} <= actions
    assert {"order", "close", "close_all"} <= actions
    assert params["required"] == ["action"]


# --------------------------------------------------------------------------- #
# command construction
# --------------------------------------------------------------------------- #
def test_install_command_points_at_the_sandbox_installer():
    cmd = build_cli_command("install", {})
    assert "mt5_cli.py install" in cmd
    assert "--script" in cmd and "install_mt5_sandbox.sh" in cmd


def test_install_is_detached_by_default():
    """A full install outlives the sandbox command ceiling.

    The Novita tool caps every command at 900 s while Wine + MT5 + the bridge
    takes longer, so ``install`` must hand off to a detached process instead of
    holding one command open (which would be killed mid-prefix-build).
    """
    cmd = build_cli_command("install", {})
    assert "--foreground" not in cmd


def test_install_can_be_forced_to_the_foreground():
    cmd = build_cli_command("install", {"foreground": True})
    assert "--foreground" in cmd


def test_install_action_timeout_stays_under_the_sandbox_ceiling():
    """The install kick-off is a short call; only the detached work is long."""
    from nanobot.agent.tools.mt5_sandbox import _MAX_SANDBOX_COMMAND_TIMEOUT, _TIMEOUTS

    assert _TIMEOUTS["install"] <= _MAX_SANDBOX_COMMAND_TIMEOUT
    for action, value in _TIMEOUTS.items():
        assert value <= _MAX_SANDBOX_COMMAND_TIMEOUT, f"{action} exceeds the ceiling"


def test_status_is_a_pollable_read_only_action():
    from nanobot.agent.tools.mt5_sandbox import _READ_ONLY_ACTIONS

    assert "status" in _READ_ONLY_ACTIONS
    assert "status" in MT5SandboxTool().parameters["properties"]["action"]["enum"]
    assert "--lines 25" in build_cli_command("status", {})
    assert "--lines 5" in build_cli_command("status", {"lines": 5})


def _load_cli_module():
    """Import scripts/mt5_cli.py, which is not an importable package.

    The CLI lives outside the package tree (it is deployed into the sandbox as a
    standalone script), so it has to be loaded by path.
    """
    path = Path(__file__).resolve().parents[2] / "scripts" / "mt5_cli.py"
    spec = importlib.util.spec_from_file_location("mt5_cli_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_wine_env_never_sets_winedebug():
    """WINEDEBUG presence makes MetaTrader think a debugger is attached.

    Wine sets PEB heap-debug flags for any process while WINEDEBUG exists in the
    environment — even ``WINEDEBUG=-all``. mt5setup.exe then aborts with
    "A debugger has been found running in your system." and the install silently
    stalls at 0% CPU. The env builder must therefore strip it, never add it.
    """
    env = _load_cli_module().wine_env()
    assert "WINEDEBUG" not in env

    # ...and it must actively remove an inherited value too.
    os.environ["WINEDEBUG"] = "-all"
    try:
        assert "WINEDEBUG" not in _load_cli_module().wine_env()
    finally:
        del os.environ["WINEDEBUG"]


def test_installer_pins_wine_10_and_strips_winedebug():
    """The installer must pin Wine 10 and never pass WINEDEBUG to MT5.

    Wine 11 trips MetaTrader's anti-debug check; Wine 10 installs in ~30 s. Both
    facts were measured in a real sandbox, so they are pinned here to stop a
    regression that is very hard to diagnose from the outside (the failure looks
    like a silent hang rather than an error).
    """
    script = (Path(__file__).resolve().parents[2] / "scripts" / "install_mt5_sandbox.sh").read_text()

    # Wine 10 is pinned for all four packages; pinning only the metapackage
    # leaves wine-stable/amd64/i386 on 11.0 and MT5 still refuses to run.
    assert "winehq-stable=${pin}" in script
    assert "wine-stable=${pin}" in script
    assert "wine-stable-amd64=${pin}" in script
    assert "wine-stable-i386=${pin}" in script
    assert "--allow-downgrades" in script
    assert "MT5_WINE_SERIES" in script

    # WINEDEBUG must never be exported/set for the whole script...
    assert 'export WINEDEBUG=' not in script
    # ...and MT5 binaries are launched through the stripping helper.
    assert "env -u WINEDEBUG" in script


def test_wine_version_comparison_triggers_reinstall_outside_the_10_series():
    """A Wine too old OR too new must trigger the pinned reinstall.

    The guard is a range (not just "< 9"), because Wine 11 is *newer* than the
    required 10 yet still unusable for MT5.
    """
    script = (Path(__file__).resolve().parents[2] / "scripts" / "install_mt5_sandbox.sh").read_text()
    assert 'if [ "${WINE_MAJOR:-0}" -lt 9 ] || [ "${WINE_MAJOR:-0}" -ge 11 ]' in script


def test_start_command_seeds_login_and_portable_mode():
    cmd = build_cli_command(
        "start", {"login": 1111291280, "password": "pw", "server": "Forex Hedged USD"}
    )
    assert "--login 1111291280" in cmd
    assert "--server 'Forex Hedged USD'" in cmd
    # Portable mode is what makes the seeded login take effect.
    assert "--portable" in cmd


def test_start_without_credentials_omits_portable():
    cmd = build_cli_command("start", {"wait": 60})
    assert "--portable" not in cmd
    assert "--wait 60" in cmd


def test_order_command_includes_levels():
    cmd = build_cli_command(
        "order", {"symbol": "EURUSD", "side": "buy", "volume": 0.1, "sl": 1.05, "tp": 1.2}
    )
    assert "--symbol EURUSD" in cmd
    assert "--side buy" in cmd
    assert "--volume 0.1" in cmd
    assert "--sl 1.05" in cmd and "--tp 1.2" in cmd


def test_quote_accepts_space_separated_symbols():
    cmd = build_cli_command("quote", {"symbols": "EURUSD GBPUSD"})
    assert cmd.rstrip().endswith("EURUSD GBPUSD")


def test_candles_defaults_timeframe():
    cmd = build_cli_command("candles", {"symbol": "XAUUSD"})
    assert "--timeframe M15" in cmd


def test_bootstrap_downloads_both_scripts():
    cmd = bootstrap_command()
    assert "mt5_cli.py" in cmd
    assert "install_mt5_sandbox.sh" in cmd


# --------------------------------------------------------------------------- #
# payload parsing
# --------------------------------------------------------------------------- #
def test_parse_payload_extracts_json_from_noisy_output():
    rendered = "[warn] something\n{\"ok\": true, \"n\": 2}\n[exit_code=0]"
    assert _parse_payload(rendered) == {"ok": True, "n": 2}


def test_parse_payload_returns_none_without_json():
    assert _parse_payload("no json at all") is None
    assert _parse_payload("") is None


def test_parse_payload_handles_braces_inside_strings():
    rendered = '{"ok": true, "msg": "a } brace", "n": 1}'
    assert _parse_payload(rendered) == {"ok": True, "msg": "a } brace", "n": 1}


# --------------------------------------------------------------------------- #
# execute() behaviour
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_read_only_action_is_forwarded_to_the_sandbox():
    sandbox = _FakeSandbox('{"ok": true, "quotes": {}}\n[exit_code=0]')
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="quote", symbol="EURUSD")

    assert "quotes" in str(result)
    assert len(sandbox.calls) == 1
    assert "mt5_cli.py quote EURUSD" in sandbox.calls[0]["command"]
    # The CLI is refreshed before every use so a fixed bridge ships without a redeploy.
    assert "mt5_cli.py doctor" in sandbox.calls[0]["command"]


@pytest.mark.asyncio
async def test_no_sandbox_refuses_instead_of_running_locally():
    tool = MT5SandboxTool.create(_ctx({}))
    result = await tool.execute(action="positions")
    assert result.is_error
    assert "sandbox" in str(result).lower()


@pytest.mark.asyncio
async def test_trading_is_blocked_by_default(monkeypatch):
    monkeypatch.delenv("MT5_ALLOW_TRADING", raising=False)
    sandbox = _FakeSandbox()
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="order", symbol="EURUSD", side="buy", volume=0.1)

    assert result.is_error
    assert "MT5_ALLOW_TRADING" in str(result)
    assert sandbox.calls == [], "a blocked order must never reach the sandbox"


@pytest.mark.asyncio
async def test_trading_forwards_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    sandbox = _FakeSandbox('{"ok": true, "retcode": 10009}\n[exit_code=0]')
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="order", symbol="EURUSD", side="buy", volume=0.1)

    assert not getattr(result, "is_error", False)
    assert "mt5_cli.py order" in sandbox.calls[0]["command"]


@pytest.mark.asyncio
async def test_dry_run_previews_without_sending(monkeypatch):
    monkeypatch.delenv("MT5_ALLOW_TRADING", raising=False)
    sandbox = _FakeSandbox()
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(
        action="order", symbol="EURUSD", side="buy", volume=0.1, dry_run=True
    )

    assert not getattr(result, "is_error", False)
    assert "dry_run" in str(result)
    assert "EURUSD" in str(result)
    assert sandbox.calls == [], "dry_run must not contact the sandbox"


@pytest.mark.asyncio
async def test_unknown_action_is_rejected():
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))
    result = await tool.execute(action="definitely_not_real")
    assert result.is_error
    assert "Unknown action" in str(result)


@pytest.mark.asyncio
async def test_order_requires_symbol_side_and_volume(monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))

    missing_side = await tool.execute(action="order", symbol="EURUSD", volume=0.1)
    assert missing_side.is_error

    missing_volume = await tool.execute(action="order", symbol="EURUSD", side="buy")
    assert missing_volume.is_error


@pytest.mark.asyncio
async def test_compile_requires_a_file():
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))
    result = await tool.execute(action="compile")
    assert result.is_error
    assert "file" in str(result)


@pytest.mark.asyncio
async def test_non_json_output_reports_a_hint():
    sandbox = _FakeSandbox("curl: command failed [exit_code=1]")
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="positions")

    assert result.is_error
    assert "no JSON result" in str(result)
    assert "install" in str(result)


@pytest.mark.asyncio
async def test_failed_payload_surfaces_as_error():
    sandbox = _FakeSandbox('{"ok": false, "error": "mt5.initialize() failed"}\n[exit_code=2]')
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="account")

    assert result.is_error
    assert "initialize" in str(result)


@pytest.mark.asyncio
async def test_password_is_never_echoed_back(monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    sandbox = _FakeSandbox('{"ok": true, "password": "hunter2"}\n[exit_code=0]')
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="login", login=1, password="hunter2", server="S")

    assert "hunter2" not in str(result)


@pytest.mark.asyncio
async def test_sandbox_transport_failure_is_reported():
    class _Boom:
        name = "novita_sandbox"

        async def execute(self, **kwargs: Any) -> str:
            raise RuntimeError("sandbox offline")

    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _Boom()}))
    result = await tool.execute(action="doctor")
    assert result.is_error
    assert "sandbox call failed" in str(result).lower()


# --------------------------------------------------------------------------- #
# install / deploy wiring
# --------------------------------------------------------------------------- #
def test_sandbox_scripts_exist_in_the_repo():
    """The tool curls these from main; they must be real committed paths."""
    repo_root = Path(__file__).resolve().parents[2]
    assert (repo_root / "scripts" / "mt5_cli.py").is_file()
    assert (repo_root / "scripts" / "install_mt5_sandbox.sh").is_file()


def test_registry_can_resolve_the_tool(tmp_path):
    """The loader must be able to register the tool with a real ToolsConfig."""
    from nanobot.config.schema import ToolsConfig

    registry = ToolRegistry()
    tool = MT5SandboxTool.create(
        ToolContext(config=ToolsConfig(), workspace=str(tmp_path))
    )
    registry.register(tool)
    assert registry.has("mt5_sandbox")