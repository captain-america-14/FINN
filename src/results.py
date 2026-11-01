import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from src import screener
from src.config import RESULTS_REFRESH_HOURS, SCREENER_PAUSE_SEC
from src.db import get_connection

logger = logging.getLogger(__name__)

METRIC_COLUMNS = [
    "revenue",
    "expenses",
    "ebitda",
    "ebitda_margin_pct",
    "other_income",
    "interest",
    "depreciation",
    "pbt",
    "tax_pct",
    "pat",
    "eps",
]
GROWTH_METRICS = ["revenue", "ebitda", "pbt", "pat"]
STORED_COLUMNS = (
    ["ticker", "basis", "period_end", "period"]
    + METRIC_COLUMNS
    + ["is_financial", "unit", "source_url", "fetched_at"]
)

RESULTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS quarterly_results (
    ticker             TEXT NOT NULL,
    basis              TEXT NOT NULL,
    period_end         TEXT NOT NULL,
    period             TEXT,
    revenue            REAL,
    expenses           REAL,
    ebitda             REAL,
    ebitda_margin_pct  REAL,
    other_income       REAL,
    interest           REAL,
    depreciation       REAL,
    pbt                REAL,
    tax_pct            REAL,
    pat                REAL,
    eps                REAL,
    is_financial       INTEGER NOT NULL DEFAULT 0,
    unit               TEXT NOT NULL,
    source_url         TEXT NOT NULL,
    fetched_at         TEXT NOT NULL,
    PRIMARY KEY (ticker, basis, period_end)
);
CREATE TABLE IF NOT EXISTS results_fetch_log (
    ticker      TEXT PRIMARY KEY,
    fetched_at  TEXT NOT NULL
);
"""

UPSERT_SQL = (
    f"INSERT OR REPLACE INTO quarterly_results ({', '.join(STORED_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in STORED_COLUMNS)})"
)


@contextmanager
def _db():
    with get_connection() as conn:
        conn.executescript(RESULTS_SCHEMA)
        yield conn


# ---------- Storage ----------


def store_bundle(ticker: str, bundle: dict) -> int:
    fetched_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    for record in bundle["records"]:
        row = {col: record.get(col) for col in METRIC_COLUMNS}
        row.update(
            {
                "ticker": ticker,
                "basis": bundle["basis"],
                "period_end": record["period_end"],
                "period": record["period"],
                "is_financial": int(bundle["is_financial"]),
                "unit": bundle["unit"],
                "source_url": bundle["url"],
                "fetched_at": fetched_at,
            }
        )
        rows.append(row)
    with _db() as conn:
        conn.executemany(UPSERT_SQL, rows)
    return len(rows)


def _is_fresh(ticker: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT fetched_at FROM results_fetch_log WHERE ticker = ?", (ticker,)
        ).fetchone()
    if not row:
        return False
    age = datetime.now() - datetime.fromisoformat(row[0])
    return age < timedelta(hours=RESULTS_REFRESH_HOURS)


def _mark_fetched(ticker: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO results_fetch_log (ticker, fetched_at) VALUES (?, ?)",
            (ticker, datetime.now().isoformat(timespec="seconds")),
        )


def refresh_results(companies: pd.DataFrame, force: bool = False) -> dict:
    """Fetch and store quarterly results for each company (both bases when available)."""
    summary = {"checked": 0, "stored_quarters": 0, "skipped_fresh": 0, "errors": []}
    for _, company in companies.iterrows():
        ticker = company["ticker"]
        if not force and _is_fresh(ticker):
            summary["skipped_fresh"] += 1
            continue
        try:
            bundles = screener.fetch_company_results(ticker)
            for bundle in bundles.values():
                summary["stored_quarters"] += store_bundle(ticker, bundle)
            _mark_fetched(ticker)
            summary["checked"] += 1
        except Exception as err:
            logger.warning("Results fetch failed for %s: %s", ticker, err)
            summary["errors"].append((ticker, str(err)))
        time.sleep(SCREENER_PAUSE_SEC)
    return summary


def available_bases(ticker: str) -> list:
    with _db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT basis FROM quarterly_results WHERE ticker = ?", (ticker,)
        ).fetchall()
    return sorted((r[0] for r in rows), key=lambda b: 0 if b == "consolidated" else 1)


def ebitda_label(is_financial: bool) -> str:
    return "Financing profit" if is_financial else "EBITDA"


# ---------- Growth maths ----------


def _pct_change(current: np.ndarray, previous: np.ndarray) -> np.ndarray:
    """% change; NaN when there is no prior figure or it is zero/negative (not meaningful)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where((previous > 0) & ~np.isnan(current), (current / previous - 1) * 100, np.nan)


