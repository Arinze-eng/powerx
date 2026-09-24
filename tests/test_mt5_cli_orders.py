"""Regression tests for ``scripts/mt5_cli.py``'s trade/ordering layer.

Every case here is a bug that was reproduced against a live MetaQuotes-Demo
account in a Novita sandbox (Wine 10.0, MT5 build 6204, MetaTrader5 5.0.6180)
on 2026-09-20, then fixed. The MT5 module is faked, so no Wine/network is needed.

1. **Filling mode is a bitmask, and ``SYMBOL_FILLING_*`` does not exist.**
   The old code compared ``info.filling_mode == mt5.SYMBOL_FILLING_FOK``, which
   raised ``AttributeError`` on *every* order — trading looked impossible while
   login, quotes and account info all worked.

2. **A 10030 rejection is a mode mismatch, not a trade decision**, so it must be
   retried against the symbol's other supported modes; a real rejection
   (10019 no money, 10018 market closed) must NOT be resent.

3. **Process discovery must not use ``pkill -x``/``pkill -f``.** Wine runs the
   terminal with kernel comm ``main``, so ``-x terminal64.exe`` matched nothing
   (``stop`` was a silent no-op) while ``-f`` also matches the calling shell and
   ``wineserver`` (measured: ``pgrep -x`` -> 0, ``pgrep -f`` -> 4).
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
from datetime import datetime, timedelta, timezone
import types
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mt5_cli.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("mt5_cli_under_test", CLI_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cli():
    return _load_cli()


class FakeMT5:
    """Just the constants the real 5.0.6180 module exposes for filling.

    Plus enough of a terminal to be asked what is open: every order passes
    through the account's risk gate, which reads the book before it agrees to
    send anything.
    """

    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ORDER_FILLING_BOC = 3
    TRADE_RETCODE_DONE = 10009

    def __init__(self, retcodes):
        self._retcodes = iter(retcodes)
        self.sent: list[int] = []
        self.calls = 0

    def order_send(self, request):
        self.calls += 1
        self.sent.append(request["type_filling"])
        code = next(self._retcodes, self.TRADE_RETCODE_DONE)
        return types.SimpleNamespace(
            retcode=code, comment="fake", order=11, deal=22,
        )

    def last_error(self):
        return "fake-last-error"

    def positions_get(self, ticket=None):
        return []

    def account_info(self):
        return types.SimpleNamespace(
            equity=10000.0, balance=10000.0, margin_free=10000.0, currency="USD",
        )

    def symbols_get(self):
        return []

    def history_deals_get(self, start, end):
        return []


def _info(mask):
    return types.SimpleNamespace(filling_mode=mask)


# --------------------------------------------------------------------------- #
# 1. the constants that broke every order really are absent
# --------------------------------------------------------------------------- #
def test_symbol_filling_constants_do_not_exist():
    """Guard the premise: relying on ``mt5.SYMBOL_FILLING_*`` is an AttributeError."""
    assert not hasattr(FakeMT5, "SYMBOL_FILLING_FOK")
    assert not hasattr(FakeMT5, "SYMBOL_FILLING_IOC")


@pytest.mark.parametrize(
    "mask,expected",
    [
        (1, ["ORDER_FILLING_FOK"]),                      # FOK only   (EURUSD)
        (2, ["ORDER_FILLING_IOC"]),                      # IOC only
        (3, ["ORDER_FILLING_IOC", "ORDER_FILLING_FOK"]), # both: IOC first
        (4, ["ORDER_FILLING_RETURN"]),                   # return only
    ],
)
def test_filling_candidates_decodes_the_bitmask(cli, mask, expected):
    got = [
        {0: "ORDER_FILLING_FOK", 1: "ORDER_FILLING_IOC", 2: "ORDER_FILLING_RETURN"}[c]
        for c in cli.filling_candidates(FakeMT5, _info(mask))
    ]
    assert got == expected


def test_filling_candidates_handles_missing_or_zero_mask(cli):
    """A zero/absent mask must fall back to trying every mode, not crash."""
    assert len(cli.filling_candidates(FakeMT5, _info(0))) == 3
    assert len(cli.filling_candidates(FakeMT5, None)) == 3
    assert len(cli.filling_candidates(FakeMT5, types.SimpleNamespace())) == 3


def test_no_source_references_the_missing_constants(cli):
    """The whole class of AttributeError must be gone from the file."""
    src = CLI_PATH.read_text(encoding="utf-8")
    assert "mt5.SYMBOL_FILLING_" not in src


# --------------------------------------------------------------------------- #
# 2. retry policy on 10030
# --------------------------------------------------------------------------- #
def test_retries_only_on_unsupported_filling(cli):
    fake = FakeMT5([10030, FakeMT5.TRADE_RETCODE_DONE])
    out = cli._order_send(fake, {"type_filling": 1}, [1, 0])
    assert out["ok"] is True
    assert fake.sent == [1, 0]            # both modes attempted
    assert out["filling_used"] == 0


@pytest.mark.parametrize("retcode", [10018, 10019, 10016, 10030 + 1])
def test_real_rejections_are_not_resent(cli, retcode):
    """10018/10019 are answers, not mode mismatches — resending spams the broker."""
    fake = FakeMT5([retcode])
    out = cli._order_send(fake, {"type_filling": 1}, [1, 0])
    assert out["ok"] is False
    assert fake.calls == 1


def test_reports_failure_when_every_mode_is_rejected(cli):
    fake = FakeMT5([10030, 10030, 10030])
    out = cli._order_send(fake, {"type_filling": 1}, [1, 0, 2])
    assert out["ok"] is False
    assert fake.sent == [1, 0, 2]


def test_order_send_survives_a_none_result(cli):
    class Boom(FakeMT5):
        def order_send(self, request):
            self.calls += 1
            return None

    out = cli._order_send(Boom([10009]), {"type_filling": 1}, [1])
    assert out["ok"] is False
    assert "error" in out


# --------------------------------------------------------------------------- #
# 3. process discovery
# --------------------------------------------------------------------------- #
def test_terminal_pids_never_matches_itself(cli):
    """A pattern kill that hits the caller is how the sandbox commands died."""
    assert os.getpid() not in cli._terminal_pids()


def test_stop_does_not_use_pkill_patterns():
    src = CLI_PATH.read_text(encoding="utf-8")
    assert '"-x", "terminal64.exe"' not in src
    assert "pkill\", \"-f\", \"terminal64" not in src


def test_terminal_running_uses_proc_scan(cli):
    src = CLI_PATH.read_text(encoding="utf-8")
    assert "return bool(_terminal_pids())" in src


def test_every_bridge_action_is_reenacted_under_wine(cli):
    """A bridge action missing from ``_BRIDGE_ACTIONS`` fails as a fake install.

    It does not raise: the command runs on the Linux python, where importing
    ``MetaTrader5`` is impossible, and returns the generic refusal "mt5_cli.py
    must run inside Wine". Measured live with the newly added ``symbols`` action,
    which read like a broken install rather than a dispatch miss.
    """
    import ast

    source = CLI_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    def callees(node: ast.AST) -> set[str]:
        out: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                fn = child.func
                if isinstance(fn, ast.Name):
                    out.add(fn.id)
                elif isinstance(fn, ast.Attribute):
                    out.add(fn.attr)
        return out

    funcs = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    needs_bridge = {"require_bridge"}

    # Fixpoint: a function needs the bridge if it calls one that does.
    changed = True
    while changed:
        changed = False
        for name, node in funcs.items():
            if name in needs_bridge:
                continue
            if callees(node) & needs_bridge:
                needs_bridge.add(name)
                changed = True

    parser = cli.build_parser()
    actions = {}
    for sub in parser._subparsers._group_actions:  # noqa: SLF001 - test introspection
        for name, subparser in sub.choices.items():
            actions[name] = subparser.get_default("func")
    assert actions, "no subcommands discovered"

    missing = []
    for action, func in actions.items():
        if func is None:
            continue
        if func.__name__ in needs_bridge and action not in cli._BRIDGE_ACTIONS:
            missing.append(action)
    assert missing == [], f"bridge actions missing from _BRIDGE_ACTIONS: {missing}"

    # And the reverse: nothing is re-exec'd through Wine without needing it.
    assert "symbols" in cli._BRIDGE_ACTIONS


# --------------------------------------------------------------------------- #
# 4. fixtures used by the sandbox verification run
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 3b. credential handling + log payloads
#
# Both reproduced against a live Novita sandbox on 2026-09-21, immediately after
# a successful Wine + MT5 install, driving the exact code path the agent uses.
# --------------------------------------------------------------------------- #
def _log(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.write_bytes(text.encode(encoding))
    return path


def test_tail_decodes_utf16le_terminal_logs(cli, tmp_path):
    """MT5 writes UTF-16LE; reading it as UTF-8 produced NUL-interleaved garbage.

    That garbage cost ~2 characters per real character and then ~6 more when
    JSON-escaped, which is what pushed ``logs`` output past the sandbox's output
    cap and destroyed its JSON.
    """
    log = _log(
        tmp_path / "20260921.log",
        "IL\t0\t04:29:48.588\tLiveUpdate\tdownloaded successfully\n",
        encoding="utf-16-le",
    )
    # BOM-less UTF-16 is the real shape of the terminal log's first write.
    (tmp_path / "20260921.log").write_bytes(b"\xff\xfe" + log.read_bytes())
    tail = cli._tail(tmp_path / "20260921.log", 40)
    assert "\x00" not in tail
    assert "LiveUpdate" in tail
    assert "downloaded successfully" in tail


def test_tail_detects_bomless_utf16(cli, tmp_path):
    log = _log(tmp_path / "metaeditor.log", "Compile\tSymbolInfoSample.mq5\t0 errors\n",
               encoding="utf-16-le")
    tail = cli._tail(log, 40)
    assert "\x00" not in tail
    assert "0 errors" in tail


def test_log_payload_always_fits_the_sandbox_output_cap(cli, tmp_path):
    """A truncated JSON is unparseable, so the payload must be trimmed, not cut.

    Measured: ``logs --lines 60`` over three UTF-16 logs serialized to ~43 000
    characters. The sandbox wrapper keeps only the last 16 000, which sliced off
    the JSON's opening brace; the host-side parser then found nothing and told
    the agent "MT5 command produced no JSON result", i.e. that the install was
    broken. Trimming the tails keeps the JSON whole.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    for name in ("20260921.log", "metaeditor.log", "old.log"):
        body = "\n".join(
            f"IL\t0\t04:29:48.588\tLiveUpdate\tline {i} with enough text to add up"
            for i in range(400)
        )
        (logs / name).write_bytes(b"\xff\xfe" + body.encode("utf-16-le"))

    payload = cli._log_payload(logs, "*.log", 60)
    rendered = json.dumps(payload, default=str)
    # Under the CLI's own budget, and therefore far under the wrapper's cap.
    assert len(rendered) <= cli._LOG_PAYLOAD_BUDGET
    assert len(rendered) <= 16_000
    assert payload["truncated"] is True
    # The head -- the opening brace -- must survive, and it must re-parse.
    assert rendered.startswith("{")
    assert json.loads(rendered)["files"]
    assert "\x00" not in rendered


def test_fit_payload_trims_a_symbol_list_too(cli):
    """The same cap applies to any list payload, not just log tails."""
    payload = cli._fit_payload(
        {
            "ok": True,
            "symbols": [
                {"name": f"SYMBOL{i:04d}", "group": "Forex", "trade_mode": 4,
                 "volume_min": 0.01, "market_open": True}
                for i in range(400)
            ],
            "note": "choose a symbol with market_open=true",
        },
        budget=cli._LIST_PAYLOAD_BUDGET,
    )
    rendered = json.dumps(payload, default=str)
    assert len(rendered) <= cli._LIST_PAYLOAD_BUDGET
    assert rendered.startswith("{")
    assert json.loads(rendered)["symbols"]
    assert payload["truncated"] is True


class FakeSymbolsMT5:
    """Enough of the module for ``cmd_symbols``, with a broker clock ~3 h ahead.

    MEASURED FAILURE (2026-09-21, live MetaQuotes-Demo, Sunday): the first version
    of ``symbols`` compared each tick's ``time`` against the SANDBOX clock and
    reported ``last_tick_age_s: -10798`` — a negative age, a quote from the
    future. Broker server time runs ahead of the box, so every frozen weekend
    quote looked newer than "now", all 26 FX symbols were labelled
    ``market_open: true``, and the flag meant nothing. A trade picked from that
    list could only ever die with retcode 10018 "Market closed".

    FX is exactly the case that matters: MetaQuotes-Demo carries no crypto, so
    the agent has to pick from the FX list, and it has to know whether that list
    is live.
    """

    def __init__(self, tick_times):
        # tick_times: symbol name -> tick epoch seconds (0 == never ticked)
        self._tick_times = tick_times

    def symbols_get(self):
        return [
            types.SimpleNamespace(
                name=name, path=f"Forex\\{name}", trade_mode=4, visible=True,
                volume_min=0.01, volume_step=0.01, spread=1, digits=5,
                filling_mode=1,
            )
            for name in self._tick_times
        ]

    def symbol_info_tick(self, name):
        return types.SimpleNamespace(time=self._tick_times.get(name, 0))


