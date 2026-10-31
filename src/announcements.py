import logging
import time
from datetime import datetime, timedelta

import pandas as pd

from src import db
from src.config import (
    ANNOUNCEMENT_LOOKBACK_DAYS,
    ANNOUNCEMENT_MAX_PAGES,
    ANNOUNCEMENT_REFRESH_MINUTES,
    BSE_ATTACHMENT_BASE,
    BSE_CACHE_DIR,
    BSE_REQUEST_PAUSE_SEC,
)

logger = logging.getLogger(__name__)


def _parse_dt(value):
    """Turn BSE's date string into 'YYYY-MM-DD HH:MM:SS', or None if unparseable."""
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts.strftime("%Y-%m-%d %H:%M:%S")


def normalize_bse_row(raw: dict, ticker: str, our_name: str):
    """Convert one raw BSE announcement into our row shape. Returns None if unusable."""
    news_id = str(raw.get("NEWSID") or "").strip()
    if not news_id:
        return None  # never store something we can't uniquely identify

    attachment = str(raw.get("ATTACHMENTNAME") or "").strip()
    page_url = str(raw.get("NSURL") or "").strip()
    filing_url = f"{BSE_ATTACHMENT_BASE}{attachment}" if attachment else page_url

    return {
        "news_id": news_id,
        "ticker": ticker,
        "company": str(raw.get("SLONGNAME") or our_name).strip(),
        "filing_dt": _parse_dt(raw.get("NEWS_DT") or raw.get("DissemDT")),
        "category": str(raw.get("CATEGORYNAME") or "").strip(),
        "subcategory": str(raw.get("SUBCATNAME") or "").strip(),
        "title": str(raw.get("NEWSSUB") or raw.get("HEADLINE") or "").strip(),
        "detail": str(raw.get("HEADLINE") or "").strip(),
        "filing_url": filing_url,
        "page_url": page_url,
        "source": "BSE",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def _fetch_bse_rows(client, scrip_code: str, from_dt: datetime, to_dt: datetime) -> list:
    """Fetch all pages of raw announcements for one scrip code."""
    rows = []
    page = 1
    while page <= ANNOUNCEMENT_MAX_PAGES:
        data = client.announcements(
            page_no=page, from_date=from_dt, to_date=to_dt, scripcode=scrip_code
        )
        table = data.get("Table") or []
        rows.extend(table)
        table1 = data.get("Table1") or []
        total = int(table1[0].get("ROWCNT", 0) or 0) if table1 else 0
        if not table or len(rows) >= total:
            break
        page += 1
    return rows


def _is_fresh(ticker: str) -> bool:
    last = db.last_fetched(ticker)
    return bool(last) and datetime.now() - last < timedelta(minutes=ANNOUNCEMENT_REFRESH_MINUTES)


def refresh_announcements(
    companies: pd.DataFrame, days: int = ANNOUNCEMENT_LOOKBACK_DAYS, force: bool = False
) -> dict:
    """Fetch recent BSE filings for each company and store the new ones."""
    summary = {"checked": 0, "new": 0, "skipped_fresh": 0, "errors": []}

    try:
        from bse import BSE
    except ImportError:
        summary["errors"].append(("ALL", "The 'bse' package is not installed (pip install bse)."))
        return summary

    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=days)
    BSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with BSE(download_folder=BSE_CACHE_DIR) as client:
            for _, company in companies.iterrows():
                ticker = company["ticker"]
                code = str(company.get("bse_code", "")).strip()
                if not code:
                    summary["errors"].append((ticker, "no BSE code in universe.csv"))
                    continue
                if not force and _is_fresh(ticker):
                    summary["skipped_fresh"] += 1
                    continue
                try:
                    raw_rows = _fetch_bse_rows(client, code, from_dt, to_dt)
                    rows = [
                        r
                        for r in (normalize_bse_row(raw, ticker, company["name"]) for raw in raw_rows)
                        if r
                    ]
                    summary["new"] += db.upsert_announcements(rows)
                    db.mark_fetched(ticker)
                    summary["checked"] += 1
                except Exception as err:
                    logger.warning("BSE fetch failed for %s: %s", ticker, err)
                    summary["errors"].append((ticker, str(err)))
                time.sleep(BSE_REQUEST_PAUSE_SEC)
    except Exception as err:
        summary["errors"].append(("BSE", f"could not open a BSE session: {err}"))

    return summary


def get_announcements(tickers=None, days: int = 7, priorities=None, order_by: str = "priority") -> pd.DataFrame:
    """Read stored, classified announcements for the last `days` days."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    return db.read_announcements(tickers, since, priorities, order_by)


if __name__ == "__main__":
    import json
    import sys

    from bse import BSE

    from src.portfolio import get_company, load_universe

    # Part 1: look at one RAW response so we can confirm the field names
    infy = get_company("INFY")
    now = datetime.now()
    with BSE(download_folder=BSE_CACHE_DIR) as client:
        data = client.announcements(
            scripcode=infy["bse_code"], from_date=now - timedelta(days=30), to_date=now
        )
    table = data.get("Table") or []
    print("Top-level keys:", list(data.keys()))
    print("INFY raw rows:", len(table))
    if table:
        print("Row keys:", sorted(table[0].keys()))
        print(json.dumps(table[0], indent=2, default=str)[:1500])

    # Part 2: refresh and normalise. Pass 'all' to include every company
    universe = load_universe()
    if len(sys.argv) > 1 and sys.argv[1] == "all":
        targets = universe
    else:
        targets = universe[universe["ticker"].isin(["INFY", "RELIANCE", "TMPV"])]
    summary = refresh_announcements(targets, force=True)
    print("\nSummary:", summary)

    # Part 3: verify that each BSE code returns the company we expect
    stored = db.read_announcements(targets["ticker"].tolist())
    for ticker in targets["ticker"]:
        sub = stored[stored["ticker"] == ticker]
        names = sorted(set(sub["company"]))[:2]
        print(f"{ticker:<11} {len(sub):>3} filings  BSE names: {names}")
        for _, r in sub.head(2).iterrows():
            print("    ", r["filing_dt"], "|", r["category"], "|", r["title"][:70])
            print("      ", r["filing_url"])