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
async def test_not_installed_refusal_auto_provisions_instead_of_erroring():
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