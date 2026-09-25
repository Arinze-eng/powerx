"""Tests for the MT5 sandbox tool (compile / log / trade via the sandbox).

These tests pin the properties that matter most:

1. **The host is never touched.** Every action must be forwarded to an execution
   sandbox; with no sandbox configured the tool must refuse rather than run
   Wine/MT5 locally.
2. **Live trading is opt-in.** ``order`` / ``close`` / ``close_all`` are blocked
   unless ``MT5_ALLOW_TRADING`` is enabled, while ``dry_run`` still previews the
   exact command.
3. **The installation rule is enforced.** Compiling an ``.mq5`` must require the
   full Wine + MT5 chain. Given a script with no chain installed, the tool must
   refuse and tell the model to ``install`` first — never let it look like an
   ordinary compilation error that the agent "fixes" in the source.

The sandbox is faked, so the tests are fast and need no network or Wine.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import sys
import time
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.mt5_sandbox import (
    _ALL_ACTIONS,
    _FLOAT_FIELDS,
    _INSTALL_COMMAND_TIMEOUT,
    _INT_FIELDS,
    _TIMEOUTS,
    BadNumberError,
    MT5SandboxTool,
    _normalize_numeric,
    _parse_payload,
    _server_is_known,
    bootstrap_command,
    build_cli_command,
)
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ToolsConfig


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


def test_enabled_is_true_even_without_a_sandbox_yet():
    """The tool must ALWAYS be registered.

    ``enabled()`` runs during loading, before the registry is populated, so a
    sandbox lookup here always fails and would silently drop the tool from the
    schema — which is exactly what happened: the model never saw `mt5_sandbox`
    and told users it could not compile MQL5. Availability is decided in
    execute(), which returns a clear error when no sandbox is configured rather
    than falling back to the host.
    """
    assert MT5SandboxTool.enabled(_ctx({"novita_sandbox": _FakeSandbox()})) is True
    assert MT5SandboxTool.enabled(_ctx({})) is True
    assert MT5SandboxTool.enabled(None) is True


def test_tool_is_advertised_in_the_registry_without_a_sandbox(tmp_path):
    """A real load must surface the tool even with no sandbox configured."""
    from nanobot.config.schema import ToolsConfig

    registry = ToolRegistry()
    registry.register(
        MT5SandboxTool.create(ToolContext(config=ToolsConfig(), workspace=str(tmp_path)))
    )
    assert registry.has("mt5_sandbox")


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


def test_wine_pin_is_discovered_rather_than_hardcoded():
    """The Wine pin must not hardcode one distro's codename.

    The apt version embeds the codename ("10.0.0.0~bookworm-1" on Debian,
    "~jammy-1" on Ubuntu). A hardcoded pin would fail to match on Ubuntu and the
    installer would quietly fall back to Wine 11 — silently reintroducing the
    anti-debug abort. So the version is resolved at runtime.
    """
    script = (Path(__file__).resolve().parents[2] / "scripts" / "install_mt5_sandbox.sh").read_text()

    assert "resolve_wine10_version" in script
    # Query apt for the real 10.x candidate...
    assert "apt-cache madison winehq-stable" in script
    # ...rather than defaulting to a fixed codename-suffixed string.
    assert 'MT5_WINE_VERSION:-10.0.0.0~bookworm-1' not in script
    # An unreachable repo must not silently install Wine 11.
    assert "would reintroduce exactly the anti-debug failure" in script


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


def test_symbols_action_is_read_only_and_discovers_instruments():
    """The agent must be able to ask what the server actually offers.

    Measured live (MetaQuotes-Demo, 2026-09-21): the agent reached for BTCUSD
    because crypto is the obvious 24/7 instrument, and MetaQuotes-Demo carries no
    crypto at all — every attempt failed with "symbol not found: (-4, 'Terminal:
    Not found')" and "copy_rates_from_pos failed: (-1, 'Terminal: Call failed')",
    which read like a broken bridge. Without a discovery action the trade step was
    unreachable by guessing.
    """
    from nanobot.agent.tools.mt5_sandbox import _READ_ONLY_ACTIONS

    assert "symbols" in _READ_ONLY_ACTIONS
    assert build_cli_command("symbols", {}).endswith("mt5_cli.py symbols")
    cmd = build_cli_command("symbols", {"tradable": True, "limit": 25, "filter": "USD"})
    assert "--tradable" in cmd
    assert "--limit 25" in cmd
    assert "--filter USD" in cmd


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


def test_bootstrap_cache_busts_the_raw_cdn():
    """GitHub raw served a stale CLI for minutes after a push.

    Measured: a fix already on main was still failing live because the sandbox
    kept downloading the previous revision, which sends debugging effort at code
    that is no longer running. Each URL needs a unique query string.
    """
    cmd = bootstrap_command()
    # A branch-name URL is cached BY PATH by the sandbox's egress, and neither a
    # query string nor Cache-Control defeats it. The primary source must be the
    # commit-pinned URL, resolved from the API.
    assert "api.github.com/repos/" in cmd
    assert "/$_sha/scripts" in cmd
    assert "--retry" in cmd
    # And the download is rejected unless it carries the version this tool needs.
    assert "CLI_VERSION = " in cmd
    assert "could not fetch mt5_cli.py version" in cmd


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

    result = await tool.execute(
        action="order", symbol="EURUSD", side="buy", volume=0.1, sl=1.05
    )

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
async def test_plan_gives_the_playbook_stop_and_target_with_no_sandbox_at_all(monkeypatch):
    """`plan` is arithmetic on the caller's numbers, so it needs no terminal.

    This is the property that matters: the question "what is my stop supposed to
    be?" is asked when the sandbox is unreachable or the terminal is down, and
    answering it with "no sandbox is configured" would be a worse tool for no
    gain. The stop and target must also be the playbook's, not the caller's.
    """
    monkeypatch.delenv("MT5_ALLOW_TRADING", raising=False)
    tool = MT5SandboxTool.create(_ctx({}))  # deliberately NO sandbox
    result = await tool.execute(
        action="plan", symbol="XAUUSD", side="buy", entry=4285.0,
        volume=0.1, equity=10302.92,
    )
    assert isinstance(result, str), result
    payload = json.loads(result)
    # 20 pips of 0.10 is $2.00; 1:7 is $14.00.
    assert payload["setup"]["sl"] == 4283.0
    assert payload["setup"]["tp"] == 4299.0
    assert payload["risk_money"] == 20.0
    assert payload["reward_money"] == 140.0
    assert payload["violations"] == []


@pytest.mark.asyncio
async def test_plan_does_not_place_anything_and_is_not_a_trading_action():
    """It must run with trading OFF and must reach no sandbox."""
    sandbox = _FakeSandbox()
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(
        action="plan", side="sell", entry=4285.0, volume=0.1,
    )
    assert isinstance(result, str), result
    assert sandbox.calls == [], "plan must not contact the sandbox"
    payload = json.loads(result)
    assert payload["setup"]["sl"] == 4287.0
    assert payload["setup"]["tp"] == 4271.0


@pytest.mark.asyncio
async def test_plan_refuses_without_an_entry_rather_than_inventing_one():
    """A stop derived from a price nobody gave is a fabricated level."""
    tool = MT5SandboxTool.create(_ctx({}))
    result = await tool.execute(action="plan", side="buy", volume=0.1)
    assert result.is_error
    assert "entry" in str(result)
    missing_side = await tool.execute(action="plan", entry=4285.0, volume=0.1)
    assert missing_side.is_error
    assert "side" in str(missing_side)


def test_plan_is_advertised_and_the_description_states_the_playbook():
    """The model reads the schema, not the module docstring."""
    params = MT5SandboxTool().parameters["properties"]
    assert "plan" in MT5SandboxTool().parameters["properties"]["action"]["enum"]
    for field in ("entry", "equity", "sl_pips", "rr", "range_low", "range_high"):
        assert field in params, field
    assert params["sl_pips"]["description"].startswith("action=plan: stop distance")
    desc = MT5SandboxTool().description
    assert "DEFAULT PLAYBOOK" in desc
    assert "action='plan'" in desc
    # The 10x trap has to be in the text the model actually reads.
    assert "a pip is 0.10" in desc


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


def test_registry_iteration_api_is_available():
    """The registry must expose mapping-style reads and be iterable.

    ``mt5_sandbox``/``arduino_verify`` resolve a PEER tool out of this registry at
    runtime. They previously did ``registry.values() if isinstance(registry, dict)
    else registry`` and iterated the result. ``ToolRegistry`` is not a dict and
    shipped no ``values()``/``__iter__``, so the lookup raised
    ``TypeError: 'ToolRegistry' object is not iterable``. The surrounding
    ``except Exception`` returned None, which reached the model as
    "No execution sandbox is configured" on a fully wired deployment.
    """
    registry = ToolRegistry()
    registry.register(
        MT5SandboxTool.create(ToolContext(config=ToolsConfig(), workspace="/tmp"))
    )

    # Must not raise, and must yield the registered tool.
    assert [t.name for t in registry] == ["mt5_sandbox"]
    assert [t.name for t in registry.values()] == ["mt5_sandbox"]
    assert registry.keys() == ["mt5_sandbox"]
    assert [name for name, _ in registry.items()] == ["mt5_sandbox"]
    assert registry.get("mt5_sandbox") is not None


def test_sandbox_lookup_resolves_a_peer_tool_by_name():
    """A configured sandbox must be found — not reported as 'no sandbox configured'.

    This is the regression that made the agent tell users it had no execution
    sandbox: the peer lookup silently returned None because the registry could not
    be iterated.
    """
    from nanobot.agent.tools.mt5_sandbox import _sandbox_tool

    class _Peer:
        name = "novita_sandbox"

    registry = ToolRegistry()
    registry.register(_Peer())

    ctx = ToolContext(config=ToolsConfig(), workspace="/tmp", tool_registry=registry)
    assert _sandbox_tool(ctx) is registry.get("novita_sandbox")


def test_sandbox_lookup_returns_none_when_absent():
    """With no sandbox registered the tool must still report None (honest error)."""
    from nanobot.agent.tools.mt5_sandbox import _sandbox_tool

    ctx = ToolContext(config=ToolsConfig(), workspace="/tmp", tool_registry=ToolRegistry())
    assert _sandbox_tool(ctx) is None
    assert _sandbox_tool(None) is None


def test_sandbox_lookup_survives_an_uniterable_registry():
    """A registry exposing only ``get`` must not raise inside the lookup."""
    from nanobot.agent.tools.mt5_sandbox import _sandbox_tool

    class _GetOnly:
        def __init__(self):
            self.tool = type("T", (), {"name": "novita_sandbox"})()

        def get(self, name):
            return self.tool if name == "novita_sandbox" else None

    ctx = ToolContext(config=ToolsConfig(), workspace="/tmp", tool_registry=_GetOnly())
    assert _sandbox_tool(ctx) is not None


def test_cli_always_exits_zero_so_novita_keeps_the_stdout():
    """REGRESSION: a non-zero exit code deletes the payload before the model sees it.

    Novita's command runner raises ``Command exited with status N`` for any
    non-zero exit and discards stdout. The CLI used to signal a refused compile
    with exit 5 and a real compilation error with exit 4, so BOTH arrived as a
    bare traceback with no JSON. A model handed an opaque transport failure
    invents an explanation — "the mt5_sandbox tool was not responding because the
    execution environment's MT5/Wine container was not initialized" — and tells
    the user to compile the .mq5 locally. Outcome must travel in the JSON only.
    """
    import subprocess
    import sys as _sys
    import tempfile

    cli = Path(__file__).resolve().parents[2] / "scripts" / "mt5_cli.py"
    with tempfile.TemporaryDirectory() as td:
        env = {
            **os.environ,
            "HOME": td,
            "MT5_ROOT": str(Path(td) / ".mt5"),
            "WINE_PREFIX": str(Path(td) / ".wine-mt5"),
        }
        src = Path(td) / "EA.mq5"
        src.write_text("//+---+\n//| test\n//+---+\n")
        proc = subprocess.run(
            [_sys.executable, str(cli), "compile", "--file", str(src)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    # The whole point: the exit code must not betray the payload.
    assert proc.returncode == 0, (
        f"non-zero exit {proc.returncode} makes Novita drop stdout; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["stage"] == "not_installed"
    assert payload["missing"]


def test_cli_reports_real_compile_failure_as_json_not_exit_code():
    """A genuine MetaEditor error must also arrive as JSON with exit code 0."""
    import subprocess
    import sys as _sys
    import tempfile

    cli = Path(__file__).resolve().parents[2] / "scripts" / "mt5_cli.py"
    with tempfile.TemporaryDirectory() as td:
        # Fabricate an installed chain, then let the compile fail for real.
        root = Path(td) / ".wine-mt5" / "drive_c" / "Program Files" / "MetaTrader 5"
        root.mkdir(parents=True)
        (root / "terminal64.exe").write_bytes(b"stub")
        (root / "MetaEditor64.exe").write_bytes(b"stub")  # capitalised, as MT5 ships
        winpy = Path(td) / ".wine-mt5" / "drive_c" / "Python311"
        winpy.mkdir(parents=True)
        (winpy / "python.exe").write_bytes(b"stub")
        src = Path(td) / "EA.mq5"
        src.write_text("this is not valid mql5;\n")
        env = {
            **os.environ,
            "HOME": td,
            "MT5_ROOT": str(Path(td) / ".mt5"),
            "WINE_PREFIX": str(Path(td) / ".wine-mt5"),
        }
        proc = subprocess.run(
            [_sys.executable, str(cli), "compile", "--file", str(src)],
            capture_output=True,
            text=True,
            env=env,
            timeout=240,
        )

    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    # Whatever the compile did, the model must get structured JSON to reason about.
    assert "ok" in payload


def test_metaeditor_resolves_despite_mixed_case(monkeypatch, tmp_path):
    """MT5 installs ``MetaEditor64.exe`` — the gate must find it, not call it missing."""
    _isolate_prefix(monkeypatch, tmp_path)
    root = tmp_path / ".wine-mt5" / "drive_c" / "Program Files" / "MetaTrader 5"
    root.mkdir(parents=True)
    (root / "terminal64.exe").write_bytes(b"stub")
    (root / "MetaEditor64.exe").write_bytes(b"stub")  # capitalised, exactly as shipped
    winpy = tmp_path / ".wine-mt5" / "drive_c" / "Python311"
    winpy.mkdir(parents=True)
    (winpy / "python.exe").write_bytes(b"stub")

    module = _load_cli_module()
    monkeypatch.setattr(module, "wine_bin", lambda: "python3")

    found = module.find_metaeditor()
    assert found is not None, "mixed-case MetaEditor64.exe must resolve"
    assert found.name == "MetaEditor64.exe"
    assert module.installed_chain()["installed"] is True


def test_find_exe_is_shallow_not_a_full_prefix_walk():
    """_find_exe must not recursively walk drive_c (that blew the compile timeout)."""
    module = _load_cli_module()
    # Check executable statements only — the docstring legitimately *describes*
    # the old rglob behaviour it replaced.
    src = inspect.getsource(module._find_exe)
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    body = code.split('"""')[-1]  # strip the docstring
    assert "iterdir" in body, "shallow scan expected"
    assert "rglob" not in body, "recursive walk in the hot path is the timeout bug"


def test_registry_can_resolve_the_tool(tmp_path):
    """The loader must be able to register the tool with a real ToolsConfig."""
    from nanobot.config.schema import ToolsConfig

    registry = ToolRegistry()
    tool = MT5SandboxTool.create(
        ToolContext(config=ToolsConfig(), workspace=str(tmp_path))
    )
    registry.register(tool)
    assert registry.has("mt5_sandbox")


# --------------------------------------------------------------------------- #
# installation rule (the regression: compile was attempted with no chain)
# --------------------------------------------------------------------------- #
def _isolate_prefix(monkeypatch, tmp_path):
    """Point the CLI at an empty HOME so no real Wine/MT5 chain is detected."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MT5_ROOT", str(tmp_path / ".mt5"))
    monkeypatch.setenv("WINE_PREFIX", str(tmp_path / ".wine-mt5"))
    monkeypatch.delenv("MT5_WIN_PYTHON", raising=False)


def test_installed_chain_reports_everything_missing_on_a_bare_box(monkeypatch, tmp_path):
    """A fresh sandbox must be reported as not installed, listing the gaps.

    ``installed_chain`` is what the gate reads. It checks the WHOLE chain, not
    just terminal64.exe: MetaEditor actually compiles, and the Windows python
    bridge is what makes the terminal usable, so a partial prefix must not pass.

    WINE IS STUBBED OUT, and it has to be: the wine arm of the chain is probed
    with ``which <wine_bin>`` against the REAL PATH, so on the developer host
    (where Wine is installed -- it is required to run this project's own tests
    against a live terminal) the assertion below failed with
    ``'wine' not in ['wine_prefix', 'terminal64.exe', ...]``. That failure said
    nothing about ``installed_chain`` and everything about the machine the suite
    ran on. ``_isolate_prefix`` already walls off HOME and the prefix; pointing
    ``wine_bin`` at a name nothing can resolve does the same for the launcher, so
    the test states its own precondition ("a bare box") instead of inheriting one.
    """
    _isolate_prefix(monkeypatch, tmp_path)
    module = _load_cli_module()
    monkeypatch.setattr(module, "wine_bin", lambda: "wine-not-installed-in-this-fixture")
    info = module.installed_chain()

    assert info["installed"] is False
    assert "wine" in info["missing"]
    assert "wine_prefix" in info["missing"]
    assert "terminal64.exe" in info["missing"]
    assert "metaeditor64.exe" in info["missing"]
    assert "windows_python" in info["missing"]


def test_compile_refuses_when_the_chain_is_missing(monkeypatch, tmp_path):
    """THE REGRESSION TEST.

    Handed an .mq5 with no chain installed, ``compile`` must refuse with
    ``stage="not_installed"`` and a machine-readable ``next`` step. Previously it
    probed only for the terminal and produced a bare failure, which the model read
    as "compile this script" — fixing MQL5 casually instead of installing
    Wine + MT5 first.
    """
    _isolate_prefix(monkeypatch, tmp_path)
    src = tmp_path / "MyEA.mq5"
    src.write_text("//+------------------------------------------------------------------+\n")

    module = _load_cli_module()
    args = module.build_parser().parse_args(["compile", "--file", str(src)])
    rc = module.cmd_compile(args)

    assert rc != 0, "a missing chain must not be reported as success"


def test_require_installed_chain_emits_the_install_directive(monkeypatch, tmp_path, capsys):
    """The gate must name the missing pieces AND the action to take."""
    import json

    _isolate_prefix(monkeypatch, tmp_path)
    module = _load_cli_module()

    rc = module.require_installed_chain("compile")
    assert rc is not None and rc != 0

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["stage"] == "not_installed"
    assert payload["missing"]
    assert "install" in payload["next"]
    # The message must steer to install, not to editing the source.
    assert "installation rules" in payload["error"]
    assert "install" in payload["error"]


def test_require_installed_chain_passes_when_the_chain_exists(monkeypatch, tmp_path):
    """With the chain present the gate must stay silent (return None)."""
    _isolate_prefix(monkeypatch, tmp_path)

    # Fabricate a complete-looking chain.
    terminal_dir = tmp_path / ".wine-mt5" / "drive_c" / "Program Files" / "MetaTrader 5"
    terminal_dir.mkdir(parents=True)
    (terminal_dir / "terminal64.exe").write_bytes(b"stub")
    (terminal_dir / "metaeditor64.exe").write_bytes(b"stub")
    winpy = tmp_path / ".wine-mt5" / "drive_c" / "Python311"
    winpy.mkdir(parents=True)
    (winpy / "python.exe").write_bytes(b"stub")

    module = _load_cli_module()
    monkeypatch.setattr(module, "wine_bin", lambda: "python3")

    info = module.installed_chain()
    assert info["installed"] is True, info["missing"]
    assert module.require_installed_chain("compile") is None


@pytest.mark.asyncio
async def test_not_installed_refusal_auto_provisions_instead_of_erroring(monkeypatch):
    """A missing chain must START the install, not return an error to route around.

    This is the regression that produced the user-visible failure. When ``compile``
    returned an error saying MT5 was not installed, the model did the "helpful"
    thing and told the user to compile the .mq5 in their own MetaEditor:

        "Since the mt5_sandbox (compiler) is currently unavailable to me, I have
         corrected the code for you below. You can copy this into your local
         MetaEditor and compile it."

    An error invites a workaround, so the tool now performs the mandatory first
    step itself and returns a *provisioning started* result, which leaves polling
    as the only next action.
    """
    # The wait budget is 25 minutes in production, and the fake sandbox answers every
    # status poll with the same non-terminal payload, so this test would otherwise
    # stall the suite until that budget expired. Zero keeps exactly the part under
    # test: the kick-off, and the refusal to hand the model an error to route around.
    monkeypatch.setenv("MT5_INSTALL_WAIT_SECONDS", "0")
    payload = (
        '{"ok": false, "stage": "not_installed", "missing": ["wine", "metaeditor64.exe"],'
        ' "error": "chain missing"}\n[exit_code=5]'
    )
    sandbox = _FakeSandbox(payload)
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="compile", file="/home/user/MyEA.mq5")

    import json as _json

    body = _json.loads(str(result))
    assert body["ok"] is False
    assert body["stage"] == "installing"
    assert body["auto_provisioned"] is True
    assert body["requested_action"] == "compile"

    # The install must actually have been kicked off in the sandbox: the refusal
    # costs one forwarded call, auto-provisioning the install costs a second.
    assert len(sandbox.calls) == 2, f"expected refusal + install kick: {sandbox.calls}"
    second = str(sandbox.calls[1].get("command", ""))
    assert "install" in second

    # And the guidance must forbid the old escape hatches.
    text = str(result).lower()
    assert "do not" in text
    assert "local metaeditor" in text or "compile it locally" in text
    assert body["stage"] == "installing"


@pytest.mark.asyncio
async def test_install_action_waits_without_re_issuing_itself():
    """action='install' waits for its OWN install, and must not restart it.

    A detached start is work in flight, not a result, so the tool watches it to a
    terminal stage inside this call. This pins the two properties that keeps true:
    the wait polls ``status``, and it never fires a second install -- the
    re-provisioning path exists for actions that need a chain, not for install
    itself, and a self-restarting install is an install that never finishes.
    """
    sandbox = _QueuedSandbox(
        [
            '{"ok": true, "detached": true, "pid": "14144"}\n[exit_code=0]',
            '{"ok": true, "stage": "done", "installed": true}\n[exit_code=0]',
        ]
    )
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="install")
    rendered = str(result)

    installs = [c for c in sandbox.calls if "mt5_cli.py install" in str(c["command"])]
    assert len(installs) == 1, "install must not re-issue itself"
    assert '"stage": "done"' in rendered
    assert '"detached": true' not in rendered


def test_tool_description_mandates_install_before_compile():
    """The schema text is the agent's primary instruction — it must say install first."""
    desc = MT5SandboxTool().description
    assert "install" in desc
    assert "MANDATORY" in desc.upper()
    assert "not_installed" in desc


def test_prompt_template_states_the_installation_rule():
    """sandbox_workspace.md drives behaviour before any skill is loaded."""
    template = (
        Path(__file__).resolve().parents[2]
        / "nanobot" / "templates" / "agent" / "sandbox_workspace.md"
    ).read_text()

    assert "INSTALLATION RULE" in template
    assert "MANDATORY FIRST" in template
    # It must explicitly forbid the wrong behaviour.
    assert "not_installed" in template
    assert "NOT a source-code problem" in template


def test_skill_documents_the_installation_rule():
    """The mt5-trading playbook must carry the same rule."""
    skill = (
        Path(__file__).resolve().parents[2]
        / "nanobot" / "skills" / "mt5-trading" / "SKILL.md"
    ).read_text()

    assert "installation rule" in skill.lower()
    assert "not_installed" in skill
    assert "Never" in skill

# --------------------------------------------------------------------------- #
# broker login: the servers.dat trap
# --------------------------------------------------------------------------- #
def test_broker_installer_url_is_forwarded_to_the_installer():
    """A broker-branded installer is the ONLY way a real broker can log in.

    MEASURED FAILURE (2026-09-22, real Runloop devbox): MetaQuotes' GENERIC
    terminal ships a ``Config/servers.dat`` with no broker entries (50 544 B,
    containing only the MetaQuotes copyright). A broker server name such as
    ``Exness-MT5Trial9`` therefore has nothing to resolve to, MT5 silently skips
    the connection, the terminal log gets ZERO ``Network`` lines, and the bridge
    reports ``-10005 IPC timeout`` -- an error that points at Wine/IPC and sends
    you debugging the wrong layer. The Exness-branded installer embeds the broker
    servers (234 324 B) and logs in first try.

    This pins that the tool passes the URL and directory name through as the
    env vars the installer already reads.
    """
    cmd = build_cli_command(
        "install",
        {
            "broker_installer_url": "https://download.mql5.com/cdn/web/exness.technologies.ltd/mt5/exness5setup.exe",
            "broker_dir_name": "MetaTrader 5 EXNESS",
        },
    )
    assert "MT5_BROKER_INSTALLER_URL=" in cmd
    assert "exness5setup.exe" in cmd
    assert "MT5_BROKER_DIR_NAME=" in cmd
    assert "MetaTrader 5 EXNESS" in cmd


def test_install_without_broker_url_emits_no_env_override():
    """No broker passed -> the CLI sets nothing, so the *script's* default wins.

    The script defaults to the broker-branded build (see
    ``test_installer_script_defaults_to_the_broker_build``); the CLI must not
    override it with an empty value, which would force the generic terminal.
    """
    cmd = build_cli_command("install", {})
    assert "MT5_BROKER_INSTALLER_URL" not in cmd
    assert "MT5_BROKER_DIR_NAME" not in cmd


def test_installer_script_defaults_to_the_broker_build():
    """A bare install must produce a terminal that can actually log in.

    MEASURED FAILURE (2026-09-22): with ``MT5_BROKER_INSTALLER_URL`` defaulting to
    empty, a bare install fetched MetaQuotes' generic build, whose ``servers.dat``
    has no broker entries. ``Exness-MT5Trial9`` then had nothing to resolve to, so
    MT5 skipped the connection silently -- zero ``Network`` log lines, then
    ``-10005 IPC timeout`` from the bridge. Two sandboxes were spent on this.

    So the default is now the deployment's broker (Exness), and the generic build
    is an explicit opt-in.
    """
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "install_mt5_sandbox.sh"
    ).read_text(encoding="utf-8")
    assert (
        'MT5_BROKER_INSTALLER_URL="${MT5_BROKER_INSTALLER_URL:-https://download.mql5.com/'
        'cdn/web/exness.technologies.ltd/mt5/exness5setup.exe}"' in script
    ), "the installer script must default to the Exness-branded MT5 build"
    # ...and the generic build must remain reachable on request.
    assert 'MT5_GENERIC_INSTALLER' in script


def test_tool_schema_advertises_the_broker_installer_params():
    props = MT5SandboxTool().parameters["properties"]
    assert "broker_installer_url" in props
    assert "broker_dir_name" in props


def test_find_terminal_prefers_the_broker_branded_build(tmp_path, monkeypatch):
    """Both terminals coexist; the generic one must never win.

    The branded installer refuses to overwrite a generic install, so a prefix can
    hold ``MetaTrader 5`` AND ``MetaTrader 5 EXNESS`` at once. An unqualified
    ``rglob("terminal64.exe")`` returns whichever the filesystem lists first --
    and picking the generic build silently reintroduces the no-authorization
    failure. Ordering is therefore explicit.
    """
    cli = _load_cli_module()

    generic = tmp_path / "drive_c" / "Program Files" / "MetaTrader 5"
    branded = tmp_path / "drive_c" / "Program Files" / "MetaTrader 5 EXNESS"
    for d in (generic, branded):
        d.mkdir(parents=True)
        (d / "terminal64.exe").write_bytes(b"MZ")

    monkeypatch.setattr(cli, "WINE_PREFIX", tmp_path)
    monkeypatch.setattr(cli, "TERMINAL_MARKER", tmp_path / "marker")
    monkeypatch.setattr(cli, "DISPLAY_NUM", "99")

    assert cli.find_terminal() == branded / "terminal64.exe"


def test_broker_server_probe_flags_the_generic_terminal(monkeypatch, tmp_path):
    """The probe that turns a silent failure into a loud one.

    Size is the FAST PATH, not the whole test: servers.dat is not plain text (a
    string scan finds just the copyright), so 234 KB is enough to call a build
    branded without asking anything else. It is NOT enough to call one generic --
    MEASURED 2026-09-22: Deriv's branded servers.dat is 43,804 B, i.e. SMALLER
    than the generic ~50 KB, so a bare size comparison reported "no broker
    servers" about a terminal that was streaming 722 Deriv symbols. A small file
    therefore falls through to the registry, which names the build from its own
    install record / directory.

    THE PREFIX IS ISOLATED, and it has to be: without that, the ARM of
    ``installed_broker_key`` that reads ``~/.mt5/.broker_key`` sees the
    developer's own REAL install. On a host with a live Deriv terminal this test
    failed with ``assert True is False`` for the generic fixture -- the probe was
    correctly reading ``.broker_key=deriv`` and answering about that, not about
    the temp directory the test built.
    """
    _isolate_prefix(monkeypatch, tmp_path)
    cli = _load_cli_module()

    generic = tmp_path / "MetaTrader 5"
    (generic / "Config").mkdir(parents=True)
    (generic / "terminal64.exe").write_bytes(b"MZ")
    (generic / "Config" / "servers.dat").write_bytes(b"\x00" * 50_544)
    assert cli._terminal_has_broker_servers(generic / "terminal64.exe") is False

    branded = tmp_path / "MetaTrader 5 EXNESS"
    (branded / "Config").mkdir(parents=True)
    (branded / "terminal64.exe").write_bytes(b"MZ")
    (branded / "Config" / "servers.dat").write_bytes(b"\x00" * 234_324)
    assert cli._terminal_has_broker_servers(branded / "terminal64.exe") is True

    assert cli._terminal_has_broker_servers(None) is None


