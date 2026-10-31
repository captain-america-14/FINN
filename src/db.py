import sqlite3
from contextlib import contextmanager
from datetime import datetime

import pandas as pd

from src.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    news_id      TEXT PRIMARY KEY,
    ticker       TEXT NOT NULL,
    company      TEXT,
    filing_dt    TEXT,
    category     TEXT,
    subcategory  TEXT,
    title        TEXT,
    detail       TEXT,
    filing_url   TEXT,
    page_url     TEXT,
    source       TEXT NOT NULL DEFAULT 'BSE',
    fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ann_ticker_dt ON announcements (ticker, filing_dt);
CREATE TABLE IF NOT EXISTS fetch_log (
    ticker      TEXT PRIMARY KEY,
    fetched_at  TEXT NOT NULL
);
"""

INSERT_SQL = """
INSERT OR IGNORE INTO announcements
    (news_id, ticker, company, filing_dt, category, subcategory,
     title, detail, filing_url, page_url, source, fetched_at)
VALUES
    (:news_id, :ticker, :company, :filing_dt, :category, :subcategory,
     :title, :detail, :filing_url, :page_url, :source, :fetched_at)
"""


@contextmanager
def get_connection():
    """Open the database, make sure tables exist, commit on success, always close."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_announcements(rows: list) -> int:
    """Insert announcements, ignoring ones we already have. Returns how many were new."""
    if not rows:
        return 0
    with get_connection() as conn:
        before = conn.total_changes
        conn.executemany(INSERT_SQL, rows)
        return conn.total_changes - before


def mark_fetched(ticker: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fetch_log (ticker, fetched_at) VALUES (?, ?)",
            (ticker, datetime.now().isoformat(timespec="seconds")),
        )


def last_fetched(ticker: str):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT fetched_at FROM fetch_log WHERE ticker = ?", (ticker,)
        ).fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def read_announcements(tickers=None, since=None) -> pd.DataFrame:
    """Read stored announcements, newest first. `since` is 'YYYY-MM-DD HH:MM:SS'."""
    query = "SELECT * FROM announcements WHERE 1=1"
    params = []
    if tickers:
        query += f" AND ticker IN ({','.join('?' * len(tickers))})"
        params.extend(tickers)
    if since:
        query += " AND filing_dt >= ?"
        params.append(since)
    query += " ORDER BY filing_dt DESC"
    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=params)
    return df.fillna("")