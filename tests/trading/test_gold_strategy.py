"""The default playbook: Gold, a 20-pip stop, 1:7.

The strategy is only the strategy if it reproduces the numbers in the guide it
came from, so the first tests here are arithmetic, not behaviour: the guide's
own worked setup is ``entry 4162.50 / SL 4160.50 / TP 4176.50``, and this module
must hand those three numbers straight back. Everything else -- the pips, the
levels, the risk in percent -- follows from them.

The one that would silently wreck an account is the pip itself. A broker quotes
XAUUSD with ``digits=2``, so ``10 ** -digits`` is 0.01 -- the *point*, not the
pip -- and a 20-pip stop becomes a $0.20 stop inside the spread of its own quote.
That is a 10x error in every level the model ever expresses on Gold, so it is
pinned here and pinned again against the bridge CLI's own copy of it.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from nanobot.trading import gold_strategy as gs

CLI_PATH = Path(__file__).resolve().parents[2] / "scripts" / "mt5_cli.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("mt5_cli_gold_under_test", CLI_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The number everything else depends on.
# ---------------------------------------------------------------------------


def test_the_guides_own_setup_comes_back_unchanged():
    """The guide: entry 4162.50, SL 4160.50, TP 4176.50. A pip is 0.10."""
    result = gs.plan(side="buy", entry=4162.50, volume=0.1)
    assert result["setup"]["sl"] == 4160.50
    assert result["setup"]["tp"] == 4176.50
    assert result["sl_pips"] == 20.0
    assert result["tp_pips"] == 140.0
    assert result["violations"] == []


def test_a_gold_pip_is_ten_points_even_though_digits_says_two():
    """``digits=2`` means the *point* is 0.01. The pip is 0.10.

    MEASURED 2026-09-24 on a live Deriv-Demo terminal: ``quote XAUUSD`` returns
    ``digits: 2, point: 0.01``. Taking 10**-digits as the pip understates the
    stop tenfold, which puts it inside the quote's own spread.
    """
    assert gs.GOLD_PIP == 0.10
    assert gs.pip_size("XAUUSD", digits=2) == 0.10
    # The naive rule, stated so the difference is impossible to miss.
    assert 10 ** -2 == 0.01
    assert gs.pip_size("XAUUSD", digits=2) == 10 * (10 ** -2)


def test_broker_spellings_of_gold_all_get_the_gold_pip():
    """A symbol we fail to recognise quietly gets a 10x-wrong stop."""
    for name in ("XAUUSD", "xauusd", "GOLD", "frxXAUUSD", "XAUUSD.raw", "XAUUSDm"):
        assert gs.is_gold(name), name
        assert gs.pip_size(name, digits=2) == 0.10, name


def test_fx_keeps_the_fx_pip():
    """Gold is the exception; it must not leak into the FX pairs."""
    assert gs.pip_size("EURUSD", digits=5) == pytest.approx(0.0001)
    assert gs.pip_size("GBPUSD", digits=5) == pytest.approx(0.0001)
    assert gs.pip_size("USDJPY", digits=3) == pytest.approx(0.01)
    assert not gs.is_gold("EURUSD")


def test_the_bridges_own_pip_agrees_with_the_playbook():
    """Two copies of 0.10, in two languages of the same bug.

    ``scripts/mt5_cli.py`` computes the pips for ``watch``, ``guard`` and the
    near-miss report on the box, where it cannot import this module. If the two
    ever disagree, the guard's levels and the model's stated levels are on
    different scales and neither error says so.
    """
    cli = _load_cli()
    assert cli.GOLD_PIP == gs.GOLD_PIP
    assert cli._is_gold_symbol("XAUUSD") is True
    assert cli._is_gold_symbol("frxXAUUSD") is True
    assert cli._is_gold_symbol("EURUSD") is False

    class _Info:
        digits = 2

    class _MT5:
        @staticmethod
        def symbol_info(symbol):
            return _Info()

    digits, pip = cli._watch_symbol_pips(_MT5(), "XAUUSD")
    assert (digits, pip) == (2, 0.10)
    # The FX path must be untouched by the Gold rule.
    assert cli._watch_symbol_pips(_MT5(), "EURUSD")[1] == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# The money, at the lot the user actually trades.
# ---------------------------------------------------------------------------


def test_a_tenth_of_a_lot_risks_twenty_dollars_and_targets_a_hundred_and_forty():
    """0.10 lot of Gold is 10 oz, so one 0.10 pip is $1 and 20 pips are $20."""
    result = gs.plan(side="buy", entry=4285.00, volume=0.1, equity=10302.92)
    assert result["money_per_pip"] == 1.0
    assert result["risk_money"] == 20.0
    assert result["reward_money"] == 140.0
    assert result["rr"] == 7.0
    assert result["risk_pct"] == pytest.approx(0.194, abs=0.001)
    assert result["reward_pct"] == pytest.approx(1.359, abs=0.001)


def test_the_documents_twenty_percent_risk_is_reported_as_what_it_is():
    """20% of the account is 10.3 lots here, and the plan must say so.

    The guide calls 20% per trade a parameter; at 0.10 lot the real risk is
    0.19%, so the two are 103x apart. A model handed "risk 20%" and no
    arithmetic picks the first number it was told.
    """
    result = gs.plan(side="buy", entry=4285.00, volume=0.1, equity=10302.92)
    assert result["document_risk_pct"] == 20.0
    assert result["volume_at_document_risk"] == pytest.approx(10.3, abs=0.05)
    assert "unrecoverable" in result["ruin_warning"]
    assert "20%" in result["ruin_warning"] or "20" in result["ruin_warning"]
    # And the margin that size would need, which is the second wall it hits.
    assert result["margin_estimate_at_document_risk"] > result["margin_estimate"]


def test_volume_for_risk_is_the_inverse_of_the_money_math():
    assert gs.volume_for_risk(2000.0, 20.0) == pytest.approx(10.0)
    assert gs.volume_for_risk(20.0, 20.0) == pytest.approx(0.1)
    assert gs.volume_for_risk(0.0, 20.0) is None


def test_a_sell_mirrors_the_buy():
    result = gs.plan(side="sell", entry=4285.00, volume=0.1)
    assert result["setup"]["sl"] == 4287.00
    assert result["setup"]["tp"] == 4271.00
    assert result["violations"] == []


# ---------------------------------------------------------------------------
# The rules, enforced rather than described.
# ---------------------------------------------------------------------------


def test_a_stop_that_is_not_twenty_pips_is_a_violation():
    """Otherwise "the playbook" means whatever the last caller typed."""
    result = gs.plan(side="buy", entry=4285.00, volume=0.1, sl_pips=30.0)
    assert result["ok"] is False
    assert any("30 pips" in v and "20" in v for v in result["violations"])


def test_a_target_that_is_not_seven_to_one_is_a_violation():
    result = gs.plan(side="buy", entry=4285.00, volume=0.1, rr=2.0)
    assert result["ok"] is False
    assert any("1:2.00" in v for v in result["violations"])


def test_a_stop_on_the_wrong_side_of_the_entry_is_a_violation_not_a_sign_flip():
    """A buy whose stop sits above the entry is not a buy."""
    bad = {"symbol": "XAUUSD", "side": "buy", "entry": 4285.0, "sl": 4287.0, "tp": 4299.0}
    violations = gs.validate_setup(bad)
    assert any("sl < entry < tp" in v for v in violations)


def test_the_guide_reproduces_at_150_percent_extension_too():
    """The guide's target of 140 pips is its 100%/150% structure target."""
    result = gs.plan(side="sell", entry=4133.00, volume=0.1)
    assert result["setup"]["sl"] == 4135.00
    assert result["setup"]["tp"] == 4119.00
    assert result["tp_pips"] / result["sl_pips"] == 7.0