def test_cli_version_matches_the_tool_pin():
    """The bootstrap refuses a CLI whose marker does not match this tool."""
    from nanobot.agent.tools.mt5_sandbox import _CLI_VERSION

    assert _load_cli_module().CLI_VERSION == _CLI_VERSION


# --------------------------------------------------------------------------- #
# the wine64 launcher trap
# --------------------------------------------------------------------------- #
def test_installer_probes_wine_instead_of_trusting_which():
    """``command -v wine`` succeeds for a binary that cannot execute.

    MEASURED FAILURE (2026-09-22, Runloop devbox): WineHQ's /usr/bin/wine is a
    32-bit ELF. That kernel has no IA32 emulation (/proc/sys/abi/ldt16 absent,
    ia32 missing from /proc/cpuinfo), so even the 32-bit loader fails with
    "cannot execute binary file: Exec format error". ``command -v`` still
    returned 0, the script selected the dead launcher, wineboot died instantly,
    and the ERR trap reported "exited with code 2" while the log's last line
    still read "initialising wine prefix" -- which reads like an OOM, not a
    broken launcher. The launcher must be PROBED.
    """
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "install_mt5_sandbox.sh"
    ).read_text()

    assert "_wine_works" in script
    # The probe must actually run the binary, not just stat it.
    assert '"$bin" --version' in script
    # Selection must consult the probe, not a bare command -v.
    assert 'for _cand in wine wine64' in script
    assert 'if _wine_works "$_cand"' in script
    # The MQL5-library launch must use the resolved launcher, never a bare `wine`.
    assert 'nohup "$WINE_BIN"' in script
    assert 'nohup wine "' not in script


def test_installer_starts_winbindd_not_just_installs_it():
    """The package on disk is not a running daemon.

    Wine's named-pipe support for the terminal needs winbind. Installing the
    ``winbind`` package only drops binaries; nothing starts the daemon and no
    smb.conf exists, so the bridge reports ``-10005 IPC timeout`` against a
    terminal that is otherwise perfectly healthy.
    """
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "install_mt5_sandbox.sh"
    ).read_text()

    assert "winbindd" in script
    assert "pgrep -x winbindd" in script
    assert "/usr/sbin/winbindd -D" in script
    assert "smb.conf" in script


def test_installer_waits_on_the_directory_it_actually_installed():
    """A bare find can match the OTHER terminal and report false success."""
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "install_mt5_sandbox.sh"
    ).read_text()
    assert "TERM_DISPLAY_NAME" in script
    assert 'Program Files/${TERM_DISPLAY_NAME}' in script


# --------------------------------------------------------------------------- #
# broker-agnostic installs: the server decides the build
# --------------------------------------------------------------------------- #
def _broker_cli(monkeypatch, tmp_path, *, broker_key=None, brands=(), broker_builds=None):
    """Load the CLI against an isolated prefix that has ``brands`` installed.

    ``broker_key`` is what the installer would have recorded in ``.broker_key``;
    None means "an install from before that record existed", which the CLI has to
    recognise from the install directory name instead.

    ``broker_builds`` is the raw ``MT5_BROKER_BUILDS`` value a deployment would
    export. It is passed through here rather than set by the caller because this
    helper clears that variable so the built-in registry is what the other tests
    see.
    """
    import os as _os

    prefix = tmp_path / ".wine-mt5"
    for brand in brands:
        d = prefix / "drive_c" / "Program Files" / brand
        d.mkdir(parents=True, exist_ok=True)
        (d / "terminal64.exe").write_bytes(b"MZ")
    (tmp_path / ".mt5").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MT5_ROOT", str(tmp_path / ".mt5"))
    monkeypatch.setenv("WINE_PREFIX", str(prefix))
    monkeypatch.delenv("MT5_BROKER_DIR_NAME", raising=False)
    monkeypatch.delenv("MT5_BROKER_BUILDS", raising=False)
    if broker_builds is not None:
        monkeypatch.setenv("MT5_BROKER_BUILDS", broker_builds)
    cli = _load_cli_module()
    if broker_key:
        cli.BROKER_KEY_FILE.write_text(broker_key, encoding="utf-8")
    assert _os.environ.get("MT5_ROOT") == str(tmp_path / ".mt5")
    return cli


def test_registry_maps_a_server_to_the_build_that_can_resolve_it(monkeypatch, tmp_path):
    """The server name is the input; a hardcoded broker is what broke this.

    MEASURED FAILURE (2026-09-22): the deployment installed the Exness build by
    default and then logged in to ``MetaQuotes-Demo``. That name is not in the
    Exness build's server database, so MT5 never attempted the connection -- zero
    ``Network`` log lines, then the bridge blocked on its IPC timeout. The account
    UI simply looked frozen.
    """
    cli = _broker_cli(monkeypatch, tmp_path)

    assert cli.broker_for_server("Exness-MT5Trial9")["key"] == "exness"
    assert cli.broker_for_server("MetaQuotes-Demo")["key"] == "metaquotes"
    # Case-insensitive: brokers are typed by hand in chat.
    assert cli.broker_for_server("exness-mt5trial")["key"] == "exness"
    # Unknown is None, NOT an error: it means "cannot reason about this name".
    assert cli.broker_for_server("SomeOtherBroker-Demo") is None
    assert cli.broker_for_server(None) is None


def test_preflight_refuses_the_server_this_terminal_cannot_resolve(monkeypatch, tmp_path):
    """The refusal that replaces a silent 60-second hang with an instant answer."""
    cli = _broker_cli(
        monkeypatch, tmp_path, broker_key="exness", brands=("MetaTrader 5 EXNESS",)
    )
    terminal = cli.find_terminal()

    refusal = cli.preflight_server(terminal, "MetaQuotes-Demo")
    assert refusal is not None
    assert refusal["ok"] is False
    assert refusal["failure"] == "server_not_in_terminal"
    assert refusal["installed_broker"] == "exness"
    assert refusal["requested_server"] == "MetaQuotes-Demo"
    # The remedy must be runnable without further thought: the generic build needs
    # no installer URL, so the action alone is enough.
    assert refusal["remedy"]["action"] == "install"
    assert refusal["remedy"]["server"] == "MetaQuotes-Demo"
    assert "MT5_BROKER_BUILDS" in refusal["remedy"].get("note", "")


def test_preflight_refuses_a_broker_server_on_the_generic_terminal(monkeypatch, tmp_path):
    """The original trap, now caught before a login is even attempted."""
    cli = _broker_cli(
        monkeypatch, tmp_path, broker_key="metaquotes", brands=("MetaTrader 5",)
    )
    terminal = cli.find_terminal()

    refusal = cli.preflight_server(terminal, "Exness-MT5Trial9")
    assert refusal is not None
    assert refusal["failure"] == "server_not_in_terminal"
    assert refusal["installed_broker"] == "metaquotes"
    # A registered broker comes with its installer URL, so the fix is one call.
    assert refusal["remedy"]["broker_installer_url"].endswith("exness5setup.exe")
    assert refusal["remedy"]["broker_dir_name"] == "MetaTrader 5 EXNESS"


def test_preflight_allows_the_matching_build(monkeypatch, tmp_path):
    """The healthy path must stay silent -- a false refusal would be worse."""
    cli = _broker_cli(
        monkeypatch, tmp_path, broker_key="exness", brands=("MetaTrader 5 EXNESS",)
    )
    terminal = cli.find_terminal()
    assert cli.preflight_server(terminal, "Exness-MT5Trial9") is None
    # ...including with no server at all (the terminal is only being started).
    assert cli.preflight_server(terminal, None) is None


def test_preflight_never_blocks_an_unregistered_broker_on_a_generic_terminal(
    monkeypatch, tmp_path
):
    """Unknown broker + generic build: unprovable, so do not refuse.

    Refusing here would break a broker whose build happens to be unregistered but
    whose servers DO resolve. The bounded wait in ``cmd_start`` reports that case
    instead (zero ``Network`` lines), which is a diagnosis, not a guess.
    """
    cli = _broker_cli(monkeypatch, tmp_path, broker_key="metaquotes", brands=("MetaTrader 5",))
    assert cli.preflight_server(cli.find_terminal(), "SomeOtherBroker-Demo") is None
    # ...and with nothing installed at all, install/doctor own that case.
    cli2 = _broker_cli(monkeypatch, tmp_path / "empty")
    assert cli2.preflight_server(cli2.find_terminal(), "SomeOtherBroker-Demo") is None


def test_env_registry_extends_the_brokers_we_can_install(monkeypatch, tmp_path):
    """A deployment must be able to add its broker without a code change."""
    cli = _broker_cli(
        monkeypatch,
        tmp_path / "x",
        broker_builds=(
            "icmarkets,ic.markets|https://download.mql5.com/cdn/web/ic.example/"
            "mt5/ic.exe|MetaTrader 5 IC Markets"
        ),
    )
    build = cli.broker_for_server("ICMarkets-Demo")
    assert build is not None and build["key"] == "icmarkets"
    assert build["dir_name"] == "MetaTrader 5 IC Markets"
    # A malformed record is ignored, not fatal: a bad hint must not disable MT5.
    monkeypatch.setenv("MT5_BROKER_BUILDS", "just-a-prefix;also|bad")
    assert cli.broker_for_server("Exness-MT5Trial9")["key"] == "exness"


def test_find_terminal_honours_the_requested_broker(monkeypatch, tmp_path):
    """A cached terminal from another broker must not win.

    MEASURED FAILURE (2026-09-22): ``.terminal_path`` is written by ``find_terminal``
    and was NEVER cleared. Two branded builds coexist in one prefix, so after a
    broker switch the marker still pointed at the old terminal and ``start`` booted
    the OLD broker -- silently, because an unresolvable server produces no error.
    """
    cli = _broker_cli(
        monkeypatch,
        tmp_path,
        brands=("MetaTrader 5 EXNESS", "MetaTrader 5 XM"),
    )
    branded = cli.find_terminal()  # caches the branded build
    assert branded is not None
    cli.TERMINAL_MARKER.write_text(str(branded), encoding="utf-8")

    chosen = cli.find_terminal(prefer_key="exness")
    assert chosen == branded
    # No preference -> the cache is fine.
    assert cli.find_terminal() == branded


def test_install_forwards_the_server_so_the_first_install_is_correct():
    """One flag picks the right build, instead of re-installing after a refusal."""
    cmd = build_cli_command("install", {"server": "MetaQuotes-Demo"})
    assert "--server" in cmd and "MetaQuotes-Demo" in cmd
    # Still detached-friendly and still pointed at the sandbox installer.
    assert "install_mt5_sandbox.sh" in cmd


def test_installer_records_which_build_landed_and_for_which_url():
    """The marker answers "which terminal is this", not just "did it finish".

    Both halves are load-bearing: the CLI reads the key to decide whether a login
    can be resolved at all, and the URL comparison is what lets a broker SWITCH
    re-run the install instead of short-circuiting on the previous broker's marker.
    """
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "install_mt5_sandbox.sh"
    ).read_text(encoding="utf-8")

    assert ".broker_key" in script
    assert "MT5_BROKER_KEY" in script
    assert ".installed.url" in script
    assert '_PREV_URL}" = "${_RESOLVED_URL}"' in script
    # A different broker's installer must not be reused from disk.
    assert 'rm -f "${INSTALLER}"' in script


def test_install_clears_the_cached_terminal_and_broker_record():
    """Switching broker has to invalidate the cache, or the old terminal is reused."""
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "mt5_cli.py"
    ).read_text(encoding="utf-8")

    assert "for stale in (TERMINAL_MARKER, METAEDITOR_MARKER, BROKER_KEY_FILE):" in source


def test_sandbox_polling_never_exceeds_120_seconds():
    """Every command this tool runs inside the sandbox must fit a 120 s ceiling.

    A command the sandbox kills returns NO JSON at all, which the model reads as
    "the tool is broken" rather than "it was still working". So the per-action
    ceilings stay at or under 120 s, and ``start``'s internal wait is capped below
    it too -- a login that outlives one call is not lost, because the terminal keeps
    authorizing in the background and the next ``account`` call sees it.
    """
    assert _INSTALL_COMMAND_TIMEOUT <= 120
    for action, timeout in _TIMEOUTS.items():
        assert timeout <= 120, f"{action} runs for {timeout}s, above the 120s ceiling"

    start_wait = int(
        build_cli_command("start", {"login": 1, "password": "x", "server": "s"}).split(
            "--wait"
        )[1].split()[0]
    )
    assert start_wait < 120, "the CLI wait must finish inside the command ceiling"

    cli = _load_cli_module()
    assert cli._PROBE_TIMEOUT < 120


def test_a_failed_login_blames_the_build_only_when_the_build_is_really_wrong(
    monkeypatch, tmp_path
):
    """MEASURED 2026-09-22: a SUCCESSFUL login wrote zero ``Network`` lines.

    The generic MetaQuotes terminal authorized on MetaQuotes-Demo (``account``
    returned balance 99 996.48 USD) and its log still held no ``Network`` line --
    so "zero Network lines" cannot be reported as proof that the server did not
    resolve. A build mismatch is what justifies that claim, and it is checked
    directly (`installed_broker_key` vs `broker_for_server`) instead of inferred.
    """
    cli = _broker_cli(
        monkeypatch, tmp_path, broker_key="metaquotes", brands=("MetaTrader 5",)
    )
    terminal = cli.find_terminal()

    # 1. The terminal matches the server: zero Network lines is NOT a diagnosis.
    matched = cli.login_failure_diagnosis(
        terminal, "MetaQuotes-Demo", "10012768157", 90, []
    )
    assert matched["failure"] == "no_network_activity"
    assert "ZERO Network lines" in matched["hint"]
    assert "cannot resolve" in matched["hint"]
    # ...and it must offer the credential reading too, not only "install a build".
    assert "credential" in matched["hint"]
    assert "already matches" in matched["hint"]

    # 2. A real mismatch is stated as one.
    mismatched = cli.login_failure_diagnosis(
        terminal, "Exness-MT5Trial9", "477199408", 90, []
    )
    assert mismatched["failure"] == "server_not_in_terminal"
    assert "metaquotes build" in mismatched["hint"]

    # 3. Network lines present: the log shows an attempt, so it is credentials.
    credential = cli.login_failure_diagnosis(
        terminal, "MetaQuotes-Demo", "10012768157", 90, ["Network '1': authorization failed"]
    )
    assert credential["failure"] is None
    assert "authorization failed" in credential["hint"]


def test_a_broker_switch_is_not_reported_as_done_before_it_has_started(
    monkeypatch, tmp_path
):
    """MEASURED 2026-09-22: the first poll of a re-install answered ``stage="done"``.

    ``installed`` is true either way -- the PREVIOUS broker's terminal is still on
    disk -- and the install marker had just been deleted for the new run, so a
    switch looked finished with an empty log and no ``.broker_key``. An agent polling
    that stops waiting for a terminal that is not there yet, then logs in against the
    old build.
    """
    import argparse

    cli = _broker_cli(
        monkeypatch, tmp_path, broker_key="metaquotes", brands=("MetaTrader 5",)
    )
    # A windows python is what makes `installed` true -- i.e. what used to force
    # "done" regardless of the install that is actually in flight.
    winpy = tmp_path / "python.exe"
    winpy.write_bytes(b"MZ")
    monkeypatch.setenv("MT5_WIN_PYTHON", str(winpy))
    mt5_root = tmp_path / ".mt5"

    captured: dict[str, Any] = {}

    def _capture(payload: dict[str, Any], **_kw: Any) -> int:
        captured.clear()
        captured.update(payload)
        return 0

    monkeypatch.setattr(cli, "emit", _capture)

    # 1. A switch to the Exness build is in flight.
    (mt5_root / ".installed.url").write_text(cli.GENERIC_INSTALLER_URL, encoding="utf-8")
    (mt5_root / ".install.target").write_text(
        cli.broker_for_server("Exness-MT5Trial9")["url"], encoding="utf-8"
    )
    (mt5_root / "install.status").write_text("starting|new build", encoding="utf-8")
    cli.cmd_status(argparse.Namespace(lines=5))
    assert captured["stage"] != "done"
    assert captured["in_progress"] is True
    assert captured["installing_target"].endswith("exness5setup.exe")

    # 2. Once that build has landed, status reports done again.
    (mt5_root / ".installed.url").write_text(
        cli.broker_for_server("Exness-MT5Trial9")["url"], encoding="utf-8"
    )
    cli.cmd_status(argparse.Namespace(lines=5))
    assert captured["stage"] == "done"
    assert captured["installing_target"] is None


def test_the_generic_url_matches_the_installer_default():
    """A divergence would make every install look permanently pending.

    ``_pending_install_target`` compares the URL this CLI recorded against the one
    the shell installer wrote into ``.installed.url``; if the two defaults drift the
    comparison never matches and a healthy install never reports done.
    """
    cli = _load_cli_module()
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "install_mt5_sandbox.sh"
    ).read_text(encoding="utf-8")
    assert f"${{MT5_INSTALLER_URL:-{cli.GENERIC_INSTALLER_URL}}}" in script


def test_doctor_and_status_name_the_terminal_of_the_recorded_broker(
    monkeypatch, tmp_path
):
    """Recorded broker and reported terminal path must describe the SAME build.

    Live 2026-09-22: after switching a box to the Exness build, ``doctor`` reported
    ``installed_broker: "exness"`` next to the GENERIC terminal's path, read that
    terminal's empty ``servers.dat``, and warned that broker login was impossible --
    about a build the box was not using. Two builds coexist, so a plain
    ``find_terminal()`` returns whichever the filesystem lists first.
    """
    import argparse

    cli = _broker_cli(
        monkeypatch,
        tmp_path,
        broker_key="exness",
        brands=("MetaTrader 5", "MetaTrader 5 EXNESS"),
    )
    # The stale-cache half of the failure: the marker still points at the generic
    # build the box was installed with BEFORE the switch, which is what made
    # `doctor` describe the wrong terminal.
    cli.TERMINAL_MARKER.write_text(
        str(cli.WINE_PREFIX / "drive_c" / "Program Files" / "MetaTrader 5"
            / "terminal64.exe"),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    def _capture(payload: dict[str, Any], **_kw: Any) -> int:
        captured.clear()
        captured.update(payload)
        return 0

    monkeypatch.setattr(cli, "emit", _capture)
    cli.cmd_doctor(argparse.Namespace())

    assert captured["installed_broker"] == "exness"
    # The reported terminal is the one that broker's build lives in -- not the
    # generic one that happens to sort first on disk.
    assert "MetaTrader 5 EXNESS" in captured["terminal_path"]
    assert captured["terminal_path"] == str(cli.find_terminal(prefer_key="exness"))

    # `status` names the same terminal: it is what an operator reads to decide
    # whether the box they are looking at is the one they think it is. The marker is
    # put back on the generic build first, because doctor's own lookup re-caches it
    # and would otherwise hide the stale value this asserts about.
    cli.TERMINAL_MARKER.write_text(
        str(cli.WINE_PREFIX / "drive_c" / "Program Files" / "MetaTrader 5"
            / "terminal64.exe"),
        encoding="utf-8",
    )
    cli.cmd_status(argparse.Namespace(lines=5))
    assert "MetaTrader 5 EXNESS" in captured["terminal_path"]


def test_known_server_prefixes_match_the_cli_registry(monkeypatch, tmp_path):
    """The tool's auto-install list must match what the CLI can actually resolve.

    Read from the registry rather than from a literal list: a broker added to
    ``BROKER_BUILDS`` (or the registry growing a prefix) must appear here too, or the
    tool refuses a server the CLI could have installed for -- the "it asks the user
    for an installer URL that we already know" failure.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    registry_prefixes = {
        prefix for build in cli.broker_builds() for prefix in (build.get("match") or ())
    }
    assert registry_prefixes
    for prefix in registry_prefixes:
        assert _server_is_known(f"{prefix.capitalize()}-Demo") is True, prefix
        assert cli.broker_for_server(f"{prefix.capitalize()}-Demo") is not None, prefix

    assert _server_is_known("SomeOtherBroker-Demo") is False
    assert _server_is_known(None) is False


def test_deriv_is_registered_with_its_measured_slug_and_directory(monkeypatch, tmp_path):
    """The two Deriv values that could not have been guessed, pinned.

    MEASURED 2026-09-22 on a live Runloop devbox: the installer slug is
    ``deriv.com.limited`` (deriv.com / deriv.ltd / deriv.markets / deriv.me / deriv
    all 404) and the directory it creates is "MetaTrader 5 Terminal", NOT the usual
    "MetaTrader 5 <BRAND>". Both are load-bearing: the installer waits for
    terminal64.exe in that exact directory, and the dir name is how a build is
    identified when no ``.broker_key`` was written.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    build = cli.broker_for_server("Deriv-Demo")
    assert build is not None and build["key"] == "deriv"
    assert build["url"] == (
        "https://download.mql5.com/cdn/web/deriv.com.limited/mt5/deriv5setup.exe"
    )
    assert build["dir_name"] == "MetaTrader 5 Terminal"
    assert cli.broker_key_from_dir_name("MetaTrader 5 Terminal") == "deriv"
    # A Deriv server must never resolve to the generic build.
    assert build["url"] != cli.GENERIC_INSTALLER_URL


def test_the_recorded_default_matches_what_a_bare_install_lands():
    """A bare ``install`` records the build the SCRIPT will actually lay down.

    MEASURED 2026-09-22 (Runloop devbox): the CLI recorded ``GENERIC_INSTALLER_URL``
    as ``.install.target`` while the installer's own default (``MT5_BROKER_INSTALLER_URL``
    -> the Exness build) is what landed on disk. The two markers could never converge,
    so ``status`` answered ``stage="installing", in_progress=true`` forever even
    though the script's ``install.status`` already read ``done``: a poll loop with no
    terminating state, which is the "it hangs" symptom itself. The old test only
    pinned ``MT5_INSTALLER_URL``'s default -- the one that does NOT land for a bare
    install -- so the divergence shipped.
    """
    cli = _load_cli_module()
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "install_mt5_sandbox.sh"
    ).read_text(encoding="utf-8")
    assert f"${{MT5_BROKER_INSTALLER_URL:-{cli.DEFAULT_INSTALLER_URL}}}" in script
    # The two defaults must not collapse into one: a bare install lands the Exness
    # build, and MetaQuotes-Demo needs the generic one.
    assert cli.DEFAULT_INSTALLER_URL != cli.GENERIC_INSTALLER_URL


def test_a_bare_install_converges_instead_of_polling_forever(monkeypatch, tmp_path):
    """``status`` must reach ``done`` for an install that named no broker."""
    import argparse

    cli = _broker_cli(monkeypatch, tmp_path, brands=("MetaTrader 5 EXNESS",))
    winpy = tmp_path / "python.exe"
    winpy.write_bytes(b"MZ")
    monkeypatch.setenv("MT5_WIN_PYTHON", str(winpy))
    mt5_root = tmp_path / ".mt5"

    captured: dict[str, Any] = {}

    def _capture(payload: dict[str, Any], **_kw: Any) -> int:
        captured.clear()
        captured.update(payload)
        return 0

    monkeypatch.setattr(cli, "emit", _capture)

    # What a bare install leaves behind: the script's default build on disk, and the
    # same URL recorded as the target, so the comparison matches.
    (mt5_root / ".installed.url").write_text(
        cli.DEFAULT_INSTALLER_URL, encoding="utf-8"
    )
    (mt5_root / ".install.target").write_text(
        cli.DEFAULT_INSTALLER_URL, encoding="utf-8"
    )
    (mt5_root / "install.status").write_text("done|install complete", encoding="utf-8")
    cli.cmd_status(argparse.Namespace(lines=5))
    assert captured["stage"] == "done"
    assert captured["installing_target"] is None
    assert captured["in_progress"] is False


def test_an_unknown_broker_key_does_not_mask_the_install_directory(
    monkeypatch, tmp_path
):
    """``unknown`` is a NON-answer and must fall through to the directory name.

    The installer writes ``${MT5_BROKER_KEY:-unknown}`` when the caller named no
    broker. Honouring that literal masked the dir-name fallback, so ``doctor``
    reported ``installed_broker: "unknown"`` for a terminal whose directory
    ("MetaTrader 5 EXNESS") names the build outright -- MEASURED 2026-09-22.
    """
    cli = _broker_cli(
        monkeypatch, tmp_path, broker_key="unknown", brands=("MetaTrader 5 EXNESS",)
    )
    assert cli.installed_broker_key() == "exness"


def test_a_small_branded_servers_dat_is_not_reported_as_absent(monkeypatch, tmp_path):
    """A byte count is not evidence of absence.

    MEASURED 2026-09-22: Deriv's branded ``servers.dat`` is 43,804 B -- SMALLER than
    the generic build's ~50 KB -- so the old ``size > 100_000`` line reported
    ``terminal_has_broker_servers: false`` for a terminal that was at that moment
    streaming 722 Deriv symbols. Only the registered GENERIC build is known to carry
    no broker servers; an unregistered build is undecidable (None), which is not a
    false.
    """
    cli = _broker_cli(
        monkeypatch, tmp_path, broker_key="exness", brands=("MetaTrader 5 EXNESS",)
    )
    cfg = (
        cli.WINE_PREFIX
        / "drive_c"
        / "Program Files"
        / "MetaTrader 5 EXNESS"
        / "Config"
    )
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "servers.dat").write_bytes(b"x" * 43_804)
    terminal = cli.find_terminal(prefer_key="exness")
    assert cli._terminal_has_broker_servers(terminal) is True

    # The registered generic build really does carry none.
    monkeypatch.setattr(cli, "installed_broker_key", lambda *a, **k: "metaquotes")
    assert cli._terminal_has_broker_servers(terminal) is False

    # Unregistered/small: undecidable, never a false alarm.
    monkeypatch.setattr(cli, "installed_broker_key", lambda *a, **k: "explicit")
    assert cli._terminal_has_broker_servers(terminal) is None


def test_a_route_placeholder_key_is_never_mistaken_for_the_build(monkeypatch, tmp_path):
    """``explicit`` names the ROUTE an install took, not the build it laid down.

    MEASURED 2026-09-22 (Deriv box, ``dbx_34TAiSoAkC56fnFUCFu7u``): ``.broker_key``
    read ``explicit`` -- the caller had supplied the installer URL -- beside an
    ``.installed.url`` holding Deriv's REGISTERED installer URL, on a terminal whose
    branded ``servers.dat`` is 43,804 B. Honouring the placeholder made ``doctor``
    report ``terminal_has_broker_servers: false`` for a terminal that was at that
    moment streaming 722 Deriv symbols. The URL that actually landed decides it.
    """
    cli = _broker_cli(
        monkeypatch,
        tmp_path,
        broker_key="explicit",
        brands=("MetaTrader 5 Terminal",),
    )
    cli.INSTALLED_URL_FILE.write_text(
        "https://download.mql5.com/cdn/web/deriv.com.limited/mt5/deriv5setup.exe",
        encoding="utf-8",
    )
    assert cli.installed_broker_key() == "deriv"
    assert cli._broker_build_by_key("explicit") is None

    cfg = (
        cli.WINE_PREFIX
        / "drive_c"
        / "Program Files"
        / "MetaTrader 5 Terminal"
        / "Config"
    )
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "servers.dat").write_bytes(b"x" * 43_804)
    terminal = cli.find_terminal(prefer_key="deriv")
    assert cli._terminal_has_broker_servers(terminal) is True


def test_an_unregistered_url_is_never_guessed_into_a_build(monkeypatch, tmp_path):
    """Recovering from a placeholder key must not invent a broker.

    A URL that matches no registry entry leaves the build genuinely unnamed, so the
    placeholder is handed back and the answer stays undecidable rather than becoming
    a confident wrong one.
    """
    cli = _broker_cli(
        monkeypatch,
        tmp_path,
        broker_key="explicit",
        brands=("MetaTrader 5 SOMEBROKER",),
    )
    cli.INSTALLED_URL_FILE.write_text(
        "https://example.invalid/mt5/somebroker5setup.exe", encoding="utf-8"
    )
    assert cli._broker_build_by_url("https://example.invalid/mt5/x.exe") is None
    assert cli.installed_broker_key() == "explicit"

    # The registered URLs still match exactly, so the recovery is real and not a
    # blanket "any URL identifies a build".
    assert cli._broker_build_by_url(cli.GENERIC_INSTALLER_URL)["key"] == "metaquotes"
    assert cli._broker_build_by_url(cli.DEFAULT_INSTALLER_URL)["key"] == "exness"