def _symbols_payload(cli, monkeypatch, capsys, tick_times, **kwargs):
    monkeypatch.setattr(
        cli, "require_bridge", lambda: (FakeSymbolsMT5(tick_times), None)
    )
    args = types.SimpleNamespace(
        filter=kwargs.pop("filter", ""), tradable=kwargs.pop("tradable", False),
        limit=kwargs.pop("limit", 50), fresh_seconds=kwargs.pop("fresh_seconds", 300),
    )
    assert cli.cmd_symbols(args) == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_symbols_age_ignores_sandbox_clock_skew(cli, monkeypatch, capsys):
    """A stale quote must never read as fresh just because the box clock lags."""
    skew = 10_800  # measured: broker server time ~3 h ahead of the sandbox
    latest = int(time.time()) + skew
    payload = _symbols_payload(
        cli, monkeypatch, capsys,
        {"EURUSD": latest, "USDJPY": latest - 7_200, "GBPUSD": 0},
    )

    ages = {row["name"]: row["last_tick_age_s"] for row in payload["symbols"]}
    # Every age is measured against the broker's newest tick, so none is negative.
    assert ages["EURUSD"] == 0
    assert ages["USDJPY"] == 7_200
    assert all(v is None or v >= 0 for v in ages.values())
    # GBPUSD never ticked: unknown age, and therefore not market-open.
    assert ages["GBPUSD"] is None

    open_now = {row["name"] for row in payload["symbols"] if row["market_open"]}
    assert open_now == {"EURUSD"}
    assert payload["market_open_now"] == 1
    # The skew itself is reported so the agent can see why it looked wrong.
    assert 10_700 <= payload["sandbox_clock_skew_s"] <= 10_900


def test_symbols_tradable_filter_excludes_stale_weekend_quotes(cli, monkeypatch, capsys):
    """``tradable=true`` is the pre-trade check: it must not hand back a dead symbol."""
    latest = int(time.time()) + 10_800
    payload = _symbols_payload(
        cli, monkeypatch, capsys,
        {
            "AUDUSD": latest,          # ticking now
            "NZDUSD": latest - 60,     # within fresh_seconds
            "USDCAD": latest - 9_000,  # weekend-frozen
            "USDCHF": 0,               # never ticked
        },
        tradable=True,
    )
    assert {row["name"] for row in payload["symbols"]} == {"AUDUSD", "NZDUSD"}
    assert payload["matching"] == 2
    assert payload["market_open_now"] == 2


def test_symbols_reports_zero_open_when_the_whole_server_is_closed(cli, monkeypatch, capsys):
    """Sunday FX: say so plainly, so a later 10018 is read as session, not bug."""
    latest = int(time.time()) + 10_800
    payload = _symbols_payload(
        cli, monkeypatch, capsys,
        {"EURUSD": latest - 80_000, "USDJPY": latest - 79_000},
    )
    assert payload["market_open_now"] == 0
    assert all(row["market_open"] is False for row in payload["symbols"])
    assert "10018" in payload["note"]


class _Deal:
    """The MetaTrader5 wrapper returns namedtuples, so deals carry ``_asdict``."""

    def __init__(self, **fields):
        self._fields = fields

    def _asdict(self):
        return dict(self._fields)


class FakeHistoryMT5:
    """A terminal whose ticks run ``offset`` seconds ahead of this box."""

    def __init__(self, offset, deals=()):
        self._offset = offset
        self._deals = list(deals)
        self.window = None

    def symbol_info_tick(self, name):
        if name != "EURUSD":
            return None
        return types.SimpleNamespace(time=int(time.time()) + self._offset)

    def symbols_get(self):
        return [types.SimpleNamespace(name="EURUSD")]

    def history_deals_get(self, start, end):
        self.window = (start, end)
        return [_Deal(**d) for d in self._deals]

    def last_error(self):
        return "fake-last-error"


def test_history_window_is_anchored_to_the_broker_clock(cli, monkeypatch, capsys):
    """THE missing-trade bug: a window built from the box clock hid the deals.

    Measured live: a buy filled with retcode 10009 and showed up in ``positions``,
    but ``history --days 1`` and ``--days 7`` returned only the account's opening
    deposit. The MetaTrader5 wrapper hands the datetimes to the terminal as
    SERVER time (UTC+3 here), so ``datetime.now()`` from the UTC box asked for a
    window ending three hours in the broker's past — every deal of that session
    was outside it.
    """
    offset = 10_799
    fake = FakeHistoryMT5(offset)
    monkeypatch.setattr(cli, "require_bridge", lambda: (fake, None))

    assert cli.cmd_history(types.SimpleNamespace(days=1)) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    start, end = fake.window
    box_now = datetime.now(timezone.utc).replace(tzinfo=None)
    # The window must end at or beyond the BROKER's now, not the box's.
    assert end >= box_now + timedelta(seconds=offset)
    assert abs((end - start) - timedelta(days=1)) == timedelta(0)
    assert abs(payload["server_clock_offset_s"] - offset) <= 2
    assert payload["window"]["timezone"] == "broker server time"


def test_history_window_never_ends_in_the_past_when_the_market_is_closed(cli, monkeypatch, capsys):
    """A stale (weekend) tick is not an offset, so it must not shrink the window."""
    fake = FakeHistoryMT5(-400_000)  # newest tick 4.6 days old: market closed
    monkeypatch.setattr(cli, "require_bridge", lambda: (fake, None))

    assert cli.cmd_history(types.SimpleNamespace(days=2)) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    start, end = fake.window
    box_now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert payload["server_clock_offset_s"] == 0
    assert end > box_now
    # Deals are reported newest-first so trimming a long window drops the oldest.
    assert payload["order"] == "newest_first"


def test_history_puts_the_newest_deal_first(cli, monkeypatch, capsys):
    fake = FakeHistoryMT5(
        10_799,
        deals=[
            {"ticket": 1, "time": 1_000, "symbol": "EURUSD", "profit": 0.0},
            {"ticket": 2, "time": 9_000, "symbol": "GBPUSD", "profit": -0.02},
            {"ticket": 3, "time": 5_000, "symbol": "USDJPY", "profit": 0.01},
        ],
    )
    monkeypatch.setattr(cli, "require_bridge", lambda: (fake, None))

    assert cli.cmd_history(types.SimpleNamespace(days=7)) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert [d["ticket"] for d in payload["deals"]] == [2, 3, 1]
    assert payload["count"] == 3


def test_history_payload_stays_under_the_sandbox_output_cap(cli, monkeypatch, capsys):
    """A month of deals must not push the JSON past the wrapper's 16 000 chars."""
    fake = FakeHistoryMT5(
        10_799,
        deals=[
            {"ticket": i, "time": i, "symbol": f"SYM{i:03d}", "profit": float(i),
             "comment": "a long enough comment to add up over many deals"}
            for i in range(600)
        ],
    )
    monkeypatch.setattr(cli, "require_bridge", lambda: (fake, None))

    assert cli.cmd_history(types.SimpleNamespace(days=30)) == 0
    raw = capsys.readouterr().out.strip().splitlines()[-1]
    assert len(raw) <= cli._LIST_PAYLOAD_BUDGET
    assert len(raw) <= 16_000
    payload = json.loads(raw)
    assert payload["truncated"] is True
    # The newest deals survive the trim, the oldest are dropped.
    assert payload["deals"][0]["ticket"] == 599
    assert payload["count"] == 600


def test_cmd_logs_reports_missing_logs_instead_of_empty_json(cli, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "MT5_ROOT", tmp_path / "mt5")
    monkeypatch.setattr(cli, "WINE_PREFIX", tmp_path / "wine")
    code = cli.cmd_logs(types.SimpleNamespace(lines=20))
    assert code == 2
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert "start" in payload["error"]


def test_start_restarts_a_terminal_that_lacks_credentials(cli, monkeypatch, tmp_path):
    """THE credential bug: ``start`` skipped its launch when ANY terminal was up.

    The installer leaves a credential-less terminal running while it materialises
    the MQL5 library. ``start`` then saw ``terminal_running() == True``, skipped
    the launch, and never applied the ``/config:`` file it had just written -- so
    login/password/server were silently discarded and the terminal never
    authorized. The reply even told the agent to pass credentials it had passed.
    """
    stopped: list[int] = []
    launched: list[list[str]] = []

    # ``prefer_key`` is the broker the credentials live on: cmd_start passes it so
    # a terminal for a DIFFERENT broker is not silently reused. The stub has to
    # accept it, or these tests fail on the call signature instead of on the
    # behaviour they pin.
    terminal = tmp_path / "MetaTrader 5 Terminal" / "terminal64.exe"
    monkeypatch.setattr(cli, "find_terminal", lambda prefer_key=None: terminal)
    monkeypatch.setattr(cli, "wine_bin", lambda: "wine")
    monkeypatch.setattr(cli, "wine_env", lambda: {})
    monkeypatch.setattr(cli, "MT5_ROOT", tmp_path / "mt5")
    monkeypatch.setattr(cli, "WINE_PREFIX", tmp_path / "wine")
    # A terminal is up, but it was launched WITHOUT /config:.
    #
    # Its argv names the SAME install directory ``find_terminal`` returns. That is
    # what makes it the terminal this call must RECLAIM rather than another broker's
    # interloper -- ``_stop_terminal_processes(keep_terminal=...)`` spares a process
    # whose argv points at the terminal being kept, matched on the install
    # directory's name (``MetaTrader 5 EXNESS`` / ``MetaTrader 5 Terminal``), the
    # way two coexisting branded builds are told apart.
    # ...and once the kill has happened it is GONE, which is what the stub below
    # models: ``cmd_start`` decides whether to launch by asking whether THIS
    # terminal is running (not merely whether a terminal is), so a stub that keeps
    # reporting 4242 after the stop suppresses the very relaunch being tested.
    monkeypatch.setattr(
        cli,
        "_terminal_processes",
        lambda: [] if stopped else [(4242, ["wine", str(terminal)])],
    )
    monkeypatch.setattr(cli, "_terminal_has_credentials", lambda terminal=None: False)
    def fake_stop(keep_terminal=None) -> list[int]:
        # The interlopers pass (``keep_terminal`` set) must find NOTHING to stop:
        # 4242 IS the terminal being kept, so it is spared -- exactly as
        # ``_process_is_for_terminal`` spares it on a real box. Appending here
        # unconditionally made the stub report two stops for one terminal.
        if keep_terminal is not None:
            return []
        stopped.append(4242)
        return [4242]

    monkeypatch.setattr(cli, "_stop_terminal_processes", fake_stop)
    # The server preflight is a separate behaviour (find_terminal/preflight_server);
    # stubbed here so this test fails on the credential logic it pins, not on a
    # broker resolution the fake terminal cannot answer.
    monkeypatch.setattr(cli, "preflight_server", lambda terminal, server=None: None)
    # After the kill no terminal is left, so the launch must actually happen.
    monkeypatch.setattr(cli, "terminal_running", lambda: not stopped)
    monkeypatch.setattr(cli, "_bridge_probe", lambda timeout=None: {"ok": True, "account": None})

    def fake_popen(cmd, **kwargs):
        launched.append(list(cmd))
        return types.SimpleNamespace(pid=1)

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cli.time, "time", lambda: 0.0)

    code = cli.cmd_start(
        types.SimpleNamespace(
            login=10012768157, password="Wa-xU8Ct", server="MetaQuotes-Demo", wait=0, portable=False
        )
    )
    # The stale terminal was reclaimed...
    assert stopped == [4242]
    # ...and ours was launched WITH the credentials file.
    assert launched, "start must relaunch when the running terminal has no credentials"
    assert any(arg.startswith("/config:") for arg in launched[0])
    assert "/portable" in launched[0]
    # Not ready (the fake probe reports no account), but the hint must no longer
    # tell the agent to supply credentials it already supplied.
    assert code == 2


def test_start_keeps_a_terminal_that_already_has_credentials(cli, monkeypatch, tmp_path):
    """No needless restart: a credential-carrying, connected terminal is reused."""
    launched: list[list[str]] = []
    # ``prefer_key`` is the broker the credentials live on: cmd_start passes it so
    # a terminal for a DIFFERENT broker is not silently reused. The stub has to
    # accept it, or these tests fail on the call signature instead of on the
    # behaviour they pin.
    monkeypatch.setattr(
        cli, "find_terminal", lambda prefer_key=None: tmp_path / "terminal64.exe"
    )
    monkeypatch.setattr(cli, "wine_bin", lambda: "wine")
    monkeypatch.setattr(cli, "MT5_ROOT", tmp_path / "mt5")
    monkeypatch.setattr(cli, "WINE_PREFIX", tmp_path / "wine")
    monkeypatch.setattr(cli, "_terminal_processes", lambda: [(1, ["wine", "/t/terminal64.exe"])])
    monkeypatch.setattr(cli, "_terminal_has_credentials", lambda terminal=None: True)
    monkeypatch.setattr(cli, "preflight_server", lambda terminal, server=None: None)
    monkeypatch.setattr(
        cli, "_bridge_probe",
        lambda timeout=None: {"ok": True, "account": {"login": 10012768157, "server": "MetaQuotes-Demo"}},
    )
    monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **k: launched.append(list(cmd)))
    code = cli.cmd_start(
        types.SimpleNamespace(
            login=10012768157, password="Wa-xU8Ct", server="MetaQuotes-Demo", wait=30, portable=False
        )
    )
    assert code == 0
    assert launched == []


def test_status_distinguishes_credential_less_terminal(cli):
    src = CLI_PATH.read_text(encoding="utf-8")
    assert "terminal_has_credentials" in src


def test_start_hint_no_longer_misattributes_the_failure(cli):
    """With credentials supplied, the hint must not ask for them again."""
    src = CLI_PATH.read_text(encoding="utf-8")
    assert "Credentials were written and the terminal was launched with" in src


def test_mq5_fixtures_are_present():
    fix = Path(__file__).resolve().parent / "fixtures" / "mt5"
    complex_src = (fix / "ComplexEA.mq5").read_text(encoding="utf-8")
    broken_src = (fix / "BrokenEA.mq5").read_text(encoding="utf-8")
    # The complex EA is the one that proves <angle> includes resolve.
    assert "#include <Trade/Trade.mqh>" in complex_src
    assert "OnTradeTransaction" in complex_src
    assert "IndicatorRelease" in complex_src
    # The broken one proves MetaEditor errors reach the model.
    assert "undefinedVariable" in broken_src


