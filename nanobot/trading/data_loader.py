"""Historical OHLCV loading with explicit local-file and Yahoo sources."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

import pandas as pd

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None


YAHOO_SYMBOLS = {
    "EURGBP": "EURGBP=X",
    "EURCAD": "EURCAD=X",
    "NZDCHF": "NZDCHF=X",
    "CADCHF": "CADCHF=X",
    "GBPCAD": "GBPCAD=X",
    "GBPCHF": "GBPCHF=X",
    "USDCAD": "USDCAD=X",
}


class DataUnavailableError(RuntimeError):
    """Raised when a requested instrument has no usable historical bars."""


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [str(col[0]).lower() for col in frame.columns]
    else:
        frame.columns = [str(col).split(".")[0].lower() for col in frame.columns]
    frame = frame.rename(columns={"adj close": "close"})
    frame = frame.loc[:, ~frame.columns.duplicated(keep="first")]
    required = ["open", "high", "low", "close"]
    missing = [col for col in required if col not in frame.columns]
    if missing:
        raise DataUnavailableError(f"OHLCV data is missing columns: {', '.join(missing)}")
    if "volume" not in frame.columns:
        frame["volume"] = 0.0
    frame = frame[["open", "high", "low", "close", "volume"]].copy()
    frame = frame.apply(pd.to_numeric, errors="coerce").dropna(subset=required)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")
    return frame


def load_local_csv(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise DataUnavailableError(f"Local data file does not exist: {path}")
    return _normalize_frame(pd.read_csv(path, index_col=0, parse_dates=True))


def _local_candidates(data_dir: Path, pair: str, timeframe: str) -> list[Path]:
    return [
        data_dir / f"{pair}_{timeframe}.csv",
        data_dir / f"{pair}.csv",
        data_dir / f"{YAHOO_SYMBOLS.get(pair, pair)}_{timeframe}.csv",
    ]


def load_pair(
    pair: str,
    start: str | None,
    end: str | None,
    timeframe: str,
    data_dir: str | Path | None = None,
) -> pd.DataFrame:
    pair = pair.upper().strip()
    if data_dir:
        data_path = next(
            (candidate for candidate in _local_candidates(Path(data_dir), pair, timeframe) if candidate.exists()),
            None,
        )
        if data_path:
            return load_local_csv(data_path)
        raise DataUnavailableError(
            f"No local CSV found for {pair}. Expected {Path(data_dir) / (pair + '_' + timeframe + '.csv')}"
        )
    if yf is None:
        raise DataUnavailableError("yfinance is not installed; provide --data-dir with CSV files")
    symbol = YAHOO_SYMBOLS.get(pair, pair)
    try:
        raw = yf.download(
            symbol,
            start=start,
            end=end,
            interval=timeframe,
            auto_adjust=False,
            progress=False,
            threads=False,
        )
    except Exception as exc:
        raise DataUnavailableError(f"Yahoo download failed for {pair}: {exc}") from exc
    if raw.empty:
        raise DataUnavailableError(
            f"Yahoo returned no bars for {pair}. Yahoo intraday history has retention limits; "
            "try a shorter range or provide --data-dir with broker-quality CSV files."
        )
    frame = _normalize_frame(raw)
    if start:
        frame = frame.loc[frame.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        frame = frame.loc[frame.index < pd.Timestamp(end, tz="UTC")]
    if frame.empty:
        raise DataUnavailableError(f"No bars remain for {pair} after applying the requested date range.")
    return frame


def load_market_data(
    pairs: tuple[str, ...] | list[str],
    start: str | None,
    end: str | None,
    timeframe: str = "1h",
    data_dir: str | Path | None = None,
) -> Mapping[str, pd.DataFrame]:
    data = {}
    failures = []
    for pair in pairs:
        try:
            data[pair.upper()] = load_pair(pair, start, end, timeframe, data_dir)
        except DataUnavailableError as exc:
            failures.append(str(exc))
    if failures:
        raise DataUnavailableError("\n".join(failures))
    if not data:
        raise DataUnavailableError("No instruments were requested.")
    return data
