import re
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.config import SCREENER_BASE_URL, SCREENER_PAUSE_SEC
from src.screener import TICKER_PATTERN, _get

PERIOD_PATTERN = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b"
)
MONTHS = {
    name: number
    for number, name in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1
    )
}


def parse_documents_html(html: str, base_url: str) -> dict:
    """Parse the Annual reports and Concalls lists from a Screener company page.

    Only annual-report PDFs and TRANSCRIPT links are returned. Screener's AI summaries,
    notes, presentations and recordings are deliberately ignored.
    """
    soup = BeautifulSoup(html, "html.parser")
    reports_section = soup.select_one(".documents.annual-reports")
    calls_section = soup.select_one(".documents.concalls")
    if reports_section is None and calls_section is None:
        raise ValueError(
            "Documents section not found (the company has none, a login wall appeared, or the layout changed)"
        )

    reports = []
    if reports_section is not None:
        for item in reports_section.find_all("li"):
            link = item.find("a", href=True)
            if link is None:
                continue
            text = " ".join(item.get_text(" ").split())
            year = re.search(r"Financial Year\s+(\d{4})", text, re.IGNORECASE)
            if not year:
                continue
            source = re.search(r"\bfrom\s+([A-Za-z0-9.\-]+)", text, re.IGNORECASE)
            reports.append(
                {
                    "fy": int(year.group(1)),
                    "label": f"Financial Year {year.group(1)}",
                    "source": source.group(1).lower() if source else "unknown",
                    "url": urljoin(base_url, link["href"]),
                }
            )

    calls = []
    if calls_section is not None:
        for item in calls_section.find_all("li"):
            text = " ".join(item.get_text(" ").split())
            period = PERIOD_PATTERN.search(text)
            if not period:
                continue
            transcript_url = None
            for link in item.find_all("a", href=True):
                if link.get_text(" ", strip=True).lower().startswith("transcript"):
                    transcript_url = urljoin(base_url, link["href"])
                    break
            calls.append(
                {
                    "period": f"{period.group(2)}-{MONTHS[period.group(1)]:02d}",
                    "label": f"{period.group(1)} {period.group(2)}",
                    "transcript_url": transcript_url,
                }
            )
    return {"annual_reports": reports, "concalls": calls}


def fetch_document_listing(ticker: str) -> dict:
    """List a company's annual reports and call transcripts from its Screener page."""
    ticker = ticker.strip().upper()
    if not TICKER_PATTERN.fullmatch(ticker):
        raise ValueError(f"Unexpected ticker format: {ticker!r}")

    last_error = None
    for path in (f"/company/{ticker}/consolidated/", f"/company/{ticker}/"):
        try:
            html, final_url = _get(SCREENER_BASE_URL + path)
            return parse_documents_html(html, final_url)
        except ValueError as err:
            last_error = err
            time.sleep(SCREENER_PAUSE_SEC)
    raise ValueError(str(last_error))