def test_a_recorded_build_still_outranks_the_url_on_disk(monkeypatch, tmp_path):
    """The installer's record stays authoritative when it names a build.

    Two builds coexist in one prefix, so the URL alone cannot say which terminal
    ``find_terminal`` hands out. Only a PLACEHOLDER record yields to the URL.
    """
    cli = _broker_cli(
        monkeypatch,
        tmp_path,
        broker_key="exness",
        brands=("MetaTrader 5 EXNESS", "MetaTrader 5 Terminal"),
    )
    cli.INSTALLED_URL_FILE.write_text(
        "https://download.mql5.com/cdn/web/deriv.com.limited/mt5/deriv5setup.exe",
        encoding="utf-8",
    )
    assert cli.installed_broker_key() == "exness"


# --------------------------------------------------------------------------- #
# A stalled download mirror is a hang, not a slow install
# --------------------------------------------------------------------------- #
def _installer_script_text() -> str:
    return (
        Path(__file__).resolve().parents[2] / "scripts" / "install_mt5_sandbox.sh"
    ).read_text(encoding="utf-8")


def test_a_stalled_mirror_is_bounded_by_the_watchdog_not_the_setup_timeout():
    """A wedged mirror must cost MT5_STALL_SECONDS, not MT5_SETUP_TIMEOUT.

    MEASURED FAILURE (2026-09-22, Novita, Wine 10.0): ``mt5setup.exe /auto``
    established TCP to ``148.113.1.241:443`` and then moved NOTHING for 20
    minutes -- 0% CPU, zero bytes into the prefix, one ESTABLISHED socket with
    empty queues. Wine's downloader has no timeout of its own, so the only bound
    was ``MT5_SETUP_TIMEOUT=900``; after that the script waited a further 300 s of
    unpack grace for a terminal that was never coming. An install with a bad
    mirror therefore took ~20 minutes to report a failure that was decidable in
    the first two.
    """
    script = _installer_script_text()
    assert 'MT5_STALL_SECONDS="${MT5_STALL_SECONDS:-120}"' in script
    assert 'MT5_INSTALL_ATTEMPTS="${MT5_INSTALL_ATTEMPTS:-3}"' in script
    # The installer must live INSIDE an attempts loop, not be launched once.
    assert 'while [ "${_attempt}" -le "${MT5_INSTALL_ATTEMPTS}" ]; do' in script
    # ...and the stall branch must name the peer it is wedged on, so the failure
    # is diagnosable from install.log alone.
    assert "moved no data and burned no CPU for ${MT5_STALL_SECONDS}s" in script
    assert "peers: $(printf '%s' \"${_peers}\" | tr '\\n' ' ')" in script


def test_the_watchdog_blackholes_the_wedged_peer_then_retries():
    """Making one peer unreachable is what lets the install fall through.

    NOT a DNS fix: the mirror list is embedded in the installer and reached as a
    literal IP. Measured on that box -- pinning the MQL5 CDN names in the prefix's
    hosts file changed nothing (the installer went straight back to the same
    wedged peer), while ``ip route add blackhole 148.113.1.241/32`` let the SAME
    installer finish the whole 736 MB terminal in ~40 s.
    """
    script = _installer_script_text()
    assert "ip route add blackhole" in script
    # Needs root, which a sandbox may only have through sudo.
    assert "sudo -n ip route add blackhole" in script
    # Each identified peer is blackholed, and a failure to blackhole stops the
    # retry loop instead of spending another MT5_STALL_SECONDS on the same peer.
    assert 'if _blackhole_peer "${_peer}"; then' in script
    assert 'MT5_INSTALL_ATTEMPTS="${_attempt}"' in script


def test_the_watchdog_never_blackholes_the_sandbox_service_addresses():
    """Blackholing the platform's control connection would kill the sandbox.

    The peer list is taken from the installer's OWN process tree and then filtered,
    so a service address (the link-local metadata/API ranges Novita uses) can never
    reach ``ip route add blackhole`` even if it turns up in ``ss`` output.
    """
    script = _installer_script_text()
    assert "169\\.254\\." in script
    assert "192\\.0\\.2\\." in script
    assert "::1$" in script
    # The peers come from the installer tree, by DESCENT from our own pid -- never
    # from a name match, which would also hit this script's own shell.
    assert "_descendants() {" in script
    assert "pgrep -P" in script
    assert "pgrep -f 'broker_setup" not in script


def test_a_killed_installer_gets_no_unpack_grace():
    """A killed installer wrote nothing, so 300 more seconds cannot change that.

    The unpack window exists for an installer that EXITED on its own and left Wine
    unpacking the tree behind it. Waiting it out after a stall (or after the
    installer's own timeout reaped it) is the second half of the 20-minute hang.
    """
    script = _installer_script_text()
    assert 'if [ "${_stalled}" -eq 0 ]; then' in script
    assert "_unpack_deadline=$(( $(date +%s) + MT5_UNPACK_GRACE ))" in script
    # The gate must come before the window it guards.
    assert script.index('if [ "${_stalled}" -eq 0 ]; then') < script.index(
        "_unpack_deadline=$(( $(date +%s) + MT5_UNPACK_GRACE ))"
    )
    # ...and the fixed 300 s grace that ignored the installer's fate is gone.
    assert "_grace_deadline=$(( $(date +%s) + 300 ))" not in script


def test_a_healthy_backend_never_reaches_the_watchdog():
    """Runloop works; the first attempt must stay the command that already ships.

    The watchdog is only ever consulted when the installer has gone
    MT5_STALL_SECONDS without growing the prefix or burning CPU, which a healthy
    install (~4 minutes on Runloop) never does. Attempt 1 must therefore still log
    to the plain ``mt5setup.log``, exactly as before.
    """
    script = _installer_script_text()
    assert '_last_attempt_log="${MT5_SETUP_LOG}"' in script
    # Only a retry gets a suffixed log; attempt 1 must not.
    assert 'mt5setup.attempt${_attempt}.log' in script
    assert "attempt ${_attempt}/${MT5_INSTALL_ATTEMPTS}" in script
    # The installer's own timeout stays as the outer bound.
    assert 'MT5_SETUP_TIMEOUT="${MT5_SETUP_TIMEOUT:-900}"' in script


def test_a_failed_install_always_lands_its_log_in_install_log():
    """``install.log`` is the one file that survives every path out of the script.

    MEASURED (2026-09-22): a box whose install failed with
    ``failed|installer exited with code 126`` had NO install.log at all, because
    the failing branch only ever wrote to stderr -- unreadable from outside a
    sandbox that has already finished and been idled. Two boxes were spent on that
    diagnosis.
    """
    script = _installer_script_text()
    assert '} >>"${MT5_ROOT}/install.log" 2>/dev/null || true' in script
    assert "installer output (%s, last 2000 bytes)" in script
    # The status line must say how many attempts were spent and which log holds
    # the evidence.
    assert (
        'status failed "terminal64.exe was not produced after ${_attempt} attempt(s) '
        '(installer log: ${_last_attempt_log})"' in script
    )


# --------------------------------------------------------------------------- #
# instant exits at a price: `modify` and `guard`
# --------------------------------------------------------------------------- #
# THE BUG THESE PIN: an exit at a price had only two routes and both put the
# model in the loop. ``order --tp`` is broker-side and instant, but it was
# attachable at ORDER TIME only -- an open position could not be given a stop
# afterwards. So "close when it reaches X" had to be polled: quote, compare,
# close. Every poll is a sandbox round trip plus an agent turn, and a level
# touched between two polls is missed entirely. MEASURED in a Runloop devbox
# (2026-09-23): one ``quote`` call costs ~1.0-1.5 s inside the box, before the
# model's own latency is added. MEASURED with the fix: the tick-level watcher's
# own trigger->fill latency was 97.3 ms and 109.1 ms on two live fires.


def test_modify_and_guard_are_advertised_actions():
    """The model cannot use a primitive it cannot see in the schema."""
    params = MT5SandboxTool().parameters
    assert {"modify", "guard"} <= set(params["properties"]["action"]["enum"])
    assert params["required"] == ["action"]
    # The fields exist so the model never hand-writes the watcher's rule JSON: a
    # caller that has to build JSON is a caller that produces JSON the watcher
    # cannot read, and the failure arrives as silence rather than an error.
    assert {
        "tickets",
        "all_positions",
        "exit_at",
        "guard_action",
        "trigger_price",
        "trigger_op",
        "trigger_side",
        "interval_ms",
        "max_seconds",
    } <= set(params["properties"])


def test_modify_command_attaches_an_exit_to_an_open_position():
    cmd = build_cli_command("modify", {"ticket": 123, "exit_at": 1.1650})
    assert cmd.endswith("mt5_cli.py modify --ticket 123 --exit-at 1.165")


def test_modify_command_can_target_many_positions():
    cmd = build_cli_command(
        "modify", {"tickets": [11, 22], "all_positions": True, "exit_at": 1.165}
    )
    assert "--ticket 11 --ticket 22" in cmd
    assert " --all " in cmd
    assert "--exit-at 1.165" in cmd


def test_a_guard_rule_is_built_from_flat_fields_never_hand_written_json():
    from nanobot.agent.tools.mt5_sandbox import build_guard_rule

    rule, error = build_guard_rule(
        {
            "symbol": "EURUSD",
            "trigger_price": 1.1650,
            "trigger_side": "mid",
            "ticket": 123,
        }
    )
    assert error is None
    assert rule == {
        "symbol": "EURUSD",
        "price": 1.165,
        "side": "mid",
        "action": "close",
        "ticket": 123,
    }
    # 'op' is ABSENT when the caller gave none: the CLI infers the direction from
    # the live price, because "close when it hits X" does not say which side of
    # the market X is on -- and that is the one input a caller gets backwards.
    assert "op" not in rule

    # An explicit op survives, and `all_positions` becomes the basket scope.
    rule, error = build_guard_rule(
        {
            "symbol": "EURUSD",
            "trigger_price": 1.1650,
            "trigger_op": "<=",
            "all_positions": True,
        }
    )
    assert error is None
    assert rule["op"] == "<=" and rule["scope"] == {"all": True}
    assert "ticket" not in rule


def test_a_guard_rule_without_a_level_is_refused_with_the_reason():
    from nanobot.agent.tools.mt5_sandbox import build_guard_rule

    rule, error = build_guard_rule({"symbol": "EURUSD"})
    assert rule is None and "trigger_price" in error

    rule, error = build_guard_rule({"trigger_price": 1.165})
    assert rule is None and "symbol" in error

    # '>' and '<' are refused rather than accepted: the watcher would have to
    # decide what a strict comparison means at tick granularity, and every caller
    # who means "at or through the level" writes '>='.
    rule, error = build_guard_rule(
        {"symbol": "EURUSD", "trigger_price": 1.165, "trigger_op": ">"}
    )
    assert rule is None and "trigger_op" in error

    rule, error = build_guard_rule(
        {"symbol": "EURUSD", "trigger_price": 1.165, "trigger_side": "last"}
    )
    assert rule is None and "trigger_side" in error


def test_guard_arm_command_serialises_the_rule_itself():
    cmd = build_cli_command(
        "guard",
        {
            "guard_action": "arm",
            "symbol": "EURUSD",
            "trigger_price": 1.165,
            "ticket": 123,
            "interval_ms": 100,
            "max_seconds": 900,
        },
    )
    assert "mt5_cli.py guard arm " in cmd
    assert "--rule" in cmd and '"price": 1.165' in cmd
    assert "--interval-ms 100" in cmd
    assert "--max-seconds 900" in cmd


def test_guard_events_asks_for_a_bounded_number_of_lines():
    cmd = build_cli_command("guard", {"guard_action": "events", "lines": 5})
    assert "mt5_cli.py guard events --lines 5" in cmd


def test_modify_and_guard_fit_the_sandbox_command_ceiling():
    from nanobot.agent.tools.mt5_sandbox import _MAX_SANDBOX_COMMAND_TIMEOUT

    for action in ("modify", "guard"):
        assert _TIMEOUTS[action] <= _MAX_SANDBOX_COMMAND_TIMEOUT


async def test_modify_needs_a_target_and_a_level(monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    sandbox = _FakeSandbox()
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="modify", exit_at=1.165)
    assert result.is_error and "target" in str(result)

    result = await tool.execute(action="modify", ticket=123)
    assert result.is_error and "exit_at" in str(result)

    assert sandbox.calls == [], "a refused modify must never reach the sandbox"


async def test_guard_arm_is_gated_but_a_live_guard_is_always_disarmable(monkeypatch):
    """Refusing `guard status` with trading off would strand a live guard.

    The watcher places orders, so arming it needs the same opt-in as `order`. But
    an operator who has turned trading off must still be able to SEE the guard
    that is running and STOP it -- otherwise the only way to disarm a watcher is
    to enable trading again.
    """
    monkeypatch.delenv("MT5_ALLOW_TRADING", raising=False)
    sandbox = _FakeSandbox('{"ok": true, "running": false}\n[exit_code=0]')
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    for sub in ("status", "stop", "events", "clear"):
        result = await tool.execute(action="guard", guard_action=sub)
        assert not getattr(result, "is_error", False), sub
    assert len(sandbox.calls) == 4

    result = await tool.execute(
        action="guard", guard_action="arm", symbol="EURUSD", trigger_price=1.165
    )
    assert result.is_error and "MT5_ALLOW_TRADING" in str(result)

    result = await tool.execute(action="modify", ticket=123, exit_at=1.165)
    assert result.is_error and "MT5_ALLOW_TRADING" in str(result)

    assert len(sandbox.calls) == 4, "neither arm nor modify may reach the sandbox"


async def test_an_unknown_guard_subaction_is_refused_by_name(monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    sandbox = _FakeSandbox()
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="guard", guard_action="watch")

    assert result.is_error
    assert "watch" in str(result) and "arm" in str(result)
    assert sandbox.calls == []


async def test_positions_names_the_positions_nothing_will_ever_close_on_its_own():
    """A position with no SL and no TP has NO server-side exit.

    This is the live-trading shape that produced the original complaint: the
    first position placed in a real box came back ``sl 0.0 tp 0.0``, so nothing
    but an agent turn could ever close it. Naming those tickets where the model
    is already reading its positions is what turns the instruction back into a
    server-held exit.
    """
    payload = {
        "ok": True,
        "count": 2,
        "positions": [
            {"ticket": 1, "symbol": "EURUSD", "sl": 0.0, "tp": 0.0},
            {"ticket": 2, "symbol": "EURUSD", "sl": 1.14, "tp": 0.0},
        ],
    }
    sandbox = _FakeSandbox(json.dumps(payload) + "\n[exit_code=0]")
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="positions")

    rendered = str(result)
    assert "positions_without_a_server_side_exit" in rendered
    assert "modify" in rendered and "exit_at" in rendered

    # A position that already holds a stop is not called out: the hint is for the
    # ones at risk, and a hint that fires on everything is a hint the model learns
    # to ignore.
    payload["positions"] = [{"ticket": 2, "symbol": "EURUSD", "sl": 1.14, "tp": 0.0}]
    sandbox.response = json.dumps(payload) + "\n[exit_code=0]"
    result = await tool.execute(action="positions")
    assert "positions_without_a_server_side_exit" in str(result)
    assert "hint" not in str(result)


# --------------------------------------------------------------------------- #
# the CLI side of the same fix
# --------------------------------------------------------------------------- #
def _fake_mt5(*, bid=1.14240, ask=1.14242, point=0.00001, stops_level=0, digits=5,
              position_type=0, sl=0.0, tp=0.0, ticket=777):
    """A stand-in for the MetaTrader5 module, wired for one open position."""
    import types

    mt5 = types.SimpleNamespace()
    mt5.POSITION_TYPE_BUY = 0
    mt5.POSITION_TYPE_SELL = 1
    mt5.TRADE_ACTION_SLTP = 5
    mt5.TRADE_RETCODE_DONE = 10009
    mt5.symbol_info = lambda symbol: types.SimpleNamespace(
        point=point, trade_stops_level=stops_level, trade_freeze_level=0, digits=digits
    )
    mt5.symbol_info_tick = lambda symbol: types.SimpleNamespace(bid=bid, ask=ask)
    mt5.positions_get = lambda *a, **k: [
        types.SimpleNamespace(
            ticket=ticket, symbol="EURUSD", type=position_type, sl=sl, tp=tp,
            volume=0.01,
        )
    ]
    def _order_send(request):
        mt5.sent = request
        return types.SimpleNamespace(retcode=10009, comment="Request executed")

    mt5.order_send = _order_send
    mt5.sent = None
    mt5.last_error = lambda: (-1, "no error")
    return mt5


def _run_modify(monkeypatch, cli, mt5, **kwargs):
    """Call cmd_modify against the fake bridge and return the emitted payload."""
    import argparse

    monkeypatch.setattr(cli, "require_bridge", lambda: (mt5, None))
    captured: dict[str, Any] = {}

    def _capture(payload, **_kw):
        captured.clear()
        captured.update(payload)
        return 0

    monkeypatch.setattr(cli, "emit", _capture)
    args = argparse.Namespace(
        ticket=[kwargs.get("ticket")], symbol=None, all=False,
        exit_at=kwargs.get("exit_at"), sl=kwargs.get("sl"), tp=kwargs.get("tp"),
    )
    cli.cmd_modify(args)
    return captured


def test_modify_routes_the_level_to_the_stop_the_broker_can_hold(monkeypatch, tmp_path):
    """The side is inferred from the position's direction, never guessed.

    A level above the market protects a LONG in profit (a take-profit) and a
    SHORT from loss (a stop-loss). Attaching a take-profit where a stop was meant
    is the one mistake here that costs money instead of returning an error, so
    both directions are pinned.
    """
    cli = _broker_cli(monkeypatch, tmp_path)

    # Long, level above the market -> TP.
    long_mt5 = _fake_mt5(position_type=0)
    payload = _run_modify(monkeypatch, cli, long_mt5, ticket=777, exit_at=1.14500)
    row = payload["results"][0]
    assert row["routed_to"] == "tp" and row["tp"] == 1.145 and row["sl"] == 0.0
    # The request the bridge actually sent, not just what was reported back.
    assert long_mt5.sent["action"] == long_mt5.TRADE_ACTION_SLTP
    assert long_mt5.sent["position"] == 777
    assert long_mt5.sent["sl"] == 0.0 and long_mt5.sent["tp"] == 1.145

    # Long, level below the market -> SL.
    payload = _run_modify(monkeypatch, cli, _fake_mt5(position_type=0), ticket=777,
                          exit_at=1.14000)
    assert payload["results"][0]["routed_to"] == "sl"
    assert payload["results"][0]["sl"] == 1.14

    # SHORT inverts both: above the market is the stop, below it the target.
    short_high = _run_modify(monkeypatch, cli, _fake_mt5(position_type=1, sl=0.0),
                             ticket=777, exit_at=1.14500)
    assert short_high["results"][0]["routed_to"] == "sl"
    short_low = _run_modify(monkeypatch, cli, _fake_mt5(position_type=1, sl=0.0),
                            ticket=777, exit_at=1.14000)
    assert short_low["results"][0]["routed_to"] == "tp"


def test_modify_keeps_a_level_inside_the_spread_out_of_the_brokers_rejection(
    monkeypatch, tmp_path
):
    """A level inside the minimum stop distance is moved out, and SAID SO.

    The server answers retcode 10016 "invalid stops" for a stop that is too close
    to the market, which reads like a bad price rather than a too-tight one. The
    level is nudged just far enough for the server to hold it, and the answer
    reports both the original level and the reason.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _fake_mt5(position_type=0, bid=1.14240, ask=1.14242, point=0.00001)

    # A level between the two legal bounds, above the BID the position is valued
    # at: the caller means "exit when the market comes up to me", so it is clamped
    # up to the nearest legal TARGET -- not down to a stop, which is the opposite
    # instruction about the money.
    payload = _run_modify(monkeypatch, cli, mt5, ticket=777, exit_at=1.14241)
    row = payload["results"][0]

    assert row["ok"] is True
    assert row["min_distance"] == pytest.approx(0.00001)
    assert row["routed_to"] == "tp"
    assert row["tp"] == pytest.approx(1.14243)
    assert row["adjusted_from"] == pytest.approx(1.14241)
    assert "minimum stop distance" in row["adjust_reason"]
    assert mt5.sent["tp"] == pytest.approx(1.14243)
    assert mt5.sent["sl"] == 0.0

    # ...and a level BELOW the bid clamps down to a stop, in the same 5-digit
    # granularity where the two bounds are only a point apart.
    tight = _fake_mt5(position_type=0, bid=1.14240, ask=1.14242, point=0.00001,
                      stops_level=5)
    payload = _run_modify(monkeypatch, cli, tight, ticket=777, exit_at=1.14237)
    row = payload["results"][0]
    assert row["routed_to"] == "sl"
    assert row["sl"] == pytest.approx(1.14235)
    assert row["tp"] == 0.0
    assert tight.sent["sl"] == pytest.approx(1.14235)

    # A level the broker will take is passed through untouched.
    clean = _run_modify(monkeypatch, cli, _fake_mt5(position_type=0), ticket=777,
                        exit_at=1.15000)
    assert "adjusted_from" not in clean["results"][0]


def test_modify_snaps_the_level_to_the_symbols_precision(monkeypatch, tmp_path):
    """A 6-decimal level on a 5-digit symbol is an invalid stop, not a rounded one."""
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _fake_mt5(position_type=0, digits=5)

    payload = _run_modify(monkeypatch, cli, mt5, ticket=777, exit_at=1.1450049)

    assert payload["results"][0]["tp"] == 1.145
    assert mt5.sent["tp"] == 1.145


def test_modify_reports_a_missing_ticket_as_closed_rather_than_silently_passing(
    monkeypatch, tmp_path
):
    cli = _broker_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "require_bridge", lambda: (_fake_mt5(), None))
    code = cli.cmd_modify(
        __import__("argparse").Namespace(
            ticket=[999], symbol=None, all=False, exit_at=1.145, sl=None, tp=None
        )
    )
    assert code == 2


def test_the_watcher_is_embedded_valid_python(monkeypatch, tmp_path):
    """The watcher ships as a string in the CLI, so nothing else compiles it.

    A syntax error in it would only ever surface as "guard failed to start" in a
    sandbox, with the traceback buried in a Wine log -- so it is compiled here.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    compile(cli._GUARD_WATCH_SOURCE, "<guard_watch>", "exec")
    assert "MetaTrader5" in cli._GUARD_WATCH_SOURCE
    # It writes measured latency, which is the number that says the exit was
    # instant rather than merely fast enough.
    assert "latency_ms" in cli._GUARD_WATCH_SOURCE


def test_the_guard_log_holds_readable_lines_not_wine_chatter(monkeypatch, tmp_path):
    """``log_file`` is what a caller is pointed at when a guard fails to start.

    Wine writes fixme/err chatter to stderr for every process it starts, so
    merging stderr into that log buried the watcher's own lines under hundreds of
    them (MEASURED 2026-09-23: the log was 100% Wine noise). The two streams are
    kept in separate files.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    assert cli.GUARD_ERR_FILE.name == "watcher.err"
    assert cli.GUARD_ERR_FILE != cli.GUARD_LOG_FILE

    source = (Path(__file__).resolve().parents[2] / "scripts" / "mt5_cli.py").read_text()
    assert "2>> {shlex.quote(str(GUARD_ERR_FILE))}" in source
    # The watcher says what it is doing on stdout, which is the stream the
    # readable log captures.
    assert "def note(message):" in cli._GUARD_WATCH_SOURCE
    assert 'note(f"finished: {exit_reason} after {polls} polls, "' in cli._GUARD_WATCH_SOURCE


def test_the_guard_rule_direction_is_inferred_from_the_live_price(
    monkeypatch, tmp_path
):
    """ "Close when it hits X" does not say which side X is on, so it is inferred.

    Both directions, and the inference is REPORTED (``op_inferred``) so a caller
    can see that the CLI chose the side rather than being told it.
    """
    cli = _broker_cli(monkeypatch, tmp_path)

    above = cli._validate_rule({"symbol": "EURUSD", "price": 1.165}, 0, price_hint=1.142)
    assert above["op"] == ">=" and above["op_inferred"] is True
    assert above["price_at_arm"] == 1.142

    below = cli._validate_rule({"symbol": "EURUSD", "price": 1.100}, 0, price_hint=1.142)
    assert below["op"] == "<="

    given = cli._validate_rule({"symbol": "EURUSD", "price": 1.165, "op": "<="}, 0, 1.142)
    assert given["op"] == "<=" and given["op_inferred"] is False

    # With no price to infer from, guessing would be the only alternative to
    # asking -- and a wrong direction is a stop where a target was meant.
    with pytest.raises(ValueError) as excinfo:
        cli._validate_rule({"symbol": "EURUSD", "price": 1.165}, 0, price_hint=None)
    assert "op" in str(excinfo.value)


def test_the_guard_refuses_a_rule_it_cannot_watch(monkeypatch, tmp_path):
    cli = _broker_cli(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="symbol"):
        cli._validate_rule({"price": 1.165}, 0, 1.142)
    with pytest.raises(ValueError, match="price"):
        cli._validate_rule({"symbol": "EURUSD"}, 0, 1.142)
    with pytest.raises(ValueError, match="number"):
        cli._validate_rule({"symbol": "EURUSD", "price": "soon"}, 0, 1.142)
    with pytest.raises(ValueError, match="op"):
        cli._validate_rule({"symbol": "EURUSD", "price": 1.165, "op": "~="}, 0, 1.142)
    with pytest.raises(ValueError, match="side"):
        cli._validate_rule(
            {"symbol": "EURUSD", "price": 1.165, "side": "close"}, 0, 1.142
        )
    # volume=0 is "no partial close", not an error; a NEGATIVE size is.
    with pytest.raises(ValueError, match="volume"):
        cli._validate_rule(
            {"symbol": "EURUSD", "price": 1.165, "volume": -0.5}, 0, 1.142
        )
    assert cli._validate_rule(
        {"symbol": "EURUSD", "price": 1.165, "volume": 0}, 0, 1.142
    )["volume"] is None


def test_a_guard_rule_scope_prefers_a_ticket_then_the_basket_then_the_symbol(
    monkeypatch, tmp_path
):
    """Three scopes, in the order of how specific the caller was."""
    cli = _broker_cli(monkeypatch, tmp_path)

    by_ticket = cli._validate_rule({"symbol": "EURUSD", "price": 1.165, "ticket": 42},
                                   0, 1.142)
    assert by_ticket["scope"] == {"ticket": 42}

    basket = cli._validate_rule(
        {"symbol": "EURUSD", "price": 1.165, "scope": {"all": True}}, 0, 1.142
    )
    assert basket["scope"] == {"all": True}

    # Default: every position on the rule's own symbol -- so a second position
    # opened later on that symbol is still protected by the rule.
    whole_symbol = cli._validate_rule({"symbol": "EURUSD", "price": 1.165}, 0, 1.142)
    assert whole_symbol["scope"] == {"symbol": "EURUSD"}


def test_a_generic_install_records_the_url_it_will_actually_land(monkeypatch, tmp_path):
    """The recorded target and the installer's own default must be the same build.

    MEASURED FAILURE (2026-09-23, Runloop devbox): ``install --server
    MetaQuotes-Demo`` wrote the EXNESS url into ``.install.target`` while the
    installer downloaded the generic build into ``.installed.url``. The two
    markers can never converge, so ``status`` reported
    ``stage="installing", in_progress=true`` -- "a different build is being
    installed" -- for the life of the box, even though the installer's own
    ``install.status`` already read ``done``. The host tool's install wait then
    polls until its whole budget expires and reports a timeout, which reads as a
    permanent hang instead of a finished install.
    """
    import argparse
    import subprocess as _subprocess

    cli = _broker_cli(monkeypatch, tmp_path)
    script = tmp_path / "install_mt5_sandbox.sh"
    script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")

    # The installer is detached; the test only inspects what was recorded.
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: _subprocess.CompletedProcess(a, 0, "4242\n", ""),
    )
    cli.cmd_install(
        argparse.Namespace(
            script=str(script),
            server="MetaQuotes-Demo",
            broker_installer_url="",
            broker_dir_name="",
            timeout=60,
            detach=True,
            foreground=False,
        )
    )

    recorded = cli.INSTALL_TARGET_FILE.read_text(encoding="utf-8").strip()
    assert recorded == cli.GENERIC_INSTALLER_URL
    assert recorded != cli.DEFAULT_INSTALLER_URL

    # The property that matters: once the installer has landed that same URL, the
    # install stops reading as "a different build is being installed" and the
    # poll loop can terminate.
    assert cli._pending_install_target() == recorded
    cli.INSTALLED_URL_FILE.write_text(recorded, encoding="utf-8")
    assert cli._pending_install_target() == ""


def test_a_broker_install_still_records_the_broker_url(monkeypatch, tmp_path):
    """The generic fix must not swallow the broker path it sits next to."""
    import argparse
    import subprocess as _subprocess

    cli = _broker_cli(monkeypatch, tmp_path)
    script = tmp_path / "install_mt5_sandbox.sh"
    script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: _subprocess.CompletedProcess(a, 0, "4242\n", ""),
    )

    cli.cmd_install(
        argparse.Namespace(
            script=str(script),
            server="Exness-MT5Trial9",
            broker_installer_url="",
            broker_dir_name="",
            timeout=60,
            detach=True,
            foreground=False,
        )
    )

    recorded = cli.INSTALL_TARGET_FILE.read_text(encoding="utf-8").strip()
    assert recorded != cli.GENERIC_INSTALLER_URL
    assert "exness" in recorded.lower()


# --------------------------------------------------------------------------- #
# a level on the wrong side of the market, and a guard that cannot see
# --------------------------------------------------------------------------- #
def test_a_long_stop_above_the_market_is_refused_not_relocated(
    monkeypatch, tmp_path
):
    """The clamp may move a level OUT, never to the other side of the market.

    MEASURED 2026-09-23 (MetaQuotes demo, live): ``modify --sl <above the bid>``
    on a long came back ok=true with ``adjusted_from`` set, and what had been
    sent was a stop BELOW the market -- a level the caller never asked for, on
    the other side of their position, reported as a success. A level the market
    has already gone through is refused instead, and ``--exit-at`` is named for
    the caller who really did mean "exit at this price".
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _fake_mt5(position_type=0, bid=1.14240, ask=1.14242, point=0.00001)

    payload = _run_modify(monkeypatch, cli, mt5, ticket=777, sl=1.14300)

    row = payload["results"][0]
    assert row["ok"] is False
    assert row["wrong_side"] == ["sl"]
    assert "exit-at" in row["hint"]
    assert payload["ok"] is False
    assert mt5.sent is None, "a refused leg must never reach the broker"

    # A long's TAKE PROFIT below the market is the same mistake the other way.
    below = _fake_mt5(position_type=0, bid=1.14240, ask=1.14242, point=0.00001)
    payload = _run_modify(monkeypatch, cli, below, ticket=777, tp=1.14000)
    assert payload["results"][0]["wrong_side"] == ["tp"]
    assert below.sent is None

    # A SHORT is the mirror: its stop is above the market, its target below.
    short_below = _fake_mt5(position_type=1, bid=1.14240, ask=1.14242, point=0.00001)
    assert _run_modify(monkeypatch, cli, short_below, ticket=777, sl=1.14100)[
        "results"
    ][0]["wrong_side"] == ["sl"]
    assert short_below.sent is None
    short_above = _fake_mt5(position_type=1, bid=1.14240, ask=1.14242, point=0.00001)
    assert _run_modify(monkeypatch, cli, short_above, ticket=777, tp=1.14500)[
        "results"
    ][0]["wrong_side"] == ["tp"]
    assert short_above.sent is None

    # The guidance names the side THAT LEG needs: a long's stop is below the
    # market and its target is above it. MEASURED 2026-09-23 (live): the first
    # version told a caller their long's --tp had to be "below the market", the
    # exact opposite of the truth.
    long_tp = _run_modify(
        monkeypatch, cli,
        _fake_mt5(position_type=0, bid=1.14240, ask=1.14242, point=0.00001),
        ticket=777, tp=1.14000,
    )["results"][0]
    assert "has to be above the market" in long_tp["hint"]
    long_sl = _run_modify(
        monkeypatch, cli,
        _fake_mt5(position_type=0, bid=1.14240, ask=1.14242, point=0.00001),
        ticket=777, sl=1.14300,
    )["results"][0]
    assert "has to be below the market" in long_sl["hint"]
    short_tp = _run_modify(
        monkeypatch, cli,
        _fake_mt5(position_type=1, bid=1.14240, ask=1.14242, point=0.00001),
        ticket=777, tp=1.14500,
    )["results"][0]
    assert "has to be below the market" in short_tp["hint"]