class _FakeMT5:
    """Just enough MT5 for cmd_split and the group close: select, info, tick, send.

    The constants are the real ones, because the request the CLI builds is part of
    what these tests assert -- a fake with invented values would let the CLI send
    a request no terminal would accept and still pass.
    """

    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009

    def __init__(self, sent, info, tick, positions=()):
        self.sent = sent
        self._info, self._tick = info, tick
        self._positions = list(positions)

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info(self, symbol):
        return self._info

    def symbol_info_tick(self, symbol):
        return self._tick

    def positions_get(self, ticket=None):
        if ticket is None:
            return list(self._positions)
        return [p for p in self._positions if int(p.ticket) == int(ticket)]


# --------------------------------------------------------------------------- #
# SPLIT TRADING -- one idea, N positions
# --------------------------------------------------------------------------- #
def test_split_divides_the_total_volume_and_gives_every_ticket_the_same_stop(cli, monkeypatch):
    """The property the method depends on: the split risks what one did.

    Ten 0.10-lot tickets with a 20-pip stop risk exactly what one 1.00-lot ticket
    with a 20-pip stop risks. The split multiplies EXITS, not risk -- so every
    ticket must carry the same sl, and the total volume must be the volume asked
    for, or the position is not the one that was planned.
    """
    sent = []

    class _Info:
        volume_min, volume_max, volume_step, digits = 0.01, 100.0, 0.01, 2
        filling_mode = 1

    class _Tick:
        bid, ask = 4285.00, 4285.18

    monkeypatch.setattr(cli, "require_bridge", lambda: (_FakeMT5(sent, _Info(), _Tick()), None))
    monkeypatch.setattr(cli, "filling_candidates", lambda mt5, info: [1])
    monkeypatch.setattr(cli, "_order_send", lambda mt5, req, fillings: (
        sent.append(dict(req)),
        {"ok": True, "retcode": 10009, "comment": "Done",
         "result": {"order": len(sent), "deal": len(sent), "price": req["price"]}},
    )[1])

    args = types.SimpleNamespace(
        symbol="XAUUSD", side="sell", volume=1.0, splits=10, group="xau-leg2",
        sl=4294.18, tp=4278.18, deviation=20, magic=20240919,
        comment="powerx-split", stop_on_failure=False, check_cost=False,
        allow_no_stop=False,
    )
    code = cli.cmd_split(args)
    assert code == 0
    assert len(sent) == 10, "one request per ticket, inside ONE bridge invocation"
    assert all(r["volume"] == 0.1 for r in sent)
    assert all(r["sl"] == 4294.18 and r["tp"] == 4278.18 for r in sent)
    # Every ticket prices off the ONE tick read before the loop, so they are the
    # same price rather than merely near each other.
    assert {r["price"] for r in sent} == {4285.00}
    assert all("xau-leg2" in r["comment"] for r in sent)


def test_split_rounds_down_rather_than_risking_more_than_asked(cli, monkeypatch):
    """0.25 over 10 is 0.025, which the broker's 0.01 step cannot take.

    Rounding UP the last ticket would make the split risk more than the caller
    asked for, which is the one error this method cannot survive. It reports the
    leftover instead.
    """
    sent = []

    class _Info:
        volume_min, volume_max, volume_step, digits = 0.01, 100.0, 0.01, 2
        filling_mode = 1

    class _Tick:
        bid, ask = 4285.00, 4285.18

    monkeypatch.setattr(cli, "require_bridge", lambda: (_FakeMT5(sent, _Info(), _Tick()), None))
    monkeypatch.setattr(cli, "filling_candidates", lambda mt5, info: [1])
    monkeypatch.setattr(cli, "_order_send", lambda mt5, req, fillings: (
        sent.append(dict(req)),
        {"ok": True, "retcode": 10009, "comment": "Done", "result": {"order": 1}},
    )[1])

    args = types.SimpleNamespace(
        symbol="XAUUSD", side="buy", volume=0.25, splits=10, group="g",
        sl=4284.00, tp=None, deviation=20, magic=20240919,
        comment="powerx-split", stop_on_failure=False, check_cost=False,
        allow_no_stop=False,
    )
    assert cli.cmd_split(args) == 0
    assert all(r["volume"] == 0.02 for r in sent)
    assert sum(r["volume"] for r in sent) == 0.20


def test_split_refuses_when_the_per_ticket_volume_is_below_the_broker_minimum(cli, monkeypatch):
    """A split that cannot be filled must say so, not quietly send nothing."""
    class _Info:
        volume_min, volume_max, volume_step, digits = 0.10, 100.0, 0.01, 2
        filling_mode = 1

    class _Tick:
        bid, ask = 4285.00, 4285.18

    monkeypatch.setattr(cli, "require_bridge", lambda: (_FakeMT5([], _Info(), _Tick()), None))
    args = types.SimpleNamespace(
        symbol="XAUUSD", side="buy", volume=0.5, splits=10, group="g",
        sl=4284.00, tp=None, deviation=20, magic=20240919,
        comment="c", stop_on_failure=False, check_cost=False,
        allow_no_stop=False,
    )
    assert cli.cmd_split(args) == 1


def test_a_group_tag_matches_its_own_group_and_not_a_longer_name(cli):
    """`a` must not select the tickets of `abc`; the colons make it a field."""

    class _Pos:
        def __init__(self, ticket, comment):
            self.ticket, self.comment, self.volume = ticket, comment, 0.1

    class _MT5:
        @staticmethod
        def positions_get():
            return [
                _Pos(1, "powerx-split:abc:1of10"),
                _Pos(2, "powerx-split:a:1of10"),
                _Pos(3, "powerx-split:a:2of10"),
                _Pos(4, "powerx-split:a:1of10"),
                _Pos(5, "unrelated"),
            ]

    matched = cli._positions_in_group(_MT5(), "a")
    assert [p.ticket for p in matched] == [2, 3, 4]


def test_the_ledger_makes_consecutive_watches_one_observation(cli, tmp_path, monkeypatch):
    """The whole point of --session: the second call knows what the first saw."""
    monkeypatch.setattr(cli, "MT5_ROOT", tmp_path)
    path = cli._watch_session_path("xau-leg-2")
    assert path is not None

    def _fold(track, observed=(), samples=3, seconds=10.0):
        return cli._watch_session_fold(
            path, "xau-leg-2", ["XAUUSD"], track, {"XAUUSD": 0.10},
            list(observed), seconds, samples, {11},
        )

    first = _fold({"XAUUSD": {"samples": 3, "first_mid": 4285.0, "last_mid": 4286.0,
                              "min_mid": 4285.0, "max_mid": 4286.0}})
    assert first["calls"] == 1
    assert first["price_path_total"]["XAUUSD"]["first_mid"] == 4285.0

    # A second call that only saw the high end of the move must still report the
    # TRUE high and low of the whole session, not just its own window.
    second = _fold({"XAUUSD": {"samples": 2, "first_mid": 4290.0, "last_mid": 4284.0,
                               "min_mid": 4284.0, "max_mid": 4290.0}}, samples=2)
    total = second["price_path_total"]["XAUUSD"]
    assert second["calls"] == 2
    assert total["first_mid"] == 4285.0, "the session's first price, not this call's"
    assert total["last_mid"] == 4284.0
    assert total["min_mid"] == 4284.0 and total["max_mid"] == 4290.0
    assert total["samples"] == 5, "samples accumulate across calls"
    assert second["watch_seconds_total"] == 20.0
    assert second["tickets_at_start"] == [11]
    # "moved since you last looked" is a different question from "moved in this
    # call", and only the ledger can answer it.
    assert second["since_last_call"]["XAUUSD"]["was"] == 4286.0
    assert second["since_last_call"]["XAUUSD"]["now"] == 4284.0
    assert second["since_last_call"]["XAUUSD"]["moved_pips"] == -20.0


def test_a_session_name_that_cannot_be_a_filename_is_refused_not_sanitised_away(cli, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "MT5_ROOT", tmp_path)
    assert cli._watch_session_path("xau/leg:2") == tmp_path / "watch_sessions" / "xauleg2.json"
    assert cli._watch_session_path("////") is None
    assert cli._watch_session_path("") is None


def test_the_ledger_keeps_the_events_that_happened_between_calls(cli, tmp_path, monkeypatch):
    """Events are the reason to keep a session: one call's fire is the next
    call's context, and dropping it would make each call start from nothing."""
    monkeypatch.setattr(cli, "MT5_ROOT", tmp_path)
    path = cli._watch_session_path("s")
    track = {"XAUUSD": {"samples": 1, "first_mid": 1.0, "last_mid": 1.0,
                        "min_mid": 1.0, "max_mid": 1.0}}
    cli._watch_session_fold(path, "s", ["XAUUSD"], track, {"XAUUSD": 0.1},
                            [{"event": "rule_fired"}], 5.0, 1, set())
    result = cli._watch_session_fold(path, "s", ["XAUUSD"], track, {"XAUUSD": 0.1},
                                     [{"event": "position_closed"}], 5.0, 1, set())
    assert result["event_count"] == 2
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert [e["event"] for e in stored["events"]] == ["rule_fired", "position_closed"]


def test_split_reports_what_the_account_actually_got_not_what_it_asked_for(cli, monkeypatch):
    """A split is NEAR one price, never exactly one.

    MEASURED 2026-09-24 on a live Deriv-Demo terminal: ten XAUUSD tickets
    requested at one price filled across 4284.06..4284.25, because a market order
    fills at whatever the other side is when it lands and ten of them land at ten
    moments. Reporting only the requested price makes the method look better than
    it is, and understates the risk on the tickets that filled worst.
    """
    sent = []

    class _Info:
        volume_min, volume_max, volume_step, digits = 0.01, 100.0, 0.01, 2
        filling_mode = 1

    class _Tick:
        bid, ask = 4284.15, 4284.33

    fills = [4284.06, 4284.11, 4284.15, 4284.22, 4284.25] * 2

    def _fake_send(mt5, req, fillings):
        sent.append(dict(req))
        executed = fills[len(sent) - 1]
        return {"ok": True, "retcode": 10009, "comment": "Request executed",
                "order": len(sent), "deal": len(sent), "price": executed}

    monkeypatch.setattr(cli, "require_bridge", lambda: (_FakeMT5(sent, _Info(), _Tick()), None))
    monkeypatch.setattr(cli, "filling_candidates", lambda mt5, info: [1])
    monkeypatch.setattr(cli, "_order_send", _fake_send)

    args = types.SimpleNamespace(
        symbol="XAUUSD", side="sell", volume=0.10, splits=10, group="probe1",
        sl=4330.00, tp=4240.00, deviation=20, magic=20240919,
        comment="probe", stop_on_failure=False, check_cost=False,
    )
    out = {}
    monkeypatch.setattr(cli, "emit", lambda payload, text=None, code=0: (out.update(payload), 0)[1])
    assert cli.cmd_split(args) == 0

    assert out["price"] == 4284.15, "the requested price is still reported"
    assert out["fill_price_first"] == 4284.06 and out["fill_price_last"] == 4284.25
    # 0.19 of price at a 0.10 pip is 1.9 pips -- on a 20-pip stop that is a tenth
    # of the trade's risk, purely from the tickets not being simultaneous.
    assert out["fill_dispersion_pips"] == 1.9
    assert "dispersion" in out["note"]
    # Risk is summed off each ticket's OWN fill -- the distance to the stop differs
    # per ticket by exactly the dispersion, so the request-price figure is a
    # different number and the wrong one.
    per_pip = 0.01 * 100.0 * 0.10
    assert out["total_risk_money"] == round(
        sum(abs(f - 4330.00) / 0.10 * per_pip for f in fills), 2
    )
    from_request_price = abs(4284.15 - 4330.00) / 0.10 * per_pip * 10
    assert out["total_risk_money"] != round(from_request_price, 2)


def test_trade_state_gives_the_loop_the_arithmetic_to_decide_on(cli):
    """The polling loop must be able to reason without redoing the sums.

    R is a ratio of PRICE distances, so one formula has to be right for Gold
    (pip 0.10, contract 100) and for EURUSD (pip 0.00001, contract 100000) at
    once. Getting this wrong at 3 a.m. on call forty is how money is lost.
    """
    import collections

    Pos = collections.namedtuple(
        "Pos", "ticket symbol type volume price_open sl tp profit comment"
    )
    # A Gold buy at +2R: entry 4200, stop 4180 (20 pips), now 4240.
    gold = Pos(1, "XAUUSD", 0, 0.10, 4200.0, 4180.0, 4400.0, 200.0, "g")
    # A EURUSD sell at -0.5R: entry 1.10000, stop 1.10200 (20 pips), now 1.10100.
    fx = Pos(2, "EURUSD", 1, 0.50, 1.10000, 1.10200, 1.09600, -50.0, "f")
    pips = {"XAUUSD": 0.10, "EURUSD": 0.00001}
    prices = {"XAUUSD": {"mid": 4240.0}, "EURUSD": {"mid": 1.10100}}
    contracts = {"XAUUSD": 100.0, "EURUSD": 100000.0}

    out = cli._analyse_trade([gold, fx], pips, prices, contracts, 10000.0)
    rows = {r["ticket"]: r for r in out["positions"]}

    assert rows[1]["r_multiple"] == 2.0
    assert rows[2]["r_multiple"] == -0.5
    # Distances use each symbol's OWN pip, so a 60-dollar Gold move and a
    # 10-pip FX move are measured against the right unit on each.
    assert rows[1]["pips_to_sl"] == 600.0   # 4240 -> 4180 at pip 0.10
    assert rows[2]["pips_to_sl"] == 100.0   # 1.10100 -> 1.10200 at pip 0.00001
    # Risk is measured at ENTRY, from the stop, not from the current price:
    # 0.10 lots of Gold, 20 dollars offside = 10 oz x $20 = $200.
    assert rows[1]["risk_money"] == 200.0
    # 0.50 lots of EURUSD, 200 pips offside = 50,000 x 0.00200 = $100.
    assert rows[2]["risk_money"] == 100.0
    assert rows[1]["breakeven_price"] == 4200.0

    # Gold at +2R has paid for its risk and is still carrying it -- the single
    # most actionable state in the whole payload.
    assert rows[1]["breakeven_due"] is True
    assert not rows[2].get("breakeven_due")
    assert out["totals"]["profit_money"] == 150.0
    assert out["totals"]["risk_money"] == 300.0
    assert out["totals"]["risk_pct_of_equity"] == 3.0
    assert out["totals"]["best_r"] == 2.0 and out["totals"]["worst_r"] == -0.5
    assert any("breakeven" in n or "free" in n for n in out["notes"])


