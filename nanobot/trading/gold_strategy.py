"""The default trading playbook: Gold, a 20-pip stop and a 1:7 reward-to-risk.

This encodes the strategy the model is expected to trade by default, taken from
"Combined Crypto & Gold Trading Strategy: Integrating CRYPTODAVIDNWAN and Legend
Pro Crypto" (Mohanta, T. K.) — the compiled guide the user supplied, whose
section 7 fixes the Gold parameters, and whose sections 4-6 fix the levels and
the entry signals.

The document's own numbers, which this module reproduces rather than
paraphrases::

    Entry 4,162.50   SL 4,160.50   TP 4,176.50

``4,162.50 - 4,160.50 = 2.00`` and ``4,176.50 - 4,162.50 = 14.00``. The document
calls those "20 pips" and "140 pips", so **one Gold pip is 0.10** — ten points on
a two-decimal quote. That is the single most load-bearing fact in this file.

Getting it wrong is not a rounding error, it is a factor of ten. A broker quotes
XAUUSD with ``digits=2`` and ``point=0.01``, and ``10 ** -digits`` therefore
yields 0.01 — the *point*, not the pip. Measured on a live Deriv-Demo terminal
2026-09-24: ``quote XAUUSD`` reports ``digits: 2, point: 0.01``. Treated as the
pip, "a 20-pip stop" becomes a $0.20 stop, which on gold is inside the spread of
its own quote — the position is stopped out on the next tick, and every guard
level expressed in pips is off by 10x. So the convention is declared once, here
and in ``scripts/mt5_cli.py``, and asserted by tests in both places.

The document's other fixed parameters:

* **Range divisor 4.68** — the Gold setting for the "Trading Legend Range
  Levels" indicator. It projects the session range into the sub-levels below.
* **Sub-levels** — 25% / 50% / 62.5% / 75% / 87.5% / 100%, plus the 150% (1.5x)
  structure extension. The 62.5% "Golden Retracement" is the headline entry.
* **Trend filter** — the MA Ribbon. Bullish is price above the ribbon AND above
  the 50% level; bearish is price below both. When the two disagree the document
  says stand aside, and so does this module.
* **Entry signals** — pin bar, engulfing bar, inside bar, and only at a level.

What this module deliberately does NOT do is decide that a setup is good. It
does the arithmetic, checks the setup against the document's own rules, and
reports what each rule says. The judgement stays with the caller: a 1:7 target
is a claim about the market, and no module can make it true.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = [
    "GOLD_PIP",
    "SL_PIPS",
    "TARGET_RR",
    "TP_PIPS",
    "RANGE_DIVISOR",
    "RANGE_LEVELS",
    "AGGRESSIVE_RISK_PCT",
    "is_gold",
    "pip_size",
    "level_prices",
    "ema",
    "ma_ribbon",
    "bias",
    "pin_bar",
    "engulfing",
    "inside_bar",
    "entry_signal",
    "validate_setup",
    "volume_for_risk",
    "margin_estimate",
    "plan",
]


#: One Gold pip, in price. See the module docstring for the derivation: the
#: source document's 4,162.50 -> 4,160.50 stop is "20 pips", so a pip is 0.10.
GOLD_PIP = 0.10

#: The document's Gold stop, in pips. "A strict 20-pip Stop Loss must be applied
#: to all Gold trades."
SL_PIPS = 20.0

#: The document's Gold reward-to-risk. "For every 20 pips risked, the target
#: profit must be 140 pips."
TARGET_RR = 7.0

#: 1:7 as an absolute pip distance, so the two can never drift apart.
TP_PIPS = SL_PIPS * TARGET_RR

#: The Gold setting of the document's range indicator. The document states the
#: number but not the arithmetic behind it, so it is carried as a parameter and
#: surfaced in every plan rather than silently baked into the levels.
RANGE_DIVISOR = 4.68

#: The document's range sub-levels, as fractions of the projected range, in the
#: order price meets them on the way up. 0.625 is the "Golden Retracement" and
#: 1.5 is the "1.5x Structure" extension.
RANGE_LEVELS: tuple[tuple[str, float], ...] = (
    ("0%", 0.0),
    ("25%", 0.25),
    ("50%", 0.50),
    ("62.5%", 0.625),
    ("75%", 0.75),
    ("87.5%", 0.875),
    ("100%", 1.0),
    ("150%", 1.5),
)

#: The document's risk per trade. It is reported, never applied implicitly: see
#: ``plan``'s ``ruin_warning``. Risking a fifth of an account per trade is a
#: parameter that empties accounts, and the document itself says so.
AGGRESSIVE_RISK_PCT = 20.0

#: Contract size of Gold, in troy ounces. 1.00 lot moves $100 per $1.00 of
#: price, which is what makes the money arithmetic below true.
GOLD_CONTRACT = 100.0


def is_gold(symbol: str) -> bool:
    """Whether ``symbol`` is a Gold instrument, by the broker's own spelling.

    Brokers label Gold ``XAUUSD``, ``GOLD``, ``XAUUSD.raw``, ``frxXAUUSD``,
    ``XAUUSDm`` … The test is on the substring rather than a fixed list, because
    a symbol this function fails to recognise silently falls back to the
    point-as-pip convention and gets a 10x-wrong stop.
    """
    return "XAU" in (symbol or "").upper() or "GOLD" in (symbol or "").upper()


def pip_size(symbol: str, digits: int | None = None) -> float:
    """The pip of ``symbol``, in price.

    Gold is 0.10 regardless of ``digits`` (see the module docstring). Everything
    else follows the FX convention: a pip is the second-to-last quoted digit, so
    a 5-digit pair gives 0.0001 and a 3-digit JPY pair gives 0.01. When ``digits``
    is unknown the FX rule assumes 5, which is the common case and the safer
    error — it understates the pip rather than overstating it.
    """
    if is_gold(symbol):
        return GOLD_PIP
    d = 5 if digits is None else int(digits)
    # 5 digits -> 0.0001; 3 digits -> 0.01; and a 2-digit exotic gets 0.01 too
    # rather than 0.001, which no FX quote uses.
    exponent = max(d - 1, 2)
    return float(10 ** -exponent)


def level_prices(low: float, high: float) -> dict[str, float]:
    """The document's range sub-levels for a session range ``low``..``high``.

    Returns a name -> price mapping in the document's own labels, so a plan can
    say "the 62.5% Golden Retracement sits at 4,292.18" in the words the guide
    uses. ``150%`` is the 1.5x structure extension above the range.
    """
    span = float(high) - float(low)
    return {
        name: round(float(low) + fraction * span, 5) for name, fraction in RANGE_LEVELS
    }


def _closes(bars: Sequence[Mapping[str, Any]]) -> list[float]:
    out: list[float] = []
    for bar in bars:
        value = bar.get("close")
        if value is None:
            continue
        out.append(float(value))
    return out


def ema(values: Sequence[float], period: int) -> float | None:
    """The EMA of ``values`` at its last point, or ``None`` if too short."""
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / float(period)
    value = seed
    for point in values[period:]:
        value = point * k + value * (1.0 - k)
    return value


def ma_ribbon(
    bars: Sequence[Mapping[str, Any]], fast: int = 9, slow: int = 21
) -> dict[str, Any]:
    """The document's trend filter: the first two lines of the MA Ribbon.

    The guide does not name the ribbon's periods, so the fastest pair that is
    still a ribbon is assumed (9/21) and the assumption is returned in the
    result rather than hidden — the caller can recompute with other pairs by
    calling ``ema`` directly.
    """
    closes = _closes(bars)
    return {
        "fast_period": fast,
        "slow_period": slow,
        "fast": ema(closes, fast),
        "slow": ema(closes, slow),
        "bars": len(closes),
    }


def bias(
    price: float | None,
    ribbon_fast: float | None,
    ribbon_slow: float | None,
    midpoint: float | None,
) -> dict[str, Any]:
    """The document's bias rule, with the two halves reported separately.

    "Bullish: Price > MA Ribbon AND Price > 50% of Opening Range. Bearish: Price
    < MA Ribbon AND Price < 50%." The conjunction is the point: when price is
    above the ribbon but below the midpoint, the document says stand aside, and
    reporting that as "bullish" would be the exact failure this function exists
    to prevent.
    """
    if price is None:
        return {"bias": "unknown", "price_vs_ribbon": None, "price_vs_mid": None}
    ribbon = None
    if ribbon_fast is not None and ribbon_slow is not None:
        # "the ribbon" is a band; price is above it only if it is above both.
        hi, lo = max(ribbon_fast, ribbon_slow), min(ribbon_fast, ribbon_slow)
        if price > hi:
            ribbon = "above"
        elif price < lo:
            ribbon = "below"
        else:
            ribbon = "inside"
    vs_mid = None if midpoint is None else ("above" if price > midpoint else "below")
    if ribbon == "above" and vs_mid == "above":
        verdict = "bullish"
    elif ribbon == "below" and vs_mid == "below":
        verdict = "bearish"
    else:
        verdict = "none"
    return {
        "bias": verdict,
        "price_vs_ribbon": ribbon,
        "price_vs_mid": vs_mid,
        "note": (
            "Trend filter agrees with the range midpoint."
            if verdict in ("bullish", "bearish")
            else "Price and the range midpoint disagree: the document says stand "
            "aside until they don't."
        ),
    }


def pin_bar(bar: Mapping[str, Any]) -> str | None:
    """The document's rejection signal, as the side it points at.

    "Small body with a long tail (at least 2/3 of the candle length)." A long
    lower tail rejects lower prices and points up; a long upper tail points
    down. ``None`` when the candle is not a pin bar, which is the common case.
    """
    return _tail_bar(bar, 2.0 / 3.0)


def _tail_bar(bar: Mapping[str, Any], tail_fraction: float) -> str | None:
    try:
        op, hi, lo, cl = (float(bar["open"]), float(bar["high"]),
                          float(bar["low"]), float(bar["close"]))
    except (KeyError, TypeError, ValueError):
        return None
    span = hi - lo
    if span <= 0:
        return None
    body = abs(cl - op)
    if body > span * (1.0 - tail_fraction):
        return None
    upper, lower = hi - max(op, cl), min(op, cl) - lo
    if lower >= span * tail_fraction and lower > upper:
        return "buy"
    if upper >= span * tail_fraction and upper > lower:
        return "sell"
    return None


def engulfing(previous: Mapping[str, Any], current: Mapping[str, Any]) -> str | None:
    """The document's momentum signal: a body that swallows the previous body."""
    try:
        po, pc = float(previous["open"]), float(previous["close"])
        co, cc = float(current["open"]), float(current["close"])
    except (KeyError, TypeError, ValueError):
        return None
    top, bottom = max(po, pc), min(po, pc)
    if co <= bottom and cc >= top and cc > co:
        return "buy"
    if co >= top and cc <= bottom and cc < co:
        return "sell"
    return None