def test_a_tight_leg_is_clamped_on_its_own_side_and_both_legs_are_reported(
    monkeypatch, tmp_path
):
    """Too tight is not wrong side: that one is moved out, and every move is named.

    A single ``adjusted_from`` field could only ever carry one leg, so clamping
    both of them reported half of what had been changed about the caller's money.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    # stops_level 5 -> the broker wants 5 points; both legs are asked 1 point out.
    mt5 = _fake_mt5(position_type=0, bid=1.14240, ask=1.14242, point=0.00001,
                    stops_level=5)

    payload = _run_modify(monkeypatch, cli, mt5, ticket=777,
                          sl=1.14239, tp=1.14243)

    row = payload["results"][0]
    assert row["ok"] is True
    assert row["sl"] == pytest.approx(1.14235)   # bid - 5 points, still BELOW
    assert row["tp"] == pytest.approx(1.14247)   # ask + 5 points, still ABOVE
    # adjusted_from is the first leg moved; the list is what carries every one.
    assert row["adjusted_from"] == pytest.approx(1.14243)
    assert [a["leg"] for a in row["adjustments"]] == ["tp", "sl"]
    assert {a["leg"]: a["placed"] for a in row["adjustments"]} == {
        "tp": pytest.approx(1.14247),
        "sl": pytest.approx(1.14235),
    }
    assert mt5.sent["sl"] == pytest.approx(1.14235)
    assert mt5.sent["tp"] == pytest.approx(1.14247)


def test_modify_leaves_alone_the_leg_the_caller_did_not_pass(monkeypatch, tmp_path):
    """``modify --tp`` must not quietly re-place the stop that is already there.

    Only the legs the caller passed are checked and clamped, so a stop the server
    accepted earlier is carried through exactly as it stands.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _fake_mt5(position_type=0, bid=1.14240, ask=1.14242, point=0.00001,
                    stops_level=5, sl=1.14200, tp=0.0)

    payload = _run_modify(monkeypatch, cli, mt5, ticket=777, tp=1.14500)

    assert mt5.sent["sl"] == pytest.approx(1.14200), "the existing stop was rewritten"
    assert mt5.sent["tp"] == pytest.approx(1.14500)

    # An explicit zero REMOVES that leg rather than placing one at price zero.
    mt5 = _fake_mt5(position_type=0, bid=1.14240, ask=1.14242, sl=1.14200, tp=1.14500)
    _run_modify(monkeypatch, cli, mt5, ticket=777, tp=0)
    assert mt5.sent["tp"] == 0.0
    assert mt5.sent["sl"] == pytest.approx(1.14200)


def test_arming_a_guard_on_an_unpriceable_symbol_is_refused(monkeypatch, tmp_path):
    """A guard that cannot read a price protects nothing, and says nothing.

    MEASURED 2026-09-23 (Runloop box, MetaQuotes demo): a rule on a misspelled
    symbol answered ``ok=true, guard="armed"`` with a live heartbeat, polled 47
    times and priced NOTHING. It was indistinguishable from protection. The arm
    now reads one price per rule symbol up front and refuses the ones it cannot
    see, naming them.
    """
    import argparse

    cli = _broker_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_guard_current_price", lambda symbol: None)
    spawned: list[Any] = []
    monkeypatch.setattr(cli, "_guard_spawn", lambda *a, **k: spawned.append(a))

    args = argparse.Namespace(
        guard_action="arm",
        rule=[json.dumps({"symbol": "NOTASYMBOL", "price": 1.0, "op": ">=",
                          "action": "close", "ticket": 1})],
        interval_ms=100,
        max_seconds=60,
        deviation=30,
    )
    code = cli.cmd_guard(args)
    assert code == 4
    # Nothing was armed and nothing was started.
    assert spawned == []
    assert cli._read_guard_rules() == []

    # The escape hatch exists for a symbol that prices later, and only then.
    monkeypatch.setattr(cli, "_guard_current_price", lambda symbol: 1.15)
    monkeypatch.setattr(cli, "_read_guard_state",
                        lambda: {"status": "running", "pid": 1, "heartbeat": 1e18})
    args.allow_unpriceable = True
    assert cli.cmd_guard(
        argparse.Namespace(**{**vars(args), "rule": [json.dumps(
            {"symbol": "EURUSD", "price": 1.16, "op": ">=", "ticket": 1})]})
    ) in (0, 2)


def test_an_arm_that_fired_immediately_is_not_reported_as_a_failed_arm(
    monkeypatch, tmp_path
):
    """A rule that is already satisfied fires on tick one and the watcher exits.

    MEASURED 2026-09-23 (MetaQuotes demo, live): the position was closed with
    ``latency_ms: 92.5, polls: 1`` and the arm still answered ``ok=false,
    guard="not_running"`` -- a completed exit reported as a failure, which invites
    arming a second guard over a position that is already closed.
    """
    import argparse

    cli = _broker_cli(monkeypatch, tmp_path)
    cli.GUARD_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "_guard_current_price", lambda symbol: 1.14212)
    monkeypatch.setattr(
        cli, "_guard_python", lambda: ("wine", tmp_path / "python.exe")
    )
    rule = cli._validate_rule(
        {"symbol": "EURUSD", "price": 1.14222, "op": "<=", "side": "bid",
         "ticket": 152670647138},
        0, 1.14212,
    )

    # The spawn stub writes the fire exactly as the real watcher does -- during
    # the arm, after the ruleset went live. Writing it BEFORE the arm instead
    # would be testing the false positive the timestamp filter exists to stop.
    def _spawn_writes_the_fire(*_a, **_k):
        with open(cli.GUARD_EVENTS_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "event": "fired", "rule_id": rule["id"], "symbol": "EURUSD",
                "op": "<=", "level": 1.14222, "trigger_price": 1.14212,
                "side": "bid", "trigger_ts": time.time(),
                "close_ts": time.time(), "latency_ms": 92.5,
                "positions_matched": 1, "polls": 1,
            }) + "\n")
        return {"state": {"status": "finished", "exit_reason": "rules_satisfied"},
                "launcher_pid": "7", "waited_s": 1.0,
                "log_mark": 0, "err_mark": 0}

    monkeypatch.setattr(cli, "_guard_spawn", _spawn_writes_the_fire)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "emit",
                        lambda payload, **_k: captured.update(payload) or 0)

    cli.cmd_guard(argparse.Namespace(
        guard_action="arm", rule=[json.dumps(
            {"id": rule["id"], "symbol": "EURUSD", "price": 1.14222, "op": "<=",
             "side": "bid", "ticket": 152670647138})],
        interval_ms=100, max_seconds=60, deviation=30,
    ))

    assert captured["ok"] is True
    assert captured["guard"] == "fired_immediately"
    assert captured["fired"][0]["latency_ms"] == 92.5
    assert "already happened" in captured["message"]
    assert "error" not in captured


def test_an_old_fire_for_the_same_rule_id_is_not_read_as_this_arms_fire(
    monkeypatch, tmp_path
):
    """A re-arm of an explicit rule id must not inherit that rule's old fire.

    Rule ids default to ``g<arm-time>-<index>``, but a caller may pass its own id
    and re-arm it. Without a timestamp filter, the fire from the FIRST arm is
    still the newest event for that id, so a watcher that died for a real reason
    (Wine could not import MetaTrader5, the terminal logged out) would be reported
    as a completed exit -- the same lie as before, pointing the other way.
    """
    import argparse

    cli = _broker_cli(monkeypatch, tmp_path)
    cli.GUARD_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "_guard_current_price", lambda symbol: 1.14212)
    monkeypatch.setattr(cli, "_guard_python", lambda: ("wine", tmp_path / "p.exe"))
    old_fire = {
        "event": "fired", "rule_id": "g1700000000-0", "symbol": "EURUSD",
        "op": "<=", "level": 1.14222, "trigger_price": 1.14212, "side": "bid",
        "trigger_ts": time.time() - 600.0, "close_ts": time.time() - 600.0,
        "latency_ms": 92.5, "positions_matched": 1, "polls": 1,
    }
    cli.GUARD_EVENTS_FILE.write_text(json.dumps(old_fire) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        cli, "_guard_spawn",
        lambda *a, **k: {"state": {"status": "failed", "error": "no MetaTrader5"},
                         "launcher_pid": "7", "waited_s": 1.0,
                         "log_mark": 0, "err_mark": 0},
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "emit",
                        lambda payload, **_k: captured.update(payload) or 0)

    cli.cmd_guard(argparse.Namespace(
        guard_action="arm", rule=[json.dumps(
            {"id": "g1700000000-0", "symbol": "EURUSD", "price": 1.14222,
             "op": "<=", "side": "bid", "ticket": 152670647138})],
        interval_ms=100, max_seconds=60, deviation=30,
    ))

    assert captured["ok"] is False
    assert captured["guard"] == "not_running"
    assert "fired" not in captured


def test_a_fire_whose_close_was_rejected_is_not_reported_as_a_dead_watcher(
    monkeypatch, tmp_path
):
    """Level touched, close refused: the position is still open and must be said.

    A ``close_failed`` event means the watcher DID its job and the broker said no
    ("market closed", "no prices", an invalid deviation). Reading that as a failed
    arm sends the caller to diagnose Wine while a position sits open at a level
    they asked to be out at.
    """
    import argparse

    cli = _broker_cli(monkeypatch, tmp_path)
    cli.GUARD_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "_guard_current_price", lambda symbol: 1.14212)
    monkeypatch.setattr(cli, "_guard_python", lambda: ("wine", tmp_path / "p.exe"))
    rule = cli._validate_rule(
        {"symbol": "EURUSD", "price": 1.14222, "op": "<=", "side": "bid",
         "ticket": 152670647138},
        0, 1.14212,
    )

    def _spawn_writes_the_rejection(*_a, **_k):
        with open(cli.GUARD_EVENTS_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "event": "close_failed", "rule_id": rule["id"],
                "symbol": "EURUSD", "op": "<=", "level": 1.14222,
                "trigger_price": 1.14212, "trigger_ts": time.time(),
                "close_ts": time.time(), "latency_ms": 88.0,
                "positions_matched": 1, "polls": 1,
                "results": [{"ok": False, "retcode": 10018,
                             "comment": "market closed"}],
            }) + "\n")
        return {"state": {"status": "finished", "exit_reason": "rules_satisfied"},
                "launcher_pid": "7", "waited_s": 1.0,
                "log_mark": 0, "err_mark": 0}

    monkeypatch.setattr(cli, "_guard_spawn", _spawn_writes_the_rejection)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "emit",
                        lambda payload, **_k: captured.update(payload) or 0)

    cli.cmd_guard(argparse.Namespace(
        guard_action="arm", rule=[json.dumps(
            {"id": rule["id"], "symbol": "EURUSD", "price": 1.14222, "op": "<=",
             "side": "bid", "ticket": 152670647138})],
        interval_ms=100, max_seconds=60, deviation=30,
    ))

    assert captured["ok"] is False
    assert captured["guard"] == "close_failed"
    assert captured["close_failed"][0]["results"][0]["retcode"] == 10018
    assert "still open" in captured["message"]
    assert "error" not in captured


def test_a_guard_that_stopped_is_an_alert_not_a_healthy_status(monkeypatch, tmp_path):
    """Armed rules plus a stopped watcher is the failure that must never read OK.

    MEASURED 2026-09-23: after a guard hit its own --max-seconds, ``status``
    answered ``ok=true, running=false`` with the rules still armed and a hint --
    which reads as "everything is fine" while the level is watched by nobody.
    """
    import argparse

    cli = _broker_cli(monkeypatch, tmp_path)
    cli.GUARD_DIR.mkdir(parents=True, exist_ok=True)
    cli.GUARD_RULES_FILE.write_text(
        json.dumps([{"id": "g1", "symbol": "EURUSD", "op": ">=", "price": 1.2}]),
        encoding="utf-8",
    )
    cli.GUARD_STATE_FILE.write_text(
        json.dumps({"status": "finished", "exit_reason": "max_seconds",
                    "heartbeat": 1.0, "polls": 78}),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "emit",
                        lambda payload, **_k: captured.update(payload) or 0)

    code = cli.cmd_guard(argparse.Namespace(guard_action="status"))
    assert code == 0
    assert captured["ok"] is False
    assert captured["alert"] == "guard_not_running"
    assert captured["exit_reason"] == "max_seconds"
    assert "ensure" in captured["recovery"]
    assert "NOTHING is watching them" in captured["warning"]

    # The same rules with nothing running are also what `positions` reports, so a
    # caller reading open risk is told in the same breath.
    summary = cli._guard_summary()
    assert summary["live"] is False
    assert summary["rules_armed"] == 1
    assert summary["alert"] == "guard_not_running"


