"""Chart rendering for Pine results.

Two deliverables, both **without TradingView**:

* :func:`render_mplfinance` — server-side PNG candlestick chart via
  ``mplfinance``, with indicator overlays, Pine plot series, and signal markers.
  This is the primary, most reliable output.
* :func:`render_interactive_html` — a dependency-free, self-contained HTML file
  using TradingView's *open-source* Lightweight Charts library from a CDN, so
  users get zoom / pan / crosshair. Self-hosted, no TradingView account or
  widget involved.

Chart clarity rules applied to both:
* dark theme with high-contrast up/down candles,
* indicators offset into their own sub-panel (they are read on a different
  scale than price),
* signal markers labelled with direction, ETA and confidence,
* explicit axis labels, dates formatted, and a legend.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

# Indicators whose scale is close enough to price to share the main panel.
PRICE_SCALE_HINTS = (
    "sma", "ema", "wma", "hma", "vwap", "bb", "basis", "upper", "lower",
    "ma", "band", "vwma", "swma", "trend", "supertrend", "keltner", "donchian",
    "sar", "pivot", "hl2", "hlc3",
)


@dataclass
class ChartSpec:
    """What to draw."""

    symbol: str = "CHART"
    timeframe: str = "1d"
    title: str = "Pine Script"
    theme: str = "dark"
    last_bars: int = 250
    overlay_indicators: dict[str, pd.Series] | None = None
    panel_indicators: dict[str, pd.Series] | None = None
    condition_plots: dict[str, pd.Series] | None = None
    signals_long: pd.Series | None = None
    signals_short: pd.Series | None = None
    precursor: Any = None
    annotations: list[str] | None = None


def is_price_scale(name: str) -> bool:
    low = name.lower()
    return any(hint in low for hint in PRICE_SCALE_HINTS)


def split_plot_series(
    plots: dict[str, pd.Series], *, force_panel: bool = True
) -> tuple[dict[str, pd.Series], dict[str, pd.Series], dict[str, pd.Series]]:
    """Split Pine ``plot()`` outputs into three groups.

    Returns ``(overlay, panel, signals)`` where:

    * ``overlay`` — price-scale series (moving averages, bands) drawn on candles
    * ``panel``   — oscillator series drawn in their own sub-panel
    * ``signals`` — 0/1 condition plots (the result of ``plotshape``/``plotchar``)
      which are drawn as arrow markers rather than as lines, so they never
      clutter an indicator panel
    """
    overlay: dict[str, pd.Series] = {}
    panel: dict[str, pd.Series] = {}
    signals: dict[str, pd.Series] = {}
    for name, series in plots.items():
        if not isinstance(series, pd.Series):
            continue
        if _is_condition_series(series):
            signals[name] = series
            continue
        (overlay if is_price_scale(name) else panel)[name] = series
    if force_panel and not panel and not overlay and not signals:
        return {}, {}, {}
    return overlay, panel, signals


def _is_condition_series(series: pd.Series) -> bool:
    """True when a plotted series looks like a boolean condition (0/1 or bool)."""
    if series.dtype == bool:
        return True
    values = series.dropna()
    if values.empty:
        return False
    unique = set(values.unique().tolist())
    return unique.issubset({0.0, 1.0}) and len(unique) <= 2


def _slice_frame(data: pd.DataFrame, last_bars: int) -> pd.DataFrame:
    if last_bars and len(data) > last_bars:
        return data.iloc[-last_bars:]
    return data


def _align(series: pd.Series | None, index: pd.Index) -> pd.Series | None:
    if series is None:
        return None
    if not isinstance(series, pd.Series):
        return pd.Series(series, index=index)
    return series.reindex(index)


# --------------------------------------------------------------------------
# mplfinance renderer
# --------------------------------------------------------------------------


def render_mplfinance(
    data: pd.DataFrame,
    spec: ChartSpec,
    out_path: str | Path,
    *,
    dpi: int = 130,
    figsize: tuple[float, float] = (16.0, 9.5),
) -> Path:
    """Render a candlestick PNG with overlays, panels and signal markers."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    try:
        import mplfinance as mpf
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "mplfinance is required for image charts. Install with: pip install mplfinance"
        ) from exc

    frame = _slice_frame(data, spec.last_bars).copy()
    index = frame.index
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    dark = spec.theme != "light"
    if dark:
        face = "#0e1116"
        grid = "#2a323c"
        text = "#e6edf3"
        up, down = "#26a69a", "#ef5350"
    else:
        face = "#ffffff"
        grid = "#dfe3e8"
        text = "#1b1f24"
        up, down = "#0f8f7f", "#d32f2f"

    mc = mpf.make_marketcolors(up=up, down=down, edge="inherit", wick="inherit", volume="in")
    style = mpf.make_mpf_style(
        marketcolors=mc,
        facecolor=face,
        figcolor=face,
        gridcolor=grid,
        gridstyle="-",
        rc={
            "axes.labelcolor": text,
            "xtick.color": text,
            "ytick.color": text,
            "text.color": text,
            "font.size": 9,
        },
        y_on_right=True,
    )

    overlay = dict(spec.overlay_indicators or {})
    panel = dict(spec.panel_indicators or {})
    addplots: list[Any] = []
    palette = ["#f5c542", "#4fc3f7", "#ab47bc", "#66bb6a", "#ff7043", "#26c6da", "#d4e157", "#ec407a"]

    def _to_addplot(series: pd.Series, panel_id: int, color: str, label: str, width: float = 1.2):
        aligned = series.reindex(index)
        if aligned.dropna().empty:
            return None
        return mpf.make_addplot(
            aligned,
            panel=panel_id,
            color=color,
            width=width,
            label=label,
            ylabel="" if panel_id else "",
        )

    for i, (name, series) in enumerate(overlay.items()):
        if not isinstance(series, pd.Series):
            continue
        ap = _to_addplot(series, 0, palette[i % len(palette)], name)
        if ap is not None:
            addplots.append(ap)

    # Panel layout:
    #   0 = price + overlays
    #   1..k = indicator panels (RSI, MACD, ...)
    #   last = volume (own panel so it never collides with indicators)
    indicator_panels = list(panel.items())
    n_indicator_panels = len(indicator_panels)
    volume_panel = None
    has_volume = bool(
        "volume" in frame.columns and frame["volume"].fillna(0).abs().sum() > 0
    )
    if has_volume:
        # mplfinance's built-in volume=True does not coexist predictably with
        # addplot panels, so the volume histogram is added explicitly as its
        # own bottom panel. That keeps RSI/MACD never overlapping volume.
        volume_panel = 1 + n_indicator_panels
    total_panels = 1 + n_indicator_panels + (1 if has_volume else 0)

    for i, (name, series) in enumerate(indicator_panels):
        if not isinstance(series, pd.Series):
            continue
        ap = _to_addplot(series, 1 + i, palette[i % len(palette)], name)
        if ap is not None:
            addplots.append(ap)

    if volume_panel is not None:
        volume_colors = [
            (up if row["close"] >= row["open"] else down)
            for _, row in frame.iterrows()
        ]
        addplots.append(
            mpf.make_addplot(
                frame["volume"].fillna(0.0),
                panel=volume_panel,
                type="bar",
                color=volume_colors,
                alpha=0.55,
                width=0.7,
                ylabel="Volume",
            )
        )

    long_mask = _align(spec.signals_long, index)
    short_mask = _align(spec.signals_short, index)
    # Markers live on the price panel when it is the only panel; otherwise on
    # the first indicator panel, which keeps the candles readable.
    marker_panel = 1 if n_indicator_panels else 0

    # Plotshape/plotchar conditions render as markers, not as noisy lines.
    condition_markers = 0
    for name, series in (spec.condition_plots or {}).items():
        aligned = series.reindex(index).fillna(False)
        mask = aligned.astype(bool)
        if not mask.any():
            continue
        if condition_markers == 0 and long_mask is not None and long_mask.fillna(False).any():
            continue  # strategy entry markers already cover this
        marker_series = pd.Series(np.nan, index=index, dtype="float64")
        base = frame["low"] * 0.99 if marker_panel == 0 else frame["close"]
        marker_series[mask] = base[mask]
        addplots.append(
            mpf.make_addplot(
                marker_series,
                panel=marker_panel,
                type="scatter",
                marker="^",
                markersize=70,
                color="#00e676",
                label=str(name),
            )
        )
        condition_markers += 1
        if condition_markers >= 3:
            break

    if long_mask is not None and long_mask.fillna(False).any():
        marker_series = pd.Series(np.nan, index=index, dtype="float64")
        base = frame["low"] * 0.995 if marker_panel == 0 else frame["close"]
        marker_series[long_mask.fillna(False)] = base[long_mask.fillna(False)]
        ap = mpf.make_addplot(
            marker_series,
            panel=marker_panel,
            type="scatter",
            marker="^",
            markersize=90,
            color="#00e676",
            label="LONG",
        )
        addplots.append(ap)

    if short_mask is not None and short_mask.fillna(False).any():
        marker_series = pd.Series(np.nan, index=index, dtype="float64")
        base = frame["high"] * 1.005 if marker_panel == 0 else frame["close"]
        marker_series[short_mask.fillna(False)] = base[short_mask.fillna(False)]
        ap = mpf.make_addplot(
            marker_series,
            panel=marker_panel,
            type="scatter",
            marker="v",
            markersize=90,
            color="#ff1744",
            label="SHORT",
        )
        addplots.append(ap)

    plot_kwargs: dict[str, Any] = {
        "type": "candle",
        "style": style,
        "addplot": addplots or None,
        "volume": False,
        "panel_ratios": tuple(
            [4] + [1.5] * n_indicator_panels + ([1.1] if has_volume else [])
        ),
        "figsize": figsize,
        "figratio": (16, 9),
        "title": f"{spec.symbol} · {spec.timeframe} · {spec.title}",
        "ylabel": "Price",
        "ylabel_lower": "",
        "tight_layout": True,
        "returnfig": True,
        "warn_too_much_data": 100000,
    }
    _ = total_panels
    fig, axes = mpf.plot(frame, **plot_kwargs)

    # width of the last N bars for scaling annotations
    n = len(frame)
    for ax in np.atleast_1d(axes):
        ax.grid(True, alpha=0.25, linestyle="-", linewidth=0.6)
        try:
            locator = mdates.AutoDateLocator(minticks=6, maxticks=12)
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        except Exception:  # pragma: no cover
            pass

    # precursor annotation
    if spec.precursor is not None and getattr(spec.precursor, "direction", 0):
        pre = spec.precursor
        side = "LONG" if pre.direction > 0 else "SHORT"
        color = "#00e676" if pre.direction > 0 else "#ff1744"
        text = (
            f"PRECURSOR {side}\nETA {pre.eta_bars} bar(s) ~{pre.eta_minutes:g} min\n"
            f"confidence {pre.confidence:.0%}"
        )
        fig.text(
            0.012,
            0.02,
            text,
            color=color,
            fontsize=11,
            fontweight="bold",
            va="bottom",
            ha="left",
            bbox=dict(facecolor="#141a21" if dark else "#f5f5f5", edgecolor=color, alpha=0.95, pad=6),
        )

    if spec.annotations:
        for i, note in enumerate(spec.annotations[:6]):
            fig.text(0.012, 0.13 + i * 0.032, note, color=text, fontsize=8.5, va="bottom")
    _ = n

    fig.savefig(out_path, dpi=dpi, facecolor=face, bbox_inches="tight")
    plt.close(fig)
    return out_path


