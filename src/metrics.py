import numpy as np
import pandas as pd

from src.config import AVG_VOLUME_WINDOW, PRICE_ALERT_PCT, VOLUME_ALERT_RATIO


def daily_returns(df: pd.DataFrame) -> pd.Series:
    """Daily % return for every day in the history."""
    return df["Close"].pct_change() * 100


def compute_snapshot(
    df: pd.DataFrame,
    avg_window: int = AVG_VOLUME_WINDOW,
    price_alert_pct: float = PRICE_ALERT_PCT,
    volume_alert_ratio: float = VOLUME_ALERT_RATIO,
):
    """Summarise the latest trading day. Returns None if there isn't enough data."""
    if df is None or len(df) < 2:
        return None

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(latest["Close"])
    prev_close = float(prev["Close"])
    if prev_close == 0:
        return None

    change_pct = (close / prev_close - 1) * 100

    # Baseline excludes the latest day, so a spike can't inflate its own average
    past_volumes = df["Volume"].iloc[-(avg_window + 1):-1].dropna()
    avg_volume = float(past_volumes.mean()) if len(past_volumes) > 0 else np.nan
    volume = float(latest["Volume"]) if pd.notna(latest["Volume"]) else np.nan

    if pd.notna(avg_volume) and avg_volume > 0 and pd.notna(volume):
        volume_ratio = volume / avg_volume
    else:
        volume_ratio = np.nan

    return {
        "as_of": df.index[-1].date(),
        "open": float(latest["Open"]),
        "high": float(latest["High"]),
        "low": float(latest["Low"]),
        "close": close,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "volume": volume,
        "avg_volume": avg_volume,
        "volume_ratio": volume_ratio,
        "price_alert": abs(change_pct) >= price_alert_pct,
        "volume_alert": bool(pd.notna(volume_ratio) and volume_ratio >= volume_alert_ratio),
    }


def portfolio_totals(table: pd.DataFrame) -> dict:
    """Total demo-portfolio value and today's change, using only stocks with data."""
    valid = table[table["has_data"]]
    value = float((valid["shares"] * valid["close"]).sum())
    day_change = float((valid["shares"] * (valid["close"] - valid["prev_close"])).sum())
    prev_value = value - day_change
    day_change_pct = (day_change / prev_value * 100) if prev_value else 0.0
    return {"value": value, "day_change": day_change, "day_change_pct": day_change_pct}


if __name__ == "__main__":
    # Known-answer test: flat price of 100 then +3%, volume 1000 then 2500
    dates = pd.bdate_range("2026-01-01", periods=30)
    close = [100.0] * 29 + [103.0]
    volume = [1000] * 29 + [2500]
    df = pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": volume},
        index=dates,
    )
    snap = compute_snapshot(df)
    assert abs(snap["change_pct"] - 3.0) < 1e-9
    assert abs(snap["avg_volume"] - 1000) < 1e-9
    assert abs(snap["volume_ratio"] - 2.5) < 1e-9
    assert snap["price_alert"] and snap["volume_alert"]
    assert compute_snapshot(df.iloc[:1]) is None
    print("All metric checks passed:", snap)