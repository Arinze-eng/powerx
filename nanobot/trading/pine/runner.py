"""High-level orchestration: run a Pine script end to end and report.

Responsibilities
----------------
* load OHLCV for a symbol/timeframe (Yahoo, or local CSV)
* lint + auto-fix the script
* evaluate it into plots and signals
* run a trade simulation from the script's own entries/exits
* compute a preview (precursor) signal 1-5 bars ahead
* render PNG (mplfinance) and/or interactive HTML
* produce a compact text report an agent can hand back to a user
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from nanobot.trading.pine import indicators as ind
from nanobot.trading.pine.chart import (
    ChartSpec,
    render_interactive_html,
    render_mplfinance,
    split_plot_series,
)
from nanobot.trading.pine.evaluator import PineResult, PineRuntimeError, evaluate
from nanobot.trading.pine.linter import LintReport, lint
from nanobot.trading.pine.parser import PineSyntaxError, header_options, parse
from nanobot.trading.pine.precursor import (
    detect_precursor,
)

# Symbol aliases shared with the rest of the trading package.
SYMBOL_ALIASES = {
    "XAUUSD": "GC=F", "XAU": "GC=F", "GOLD": "GC=F",
    "XAGUSD": "SI=F", "SILVER": "SI=F",
    "WTI": "CL=F", "OIL": "CL=F", "USOIL": "CL=F",
    "BTCUSD": "BTC-USD", "BTC": "BTC-USD",
    "ETHUSD": "ETH-USD", "ETH": "ETH-USD",
    "SPX": "^GSPC", "US500": "^GSPC", "NAS100": "^IXIC", "NASDAQ": "^IXIC",
    "US30": "^DJI", "DOW": "^DJI",
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
}

# Yahoo intraday retention limits (days of history available).
_INTRADAY_MAX_DAYS = {"1m": 7, "2m": 60, "5m": 60, "15m": 60, "30m": 60, "60m": 730, "1h": 730, "90m": 60}


def resolve_symbol(symbol: str) -> tuple[str, str]:
    """Return (yahoo_symbol, display_symbol)."""
    raw = (symbol or "").strip().upper()
    if not raw:
        raise ValueError("missing symbol")
    return SYMBOL_ALIASES.get(raw, raw), raw


def _cache_dir() -> Path:
    """Writable location for OHLCV cache (keeps runs working when a provider throttles)."""
    import os

    base = os.getenv("NANOBOT_DATA_DIR") or os.getenv("NANOBOT_WORKSPACE") or ""
    root = Path(base) if base else Path.home() / ".cache" / "nanobot"
    target = root / "ohlcv"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        target = Path.cwd() / ".ohlcv_cache"
        target.mkdir(parents=True, exist_ok=True)
    return target


def _cache_path(symbol: str, interval: str, bars: int) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{symbol}_{interval}_{bars}")
    return _cache_dir() / f"{safe}.csv"


def _read_cache(symbol: str, interval: str, bars: int) -> pd.DataFrame | None:
    path = _cache_path(symbol, interval, bars)
    if not path.exists():
        return None
    try:
        frame = pd.read_csv(path, index_col=0, parse_dates=True)
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")
        return frame if not frame.empty else None
    except Exception:
        return None


def _write_cache(symbol: str, interval: str, bars: int, frame: pd.DataFrame) -> None:
    try:
        _cache_path(symbol, interval, bars).write_text(frame.to_csv(), encoding="utf-8")
    except OSError:
        pass


def load_ohlcv(
    symbol: str,
    timeframe: str = "1d",
    *,
    bars: int = 500,
    start: str | None = None,
    end: str | None = None,
    csv_path: str | Path | None = None,
    allow_cache: bool = True,
    retries: int = 3,
) -> tuple[pd.DataFrame, str]:
    """Load OHLCV data. Returns (frame, display_symbol).

    Order of preference: explicit CSV -> provider -> local disk cache. The cache
    only kicks in when the provider fails (e.g. rate limiting), so a run does not
    hard-fail just because a data vendor throttled us.
    """
    display = (symbol or "").strip().upper()
    if csv_path:
        from nanobot.trading.data_loader import load_local_csv

        return load_local_csv(csv_path), display

    yahoo_symbol, display = resolve_symbol(symbol)
    from nanobot.trading.data_loader import DataUnavailableError, load_pair

    interval = _yahoo_interval(timeframe)
    if not end:
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not start:
        start = _default_start(interval, bars)

    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            frame = load_pair(yahoo_symbol, start, end, interval)
            if not frame.empty:
                if bars and len(frame) > bars:
                    frame = frame.iloc[-bars:]
                if allow_cache:
                    _write_cache(yahoo_symbol, interval, bars, frame)
                return frame, display
        except Exception as exc:  # transient provider failure
            last_error = exc
            if attempt < retries - 1:
                time.sleep(0.8 * (attempt + 1))

    if allow_cache:
        cached = _read_cache(yahoo_symbol, interval, bars)
        if cached is not None and not cached.empty:
            cached.attrs["from_cache"] = True
            return cached, display

    raise DataUnavailableError(
        f"No market data for {display} on {timeframe}"
        + (f" ({last_error})" if last_error else "")
        + ". The data provider may be rate limiting; retry shortly, or pass a CSV."
    )


def _yahoo_interval(timeframe: str) -> str:
    token = (timeframe or "1d").lower()
    if token in {"1d", "d", "daily"}:
        return "1d"
    if token in {"1wk", "1w", "w", "weekly"}:
        return "1wk"
    if token in {"1mo", "1M", "monthly"}:
        return "1mo"
    if token.endswith("h"):
        hours = token[:-1]
        return "1h" if hours in {"1", ""} else "1h"
    return token if token.endswith("m") else "1d"


def _days_for(interval: str) -> int:
    if interval in _INTRADAY_MAX_DAYS:
        return max(1, _INTRADAY_MAX_DAYS[interval] - 1)
    if interval in {"1wk", "1mo"}:
        return 365 * 8
    return 365 * 6


def _default_start(interval: str, bars: int) -> str:
    from datetime import timedelta

    days = _days_for(interval)
    if bars and interval not in _INTRADAY_MAX_DAYS:
        approx = int(bars * (7 / 5 if interval == "1d" else 1))
        days = min(days, max(days // 4, approx + 30))
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# simulation
# --------------------------------------------------------------------------


@dataclass
class TradeRecord:
    side: str
    entry_index: int
    entry_time: Any
    entry_price: float
    exit_index: int | None = None
    exit_time: Any = None
    exit_price: float | None = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    bars_held: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "entry_time": str(self.entry_time),
            "entry_price": round(self.entry_price, 6),
            "exit_time": str(self.exit_time) if self.exit_time is not None else None,
            "exit_price": round(self.exit_price, 6) if self.exit_price is not None else None,
            "pnl": round(self.pnl, 4),
            "pnl_pct": round(self.pnl_pct, 4),
            "bars_held": self.bars_held,
            "reason": self.reason,
        }


@dataclass
class BacktestStats:
    trades: list[TradeRecord] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.count * 100.0) if self.count else 0.0

    @property
    def total_pnl_pct(self) -> float:
        return float(sum(t.pnl_pct for t in self.trades))

    @property
    def avg_pnl_pct(self) -> float:
        return self.total_pnl_pct / self.count if self.count else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl_pct for t in self.trades if t.pnl_pct > 0)
        losses = abs(sum(t.pnl_pct for t in self.trades if t.pnl_pct < 0))
        if losses == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def max_drawdown_pct(self) -> float:
        if not self.trades:
            return 0.0
        equity = np.cumsum([t.pnl_pct for t in self.trades])
        peak = np.maximum.accumulate(equity)
        return float(np.max(peak - equity)) if len(equity) else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trades": self.count,
            "wins": self.wins,
            "losses": self.count - self.wins,
            "win_rate_pct": round(self.win_rate, 2),
            "total_pnl_pct": round(self.total_pnl_pct, 3),
            "avg_pnl_pct": round(self.avg_pnl_pct, 3),
            "profit_factor": (
                round(self.profit_factor, 3) if np.isfinite(self.profit_factor) else None
            ),
            "max_drawdown_pct": round(self.max_drawdown_pct, 3),
        }


def simulate_trades(
    data: pd.DataFrame,
    result: PineResult,
    *,
    fee_bps: float = 0.0,
    allow_short: bool = True,
) -> BacktestStats:
    """Long/short, one-position-at-a-time simulation from the script's signals.

    Entry on the bar after the signal (causal), exit on explicit exit signals.
    Open trades at the end are marked to market so statistics stay honest.
    """
    stats = BacktestStats()
    longs = result.entries_long if result.entries_long is not None else pd.Series(False, index=data.index)
    shorts = result.entries_short if result.entries_short is not None else pd.Series(False, index=data.index)
    exits = result.exits if result.exits is not None else pd.Series(False, index=data.index)

    longs = longs.reindex(data.index).fillna(False).astype(bool)
    shorts = shorts.reindex(data.index).fillna(False).astype(bool)
    exits = exits.reindex(data.index).fillna(False).astype(bool)

    opens = np.asarray(data["open"].to_numpy(), dtype="float64")
    closes = np.asarray(data["close"].to_numpy(), dtype="float64")
    times = list(data.index)

    open_trade: TradeRecord | None = None
    fee = fee_bps / 10_000.0

    for i in range(1, len(data)):
        if open_trade is None:
            want_long = bool(longs.iloc[i - 1])
            want_short = bool(shorts.iloc[i - 1]) and allow_short
            if not (want_long or want_short):
                continue
            side = "long" if want_long else "short"
            price = opens[i] * (1 + fee if side == "long" else 1 - fee)
            open_trade = TradeRecord(
                side=side,
                entry_index=i,
                entry_time=times[i],
                entry_price=float(price),
                reason="entry",
            )
            continue

        should_exit = bool(exits.iloc[i - 1])
        # opposite signal also flattens
        opposite = (open_trade.side == "long" and bool(shorts.iloc[i - 1])) or (
            open_trade.side == "short" and bool(longs.iloc[i - 1])
        )
        if should_exit or opposite:
            price = closes[i] * (1 - fee if open_trade.side == "long" else 1 + fee)
            open_trade.exit_index = i
            open_trade.exit_time = times[i]
            open_trade.exit_price = float(price)
            sign = 1.0 if open_trade.side == "long" else -1.0
            open_trade.pnl = sign * (price - open_trade.entry_price)
            open_trade.pnl_pct = sign * (price / open_trade.entry_price - 1) * 100.0
            open_trade.bars_held = i - open_trade.entry_index
            open_trade.reason = "exit signal" if should_exit else "opposite signal"
            stats.trades.append(open_trade)
            open_trade = None

    if open_trade is not None and len(data):
        price = float(closes[-1])
        sign = 1.0 if open_trade.side == "long" else -1.0
        open_trade.exit_index = len(data) - 1
        open_trade.exit_time = times[-1]
        open_trade.exit_price = price
        open_trade.pnl = sign * (price - open_trade.entry_price)
        open_trade.pnl_pct = sign * (price / open_trade.entry_price - 1) * 100.0
        open_trade.bars_held = len(data) - 1 - open_trade.entry_index
        open_trade.reason = "still open (marked to market)"
        stats.trades.append(open_trade)

    return stats


# --------------------------------------------------------------------------
# outcome
# --------------------------------------------------------------------------


@dataclass
class PineRun:
    symbol: str
    timeframe: str
    source: str
    lint: LintReport
    result: PineResult | None = None
    data: pd.DataFrame | None = None
    stats: BacktestStats | None = None
    precursor: Any = None
    header: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    png_path: Path | None = None
    html_path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None and not self.error

    # -- charts -----------------------------------------------------------
    def _spec(self, last_bars: int, theme: str, title: str) -> ChartSpec:
        plots = self.result.plots if self.result else {}
        overlay, panel, conditions = split_plot_series(plots)
        return ChartSpec(
            symbol=self.symbol,
            timeframe=self.timeframe,
            title=title,
            theme=theme,
            last_bars=last_bars,
            overlay_indicators=overlay,
            panel_indicators=panel,
            condition_plots=conditions,
            signals_long=self.result.entries_long if self.result else None,
            signals_short=self.result.entries_short if self.result else None,
            precursor=self.precursor,
        )

    def render_png(self, out_path: str | Path, *, last_bars: int = 250, theme: str = "dark") -> Path:
        if self.data is None:
            raise RuntimeError("no data loaded")
        spec = self._spec(last_bars, theme, self.header.get("title", "Pine Script"))
        self.png_path = render_mplfinance(self.data, spec, out_path)
        return self.png_path

    def render_html(self, out_path: str | Path, *, last_bars: int = 400, theme: str = "dark") -> Path:
        if self.data is None:
            raise RuntimeError("no data loaded")
        spec = self._spec(last_bars, theme, self.header.get("title", "Pine Script"))
        self.html_path = render_interactive_html(self.data, spec, out_path)
        return self.html_path

    def linter_note(self) -> str:
        """One-line summary of auto-fixes/diagnostics applied before running."""
        if not self.lint.diagnostics:
            return ""
        fixes = [d for d in self.lint.diagnostics if d.severity == "info"]
        if fixes:
            return f"{len(fixes)} auto-fix(es): " + "; ".join(
                d.message for d in fixes[:3]
            )
        return self.lint.summary()

    # -- reporting --------------------------------------------------------
    def summary_text(self, *, max_trades: int = 5) -> str:
        lines: list[str] = []
        status = "OK" if self.ok else "FAILED"
        lines.append(f"Pine run: {status} · {self.symbol} · {self.timeframe}")
        if self.header:
            lines.append(
                f"  header: {self.header.get('kind', 'indicator')} "
                f"'{self.header.get('title', '')}' "
                f"overlay={self.header.get('overlay', True)}"
            )
        lines.append(f"  lint: {self.lint.summary()}")
        for diag in self.lint.errors[:5]:
            lines.append(f"    x {diag}")
        for diag in self.lint.warnings[:5]:
            lines.append(f"    ! {diag}")

        if self.error:
            lines.append(f"  error: {self.error}")
            return "\n".join(lines)

        assert self.result is not None
        lines.append(f"  bars: {len(self.data) if self.data is not None else 0}")
        lines.append(f"  plots: {', '.join(list(self.result.plots)[:8]) or 'none'}")
        lines.append(
            f"  signals: {int(self.result.entries_long.sum()) if self.result.entries_long is not None else 0} long, "
            f"{int(self.result.entries_short.sum()) if self.result.entries_short is not None else 0} short, "
            f"{int(self.result.exits.sum()) if self.result.exits is not None else 0} exit"
        )
        if self.result.notes:
            lines.append(f"  notes: {'; '.join(self.result.notes[:3])}")

        if self.stats is not None and self.stats.count:
            d = self.stats.to_dict()
            lines.append(
                f"  backtest: {d['trades']} trades · win {d['win_rate_pct']}% · "
                f"total {d['total_pnl_pct']}% · avg {d['avg_pnl_pct']}% · "
                f"PF {d['profit_factor']} · maxDD {d['max_drawdown_pct']}%"
            )
            for trade in self.stats.trades[-max_trades:]:
                lines.append(
                    f"    {trade.side.upper()} {trade.entry_time} @ {trade.entry_price:.6g} -> "
                    f"{trade.exit_price:.6g} ({trade.pnl_pct:+.2f}%, {trade.bars_held} bars)"
                )
        elif self.stats is not None:
            lines.append("  backtest: no trades produced by this script")

        if self.precursor is not None and getattr(self.precursor, "direction", 0):
            p = self.precursor
            side = "LONG" if p.direction > 0 else "SHORT"
            lines.append(
                f"  precursor: {side} in {p.eta_bars} bar(s) (~{p.eta_minutes:g} min), "
                f"confidence {p.confidence:.0%}"
            )
            for reason in p.reasons[:4]:
                lines.append(f"    - {reason}")
        elif self.precursor is not None:
            lines.append("  precursor: no imminent cross")

        if self.png_path:
            lines.append(f"  png: {self.png_path}")
        if self.html_path:
            lines.append(f"  html: {self.html_path}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------


def run_pine_script(
    source: str,
    *,
    symbol: str = "BTC-USD",
    timeframe: str = "1d",
    bars: int = 500,
    start: str | None = None,
    end: str | None = None,
    csv_path: str | Path | None = None,
    autofix: bool = True,
    backtest: bool = True,
    preview_bars: int = 5,
    precursor: bool = True,
) -> PineRun:
    """Run a Pine script end to end against real market data."""
    report = lint(source, autofix_enabled=autofix)
    working = report.fixed_source or source

    run = PineRun(
        symbol=(symbol or "").strip().upper(),
        timeframe=timeframe,
        source=working,
        lint=report,
    )

    if report.errors:
        run.error = "lint failed: " + "; ".join(str(d) for d in report.errors[:3])
        return run

    try:
        program = parse(working)
        run.header = header_options(program)
    except Exception:  # pragma: no cover - lint already covered this
        run.header = {}

    try:
        data, display = load_ohlcv(
            symbol, timeframe, bars=bars, start=start, end=end, csv_path=csv_path
        )
    except Exception as exc:
        run.error = f"data unavailable: {exc}"
        return run

    run.symbol = display
    run.data = data

    try:
        result, _program = evaluate(working, data, symbol=display, timeframe=timeframe)
    except PineRuntimeError as exc:
        run.error = f"runtime: {exc}"
        return run
    except PineSyntaxError as exc:
        run.error = f"syntax: {exc}"
        return run
    except Exception as exc:  # pragma: no cover - defensive
        run.error = f"evaluation failed: {exc}"
        return run

    run.result = result

    if backtest:
        run.stats = simulate_trades(data, result)

    if precursor and preview_bars > 0:
        run.precursor = _preview(data, result, timeframe, preview_bars)

    return run


def _preview(data: pd.DataFrame, result: PineResult, timeframe: str, horizon: int):
    """Pick the most relevant indicator pair for a precursor projection."""
    plots = {k: v for k, v in result.plots.items() if isinstance(v, pd.Series) and v.notna().any()}
    rsi_series = None

    # Prefer a same-panel moving-average pair if the script plotted two of them.
    ma_like = [name for name in plots if any(t in name.lower() for t in ("sma", "ema", "ma", "vwap", "basis"))]
    try:
        if any("rsi" in n.lower() for n in plots):
            rsi_series = next(v for k, v in plots.items() if "rsi" in k.lower())
        else:
            rsi_series = ind.rsi(data["close"], 14)

        if len(ma_like) >= 2:
            fast_name, slow_name = ma_like[0], ma_like[1]
            fast, slow = plots[fast_name], plots[slow_name]
        else:
            fast = ind.ema(data["close"], 9)
            slow = ind.ema(data["close"], 21)
            fast_name, slow_name = "EMA 9", "EMA 21"

        return detect_precursor(
            fast,
            slow,
            data,
            horizon_bars=horizon,
            timeframe=timeframe,
            fast_name=fast_name,
            slow_name=slow_name,
            confirm_rsi=rsi_series,
        )
    except Exception:
        return None


def self_check(
    source: str,
    *,
    symbol: str = "BTC-USD",
    timeframe: str = "1d",
    bars: int = 300,
    csv_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run the engine against its own output so the agent can verify work.

    Returns a dict with lint status, parse status, run status, signal count and
    a verdict. Used by the agent to *test before deploying/answering*.

    Pass ``csv_path`` to verify against a fixed local dataset instead of a live
    provider (useful for deterministic checks and offline environments).
    """
    checks: list[dict[str, Any]] = []

    report = lint(source, autofix_enabled=True)
    checks.append({
        "check": "lint",
        "passed": report.ok,
        "detail": report.summary(),
        "auto_fixed": bool(report.fixed_source and report.fixed_source != source),
    })

    working = report.fixed_source or source
    try:
        parse(working)
        checks.append({"check": "parse", "passed": True, "detail": "AST built"})
    except PineSyntaxError as exc:
        checks.append({"check": "parse", "passed": False, "detail": str(exc)})

    run = run_pine_script(
        source,
        symbol=symbol,
        timeframe=timeframe,
        bars=bars,
        csv_path=csv_path,
        autofix=True,
        backtest=True,
    )
    checks.append({
        "check": "execute",
        "passed": run.ok,
        "detail": run.error or f"{run.result.signal_count() if run.result else 0} signals",
    })
    if run.result is not None:
        checks.append({
            "check": "signals",
            "passed": run.result.signal_count() > 0 or run.stats is None,
            "detail": (
                f"{run.result.signal_count()} signals, "
                f"{run.stats.count if run.stats else 0} simulated trades"
            ),
        })
    if run.stats is not None:
        d = run.stats.to_dict()
        checks.append({
            "check": "backtest",
            "passed": True,
            "detail": f"win {d['win_rate_pct']}% over {d['trades']} trades",
        })

    verdict = "pass" if all(c["passed"] for c in checks) else "fail"
    return {
        "verdict": verdict,
        "checks": checks,
        "summary": run.summary_text(),
        "diagnostics": [d.to_dict() for d in report.diagnostics],
    }


def save_script(source: str, path: str | Path, *, autofix: bool = True) -> Path:
    """Persist a (optionally corrected) script to disk."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = source
    if autofix:
        report = lint(source, autofix_enabled=True)
        text = report.fixed_source or source
    path.write_text(text, encoding="utf-8")
    return path