# ---------------------------------------------------------------------------
# The levels: the guide enters AT one, not between two.
# ---------------------------------------------------------------------------


def test_the_range_sub_levels_are_the_guides_own_labels():
    levels = gs.level_prices(4273.60, 4303.32)
    assert set(levels) == {"0%", "25%", "50%", "62.5%", "75%", "87.5%", "100%", "150%"}
    assert levels["0%"] == pytest.approx(4273.60)
    assert levels["100%"] == pytest.approx(4303.32)
    # 62.5% is the Golden Retracement: 18.575 above the low of a 29.72 range.
    assert levels["62.5%"] == pytest.approx(4292.175)
    assert levels["150%"] == pytest.approx(4318.18)
    assert levels["50%"] < levels["62.5%"] < levels["75%"]


def test_the_62_5_golden_retracement_is_recognised_as_a_level():
    """A sell at the 62.5% of the last leg is the guide's headline Gold entry."""
    low, high = 4273.60, 4303.32
    entry = gs.level_prices(low, high)["62.5%"]
    result = gs.plan(
        side="sell", entry=entry, volume=0.1, equity=10302.92, low=low, high=high
    )
    assert result["entry_level"] == "62.5%"
    assert result["entry_level_distance_pips"] == 0.0
    assert result["ok"] is True, result["violations"]
    # 1:7 off the golden retracement, and the target clears the swing low.
    assert result["setup"]["sl"] == pytest.approx(4294.175)
    assert result["setup"]["tp"] == pytest.approx(4278.175)