def inside_bar(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """The document's breakout signal: a candle inside the previous mother bar."""
    try:
        return (
            float(current["high"]) <= float(previous["high"])
            and float(current["low"]) >= float(previous["low"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def entry_signal(
    bars: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The document's entry signals, read off the last closed bars.

    Checks the most recent bar against its predecessor for all three patterns
    and reports every one that matched rather than the first, because the
    document treats them as independent confirmations and a caller deciding
    whether to enter needs to know if two of them agree.
    """
    if len(bars) < 2:
        return {"signals": [], "side": None}
    current, previous = bars[-1], bars[-2]
    signals: list[str] = []
    sides: list[str] = []
    side = pin_bar(current)
    if side:
        signals.append("pin_bar")
        sides.append(side)
    side = engulfing(previous, current)
    if side:
        signals.append("engulfing_bar")
        sides.append(side)
    if inside_bar(previous, current):
        # An inside bar is a breakout setup, not a direction: the document
        # enters when price breaks the mother bar, so the side is not known yet.
        signals.append("inside_bar")
    agreed = sides[0] if sides and len(set(sides)) == 1 else None
    return {
        "signals": signals,
        "side": agreed,
        "conflicting": len(set(sides)) > 1,
        "note": (
            "Inside bar alone: wait for the break of the mother bar "
            f"({previous.get('high')} / {previous.get('low')})."
            if signals == ["inside_bar"]
            else ""
        ),
    }


def volume_for_risk(
    risk_money: float, sl_pips: float, symbol: str = "XAUUSD"
) -> float | None:
    """The lot size that risks exactly ``risk_money`` over a ``sl_pips`` stop.

    Gold: 1.00 lot is 100 oz, so one pip of 0.10 is worth $10 per lot.
    """
    if risk_money <= 0 or sl_pips <= 0:
        return None
    lots = risk_money / (sl_pips * money_per_pip(1.0, symbol))
    return round(lots, 2)


def money_per_pip(volume: float, symbol: str = "XAUUSD") -> float:
    """What one pip is worth, in account currency, at ``volume`` lots."""
    return float(volume) * GOLD_CONTRACT * pip_size(symbol)


def margin_estimate(
    volume: float,
    price: float,
    leverage: float = 1000.0,
    contract: float = GOLD_CONTRACT,
) -> float | None:
    """Rough margin for a Gold position; ``None`` if it cannot be estimated."""
    if not price or leverage <= 0:
        return None
    return round(float(volume) * contract * float(price) / float(leverage), 2)


def validate_setup(setup: Mapping[str, Any]) -> list[str]:
    """Every way ``setup`` breaks the document's fixed rules.

    Returns the violations as sentences, empty when the setup complies. This is
    the function that makes the strategy *the* strategy: a plan that is not a
    20-pip stop at 1:7 is not this playbook, whatever else it may be.
    """
    violations: list[str] = []
    try:
        entry = float(setup["entry"])
        stop = float(setup["sl"])
        target = float(setup["tp"])
        side = str(setup["side"]).lower()
    except (KeyError, TypeError, ValueError):
        return ["Setup is missing entry, sl, tp or side."]
    if side not in ("buy", "sell"):
        violations.append(f"side must be buy or sell, got {setup.get('side')!r}.")
        return violations
    symbol = str(setup.get("symbol") or "XAUUSD")
    pip = pip_size(symbol, setup.get("digits"))
    if side == "buy":
        if not stop < entry < target:
            violations.append(
                f"A buy needs sl < entry < tp; got {stop} < {entry} < {target}."
            )
    else:
        if not target < entry < stop:
            violations.append(
                f"A sell needs tp < entry < sl; got {target} < {entry} < {stop}."
            )
    risk_pips = abs(entry - stop) / pip
    reward_pips = abs(target - entry) / pip
    if abs(risk_pips - SL_PIPS) > 0.05:
        violations.append(
            f"Stop is {risk_pips:g} pips, not the required {SL_PIPS:g}."
        )
    if reward_pips <= 0:
        violations.append("Target is on the wrong side of the entry: reward is zero.")
    elif abs(reward_pips / risk_pips - TARGET_RR) > 0.05:
        violations.append(
            f"Reward-to-risk is 1:{reward_pips / risk_pips:.2f}, not the required "
            f"1:{TARGET_RR:g}."
        )
    return violations


def plan(
    side: str,
    entry: float,
    volume: float = 0.1,
    symbol: str = "XAUUSD",
    equity: float | None = None,
    sl_pips: float = SL_PIPS,
    rr: float = TARGET_RR,
    digits: int | None = None,
    low: float | None = None,
    high: float | None = None,
    spread: float | None = None,
) -> dict[str, Any]:
    """Build the playbook's order: entry, stop, target, lots, and the checks.

    ``low``/``high``, when given, add the document's range sub-levels and say
    which level the entry is sitting on — the guide enters *at a level*, so an
    entry that is not within a pip or two of one is reported as a violation
    rather than quietly accepted.

    ``equity``, when given, turns the pip arithmetic into account percentages,
    which is where the document's 20% risk parameter becomes visible as the
    account-destroying number it is.
    """
    pip = pip_size(symbol, digits)
    direction = 1.0 if str(side).lower() == "buy" else -1.0
    if str(side).lower() not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    stop = round(entry - direction * sl_pips * pip, 5)
    target = round(entry + direction * rr * sl_pips * pip, 5)
    volume = float(volume)
    per_pip = money_per_pip(volume, symbol)
    risk_money = round(sl_pips * per_pip, 2)
    reward_money = round(rr * sl_pips * per_pip, 2)

    setup = {
        "symbol": symbol,
        "side": str(side).lower(),
        "entry": float(entry),
        "sl": stop,
        "tp": target,
        "volume": volume,
        "digits": digits,
    }
    out: dict[str, Any] = {
        "ok": True,
        "strategy": "gold-20pip-1to7",
        "setup": setup,
        "pip": pip,
        "sl_pips": sl_pips,
        "tp_pips": rr * sl_pips,
        "rr": rr,
        "money_per_pip": round(per_pip, 2),
        "risk_money": risk_money,
        "reward_money": reward_money,
        "violations": validate_setup(setup),
    }
    if equity:
        out["equity"] = float(equity)
        out["risk_pct"] = round(risk_money / float(equity) * 100.0, 3)
        out["reward_pct"] = round(reward_money / float(equity) * 100.0, 3)
        aggressive_volume = volume_for_risk(
            float(equity) * AGGRESSIVE_RISK_PCT / 100.0, sl_pips, symbol
        )
        out["document_risk_pct"] = AGGRESSIVE_RISK_PCT
        out["volume_at_document_risk"] = aggressive_volume
        out["ruin_warning"] = (
            f"The document's {AGGRESSIVE_RISK_PCT:g}% risk per trade would be "
            f"{aggressive_volume} lots here (${round(float(equity) * AGGRESSIVE_RISK_PCT / 100.0, 2)} "
            "at risk). At that size a run of losses is unrecoverable, and the "
            "document says so itself. The lot actually planned above risks "
            f"{out['risk_pct']:.2f}% -- {AGGRESSIVE_RISK_PCT / max(out['risk_pct'], 1e-9):.0f}x less."
        )
        out["margin_estimate"] = margin_estimate(volume, float(entry))
        if aggressive_volume:
            out["margin_estimate_at_document_risk"] = margin_estimate(
                aggressive_volume, float(entry)
            )
    if spread is not None:
        out["spread_pips"] = round(float(spread) / pip, 2)
        if float(spread) > sl_pips * pip * 0.25:
            out["violations"].append(
                f"Spread is {out['spread_pips']:g} pips -- more than a quarter of "
                f"the {sl_pips:g}-pip stop. The stop is inside the cost of the round "
                "trip, so a tight entry cannot survive it."
            )
    if low is not None and high is not None:
        levels = level_prices(float(low), float(high))
        out["levels"] = levels
        nearest, distance = None, None
        for name, price in levels.items():
            if name == "150%":
                continue
            gap = abs(float(entry) - price)
            if distance is None or gap < distance:
                nearest, distance = name, gap
        out["entry_level"] = nearest
        out["entry_level_price"] = levels.get(nearest) if nearest else None
        out["entry_level_distance_pips"] = (
            round(distance / pip, 2) if distance is not None else None
        )
        if distance is not None and distance > 2 * pip:
            out["violations"].append(
                f"Entry is {out['entry_level_distance_pips']:g} pips away from the "
                f"nearest range level ({nearest} at {levels.get(nearest)}). The "
                "document enters AT a level, not between them."
            )
        out["range"] = {
            "low": float(low),
            "high": float(high),
            "span": round(float(high) - float(low), 5),
            "divisor": RANGE_DIVISOR,
            "span_over_divisor": round((float(high) - float(low)) / RANGE_DIVISOR, 5),
        }
    out["ok"] = not out["violations"]
    return out