def test_ensure_restarts_a_stopped_guard_and_names_the_unwatched_window(
    monkeypatch, tmp_path
):
    """Recovery is one call, and the gap is reported rather than papered over.

    Anything that touched an armed level while the watcher was down was missed, so
    the caller is told how long that window was instead of being handed a fresh
    "armed" and left to assume it was never open.
    """
    import argparse

    cli = _broker_cli(monkeypatch, tmp_path)
    cli.GUARD_DIR.mkdir(parents=True, exist_ok=True)
    cli.GUARD_RULES_FILE.write_text(
        json.dumps([{"id": "g1", "symbol": "EURUSD", "op": ">=", "price": 1.2}]),
        encoding="utf-8",
    )
    stopped_at = cli.time.time() - 42.0
    cli.GUARD_STATE_FILE.write_text(
        json.dumps({"status": "finished", "exit_reason": "max_seconds",
                    "interval_ms": 100, "heartbeat": stopped_at}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_guard_python", lambda: ("wine", tmp_path / "py.exe"))
    monkeypatch.setattr(
        cli, "_guard_spawn",
        lambda *a, **k: {"state": {"status": "running", "pid": 9,
                                   "heartbeat": cli.time.time()},
                         "launcher_pid": "9", "waited_s": 1.0},
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "emit",
                        lambda payload, **_k: captured.update(payload) or 0)

    code = cli.cmd_guard(argparse.Namespace(guard_action="ensure", max_seconds=600))
    assert code == 0
    assert captured["action"] == "rearmed"
    assert captured["ok"] is True
    assert captured["previous_exit_reason"] == "max_seconds"
    assert captured["unprotected_seconds"] == pytest.approx(42.0, abs=1.0)
    assert "NOT acted on" in captured["warning"]

    # Nothing armed and nothing to restart -> no watcher is spawned for nothing.
    cli.GUARD_RULES_FILE.write_text("[]", encoding="utf-8")
    spawned: list[Any] = []
    monkeypatch.setattr(cli, "_guard_spawn", lambda *a, **k: spawned.append(a))
    cli.cmd_guard(argparse.Namespace(guard_action="ensure", max_seconds=600))
    assert spawned == []
    assert captured["running"] is False and captured["rules_armed"] == 0

    # Rules armed, watcher dead, and no Windows python to restart it: the armed
    # rules are UNPROTECTED and that is what the refusal says.
    cli.GUARD_RULES_FILE.write_text(
        json.dumps([{"id": "g1", "symbol": "EURUSD", "op": ">=", "price": 1.2}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_guard_python", lambda: None)
    captured.clear()
    cli.cmd_guard(argparse.Namespace(guard_action="ensure", max_seconds=600))
    assert captured["ok"] is False
    assert captured["alert"] == "guard_not_running"
    assert captured["rules_armed"] == 1
    assert "UNPROTECTED" in str(captured)


def test_the_watcher_reports_a_symbol_it_cannot_price(monkeypatch, tmp_path):
    """A symbol that goes dark mid-watch is an event, and a stop is a stop.

    The watcher must not poll forever in silence on a symbol with no tick: it
    writes ``rule_unpriceable`` (and ``rule_priceable`` when the feed returns),
    keeps the outage in its state file, and -- if NOTHING has ever been priced --
    exits with ``unpriceable_symbol`` instead of pretending to watch.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    source = cli._GUARD_WATCH_SOURCE
    assert "rule_unpriceable" in source
    assert "rule_priceable" in source
    assert "unpriceable_symbol" in source
    assert '"unpriceable": {s: round(t, 1) for s, t in unpriced_since.items()}' in source
    assert "UNPRICEABLE_STALE" in source


def test_the_wine_reexec_passes_the_roots_through(monkeypatch, tmp_path):
    """The re-exec'd child must read the SAME ``.mt5`` the parent does.

    MEASURED 2026-09-23 on a live MetaQuotes box: the child resolved
    ``Path.home()`` as ``C:\\users\\user`` -- Wine's USERPROFILE, not the Linux
    home -- so its ``MT5_ROOT`` was a different, empty directory. ``positions``
    then answered ``guard: {live: false, rules_armed: 0}`` while a guard was
    running with a rule armed and a refused close being retried, and the tool
    told the model that no guard was protecting anything. The roots are now
    written into the batch file the bridge is launched from.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    (cli.WINE_PREFIX / "drive_c" / "Python311").mkdir(parents=True, exist_ok=True)
    winpy = cli.WINE_PREFIX / "drive_c" / "Python311" / "python.exe"
    winpy.write_bytes(b"MZ")
    monkeypatch.setattr(cli, "win_python", lambda: winpy)
    monkeypatch.setattr(cli, "wine_bin", lambda: "wine")
    captured: dict[str, Any] = {}

    def _run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env") or {}
        bats = list((cli.WINE_PREFIX / "drive_c" / "mt5tmp").glob("*/run.bat"))
        assert len(bats) == 1, bats
        captured["bat"] = bats[0].read_text(encoding="utf-8")
        (bats[0].parent / "stdout.txt").write_text('{"ok": true}\n', encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", _run)

    code = cli._reexec_under_wine(["positions"])

    assert code == 0
    bat = captured["bat"]
    # The Wine Z: form, so the child does not care which drive is current, and
    # it points at the parent's real root rather than a guessed one.
    def _wine_form(path: Any) -> str:
        return "Z:" + str(path).replace("/", chr(92))

    assert f'set "MT5_ROOT={_wine_form(cli.MT5_ROOT)}"' in bat
    assert f'set "WINE_PREFIX={_wine_form(cli.WINE_PREFIX)}"' in bat
    # The guard's own files are under the root the child is told about: this is
    # the link that was broken, and it is what positions reads to answer "is a
    # guard watching a price".
    assert cli.GUARD_STATE_FILE == cli.MT5_ROOT / "guard" / "state.json"
    assert captured["env"].get("MT5_UNDER_WINE") == "1"


def test_two_bridge_invocations_never_share_a_temp_file(monkeypatch, tmp_path):
    """Two bridge actions at once must not write over each other's output.

    MEASURED 2026-09-24, live: an ``order`` sent while a ``watch`` was sampling
    made the watch report the ORDER's JSON as its own result. The paths were
    fixed (``C:\\mt5tmp\\run.bat`` and ``stdout.txt``), so the second invocation
    deleted the file the first was about to read and left its own answer there.

    That overlap is the INTENDED usage now rather than an accident: watching a
    live trade and acting on it in the same breath is what ``watch`` is for. A
    private directory per invocation is what makes it harmless.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    (cli.WINE_PREFIX / "drive_c" / "Python311").mkdir(parents=True, exist_ok=True)
    winpy = cli.WINE_PREFIX / "drive_c" / "Python311" / "python.exe"
    winpy.write_bytes(b"MZ")
    monkeypatch.setattr(cli, "win_python", lambda: winpy)
    monkeypatch.setattr(cli, "wine_bin", lambda: "wine")

    bats: list[str] = []
    outs: list[str] = []

    def _run(argv, **kwargs):
        bats.append(str(argv[-1]))
        # The batch redirect target, read from the batch file itself: the test
        # must not assume where the CLI decided to put it.
        bat = list((cli.WINE_PREFIX / "drive_c" / "mt5tmp").glob("*/run.bat"))
        assert len(bat) == 1, bat
        text = bat[0].read_text(encoding="utf-8")
        out = bat[0].parent / "stdout.txt"
        outs.append(str(out))
        assert str(out.name) in text
        out.write_text('{"ok": true}\n', encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", _run)

    cli._reexec_under_wine(["positions"])
    cli._reexec_under_wine(["quote", "EURUSD"])

    assert len(set(bats)) == 2, bats
    assert len(set(outs)) == 2, outs
    # And the scratch space is cleaned up, so a long session of bridge calls
    # does not grow the prefix one directory at a time.
    assert list((cli.WINE_PREFIX / "drive_c" / "mt5tmp").glob("*/run.bat")) == []


# ---------------------------------------------------------------------------
# The close-retry loop: driven by running the REAL watcher source.
#
# Every other guard test asserts on strings or on CLI payloads, which cannot see
# whether a refused close is actually retried. These run ``_GUARD_WATCH_SOURCE``
# as the Wine python would, against a fake MetaTrader5 and a clock the test
# drives -- so a 30 s retry deadline costs no real time and the assertions are
# about observed behaviour rather than about the shape of the code.
# ---------------------------------------------------------------------------


class _FakeClock:
    """A clock the test moves, so retry deadlines pass instantly."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = float(start)
        self.slept = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        seconds = max(float(seconds), 0.01)
        self.slept += seconds
        self.now += seconds

    def strftime(self, *_a: Any, **_k: Any) -> str:
        return "00:00:00"


class _FakeMT5:
    """Enough of MetaTrader5 to drive the watcher: ticks, positions, order_send.

    ``retcodes`` is the script a broker runs: one entry per send, the last one
    repeating forever. 10009 closes the position, anything else refuses it and
    leaves it open -- which is exactly the difference the retry loop exists for.
    """

    POSITION_TYPE_BUY = 0
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 2
    ORDER_FILLING_RETURN = 3
    ORDER_TIME_GTC = 0
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    COPY_TICKS_ALL = 0
    COPY_TICKS_TRADE = 1
    COPY_TICKS_INFO = 2

    def __init__(
        self,
        *,
        retcodes: tuple[int, ...] = (10009,),
        tickets: tuple[int, ...] = (777,),
        symbol: str = "EURUSD",
        bid: float = 1.14190,
        ask: float = 1.14210,
        tick_after: tuple[float, float] | None = None,
        tick_after_sends: int = 1,
        ticks: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
        tick_after_polls: tuple[int, float, float] | None = None,
        ticks_after_polls: tuple[int, list[dict[str, Any]]] | None = None,
        stops_level_points: int = 0,
        sltp_retcodes: tuple[int, ...] = (10009,),
    ) -> None:
        self.script = list(retcodes) or [10009]
        #: How much room the broker demands between the price and a stop, in
        #: points. Non-zero is how the "refused with 10016 Invalid stops" case is
        #: reproduced without a broker.
        self.stops_level_points = int(stops_level_points)
        self.sltp_script = list(sltp_retcodes) or [10009]
        #: Every TRADE_ACTION_SLTP sent, kept apart from ``sends`` so a test can
        #: read the stop moves without filtering the deals out of them.
        self.sltp_sends: list[dict[str, Any]] = []
        self.poll_count = 0
        self.swapped = False
        self.tick_after_polls = tick_after_polls
        #: Recorded ticks that reach the terminal only after a later poll, as
        #: (after_polls, rows). A stream that is static cannot show a crossing
        #: arriving BETWEEN two passes, which is the only way a crossing tick can
        #: be newer than a watermark that has already been armed -- and the case
        #: the scan exists for.
        self.ticks_after_polls = ticks_after_polls
        self.ticks_appended = False
        self.positions = [
            types.SimpleNamespace(
                ticket=ticket, symbol=symbol, type=self.POSITION_TYPE_BUY,
                volume=0.1, price_open=bid, sl=0.0, tp=0.0,
            )
            for ticket in tickets
        ]
        self.tick = types.SimpleNamespace(
            bid=bid, ask=ask, time=1_700_000_000, last=bid, volume=1,
            time_msc=1_700_000_000_000,
        )
        #: The RECORDED tick stream, as dicts keyed like the MT5 rows
        #: (``time_msc`` in ms, ``bid``/``ask``). Empty by default: a fake that
        #: has no stream must behave exactly like a terminal that cannot serve
        #: one, which is the fallback every pre-existing test relies on.
        self.ticks = list(ticks or ())
        #: Every ``since`` the watcher asked the recorded stream for, in seconds.
        #: Recorded so a test can assert WHICH CLOCK the request was expressed in:
        #: the terminal stamps ticks in its own (server) time, so a window built
        #: from this process's clock asks a question about the wrong hours.
        self.tick_windows: list[float] = []
        self.tick_after = tick_after
        self.tick_after_sends = tick_after_sends
        self.sends: list[dict[str, Any]] = []
        self.initialised = False

    def initialize(self) -> bool:
        self.initialised = True
        return True

    def last_error(self) -> tuple[int, str]:
        return (0, "no error")

    def symbol_select(self, _symbol: str, _enable: bool = True) -> bool:
        return True

    def symbol_info(self, _symbol: str) -> Any:
        return types.SimpleNamespace(
            filling_mode=self.ORDER_FILLING_FOK,
            trade_stops_level=self.stops_level_points,
            point=0.01,
        )

    def symbol_info_tick(self, _symbol: str) -> Any:
        self.poll_count += 1
        if (
            self.tick_after_polls is not None
            and not self.swapped
            and self.poll_count > int(self.tick_after_polls[0])
        ):
            # The market moves onto the level AFTER the first look, which is what
            # makes the tick SCAN run: on the pass a symbol is first seen the scan
            # is only armed at the current tick, so a crossing can only be named
            # on a later pass. Swapping on the first call reproduces that.
            self.swapped = True
            self.tick = types.SimpleNamespace(
                bid=float(self.tick_after_polls[1]),
                ask=float(self.tick_after_polls[2]),
                time=1_700_000_200, last=float(self.tick_after_polls[1]), volume=1,
                time_msc=self.tick.time_msc + 200,
            )
        if (
            self.ticks_after_polls is not None
            and not self.ticks_appended
            and self.poll_count > int(self.ticks_after_polls[0])
        ):
            self.ticks_appended = True
            self.ticks.extend(self.ticks_after_polls[1])
        return self.tick

    def copy_ticks_from(self, _symbol: str, since: Any, _limit: int = 20000,
                        _flags: int = 0) -> list[dict[str, Any]]:
        """The RECORDED stream, which is the point of the tick scan.

        Returns every fake tick at or after ``since`` (seconds), so a test can
        put ticks in the gap between two polls -- exactly what a 10 Hz sample of
        several ticks per second throws away -- and assert what the watcher did with
        them. An empty ``ticks`` list is the default, so every older test keeps
        exercising the degradation path where the read is simply unavailable.
        """
        self.tick_windows.append(float(since))
        if not self.ticks:
            return []
        floor_msc = int(float(since) * 1000.0)
        return [t for t in self.ticks if int(t["time_msc"]) >= floor_msc]

    def positions_get(self, ticket: int | None = None) -> list[Any]:
        if ticket is not None:
            return [p for p in self.positions if p.ticket == int(ticket)]
        return list(self.positions)

    def order_send(self, request: dict[str, Any]) -> Any:
        self.sends.append(dict(request))
        if int(request.get("action") or 0) == self.TRADE_ACTION_SLTP:
            # Moving a stop is not a deal: the position stays OPEN and the new
            # stop is what the next pass reads back off it, which is what makes a
            # trailing rule a loop rather than a one-shot.
            self.sltp_sends.append(dict(request))
            retcode = (
                self.sltp_script.pop(0) if len(self.sltp_script) > 1
                else self.sltp_script[0]
            )
            if retcode == 10009:
                for position in self.positions:
                    if position.ticket == int(request.get("position") or 0):
                        position.sl = float(request.get("sl") or 0.0)
            return types.SimpleNamespace(retcode=retcode, comment="moved", deal=0)
        if self.tick_after is not None and len(self.sends) == self.tick_after_sends:
            # The price moves back INSIDE the level while the refused close is
            # still pending -- the case that used to abandon the exit.
            self.tick = types.SimpleNamespace(
                bid=self.tick_after[0], ask=self.tick_after[1],
                time=1_700_000_100, last=self.tick_after[0], volume=1,
            )
        retcode = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if retcode == 10009:
            self.positions = [
                p for p in self.positions if p.ticket != int(request.get("position") or 0)
            ]
            return types.SimpleNamespace(retcode=10009, comment="done", deal=11)
        return types.SimpleNamespace(retcode=retcode, comment="market closed", deal=0)


def _run_the_watcher(
    cli: Any,
    monkeypatch: Any,
    tmp_path: Path,
    mt5: _FakeMT5,
    clock: _FakeClock,
    rules: list[dict[str, Any]],
    *,
    interval_ms: int = 100,
    max_seconds: int = 100,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], Path]:
    """Run the embedded watcher once, exactly as the sandbox would.

    Returns (timings-ish payload, event log, rules after the run, guard dir).
    """
    guard = cli.GUARD_DIR
    guard.mkdir(parents=True, exist_ok=True)
    cli.GUARD_RULES_FILE.write_text(json.dumps(rules), encoding="utf-8")
    cli.GUARD_EVENTS_FILE.write_text("", encoding="utf-8")
    if cli.GUARD_STOP_FILE.exists():
        cli.GUARD_STOP_FILE.unlink()

    fake_time = types.ModuleType("time")
    fake_time.__dict__.update(
        {k: v for k, v in vars(time).items() if not k.startswith("__")}
    )
    fake_time.time = clock.time
    fake_time.sleep = clock.sleep
    fake_time.strftime = clock.strftime
    monkeypatch.setitem(sys.modules, "time", fake_time)
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt5)
    monkeypatch.setattr(sys, "argv", [
        "guard_watch.py",
        "--rules", str(cli.GUARD_RULES_FILE),
        "--events", str(cli.GUARD_EVENTS_FILE),
        "--state", str(cli.GUARD_STATE_FILE),
        "--stop-file", str(cli.GUARD_STOP_FILE),
        "--interval-ms", str(interval_ms),
        "--max-seconds", str(max_seconds),
        "--deviation", "30",
    ])

    namespace: dict[str, Any] = {"__name__": "guard_watch_under_test"}
    exec(compile(cli._GUARD_WATCH_SOURCE, "<guard_watch>", "exec"), namespace)
    code = namespace["main"]()

    events = [
        json.loads(line)
        for line in cli.GUARD_EVENTS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    after = json.loads(cli.GUARD_RULES_FILE.read_text(encoding="utf-8"))
    state = json.loads(cli.GUARD_STATE_FILE.read_text(encoding="utf-8"))
    return {"code": code, "state": state, "clock": clock}, events, after, guard


def _rule(**overrides: Any) -> dict[str, Any]:
    rule = {
        "id": "g-retry", "symbol": "EURUSD", "op": ">=", "price": 1.0,
        "side": "mid", "once": True,
    }
    rule.update(overrides)
    return rule


def test_a_refused_close_is_retried_on_a_persisted_cooldown_then_given_up_loudly(
    monkeypatch, tmp_path
):
    """A persistent refusal must not be silent, and must not retry forever.

    MEASURED 2026-09-23: a refused close stayed armed and was retried only while
    the price remained beyond the level, with no deadline -- so a position could
    be abandoned by a price tick and retried 10x a second by a market that was
    simply closed. The case here is the worst one: the broker refuses every time.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock()
    mt5 = _FakeMT5(retcodes=(10018,), tickets=(777,))

    _run, events, rules_after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock, [_rule()], max_seconds=100
    )

    failures = [e for e in events if e["event"] == "close_failed"]
    gives_up = [e for e in events if e["event"] == "close_gave_up"]
    assert not [e for e in events if e["event"] == "fired"]

    first = [f for f in failures if f["retry_window"] == 1]

    # Bounded: the attempt cap lands before the 30 s deadline, and every attempt
    # of the first window is counted and marked as a retry after the first.
    assert len(first) == cli.GUARD_CLOSE_RETRY_MAX_ATTEMPTS == 20
    assert [f["attempt"] for f in first] == list(range(1, 21))
    assert first[0]["retry"] is False and all(f["retry"] for f in first[1:])

    # The cooldown is PERSISTED, which it was not: the rules file is re-read each
    # pass, so a failure that only set ``last_attempt`` in memory re-sent a deal
    # on every 100 ms poll. The gaps are what prove the fix.
    gaps = [
        round(b["trigger_ts"] - a["trigger_ts"], 2)
        for a, b in zip(first, first[1:])
    ]
    # The opening burst is FAST. A refusal is usually transient -- a requote, or a
    # price that moved under a market order -- and the flat 1.5 s cooldown this
    # replaced sat on a deal the broker would have taken ~300 ms later.
    burst = gaps[: cli.GUARD_CLOSE_RETRY_FAST_ATTEMPTS - 1]
    assert burst and all(g <= 0.5 for g in burst), gaps
    # ...and the burst is BOUNDED, which is what stops "fast" becoming the 10 Hz
    # refusal storm this whole retry state was written to remove. Once the burst
    # is spent the cadence settles back to the flat cooldown, so a HARD refusal
    # (a closed market) is not hammered any harder than it used to be.
    settled = gaps[cli.GUARD_CLOSE_RETRY_FAST_ATTEMPTS - 1:]
    assert settled and all(g >= 1.4 for g in settled), gaps
    # ...and one refused close is one deal sent to the broker, not one per poll.
    assert len(mt5.sends) == len(failures)

    # The give-up is a real event with the broker's own answer and the tickets
    # that are still open -- not a line in a log nobody reads.
    assert len(gives_up) == 1
    give = gives_up[0]
    assert give["attempts"] == 20 and give["retcodes"] == [10018]
    assert give["still_open_tickets"] == [777]
    assert give["retry_in_s"] == cli.GUARD_CLOSE_RETRY_PARK_SECONDS == 60.0

    # The rule is PARKED rather than retried at speed, then a FRESH window opens
    # (a weekend gap is minutes away from closable, not never).
    later = [f for f in failures if f["trigger_ts"] > give["ts"]]
    assert later, "the rule must come back for another window, not be abandoned"
    assert all(f["trigger_ts"] >= give["ts"] + 59.0 for f in later)
    assert later[0]["attempt"] == 1 and later[0]["retry_window"] == 2

    # Nothing was consumed by a failure: the rule is still armed, with the retry
    # state on it in the file the watcher rewrites.
    assert [r["id"] for r in rules_after] == ["g-retry"]
    assert rules_after[0]["gave_up_attempts"] == 20
    assert rules_after[0]["gave_up_retcodes"] == [10018]
    assert "gave_up_at" in rules_after[0]
    assert events[-1]["event"] == "watcher_stop"
    assert events[-1]["exit_reason"] == "max_seconds"


def test_a_refused_close_is_retried_even_after_the_price_leaves_the_level(
    monkeypatch, tmp_path
):
    """A DECIDED close is retried until it is confirmed, not while it triggers.

    The broker refuses the deal on the tick that touched the level and the price
    ticks straight back inside it. Before the retry loop the rule stopped being
    evaluated as a close (its condition no longer held), so the position stayed
    open, the guard stayed live, and nothing said so.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock()
    mt5 = _FakeMT5(
        retcodes=(10018, 10009), tickets=(888,),
        bid=1.09950, ask=1.09970,
        tick_after=(1.15000, 1.15020),
    )
    # Triggered while the price is BELOW 1.10, which it leaves after the refusal.
    rules = [_rule(price=1.10, op="<=")]

    _run, events, rules_after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock, rules, max_seconds=60
    )

    failures = [e for e in events if e["event"] == "close_failed"]
    fired = [e for e in events if e["event"] == "fired"]
    assert len(failures) == 1 and failures[0]["attempt"] == 1
    assert len(fired) == 1, "the retry must not depend on the price still triggering"

    went_out = fired[0]
    assert went_out["retry"] is True and went_out["attempt"] == 2
    # The FIRST retry of a window goes out on the fast cooldown, not the flat
    # 1.5 s one: this deal was refused and then accepted one burst-step later.
    assert 0.0 < went_out["pending_seconds"] <= 0.5
    # It fired on a price that NO LONGER satisfies the rule -- that is the point.
    assert (went_out["bid"] + went_out["ask"]) / 2.0 > 1.10
    assert len(mt5.sends) == 2

    # A CONFIRMED close is what consumes the rule, and only then.
    assert rules_after == []
    assert mt5.positions == []
    assert events[-1]["exit_reason"] == "rules_satisfied"
    assert not [e for e in events if e["event"] == "close_gave_up"]


def test_the_watcher_resumes_a_retry_it_inherited_from_the_rules(
    monkeypatch, tmp_path
):
    """A refused close outlives the process that made it.

    The retry lives on the rule, so a watcher that is restarted (a sandbox pause,
    a re-arm, a crash) picks the attempt up instead of forgetting a deal the
    broker already refused -- and says that it is doing so.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock()
    mt5 = _FakeMT5(retcodes=(10009,), tickets=(999,))
    started = clock.now
    rules = [_rule(pending_since=started - 10.0, pending_attempts=3,
                   pending_deadline=started + 20.0)]

    _run, events, rules_after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock, rules, max_seconds=30
    )

    resumed = [e for e in events if e["event"] == "close_retry_resumed"]
    assert len(resumed) == 1
    assert resumed[0]["rules"] == [
        {"rule_id": "g-retry", "symbol": "EURUSD", "attempts": 3}
    ]
    fired = [e for e in events if e["event"] == "fired"]
    assert len(fired) == 1
    assert fired[0]["retry"] is True and fired[0]["attempt"] == 4
    assert fired[0]["pending_seconds"] >= 9.0
    assert rules_after == []
    assert mt5.positions == []


def test_the_cli_and_the_watcher_agree_on_the_retry_constants(monkeypatch, tmp_path):
    """The retry numbers exist twice and must not drift apart.

    The watcher's copy runs in the sandbox's Wine python and cannot import the
    CLI's; the CLI's copy is what ``guard status`` uses to say how long a refused
    close will keep trying. Two copies of a number is a drift waiting to happen,
    so they are compared here.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    source = cli._GUARD_WATCH_SOURCE

    for name, value in (
        ("RETRY_COOLDOWN_SECONDS", cli.GUARD_CLOSE_RETRY_COOLDOWN_SECONDS),
        ("CLOSE_RETRY_DEADLINE_SECONDS", cli.GUARD_CLOSE_RETRY_DEADLINE_SECONDS),
        ("CLOSE_RETRY_MAX_ATTEMPTS", cli.GUARD_CLOSE_RETRY_MAX_ATTEMPTS),
        ("CLOSE_RETRY_PARK_SECONDS", cli.GUARD_CLOSE_RETRY_PARK_SECONDS),
    ):
        assert f"{name} = {value!r}" in source, name

    # Both bounds are enforced, the terminal event exists, and the retry state is
    # written to the rules file (the failure path used to leave it in memory).
    assert "attempt >= CLOSE_RETRY_MAX_ATTEMPTS or now >= deadline" in source
    assert '"event": "close_gave_up"' in source
    assert 'rule["pending_attempts"] = attempt' in source
    assert 'rule["parked_until"] = now + CLOSE_RETRY_PARK_SECONDS' in source
    assert "pending = bool(rule.get(\"pending_since\"))" in source


def test_a_close_the_watcher_is_still_retrying_is_not_a_healthy_status(
    monkeypatch, tmp_path
):
    """ "Armed" and "armed, and the broker has refused my exit" are not the same.

    A caller who asked to be out is still IN, so the status must not read as
    healthy -- but it must also not send them off to re-arm: the watcher is
    retrying, and the recovery line says so.
    """
    import argparse

    cli = _broker_cli(monkeypatch, tmp_path)
    cli.GUARD_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    cli.GUARD_RULES_FILE.write_text(json.dumps([{
        "id": "g1", "symbol": "EURUSD", "op": ">=", "price": 1.14,
        "pending_since": now - 4.0, "pending_attempts": 3,
        "pending_deadline": now + 26.0, "last_attempt": now,
    }]), encoding="utf-8")
    cli.GUARD_STATE_FILE.write_text(json.dumps({
        "status": "running", "heartbeat": now, "polls": 41, "interval_ms": 100,
        "max_seconds": 0, "prices": {"EURUSD": 1.142}, "rules": 1,
    }), encoding="utf-8")

    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "emit",
                        lambda payload, **_k: captured.update(payload) or 0)
    cli.cmd_guard(argparse.Namespace(guard_action="status"))

    assert captured["running"] is True
    assert captured["ok"] is False
    assert captured["alert"] == "close_retrying"
    assert captured["retrying"][0]["rule_id"] == "g1"
    assert captured["retrying"][0]["attempts"] == 3
    assert captured["retrying"][0]["trying_for_s"] >= 3.9
    assert 0.0 <= captured["retrying"][0]["retry_in_s"] <= 1.6
    assert captured["retrying"][0]["deadline_in_s"] > 20.0
    assert "not out yet" in captured["warning"]
    assert "retries on its own" in captured["recovery"]

    # The stopped-watcher alarm still outranks it, and positions carries the same
    # reading, so the two surfaces cannot disagree.
    assert cli._guard_summary()["alert"] == "close_retrying"
    assert cli._guard_summary()["retrying"][0]["attempts"] == 3


def test_a_close_the_watcher_gave_up_on_is_an_alarm_carrying_the_brokers_codes(
    monkeypatch, tmp_path
):
    """Giving up is a position that needs the caller, so it is not ok=true."""
    import argparse

    cli = _broker_cli(monkeypatch, tmp_path)
    cli.GUARD_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    cli.GUARD_RULES_FILE.write_text(json.dumps([{
        "id": "g1", "symbol": "EURUSD", "op": ">=", "price": 1.14,
        "pending_windows": 1, "gave_up_at": now - 5.0, "gave_up_attempts": 20,
        "gave_up_retcodes": [10018], "parked_until": now + 55.0,
    }]), encoding="utf-8")
    cli.GUARD_STATE_FILE.write_text(json.dumps({
        "status": "running", "heartbeat": now, "polls": 500, "interval_ms": 100,
        "max_seconds": 0, "prices": {"EURUSD": 1.142}, "rules": 1,
    }), encoding="utf-8")

    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "emit",
                        lambda payload, **_k: captured.update(payload) or 0)
    cli.cmd_guard(argparse.Namespace(guard_action="status"))

    assert captured["ok"] is False
    assert captured["alert"] == "close_gave_up"
    assert captured["gave_up"][0]["attempts"] == 20
    assert captured["gave_up"][0]["retcodes"] == [10018]
    assert captured["gave_up"][0]["gave_up_s_ago"] >= 4.9
    assert 50.0 <= captured["gave_up"][0]["next_window_in_s"] <= 55.0
    assert "STILL OPEN" in captured["warning"]
    assert "action='close'" in captured["recovery"]
    assert cli._guard_summary()["alert"] == "close_gave_up"

    # A retrying close and a given-up close are different answers, and gave-up
    # wins: it is the one that needs a human.
    cli.GUARD_RULES_FILE.write_text(json.dumps([{
        "id": "g1", "symbol": "EURUSD", "pending_since": now - 2.0,
        "pending_attempts": 2, "pending_deadline": now + 28.0,
        "last_attempt": now, "gave_up_at": now - 300.0, "parked_until": now,
    }]), encoding="utf-8")
    assert cli._guard_summary()["alert"] == "close_retrying"


def test_the_watcher_writes_its_retry_state_to_the_rules_not_a_stale_copy(
    monkeypatch, tmp_path
):
    """The rule the watcher rewrites is what the CLI reads: no second source.

    ``guard status`` reports the retry from the rules file the watcher keeps, so
    the state file's view is only a convenience -- and it must agree.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock()
    mt5 = _FakeMT5(retcodes=(10018,), tickets=(777,))

    run, events, _after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock, [_rule()], max_seconds=6
    )

    # The COUNT is not the assertion -- how many attempts fit in the window is a
    # consequence of the cadence -- so it is derived from the events: state and
    # rules must agree on the same attempt number, whatever that number is.
    failures = [e for e in events if e["event"] == "close_failed"]
    attempts = [f["attempt"] for f in failures if f["retry_window"] == 1]
    assert attempts, events
    assert run["state"]["retrying"] == {"g-retry": max(attempts)}, run["state"]
    assert run["state"]["rules"] == 1
    on_disk = json.loads(cli.GUARD_RULES_FILE.read_text(encoding="utf-8"))
    assert on_disk[0]["pending_attempts"] == max(attempts)
    assert cli._guard_summary()["retrying"], "the CLI reads the same retry state"


def test_a_level_touched_and_reverted_between_polls_is_reported_and_not_closed(
    monkeypatch, tmp_path
):
    """The tick the 10 Hz sample could not see is REPORTED, and NOT acted on.

    MEASURED 2026-09-23 on a live box: the watcher reads ONE tick per poll while
    EURUSD records several a second, so the ticks in between were never examined
    and a level touched and reverted inside a 100 ms gap was invisible. This is that tick --
    and the decision it must produce is the OPPOSITE of a fire: the price is back
    inside the level, so closing now would fill at a price the caller never asked
    to be out at. Seeing the tick must not become acting on the tick.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock()
    # The level is 1.15. The live price never reaches it; ONE recorded tick does.
    mt5 = _FakeMT5(
        bid=1.14190, ask=1.14210,
        ticks=[
            {"time_msc": 1_700_000_000_400, "bid": 1.15000, "ask": 1.15000},
            {"time_msc": 1_700_000_000_600, "bid": 1.14200, "ask": 1.14220},
        ],
    )

    _run, events, rules_after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock, [_rule(price=1.15)], max_seconds=2
    )

    assert not [e for e in events if e["event"] == "fired"], events
    assert mt5.sends == [], "a reverted touch must not send a deal"

    near = [e for e in events if e["event"] == "level_touched_then_reverted"]
    assert len(near) == 1, events
    miss = near[0]
    assert miss["rule_id"] == "g-retry" and miss["level"] == 1.15
    # mid of (1.14190, 1.14210) -- the price it is back INSIDE the level at.
    assert miss["touch_price"] == 1.15 and miss["price_now"] == pytest.approx(1.14200)
    assert miss["ticks_scanned"] >= 1
    assert "NOT closing" in miss["detail"]

    # Reported, not consumed: the rule is still armed and still watching.
    assert [r["id"] for r in rules_after] == ["g-retry"]


def test_the_scan_never_counts_a_tick_from_before_the_rule_was_armed(
    monkeypatch, tmp_path
):
    """No backfill: history is not this rule's crossing.

    On the pass a symbol is first seen the scan is ARMED at the tick already
    visible rather than walked backwards. Without that, the first pass would read
    the lookback window as ticks it had just watched and invent a crossing that
    happened before the rule existed -- and report it as a near miss or, worse,
    attach it to a real fire as the trigger.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock()
    # Satisfied ALREADY, with a crossing-looking tick stamped BEFORE the live one.
    mt5 = _FakeMT5(
        bid=1.15000, ask=1.15000,
        ticks=[{"time_msc": 1_699_999_999_000, "bid": 1.16000, "ask": 1.16000}],
    )

    _run, events, _after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock, [_rule(price=1.15)], max_seconds=1
    )

    fired = [e for e in events if e["event"] == "fired"]
    assert len(fired) == 1, events
    # It fires on the LIVE tick, and claims no crossing from the pre-arm history.
    assert "crossing_msc" not in fired[0]
    assert "crossing_price" not in fired[0]
    assert not [e for e in events if e["event"] == "level_touched_then_reverted"]


def test_a_fire_names_the_tick_that_actually_crossed(monkeypatch, tmp_path):
    """The reported trigger is the tick that crossed, not the poll that noticed.

    A fire whose ``trigger_price`` is the poll sample says "we sampled this"; the
    crossing fields say how long the level had really been crossed. That is the
    difference the recorded stream buys, and it is reported WITHOUT changing what
    the deal is sent at: the order still uses the live tick.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock()
    mt5 = _FakeMT5(
        # Below the level on the first look, at it afterwards -- so the scan is
        # armed on pass 1 and the crossing is named on pass 2.
        bid=1.14190, ask=1.14210,
        tick_after_polls=(1, 1.15030, 1.15050),
        # Stamped BETWEEN the first poll's tick (…000) and the poll that noticed
        # (…200) -- i.e. in the gap the loop samples over.
        ticks=[{"time_msc": 1_700_000_000_150, "bid": 1.15, "ask": 1.15}],
    )

    _run, events, _after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock, [_rule(price=1.15)], max_seconds=2
    )

    fired = [e for e in events if e["event"] == "fired"]
    assert len(fired) == 1, events
    event = fired[0]
    assert event["crossing_msc"] == 1_700_000_000_150
    assert event["crossing_price"] == 1.15
    assert event["crossing_age_ms"] >= 0.0
    assert event["ticks_scanned"] >= 1
    # The crossing sits BETWEEN the first poll's tick and the poll that noticed
    # it, which is the whole claim: it happened in the gap the old loop sampled
    # over, and it is now named instead of lost.
    assert event["crossing_msc"] > 1_700_000_000_000
    assert event["crossing_msc"] < mt5.tick.time_msc
    # And the deal still went out at the live price, not the historical one.
    assert mt5.sends and mt5.sends[0]["price"] == pytest.approx(1.15030)


def test_a_second_rule_on_the_same_symbol_still_sees_the_crossing(monkeypatch, tmp_path):
    """The ticks of a pass belong to the SYMBOL, not to whichever rule read first.

    MEASURED live 2026-09-23, and the bug this pins: two rules armed on EURUSD --
    a far level that never fires, listed FIRST, and the level that actually fired,
    listed second. The first rule's read advanced the per-symbol watermark to the
    crossing tick, so the firing rule's own read found nothing newer than its own
    watermark and its fire carried NO crossing. It closed the position correctly
    (retcode 10009, 139.6 ms) and named no tick -- losing exactly the evidence the
    scan was added for, while looking healthy. Every rule on a symbol now measures
    its level against the ticks that symbol produced in that pass.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock()
    mt5 = _FakeMT5(
        bid=1.14190, ask=1.14210,
        # Two rules, so a poll is two visits: the swap has to land on the SECOND
        # pass, which is the pass the bug is on (pass 1 arms and reads nothing).
        tick_after_polls=(2, 1.15030, 1.15050),
        # The crossing tick reaches the terminal only on the second pass, so it is
        # genuinely newer than the watermark either rule reads against -- and the
        # rule listed FIRST consumes it.
        ticks=[{"time_msc": 1_700_000_000_000, "bid": 1.14200, "ask": 1.14220}],
        ticks_after_polls=(2, [{"time_msc": 1_700_000_000_150, "bid": 1.15,
                               "ask": 1.15}]),
    )

    _run, events, _after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock,
        [_rule(id="first-far", price=1.5), _rule(id="second-near", price=1.15)],
        max_seconds=2,
    )

    fired = [e for e in events if e["event"] == "fired"]
    assert len(fired) == 1, events
    assert fired[0]["rule_id"] == "second-near"
    assert fired[0]["crossing_msc"] == 1_700_000_000_150
    assert fired[0]["crossing_price"] == 1.15
    assert fired[0]["crossing_age_ms"] == pytest.approx(50.0, abs=1.0)
    assert fired[0]["ticks_scanned"] >= 1
    assert mt5.sends and mt5.sends[0]["price"] == pytest.approx(1.15030)
    assert not [e for e in events if e["event"] == "level_touched_then_reverted"]


def test_the_tick_scan_asks_in_the_tick_clock_not_this_processs(monkeypatch, tmp_path):
    """The scan is expressed in the TICK clock, because that is the one the rows use.

    MEASURED 2026-09-23 on a live box, and the reason the first live run of this
    scan reported ``ticks_scanned: {}``: the terminal stamps ticks in the SERVER's
    time, which ran 10799 s (~3 h) AHEAD of the box. The scan asked for "recorded
    ticks since now - 3 s" with ``now`` from the box, so the window opened three
    hours before the present and was answered with 20000 rows of history, every
    one of them older than the live tick -- older than the scan's own watermark,
    so every pass found nothing fresh, forever. A guard that misses the ticks
    between two polls and a guard whose scan silently reads the wrong hours look
    identical
    from the outside: both say nothing.

    This pins the two clocks apart by exactly that measured skew and asserts the
    window is expressed in the tick's hours, and that the crossing is still named
    and still aged against the tick.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock(start=1_700_000_000.0 - 10_799.0)  # the box, ~3 h behind
    mt5 = _FakeMT5(
        bid=1.14190, ask=1.14210,
        tick_after_polls=(1, 1.15030, 1.15050),
        ticks=[{"time_msc": 1_700_000_000_150, "bid": 1.15, "ask": 1.15}],
    )

    _run, events, _after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock, [_rule(price=1.15)], max_seconds=2
    )

    assert mt5.tick_windows, "the scan must actually ask the recorded stream"
    assert clock.now < 1_700_000_000.0, "the box clock really is behind the tick"
    for since in mt5.tick_windows:
        # In the tick's hours: inside the lookback of the live tick ...
        assert since > 1_700_000_000.0 - 60.0, since
        # ... and not in the box's, which is the bug that was measured live.
        assert since > clock.now, since

    fired = [e for e in events if e["event"] == "fired"]
    assert len(fired) == 1, events
    event = fired[0]
    assert event["crossing_msc"] == 1_700_000_000_150
    assert event["ticks_scanned"] >= 1
    # 50 ms between the crossing and the live tick that noticed it. Read against
    # the box clock this difference is NEGATIVE (the crossing is stamped in the
    # future), which is how the age silently became a constant 0.0.
    assert event["crossing_age_ms"] == pytest.approx(50.0, abs=1.0)


def test_a_skewed_tick_clock_still_ages_a_near_miss_correctly(monkeypatch, tmp_path):
    """The near-miss age is read in the tick clock too, not clamped to zero.

    Same measured skew as the fire above, on the other path that ages a tick: a
    level touched and already back inside. Aged against the box clock the touch
    is always "in the future", so the reported age would always be 0 ms -- the
    line would look right and be worthless. The touch here is 50 ms old and must
    be reported as 50 ms old.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock(start=1_700_000_000.0 - 10_799.0)
    mt5 = _FakeMT5(
        # Below the level throughout: the only tick at the level is RECORDED, and
        # the live poll that notices it is already back inside.
        bid=1.14190, ask=1.14210,
        tick_after_polls=(1, 1.14200, 1.14220),
        ticks=[{"time_msc": 1_700_000_000_150, "bid": 1.15, "ask": 1.15}],
    )

    _run, events, _after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock, [_rule(price=1.15)], max_seconds=2
    )

    near = [e for e in events if e["event"] == "level_touched_then_reverted"]
    assert len(near) == 1, events
    assert near[0]["touch_price"] == 1.15
    assert near[0]["touch_age_ms"] == pytest.approx(50.0, abs=1.0)
    assert not [e for e in events if e["event"] == "fired"], events


def test_guard_watch_returns_the_moment_something_happens(monkeypatch, tmp_path):
    """A blocking status OBSERVES the exit instead of asserting it is monitored.

    "The guard is monitoring" is a claim until something watches it. This is the
    watch: it samples and returns the moment the event log grows, carrying the
    event, so the caller can report what HAPPENED rather than what is configured.
    """
    import threading

    cli = _broker_cli(monkeypatch, tmp_path)
    cli.GUARD_DIR.mkdir(parents=True, exist_ok=True)
    cli.GUARD_EVENTS_FILE.write_text("", encoding="utf-8")

    def write_fire() -> None:
        with cli.GUARD_EVENTS_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "event": "fired", "rule_id": "g-watch", "symbol": "EURUSD",
                "trigger_price": 1.15, "latency_ms": 146.1,
            }) + "\n")

    timer = threading.Timer(0.3, write_fire)
    timer.start()
    try:
        watched = cli._guard_wait(5.0, 0.2)
    finally:
        timer.cancel()

    assert watched["timed_out"] is False
    assert watched["observed_event"] == "fired"
    assert watched["observed"][-1]["latency_ms"] == 146.1
    assert watched["samples"] >= 1
    # It returned because of the event, not because the budget ran out.
    assert watched["waited_s"] < 5.0
    assert "observed 'fired'" in watched["note"]


def test_guard_watch_says_when_nothing_happened(monkeypatch, tmp_path):
    """"Nothing happened" must be reported as nothing, never as an event.

    A watch that timed out and a watch that saw a fire are different answers, and
    conflating them is how a caller ends up reporting coverage it never observed.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    cli.GUARD_DIR.mkdir(parents=True, exist_ok=True)
    cli.GUARD_EVENTS_FILE.write_text("", encoding="utf-8")

    watched = cli._guard_wait(0.5, 0.2)

    assert watched["timed_out"] is True
    assert watched["observed"] == [] and watched["observed_event"] is None
    assert watched["samples"] >= 1
    assert "nothing happened" in watched["note"]
    # The cap is stated in the answer, and it respects the sandbox ceiling.
    assert watched["capped_at_s"] == cli.GUARD_MAX_WAIT_SECONDS <= 120.0


