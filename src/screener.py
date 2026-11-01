import logging
import re
import time
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

from src.config import (
    SCREENER_BASE_URL,
    SCREENER_PAUSE_SEC,
    SCREENER_TIMEOUT_SEC,
    SCREENER_USER_AGENT,
)

logger = logging.getLogger(__name__)

# Screener's row labels (lower-case) -> our metric names.
# Banks/NBFCs use "Revenue" and "Financing Profit" instead of "Sales" and "Operating Profit".
LABEL_MAP = {
    "sales": "revenue",
    "revenue": "revenue",
    "expenses": "expenses",
    "operating profit": "ebitda",
    "financing profit": "ebitda",
    "opm %": "ebitda_margin_pct",
    "financing margin %": "ebitda_margin_pct",
    "other income": "other_income",
    "interest": "interest",
    "depreciation": "depreciation",
    "profit before tax": "pbt",
    "tax %": "tax_pct",
    "net profit": "pat",
    "eps in rs": "eps",
}
CORE_METRICS = ["revenue", "ebitda", "pbt", "pat"]

# e.g. "Consolidated Figures in Rs. Crores / View Standalone"
NOTE_PATTERN = re.compile(
    r"\s*(consolidated|standalone)\s+figures\s+in\s+rs\.?\s*crores?", re.IGNORECASE
)
TICKER_PATTERN = re.compile(r"[A-Z0-9&\-]{1,20}")


