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
    """The tool's auto-install list must match what the CLI can actually resolve."""
    cli = _broker_cli(monkeypatch, tmp_path)
    for prefix in ("MetaQuotes-Demo", "Exness-MT5Trial9"):
        assert _server_is_known(prefix) is True
        assert cli.broker_for_server(prefix) is not None
    assert _server_is_known("SomeOtherBroker-Demo") is False
    assert _server_is_known(None) is False


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
