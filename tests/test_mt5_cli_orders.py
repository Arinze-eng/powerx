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