def test_guard_watch_reaches_the_cli_and_the_wait_is_bounded(monkeypatch, tmp_path):
    """The tool passes the watch through, and the wait can never outrun its cap."""
    watched = build_cli_command(
        "guard",
        {"guard_action": "status", "wait_seconds": 30, "poll_seconds": 2},
    )
    assert "--wait-seconds 30" in watched
    assert "--poll-seconds 2" in watched

    # No wait asked for -> no flag, so a plain status stays a snapshot.
    plain = build_cli_command("guard", {"guard_action": "status"})
    assert "--wait-seconds" not in plain

    # The cap is enforced CLI-side, so a caller asking for an hour cannot hold a
    # sandbox command open past the ceiling and get killed mid-wait with no JSON.
    assert _broker_cli(monkeypatch, tmp_path).GUARD_MAX_WAIT_SECONDS <= 120.0


# --------------------------------------------------------------------------- #
# Following a long-running thing: the install, and a live trade
# --------------------------------------------------------------------------- #
def test_status_watch_returns_the_moment_the_install_advances(monkeypatch, tmp_path):
    """Following an install must OBSERVE it advance, not assert that it is.

    ``install`` is detached, so ``status`` was the only way to follow it -- and
    ``status`` answered instantly. A caller in that position repeats "still
    installing" each turn with nothing behind it. This waits for the stage file
    to move and returns the moment it does, with the stage it moved TO.
    """
    import threading

    cli = _broker_cli(monkeypatch, tmp_path)
    cli.MT5_ROOT.mkdir(parents=True, exist_ok=True)
    (cli.MT5_ROOT / "install.status").write_text("downloading|wine", encoding="utf-8")
    monkeypatch.setattr(cli, "_installer_alive", lambda: True)

    def advance() -> None:
        (cli.MT5_ROOT / "install.status").write_text(
            "done|install complete", encoding="utf-8"
        )

    timer = threading.Timer(0.3, advance)
    timer.start()
    try:
        watched = cli._install_wait(5.0, 0.2)
    finally:
        timer.cancel()

    assert watched["timed_out"] is False
    assert watched["observed_event"] == "install_done"
    assert watched["observed"][-1]["from_stage"] == "downloading"
    assert watched["observed"][-1]["stage"] == "done"
    assert watched["samples"] >= 1
    # It returned because of the change, not because the budget ran out.
    assert watched["waited_s"] < 5.0
    assert "observed 'install_done'" in watched["note"]


def test_status_watch_reports_log_growth_as_progress(monkeypatch, tmp_path):
    """A stage that has not changed is not the same as an install that has not.

    Most of a Wine + MT5 install is one long stage with output scrolling past.
    Without this the caller either waits for a stage change that is minutes away
    or concludes nothing is happening while the log is being written to.
    """
    import threading

    cli = _broker_cli(monkeypatch, tmp_path)
    cli.MT5_ROOT.mkdir(parents=True, exist_ok=True)
    log = cli.MT5_ROOT / "install.log"
    log.write_text("step 1\n", encoding="utf-8")
    (cli.MT5_ROOT / "install.status").write_text("downloading|wine", encoding="utf-8")
    monkeypatch.setattr(cli, "_installer_alive", lambda: True)

    def grow() -> None:
        with log.open("a", encoding="utf-8") as fh:
            fh.write("step 2\n")

    timer = threading.Timer(0.3, grow)
    timer.start()
    try:
        watched = cli._install_wait(5.0, 0.2)
    finally:
        timer.cancel()

    assert watched["observed_event"] == "install_log_grew"
    assert watched["observed"][-1]["grew_bytes"] == len("step 2\n")


def test_status_watch_notices_the_installer_dying_without_a_stage_change(
    monkeypatch, tmp_path,
):
    """The installer disappearing IS the news, even if the stage file never moved.

    A crashed installer leaves the last stage it wrote on disk. A wait that only
    looked at the stage would sit there until the budget ran out and then report
    "nothing moved", while the thing doing the work had already gone.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    cli.MT5_ROOT.mkdir(parents=True, exist_ok=True)
    log = cli.MT5_ROOT / "install.log"
    log.write_text("step 1\n", encoding="utf-8")
    (cli.MT5_ROOT / "install.status").write_text("downloading|wine", encoding="utf-8")
    alive = {"n": 0}

    def flaky() -> bool:
        alive["n"] += 1
        if alive["n"] > 1:
            # The exit also writes to the log, because ending IS the installer's
            # last act. This is what makes the ORDER of the checks matter: read
            # the log first and the caller is told "it wrote something" and never
            # told that the process doing the work is gone.
            with log.open("a", encoding="utf-8") as fh:
                fh.write("bye\n")
            return False
        return True

    monkeypatch.setattr(cli, "_installer_alive", flaky)

    watched = cli._install_wait(5.0, 0.2)

    assert watched["observed_event"] == "installer_exited"
    assert watched["to"]["installer_alive"] is False
    assert watched["timed_out"] is False


def test_status_watch_against_a_finished_install_does_not_stall(monkeypatch, tmp_path):
    """Nothing to wait for is answered immediately, and named.

    A wait against a finished install is a 120 s stall that then reports a
    timeout, which reads as "something is wrong" when the truth is "it is over".
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    cli.MT5_ROOT.mkdir(parents=True, exist_ok=True)
    (cli.MT5_ROOT / "install.status").write_text("done|install complete", encoding="utf-8")
    monkeypatch.setattr(cli, "_installer_alive", lambda: False)

    watched = cli._install_wait(30.0, 0.2)

    assert watched["observed_event"] == "install_already_done"
    assert watched["waited_s"] == 0.0 and watched["samples"] == 0
    assert watched["timed_out"] is False


def test_status_watch_says_when_nothing_moved(monkeypatch, tmp_path):
    """"Still installing" must be reported as an observation, never as progress."""
    cli = _broker_cli(monkeypatch, tmp_path)
    cli.MT5_ROOT.mkdir(parents=True, exist_ok=True)
    (cli.MT5_ROOT / "install.status").write_text("downloading|wine", encoding="utf-8")
    monkeypatch.setattr(cli, "_installer_alive", lambda: True)

    watched = cli._install_wait(0.5, 0.2)

    assert watched["timed_out"] is True
    assert watched["observed"] == [] and watched["observed_event"] is None
    assert watched["samples"] >= 1
    assert "nothing moved" in watched["note"]
    # The cap plus the tool's slack for the round trip has to land inside the
    # 120 s ceiling for ONE sandbox command, or the wait would be killed
    # mid-flight and return no JSON at all.
    assert watched["capped_at_s"] == cli.WATCH_MAX_WAIT_SECONDS
    assert cli.WATCH_MAX_WAIT_SECONDS + 30 <= 120.0


def test_a_plain_status_is_still_a_snapshot(monkeypatch, tmp_path):
    """No wait asked for means no wait: the existing contract is unchanged."""
    cli = _broker_cli(monkeypatch, tmp_path)
    cli.MT5_ROOT.mkdir(parents=True, exist_ok=True)
    (cli.MT5_ROOT / "install.status").write_text("done|install complete", encoding="utf-8")

    args = types.SimpleNamespace(lines=5, wait_seconds=0.0, poll_seconds=2.0)
    text = _capture_emit(cli, cli.cmd_status, args)
    assert "watched" not in json.loads(text)


def test_status_watch_reaches_the_cli_and_the_wait_is_bounded(monkeypatch, tmp_path):
    """The tool passes the install watch through, and never past its ceiling."""
    watching = build_cli_command(
        "status", {"wait_seconds": 45, "poll_seconds": 3, "lines": 40}
    )
    assert "mt5_cli.py status" in watching
    assert "--wait-seconds 45" in watching and "--poll-seconds 3" in watching
    assert "--lines 40" in watching

    # No wait asked for -> no flags, so a plain status stays a snapshot.
    plain = build_cli_command("status", {"lines": 25})
    assert "--wait-seconds" not in plain and "--poll-seconds" not in plain

    cli = _broker_cli(monkeypatch, tmp_path)
    assert cli.WATCH_MAX_WAIT_SECONDS + 30 <= 120.0
    # One number for every watch path, or one of them would be holding a
    # command open past the ceiling the others respect.
    assert cli.GUARD_MAX_WAIT_SECONDS == cli.WATCH_MAX_WAIT_SECONDS


# --------------------------------------------------------------------------- #
# `watch`: one frame of a live trade instead of three photographs of it
# --------------------------------------------------------------------------- #
def _watch_position(ticket: int, symbol: str, price: float = 1.14190):
    return types.SimpleNamespace(
        ticket=ticket,
        symbol=symbol,
        _asdict=lambda: {
            "ticket": ticket, "symbol": symbol, "volume": 0.1, "price_open": price,
            "sl": 0.0, "tp": 0.0, "profit": 0.0,
        },
    )


class _WatchMT5:
    """Just enough of MetaTrader5 to drive ``watch``: positions and ticks."""

    def __init__(self, *, positions=(), ticks=None, digits=5):
        self._positions = list(positions)
        self.ticks = dict(ticks or {})
        self.digits = digits
        self.selected: list[str] = []
        self.unavailable = False

    def initialize(self):  # noqa: ANN201
        return True

    def last_error(self):  # noqa: ANN201
        return (0, "no error")

    def symbol_select(self, name, enable=True):  # noqa: ANN001, ANN201
        self.selected.append(name)
        return True

    def symbol_info(self, name):  # noqa: ANN001, ANN201
        return types.SimpleNamespace(name=name, digits=self.digits)

    def symbol_info_tick(self, name):  # noqa: ANN001, ANN201
        row = self.ticks.get(name)
        if row is None:
            return None
        bid, ask = row
        return types.SimpleNamespace(
            bid=bid, ask=ask, last=bid, volume=1, time=1_700_000_000,
            time_msc=1_700_000_000_000,
        )

    def positions_get(self):  # noqa: ANN201
        if self.unavailable:
            return None
        return tuple(self._positions)


def _serve_watch(monkeypatch, cli, mt5) -> None:
    monkeypatch.setattr(cli, "require_bridge", lambda: (mt5, None))


def _live_guard_state(cli, **extra) -> None:
    cli.GUARD_DIR.mkdir(parents=True, exist_ok=True)
    cli.GUARD_EVENTS_FILE.write_text("", encoding="utf-8")
    state = {"status": "running", "heartbeat": time.time(), "rules": 1, "polls": 7}
    state.update(extra)
    cli.GUARD_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def _capture_emit(cli, func, args) -> str:
    """Run a CLI command and return the JSON it printed."""
    out: list[str] = []
    original = cli.emit
    try:
        cli.emit = lambda payload, **kw: (out.append(json.dumps(payload)), 0)[1]
        func(args)
    finally:
        cli.emit = original
    assert out, "the command emitted nothing"
    return out[-1]


def test_watch_returns_the_moment_a_rule_fires(monkeypatch, tmp_path):
    """A live trade is OBSERVED, not assumed: the fire ends the wait.

    This is the whole point of the action. "The guard is monitoring this
    position" is a claim until a call returns having watched it do something,
    and this returns carrying the event, the position, and the price path.
    """
    import threading

    cli = _broker_cli(monkeypatch, tmp_path)
    _live_guard_state(cli, ticks_scanned={"EURUSD": 35})
    mt5 = _WatchMT5(
        positions=[_watch_position(777, "EURUSD")],
        ticks={"EURUSD": (1.14190, 1.14210)},
    )
    _serve_watch(monkeypatch, cli, mt5)

    def write_fire() -> None:
        with cli.GUARD_EVENTS_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "event": "fired", "rule_id": "g-1", "symbol": "EURUSD",
                "latency_ms": 141.8, "positions_matched": 1,
            }) + "\n")

    timer = threading.Timer(0.3, write_fire)
    timer.start()
    try:
        payload = json.loads(_capture_emit(
            cli, cli.cmd_watch,
            types.SimpleNamespace(symbol=[], wait_seconds=5.0, poll_seconds=0.2, lines=20),
        ))
    finally:
        timer.cancel()

    assert payload["watched"]["observed_event"] == "fired"
    assert payload["watched"]["timed_out"] is False
    assert payload["watched"]["waited_s"] < 5.0
    # Defaults to the symbols AT RISK -- no --symbol was given.
    assert payload["symbols_watched"] == ["EURUSD"]
    assert payload["position_count"] == 1
    assert payload["positions"][0]["ticket"] == 777
    assert payload["prices"]["EURUSD"]["mid"] == pytest.approx(1.142)
    # Proof the guard is LOOKING, carried into the same payload.
    assert payload["ticks_scanned"] == {"EURUSD": 35}
    assert payload["ok"] is True


def test_watch_reports_the_price_path_when_nothing_happened(monkeypatch, tmp_path):
    """"Nothing happened" plus how far the price travelled is the useful answer.

    A snapshot cannot tell "moved 3 pips and came back" from "sat still"; those
    are the same number in a snapshot and different facts about the market. The
    path is what makes the timeout worth reading rather than merely honest.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    _live_guard_state(cli)
    mt5 = _WatchMT5(
        positions=[_watch_position(777, "EURUSD")],
        ticks={"EURUSD": (1.14190, 1.14210)},
    )
    _serve_watch(monkeypatch, cli, mt5)

    payload = json.loads(_capture_emit(
        cli, cli.cmd_watch,
        types.SimpleNamespace(symbol=[], wait_seconds=0.5, poll_seconds=0.1, lines=5),
    ))

    watched = payload["watched"]
    assert watched["timed_out"] is True
    assert watched["observed"] == [] and watched["observed_event"] is None
    assert "nothing happened" in watched["note"]
    assert "Price moved" in watched["note"]
    path = payload["price_path"]["EURUSD"]
    assert path["samples"] >= 1
    assert path["first_mid"] == pytest.approx(1.142)
    assert path["range_pips"] == 0.0
    # The cap plus the tool's slack for the round trip has to land inside the
    # 120 s ceiling for ONE sandbox command, or the wait would be killed
    # mid-flight and return no JSON at all.
    assert watched["capped_at_s"] == cli.WATCH_MAX_WAIT_SECONDS
    assert cli.WATCH_MAX_WAIT_SECONDS + 30 <= 120.0


def test_watch_notices_the_set_of_open_positions_changing(monkeypatch, tmp_path):
    """The position closing under the caller's feet ends the wait, loudly.

    Nothing in the market can tell the caller that their exposure changed; only
    the position set can. A watch that waited for a price event would sit through
    the exit it exists to report.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    _live_guard_state(cli)
    mt5 = _WatchMT5(
        positions=[_watch_position(777, "EURUSD")],
        ticks={"EURUSD": (1.14190, 1.14210)},
    )
    _serve_watch(monkeypatch, cli, mt5)

    calls = {"n": 0}
    real = mt5.positions_get

    def vanish_after_one():
        calls["n"] += 1
        if calls["n"] > 1:
            mt5._positions = []
        return real()

    mt5.positions_get = vanish_after_one

    payload = json.loads(_capture_emit(
        cli, cli.cmd_watch,
        types.SimpleNamespace(symbol=[], wait_seconds=5.0, poll_seconds=0.05, lines=5),
    ))

    assert payload["watched"]["observed_event"] == "position_closed"
    assert payload["watched"]["observed"][-1]["closed"] == [777]
    assert payload["position_count"] == 0


def test_watch_notices_the_watcher_dying(monkeypatch, tmp_path):
    """A watcher that stops mid-watch is an observation and a failure, not a timeout."""
    cli = _broker_cli(monkeypatch, tmp_path)
    _live_guard_state(cli)
    mt5 = _WatchMT5(
        positions=[_watch_position(777, "EURUSD")],
        ticks={"EURUSD": (1.14190, 1.14210)},
    )
    _serve_watch(monkeypatch, cli, mt5)

    live = {"n": 0}

    def flaky_live(state):  # noqa: ANN001, ANN201
        live["n"] += 1
        return live["n"] <= 1

    monkeypatch.setattr(cli, "_guard_is_live", flaky_live)

    payload = json.loads(_capture_emit(
        cli, cli.cmd_watch,
        types.SimpleNamespace(symbol=[], wait_seconds=5.0, poll_seconds=0.05, lines=5),
    ))

    assert payload["watched"]["observed_event"] == "watcher_stop"
    # The level is watched by nobody now, so this is not a healthy answer.
    assert payload["ok"] is False
    assert payload["alert"] == "watcher_stop"


def test_watch_with_nothing_to_watch_says_so(monkeypatch, tmp_path):
    """No positions and no symbols is an empty watch, not a silent one."""
    cli = _broker_cli(monkeypatch, tmp_path)
    _live_guard_state(cli)
    mt5 = _WatchMT5(positions=[], ticks={})
    _serve_watch(monkeypatch, cli, mt5)

    payload = json.loads(_capture_emit(
        cli, cli.cmd_watch,
        types.SimpleNamespace(symbol=[], wait_seconds=0.0, poll_seconds=1.0, lines=5),
    ))

    assert payload["symbols_watched"] == []
    assert payload["position_count"] == 0
    assert "nothing to watch" in payload["hint"]


def test_watch_reports_a_terminal_that_did_not_answer(monkeypatch, tmp_path):
    """A failed position read must never be reported as a flat account.

    ``positions_get`` returns None for a FAILED request and 0 rows for "nothing
    is open". Collapsing the two would tell a caller their position is gone when
    the terminal simply did not answer -- the worst thing this command could say.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    _live_guard_state(cli)
    mt5 = _WatchMT5(positions=[], ticks={})
    mt5.unavailable = True
    _serve_watch(monkeypatch, cli, mt5)

    payload = json.loads(_capture_emit(
        cli, cli.cmd_watch,
        types.SimpleNamespace(symbol=[], wait_seconds=0.0, poll_seconds=1.0, lines=5),
    ))

    assert payload["ok"] is False
    assert payload["alert"] == "terminal_unavailable"
    assert payload["terminal"]["available"] is False
    assert "NOT known to be the whole picture" in payload["warning"]


def test_watch_reaches_the_cli_and_runs_under_wine():
    """The tool passes symbols and the wait through; the CLI runs it in Wine.

    Wine matters: on the Linux python every sample is a fresh re-exec into Wine
    (seconds each), so a 1 Hz watch would sample the market once per call instead
    of once per second -- and the price PATH could not be built at all.
    """
    watching = build_cli_command(
        "watch",
        {"symbols": "EURUSD, XAUUSD", "wait_seconds": 60, "poll_seconds": 2, "lines": 30},
    )
    assert "mt5_cli.py watch" in watching
    assert "--symbol EURUSD" in watching and "--symbol XAUUSD" in watching
    assert "--wait-seconds 60" in watching and "--poll-seconds 2" in watching
    assert "--lines 30" in watching

    # No symbols and no wait -> a plain snapshot of the positions at risk.
    bare = build_cli_command("watch", {})
    assert bare.endswith("mt5_cli.py watch --lines 20")

    from nanobot.agent.tools.mt5_sandbox import _READ_ONLY_ACTIONS

    assert "watch" in _READ_ONLY_ACTIONS
    assert _TIMEOUTS["watch"] <= 120


def test_watch_survives_a_trading_disabled_deployment():
    """Observing a live trade must not need the trading opt-in.

    The caller most in need of watching is the one who has just been told live
    trading is off; refusing to show them the position would be perverse. The
    gate is driven by _TRADING_ACTIONS, so `watch` being absent from it (and
    present in the read-only set) is the whole property.
    """
    from nanobot.agent.tools.mt5_sandbox import _READ_ONLY_ACTIONS, _TRADING_ACTIONS

    assert "watch" in _READ_ONLY_ACTIONS
    assert "watch" not in _TRADING_ACTIONS
    # And it is offered to the model: an action the schema does not list cannot
    # be called, which would make the whole feature unreachable.
    schema = MT5SandboxTool.__new__(MT5SandboxTool).parameters["properties"]["action"]
    assert "watch" in schema["enum"]


def test_guard_ensure_and_the_unpriceable_override_reach_the_cli():
    ensure = build_cli_command(
        "guard", {"guard_action": "ensure", "max_seconds": 600, "interval_ms": 50}
    )
    assert "mt5_cli.py guard ensure" in ensure
    assert "--max-seconds 600" in ensure and "--interval-ms 50" in ensure

    armed = build_cli_command(
        "guard",
        {"guard_action": "arm", "symbol": "EURUSD", "trigger_price": 1.165,
         "guard_allow_unpriceable": True},
    )
    assert "--allow-unpriceable" in armed
    # Off unless asked for: refusing is the default.
    plain = build_cli_command(
        "guard", {"guard_action": "arm", "symbol": "EURUSD", "trigger_price": 1.165}
    )
    assert "--allow-unpriceable" not in plain


async def test_guard_ensure_is_gated_like_arming(monkeypatch):
    """ensure SPAWNS the watcher, so trading-off must not start one silently."""
    monkeypatch.delenv("MT5_ALLOW_TRADING", raising=False)
    sandbox = _FakeSandbox('{"ok": true}\n[exit_code=0]')
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="guard", guard_action="ensure")

    assert result.is_error and "MT5_ALLOW_TRADING" in str(result)
    assert sandbox.calls == []


async def test_positions_says_when_a_guard_is_not_actually_watching():
    """Open risk and the answer to "is anything watching a price" travel together."""
    payload = {
        "ok": True,
        "count": 1,
        "positions": [{"ticket": 1, "symbol": "EURUSD", "sl": 0.0, "tp": 0.0}],
        "guard": {"live": False, "rules_armed": 2,
                  "alert": "guard_not_running", "exit_reason": "max_seconds"},
    }
    sandbox = _FakeSandbox(json.dumps(payload) + "\n[exit_code=0]")
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    rendered = str(await tool.execute(action="positions"))

    assert '"guard_alert": "guard_not_running"' in rendered
    assert "GUARD NOT WATCHING" in rendered
    assert "ensure" in rendered
    assert "NOT currently protecting anything" in rendered

    # And with a live guard the hint says so instead of implying the rule is idle.
    payload["guard"] = {"live": True, "rules_armed": 2, "alert": None}
    sandbox.response = json.dumps(payload) + "\n[exit_code=0]"
    rendered = str(await tool.execute(action="positions"))
    assert "GUARD NOT WATCHING" not in rendered
    assert "a tick-level guard IS running" in rendered


async def test_positions_reports_a_close_the_guard_could_not_get_out():
    """ "The guard is live" is not the same as "the exit happened".

    A guard that fired and whose close the broker REFUSED has a live watcher and
    a position that is still open. Reading open risk from the positions payload
    must therefore say which of those it is, in the same breath.
    """
    payload = {
        "ok": True,
        "count": 1,
        "positions": [{"ticket": 1, "symbol": "EURUSD", "sl": 0.0, "tp": 0.0}],
        "guard": {
            "live": True, "rules_armed": 1, "alert": "close_retrying",
            "retrying": [{"rule_id": "g1", "symbol": "EURUSD", "attempts": 3,
                          "trying_for_s": 4.5, "retry_in_s": 1.0,
                          "deadline_in_s": 25.5}],
            "gave_up": [],
        },
    }
    sandbox = _FakeSandbox(json.dumps(payload) + "\n[exit_code=0]")
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    rendered = str(await tool.execute(action="positions"))

    assert '"guard_alert": "close_retrying"' in rendered
    assert '"guard_retrying_close": [{"rule_id": "g1"' in rendered
    assert "GUARD RETRYING A REFUSED CLOSE" in rendered
    assert "NOT out yet" in rendered

    # Given up: the caller has to act, and the broker's own refusals travel with
    # the positions that are still open.
    payload["guard"] = {
        "live": True, "rules_armed": 1, "alert": "close_gave_up",
        "retrying": [],
        "gave_up": [{"rule_id": "g1", "symbol": "EURUSD", "attempts": 20,
                     "retcodes": [10018], "gave_up_s_ago": 5.0,
                     "next_window_in_s": 55.0}],
    }
    sandbox.response = json.dumps(payload) + "\n[exit_code=0]"
    rendered = str(await tool.execute(action="positions"))

    assert '"guard_gave_up_close"' in rendered
    assert "GUARD COULD NOT CLOSE" in rendered
    assert "STILL OPEN" in rendered
    assert "10018" in rendered


def test_spawn_reports_the_log_marks_its_callers_tail_from(monkeypatch, tmp_path):
    """A REAL spawn must carry the offsets the arm/ensure tails read.

    This is the test that was missing when it mattered: every arm test stubbed
    ``_guard_spawn``, so a ``NameError`` inside the real one was invisible to the
    suite and shipped -- on a live MetaQuotes box ``guard arm`` answered
    ``ok=false, error="NameError: name 'log_mark' is not defined"`` and a caller
    reading that cannot tell a broken guard from a refused one.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    cli.GUARD_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "_guard_python", lambda: ("wine", tmp_path / "py.exe"))
    cli.GUARD_LOG_FILE.write_text("old run 1\nold run 2\n", encoding="utf-8")

    class _Proc:
        returncode = 0
        stdout = "4242\n"
        stderr = ""

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _Proc())

    spawn = cli._guard_spawn(100, 60, 30)

    for key in ("state", "launcher_pid", "waited_s", "log_mark", "err_mark"):
        assert key in spawn, f"_guard_spawn must report {key!r}"
    # Counted BEFORE the spawn, so a tail skips the previous run's lines.
    assert spawn["log_mark"] == 2
    assert spawn["err_mark"] == 0
    assert spawn["launcher_pid"] == "4242"


def test_a_guard_is_not_killed_by_an_hour_it_was_never_given(monkeypatch, tmp_path):
    """No budget means NO limit; only a positive number bounds the watch.

    The guard used to be armed with ``--max-seconds 3600`` whether or not anyone
    asked for it, so an exit level that took longer than an hour to be touched
    was watched by nobody and had to be noticed by a later status call. "Close
    when it hits X" is a standing instruction, not a one-hour one.
    """
    cli = _broker_cli(monkeypatch, tmp_path)

    assert cli._guard_max_seconds(None) == 0
    assert cli._guard_max_seconds("") == 0
    assert cli._guard_max_seconds(0) == 0
    assert cli._guard_max_seconds(-5) == 0
    assert cli._guard_max_seconds("nonsense") == 0
    assert cli._guard_max_seconds(600) == 600
    assert cli._guard_max_seconds("90") == 90
    # A re-arm that does not restate a budget inherits the one it was armed with.
    assert cli._guard_max_seconds(None, default=cli._guard_max_seconds(1800)) == 1800
    assert cli._guard_max_seconds(None, default=cli._guard_max_seconds(0)) == 0

    # The watcher must actually honour it: a zero budget is not "expire at once".
    source = cli._GUARD_WATCH_SOURCE
    assert "int(args.max_seconds) > 0 and now - started > int(args.max_seconds)" in source
    assert '"max_seconds": int(args.max_seconds)' in source


def test_the_tools_guard_omits_the_budget_so_the_cli_default_applies():
    """An unset budget must reach the CLI as absent, not as 3600.

    ``int(kwargs.get("max_seconds") or 3600)`` meant the tool could never ask for
    an unlimited guard: whatever the caller sent, an hour was forced on top of it.
    """
    cmd = build_cli_command("guard", {
        "guard_action": "arm", "symbol": "EURUSD", "trigger_price": 1.1,
        "trigger_op": "<=",
    })
    assert "--max-seconds" not in cmd

    cmd = build_cli_command("guard", {
        "guard_action": "arm", "symbol": "EURUSD", "trigger_price": 1.1,
        "trigger_op": "<=", "max_seconds": 600,
    })
    assert "--max-seconds 600" in cmd

    cmd = build_cli_command("guard", {"guard_action": "ensure"})
    assert "--max-seconds" not in cmd

    cmd = build_cli_command("guard", {"guard_action": "ensure", "max_seconds": 120})
    assert "--max-seconds 120" in cmd


def test_the_reported_latency_survives_a_narrow_events_window(monkeypatch, tmp_path):
    """``last_latency_ms`` is the last FIRE's, not the last line's.

    MEASURED 2026-09-23 (live, MetaQuotes): a guard fired and closed its position
    in 145.1 ms; ``guard events --lines 1`` then answered ``last_latency_ms:
    null``, because the one newest line was the ``watcher_stop`` that followed the
    fire. The latency is what the caller asks for when a close was late, so it has
    to survive the window they happened to ask for.
    """
    import argparse

    cli = _broker_cli(monkeypatch, tmp_path)
    cli.GUARD_DIR.mkdir(parents=True, exist_ok=True)
    rows = [
        {"event": "fired", "rule_id": "g1", "latency_ms": 145.1,
         "positions_matched": 1, "close_ts": 1.0},
        {"event": "watcher_stop", "exit_reason": "rules_satisfied", "ts": 1.1},
    ]
    cli.GUARD_EVENTS_FILE.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "emit",
                        lambda payload, **_k: captured.update(payload) or 0)

    cli.cmd_guard(argparse.Namespace(guard_action="events", lines=1))

    assert captured["count"] == 1
    assert captured["events"][0]["event"] == "watcher_stop"
    assert captured["last_latency_ms"] == 145.1