def _to_number(text: str):
    cleaned = text.replace(",", "").replace("%", "").replace("\u00a0", " ").strip()
    if cleaned in ("", "-", "—"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _clean_label(text: str) -> str:
    text = text.replace("\u00a0", " ").strip()
    text = re.sub(r"\s*\+$", "", text)  # Screener adds an expand '+' button to some labels
    return re.sub(r"\s+", " ", text)


def _period_end(period: str):
    """'Sep 2026' -> '2026-09-30'. Returns None if it isn't a month-year header."""
    ts = pd.to_datetime(period, format="%b %Y", errors="coerce")
    if pd.isna(ts):
        return None
    return (ts + pd.offsets.MonthEnd(0)).strftime("%Y-%m-%d")


def parse_quarterly_html(html: str) -> dict:
    """Parse the Quarterly Results section of a Screener company page.

    Raises ValueError (with a clear message) rather than guessing, if the basis,
    the unit or any core row can't be confirmed.
    """
    soup = BeautifulSoup(html, "html.parser")
    section = soup.find("section", id="quarters")
    if section is None:
        raise ValueError(
            "Quarterly Results section not found (company missing, login wall, or layout changed)"
        )

    note = section.find("p", class_="sub")
    note_text = " ".join(note.get_text(" ").split()) if note else ""
    match = NOTE_PATTERN.match(note_text)
    if not match:
        raise ValueError(f"Could not confirm basis and units (₹ crore) from: '{note_text}'")
    basis = match.group(1).lower()

    toggle_href = None
    if note:
        for link in note.find_all("a", href=True):
            if re.search(r"view\s+(standalone|consolidated)", link.get_text(), re.IGNORECASE):
                toggle_href = link["href"]
                break

    table = section.find("table")
    thead = table.find("thead") if table else None
    tbody = table.find("tbody") if table else None
    if thead is None or tbody is None:
        raise ValueError("Quarterly results table not found")

    header = [th.get_text(" ", strip=True) for th in thead.find_all("th")]
    periods = header[1:]

    rows = {}
    for tr in tbody.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        label = _clean_label(cells[0].get_text(" ", strip=True))
        values = [_to_number(c.get_text(" ", strip=True)) for c in cells[1:]]
        rows[label] = dict(zip(periods, values))

    is_financial = "financing profit" in {label.lower() for label in rows}

    records = []
    for period in periods:
        period_end = _period_end(period)
        if period_end is None:
            continue
        record = {"period": period, "period_end": period_end}
        for label, values in rows.items():
            metric = LABEL_MAP.get(label.lower())
            if metric and metric not in record:
                record[metric] = values.get(period)
        records.append(record)

    missing = [m for m in CORE_METRICS if not any(m in r for r in records)]
    if not records or missing:
        raise ValueError(f"Missing core rows {missing} (labels seen: {sorted(rows)})")

    return {
        "basis": basis,
        "unit": "INR crore",
        "is_financial": is_financial,
        "toggle_href": toggle_href,
        "records": records,
        "note": note_text,
    }


def _get(url: str):
    if not url.startswith(SCREENER_BASE_URL + "/"):
        raise ValueError(f"Refusing to fetch a URL outside Screener: {url}")
    response = requests.get(
        url,
        headers={"User-Agent": SCREENER_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        timeout=SCREENER_TIMEOUT_SEC,
    )
    if response.status_code == 404:
        raise ValueError(f"Screener has no page for this company ({url})")
    if response.status_code == 429:
        raise RuntimeError("Screener is rate-limiting requests. Try again later.")
    response.raise_for_status()
    if not response.url.startswith(SCREENER_BASE_URL + "/"):
        raise ValueError("Screener redirected outside its own domain")
    return response.text, response.url


def fetch_company_results(ticker: str) -> dict:
    """Fetch consolidated and (if offered) standalone quarterly results.

    Returns {basis: parsed bundle}. The basis keys come from Screener's own labels,
    so a company with no consolidated data is never mislabelled.
    """
    ticker = ticker.strip().upper()
    if not TICKER_PATTERN.fullmatch(ticker):
        raise ValueError(f"Unexpected ticker format: {ticker!r}")

    html, final_url = _get(f"{SCREENER_BASE_URL}/company/{ticker}/consolidated/")
    first = parse_quarterly_html(html)
    first["url"] = final_url
    bundles = {first["basis"]: first}

    if first["toggle_href"]:
        time.sleep(SCREENER_PAUSE_SEC)
        try:
            html2, url2 = _get(urljoin(final_url, first["toggle_href"]))
            second = parse_quarterly_html(html2)
            second["url"] = url2
            bundles.setdefault(second["basis"], second)
        except ValueError as err:
            logger.warning("Could not read the other basis for %s: %s", ticker, err)
    return bundles


# ---------- Self-test (no network) and live inspection ----------


def _sample_html(note: str, periods: list, rows: list) -> str:
    head = "".join(f"<th>{p}</th>" for p in periods)
    body = ""
    for label, values in rows:
        cells = "".join(f"<td>{v}</td>" for v in values)
        body += f"<tr><td class='text'><button>{label}&nbsp;<span>+</span></button></td>{cells}</tr>"
    return (
        f'<section id="quarters"><p class="sub">{note}</p><table><thead><tr><th></th>{head}'
        f"</tr></thead><tbody>{body}</tbody></table></section>"
    )


def run_self_test() -> None:
    periods = ["Dec 2025", "Mar 2026", "Jun 2026"]
    industrial = [
        ("Sales", ["1,000", "1,100", "1,210"]),
        ("Expenses", ["800", "869", "956"]),
        ("Operating Profit", ["200", "231", "254"]),
        ("OPM %", ["20%", "21%", "21%"]),
        ("Other Income", ["10", "12", "11"]),
        ("Interest", ["5", "5", "6"]),
        ("Depreciation", ["30", "31", "32"]),
        ("Profit before tax", ["175", "207", "227"]),
        ("Tax %", ["25%", "25%", "26%"]),
        ("Net Profit", ["131", "155", "168"]),
        ("EPS in Rs", ["5.2", "6.1", "6.6"]),
    ]
    note = 'Consolidated Figures in Rs. Crores / <a href="/company/TEST/">View Standalone</a>'
    parsed = parse_quarterly_html(_sample_html(note, periods, industrial))
    assert parsed["basis"] == "consolidated"
    assert parsed["toggle_href"] == "/company/TEST/"
    assert parsed["is_financial"] is False
    assert [r["revenue"] for r in parsed["records"]] == [1000.0, 1100.0, 1210.0]
    assert parsed["records"][-1]["period_end"] == "2026-06-30"
    assert parsed["records"][0]["ebitda_margin_pct"] == 20.0
    assert parsed["records"][1]["pat"] == 155.0

    # A bank-style page: different labels, standalone basis
    bank = [
        ("Revenue", ["500", "520", "540"]),
        ("Interest", ["250", "260", "270"]),
        ("Expenses", ["150", "155", "160"]),
        ("Financing Profit", ["100", "105", "110"]),
        ("Financing Margin %", ["20%", "20%", "20%"]),
        ("Other Income", ["20", "21", "22"]),
        ("Depreciation", ["5", "5", "5"]),
        ("Profit before tax", ["115", "121", "127"]),
        ("Tax %", ["25%", "25%", "25%"]),
        ("Net Profit", ["86", "91", "95"]),
        ("EPS in Rs", ["8", "8.5", "9"]),
    ]
    bank_note = 'Standalone Figures in Rs. Crores / <a href="/company/BANK/consolidated/">View Consolidated</a>'
    parsed_bank = parse_quarterly_html(_sample_html(bank_note, periods, bank))
    assert parsed_bank["basis"] == "standalone"
    assert parsed_bank["is_financial"] is True
    assert parsed_bank["records"][-1]["ebitda"] == 110.0

    # Wrong units and missing rows must be rejected, never guessed around
    for bad_note, bad_rows in [
        ("Consolidated Figures in Rs. Lakhs / View Standalone", industrial),
        (note, [r for r in industrial if r[0] != "Net Profit"]),
    ]:
        try:
            parse_quarterly_html(_sample_html(bad_note, periods, bad_rows))
        except ValueError:
            continue
        raise AssertionError("A bad page was accepted")

    print("All parser tests passed.")


def inspect_company(ticker: str) -> None:
    """Fetch a real company page and print what the parser understood."""
    bundles = fetch_company_results(ticker)
    for basis, bundle in bundles.items():
        records = bundle["records"]
        print(f"--- {ticker} / {basis} ---")
        print("URL:", bundle["url"])
        print("Page note:", bundle["note"])
        print("Financial company (bank/NBFC):", bundle["is_financial"])
        print(f"Quarters: {len(records)} from {records[0]['period']} to {records[-1]['period']}")
        print("Latest:", records[-1])


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        inspect_company(sys.argv[1])
    else:
        run_self_test()