def compute_growth(df: pd.DataFrame) -> pd.DataFrame:
    """Add QoQ and YoY growth columns, matching quarters by calendar date, not row position."""
    df = df.sort_values("period_end").reset_index(drop=True).copy()
    ends = pd.DatetimeIndex(pd.to_datetime(df["period_end"]))

    def series(column: str) -> pd.Series:
        values = pd.to_numeric(df[column], errors="coerce").to_numpy(dtype=float)
        return pd.Series(values, index=ends)

    def shifted(s: pd.Series, months: int) -> np.ndarray:
        return s.reindex(ends - pd.offsets.MonthEnd(months)).to_numpy(dtype=float)

    for metric in GROWTH_METRICS:
        if metric not in df.columns:
            continue
        s = series(metric)
        current = s.to_numpy(dtype=float)
        df[f"{metric}_qoq"] = _pct_change(current, shifted(s, 3))
        df[f"{metric}_yoy"] = _pct_change(current, shifted(s, 12))

    if "ebitda_margin_pct" in df.columns:
        margin = series("ebitda_margin_pct")
        df["ebitda_margin_yoy_pp"] = margin.to_numpy(dtype=float) - shifted(margin, 12)

    if "pat" in df.columns and "revenue" in df.columns:
        revenue = series("revenue").to_numpy(dtype=float)
        pat = series("pat").to_numpy(dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            df["pat_margin_pct"] = np.where(revenue > 0, pat / revenue * 100, np.nan)
    return df


def get_growth_table(ticker: str, basis: str) -> pd.DataFrame:
    with _db() as conn:
        df = pd.read_sql_query(
            "SELECT * FROM quarterly_results WHERE ticker = ? AND basis = ? ORDER BY period_end",
            conn,
            params=[ticker, basis],
        )
    return compute_growth(df) if not df.empty else df


# ---------- Self-test (no network, no database) and bulk fetch ----------


def run_self_test() -> None:
    ends = ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]
    revenue = [100, 110, 120, 130, 150]
    pat = [-10, 5, 6, 8, 12]
    df = pd.DataFrame(
        {
            "period_end": ends,
            "revenue": revenue,
            "ebitda": [r * 0.2 for r in revenue],
            "ebitda_margin_pct": [20.0] * 5,
            "pbt": [p * 1.3 for p in pat],
            "pat": pat,
        }
    )
    g = compute_growth(df)
    last = g.iloc[-1]
    assert abs(last["revenue_qoq"] - (150 / 130 - 1) * 100) < 1e-9
    assert abs(last["revenue_yoy"] - 50.0) < 1e-9
    assert abs(last["pat_qoq"] - 50.0) < 1e-9          # 12 vs 8
    assert pd.isna(last["pat_yoy"])                    # a year ago PAT was negative -> n/m
    assert pd.isna(g.iloc[0]["revenue_qoq"])           # no earlier quarter
    assert pd.isna(g.iloc[1]["pat_qoq"])               # previous PAT negative -> n/m
    assert abs(last["ebitda_margin_yoy_pp"]) < 1e-9
    assert abs(last["pat_margin_pct"] - 8.0) < 1e-9

    # A missing quarter must not be papered over with the wrong comparison
    gap = compute_growth(df[df["period_end"] != "2026-03-31"])
    jun = gap[gap["period_end"] == "2026-06-30"].iloc[0]
    assert pd.isna(jun["revenue_qoq"]) and pd.isna(jun["revenue_yoy"])
    sep = gap[gap["period_end"] == "2026-09-30"].iloc[0]
    assert abs(sep["revenue_qoq"] - (150 / 130 - 1) * 100) < 1e-9
    print("All growth tests passed.")


def _fmt(value) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{value:,.1f}"


def run_fetch_all() -> None:
    from src.portfolio import load_universe

    universe = load_universe()
    summary = refresh_results(universe, force=True)
    print("Summary:", summary)
    for ticker in universe["ticker"]:
        for basis in available_bases(ticker):
            table = get_growth_table(ticker, basis)
            last = table.iloc[-1]
            tag = " [financial]" if last["is_financial"] else ""
            print(
                f"{ticker:<11} {basis:<12} {len(table):>2} qtrs  latest {last['period']:<9}"
                f" revenue {_fmt(last['revenue']):>12}  YoY {_fmt(last['revenue_yoy']):>6}%"
                f"  PAT {_fmt(last['pat']):>10}{tag}"
            )


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "all":
        run_fetch_all()
    else:
        run_self_test()