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

import importlib.util
import inspect
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.mt5_sandbox import (
    MT5SandboxTool,
    _INSTALL_COMMAND_TIMEOUT,
    _TIMEOUTS,
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
    """
    _isolate_prefix(monkeypatch, tmp_path)
    info = _load_cli_module().installed_chain()

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
async def test_install_action_itself_does_not_recurse_into_auto_provision():
    """action='install' must not auto-provision itself forever."""
    payload = (
        '{"ok": true, "detached": true, "stage": "in_progress"}\n[exit_code=0]'
    )
    sandbox = _FakeSandbox(payload)
    tool = MT5SandboxTool.create(_ctx({"novita_sandbox": sandbox}))

    result = await tool.execute(action="install")

    # Exactly one forwarded call: install must not trigger a second one.
    assert len(sandbox.calls) == 1
    assert "detached" in str(result)


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


def test_broker_server_probe_flags_the_generic_terminal(tmp_path):
    """The probe that turns a silent failure into a loud one.

    Size is the only readable signal: servers.dat is not plain text (a string
    scan finds just the copyright), so the generic 50 KB build and the branded
    234 KB build are told apart by size.
    """
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