def test_an_entry_between_levels_is_a_violation():
    """The guide says enter at a level; an entry a dollar from one is not."""
    result = gs.plan(
        side="sell", entry=4290.00, volume=0.1, low=4273.60, high=4303.32
    )
    assert result["ok"] is False
    assert any("nearest range level" in v for v in result["violations"])


def test_a_spread_that_eats_the_stop_is_reported():
    """A 20-pip Gold stop only survives a normal spread, so say when it does not."""
    result = gs.plan(side="buy", entry=4285.00, volume=0.1, spread=0.60)
    assert result["spread_pips"] == 6.0
    assert any("Spread is 6 pips" in v for v in result["violations"])
    tight = gs.plan(side="buy", entry=4285.00, volume=0.1, spread=0.18)
    assert tight["spread_pips"] == 1.8
    assert tight["ok"] is True


def test_the_range_divisor_is_carried_not_baked_in():
    """The guide states 4.68 without its arithmetic, so it is surfaced."""
    result = gs.plan(
        side="buy", entry=4273.60, volume=0.1, low=4273.60, high=4303.32
    )
    assert result["range"]["divisor"] == 4.68
    assert result["range"]["span"] == pytest.approx(29.72)
    assert result["range"]["span_over_divisor"] == pytest.approx(6.3504, abs=0.001)


# ---------------------------------------------------------------------------
# Bias: the conjunction is the whole point.
# ---------------------------------------------------------------------------


def test_bias_needs_the_ribbon_and_the_midpoint_to_agree():
    agree_up = gs.bias(4290.0, 4287.0, 4288.0, 4285.0)
    assert agree_up["bias"] == "bullish"
    agree_down = gs.bias(4280.0, 4287.0, 4288.0, 4285.0)
    assert agree_down["bias"] == "bearish"


def test_price_inside_or_disagreeing_with_the_midpoint_is_no_bias():
    """"Price above the ribbon but below the midpoint" is not a bullish bias."""
    assert gs.bias(4284.0, 4280.0, 4281.0, 4290.0)["bias"] == "none"
    inside = gs.bias(4282.0, 4280.0, 4285.0, 4280.0)
    assert inside["price_vs_ribbon"] == "inside"
    assert inside["bias"] == "none"
    assert gs.bias(None, 1.0, 2.0, 3.0)["bias"] == "unknown"


def test_the_ribbon_is_two_emas_and_says_which_periods():
    bars = [{"close": float(c)} for c in range(1, 41)]
    ribbon = gs.ma_ribbon(bars)
    assert ribbon["fast_period"] == 9 and ribbon["slow_period"] == 21
    assert ribbon["fast"] is not None and ribbon["slow"] is not None
    # A monotonically rising series puts the fast line above the slow one.
    assert ribbon["fast"] > ribbon["slow"]
    assert gs.ma_ribbon(bars[:5])["fast"] is None


# ---------------------------------------------------------------------------
# Entry signals: the guide's three candlestick patterns.
# ---------------------------------------------------------------------------


def _bar(op, hi, lo, cl):
    return {"open": op, "high": hi, "low": lo, "close": cl}


def test_a_pin_bar_is_a_long_tail_and_points_away_from_it():
    lower = _bar(4280.0, 4281.0, 4270.0, 4280.5)
    assert gs.pin_bar(lower) == "buy"
    upper = _bar(4280.0, 4290.0, 4279.5, 4279.6)
    assert gs.pin_bar(upper) == "sell"