def test_a_position_with_no_stop_is_an_alert_not_a_silent_zero(cli):
    """No stop is unbounded risk, and it must be impossible to miss."""
    import collections

    Pos = collections.namedtuple(
        "Pos", "ticket symbol type volume price_open sl tp profit comment"
    )
    naked = Pos(9, "XAUUSD", 0, 1.0, 4200.0, 0.0, 0.0, -30.0, "")
    out = cli._analyse_trade(
        [naked], {"XAUUSD": 0.10}, {"XAUUSD": {"mid": 4190.0}}, {"XAUUSD": 100.0}, 10000.0
    )
    assert out["positions"][0]["alert"] == "no_stop"
    assert out["positions"][0]["r_multiple"] is None
    assert any("NO STOP" in n for n in out["notes"])


def test_the_ledger_remembers_the_best_and_worst_the_trade_ever_was(cli, tmp_path, monkeypatch):
    """A trade that was +3R an hour ago and is flat now is a different decision
    from one that has never been in profit, and only the ledger saw the first."""
    monkeypatch.setattr(cli, "MT5_ROOT", tmp_path)
    path = cli._watch_session_path("t")
    track = {"XAUUSD": {"samples": 1, "first_mid": 1.0, "last_mid": 1.0,
                        "min_mid": 1.0, "max_mid": 1.0}}
    cli._watch_session_fold(path, "t", ["XAUUSD"], track, {"XAUUSD": 0.1}, [], 5.0, 1, set(),
                            {"totals": {"positions": 1, "profit_money": 300.0,
                                        "best_r": 3.0, "worst_r": 0.5}})
    cli._watch_session_fold(path, "t", ["XAUUSD"], track, {"XAUUSD": 0.1}, [], 5.0, 1, set(),
                            {"totals": {"positions": 1, "profit_money": 0.0,
                                        "best_r": -0.4, "worst_r": -0.4}})
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["trade_path"]["best_profit_money"] == 300.0
    assert stored["trade_path"]["worst_profit_money"] == 0.0
    assert stored["trade_path"]["best_r"] == 3.0
    assert stored["trade_path"]["worst_r"] == -0.4


def test_a_netting_account_refuses_a_split_instead_of_netting_it(cli, monkeypatch):
    """On netting, N tickets net into ONE position at a blended price.

    That is not a split: it is an oversized single trade on a stop that now
    covers every ticket at once, which is the opposite of granular exits. It has
    to be refused BEFORE anything is sent, not reported after.
    """
    sent = []

    class _Info:
        volume_min, volume_max, volume_step, digits = 0.01, 100.0, 0.01, 2
        filling_mode = 1

    class _Tick:
        bid, ask = 4285.00, 4285.18

    class _Netting(_FakeMT5):
        def account_info(self):
            return types.SimpleNamespace(equity=10000.0, margin_free=9000.0, margin_mode=0)

    monkeypatch.setattr(cli, "require_bridge",
                        lambda: (_Netting(sent, _Info(), _Tick()), None))
    monkeypatch.setattr(cli, "_order_send",
                        lambda mt5, req, fillings: {"ok": True, "retcode": 10009})
    args = types.SimpleNamespace(
        symbol="XAUUSD", side="buy", volume=0.10, splits=10, group="g",
        sl=4284.00, tp=None, deviation=20, magic=1, comment="c",
        stop_on_failure=False, check_cost=False,
        allow_no_stop=False,
    )
    err = {}
    monkeypatch.setattr(cli, "emit", lambda payload, text=None, code=0: (err.update(payload, _code=code), code)[1])
    assert cli.cmd_split(args) == 1
    assert sent == [], "nothing may be sent to a netting account"
    assert "NETTING" in err.get("error", "")
    assert "multiply size, not exits" in err.get("error", "")


def test_a_hedging_account_is_told_it_is_hedging(cli, monkeypatch):
    class _Info:
        volume_min, volume_max, volume_step, digits = 0.01, 100.0, 0.01, 2
        filling_mode = 1

    class _Tick:
        bid, ask = 4285.00, 4285.18

    class _Hedging(_FakeMT5):
        def account_info(self):
            return types.SimpleNamespace(equity=10000.0, margin_free=9000.0, margin_mode=2)

        def order_calc_margin(self, action, symbol, volume, price):
            return volume * 100.0 * price / 1000.0

    sent = []
    monkeypatch.setattr(cli, "require_bridge",
                        lambda: (_Hedging(sent, _Info(), _Tick()), None))
    monkeypatch.setattr(cli, "filling_candidates", lambda mt5, info: [1])
    monkeypatch.setattr(cli, "_order_send", lambda mt5, req, fillings: (
        sent.append(dict(req)), {"ok": True, "retcode": 10009, "price": req["price"]},
    )[1])
    out = {}
    monkeypatch.setattr(cli, "emit", lambda payload, text=None, code=0: (out.update(payload), 0)[1])
    args = types.SimpleNamespace(
        symbol="XAUUSD", side="buy", volume=0.10, splits=10, group="g",
        sl=4284.00, tp=None, deviation=20, magic=1, comment="c",
        stop_on_failure=False, check_cost=False,
        allow_no_stop=False,
    )
    assert cli.cmd_split(args) == 0
    assert out["account_margin_mode"]["hedging"] is True
    assert out["account_margin_mode"]["netting"] is False
    assert len(sent) == 10
    # 0.01 lots of Gold is 0.01 x 100 x 4285 / 1000 = 4.285 a ticket, x10.
    assert out["margin_required"] == 42.85


def test_a_split_that_cannot_be_margined_is_refused_before_anything_is_sent(cli, monkeypatch):
    """Half-filling on margin leaves a stop covering fewer tickets than planned.

    Ten tickets are ten margin reservations, so the failure is not a clean
    refusal -- the first few fill and the rest come back 10019. Being told in
    advance is strictly better than that, so the broker's own margin figure is
    consulted before the first ticket is sent.
    """
    sent = []

    class _Info:
        volume_min, volume_max, volume_step, digits = 0.01, 100.0, 0.01, 2
        filling_mode = 1

    class _Tick:
        bid, ask = 4285.00, 4285.18

    class _Tight(_FakeMT5):
        def account_info(self):
            # Enough free margin for about 5 of the 10 tickets.
            return types.SimpleNamespace(equity=1000.0, margin_free=21.0, margin_mode=2)

        def order_calc_margin(self, action, symbol, volume, price):
            return volume * 100.0 * price / 1000.0

    monkeypatch.setattr(cli, "require_bridge", lambda: (_Tight(sent, _Info(), _Tick()), None))
    args = types.SimpleNamespace(
        symbol="XAUUSD", side="buy", volume=0.10, splits=10, group="g",
        sl=4284.00, tp=None, deviation=20, magic=1, comment="c",
        stop_on_failure=False, check_cost=False,
        allow_no_stop=False,
    )
    err = {}
    monkeypatch.setattr(cli, "emit", lambda payload, text=None, code=0: (err.update(payload), code)[1])
    assert cli.cmd_split(args) == 1
    assert sent == [], "a split that cannot be margined must send nothing at all"
    assert "half-fill" in err.get("error", "")
    # And it says what WOULD fit, so the caller can adjust rather than guess.
    assert "--splits 4" in err.get("error", "")


def test_margin_mode_is_reported_rather_than_left_as_a_number(cli):
    class _Acct:
        def __init__(self, mode):
            self.margin_mode = mode

    class _MT5:
        def __init__(self, mode):
            self._mode = mode

        def account_info(self):
            return _Acct(self._mode)

    assert cli._margin_mode_of(_MT5(2)) == {
        "margin_mode": 2, "margin_mode_name": "hedging",
        "hedging": True, "netting": False, "multiple_positions_ok": True,
    }
    net = cli._margin_mode_of(_MT5(0))
    assert net["netting"] is True and net["multiple_positions_ok"] is False
    # An unreadable mode is UNKNOWN and must never be treated as permissive.
    unknown = cli._margin_mode_of(object())
    assert unknown["margin_mode"] is None
    assert unknown["multiple_positions_ok"] is False
    assert unknown["hedging"] is False


# --------------------------------------------------------------------------- #
# ENTERING AT A PRICE, AND SIZING BY MONEY AT RISK
# --------------------------------------------------------------------------- #
# Two instructions a trader gives all the time and the tool could not serve:
#
#   * "buy the dip at 4270" / "buy the breakout above 4300" -- a price that is
#     NOT the market, so a market order fills at a price the caller never asked
#     for and the difference is the whole trade.
#   * "risk $100 on this" -- money, not lots. The lots are arithmetic over the
#     stop distance, the pip and the contract size, and doing it by hand is the
#     error that costs money.
#
# Everything here is a pure function or a faked terminal, so no Wine is needed.


class _SymMT5:
    """A terminal that knows one symbol's contract and no more."""

    def __init__(self, info):
        self._info = info

    def symbol_info(self, symbol):
        return self._info


def _sym(digits, contract, step=0.01, minimum=0.01, maximum=100.0):
    return types.SimpleNamespace(
        digits=digits, trade_contract_size=contract, volume_step=step,
        volume_min=minimum, volume_max=maximum, filling_mode=1,
    )


def test_risk_sizing_turns_money_into_lots_on_gold(cli):
    """Gold's pip is 0.10 and its contract is 100 oz, so a 20-pip stop is $2.

    $20 of risk over a 20-pip stop is therefore 0.10 lots, not 0.02 and not 1.00.
    The published pip (0.10) is the whole reason this cannot be derived from the
    two-digit quote.
    """
    info = _sym(2, 100.0)
    volume, detail, err = cli._volume_for_risk(
        _SymMT5(info), "XAUUSD", info, 4285.18, 4283.18, 20.0
    )
    assert err is None
    assert volume == 0.10
    assert detail["pip"] == 0.10
    assert detail["stop_pips"] == 20.0
    assert detail["money_per_pip_per_lot"] == 10.0
    assert detail["requested_risk_money"] == 20.0


def test_risk_sizing_works_on_a_five_digit_fx_pair(cli):
    """The same arithmetic on a different pip and contract must agree in money."""
    info = _sym(5, 100000.0)
    volume, detail, err = cli._volume_for_risk(
        _SymMT5(info), "EURUSD", info, 1.10000, 1.09980, 20.0
    )
    assert err is None
    assert volume == 1.0
    assert detail["stop_pips"] == 20.0
    # 1.00 lot of EURUSD is $100,000, so 0.0002 of movement IS the $20 asked for.
    assert round(detail["stop_pips"] * detail["money_per_pip_per_lot"] * volume, 2) == 20.0


def test_risk_sizing_rounds_down_so_it_never_risks_more_than_asked(cli):
    """$25 over a 20-pip Gold stop is 0.125 lots, which is not expressible.

    0.13 would risk $26 -- more than the caller asked for, on the one number the
    caller actually chose. It must round DOWN and say so.
    """
    info = _sym(2, 100.0)
    volume, detail, err = cli._volume_for_risk(
        _SymMT5(info), "XAUUSD", info, 4285.18, 4283.18, 25.0
    )
    assert err is None
    assert detail["volume_unrounded"] == 0.125
    assert volume == 0.12
    assert volume * 20.0 * 10.0 <= 25.0


def test_risk_sizing_below_the_broker_minimum_says_what_would_fit(cli):
    """A risk the symbol cannot express is a refusal that names the floor.

    "$0.10 on a 20-pip Gold stop" needs 0.0005 lots, and the smallest Gold trade
    is 0.01 -- which risks $2.00. Telling the caller that number is the only
    useful answer; sending the minimum instead would risk 20x the ask.
    """
    info = _sym(2, 100.0)
    volume, detail, err = cli._volume_for_risk(
        _SymMT5(info), "XAUUSD", info, 4285.18, 4283.18, 0.10
    )
    assert volume is None
    assert err is not None
    assert "below this symbol's minimum" in err
    assert "2.0" in err
    assert detail["volume"] == 0.0


def test_risk_sizing_refuses_a_stop_that_is_not_a_stop(cli):
    """Without a stop, or with a zero-distance one, the risk is undefined."""
    info = _sym(2, 100.0)
    volume, _detail, err = cli._volume_for_risk(
        _SymMT5(info), "XAUUSD", info, 4285.18, 4285.18, 20.0
    )
    assert volume is None and err is not None


