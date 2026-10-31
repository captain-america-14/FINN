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
CREATE TABLE IF NOT EXISTS classifications (
    news_id        TEXT PRIMARY KEY,
    fin_category   TEXT NOT NULL,
    priority       TEXT NOT NULL,
    priority_rank  INTEGER NOT NULL,
    rule_id        TEXT,
    reason         TEXT,
    needs_llm      INTEGER NOT NULL DEFAULT 0,
    rules_version  TEXT NOT NULL,
    classified_at  TEXT NOT NULL
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

CLASSIFY_SQL = """
INSERT OR REPLACE INTO classifications
    (news_id, fin_category, priority, priority_rank, rule_id, reason,
     needs_llm, rules_version, classified_at)
VALUES
    (:news_id, :fin_category, :priority, :priority_rank, :rule_id, :reason,
     :needs_llm, :rules_version, :classified_at)
"""

# Whitelist of sort orders, so user input never gets pasted into SQL
ORDER_CLAUSES = {
    "priority": "COALESCE(c.priority_rank, 0) DESC, a.filing_dt DESC",
    "date": "a.filing_dt DESC",
}


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


def read_unclassified(rules_version: str) -> list:
    """Announcements with no classification, or one made by an older rule set."""
    query = """
        SELECT a.news_id, a.category, a.subcategory, a.title, a.detail
        FROM announcements a
        LEFT JOIN classifications c ON c.news_id = a.news_id
        WHERE c.news_id IS NULL OR c.rules_version != ?
    """
    columns = ["news_id", "category", "subcategory", "title", "detail"]
    with get_connection() as conn:
        rows = conn.execute(query, (rules_version,)).fetchall()
    return [dict(zip(columns, row)) for row in rows]


def upsert_classifications(rows: list) -> int:
    if not rows:
        return 0
    with get_connection() as conn:
        conn.executemany(CLASSIFY_SQL, rows)
    return len(rows)


def read_announcements(tickers=None, since=None, priorities=None, order_by="priority") -> pd.DataFrame:
    """Stored announcements joined with their classification.

    since: 'YYYY-MM-DD HH:MM:SS'. priorities: e.g. ['High']. order_by: 'priority' or 'date'.
    """
    query = """
        SELECT a.*, c.fin_category, c.priority, c.priority_rank,
               c.rule_id, c.reason, c.needs_llm
        FROM announcements a
        LEFT JOIN classifications c ON c.news_id = a.news_id
        WHERE 1=1
    """
    params = []
    if tickers:
        query += f" AND a.ticker IN ({','.join('?' * len(tickers))})"
        params.extend(tickers)
    if since:
        query += " AND a.filing_dt >= ?"
        params.append(since)
    if priorities:
        query += f" AND c.priority IN ({','.join('?' * len(priorities))})"
        params.extend(priorities)
    query += " ORDER BY " + ORDER_CLAUSES.get(order_by, ORDER_CLAUSES["date"])

    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=params)

    number_cols = ["priority_rank", "needs_llm"]
    text_cols = [c for c in df.columns if c not in number_cols]
    df[text_cols] = df[text_cols].fillna("")
    for col in number_cols:
        df[col] = df[col].fillna(0).astype(int)
    return df