def enforce_signal_panel(panel: dict[str, pd.Series]) -> bool:
    """Signals always need a panel of their own when no indicator panel exists."""
    return not panel


# --------------------------------------------------------------------------
# interactive HTML renderer
# --------------------------------------------------------------------------


def render_interactive_html(
    data: pd.DataFrame,
    spec: ChartSpec,
    out_path: str | Path,
) -> Path:
    """Write a self-contained interactive chart (Lightweight Charts, CDN)."""
    frame = _slice_frame(data, spec.last_bars)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _time_label(ts) -> Any:
        if isinstance(ts, pd.Timestamp):
            return int(ts.timestamp())
        return str(ts)

    candles = [
        {
            "time": _time_label(ts),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        for ts, row in frame.iterrows()
    ]
    volumes = [
        {
            "time": _time_label(ts),
            "value": float(row.get("volume", 0) or 0),
            "color": "#26a69a55" if row["close"] >= row["open"] else "#ef535055",
        }
        for ts, row in frame.iterrows()
    ]

    overlay_series = []
    palette = ["#f5c542", "#4fc3f7", "#ab47bc", "#66bb6a", "#ff7043", "#26c6da"]
    for i, (name, series) in enumerate((spec.overlay_indicators or {}).items()):
        if not isinstance(series, pd.Series):
            continue
        aligned = series.reindex(frame.index).astype("float64")
        points = [
            {"time": _time_label(ts), "value": float(val)}
            for ts, val in aligned.items()
            if pd.notna(val)
        ]
        if points:
            overlay_series.append({"name": name, "color": palette[i % len(palette)], "data": points})

    markers: list[dict[str, Any]] = []
    long_mask = _align(spec.signals_long, frame.index)
    short_mask = _align(spec.signals_short, frame.index)
    if long_mask is not None:
        for ts, flag in long_mask.fillna(False).items():
            if flag:
                markers.append({
                    "time": _time_label(ts),
                    "position": "belowBar",
                    "color": "#00e676",
                    "shape": "arrowUp",
                    "text": "LONG",
                })
    if short_mask is not None:
        for ts, flag in short_mask.fillna(False).items():
            if flag:
                markers.append({
                    "time": _time_label(ts),
                    "position": "aboveBar",
                    "color": "#ff1744",
                    "shape": "arrowDown",
                    "text": "SHORT",
                })
    markers.sort(key=lambda m: m["time"])

    precursor = spec.precursor
    banner = ""
    if precursor is not None and getattr(precursor, "direction", 0):
        side = "LONG" if precursor.direction > 0 else "SHORT"
        banner = (
            f"PRECURSOR {side} · ETA {precursor.eta_bars} bar(s) "
            f"(~{precursor.eta_minutes:g} min) · confidence {precursor.confidence:.0%}"
        )

    payload = {
        "candles": candles,
        "volumes": volumes,
        "overlays": overlay_series,
        "markers": markers,
        "symbol": spec.symbol,
        "timeframe": spec.timeframe,
        "title": spec.title,
        "banner": banner,
    }
    blob = json.dumps(payload).replace("</", "<\\/")

    html = _HTML_TEMPLATE.replace("__PAYLOAD__", blob).replace("__THEME__", spec.theme)
    out_path.write_text(html, encoding="utf-8")
    return out_path


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" data-theme="__THEME__">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Chart</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root { --bg:#0e1116; --panel:#141a21; --text:#e6edf3; --muted:#8b949e; --grid:#222b36; }
  html[data-theme="light"] { --bg:#ffffff; --panel:#f6f8fa; --text:#1b1f24; --muted:#57606a; --grid:#e3e8ee; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family: ui-sans-serif, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  header { display:flex; align-items:center; gap:16px; padding:14px 18px; border-bottom:1px solid var(--grid); flex-wrap:wrap; }
  h1 { font-size:15px; margin:0; font-weight:600; letter-spacing:.2px; }
  .pill { font-size:12px; color:var(--muted); border:1px solid var(--grid);
          border-radius:999px; padding:3px 10px; }
  .banner { margin-left:auto; font-weight:700; font-size:13px; padding:6px 12px; border-radius:8px;
            border:1px solid currentColor; }
  #chart { width:100%; height:calc(100vh - 118px); }
  footer { padding:8px 18px; font-size:11.5px; color:var(--muted); border-top:1px solid var(--grid); }
  .err { padding:24px; color:#ff8a80; font-size:13px; }
</style>
</head>
<body>
<header>
  <h1 id="title">Chart</h1>
  <span class="pill" id="symbol"></span>
  <span class="pill" id="tf"></span>
  <span class="banner" id="banner" style="display:none"></span>
</header>
<div id="chart"></div>
<footer>Rendered locally by the Pine engine (mplfinance + Lightweight Charts). Not TradingView.</footer>
<script>
const PAYLOAD = __PAYLOAD__;
(function () {
  if (typeof LightweightCharts === "undefined") {
    document.getElementById("chart").innerHTML =
      '<div class="err">Interactive chart library could not load (offline?). ' +
      'Use the PNG chart instead.</div>';
    return;
  }
  const dark = document.documentElement.getAttribute("data-theme") !== "light";
  document.getElementById("title").textContent = PAYLOAD.title || "Chart";
  document.getElementById("symbol").textContent = PAYLOAD.symbol || "";
  document.getElementById("tf").textContent = PAYLOAD.timeframe || "";
  if (PAYLOAD.banner) {
    const b = document.getElementById("banner");
    b.textContent = PAYLOAD.banner;
    b.style.display = "inline-block";
    b.style.color = PAYLOAD.banner.indexOf("LONG") >= 0 ? "#00e676" : "#ff1744";
  }
  const chart = LightweightCharts.createChart(document.getElementById("chart"), {
    layout: { background: { color: dark ? "#0e1116" : "#ffffff" },
              textColor: dark ? "#e6edf3" : "#1b1f24" },
    grid: { vertLines: { color: dark ? "#222b36" : "#e3e8ee" },
            horzLines: { color: dark ? "#222b36" : "#e3e8ee" } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: dark ? "#222b36" : "#e3e8ee" },
    timeScale: { borderColor: dark ? "#222b36" : "#e3e8ee", timeVisible: true, secondsVisible: false },
  });
  const candles = chart.addCandlestickSeries({
    upColor: "#26a69a", downColor: "#ef5350",
    borderUpColor: "#26a69a", borderDownColor: "#ef5350",
    wickUpColor: "#26a69a", wickDownColor: "#ef5350",
  });
  candles.setData(PAYLOAD.candles);
  const vol = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "" });
  vol.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  vol.setData(PAYLOAD.volumes);
  (PAYLOAD.overlays || []).forEach(function (o) {
    const line = chart.addLineSeries({ color: o.color, lineWidth: 2, title: o.name,
                                       priceLineVisible: false, lastValueVisible: false });
    line.setData(o.data);
  });
  if (PAYLOAD.markers && PAYLOAD.markers.length) candles.setMarkers(PAYLOAD.markers);
  chart.timeScale().fitContent();
  new ResizeObserver(function () {
    chart.applyOptions({ width: document.getElementById("chart").clientWidth });
  }).observe(document.getElementById("chart"));
})();
</script>
</body>
</html>
"""