@pytest.mark.parametrize(
    "side,entry_type,price,ok",
    [
        ("buy", "limit", 4270.0, True),    # below the ask: a dip
        ("buy", "limit", 4290.0, False),   # above the ask: that is a buy STOP
        ("sell", "limit", 4300.0, True),   # above the bid: a fade
        ("sell", "limit", 4280.0, False),  # below the bid: that is a sell STOP
        ("buy", "stop", 4300.0, True),     # through the ask: a breakout
        ("buy", "stop", 4280.0, False),    # below the ask: that is a buy LIMIT
        ("sell", "stop", 4270.0, True),    # through the bid
        ("sell", "stop", 4290.0, False),   # above the bid: that is a sell LIMIT
    ],
)
def test_a_pending_price_on_the_wrong_side_is_caught_before_the_round_trip(
    cli, side, entry_type, price, ok
):
    """retcode 10015 reads like a bad number. It is a limit on the wrong side.

    The server rejects both wrong-side cases with "invalid price", which names
    neither the side nor the fix, so the order looks like a formatting bug
    instead of an order that cannot work.
    """
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    error = cli._validate_pending_price(side, entry_type, price, tick)
    assert (error is None) is ok
    if error:
        # The refusal must NAME the correct style, not just refuse.
        assert "LIMIT" in error or "STOP" in error


def test_a_market_entry_is_never_validated_as_a_pending_one(cli):
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    assert cli._validate_pending_price("buy", "market", 4290.0, tick) is None


class _OrderMT5:
    """A terminal with the order constants and one symbol, for cmd_order.

    It also answers ``positions_get``: every order now passes through the
    account's risk gate, which reads the open book before it agrees to send
    anything, so a terminal that cannot be asked what is open cannot place an
    order.
    """

    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_REMOVE = 8
    ORDER_TIME_GTC = 0
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009

    def __init__(self, info, tick, equity=10000.0, orders=(), positions=()):
        self._info, self._tick, self._equity = info, tick, equity
        self._orders = list(orders)
        self._positions = list(positions)

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info(self, symbol):
        return self._info

    def symbol_info_tick(self, symbol):
        return self._tick

    def account_info(self):
        return types.SimpleNamespace(equity=self._equity, balance=self._equity)

    def orders_get(self):
        return list(self._orders)

    def positions_get(self, ticket=None):
        return list(self._positions)

    def symbols_get(self):
        return []

    def history_deals_get(self, start, end):
        return []


def _order_args(**over):
    base = dict(
        symbol="XAUUSD", side="buy", volume=None, sl=None, tp=None, entry_type="market",
        price=None, risk_money=None, risk_pct=None, deviation=20, magic=1, comment="c",
        allow_no_stop=False,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _wire_order(cli, monkeypatch, mt5):
    sent = []

    def _send(m, req, fillings):
        sent.append(dict(req))
        return {
            "ok": True, "retcode": 10009, "comment": "Request executed",
            "order": 77, "deal": 88, "price": req["price"],
        }

    out: dict = {}
    monkeypatch.setattr(cli, "require_bridge", lambda: (mt5, None))
    monkeypatch.setattr(cli, "filling_candidates", lambda m, info: [2])
    monkeypatch.setattr(cli, "_order_send", _send)
    monkeypatch.setattr(
        cli, "emit", lambda payload, text=None, code=0: (out.update(payload), code)[1]
    )
    return sent, out


def test_order_sized_in_money_sends_the_lots_that_money_implies(cli, monkeypatch):
    """"Risk $100 on this" must reach the broker as a lot size, not as a wish.

    Live Gold tick: ask 4285.18, stop 4283.18 -- 20 pips. $20 of risk is 0.10
    lots, and the order that reaches the terminal must already carry it.
    """
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick))
    args = _order_args(sl=4283.18, risk_money=20.0)

    assert cli.cmd_order(args) == 0
    assert len(sent) == 1
    assert sent[0]["action"] == _OrderMT5.TRADE_ACTION_DEAL
    assert sent[0]["type"] == _OrderMT5.ORDER_TYPE_BUY
    assert sent[0]["volume"] == 0.10
    assert sent[0]["price"] == 4285.18
    assert sent[0]["sl"] == 4283.18
    assert out["volume"] == 0.10
    assert out["sizing"]["stop_pips"] == 20.0
    # $20 of a $10,000 account, reported so the caller can see the size it chose.
    assert out["risk_pct_of_equity"] == 0.2
    assert out["filled"] is True


def test_a_limit_order_rests_at_the_price_instead_of_filling_now(cli, monkeypatch):
    """The whole point: an order at a price that is not the market."""
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick))
    args = _order_args(volume=0.10, entry_type="limit", price=4270.0, sl=4268.0)

    assert cli.cmd_order(args) == 0
    assert sent[0]["action"] == _OrderMT5.TRADE_ACTION_PENDING
    assert sent[0]["type"] == _OrderMT5.ORDER_TYPE_BUY_LIMIT
    assert sent[0]["price"] == 4270.0
    assert sent[0]["sl"] == 4268.0
    assert out["pending"] is True
    assert out["order_ticket"] == 77
    # It holds NO position, and the report must not read as though it did.
    assert "filled" not in out
    assert "NO position" in out["note"]


def test_a_sell_stop_rests_below_the_bid_as_a_sell_stop(cli, monkeypatch):
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick))
    args = _order_args(side="sell", volume=0.10, entry_type="stop", price=4270.0,
                       sl=4272.0)

    assert cli.cmd_order(args) == 0
    assert sent[0]["type"] == _OrderMT5.ORDER_TYPE_SELL_STOP
    assert sent[0]["action"] == _OrderMT5.TRADE_ACTION_PENDING
    assert out["pending"] is True


def test_a_wrong_side_pending_order_never_reaches_the_broker(cli, monkeypatch):
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick))
    args = _order_args(volume=0.10, entry_type="limit", price=4290.0, sl=4288.0)

    code = cli.cmd_order(args)
    assert sent == [], "an order that cannot work must not be sent"
    errs = out
    assert code, "a refused order is a failure, not a silent no-op"
    assert "BUY STOP" in str(errs.get("error", ""))


def test_a_market_order_refuses_a_price_it_cannot_honour(cli, monkeypatch):
    """Silently ignoring 'price' would fill at a price the caller did not ask for."""
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick))
    assert cli.cmd_order(_order_args(volume=0.1, price=4290.0, sl=4283.18)) == 1
    assert sent == []
    assert "market" in str(out.get("error", ""))


def test_risk_sizing_needs_a_stop_and_only_one_size(cli, monkeypatch):
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick))

    assert cli.cmd_order(_order_args(risk_money=20.0)) == 1
    assert "sl" in str(out.get("error", ""))
    assert sent == []

    out.clear()
    assert cli.cmd_order(_order_args(volume=0.1, risk_money=20.0, sl=4283.18)) == 1
    assert "not both" in str(out.get("error", ""))
    assert sent == []

    out.clear()
    assert cli.cmd_order(_order_args(risk_money=20.0, risk_pct=1.0, sl=4283.18)) == 1
    assert "not both" in str(out.get("error", ""))
    assert sent == []


def test_a_stop_on_the_winning_side_is_refused_rather_than_priced(cli, monkeypatch):
    """A buy whose 'stop' is above the entry is a target, and |entry-sl| would
    give a plausible lot size for a trade that cannot exist."""
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick))
    assert cli.cmd_order(_order_args(sl=4290.0, risk_money=20.0)) == 1
    assert "target, not a stop" in str(out.get("error", ""))
    assert sent == []


def test_order_without_any_size_is_refused(cli, monkeypatch):
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick))
    assert cli.cmd_order(_order_args(sl=4283.18)) == 1
    assert "size" in str(out.get("error", ""))
    assert sent == []


def test_risk_pct_sizes_against_equity_not_balance(cli, monkeypatch):
    """Equity is what the account is worth right now, which is what risk is a
    percentage OF. Sizing off balance would over-risk a losing account."""
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick, equity=10000.0))
    assert cli.cmd_order(_order_args(sl=4283.18, risk_pct=0.2)) == 0
    # 0.2% of 10,000 is $20, which is 0.10 lots over a 20-pip Gold stop.
    assert sent[0]["volume"] == 0.10
    assert out["risk_pct_of_equity"] == 0.2


# --------------------------------------------------------------------------- #
# CANCEL -- removing a resting order, which holds no position
# --------------------------------------------------------------------------- #
def _wire_cancel(cli, monkeypatch, mt5, ok_for=()):
    sent = []

    def _send(m, req, fillings):
        sent.append(dict(req))
        ok = (not ok_for) or req["order"] in ok_for
        return {"ok": ok, "retcode": 10009 if ok else 10013,
                "comment": "Done" if ok else "Invalid request"}

    out: dict = {}
    monkeypatch.setattr(cli, "require_bridge", lambda: (mt5, None))
    monkeypatch.setattr(cli, "_order_send", _send)
    monkeypatch.setattr(
        cli, "emit", lambda payload, text=None, code=0: (out.update(payload), code)[1]
    )
    return sent, out


def test_cancel_removes_one_named_pending_order(cli, monkeypatch):
    class _O:
        def __init__(self, t):
            self.ticket = t

    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    mt5 = _OrderMT5(info, tick, orders=[_O(11), _O(12)])
    sent, out = _wire_cancel(cli, monkeypatch, mt5)

    assert cli.cmd_cancel(types.SimpleNamespace(ticket=12, all=False)) == 0
    assert sent == [{"action": _OrderMT5.TRADE_ACTION_REMOVE, "order": 12}]
    assert out["cancelled"] == 1 and out["requested"] == 1
    assert "Nothing was closed" in out["note"]


def test_cancel_all_sweeps_every_resting_order(cli, monkeypatch):
    class _O:
        def __init__(self, t):
            self.ticket = t

    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    mt5 = _OrderMT5(info, tick, orders=[_O(11), _O(12)])
    sent, out = _wire_cancel(cli, monkeypatch, mt5)

    assert cli.cmd_cancel(types.SimpleNamespace(ticket=None, all=True)) == 0
    assert [r["order"] for r in sent] == [11, 12]
    assert out["cancelled"] == 2


def test_cancel_all_with_nothing_resting_is_a_success_not_an_error(cli, monkeypatch):
    """"There was nothing to cancel" is the desired end state, not a failure."""
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    mt5 = _OrderMT5(info, tick, orders=[])
    sent, out = _wire_cancel(cli, monkeypatch, mt5)

    assert cli.cmd_cancel(types.SimpleNamespace(ticket=None, all=True)) == 0
    assert sent == []
    assert out["cancelled"] == 0
    assert "no pending orders" in out["note"]


def test_cancel_without_a_target_is_refused(cli, monkeypatch):
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_cancel(cli, monkeypatch, _OrderMT5(info, tick))
    assert cli.cmd_cancel(types.SimpleNamespace(ticket=None, all=False)) == 1
    assert sent == []
    assert "all=true" in str(out.get("error", ""))


def test_a_partial_cancel_is_an_alert_never_a_success(cli, monkeypatch):
    """An order still resting in the market must never be reported as removed."""
    class _O:
        def __init__(self, t):
            self.ticket = t

    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    mt5 = _OrderMT5(info, tick, orders=[_O(11), _O(12)])
    sent, out = _wire_cancel(cli, monkeypatch, mt5, ok_for={11})

    code = cli.cmd_cancel(types.SimpleNamespace(ticket=None, all=True))
    assert out["cancelled"] == 1 and out["requested"] == 2
    assert out["alert"] == "cancel_incomplete"
    assert code, "an incomplete sweep is not a clean result"


def test_a_stop_on_the_wrong_side_is_caught_even_with_an_explicit_volume(cli, monkeypatch):
    """MEASURED live 2026-09-24 on Deriv-Demo: a buy limit resting at 4282.05
    carrying an sl of 4285.05 came back as retcode 10016 "Invalid stops".

    "Invalid stops" names neither the leg, the direction, nor the entry the leg
    should have been measured against, so it reads like a broker fault. The same
    numbers are refused here with the sentence that fixes them.
    """
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick))

    assert cli.cmd_order(_order_args(volume=0.01, sl=4290.0)) == 1
    assert sent == []
    assert "target, not a stop" in str(out.get("error", ""))

    # A pending entry is measured against ITS price, not the market: the same sl
    # that is wrong for the market is also wrong for a limit resting below it.
    out.clear()
    assert cli.cmd_order(
        _order_args(volume=0.01, entry_type="limit", price=4282.05, sl=4285.05)
    ) == 1
    assert sent == []
    assert "target, not a stop" in str(out.get("error", ""))

    # Below the pending entry is a real stop, and it must go through untouched.
    out.clear()
    assert cli.cmd_order(
        _order_args(volume=0.01, entry_type="limit", price=4282.05, sl=4280.05)
    ) == 0
    assert sent[0]["sl"] == 4280.05
    assert sent[0]["action"] == _OrderMT5.TRADE_ACTION_PENDING


def test_a_target_on_the_wrong_side_is_refused_too(cli, monkeypatch):
    """The mirror of the stop check: a buy's target belongs ABOVE the entry."""
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick))

    assert cli.cmd_order(_order_args(volume=0.01, tp=4280.0, sl=4283.18)) == 1
    assert "target" in str(out.get("error", ""))
    assert sent == []

    assert cli.cmd_order(_order_args(volume=0.01, tp=4302.18, sl=4283.18)) == 0
    assert sent[0]["tp"] == 4302.18