def test_a_candle_with_a_long_body_is_not_a_pin_bar():
    """The tail must be two thirds of the candle; a big body fails that."""
    assert gs.pin_bar(_bar(4270.0, 4290.0, 4269.0, 4289.0)) is None
    assert gs.pin_bar(_bar(4280.0, 4280.0, 4280.0, 4280.0)) is None


def test_an_engulfing_bar_swallows_the_previous_body():
    prev = _bar(4285.0, 4286.0, 4279.5, 4280.0)
    assert gs.engulfing(prev, _bar(4279.0, 4287.0, 4278.5, 4286.0)) == "buy"
    assert gs.engulfing(prev, _bar(4286.0, 4287.0, 4278.0, 4279.0)) == "sell"
    # A candle that merely gaps is not an engulfing bar.
    assert gs.engulfing(prev, _bar(4279.0, 4281.0, 4278.5, 4280.5)) is None


def test_an_inside_bar_is_a_breakout_setup_with_no_direction_yet():
    """The guide enters on the break of the mother bar, so the side is unknown."""
    mother = _bar(4282.0, 4290.0, 4280.0, 4288.0)
    child = _bar(4283.0, 4287.0, 4281.0, 4285.0)
    signal = gs.entry_signal([mother, child])
    assert "inside_bar" in signal["signals"]
    assert signal["side"] is None
    assert "break of the mother bar" in signal["note"]


def test_entry_signal_reports_agreement_between_patterns():
    """Two confirmations agreeing is a stronger claim than one."""
    prev = _bar(4285.0, 4286.0, 4279.5, 4280.0)
    current = _bar(4279.0, 4287.0, 4278.5, 4286.0)  # engulfing buy
    signal = gs.entry_signal([prev, current])
    assert signal["signals"] == ["engulfing_bar"]
    assert signal["side"] == "buy"
    assert gs.entry_signal([])["side"] is None


def test_a_callers_numbers_that_are_not_numbers_do_not_crash_the_plan():
    violations = gs.validate_setup({"side": "buy", "entry": "lots", "sl": 1, "tp": 2})
    assert violations and "missing" in violations[0]
    assert gs.pin_bar({}) is None
    assert gs.engulfing({}, {}) is None
    assert gs.inside_bar({}, {}) is False


def test_plan_refuses_an_impossible_side():
    with pytest.raises(ValueError):
        gs.plan(side="hold", entry=4285.0)


def test_the_plan_is_json_serialisable():
    """It is returned through a tool result; a set or a Decimal would break it."""
    assert json.loads(json.dumps(gs.plan(side="buy", entry=4285.0, volume=0.1)))


# ---------------------------------------------------------------------------
# Backtesting: the playbook measured over history, not believed
# ---------------------------------------------------------------------------
def _playbook_bars(*, after=frozenset(), bars_after=5, step=4.0):
    """A series the playbook MUST trade: a coil at the range high, then a run.

    Built so the entry is unambiguous rather than lucky: a steady uptrend puts
    the bias bullish and the price on the range's 100% level, a mother bar is
    followed by an inside bar (the guide's coil), and the bar after that closes
    through the mother's high. ``after`` names the bars of the move that should
    be poisoned -- "both" makes one bar span the stop AND the target.
    """
    bars: list[dict[str, float]] = []
    price = 4000.0
    for i in range(30):
        price += 1.0
        bars.append({"time": i, "open": price - 1.0, "high": price + 0.05,
                     "low": price - 1.1, "close": price})
    bars.append({"time": 30, "open": 4030.0, "high": 4035.0, "low": 4029.0, "close": 4034.0})
    bars.append({"time": 31, "open": 4031.0, "high": 4033.0, "low": 4030.0, "close": 4032.0})
    bars.append({"time": 32, "open": 4032.0, "high": 4036.0, "low": 4031.5, "close": 4035.5})
    run = 4036.0
    for i in range(bars_after):
        nxt = run + step
        low, high = run, nxt
        if "both" in after and i == 1:
            # A single bar that reaches the target AND the stop: the bar does not
            # say which came first.
            low, high = 4030.0, 4060.0
        bars.append({"time": 33 + i, "open": run, "high": high, "low": low, "close": nxt})
        run = nxt
    return bars


