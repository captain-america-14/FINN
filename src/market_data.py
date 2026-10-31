import logging

import pandas as pd
import yfinance as yf

from src.alerts import load_alert_config, thresholds_for
from src.config import PRICE_CACHE_DIR, PRICE_PERIOD
from src.metrics import compute_snapshot
from src.portfolio import to_yf_symbol

logger = logging.getLogger(__name__)

PRICE_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def _empty_prices() -> pd.DataFrame:
    return pd.DataFrame(columns=PRICE_COLUMNS)


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only OHLCV, drop rows with no close, and use a plain date index."""
    df = df.reindex(columns=PRICE_COLUMNS).dropna(subset=["Close"])
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df.index.name = "Date"
    return df.sort_index()


def _extract_symbol(raw, symbol: str) -> pd.DataFrame:
    """Pull one symbol's data out of yfinance's (possibly multi-level) result."""
    if raw is None or raw.empty:
        return _empty_prices()
    if isinstance(raw.columns, pd.MultiIndex):
        if symbol not in raw.columns.get_level_values(0):
            return _empty_prices()
        df = raw[symbol]
    else:
        df = raw
    return _clean(df)


def _cache_path(ticker: str):
    return PRICE_CACHE_DIR / f"{ticker}.csv"


def _save_cache(ticker: str, df: pd.DataFrame) -> None:
    PRICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(_cache_path(ticker))


def _load_cache(ticker: str) -> pd.DataFrame:
    path = _cache_path(ticker)
    if not path.exists():
        return _empty_prices()
    try:
        df = pd.read_csv(path, index_col="Date", parse_dates=True)
        return _clean(df)
    except Exception as err:
        logger.warning("Could not read cache for %s: %s", ticker, err)
        return _empty_prices()


def fetch_prices(tickers: list, period: str = PRICE_PERIOD) -> dict:
    """Download daily prices for many NSE tickers in one request.

    Returns {ticker: {"df": DataFrame, "stale": bool}}.
    If live data is unavailable, falls back to the last saved copy (stale=True).
    """
    tickers = [t.strip().upper() for t in tickers]
    symbols = [to_yf_symbol(t) for t in tickers]

    raw = None
    try:
        raw = yf.download(
            symbols,
            period=period,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as err:
        logger.warning("yfinance download failed: %s", err)

    results = {}
    for ticker, symbol in zip(tickers, symbols):
        df = _extract_symbol(raw, symbol)
        if not df.empty:
            _save_cache(ticker, df)
            results[ticker] = {"df": df, "stale": False}
        else:
            results[ticker] = {"df": _load_cache(ticker), "stale": True}
    return results


def build_snapshot_table(
    companies: pd.DataFrame, prices: dict = None, alert_config: dict = None
) -> pd.DataFrame:
    """One row per company with the latest price/volume metrics and alert flags.

    prices: output of fetch_prices(). If None, it is fetched here.
    alert_config: saved thresholds. If None, the saved config is loaded.
    """
    cfg = alert_config or load_alert_config()
    if prices is None:
        prices = fetch_prices(companies["ticker"].tolist())

    rows = []
    for _, company in companies.iterrows():
        ticker = company["ticker"]
        entry = prices.get(ticker)
        price_pct, volume_ratio = thresholds_for(cfg, ticker)
        snap = None
        if entry:
            snap = compute_snapshot(
                entry["df"],
                avg_window=cfg["avg_window"],
                price_alert_pct=price_pct,
                volume_alert_ratio=volume_ratio,
            )
        row = {
            "ticker": ticker,
            "name": company["name"],
            "shares": company["shares"],
            "stale": entry["stale"] if entry else True,
            "has_data": snap is not None,
            "price_alert": False,
            "volume_alert": False,
            "price_threshold": price_pct,
            "volume_threshold": volume_ratio,
        }
        if snap:
            row.update(snap)
        rows.append(row)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import time

    # Includes a ticker that is NOT in our CSV, to prove the engine generalises
    sample = ["RELIANCE", "INFY", "TMPV", "POLYCAB"]
    start = time.time()
    results = fetch_prices(sample)
    print(f"Fetched {len(sample)} tickers in {time.time() - start:.1f}s")
    for ticker, entry in results.items():
        snap = compute_snapshot(entry["df"])
        status = "STALE (cache)" if entry["stale"] else "live"
        brief = None
        if snap:
            brief = {k: snap[k] for k in ("as_of", "close", "change_pct", "volume_ratio")}
        print(ticker, status, f"{len(entry['df'])} rows", brief)