def test_the_report_names_the_money_at_risk_at_the_price_paid(cli, monkeypatch):
    """A market order fills at whatever the other side is when it lands.

    MEASURED live 2026-09-24: the XAUUSD ask moved 0.18 between the quote and the
    fill, which turned a 20.0-pip stop into 21.8 pips -- so the risk that was
    sized is not exactly the risk that was taken. Reporting BOTH is what makes
    the size checkable instead of merely plausible.
    """
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent = []

    def _send(m, req, fillings):
        sent.append(dict(req))
        return {"ok": True, "retcode": 10009, "comment": "Request executed",
                "order": 77, "deal": 88, "price": 4285.36}  # 0.18 worse

    out: dict = {}
    monkeypatch.setattr(cli, "require_bridge", lambda: (_OrderMT5(info, tick), None))
    monkeypatch.setattr(cli, "filling_candidates", lambda m, i: [2])
    monkeypatch.setattr(cli, "_order_send", _send)
    monkeypatch.setattr(
        cli, "emit", lambda payload, text=None, code=0: (out.update(payload), code)[1]
    )

    assert cli.cmd_order(_order_args(sl=4283.18, risk_money=20.0)) == 0
    assert sent[0]["volume"] == 0.10
    assert out["sizing"]["stop_pips"] == 20.0
    assert out["sizing"]["fill_stop_pips"] == 21.8
    # 0.10 lots of Gold is $1 a pip, so 21.8 pips is $21.80 of real risk.
    assert out["sizing"]["actual_risk_money"] == 21.8
    assert out["risk_pct_of_equity"] == 0.22


# --------------------------------------------------------------------------- #
# NOTHING OPENS WITHOUT A SERVER-SIDE EXIT
# --------------------------------------------------------------------------- #
# A position with no stop has NO exit on the broker: MetaQuotes holds nothing,
# so the only thing that can close it is something looking at the price -- and
# nothing looks between calls. "I will watch it" expires; a stop does not. So an
# unprotected trade is allowed, but only when it is ASKED for.


def test_nothing_opens_without_a_stop_unless_that_was_asked_for(cli, monkeypatch):
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick))

    assert cli.cmd_order(_order_args(volume=0.10)) == 1
    assert sent == [], "an order with no stop must not reach the broker by default"
    assert "no stop" in str(out.get("error", ""))
    assert "allow_no_stop" in str(out.get("error", ""))

    # Asked for explicitly, it goes through -- and says what it is.
    out.clear()
    assert cli.cmd_order(_order_args(volume=0.10, allow_no_stop=True)) == 0
    assert len(sent) == 1
    assert "sl" not in sent[0]
    assert out["alert"] == "opened_without_a_stop"
    assert "no stop" in out["warning"]


def test_a_pending_order_without_a_stop_is_refused_too(cli, monkeypatch):
    """A resting order is not "safe because it has not filled": when it does
    fill it opens the same naked position."""
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick))
    assert cli.cmd_order(
        _order_args(volume=0.01, entry_type="limit", price=4270.0)
    ) == 1
    assert sent == []
    assert "no stop" in str(out.get("error", ""))


def test_a_split_without_a_stop_is_refused_the_same_way(cli, monkeypatch):
    """A split shares ONE stop across every ticket, so a split with no stop is
    the naked-position problem multiplied by the split count."""
    class _Info:
        volume_min, volume_max, volume_step, digits = 0.01, 100.0, 0.01, 2
        filling_mode = 1

    class _Tick:
        bid, ask = 4285.00, 4285.18

    class _Hedging(_FakeMT5):
        def account_info(self):
            return types.SimpleNamespace(equity=10000.0, margin_free=9000.0, margin_mode=2)

        def order_calc_margin(self, action, symbol, volume, price):
            return volume * 100.0 * price / 1000.0

    sent = []
    monkeypatch.setattr(cli, "require_bridge", lambda: (_Hedging(sent, _Info(), _Tick()), None))
    out = {}
    monkeypatch.setattr(cli, "emit", lambda payload, text=None, code=0: (out.update(payload), code)[1])
    args = types.SimpleNamespace(
        symbol="XAUUSD", side="buy", volume=0.10, splits=10, group="g",
        sl=None, tp=None, deviation=20, magic=1, comment="c",
        stop_on_failure=False, check_cost=False, allow_no_stop=False,
    )
    assert cli.cmd_split(args) == 1
    assert sent == []
    assert "no stop" in str(out.get("error", ""))


def test_modify_reports_when_it_leaves_a_position_with_nothing_holding_it(cli, monkeypatch):
    """A modify can REMOVE protection (--sl 0), and a position with neither leg
    has no server-side exit at all. Whoever reads the next result has to be able
    to see that nothing is holding it but the model."""
    class _Pos:
        ticket, symbol, type, sl, tp, volume = 500, "XAUUSD", 0, 4283.18, 4300.0, 0.10

    class _Info:
        digits, point = 2, 0.01
        trade_stops_level, trade_freeze_level = 0, 0

    class _Tick:
        bid, ask = 4285.00, 4285.18

    class _M:
        POSITION_TYPE_BUY = 0
        TRADE_RETCODE_DONE = 10009
        TRADE_ACTION_SLTP = 6

        def positions_get(self, ticket=None):
            return [_Pos()]

        def symbol_info(self, symbol):
            return _Info()

        def symbol_info_tick(self, symbol):
            return _Tick()

        def order_send(self, request):
            return types.SimpleNamespace(
                retcode=10009, comment="Request executed", order=1, deal=1, price=None
            )

        def last_error(self):
            return "fake"

    out: dict = {}
    monkeypatch.setattr(cli, "require_bridge", lambda: (_M(), None))
    monkeypatch.setattr(cli, "emit", lambda payload, text=None, code=0: (out.update(payload), code)[1])

    assert cli.cmd_modify(types.SimpleNamespace(
        ticket=[500], tickets=[], symbol=None, all=False, exit_at=None, sl=0.0, tp=0.0,
    )) == 0
    assert out["alert"] == "position_left_without_a_stop"
    assert out["positions_without_a_stop"] == [500]

    # A modify that LEAVES a stop in place is not an alert.
    out.clear()
    assert cli.cmd_modify(types.SimpleNamespace(
        ticket=[500], tickets=[], symbol=None, all=False, exit_at=None, sl=0.0, tp=4300.0,
    )) == 0
    assert "alert" not in out


# --------------------------------------------------------------------------- #
# THE ACCOUNT CIRCUIT BREAKER, AND ONE CALL FOR TOTAL EXPOSURE
# --------------------------------------------------------------------------- #
# A stop limits what ONE trade can lose. Nothing limited what the ACCOUNT could
# lose, and the account is the thing that runs out. Five "small" positions each
# risking 2% is 10% on the table, and no single ticket's stop stops that -- it
# takes a rule that looks at the whole book, at the moment the next order is
# about to be sent. A rule only the model remembers to apply is not a rule, so
# it is enforced at the one point every order passes through.


class _Pos:
    def __init__(self, ticket, entry, sl, volume=0.10, profit=0.0, symbol="XAUUSD"):
        self.ticket, self.price_open, self.sl = ticket, entry, sl
        self.volume, self.profit, self.symbol = volume, profit, symbol
        self.tp, self.price_current, self.type = 0.0, entry, 0


class _RiskMT5:
    """A terminal with positions, an account, and a day's worth of deals."""

    DEAL_TYPE_BALANCE = 2

    def __init__(self, positions=(), equity=10000.0, deals=(), contract=100.0):
        self._positions, self._equity = list(positions), equity
        self._deals, self._contract = list(deals), contract

    def positions_get(self, ticket=None):
        return list(self._positions)

    def account_info(self):
        return types.SimpleNamespace(
            equity=self._equity, balance=self._equity, margin_free=self._equity,
            currency="USD",
        )

    def symbol_info(self, symbol):
        return types.SimpleNamespace(trade_contract_size=self._contract, digits=2, point=0.01)

    def symbol_info_tick(self, symbol):
        return None

    def symbols_get(self):
        return []

    def history_deals_get(self, start, end):
        return list(self._deals)


def _deal(profit, swap=0.0, commission=0.0, kind=0):
    return types.SimpleNamespace(profit=profit, swap=swap, commission=commission, type=kind)


def _limits_file(cli, monkeypatch, tmp_path, limits=None):
    path = tmp_path / "risk_limits.json"
    monkeypatch.setattr(cli, "RISK_LIMITS_FILE", path)
    if limits is not None:
        path.write_text(json.dumps(limits), encoding="utf-8")
    return path


def test_a_position_risk_is_measured_from_its_stop_in_money(cli):
    """|entry - stop| x contract x lots. Gold, 0.10 lots, $2 of stop = $20."""
    mt5 = _RiskMT5()
    pos = _Pos(1, entry=4287.23, sl=4285.05, volume=0.09)
    assert cli._position_risk_money(mt5, pos) == 19.62


def test_a_position_with_no_stop_has_unknown_risk_rather_than_zero(cli):
    """Zero would let a naked position pass every total-risk check as though it
    were free, which is the opposite of what it is."""
    mt5 = _RiskMT5()
    assert cli._position_risk_money(mt5, _Pos(1, entry=4287.23, sl=0.0)) is None


def test_total_exposure_says_how_many_positions_it_does_not_cover(cli):
    mt5 = _RiskMT5([
        _Pos(1, 4287.23, 4285.05, volume=0.09, profit=1.44),
        _Pos(2, 4287.00, 4285.00, volume=0.10, profit=-2.00),
        _Pos(3, 4287.00, 0.0, volume=0.10),          # naked
    ])
    out = cli._open_exposure(mt5)
    assert out["totals"]["positions"] == 3
    assert out["totals"]["positions_counted"] == 2
    assert out["totals"]["risk_unknown_positions"] == 1
    assert out["positions_without_a_stop"] == [3]
    # 0.09 x 100 x 2.18 = 19.62, and 0.10 x 100 x 2.00 = 20.00
    assert out["totals"]["risk_money"] == 39.62
    assert out["totals"]["risk_pct_of_equity"] == 0.4
    assert out["totals"]["unrealised_pnl_money"] == -0.56


def test_no_limits_means_no_breach_and_no_limit_read_is_an_error_not_a_pass(cli, monkeypatch, tmp_path):
    """The dangerous case is an unreadable limits file: treating it as "no
    limits" would drop the protection at exactly the moment something is wrong."""
    _limits_file(cli, monkeypatch, tmp_path, limits=None)
    gate = cli._risk_gate(_RiskMT5(), new_risk_money=20.0)
    assert gate["limits"] == {} and gate["limits_error"] is None
    assert gate["breaches"] == []

    path = _limits_file(cli, monkeypatch, tmp_path, limits={"max_positions": 3})
    path.write_text("{not json", encoding="utf-8")
    gate = cli._risk_gate(_RiskMT5(), new_risk_money=20.0)
    assert gate["limits_error"] is not None
    assert "not valid JSON" in gate["limits_error"]


def test_max_positions_refuses_the_order_that_would_cross_it(cli, monkeypatch, tmp_path):
    _limits_file(cli, monkeypatch, tmp_path, limits={"max_positions": 2})
    mt5 = _RiskMT5([_Pos(1, 4287.0, 4285.0), _Pos(2, 4287.0, 4285.0)])
    gate = cli._risk_gate(mt5, new_risk_money=20.0, new_positions=1)
    assert [b["limit"] for b in gate["breaches"]] == ["max_positions"]
    assert gate["breaches"][0]["actual"] == 2 and gate["breaches"][0]["allowed"] == 2


def test_max_total_risk_refuses_when_the_book_would_carry_too_much(cli, monkeypatch, tmp_path):
    _limits_file(cli, monkeypatch, tmp_path, limits={"max_total_risk_money": 50.0})
    mt5 = _RiskMT5([_Pos(1, 4287.0, 4285.0, volume=0.10)])   # $20 of open risk
    # $20 open + $40 new = $60, past the $50 limit.
    gate = cli._risk_gate(mt5, new_risk_money=40.0, new_positions=1)
    assert [b["limit"] for b in gate["breaches"]] == ["max_total_risk_money"]
    assert gate["breaches"][0]["actual"] == 60.0
    # ...and it fits when it does fit.
    assert cli._risk_gate(mt5, new_risk_money=25.0, new_positions=1)["breaches"] == []


def test_a_stopless_order_cannot_be_counted_against_a_total_risk_limit(cli, monkeypatch, tmp_path):
    """An uncountable position is exactly the one that makes the limit
    meaningless, so it is refused rather than waved through as free."""
    _limits_file(cli, monkeypatch, tmp_path, limits={"max_total_risk_money": 50.0})
    gate = cli._risk_gate(_RiskMT5(), new_risk_money=None, new_positions=1)
    assert [b["limit"] for b in gate["breaches"]] == ["max_total_risk_money"]
    assert "no stop" in gate["breaches"][0]["reason"]


def test_a_total_risk_limit_says_when_the_total_is_an_understatement(cli, monkeypatch, tmp_path):
    """A naked OPEN position makes the total too small, and a limit enforced
    against a number known to be too small reads as protection while being none."""
    _limits_file(cli, monkeypatch, tmp_path, limits={"max_total_risk_money": 500.0})
    mt5 = _RiskMT5([_Pos(1, 4287.0, 0.0)])
    gate = cli._risk_gate(mt5, new_risk_money=20.0, new_positions=1)
    assert len(gate["breaches"]) == 1
    assert "UNDERSTATEMENT" in gate["breaches"][0]["reason"]


def test_the_daily_loss_limit_ends_the_day(cli, monkeypatch, tmp_path):
    _limits_file(cli, monkeypatch, tmp_path, limits={"max_daily_loss_money": 200.0})
    # A deposit is DEAL_TYPE_BALANCE: counting one would let a funding transfer
    # read as a winning day, or a withdrawal as a loss that trips the breaker.
    losing = _RiskMT5(deals=[_deal(-150.0), _deal(-60.0, commission=-5.0), _deal(9999.0, kind=2)])
    gate = cli._risk_gate(losing, new_risk_money=20.0, new_positions=1)
    assert [b["limit"] for b in gate["breaches"]] == ["max_daily_loss_money"]
    assert gate["today"]["realised_pnl_money"] == -215.0
    assert gate["today"]["losing_deals"] == 2

    # Down but not out: the day continues.
    mild = _RiskMT5(deals=[_deal(-30.0)])
    assert cli._risk_gate(mild, new_risk_money=20.0, new_positions=1)["breaches"] == []