def test_backtest_trades_the_playbook_and_reaches_the_target():
    """The measurement exists so a playbook can be checked, not asserted.

    Every rule has to line up for this trade to exist at all -- ribbon and
    midpoint agreeing, a coil broken, the entry AT the range high -- and the
    result has to carry the arithmetic a caller would otherwise redo by hand.
    """
    result = gs.backtest_bars(_playbook_bars(), risk_money=100.0)

    assert result["ok"] is True
    assert result["trades"] == 1
    assert result["wins"] == 1 and result["losses"] == 0
    assert result["win_rate_pct"] == 100.0
    trade = result["sample_trades"][0]
    assert trade["signal"] == "inside_bar_break"
    assert trade["side"] == "buy"
    assert trade["bias"] == "bullish"
    assert trade["level"] == "100%"          # entered AT a range level
    assert trade["outcome"] == "win"
    # 20 pips of stop and 7x of target, from the fill -- the playbook's own
    # numbers, not a default that drifted.
    assert trade["fill"] - trade["sl"] == pytest.approx(2.00)
    assert trade["tp"] - trade["fill"] == pytest.approx(14.00)
    assert result["expectancy_r"] == 7.0
    assert result["net_r"] == pytest.approx(7.0)
    assert result["net_money"] == pytest.approx(700.0)
    assert "1:7 needs 12.5% wins" in result["verdict"]


def test_backtest_charges_the_spread_and_says_so():
    """A result without its costs is a sales pitch, not a measurement."""
    free = gs.backtest_bars(_playbook_bars())
    charged = gs.backtest_bars(_playbook_bars(), spread_pips=1.0)

    assert free["net_r"] == pytest.approx(7.0)
    # One pip of spread against a 20-pip stop is 0.05R per trade, so the win is
    # 6.95R -- a tenth as much as the target claims, which is the point.
    assert charged["net_r"] == pytest.approx(6.95)
    assert charged["cost_per_trade_r"] == pytest.approx(0.05)
    assert free["params"]["spread_pips"] == 0.0


def test_backtest_scores_a_bar_that_touches_both_levels_as_a_loss():
    """The single most common way a backtest is made to look good.

    When one bar reaches the stop and the target, the bar does not say which came
    first. Scoring it as a win is a choice; scoring it as a loss is the
    conservative one, and the trade is counted separately in `ambiguous_bars` so
    the caller can see how much of the result rests on that choice.
    """
    result = gs.backtest_bars(
        _playbook_bars(after=frozenset({"both"})), spread_pips=0.0
    )

    assert result["trades"] == 1
    assert result["sample_trades"][0]["outcome"] == "loss"
    assert result["ambiguous_bars"] == 1
    assert result["net_r"] == pytest.approx(-1.0)
    assert result["wins"] == 0


def test_backtest_says_which_rule_rejected_each_bar():
    """`trades: 0` is a result, and the reason has to be readable.

    The guide's filter is meant to reject most bars, so "nothing traded" without
    the per-rule counts is indistinguishable from a broken backtest.
    """
    flat = [
        {"time": i, "open": 4000.0, "high": 4000.5, "low": 3999.5, "close": 4000.0}
        for i in range(80)
    ]
    result = gs.backtest_bars(flat)

    assert result["ok"] is False
    assert result["trades"] == 0
    assert result["expectancy_r"] is None
    assert result["skipped_total"] > 0
    assert sum(result["skipped"].values()) == result["skipped_total"]
    assert "RESULT, not a" in result["note"]


def test_backtest_keeps_the_money_arithmetic_in_the_played_pip():
    """Every distance is in Gold pips (0.10), not the quote's points (0.01).

    The 10x trap this module exists to prevent: measured against a 4000-wide
    series, a 20-pip stop is $2.00 of price. A backtest that read `digits=2` as
    the pip would call it $0.20, stop out on the next bar, and report a strategy
    that cannot work.
    """
    result = gs.backtest_bars(_playbook_bars())

    assert result["pip"] == pytest.approx(0.10)
    assert result["net_pips"] == pytest.approx(140.0)   # 7R of a 20-pip stop
    assert result["params"]["fill"] == "next bar's open"
    assert result["bars"] == len(_playbook_bars())
