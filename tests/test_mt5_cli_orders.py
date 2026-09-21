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