def test_the_history_query_is_only_paid_for_when_a_daily_limit_exists(cli, monkeypatch, tmp_path):
    """This gate runs on EVERY order, so it must not add a deals query to the
    path where no daily limit is set."""
    _limits_file(cli, monkeypatch, tmp_path, limits={"max_positions": 5})
    gate = cli._risk_gate(_RiskMT5(), new_risk_money=20.0, new_positions=1)
    assert gate["today"] == {"skipped": "no max_daily_loss_money limit is set"}

    _limits_file(cli, monkeypatch, tmp_path, limits={"max_daily_loss_money": 100.0})
    gate = cli._risk_gate(_RiskMT5(), new_risk_money=20.0, new_positions=1)
    assert "realised_pnl_money" in gate["today"]


def test_limits_set_show_and_clear_round_trip(cli, monkeypatch, tmp_path):
    path = _limits_file(cli, monkeypatch, tmp_path, limits=None)
    out: dict = {}
    monkeypatch.setattr(cli, "emit", lambda payload, text=None, code=0: (out.update(payload), code)[1])
    monkeypatch.setattr(cli, "require_bridge", lambda: (_RiskMT5(), None))
    setargs = types.SimpleNamespace(
        limits_action="set", max_daily_loss_money=200.0, max_positions=3,
        max_total_risk_money=500.0, max_total_risk_pct=None,
    )
    assert cli.cmd_limits(setargs) == 0
    assert out["limits"] == {
        "max_daily_loss_money": 200.0, "max_positions": 3, "max_total_risk_money": 500.0,
    }
    assert json.loads(path.read_text(encoding="utf-8"))["max_positions"] == 3

    # set MERGES: a second call adds rather than replacing the first.
    out.clear()
    assert cli.cmd_limits(types.SimpleNamespace(
        limits_action="set", max_daily_loss_money=None, max_positions=None,
        max_total_risk_money=None, max_total_risk_pct=5.0,
    )) == 0
    assert out["limits"]["max_positions"] == 3
    assert out["limits"]["max_total_risk_pct"] == 5.0

    out.clear()
    assert cli.cmd_limits(types.SimpleNamespace(limits_action="clear")) == 0
    assert out["cleared"] is True and not path.exists()


def test_limits_set_with_nothing_to_set_is_refused(cli, monkeypatch, tmp_path):
    _limits_file(cli, monkeypatch, tmp_path, limits=None)
    out: dict = {}
    monkeypatch.setattr(cli, "emit", lambda payload, text=None, code=0: (out.update(payload), code)[1])
    code = cli.cmd_limits(types.SimpleNamespace(
        limits_action="set", max_daily_loss_money=None, max_positions=None,
        max_total_risk_money=None, max_total_risk_pct=None,
    ))
    assert code == 1
    assert "at least one" in str(out.get("error", ""))
    # A non-positive limit is not a limit.
    assert cli.cmd_limits(types.SimpleNamespace(
        limits_action="set", max_daily_loss_money=0.0, max_positions=None,
        max_total_risk_money=None, max_total_risk_pct=None,
    )) == 1


def test_risk_reports_the_room_left_before_the_next_order(cli, monkeypatch, tmp_path):
    _limits_file(cli, monkeypatch, tmp_path, limits={
        "max_positions": 3, "max_total_risk_money": 200.0, "max_daily_loss_money": 100.0,
    })
    mt5 = _RiskMT5([_Pos(1, 4287.0, 4285.0, volume=0.10)], deals=[_deal(-25.0)])
    out: dict = {}
    monkeypatch.setattr(cli, "emit", lambda payload, text=None, code=0: (out.update(payload), code)[1])
    monkeypatch.setattr(cli, "require_bridge", lambda: (mt5, None))
    assert cli.cmd_risk(types.SimpleNamespace()) == 0
    assert out["totals"]["risk_money"] == 20.0
    assert out["headroom"]["positions"] == 2
    assert out["headroom"]["risk_money"] == 180.0
    assert out["headroom"]["daily_loss_money"] == -75.0
    assert out["today"]["realised_pnl_money"] == -25.0
    assert "alert" not in out


def test_an_order_that_breaks_a_limit_is_refused_before_it_is_sent(cli, monkeypatch, tmp_path):
    """The limit has to be enforced at the point of sending, or it is only a
    suggestion that depends on the caller remembering it."""
    _limits_file(cli, monkeypatch, tmp_path, limits={"max_positions": 1})
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    mt5 = _OrderMT5(info, tick, orders=[])
    mt5.positions_get = lambda ticket=None: [_Pos(1, 4287.0, 4285.0)]
    sent, out = _wire_order(cli, monkeypatch, mt5)

    assert cli.cmd_order(_order_args(volume=0.01, sl=4283.18)) == 1
    assert sent == [], "an order that breaks the account's limits must not be sent"
    assert "max_positions" in json.dumps(out["breaches"])
    assert "risk limits refuse" in str(out.get("error", ""))

    # ...and the same order goes through once the limit allows it.
    _limits_file(cli, monkeypatch, tmp_path, limits={"max_positions": 2})
    out.clear()
    assert cli.cmd_order(_order_args(volume=0.01, sl=4283.18)) == 0
    assert len(sent) == 1
    assert out["book_after"]["limits_in_force"] == ["max_positions"]
    assert out["risk_money"] == 2.0   # 0.01 lots x 100 oz x $2.00 of stop


def test_a_gate_that_cannot_read_the_book_refuses_rather_than_guesses(cli, monkeypatch, tmp_path):
    """An unreadable book returns nothing, and "nothing open" is exactly what a
    total of zero looks like -- so the failure is carried as an error and the
    gate stops sending, instead of trading against a number it knows is wrong."""
    _limits_file(cli, monkeypatch, tmp_path, limits={"max_total_risk_money": 50.0})

    class _Blind(_RiskMT5):
        def positions_get(self, ticket=None):
            raise RuntimeError("terminal not answering")

    gate = cli._risk_gate(_Blind(), new_risk_money=20.0, new_positions=1)
    assert gate["exposure"]["error"] is not None
    assert [b["limit"] for b in gate["breaches"]] == ["open_book_unreadable"]
    assert "known to be wrong" in gate["breaches"][0]["reason"]

    # With no limits in force there is nothing to enforce, so the order is not
    # refused -- but the report still says the total is not to be trusted.
    path = _limits_file(cli, monkeypatch, tmp_path, limits=None)
    path.unlink()
    gate = cli._risk_gate(_Blind(), new_risk_money=20.0, new_positions=1)
    assert gate["breaches"] == []
    assert gate["exposure"]["error"] is not None


def test_a_terminal_that_cannot_report_the_account_does_not_crash_the_gate(cli, monkeypatch, tmp_path):
    _limits_file(cli, monkeypatch, tmp_path, limits=None)

    class _Anonymous(_RiskMT5):
        def account_info(self):
            raise RuntimeError("no account info")

    mt5 = _Anonymous([_Pos(1, 4287.0, 4285.0)])
    exposure = cli._open_exposure(mt5)
    assert exposure["account"]["equity"] is None
    assert exposure["account"]["error"] is not None
    assert exposure["error"] is not None
    # The risk total is still measured, and is not silently zero.
    assert exposure["totals"]["risk_money"] == 20.0
    assert exposure["totals"]["risk_pct_of_equity"] is None


# --------------------------------------------------------------------------- #
# TWO THINGS THE LIVE ACCOUNT SAID THAT THE TESTS DID NOT
# --------------------------------------------------------------------------- #
# Both were found on 2026-09-24 by reading the box's actual output rather than
# the payload it came with. The JSON was right in both cases and the words were
# wrong, which is the harder failure to catch: the JSON is what the tests looked
# at, and the words are what a person reads.


def test_a_stopped_orders_text_line_is_not_the_no_stop_warning(cli, monkeypatch):
    """MEASURED LIVE: `order --risk-money 20 --sl 4284.92` filled at 4286.69 with
    the stop at 4284.92 on the position, and the payload carried no `alert` and
    no `warning` -- but the line printed above it said "warning: this order
    carries no stop". Every successful market order said that."""
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick))

    texts: list[str] = []
    monkeypatch.setattr(
        cli, "emit", lambda payload, text=None, code=0: (out.update(payload), texts.append(text), code)[2]
    )

    assert cli.cmd_order(_order_args(sl=4283.18, risk_money=20.0)) == 0
    assert out["filled"] is True
    assert "alert" not in out and "warning" not in out
    assert texts and texts[0] is not None
    assert "warning" not in texts[0].lower()
    assert "no stop" not in texts[0].lower()
    # It says what actually happened instead.
    assert "accepted" in texts[0]

    # ...and a genuinely stopless order still says so, in both places.
    texts.clear()
    out.clear()
    assert cli.cmd_order(_order_args(volume=0.10, allow_no_stop=True)) == 0
    assert out["alert"] == "opened_without_a_stop"


def test_limits_set_does_not_report_a_breach_about_an_order_that_does_not_exist(cli, monkeypatch, tmp_path):
    """MEASURED LIVE: `limits set --max-total-risk-money 30` on a FLAT account
    answered with a standing breach saying "this order carries no stop, so what it
    risks cannot be counted" -- there was no order. The gate is asked about the
    book by passing new_risk_money=None, which is by definition the stopless case,
    so that complaint had to be filtered out of the standing report."""
    _limits_file(cli, monkeypatch, tmp_path, limits=None)
    out: dict = {}
    monkeypatch.setattr(cli, "emit", lambda payload, text=None, code=0: (out.update(payload), code)[1])
    monkeypatch.setattr(cli, "require_bridge", lambda: (_RiskMT5(), None))

    assert cli.cmd_limits(types.SimpleNamespace(
        limits_action="set", max_daily_loss_money=None, max_positions=None,
        max_total_risk_money=30.0, max_total_risk_pct=None,
    )) == 0
    assert out["standing"] == []
    assert out["limits"] == {"max_total_risk_money": 30.0}

    # A breach that IS about the book still shows: a naked position makes the
    # total too small to enforce anything against.
    out.clear()
    monkeypatch.setattr(
        cli, "require_bridge", lambda: (_RiskMT5([_Pos(1, 4287.0, 0.0)]), None)
    )
    assert cli.cmd_limits(types.SimpleNamespace(
        limits_action="set", max_daily_loss_money=None, max_positions=None,
        max_total_risk_money=None, max_total_risk_pct=5.0,
    )) == 0
    assert len(out["standing"]) == 1
    assert "UNDERSTATEMENT" in out["standing"][0]["reason"]


# --------------------------------------------------------------------------- #
# The Wine command layer: multi-line code and `%` used to be lost SILENTLY
# --------------------------------------------------------------------------- #
def test_a_multi_line_run_code_is_spilled_to_a_file_for_the_batch_layer(cli, tmp_path):
    """MEASURED 2026-09-24 (live Deriv-Demo box): ``run --code "<multi-line>"``
    returned NOTHING -- no JSON, exit 0.

    The re-exec writes ``<python.exe> <mt5_cli.py> "run" "--code" "<code>"`` into a
    ``.bat``, and a batch file is line-based: the first newline ended the command,
    the redirect never happened, and the caller got an empty stdout. Indistinguishable
    from a hang, in the one subcommand meant to be the escape hatch.
    """
    script = "x = 1\ny = 2\nresult = x + y"
    argv = cli._spill_code_to_file(["run", "--code", script], tmp_path)

    assert "--code-file" in argv and "--code" not in argv
    passed = argv[argv.index("--code-file") + 1]
    assert passed.startswith("C:\\mt5tmp\\")
    assert (tmp_path / "code.py").read_text(encoding="utf-8") == script

    # A single-line script keeps the inline path: nothing about the common case moves.
    assert cli._spill_code_to_file(["run", "--code", "result = 1"], tmp_path) == [
        "run", "--code", "result = 1",
    ]
    # Only `run` carries free-form code; other actions are left alone.
    assert cli._spill_code_to_file(["order", "--code", "a\nb"], tmp_path) == [
        "order", "--code", "a\nb",
    ]


def test_cmd_run_reads_the_code_file_the_reexec_spilled(cli, monkeypatch, tmp_path, capsys):
    script = tmp_path / "code.py"
    script.write_text("x = 40\nresult = x + 2", encoding="utf-8")
    monkeypatch.setattr(cli, "require_bridge", lambda: (types.SimpleNamespace(), None))

    assert cli.cmd_run(types.SimpleNamespace(code=None, code_file=str(script))) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload == {"ok": True, "result": 42}


def test_a_windows_code_file_path_is_localized_into_the_prefix(cli, monkeypatch, tmp_path):
    """The re-exec passes ``C:\\mt5tmp\\<id>\\code.py``; either interpreter may read it."""
    monkeypatch.setattr(cli, "WINE_PREFIX", tmp_path / "wine")
    target = tmp_path / "wine" / "drive_c" / "mt5tmp" / "9911-abcd" / "code.py"
    target.parent.mkdir(parents=True)
    target.write_text("result = 'from a windows path'", encoding="utf-8")

    assert cli._localize_wine_path(r"C:\mt5tmp\9911-abcd\code.py") == target
    # A Linux path is already local and passes through untouched.
    assert cli._localize_wine_path(str(target)) == target