# --------------------------------------------------------------------------- #
# split trading, and polling that carries across calls
# --------------------------------------------------------------------------- #
def test_split_sends_one_virtual_command_for_many_tickets():
    """N tickets must be ONE bridge invocation.

    `--splits 10` as ten separate CLI calls would be ten Wine re-execs AND ten
    windows for the price to move between the first ticket and the last -- which
    is the opposite of "the same price". The CLI reads the tick once and prices
    every ticket off it, and that only holds if the command is built once.
    """
    cmd = build_cli_command(
        "split",
        {"symbol": "XAUUSD", "side": "sell", "volume": 1.0, "splits": 10,
         "sl": 4294.18, "tp": 4278.18, "group": "xau-leg2"},
    )
    assert "split" in cmd
    assert "--splits 10" in cmd
    assert "--volume 1.0" in cmd
    assert "--group xau-leg2" in cmd
    assert "--sl 4294.18" in cmd and "--tp 4278.18" in cmd


def test_split_defaults_to_ten_and_says_so_in_the_schema():
    cmd = build_cli_command(
        "split", {"symbol": "XAUUSD", "side": "buy", "volume": 1.0}
    )
    assert "--splits 10" in cmd
    desc = MT5SandboxTool().parameters["properties"]["splits"]["description"]
    assert "2..50" in desc
    # The property the method depends on has to be in the text the model reads.
    assert "SAME TOTAL RISK" in desc
    assert "grid" in desc


@pytest.mark.asyncio
async def test_split_requires_a_total_volume_and_a_real_split(monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))
    missing = await tool.execute(action="split", symbol="XAUUSD", side="sell")
    assert missing.is_error
    assert "TOTAL lots" in str(missing)
    # splits=1 is not a split, and allowing it would let "split" mean "order".
    degenerate = await tool.execute(
        action="split", symbol="XAUUSD", side="sell", volume=1.0, splits=1
    )
    assert degenerate.is_error
    assert "order" in str(degenerate)


def test_close_by_group_targets_the_group_and_never_a_ticket_zero():
    """`--ticket 0` would be read as a real ticket and close the wrong thing."""
    cmd = build_cli_command("close", {"group": "xau-leg2", "count": 3})
    assert "--group xau-leg2" in cmd
    assert "--count 3" in cmd
    assert "--ticket" not in cmd
    # A plain ticket close still builds exactly as it did.
    plain = build_cli_command("close", {"ticket": 123})
    assert "--ticket 123" in plain and "--group" not in plain


@pytest.mark.asyncio
async def test_close_accepts_a_group_instead_of_a_ticket(monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))
    neither = await tool.execute(action="close")
    assert neither.is_error
    assert "group" in str(neither)
    # execute() returns the raw sandbox string on success and a ToolResult only on
    # error, so success is asserted the way the rest of this file does it.
    by_group = await tool.execute(action="close", group="xau-leg2", count=3)
    assert not getattr(by_group, "is_error", False)


def test_watch_session_is_passed_through_to_the_cli():
    """Continuity is the whole point, so the name must reach the command."""
    cmd = build_cli_command(
        "watch", {"watch_session": "xau-leg-2", "wait_seconds": 90, "symbols": "XAUUSD"}
    )
    assert "--session xau-leg-2" in cmd
    assert "--wait-seconds 90.0" in cmd
    # And omitting it must not invent one: a session nobody asked for would
    # silently merge two unrelated trades into one timeline.
    assert "--session" not in build_cli_command("watch", {"wait_seconds": 5})


def test_the_description_tells_the_model_to_loop_the_watch_and_says_what_split_is():
    desc = MT5SandboxTool().description
    assert "watch_session" in desc
    assert "POLLING A LIVE TRADE" in desc
    # The instruction that fixes "it says it is watching but nobody is watching".
    assert "watch again" in desc
    assert "timed_out" in desc
    assert "SPLIT TRADING" in desc
    assert "grid" in desc


# --------------------------------------------------------------------------- #
# ENTERING AT A PRICE, AND SIZING BY MONEY AT RISK
# --------------------------------------------------------------------------- #
# The two instructions the tool could not serve: "buy the dip at X"/"buy the
# breakout above X" (not a market order), and "risk $100 on this" (not a lot
# size). Both are now arguments, and both must reach the CLI verbatim.


def test_order_can_rest_at_a_price_instead_of_filling_now():
    cmd = build_cli_command(
        "order", {"symbol": "XAUUSD", "side": "buy", "volume": 0.1,
                  "entry_type": "limit", "price": 4270.0, "sl": 4268.0}
    )
    assert "--entry-type limit" in cmd
    assert "--price 4270.0" in cmd
    # A market entry is the default and must stay byte-identical to before, so a
    # plain order does not start carrying a flag the CLI has to ignore.
    plain = build_cli_command("order", {"symbol": "XAUUSD", "side": "buy", "volume": 0.1})
    assert "--entry-type" not in plain
    assert "--price" not in plain


def test_order_can_be_sized_by_money_at_risk_and_omits_the_zero_volume():
    """`--volume 0.0` next to `--risk-money` is a second, zero answer to one
    question, and the CLI refuses two sizes for one order."""
    cmd = build_cli_command(
        "order", {"symbol": "XAUUSD", "side": "buy", "sl": 4283.18, "risk_money": 100}
    )
    assert "--volume" not in cmd
    assert "--risk-money 100.0" in cmd
    assert "--sl 4283.18" in cmd
    assert "--risk-pct 1.0" in build_cli_command(
        "order", {"symbol": "XAUUSD", "side": "buy", "sl": 4283.18, "risk_pct": 1.0}
    )


def test_cancel_is_a_trading_action_with_a_ticket_or_a_sweep():
    from nanobot.agent.tools.mt5_sandbox import _TRADING_ACTIONS

    # Removing a resting order changes what will happen to real money, so it sits
    # behind the same opt-in as every other action that reaches the broker.
    assert "cancel" in _TRADING_ACTIONS
    assert "cancel" in _TIMEOUTS
    assert "--ticket 12" in build_cli_command("cancel", {"ticket": 12})
    assert "--all" in build_cli_command("cancel", {"cancel_all": True})


@pytest.mark.asyncio
async def test_order_accepts_money_at_risk_instead_of_lots(monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))

    # No size at all is still an error...
    assert (await tool.execute(action="order", symbol="XAUUSD", side="buy")).is_error
    # ...but money-at-risk IS a size, provided it comes with the stop that defines it.
    no_stop = await tool.execute(
        action="order", symbol="XAUUSD", side="buy", risk_money=100
    )
    assert no_stop.is_error
    assert "sl" in str(no_stop)
    with_stop = await tool.execute(
        action="order", symbol="XAUUSD", side="buy", risk_money=100, sl=4283.18
    )
    assert not getattr(with_stop, "is_error", False)
    # And two sizes for one order is refused rather than resolved silently.
    both = await tool.execute(
        action="order", symbol="XAUUSD", side="buy", volume=0.1,
        risk_money=100, sl=4283.18,
    )
    assert both.is_error
    assert "not both" in str(both)


@pytest.mark.asyncio
async def test_a_pending_entry_needs_the_price_it_rests_at(monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))

    missing = await tool.execute(
        action="order", symbol="XAUUSD", side="buy", volume=0.1, entry_type="limit"
    )
    assert missing.is_error
    assert "price" in str(missing)

    # And a market order must not pretend to honour a price it cannot.
    contradictory = await tool.execute(
        action="order", symbol="XAUUSD", side="buy", volume=0.1,
        entry_type="market", price=4270.0,
    )
    assert contradictory.is_error
    assert "market" in str(contradictory)

    nonsense = await tool.execute(
        action="order", symbol="XAUUSD", side="buy", volume=0.1, entry_type="wish"
    )
    assert nonsense.is_error


@pytest.mark.asyncio
async def test_cancel_needs_a_target(monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))

    assert (await tool.execute(action="cancel")).is_error
    swept = await tool.execute(action="cancel", cancel_all=True)
    assert not getattr(swept, "is_error", False)


def test_the_description_explains_entering_at_a_price_and_sizing_by_risk():
    desc = MT5SandboxTool().description
    assert "ENTERING AT A PRICE" in desc
    assert "entry_type" in desc
    # The confusion worth naming out loud: a resting order is not an open trade.
    assert "resting order is" in desc
    assert "SIZING BY MONEY AT RISK" in desc
    assert "risk_money" in desc


# --------------------------------------------------------------------------- #
# NOTHING OPENS WITHOUT A SERVER-SIDE EXIT
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_an_order_without_a_stop_is_refused_at_the_tool(monkeypatch):
    """The CLI is the enforcement point; this is the same refusal without the
    cost of a sandbox round trip to learn it."""
    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))

    naked = await tool.execute(action="order", symbol="XAUUSD", side="buy", volume=0.1)
    assert naked.is_error
    assert "sl" in str(naked)
    assert "allow_no_stop" in str(naked)

    stopped = await tool.execute(
        action="order", symbol="XAUUSD", side="buy", volume=0.1, sl=4283.18
    )
    assert not getattr(stopped, "is_error", False)

    # And the opt-out is a real opt-out, passed through to the CLI.
    deliberate = await tool.execute(
        action="order", symbol="XAUUSD", side="buy", volume=0.1, allow_no_stop=True
    )
    assert not getattr(deliberate, "is_error", False)


@pytest.mark.asyncio
async def test_a_split_without_a_stop_is_refused_at_the_tool(monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))
    naked = await tool.execute(
        action="split", symbol="XAUUSD", side="buy", volume=1.0, splits=10
    )
    assert naked.is_error
    assert "sl" in str(naked)


def test_the_opt_out_reaches_the_command_line():
    cmd = build_cli_command(
        "order", {"symbol": "XAUUSD", "side": "buy", "volume": 0.1, "allow_no_stop": True}
    )
    assert "--allow-no-stop" in cmd
    # ...and is absent unless asked for, so the default stays protective.
    assert "--allow-no-stop" not in build_cli_command(
        "order", {"symbol": "XAUUSD", "side": "buy", "volume": 0.1, "sl": 1.05}
    )
    assert "--allow-no-stop" in build_cli_command(
        "split", {"symbol": "XAUUSD", "side": "buy", "volume": 1.0, "allow_no_stop": True}
    )


def test_the_description_says_a_position_gets_a_stop_by_default():
    desc = MT5SandboxTool().description
    assert "EVERY ORDER CARRIES A STOP" in desc
    assert "allow_no_stop" in desc
    # The reason, not just the rule: the broker holds nothing without one.
    assert "BROKER holds no" in desc


# --------------------------------------------------------------------------- #
# THE ACCOUNT CIRCUIT BREAKER
# --------------------------------------------------------------------------- #
# A stop caps what ONE trade can lose. Nothing capped what the ACCOUNT could
# lose, and the account is the thing that runs out. The limits are enforced by
# the CLI at the point an order is sent, so the tool's job here is the smaller
# one: make them reachable, refuse the nonsense before a round trip, and say
# what they are.


def test_the_risk_report_is_readable_without_live_trading():
    """Refusing to report the account's exposure because trading is switched off
    hides it from exactly the caller who needs to see it."""
    cmd = build_cli_command("risk", {})
    assert cmd.endswith("mt5_cli.py risk")
    assert "risk" in _ALL_ACTIONS


@pytest.mark.asyncio
async def test_risk_reads_the_account_book_without_the_trading_opt_in(monkeypatch):
    monkeypatch.delenv("MT5_ALLOW_TRADING", raising=False)
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))
    result = await tool.execute(action="risk")
    assert not getattr(result, "is_error", False)


def test_limits_set_passes_only_what_was_asked_for_so_it_merges():
    """A `set` that emitted every field would clear the ones it did not mention."""
    cmd = build_cli_command(
        "limits", {"limits_action": "set", "max_total_risk_money": 30, "max_positions": 2}
    )
    assert cmd.endswith("limits set --max-positions 2 --max-total-risk-money 30.0")
    assert "--max-daily-loss-money" not in cmd
    # A count is an int on the CLI: "2.0" would be rejected as a bad argument.
    assert "--max-positions 2 " in cmd + " "


def test_limits_defaults_to_show_and_clear_carries_no_values():
    assert build_cli_command("limits", {}).endswith("limits show")
    assert build_cli_command("limits", {"limits_action": "clear"}).endswith("limits clear")


@pytest.mark.asyncio
async def test_limits_show_is_available_without_the_trading_opt_in(monkeypatch):
    """Asking what protection is in force is the one call most worth having when
    trading is switched off."""
    monkeypatch.delenv("MT5_ALLOW_TRADING", raising=False)
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))
    result = await tool.execute(action="limits", limits_action="show")
    assert not getattr(result, "is_error", False)


@pytest.mark.asyncio
async def test_setting_a_limit_needs_the_trading_opt_in(monkeypatch):
    """It changes what the system will do with money, so it is gated like an order."""
    monkeypatch.delenv("MT5_ALLOW_TRADING", raising=False)
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))
    blocked = await tool.execute(
        action="limits", limits_action="set", max_total_risk_money=100
    )
    assert blocked.is_error
    # ...but asking what is in force is always allowed.
    assert not getattr(await tool.execute(action="limits", limits_action="show"), "is_error", False)

    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    allowed = await tool.execute(
        action="limits", limits_action="set", max_total_risk_money=100
    )
    assert not getattr(allowed, "is_error", False)


@pytest.mark.asyncio
async def test_limits_set_with_nothing_to_set_is_refused_here(monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))
    empty = await tool.execute(action="limits", limits_action="set")
    assert empty.is_error
    assert "at least one" in str(empty)

    # A limit of zero is not a limit.
    zero = await tool.execute(action="limits", limits_action="set", max_positions=0)
    assert zero.is_error
    assert "positive" in str(zero)


@pytest.mark.asyncio
async def test_an_unknown_limits_action_is_refused(monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_TRADING", "1")
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": _FakeSandbox()}))
    bad = await tool.execute(action="limits", limits_action="wipe")
    assert bad.is_error
    assert "show, set or clear" in str(bad)


def test_the_description_says_what_the_circuit_breaker_caps_and_where_to_read_it():
    desc = MT5SandboxTool().description
    assert "ACCOUNT CIRCUIT BREAKER" in desc
    assert "max_total_risk_money" in desc
    # The failure it exists to stop, named: many small positions nobody adds up.
    assert "10% on the table" in desc
    # And the one call to read before sizing.
    assert "action='risk'" in desc


def test_the_limits_limits_are_documented_as_money_and_counts():
    props = MT5SandboxTool().parameters["properties"]
    assert props["limits_action"]["enum"] == ["show", "set", "clear"]
    assert props["max_positions"]["type"] == "integer"
    assert props["max_total_risk_money"]["type"] == "number"
    # A stopless order cannot be counted, and says so.
    assert "no stop" in props["max_total_risk_money"]["description"]


# --------------------------------------------------------------------------- #
# A stop that moves itself: guard action ``move_stop``
#
# ``stop_room`` and ``move_stops`` live INSIDE the embedded watcher source -- the
# file the sandbox actually runs -- so they are reached by running that source,
# never by copying the arithmetic into a test that would then drift from it.
# --------------------------------------------------------------------------- #
class _StopMT5:
    """A terminal that can move a stop and nothing else."""

    POSITION_TYPE_BUY = 0
    TRADE_ACTION_SLTP = 6

    def __init__(
        self, bid: float, ask: float | None = None, *,
        stops_level_points: int = 0, point: float = 0.01, retcode: int = 10009,
        orders: tuple[float, ...] | None = None,
    ) -> None:
        self.tick = types.SimpleNamespace(
            bid=float(bid), ask=float(bid if ask is None else ask)
        )
        self.info = types.SimpleNamespace(
            point=float(point), trade_stops_level=int(stops_level_points)
        )
        self.retcode = int(retcode)
        self.sl_sends: list[dict[str, Any]] = []
        #: The stops the position's ORDER HISTORY reports, earliest first. Only
        #: installed when the test wants one, so every other test still exercises
        #: the terminal that cannot answer -- which is the honest fallback.
        if orders is not None:
            self.history_orders_get = lambda **_kw: [
                types.SimpleNamespace(sl=float(s), time_msc=1_700_000_000 + i)
                for i, s in enumerate(orders)
            ]

    def move_to(self, bid: float, ask: float | None = None) -> "_StopMT5":
        self.tick = types.SimpleNamespace(
            bid=float(bid), ask=float(bid if ask is None else ask)
        )
        return self

    def symbol_info(self, _symbol: str) -> Any:
        return self.info

    def symbol_info_tick(self, _symbol: str) -> Any:
        return self.tick

    def order_send(self, request: dict[str, Any]) -> Any:
        self.sl_sends.append(dict(request))
        return types.SimpleNamespace(retcode=self.retcode, comment="moved", deal=0)

    def last_error(self) -> tuple[int, str]:
        return (0, "no error")


def _position(
    *, entry: float, sl: float, tp: float = 0.0, ticket: int = 9001,
    symbol: str = "XAUUSD", side: int = 0,
) -> Any:
    return types.SimpleNamespace(
        ticket=ticket, symbol=symbol, type=side, volume=0.1,
        price_open=float(entry), sl=float(sl), tp=float(tp),
    )


def _watcher_defs(cli: Any, monkeypatch: Any, mt5: Any) -> dict[str, Any]:
    """The watcher's own namespace, so one stop rule can be evaluated alone."""
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt5)
    namespace: dict[str, Any] = {"__name__": "guard_watch_under_test"}
    exec(compile(cli._GUARD_WATCH_SOURCE, "<guard_watch>", "exec"), namespace)
    return namespace


def _breakeven_rule(**overrides: Any) -> dict[str, Any]:
    rule = {
        "id": "g-be", "symbol": "XAUUSD", "action": "move_stop",
        "mode": "breakeven", "when_r": 1.0,
    }
    rule.update(overrides)
    return rule


def _trail_rule(**overrides: Any) -> dict[str, Any]:
    rule = {
        "id": "g-tr", "symbol": "XAUUSD", "action": "move_stop",
        "mode": "trail", "distance": 2.0,
    }
    rule.update(overrides)
    return rule


def test_the_brokers_minimum_stop_distance_is_its_points_in_price(monkeypatch, tmp_path):
    """Points are not pips, and mixing them up is how a valid stop gets refused.

    On Gold the point is 0.01 and the pip is 0.10, so a ``trade_stops_level`` of
    18 is 0.18 of price -- 1.8 pips, not 18.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _StopMT5(4300.0)
    defs = _watcher_defs(cli, monkeypatch, mt5)

    assert defs["stop_room"](types.SimpleNamespace(trade_stops_level=18, point=0.01)) == 0.18
    # A terminal that does not report the field cannot forbid anything.
    assert defs["stop_room"](types.SimpleNamespace()) == 0.0


def test_breakeven_waits_for_its_multiple_of_r_and_then_lands_on_the_entry(
    monkeypatch, tmp_path
):
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _StopMT5(4310.0, 4310.2)
    defs = _watcher_defs(cli, monkeypatch, mt5)
    rule = _breakeven_rule()
    position = _position(entry=4300.0, sl=4280.0, tp=4360.0)

    # 4310 is 10 of the 20 the stop risks, so 1R is 4320 and the stop is untouched.
    early = defs["move_stops"](rule, [position], 30, 0)
    assert early[0]["ok"] is True and "not yet 1R" in early[0]["skipped"]
    assert mt5.sl_sends == [], "the stop must not be touched before the trigger"

    mt5.move_to(4321.0, 4321.2)
    reached = defs["move_stops"](rule, [position], 30, 0)
    assert reached[0]["ok"] is True
    assert reached[0]["from_sl"] == 4280.0 and reached[0]["to_sl"] == 4300.0
    assert len(mt5.sl_sends) == 1
    sent = mt5.sl_sends[0]
    assert sent["action"] == 6 and sent["sl"] == 4300.0
    assert sent["position"] == 9001
    # The target belongs to whoever set it: this rule owns the stop and nothing else.
    assert sent["tp"] == 4360.0


def test_the_r_a_breakeven_uses_is_the_one_it_measured_at_first_sight(
    monkeypatch, tmp_path
):
    """R must not be re-measured off a stop that has already been moved.

    Re-measured, R collapses as the stop advances, the trigger drifts down with
    it, and the rule fires again for no reason -- a stop that walks itself into
    the price.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _StopMT5(4315.0, 4315.2)
    defs = _watcher_defs(cli, monkeypatch, mt5)
    rule = _breakeven_rule()
    position = _position(entry=4300.0, sl=4280.0)

    defs["move_stops"](rule, [position], 30, 0)
    assert rule["move_ref"]["9001"]["risk"] == 20.0

    # Something else moves the stop -- the model, or a different rule.
    position.sl = 4285.0
    again = defs["move_stops"](rule, [position], 30, 0)

    assert "1R" in again[0]["skipped"], again[0]
    # ...and the answer names what the R came from, because a trigger measured off
    # a moved stop is a trigger for a reason that does not exist.
    assert again[0]["risk_source"] == "the stop as it is now"
    assert again[0]["sl"] == 4285.0
    assert rule["move_ref"]["9001"]["risk"] == 20.0
    assert mt5.sl_sends == []


def test_a_breakeven_rule_reports_a_naked_position_instead_of_inventing_a_stop(
    monkeypatch, tmp_path
):
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _StopMT5(4321.0, 4321.2)
    defs = _watcher_defs(cli, monkeypatch, mt5)

    rows = defs["move_stops"](_breakeven_rule(), [_position(entry=4300.0, sl=0.0)], 30, 0)

    assert rows[0]["ok"] is False
    assert "NO stop to move" in rows[0]["skipped"]
    assert mt5.sl_sends == [], "the level to set would be a guess, so none is set"


def test_a_trail_follows_the_best_price_and_never_loosens_the_stop(
    monkeypatch, tmp_path
):
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _StopMT5(4300.0, 4300.2)
    defs = _watcher_defs(cli, monkeypatch, mt5)
    rule = _trail_rule()
    position = _position(entry=4280.0, sl=4290.0)

    first = defs["move_stops"](rule, [position], 30, 0)
    assert first[0]["to_sl"] == 4298.0 and len(mt5.sl_sends) == 1

    # The market gives back 20 of price. The PEAK is what the stop hangs off, so
    # the answer is "already there" -- never a looser stop.
    position.sl = 4298.0
    mt5.move_to(4280.0, 4280.2)
    back = defs["move_stops"](rule, [position], 30, 0)

    assert back[0]["ok"] is True and "already at least that far" in back[0]["skipped"]
    assert back[0]["target"] == 4298.0
    assert len(mt5.sl_sends) == 1, "a trailing stop only ever moves one way"


def test_a_trail_will_not_put_the_stop_inside_the_brokers_forbidden_zone(
    monkeypatch, tmp_path
):
    """Sent anyway, this is the stop that comes back 10016 Invalid stops."""
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _StopMT5(4300.0, 4300.2, stops_level_points=18)
    defs = _watcher_defs(cli, monkeypatch, mt5)

    rows = defs["move_stops"](
        _trail_rule(distance=0.05), [_position(entry=4280.0, sl=4280.0)], 30, 0
    )

    assert rows[0]["ok"] is False
    assert "10016" in rows[0]["skipped"]
    assert mt5.sl_sends == [], "it would only come back refused"


def test_a_trail_can_be_held_back_until_a_price(monkeypatch, tmp_path):
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _StopMT5(4300.0, 4300.2)
    defs = _watcher_defs(cli, monkeypatch, mt5)

    rows = defs["move_stops"](
        _trail_rule(activate_at=4310.0), [_position(entry=4280.0, sl=4280.0)], 30, 0
    )

    assert rows[0]["skipped"] == "not active until 4310.0"
    assert mt5.sl_sends == []


def test_a_stop_move_the_broker_refuses_is_reported_with_its_retcode(
    monkeypatch, tmp_path
):
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _StopMT5(4300.0, 4300.2, retcode=10016)
    defs = _watcher_defs(cli, monkeypatch, mt5)

    rows = defs["move_stops"](_trail_rule(), [_position(entry=4280.0, sl=4290.0)], 30, 0)

    assert rows[0]["ok"] is False and rows[0]["retcode"] == 10016
    assert len(mt5.sl_sends) == 1


def test_a_moving_stop_rule_arms_without_a_price_because_it_has_no_level(
    monkeypatch, tmp_path
):
    cli = _broker_cli(monkeypatch, tmp_path)

    rule = cli._validate_rule(
        {"symbol": "XAUUSD", "action": "move_stop", "mode": "breakeven", "ticket": 9001},
        0, price_hint=4300.0,
    )

    assert rule["action"] == "move_stop" and rule["mode"] == "breakeven"
    assert rule["when_r"] == 1.0
    assert rule["scope"] == {"ticket": 9001}
    # A POLICY, not a one-shot: a moving stop that stopped moving is no protection.
    assert rule["once"] is False
    # Requiring a level here would force every caller to invent one for a field
    # the rule never reads.
    assert "price" not in rule and "op" not in rule


def test_a_trailing_rule_carries_its_distance_and_can_wait_for_a_price(
    monkeypatch, tmp_path
):
    cli = _broker_cli(monkeypatch, tmp_path)

    rule = cli._validate_rule(
        {"symbol": "XAUUSD", "action": "move_stop", "mode": "trail",
         "distance": 2.0, "activate_at": 4310.0}, 0, 4300.0,
    )

    assert rule["mode"] == "trail" and rule["distance"] == 2.0
    assert rule["activate_at"] == 4310.0
    assert "when_r" not in rule


def test_a_moving_stop_rule_refuses_a_mode_it_cannot_perform(monkeypatch, tmp_path):
    cli = _broker_cli(monkeypatch, tmp_path)

    with pytest.raises(ValueError) as excinfo:
        cli._validate_rule({"symbol": "XAUUSD", "action": "move_stop", "mode": "nope"}, 0)

    assert "breakeven" in str(excinfo.value) and "trail" in str(excinfo.value)


def test_a_trail_without_a_distance_is_refused_rather_than_defaulted(
    monkeypatch, tmp_path
):
    cli = _broker_cli(monkeypatch, tmp_path)

    with pytest.raises(ValueError) as excinfo:
        cli._validate_rule({"symbol": "XAUUSD", "action": "move_stop", "mode": "trail"}, 0)
    assert "distance" in str(excinfo.value)

    with pytest.raises(ValueError) as zero:
        cli._validate_rule(
            {"symbol": "XAUUSD", "action": "move_stop", "mode": "trail", "distance": 0}, 0
        )
    assert "positive" in str(zero.value)


def test_a_breakeven_multiple_of_r_of_zero_is_refused(monkeypatch, tmp_path):
    cli = _broker_cli(monkeypatch, tmp_path)

    with pytest.raises(ValueError) as excinfo:
        cli._validate_rule(
            {"symbol": "XAUUSD", "action": "move_stop", "mode": "breakeven", "when_r": -1}, 0
        )

    assert "positive" in str(excinfo.value)


def test_a_guard_action_nobody_implements_is_refused_by_name(monkeypatch, tmp_path):
    cli = _broker_cli(monkeypatch, tmp_path)

    with pytest.raises(ValueError) as excinfo:
        cli._validate_rule(
            {"symbol": "XAUUSD", "price": 4300.0, "action": "wibble"}, 0, 4300.0
        )

    assert "unknown action" in str(excinfo.value)


