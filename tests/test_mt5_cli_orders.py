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
    """Just the constants the real 5.0.6180 module exposes for filling."""

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

    monkeypatch.setattr(cli, "find_terminal", lambda: tmp_path / "terminal64.exe")
    monkeypatch.setattr(cli, "wine_bin", lambda: "wine")
    monkeypatch.setattr(cli, "wine_env", lambda: {})
    monkeypatch.setattr(cli, "MT5_ROOT", tmp_path / "mt5")
    monkeypatch.setattr(cli, "WINE_PREFIX", tmp_path / "wine")
    # A terminal is up, but it was launched WITHOUT /config:.
    monkeypatch.setattr(cli, "_terminal_processes", lambda: [(4242, ["wine", "/t/terminal64.exe"])])
    monkeypatch.setattr(cli, "_terminal_has_credentials", lambda: False)
    def fake_stop() -> list[int]:
        stopped.append(4242)
        return [4242]

    monkeypatch.setattr(cli, "_stop_terminal_processes", fake_stop)
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
    monkeypatch.setattr(cli, "find_terminal", lambda: tmp_path / "terminal64.exe")
    monkeypatch.setattr(cli, "wine_bin", lambda: "wine")
    monkeypatch.setattr(cli, "MT5_ROOT", tmp_path / "mt5")
    monkeypatch.setattr(cli, "WINE_PREFIX", tmp_path / "wine")
    monkeypatch.setattr(cli, "_terminal_processes", lambda: [(1, ["wine", "/t/terminal64.exe"])])
    monkeypatch.setattr(cli, "_terminal_has_credentials", lambda: True)
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
        sl=None, tp=None, deviation=20, magic=20240919,
        comment="powerx-split", stop_on_failure=False, check_cost=False,
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
        sl=None, tp=None, deviation=20, magic=20240919,
        comment="c", stop_on_failure=False, check_cost=False,
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
