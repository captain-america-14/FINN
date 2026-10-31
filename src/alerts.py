import json
import sqlite3

import pandas as pd

from src.announcements import get_announcements
from src.config import (
    ALERT_FILING_LOOKBACK_DAYS,
    AVG_VOLUME_WINDOW,
    AVG_WINDOW_RANGE,
    PRICE_ALERT_PCT,
    PRICE_PCT_RANGE,
    VOLUME_ALERT_RATIO,
    VOLUME_RATIO_RANGE,
)
from src.db import get_connection

SETTINGS_KEY = "alert_config"


# ---------- Validation ----------


def _clamp(value, low, high, fallback):
    """Convert to float and force into [low, high]. Garbage falls back to a default."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number:  # NaN
        return fallback
    return min(max(number, low), high)


def sanitize_config(raw) -> dict:
    """Return a safe, complete alert config from whatever was stored or submitted."""
    raw = raw if isinstance(raw, dict) else {}
    cfg = {
        "price_pct": _clamp(raw.get("price_pct"), *PRICE_PCT_RANGE, PRICE_ALERT_PCT),
        "volume_ratio": _clamp(raw.get("volume_ratio"), *VOLUME_RATIO_RANGE, VOLUME_ALERT_RATIO),
        "avg_window": int(_clamp(raw.get("avg_window"), *AVG_WINDOW_RANGE, AVG_VOLUME_WINDOW)),
        "overrides": {},
    }
    overrides = raw.get("overrides")
    if isinstance(overrides, dict):
        for ticker, override in overrides.items():
            if not isinstance(override, dict):
                continue
            clean = {}
            for key, bounds in (("price_pct", PRICE_PCT_RANGE), ("volume_ratio", VOLUME_RATIO_RANGE)):
                if key in override:
                    clean[key] = _clamp(override[key], *bounds, cfg[key])
            if clean:
                cfg["overrides"][str(ticker).strip().upper()] = clean
    return cfg


def thresholds_for(cfg: dict, ticker: str):
    """(price %, volume ratio) that apply to this stock, honouring any override."""
    override = cfg["overrides"].get(ticker.strip().upper(), {})
    return (
        override.get("price_pct", cfg["price_pct"]),
        override.get("volume_ratio", cfg["volume_ratio"]),
    )


# ---------- Persistence (SQLite 'settings' table) ----------


def _ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def load_alert_config() -> dict:
    """Saved config, or the defaults if nothing is saved or it can't be read."""
    raw = {}
    try:
        with get_connection() as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY,)
            ).fetchone()
        if row:
            raw = json.loads(row[0])
    except (sqlite3.Error, json.JSONDecodeError):
        raw = {}
    return sanitize_config(raw)


def save_alert_config(cfg: dict) -> dict:
    clean = sanitize_config(cfg)
    with get_connection() as conn:
        _ensure_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (SETTINGS_KEY, json.dumps(clean)),
        )
    return clean


def reset_alert_config() -> None:
    with get_connection() as conn:
        _ensure_table(conn)
        conn.execute("DELETE FROM settings WHERE key = ?", (SETTINGS_KEY,))


# ---------- Evaluation ----------


def evaluate_alerts(table: pd.DataFrame, cfg: dict) -> list:
    """One alert per flagged stock, most unusual first.

    `table` comes from build_snapshot_table(), which already applied each stock's thresholds.
    """
    alerts = []
    for _, row in table[table["has_data"]].iterrows():
        reasons = []
        severity = 0.0
        if row["price_alert"]:
            reasons.append(
                f"⚡ Price {row['change_pct']:+.2f}% vs prior close "
                f"(alert at ±{row['price_threshold']:g}%)"
            )
            severity = max(severity, abs(row["change_pct"]) / row["price_threshold"])
        if row["volume_alert"]:
            reasons.append(
                f"📊 Volume {row['volume_ratio']:.1f}× the {cfg['avg_window']}-day average "
                f"(alert at {row['volume_threshold']:g}×)"
            )
            severity = max(severity, row["volume_ratio"] / row["volume_threshold"])
        if reasons:
            alerts.append(
                {
                    "ticker": row["ticker"],
                    "name": row["name"],
                    "reasons": reasons,
                    "severity": severity,
                    "change_pct": row["change_pct"],
                    "as_of": row["as_of"],
                }
            )
    alerts.sort(key=lambda a: a["severity"], reverse=True)
    return alerts


def related_filings(tickers: list, days: int = ALERT_FILING_LOOKBACK_DAYS) -> pd.DataFrame:
    """Recent High/Medium filings for the flagged tickers (co-occurrence, not causation)."""
    if not tickers:
        return pd.DataFrame()
    return get_announcements(tickers, days=days, priorities=["High", "Medium"])


# ---------- Self-test (no network, no database) ----------


def _make_df(last_close: float, last_volume: float) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=30)
    close = [100.0] * 29 + [last_close]
    volume = [1000] * 29 + [last_volume]
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": volume},
        index=dates,
    )


def run_self_test() -> None:
    from src.market_data import build_snapshot_table

    # 1. Bad input is clamped or replaced, never trusted
    messy = sanitize_config(
        {
            "price_pct": 999,
            "volume_ratio": "abc",
            "avg_window": 2,
            "overrides": {"wipro": {"price_pct": 0.01}, "bad": "x"},
        }
    )
    assert messy["price_pct"] == PRICE_PCT_RANGE[1]
    assert messy["volume_ratio"] == VOLUME_ALERT_RATIO
    assert messy["avg_window"] == AVG_WINDOW_RANGE[0]
    assert messy["overrides"] == {"WIPRO": {"price_pct": PRICE_PCT_RANGE[0]}}

    # 2. Overrides win for their stock only
    cfg = sanitize_config(
        {"price_pct": 3, "volume_ratio": 2, "avg_window": 20, "overrides": {"BBB": {"price_pct": 5}}}
    )
    assert thresholds_for(cfg, "AAA") == (3.0, 2.0)
    assert thresholds_for(cfg, "bbb") == (5.0, 2.0)

    # 3. End to end with synthetic prices:
    #    AAA +4% (alerts at 3%), BBB +4% (override 5%, so no alert), CCC flat price but 2.5x volume
    companies = pd.DataFrame(
        {"ticker": ["AAA", "BBB", "CCC"], "name": ["Alpha", "Beta", "Gamma"], "shares": [10, 10, 10]}
    )
    prices = {
        "AAA": {"df": _make_df(104, 1000), "stale": False},
        "BBB": {"df": _make_df(104, 1000), "stale": False},
        "CCC": {"df": _make_df(100, 2500), "stale": False},
    }
    table = build_snapshot_table(companies, prices, cfg)
    alerts = evaluate_alerts(table, cfg)
    assert [a["ticker"] for a in alerts] == ["AAA", "CCC"], [a["ticker"] for a in alerts]
    print("Alerts:", [(a["ticker"], round(a["severity"], 2)) for a in alerts])
    print("All alert tests passed.")


if __name__ == "__main__":
    run_self_test()