def test_a_moving_stop_rule_keeps_the_reference_it_measured_on_the_moving_pass(
    monkeypatch, tmp_path
):
    """The write-back must not be hung off the QUIET passes.

    A pass that first measures R is usually also the pass that sends a move, so
    an ``elif`` there leaves the rule file without its reference in the common
    case -- and a restart then re-measures R off the stop it has just moved.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock()
    mt5 = _FakeMT5(symbol="XAUUSD", bid=4300.0, ask=4300.2, tickets=(9001,))
    mt5.positions[0].price_open = 4280.0
    mt5.positions[0].sl = 4290.0

    rule = cli._validate_rule(
        {"symbol": "XAUUSD", "action": "move_stop", "mode": "trail",
         "ticket": 9001, "distance": 2.0, "id": "g-trail"}, 0, price_hint=4300.0,
    )
    # ONE pass only: a 2 s sleep takes the run straight past its 1 s limit, so
    # whatever is on the rule afterwards was written by the pass that moved it.
    _run, events, rules_after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock, [rule],
        interval_ms=2000, max_seconds=1,
    )

    moved = [e for e in events if e["event"] == "stop_moved"]
    assert len(moved) == 1, events
    assert moved[0]["mode"] == "trail"
    assert moved[0]["moved"][0]["from_sl"] == 4290.0
    assert moved[0]["moved"][0]["to_sl"] == 4298.0
    # Moving a stop is not a deal: the position stays open.
    assert [s for s in mt5.sends if s["action"] == mt5.TRADE_ACTION_DEAL] == []
    # ...and the reference survives into the FILE, not only in memory.
    assert rules_after[0]["move_ref"]["9001"]["risk"] == 10.0
    assert rules_after[0]["move_best"]["9001"] == 4300.0
    # The state says what the rule DID, in the place a caller already looks for
    # the last near miss.
    published = _run["state"]["stop_move"]["g-trail"]
    assert published["outcome"] == "moved" and published["mode"] == "trail"
    assert published["detail"] == ["#9001 4290.0 -> 4298.0"]
    assert events[-1]["event"] == "watcher_stop"


def test_breakeven_armed_in_the_watcher_moves_the_stop_once_the_price_gets_there(
    monkeypatch, tmp_path
):
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock()
    # 20 short of 1R at arm time, then the price steps past it: the trigger is
    # the price ARRIVING, not the rule being armed.
    mt5 = _FakeMT5(
        symbol="XAUUSD", bid=4310.0, ask=4310.2, tickets=(9001,),
        tick_after_polls=(3, 4321.0, 4321.2),
    )
    mt5.positions[0].price_open = 4300.0
    mt5.positions[0].sl = 4280.0

    rule = cli._validate_rule(
        {"symbol": "XAUUSD", "action": "move_stop", "mode": "breakeven",
         "ticket": 9001, "when_r": 1.0, "id": "g-be"}, 0, price_hint=4310.0,
    )
    _run, events, rules_after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock, [rule],
        interval_ms=100, max_seconds=5,
    )

    moved = [e for e in events if e["event"] == "stop_moved"]
    assert len(moved) == 1, events
    assert moved[0]["mode"] == "breakeven"
    assert moved[0]["moved"][0]["to_sl"] == 4300.0
    assert mt5.positions[0].sl == 4300.0
    assert rules_after[0]["move_ref"]["9001"]["risk"] == 20.0
    assert [s for s in mt5.sends if s["action"] == mt5.TRADE_ACTION_DEAL] == []
    assert events[-1]["exit_reason"] == "max_seconds"


def test_an_armed_moving_stop_rule_does_not_stop_the_watcher_from_reading_a_level(
    monkeypatch, tmp_path
):
    """A policy rule and a level rule must coexist in one pass.

    The level is read past the move_stop branch, so a rule with no 'price' used
    to take the whole watcher down with it -- and with it every OTHER rule on the
    account.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock()
    mt5 = _FakeMT5(symbol="XAUUSD", bid=4300.0, ask=4300.2, tickets=(9001,))
    mt5.positions[0].price_open = 4280.0
    mt5.positions[0].sl = 4290.0

    policy = cli._validate_rule(
        {"symbol": "XAUUSD", "action": "move_stop", "mode": "trail",
         "ticket": 9001, "distance": 2.0, "id": "g-trail"}, 0, 4300.0,
    )
    level = _rule(id="g-close", symbol="XAUUSD")  # 4300 >= 1.0: fires at once

    _run, events, _after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock, [policy, level],
        interval_ms=100, max_seconds=2,
    )

    # Both were evaluated: the policy moved a stop, the level rule closed out.
    assert [e["event"] for e in events if e["event"] == "stop_moved"]
    assert [e for e in events if e["event"] == "fired"]
    assert events[-1]["exit_reason"] == "max_seconds"


def test_a_moving_stop_guard_rule_is_built_from_the_same_flat_fields():
    from nanobot.agent.tools.mt5_sandbox import build_guard_rule

    rule, error = build_guard_rule(
        {"symbol": "XAUUSD", "guard_mode": "breakeven", "ticket": 9001}
    )
    assert error is None
    assert rule == {
        "symbol": "XAUUSD", "action": "move_stop", "mode": "breakeven", "ticket": 9001,
    }
    # No level, because there is nothing to cross -- and no 'when_r' when the
    # caller did not ask for one, so the CLI's own default is what applies.
    assert "price" not in rule and "when_r" not in rule

    rule, error = build_guard_rule(
        {
            "symbol": "XAUUSD", "guard_mode": "breakeven", "all_positions": True,
            "when_r": 2, "activate_at": 4320,
        }
    )
    assert error is None
    assert rule["scope"] == {"all": True} and "ticket" not in rule
    assert rule["when_r"] == 2.0 and rule["activate_at"] == 4320.0


def test_a_trail_guard_mode_without_a_distance_is_refused_by_name():
    from nanobot.agent.tools.mt5_sandbox import build_guard_rule

    rule, error = build_guard_rule(
        {"symbol": "XAUUSD", "guard_mode": "trail", "ticket": 9001}
    )
    assert rule is None and "trail_distance" in error
    # The field's own units are spelled out, because "2" means different things
    # on Gold and on EURUSD and guessing one of them is how a trail becomes a stop
    # that sits on the price.
    assert "Gold" in error

    rule, error = build_guard_rule(
        {"symbol": "XAUUSD", "guard_mode": "trail", "trail_distance": 2.0, "ticket": 9001}
    )
    assert error is None
    assert rule["action"] == "move_stop" and rule["mode"] == "trail"
    assert rule["distance"] == 2.0
    assert "when_r" not in rule


def test_a_guard_mode_nobody_implements_is_refused():
    from nanobot.agent.tools.mt5_sandbox import build_guard_rule

    rule, error = build_guard_rule({"symbol": "XAUUSD", "guard_mode": "wibble"})

    assert rule is None and "wibble" in error
    assert "breakeven" in error and "trail" in error


def test_guard_mode_close_still_needs_a_level_and_says_where_to_go_instead():
    from nanobot.agent.tools.mt5_sandbox import build_guard_rule

    rule, error = build_guard_rule({"symbol": "XAUUSD", "guard_mode": "close"})

    assert rule is None and "trigger_price" in error
    # The refusal has to name the alternative, or a caller wanting a moving stop
    # reads "you gave no level" and invents a level.
    assert "breakeven" in error and "trail" in error


def test_guard_arm_command_serialises_a_moving_stop_rule():
    cmd = build_cli_command(
        "guard",
        {
            "guard_action": "arm",
            "symbol": "XAUUSD",
            "guard_mode": "trail",
            "trail_distance": 2.0,
            "ticket": 9001,
            "interval_ms": 100,
        },
    )

    assert "guard arm --rule" in cmd
    assert '"action": "move_stop"' in cmd and '"mode": "trail"' in cmd
    assert '"distance": 2.0' in cmd
    assert "--interval-ms 100" in cmd
    # A moving stop has no level, so the caller is not asked for one.
    assert "price" not in cmd


def test_the_description_and_the_schema_offer_a_stop_that_moves_itself():
    tool = MT5SandboxTool()
    props = tool.parameters["properties"]

    assert props["guard_mode"]["enum"] == ["close", "breakeven", "trail"]
    # The units, in the schema: a trail distance is price, not pips.
    assert "20 pips" in props["trail_distance"]["description"]
    assert "R" in props["when_r"]["description"]

    desc = tool.description
    assert "MOVES ITSELF" in desc
    assert "guard_mode='breakeven'" in desc and "guard_mode='trail'" in desc
    # The reason it exists, named: nothing else runs between the model's calls.
    assert "between your calls" in desc


def test_a_waiting_moving_stop_says_so_instead_of_looking_absent(
    monkeypatch, tmp_path
):
    """Armed and quietly "not yet 1R" must not look like never armed.

    A policy rule that is WAITING writes no event -- correctly, since it would
    otherwise write one per tick -- so the state file is the only place the
    difference between "working quietly" and "not there" can be read. It is
    published exactly where the last near miss is.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock()
    mt5 = _FakeMT5(symbol="XAUUSD", bid=4310.0, ask=4310.2, tickets=(9001,))
    mt5.positions[0].price_open = 4300.0
    mt5.positions[0].sl = 4280.0

    rule = cli._validate_rule(
        {"symbol": "XAUUSD", "action": "move_stop", "mode": "breakeven",
         "ticket": 9001, "when_r": 1.0, "id": "g-be"}, 0, price_hint=4310.0,
    )
    run, events, _after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock, [rule],
        interval_ms=100, max_seconds=3,
    )

    waiting = run["state"]["stop_move"]["g-be"]
    assert waiting["outcome"] == "waiting" and waiting["mode"] == "breakeven"
    assert "not yet 1R" in waiting["detail"][0]
    # ...and "waiting" is never a move.
    assert [e for e in events if e["event"] == "stop_moved"] == []
    assert mt5.sltp_sends == []


def test_the_r_a_breakeven_uses_is_the_stop_the_position_was_opened_with(
    monkeypatch, tmp_path
):
    """The stop NOW is not the risk once anything has moved it.

    MEASURED LIVE 2026-09-24: a trail moved a Gold stop from 4293.09 to 4294.82,
    and a breakeven rule armed afterwards measured R as 0.44 instead of 2.17 --
    so its "1R" sat 0.44 above the entry and it would have announced a breakeven
    move at a level that was never 1R. The order that OPENED the position still
    says 4280, and that is the only honest source.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _StopMT5(4315.0, 4315.2, orders=(4280.0,))
    defs = _watcher_defs(cli, monkeypatch, mt5)
    rule = _breakeven_rule()
    # entry 4300, and a stop that has already been trailed up to 4294.82.
    position = _position(entry=4300.0, sl=4294.82)

    rows = defs["move_stops"](rule, [position], 30, 0)

    assert rule["move_ref"]["9001"]["risk"] == 20.0
    assert rule["move_ref"]["9001"]["risk_source"] == "the stop it was opened with"
    # 1R is 4320 -- not the 4300.44 the current stop would have produced.
    assert "4320.0" in rows[0]["skipped"]
    assert mt5.sl_sends == []


def test_the_opening_stop_is_the_earliest_order_that_carried_one(
    monkeypatch, tmp_path
):
    cli = _broker_cli(monkeypatch, tmp_path)
    # A modify appends a later order row carrying the trailed stop. The OPENER is
    # the one that says what the trade was sized on.
    mt5 = _StopMT5(4300.0, 4300.2, orders=(4280.0, 4294.0))
    defs = _watcher_defs(cli, monkeypatch, mt5)

    assert defs["initial_risk_price"](_position(entry=4300.0, sl=4294.0)) == 4280.0


def test_the_r_falls_back_to_the_current_stop_and_says_which_it_used(
    monkeypatch, tmp_path
):
    """A terminal that cannot answer must not silently look like one that did."""
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _StopMT5(4315.0, 4315.2)  # no order history at all
    defs = _watcher_defs(cli, monkeypatch, mt5)

    rows = defs["move_stops"](
        _breakeven_rule(), [_position(entry=4300.0, sl=4280.0)], 30, 0
    )

    assert rows[0]["risk_source"] == "the stop as it is now"
    assert "the stop as it is now" in rows[0]["skipped"]
    # No history is not a stop to invent.
    assert defs["initial_risk_price"](_position(entry=4300.0, sl=4280.0)) is None
    # ...and neither is history with no stop on any of its orders.
    bare = _StopMT5(4315.0, 4315.2, orders=(0.0, 0.0))
    bare_defs = _watcher_defs(cli, monkeypatch, bare)
    assert bare_defs["initial_risk_price"](_position(entry=4300.0, sl=4280.0)) is None


def test_a_breakeven_rule_will_not_measure_r_off_an_already_protected_stop(
    monkeypatch, tmp_path
):
    """Nothing to take off, and no honest R to measure -- so it does nothing.

    The alternative is a trigger computed from the distance to a stop that has
    already moved, i.e. a stop move justified by a number that means nothing.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    mt5 = _StopMT5(4310.0, 4310.2)  # no history available
    defs = _watcher_defs(cli, monkeypatch, mt5)
    # The stop is already past the entry: the trade is protected.
    position = _position(entry=4300.0, sl=4302.0)

    rows = defs["move_stops"](_breakeven_rule(), [position], 30, 0)

    assert rows[0]["ok"] is False
    assert "already at or past the entry" in rows[0]["skipped"]
    assert mt5.sl_sends == []


def test_a_moving_stop_with_nothing_to_protect_says_so_rather_than_going_silent(
    monkeypatch, tmp_path
):
    """An armed rule with nothing under it must not read as "not armed".

    MEASURED LIVE 2026-09-24: the position closed on its own trail and the rule
    vanished from the published state, because the "no matching position" exit
    happened before anything was recorded. To whoever armed it, that is the same
    answer as a rule that was never written.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    clock = _FakeClock()
    # The rule scopes a ticket that is NOT open: the position it was armed for is
    # already gone.
    mt5 = _FakeMT5(symbol="XAUUSD", bid=4300.0, ask=4300.2, tickets=(9002,))

    rule = cli._validate_rule(
        {"symbol": "XAUUSD", "action": "move_stop", "mode": "trail",
         "ticket": 9001, "distance": 2.0, "id": "g-trail"}, 0, 4300.0,
    )
    run, events, rules_after, _guard = _run_the_watcher(
        cli, monkeypatch, tmp_path, mt5, clock, [rule],
        interval_ms=100, max_seconds=2,
    )

    idle = run["state"]["stop_move"]["g-trail"]
    assert idle["outcome"] == "no_position"
    assert "no open position matches this rule" in idle["detail"][0]
    # Nothing was sent, and the rule is still armed for the next position.
    assert mt5.sltp_sends == []
    assert [r["id"] for r in rules_after] == ["g-trail"]
    assert [e for e in events if e["event"].startswith("stop_move")] == []


# --------------------------------------------------------------------------- #
# numeric arguments as a model actually emits them
#
# Reported failure, verbatim from an agent: "Due to platform-specific limitations
# ... the system requires numeric parameters but substitutes them as strings
# during variable expansion ... you can manually place the above stop order."
# There was no platform limitation. `sl=""` reached `float("")`, which raised a
# fieldless ValueError inside build_cli_command -- OUTSIDE the try/except that
# guards the transport -- so the model received a bare traceback, could not
# attribute it, and told a human to click the buttons instead. The bug class is
# "optional field arrives empty", which is what JSON-emitting models do constantly.
# --------------------------------------------------------------------------- #
def test_blank_optional_number_means_not_provided():
    """The crash case: an empty `sl`/`tp` must not reach float()."""
    cmd = build_cli_command(
        "order",
        {"symbol": "EURUSD", "side": "buy", "volume": 0.01, "sl": "", "tp": "",
         "allow_no_stop": True},
    )
    assert "--sl" not in cmd and "--tp" not in cmd
    assert "--volume 0.01" in cmd


def test_blank_required_number_is_dropped_not_zeroed():
    """`volume: ""` disappears rather than becoming a 0-lot order."""
    cmd = build_cli_command("order", {"symbol": "EURUSD", "side": "buy", "volume": ""})
    assert "--volume" not in cmd
    assert " 0" not in cmd.split("--side")[1]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0.01", "0.01"),        # number shipped as a string
        ("0,01", "0.01"),        # European decimal comma
        ("0.01 lots", "0.01"),   # unit pasted from a broker page
        (0.01, "0.01"),
    ],
)
def test_volume_is_read_whichever_way_it_arrives(raw, expected):
    cmd = build_cli_command(
        "order", {"symbol": "EURUSD", "side": "buy", "volume": raw, "allow_no_stop": True}
    )
    assert f"--volume {expected}" in cmd


def test_money_shaped_risk_is_read_as_money():
    """`risk_money` is a currency field, so models hand it back with the symbol on."""
    out = _normalize_numeric({"risk_money": "$50", "risk_pct": "1.5%"})
    assert out["risk_money"] == 50.0
    assert out["risk_pct"] == 1.5


@pytest.mark.parametrize(
    "field, value",
    [
        ("sl", "soon"),          # not a number in any notation
        ("volume", "0.01,0.02"), # two numbers glued together is one bad number
        ("volume", True),        # bool is an int subclass; True is not a lot size
    ],
)
def test_unparseable_number_names_its_field(field, value):
    """The message must be attributable, or the model can only guess and stall."""
    with pytest.raises(BadNumberError) as raised:
        _normalize_numeric({"symbol": "EURUSD", field: value})
    assert raised.value.field == field
    assert field in str(raised.value)


def test_fractional_ticket_is_refused_not_rounded():
    """A ticket that silently rounds would close the WRONG position."""
    assert _normalize_numeric({"ticket": 4736608160.0})["ticket"] == 4736608160
    with pytest.raises(BadNumberError):
        _normalize_numeric({"ticket": 4736608160.5})


def test_execute_reports_bad_number_instead_of_traceback():
    """The tool must answer with JSON naming the field and confirming nothing fired."""
    tool = MT5SandboxTool()
    result = asyncio.run(
        tool.execute(action="order", symbol="EURUSD", side="buy", volume=0.01, sl="soon")
    )
    payload = json.loads(result.content) if hasattr(result, "content") else json.loads(result)
    assert payload["error"] == "bad_numeric_argument"
    assert payload["field"] == "sl"
    assert "no position changed" in payload["next"]


def test_numeric_fields_match_the_schema():
    """The schema is the contract; the coercion sets must not drift from it.

    Checks BOTH directions:
      * a numeric field in `parameters` that is in neither set keeps the old
        crash-on-blank behaviour, silently, until an agent tells a user to place
        the trade by hand;
      * a field put in the wrong set is worse than missing -- `limit` and
        `deviation` are integers, and an integer read as a float lets `2.7`
        through to a flag that then truncates it.
    """
    declared = MT5SandboxTool().parameters["properties"]
    schema_floats = {
        n for n, s in declared.items()
        if isinstance(s, dict) and s.get("type") == "number"
    }
    schema_ints = {
        n for n, s in declared.items()
        if isinstance(s, dict) and s.get("type") == "integer"
    }
    assert schema_floats == _FLOAT_FIELDS, (
        f"number fields out of sync: missing={sorted(schema_floats - _FLOAT_FIELDS)} "
        f"extra={sorted(_FLOAT_FIELDS - schema_floats)}"
    )
    assert schema_ints == _INT_FIELDS, (
        f"integer fields out of sync: missing={sorted(schema_ints - _INT_FIELDS)} "
        f"extra={sorted(_INT_FIELDS - schema_ints)}"
    )
    # A field in both sets would be parsed as an integer by accident of iteration.
    assert not (_FLOAT_FIELDS & _INT_FIELDS)


# --------------------------------------------------------------------------- #
# A NAMED server is never answered with the deployment's default build
# --------------------------------------------------------------------------- #
#: A broker this repo does not register, i.e. the case the registry cannot serve.
_PEPPERSTONE_URL = (
    "https://download.mql5.com/cdn/web/pepperstone.design/mt5/pepperstone5setup.exe"
)


def _install_namespace(script, *, server=None, url="", dir_name=""):
    import argparse

    return argparse.Namespace(
        script=str(script),
        server=server,
        broker_installer_url=url,
        broker_dir_name=dir_name,
        timeout=60,
        detach=True,
        foreground=False,
    )


def _install_script(tmp_path):
    script = tmp_path / "install_mt5_sandbox.sh"
    script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    return script


def _launcher(monkeypatch, cli):
    """Record every process the CLI tries to start, and start none of them."""
    import subprocess as _subprocess

    launched: list[list[Any]] = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: (
            launched.append(list(a))
            or _subprocess.CompletedProcess(a, 0, "4242\n", "")
        ),
    )
    return launched


def test_an_unregistered_server_is_refused_not_given_the_default_build(
    monkeypatch, tmp_path, capsys
):
    """``install --server <unknown>`` must NOT lay down the deployment's broker.

    MEASURED FAILURE (2026-09-24, live): ``install --server Pepperstone-Demo`` fell
    through to the installer's own default and the EXNESS build landed on disk, under
    a green result. That terminal carries no Pepperstone server list, so it could not
    perform the login it was installed for -- and the wrong build only becomes
    visible a call later, as a refused login, which reads as an MT5 or network fault.

    A refusal costs one turn. A silent wrong build costs the whole run, so this is
    the property worth pinning: nothing is downloaded, nothing is recorded, and the
    answer says what to do instead.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    monkeypatch.delenv("MT5_BROKER_INSTALLER_URL", raising=False)
    launched = _launcher(monkeypatch, cli)

    code = cli.cmd_install(
        _install_namespace(_install_script(tmp_path), server="Pepperstone-Demo")
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert code == 2
    assert payload["ok"] is False
    assert payload["failure"] == "server_not_resolved"
    assert payload["requested_server"] == "Pepperstone-Demo"
    # The machine itself: no installer ran, and no target was recorded for `status`
    # to poll -- an install that never started must not look like one that did.
    assert launched == []
    assert not cli.INSTALL_TARGET_FILE.exists()
    # The remedy is a URL from the broker's own page, reachable without a human.
    assert "broker_installer_url" in payload["remedy"]
    assert "discover_broker" in payload["next"]
    assert "exness" in payload["registry_brokers"]


def test_an_inherited_default_url_cannot_answer_for_a_named_server(
    monkeypatch, tmp_path, capsys
):
    """The box exports its default URL; a NAMED server must not be resolved by it.

    MEASURED (2026-09-24, live): the sandbox carries ``MT5_BROKER_INSTALLER_URL`` in
    its environment for its own bare installs. Reading that variable as "the answer"
    made every ``install --server <unknown>`` look like an explicit request, so the
    resolver had nothing left to refuse and the default build shipped anyway. argv is
    now authoritative for a named server; the environment only chooses the default
    for a bare install, which is what the variable is for.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("MT5_BROKER_INSTALLER_URL", cli.DEFAULT_INSTALLER_URL)
    launched = _launcher(monkeypatch, cli)

    code = cli.cmd_install(
        _install_namespace(_install_script(tmp_path), server="Pepperstone-Demo")
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert code == 2
    assert payload["failure"] == "server_not_resolved"
    assert launched == []


def test_a_registered_server_beats_an_inherited_default_url(monkeypatch, tmp_path):
    """The refusal must not swallow the registry path it sits next to.

    The default URL is in the environment AND the server is one the registry knows:
    the registry wins, so a bare ``install --server Deriv-Demo`` on a box that
    exports the Exness default still lays down the Deriv build.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("MT5_BROKER_INSTALLER_URL", cli.DEFAULT_INSTALLER_URL)
    _launcher(monkeypatch, cli)

    cli.cmd_install(_install_namespace(_install_script(tmp_path), server="Deriv-Demo"))

    recorded = cli.INSTALL_TARGET_FILE.read_text(encoding="utf-8").strip()
    assert recorded == cli.broker_for_server("Deriv-Demo")["url"]
    assert recorded != cli.DEFAULT_INSTALLER_URL


def test_a_url_the_caller_supplies_is_still_the_way_through(monkeypatch, tmp_path):
    """Discovery's escape hatch: an explicit URL installs an unregistered broker.

    This is what the tool uses after it resolves a URL itself, so refusing without
    accepting a URL would trade a silent wrong build for a dead end.
    """
    cli = _broker_cli(monkeypatch, tmp_path)
    _launcher(monkeypatch, cli)

    cli.cmd_install(
        _install_namespace(
            _install_script(tmp_path),
            server="Pepperstone-Demo",
            url=_PEPPERSTONE_URL,
            dir_name="MetaTrader 5 Pepperstone",
        )
    )

    assert cli.INSTALL_TARGET_FILE.read_text(encoding="utf-8").strip() == _PEPPERSTONE_URL


def test_the_installer_url_is_passed_in_argv_where_the_cli_trusts_it():
    """Both spellings, because the two layers read different ones.

    The installer reads the environment variable; the CLI trusts argv. Emitting only
    the env prefix is what let an inherited deployment default masquerade as the
    caller's answer.
    """
    command = build_cli_command(
        "install",
        {
            "server": "Pepperstone-Demo",
            "broker_installer_url": _PEPPERSTONE_URL,
            "broker_dir_name": "MetaTrader 5 Pepperstone",
        },
    )

    assert "--broker-installer-url" in command
    assert "--broker-dir-name" in command
    assert f"MT5_BROKER_INSTALLER_URL={_PEPPERSTONE_URL}" in command


def test_the_installer_script_refuses_to_build_a_default_for_a_named_server(tmp_path):
    """Layer 2 of the same rule, at the layer that actually downloads.

    ``mt5_cli.py`` refuses the combination before it reaches here. This guard is the
    same decision in the script itself, so a hand-run install cannot slip past the
    CLI -- and it reads the CALLER's URL, which is why it has to sit above the
    ``:-default`` assignment in the script.
    """
    import subprocess as _subprocess

    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "install_mt5_sandbox.sh"
    )
    text = script.read_text(encoding="utf-8")
    # Everything up to and including the guard; past it the script needs Wine.
    end = text.index("  exit 64\nfi\n") + len("  exit 64\nfi\n")
    head = tmp_path / "installer-guard.sh"
    head.write_text(text[:end], encoding="utf-8")

    def run(**env):
        return _subprocess.run(
            ["bash", str(head)],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
        )

    refused = run(MT5_BROKER_SERVER="AXI-Live", MT5_BROKER_INSTALLER_URL="")
    assert refused.returncode == 64
    assert "AXI-Live" in refused.stderr

    # A URL for that broker, and the MetaQuotes generic build, both proceed.
    assert run(
        MT5_BROKER_SERVER="AXI-Live", MT5_BROKER_INSTALLER_URL=_PEPPERSTONE_URL
    ).returncode == 0
    assert run(
        MT5_BROKER_SERVER="MetaQuotes-Demo",
        MT5_BROKER_INSTALLER_URL="",
        MT5_GENERIC_INSTALLER="1",
    ).returncode == 0
    # And a bare install -- no server named -- still gets the deployment default.
    assert run(MT5_BROKER_INSTALLER_URL="").returncode == 0


class _QueuedSandbox:
    """A sandbox that answers each command with the next scripted reply."""

    name = "novita_sandbox"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if not self.responses:
            return '{"ok": true, "stage": "done"}\n[exit_code=0]'
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_an_install_refusal_is_resolved_by_the_tool_not_the_user(monkeypatch):
    """The agent owns the install: a refusal is resolved, not handed back.

    Refusing ``install --server X`` is right -- the alternative is a terminal that
    cannot resolve X -- but "install my broker" still has an answer the agent can
    find, so returning the refusal would leave the user holding a resolver puzzle.
    The tool discovers a validated URL and runs the install in the same call.
    """
    import nanobot.agent.tools.mt5_sandbox as module

    discovery_calls: list[tuple[str, Any]] = []

    async def fake_discover(server, page_urls=None):
        discovery_calls.append((server, page_urls))
        return {"url": _PEPPERSTONE_URL, "dir_name": "MetaTrader 5 Pepperstone"}

    monkeypatch.setattr(module, "_discover_for_server", fake_discover)

    refusal = json.dumps(
        {
            "ok": False,
            "failure": "server_not_resolved",
            "requested_server": "Pepperstone-Demo",
            "remedy": {"action": "install", "server": "Pepperstone-Demo"},
            "next": "pass broker_installer_url",
        }
    )
    sandbox = _QueuedSandbox(
        [
            refusal + "\n[exit_code=2]",
            '{"ok": true, "stage": "installing"}\n[exit_code=0]',
            '{"ok": true, "stage": "done", "installed": true}\n[exit_code=0]',
        ]
    )
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="install", server="Pepperstone-Demo")
    rendered = str(result)

    assert "server_not_resolved" not in rendered, rendered
    assert _PEPPERSTONE_URL in rendered
    assert discovery_calls == [("Pepperstone-Demo", [])]

    installs = [c for c in sandbox.calls if "mt5_cli.py install" in str(c["command"])]
    # Twice: the refused first attempt, then the resolved one. The second carries
    # the URL discovery validated -- never the deployment default.
    assert len(installs) == 2
    assert _PEPPERSTONE_URL in str(installs[-1]["command"])
    assert "exness5setup.exe" not in str(installs[-1]["command"])
    assert "MT5_BROKER_DIR_NAME" in str(installs[-1]["command"])
    assert "resolved_installer_url" in rendered


def test_an_install_that_started_detached_is_still_work_in_flight():
    """``stage`` is not the only shape an unfinished install has.

    A detached start answers with ``detached: true`` and a pid and NO ``stage``, so a
    wait keyed on ``stage`` alone skips every ordinary install. MEASURED 2026-09-25,
    live: the agent's own ``install --server AXI-Live`` returned in 3 s with
    ``{"detached": true, "pid": "14144"}`` and a "poll until done" hint -- the poll
    handed back to the caller, against this tool's own rule.
    """
    from nanobot.agent.tools.mt5_sandbox import _install_in_flight

    assert _install_in_flight({"ok": True, "detached": True, "pid": "14144"}) is True
    assert _install_in_flight({"ok": True, "stage": "installing"}) is True
    # Everything that is over, or never started, must NOT be waited on.
    assert _install_in_flight({"ok": True, "stage": "done"}) is False
    assert _install_in_flight({"ok": False, "detached": True}) is False
    assert _install_in_flight({"ok": True, "stage": "failed"}) is False
    assert _install_in_flight({}) is False


@pytest.mark.asyncio
async def test_the_agent_install_waits_out_a_detached_start():
    """One call does the whole job: kick the install, then watch it to a terminal stage."""
    sandbox = _QueuedSandbox(
        [
            '{"ok": true, "detached": true, "pid": "14144", "broker": "deriv"}\n[exit_code=0]',
            '{"ok": true, "stage": "done", "installed": true}\n[exit_code=0]',
        ]
    )
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="install", server="Deriv-Demo")
    rendered = str(result)

    assert '"stage": "done"' in rendered
    assert '"detached": true' not in rendered, "the start payload must not be the answer"
    assert any("mt5_cli.py status" in str(c["command"]) for c in sandbox.calls), (
        "the tool must poll status rather than hand the poll back to the model"
    )