def test_a_newline_that_cannot_cross_the_batch_layer_is_refused_out_loud(
    cli, monkeypatch, tmp_path, capsys
):
    """Refused rather than written into a .bat that fails quietly."""
    monkeypatch.setattr(cli, "under_wine", lambda: False)
    monkeypatch.setattr(cli, "win_python", lambda: tmp_path / "python.exe")
    monkeypatch.setattr(cli, "WINE_PREFIX", tmp_path / "wine")
    monkeypatch.setattr(cli, "wine_bin", lambda: "wine")
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)

    assert cli._reexec_under_wine(["order", "--comment", "two\nlines"]) is not None
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert "newline" in payload["error"]
    assert payload["action"] == "order"


def test_a_child_that_wrote_nothing_is_a_reported_failure_not_silence(
    cli, monkeypatch, tmp_path, capsys
):
    """Silence used to be returned as SUCCESS with an empty stdout.

    The caller could then only say "the tool produced no JSON", which is the same
    message a frozen terminal gives -- for a completely different reason. The
    command file is kept as the evidence and named in the failure.
    """
    monkeypatch.setattr(cli, "under_wine", lambda: False)
    monkeypatch.setattr(cli, "win_python", lambda: tmp_path / "python.exe")
    monkeypatch.setattr(cli, "WINE_PREFIX", tmp_path / "wine")
    monkeypatch.setattr(cli, "wine_bin", lambda: "wine")
    # Wine runs, and writes nothing at all -- the silent-loss case.
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0)
    )

    assert cli._reexec_under_wine(["account"]) is not None
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert "produced no output" in payload["error"]
    assert Path(payload["bat"]).exists(), "the command file must survive as evidence"


def test_book_after_reports_the_book_the_trade_left_behind(cli, monkeypatch, tmp_path):
    """`book_after` used to be the exposure the risk gate read BEFORE the send.

    MEASURED live 2026-09-24 (Deriv-Demo): XAUUSD 0.01 with a stop filled at
    retcode 10009 with $21.29 of risk, and the payload reported
    ``book_after: {positions: 0, open_risk_money: 0.0}`` -- the one number a caller
    checks to confirm the trade landed said nothing had.
    """
    _limits_file(cli, monkeypatch, tmp_path, limits={})
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    mt5 = _OrderMT5(info, tick, orders=[])
    # The book is empty until the deal lands, then the position is there.
    opened = [_Pos(777, 4285.18, 4283.18, volume=0.01)]
    state = {"filled": False}
    mt5.positions_get = lambda ticket=None: opened if state["filled"] else []

    sent, out = _wire_order(cli, monkeypatch, mt5)
    inner = cli._order_send

    def _send(m, req, fillings):  # the fill is what puts the position on the book
        result = inner(m, req, fillings)
        state["filled"] = True
        return result

    monkeypatch.setattr(cli, "_order_send", _send)

    assert cli.cmd_order(_order_args(volume=0.01, sl=4283.18)) == 0
    assert len(sent) == 1
    assert out["book_before"]["positions"] == 0
    assert out["book_after"]["positions"] == 1
    assert out["book_after"]["settled"] is True
    # 0.01 lots x 100 oz x $2.00 of stop.
    assert out["book_after"]["open_risk_money"] == 2.0


# --------------------------------------------------------------------------- #
# THE COMMENT CEILING -- 29 characters, enforced by the WRAPPER, not the broker
# --------------------------------------------------------------------------- #
# MEASURED 2026-09-24 on a live Deriv-Demo terminal (MetaTrader5 5.0.6180), by
# asking the terminal itself with `order_check`, which validates and prices a
# request without sending it:
#
#   comment of 29 characters -> retcode 0
#   comment of 30 characters -> order_check returns None and last_error is
#                               '(-2, \'Invalid "comment" argument\')'
#
# The limit counts CHARACTERS: 29 accented characters (58 bytes) are accepted.
# This is why `split` did nothing on a real account: its comment was
# `<base>:<group>:<n>of<total>` with a 16-character base, so `--group verify-split`
# built a 30-character comment and EVERY ticket came back retcode null with the
# reason dropped -- a split that reported "0 of 3 filled" 30 seconds after a plain
# `order` on the same symbol filled with retcode 10009.
def test_the_comment_ceiling_is_the_measured_one(cli):
    assert cli.ORDER_COMMENT_MAX == 29


def test_a_comment_is_fitted_and_reported_when_it_is_cut(cli, monkeypatch):
    """An over-long comment is REFUSED BEFORE SENDING, so it cannot be sent as-is.

    It is fitted rather than passed through, and the truncation is reported: the
    comment is how the trade is found in the terminal afterwards, so quietly
    sending a different label would be its own defect.
    """
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    sent, out = _wire_order(cli, monkeypatch, _OrderMT5(info, tick))
    long_comment = "verify-" + "x" * 40

    assert cli.cmd_order(_order_args(sl=4283.18, volume=0.1, comment=long_comment)) == 0
    assert len(sent[0]["comment"]) == cli.ORDER_COMMENT_MAX
    assert sent[0]["comment"] == long_comment[: cli.ORDER_COMMENT_MAX]
    assert out["comment_truncated"] == {
        "requested": long_comment,
        "sent": long_comment[: cli.ORDER_COMMENT_MAX],
        "limit": cli.ORDER_COMMENT_MAX,
    }

    # A comment that already fits is passed through untouched and NOT reported as
    # truncated -- otherwise the notice stops meaning anything.
    sent.clear()
    out.clear()
    assert cli.cmd_order(_order_args(sl=4283.18, volume=0.1, comment="verify-book")) == 0
    assert sent[0]["comment"] == "verify-book"
    assert "comment_truncated" not in out


def test_a_comment_is_folded_onto_one_line(cli):
    """The Wine layer writes arguments into a line-based .bat.

    A comment carrying a newline would end that line early and turn the rest of
    the call into a second, broken command -- the same failure that made
    ``run --code`` return nothing at all. A comment is a label, so it is folded
    rather than refused: keeping all of it on one line beats quoting half of it.
    """
    assert cli._fit_comment("gold\nbreakout\tidea   two") == "gold breakout idea two"
    assert cli._fit_comment("") == ""
    assert cli._fit_comment(None) == ""
    assert len(cli._fit_comment("=" * 200)) == cli.ORDER_COMMENT_MAX


def test_the_split_group_budget_is_derived_from_the_comment_budget(cli):
    """The group tag is capped by the comment budget, not by a round number.

    ``pwx:<group>:<n>of<total>`` must fit for the WIDEST possible index suffix
    (``50of50``, since SPLIT_MAX_TICKETS is 50). Cutting the group afterwards
    instead would break the ``:group:`` match `close --group` depends on, and
    cutting the index would make "close the first three" pick tickets by accident.
    """
    assert cli.SPLIT_GROUP_MAX == (
        cli.ORDER_COMMENT_MAX - len(cli.SPLIT_COMMENT_PREFIX) - 2 - len("50of50")
    )
    widest = cli._split_group_tag("g" * 50)
    assert len(widest) == cli.SPLIT_GROUP_MAX
    assert len(f"{cli.SPLIT_COMMENT_PREFIX}:{widest}:50of50") <= cli.ORDER_COMMENT_MAX
    # A tag that already fits is left alone, and the separator is still ':group:'.
    assert cli._split_group_tag("verify-split") == "verify-split"
    assert cli._split_group_tag(" xau leg 2 ") == "xauleg2"


def _wire_split(cli, monkeypatch, sent, positions=(), send=None):
    class _Info:
        volume_min, volume_max, volume_step, digits = 0.01, 100.0, 0.01, 2
        filling_mode = 1

    class _Tick:
        bid, ask = 4285.00, 4285.18

    monkeypatch.setattr(
        cli, "require_bridge", lambda: (_FakeMT5(sent, _Info(), _Tick(), positions), None)
    )
    monkeypatch.setattr(cli, "filling_candidates", lambda mt5, info: [1])
    if send is None:
        def send(mt5, req, fillings):
            sent.append(dict(req))
            return {
                "ok": True, "retcode": 10009, "comment": "Done",
                "result": {"order": len(sent), "deal": len(sent), "price": req["price"]},
            }
    monkeypatch.setattr(cli, "_order_send", send)


def _split_args(**over):
    base = dict(
        symbol="XAUUSD", side="buy", volume=0.03, splits=3, group="", sl=4270.0,
        tp=4320.0, deviation=20, magic=0, comment="powerx-split",
        stop_on_failure=False, check_cost=False, allow_no_stop=False,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def test_every_split_comment_fits_for_the_group_that_failed_live(cli, monkeypatch):
    """THE LIVE REGRESSION: `--group verify-split` made all three tickets vanish.

    Its comment was ``powerx-split:verify-split:1of3`` -- 30 characters, one over
    the wrapper's ceiling -- so every ticket was refused locally and the split
    reported 0 of 3 filled with no reason on a symbol a plain `order` filled.
    """
    sent: list = []
    _wire_split(cli, monkeypatch, sent)

    assert cli.cmd_split(_split_args(group="verify-split")) == 0
    assert len(sent) == 3
    for index, req in enumerate(sent, start=1):
        assert len(req["comment"]) <= cli.ORDER_COMMENT_MAX, req["comment"]
        # The tag is still a field, with its colons, so `close --group` finds it.
        assert f":verify-split:" in req["comment"]
        assert req["comment"].endswith(f":{index}of3")
    # The exact live group is comfortably inside the budget now.
    assert sent[0]["comment"] == "pwx:verify-split:1of3"


def test_split_refuses_a_named_group_that_is_already_open(cli, monkeypatch):
    """It fires ONCE. A second split on the same group doubles the position.

    The comments still read as one group, so `close --group` would then take off
    twice what the caller believes is there, at a blend of two prices. Refused
    before anything is sent -- and the tag is the caller's own, since
    `_split_group_tag` truncates a long name to the same tag the tickets carry.
    """
    existing = [types.SimpleNamespace(ticket=11, volume=0.01,
                                      comment="pwx:verify-split:1of3")]
    sent: list = []
    _wire_split(cli, monkeypatch, sent, positions=existing)
    out: dict = {}

    def _emit(payload, text=None, code=0):
        out.update(payload)
        out["__text"] = text
        return code

    monkeypatch.setattr(cli, "emit", _emit)
    code = cli.cmd_split(_split_args(group="verify-split"))

    assert code != 0, "a split that would double the position must not be sent"
    assert sent == []
    assert "already open" in str(out.get("error", ""))
    assert out["open_tickets"] == 1
    assert out["open_volume"] == 0.01

    # AND THE UNNAMED CASE STILL WORKS: the default tag is the constant "split",
    # so refusing it would block a legitimate second split for everyone who never
    # named one. The guarantee belongs to the group the caller chose.
    sent.clear()
    _wire_split(cli, monkeypatch, sent)
    assert cli.cmd_split(_split_args(group="")) == 0
    assert len(sent) == 3


def test_a_split_ticket_that_never_left_reports_why(cli, monkeypatch, capsys):
    """A ticket with no retcode never reached the broker, and the reason is the point.

    MEASURED 2026-09-24: the 30-character comment produced three entries reading
    ``retcode: null, comment: null`` while the cause sat in ``error`` and was
    dropped by the per-ticket report -- so the caller was told "0 of 3 tickets
    filled" and nothing about why.
    """
    sent: list = []

    def _refused(mt5, req, fillings):
        return {
            "ok": False,
            "stage": "not_sent",
            "error": "(-2, 'Invalid \"comment\" argument')",
            "comment": 'refused before sending: (-2, \'Invalid "comment" argument\')',
            "request": dict(req),
        }

    _wire_split(cli, monkeypatch, sent, send=_refused)
    assert cli.cmd_split(_split_args(group="verify-split")) != 0

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["splits_filled"] == 0
    for entry in payload["results"]:
        assert entry["stage"] == "not_sent"
        assert "Invalid" in entry["error"]
    assert "Invalid" in payload["warning"]


def test_order_send_labels_a_locally_refused_request(cli):
    """``order_send`` returning None means NOTHING was sent: no retcode exists.

    The reason is put into ``comment`` as well as ``error``, because every caller
    that reports an outcome reads ``comment`` -- and the sentence it produced
    before this was "order rejected: None".
    """
    class _Refuser(FakeMT5):
        def order_send(self, request):
            return None

    out = cli._order_send(_Refuser([10009]), {"type_filling": 1}, [1])

    assert out["ok"] is False
    assert out["stage"] == "not_sent"
    assert out["retcode"] is None
    assert out["comment"].startswith("refused before sending:")
    assert out["error"] == "fake-last-error"


def test_an_order_rejection_never_reads_as_none(cli, monkeypatch, capsys):
    """The human sentence has to carry the reason, not the word "None".

    ``fail``/``emit`` print the text on stderr, so that is where the sentence a
    person reads comes from.
    """
    info = _sym(2, 100.0)
    tick = types.SimpleNamespace(bid=4285.00, ask=4285.18)
    mt5 = _OrderMT5(info, tick)

    def _refused(m, req, fillings):
        return {
            "ok": False, "stage": "not_sent", "retcode": None,
            "error": "(-2, 'Invalid \"comment\" argument')",
            "comment": 'refused before sending: (-2, \'Invalid "comment" argument\')',
        }

    monkeypatch.setattr(cli, "require_bridge", lambda: (mt5, None))
    monkeypatch.setattr(cli, "filling_candidates", lambda m, i: [2])
    monkeypatch.setattr(cli, "_order_send", _refused)

    assert cli.cmd_order(_order_args(sl=4283.18, volume=0.1)) != 0
    text = capsys.readouterr().err.strip().splitlines()[0]
    assert "NOT SENT" in text
    assert "Invalid" in text
    assert "None" not in text
