import hashlib
import ipaddress
import logging
import re
import socket
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests

from src import screener_docs
from src.config import (
    BASE_DIR,
    DOCS_ANNUAL_REPORTS,
    DOCS_DIR,
    DOCS_MAX_BYTES,
    DOCS_PAUSE_SEC,
    DOCS_SOURCE_PREFERENCE,
    DOCS_TRANSCRIPTS,
    SCREENER_USER_AGENT,
)
from src.db import get_connection

logger = logging.getLogger(__name__)

DOCS_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id         TEXT PRIMARY KEY,
    ticker         TEXT NOT NULL,
    doc_type       TEXT NOT NULL,
    period         TEXT NOT NULL,
    period_label   TEXT,
    source         TEXT,
    url            TEXT NOT NULL,
    local_path     TEXT,
    status         TEXT NOT NULL,
    status_detail  TEXT,
    pages          INTEGER,
    size_bytes     INTEGER,
    sha256         TEXT,
    listed_at      TEXT NOT NULL,
    downloaded_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_docs_ticker ON documents (ticker, doc_type, period);
"""

COLUMNS = [
    "doc_id", "ticker", "doc_type", "period", "period_label", "source", "url", "local_path",
    "status", "status_detail", "pages", "size_bytes", "sha256", "listed_at", "downloaded_at",
]
UPSERT_SQL = (
    f"INSERT OR REPLACE INTO documents ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in COLUMNS)})"
)


@contextmanager
def _db():
    with get_connection() as conn:
        conn.executescript(DOCS_SCHEMA)
        yield conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _save(record: dict) -> None:
    row = {col: record.get(col) for col in COLUMNS}
    with _db() as conn:
        conn.execute(UPSERT_SQL, row)


def _get_doc(doc_id: str):
    with _db() as conn:
        cursor = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,))
        row = cursor.fetchone()
        names = [d[0] for d in cursor.description]
    return dict(zip(names, row)) if row else None


def list_documents(ticker: str) -> pd.DataFrame:
    with _db() as conn:
        return pd.read_sql_query(
            "SELECT * FROM documents WHERE ticker = ? ORDER BY doc_type, period DESC",
            conn,
            params=[ticker.strip().upper()],
        ).fillna("")


def document_path(local_path: str) -> Path:
    """Stored paths are relative to the project folder, so the database stays portable."""
    return BASE_DIR / local_path


# ---------- Choosing what to collect ----------


def select_annual_reports(items: list, limit: int) -> list:
    """One report per financial year (preferring BSE, then NSE), newest first."""
    best = {}
    for item in items:
        pref = DOCS_SOURCE_PREFERENCE
        rank = pref.index(item["source"]) if item["source"] in pref else len(pref)
        current = best.get(item["fy"])
        if current is None or rank < current[0]:
            best[item["fy"]] = (rank, item)
    chosen = sorted((v[1] for v in best.values()), key=lambda r: r["fy"], reverse=True)
    return chosen[:limit]


def select_transcripts(calls: list, limit: int) -> list:
    """Calls that actually have a transcript link, newest first."""
    usable = [c for c in calls if c.get("transcript_url")]
    usable.sort(key=lambda c: c["period"], reverse=True)
    return usable[:limit]


# ---------- Safe downloading ----------


def is_safe_url(url: str, resolve: bool = True) -> bool:
    """Only plain https links to public hosts. Guards against links that point inside our own network."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    if port not in (None, 443):
        return False
    if "." not in host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        ipaddress.ip_address(host)
        return False  # raw IP addresses are refused
    except ValueError:
        pass
    if resolve:
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            for info in infos:
                if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
                    return False
        except (OSError, ValueError):
            return False
    return True


def download_pdf(url: str, dest: Path):
    """Download one PDF. Returns (status, detail, size_bytes, sha256); status is 'downloaded' on success."""
    if not is_safe_url(url):
        return "skipped", "link is not a plain https link to a public host", 0, None

    headers = {"User-Agent": SCREENER_USER_AGENT}
    host = urlparse(url).hostname or ""
    if host.endswith("bseindia.com"):
        headers["Referer"] = "https://www.bseindia.com/"

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    digest = hashlib.sha256()
    size = 0
    first = b""
    too_big = False
    try:
        with requests.get(url, headers=headers, timeout=60, stream=True) as response:
            response.raise_for_status()
            if not is_safe_url(response.url):
                return "skipped", "redirected to an unsafe address", 0, None
            try:
                declared = int(response.headers.get("Content-Length") or 0)
            except ValueError:
                declared = 0
            if declared > DOCS_MAX_BYTES:
                return "skipped", f"file too large ({declared / 1e6:.0f} MB)", 0, None
            with open(tmp, "wb") as handle:
                for chunk in response.iter_content(1 << 16):
                    if not first:
                        first = chunk[:5]
                    size += len(chunk)
                    if size > DOCS_MAX_BYTES:
                        too_big = True
                        break
                    digest.update(chunk)
                    handle.write(chunk)
    except requests.RequestException as err:
        tmp.unlink(missing_ok=True)
        return "failed", f"download failed: {str(err)[:150]}", 0, None

    if too_big:
        tmp.unlink(missing_ok=True)
        return "skipped", f"file too large (over {DOCS_MAX_BYTES / 1e6:.0f} MB)", 0, None
    if not first.startswith(b"%PDF"):
        tmp.unlink(missing_ok=True)
        return "failed", "not a PDF (a login page or an HTML error?)", 0, None
    tmp.replace(dest)
    return "downloaded", "", size, digest.hexdigest()


def inspect_pdf(path: Path):
    """Return (pages, has_text_layer, error). Samples a few pages, including one from the middle."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            return 0, False, "encrypted PDF"
        total = len(reader.pages)
        indexes = sorted({0, 1, 2, total // 2} & set(range(total)))
        sample = " ".join((reader.pages[i].extract_text() or "") for i in indexes)
        return total, len(sample.strip()) >= 300, None
    except Exception as err:
        return 0, False, f"unreadable PDF: {str(err)[:150]}"


# ---------- Collecting ----------


def collect_documents(ticker: str, force: bool = False) -> dict:
    """List a company's latest annual reports and transcripts, download the missing ones, and check them."""
    ticker = ticker.strip().upper()
    summary = {"ticker": ticker, "downloaded": 0, "already_had": 0, "problems": [], "error": None}

    try:
        listing = screener_docs.fetch_document_listing(ticker)
    except Exception as err:
        summary["error"] = f"Could not list documents: {err}"
        return summary

    wanted = []
    for report in select_annual_reports(listing["annual_reports"], DOCS_ANNUAL_REPORTS):
        wanted.append(
            {
                "doc_type": "annual_report",
                "period": f"FY{report['fy']}",
                "period_label": report["label"],
                "source": report["source"],
                "url": report["url"],
            }
        )
    for call in select_transcripts(listing["concalls"], DOCS_TRANSCRIPTS):
        wanted.append(
            {
                "doc_type": "transcript",
                "period": call["period"],
                "period_label": call["label"],
                "source": urlparse(call["transcript_url"]).hostname or "unknown",
                "url": call["transcript_url"],
            }
        )
    if not wanted:
        summary["error"] = "Screener lists no annual reports or call transcripts for this company."
        return summary

    for item in wanted:
        doc_id = f"{ticker}:{item['doc_type']}:{item['period']}"
        existing = _get_doc(doc_id)
        if (
            existing
            and not force
            and existing["status"] in ("ok", "scanned")
            and existing["local_path"]
            and document_path(existing["local_path"]).exists()
        ):
            summary["already_had"] += 1
            continue

        dest = DOCS_DIR / ticker / f"{item['doc_type']}_{item['period']}.pdf"
        record = {
            **item,
            "doc_id": doc_id,
            "ticker": ticker,
            "listed_at": _now(),
            "local_path": None,
        }
        status, detail, size, sha = download_pdf(item["url"], dest)
        if status != "downloaded":
            record.update(status=status, status_detail=detail)
            _save(record)
            summary["problems"].append(f"{item['period_label']} ({item['doc_type']}): {detail}")
            time.sleep(DOCS_PAUSE_SEC)
            continue

        pages, has_text, error = inspect_pdf(dest)
        if error:
            final, detail = "failed", error
        elif has_text:
            final, detail = "ok", ""
        else:
            final, detail = "scanned", "no readable text layer; would need OCR"
        record.update(
            status=final,
            status_detail=detail,
            pages=pages,
            size_bytes=size,
            sha256=sha,
            local_path=dest.relative_to(BASE_DIR).as_posix(),
            downloaded_at=_now(),
        )
        _save(record)
        if final == "ok":
            summary["downloaded"] += 1
        else:
            summary["problems"].append(f"{item['period_label']} ({item['doc_type']}): {detail}")
        time.sleep(DOCS_PAUSE_SEC)
    return summary


def register_local_pdfs(ticker: str) -> int:
    """Fallback: register PDFs you saved by hand in data/docs/<TICKER>/.

    Name them annual_report_FY2025.pdf or transcript_2026-05.pdf.
    """
    ticker = ticker.strip().upper()
    folder = DOCS_DIR / ticker
    added = 0
    if not folder.exists():
        return 0
    for path in sorted(folder.glob("*.pdf")):
        match = re.fullmatch(r"(annual_report)_(FY\d{4})|(transcript)_(\d{4}-\d{2})", path.stem)
        if not match:
            continue
        doc_type = match.group(1) or match.group(3)
        period = match.group(2) or match.group(4)
        doc_id = f"{ticker}:{doc_type}:{period}"
        if _get_doc(doc_id):
            continue
        pages, has_text, error = inspect_pdf(path)
        status = "failed" if error else ("ok" if has_text else "scanned")
        _save(
            {
                "doc_id": doc_id,
                "ticker": ticker,
                "doc_type": doc_type,
                "period": period,
                "period_label": period,
                "source": "manual",
                "url": "local file",
                "local_path": path.relative_to(BASE_DIR).as_posix(),
                "status": status,
                "status_detail": error or "",
                "pages": pages,
                "size_bytes": path.stat().st_size,
                "listed_at": _now(),
                "downloaded_at": _now(),
            }
        )
        added += 1
    return added


# ---------- Tests and command line ----------


def run_self_test() -> None:
    sample = """
    <section id="documents">
      <div class="documents annual-reports flex-column"><h3>Annual reports</h3><ul class="list-links">
        <li><a href="https://www.bseindia.com/bseplus/AnnualReport/1/fy25.pdf">Financial Year 2025<div class="ink-600 smaller">from bse</div></a></li>
        <li><a href="https://www.nseindia.com/a/fy25.pdf">Financial Year 2025<div class="ink-600 smaller">from nse</div></a></li>
        <li><a href="/static/fy24.pdf">Financial Year 2024<div class="ink-600 smaller">from bse</div></a></li>
        <li><a href="https://www.bseindia.com/bseplus/AnnualReport/1/fy23.pdf">Financial Year 2023<div class="ink-600 smaller">from nse</div></a></li>
      </ul></div>
      <div class="documents concalls flex-column"><h3>Concalls</h3><ul class="list-links">
        <li><div class="ink-600 nowrap">May 2026</div><a class="concall-link" href="https://example.com/t-may.pdf">Transcript</a><button>AI Summary</button><a href="https://example.com/p.pdf">PPT</a></li>
        <li><div class="ink-600 nowrap">Feb 2026</div><a href="https://example.com/p2.pdf">PPT</a><a href="https://youtube.com/x">REC</a></li>
        <li><div class="ink-600 nowrap">Nov 2025</div><a href="https://example.com/t-nov.pdf">Transcript</a></li>
      </ul></div>
    </section>
    """
    listing = screener_docs.parse_documents_html(sample, "https://www.screener.in/company/TEST/consolidated/")
    assert [(r["fy"], r["source"]) for r in listing["annual_reports"]] == [
        (2025, "bse"), (2025, "nse"), (2024, "bse"), (2023, "nse"),
    ]
    assert listing["annual_reports"][2]["url"] == "https://www.screener.in/static/fy24.pdf"
    assert [(c["period"], bool(c["transcript_url"])) for c in listing["concalls"]] == [
        ("2026-05", True), ("2026-02", False), ("2025-11", True),
    ]

    chosen = select_annual_reports(listing["annual_reports"], 3)
    assert [(r["fy"], r["source"]) for r in chosen] == [(2025, "bse"), (2024, "bse"), (2023, "nse")]
    assert [c["period"] for c in select_transcripts(listing["concalls"], 8)] == ["2026-05", "2025-11"]

    try:
        screener_docs.parse_documents_html("<html><body>nothing</body></html>", "https://www.screener.in/")
    except ValueError:
        pass
    else:
        raise AssertionError("an empty page was accepted")

    good = ["https://www.bseindia.com/x.pdf", "https://www.infosys.com/ir/transcript.pdf"]
    bad = [
        "http://www.bseindia.com/x.pdf",
        "ftp://example.com/x",
        "https://127.0.0.1/x",
        "https://localhost/x",
        "https://192.168.1.5/x",
        "https://[::1]/x",
        "https://user:pw@www.bseindia.com/x",
        "https://intranet/x",
        "https://example.com:8443/x",
        "https://printer.local/x",
    ]
    for url in good:
        assert is_safe_url(url, resolve=False), url
    for url in bad:
        assert not is_safe_url(url, resolve=False), url

    from pypdf import PdfWriter

    with tempfile.TemporaryDirectory() as folder:
        blank = Path(folder) / "blank.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with open(blank, "wb") as handle:
            writer.write(handle)
        assert inspect_pdf(blank) == (1, False, None)
        garbage = Path(folder) / "garbage.pdf"
        garbage.write_bytes(b"this is not a pdf")
        pages, has_text, error = inspect_pdf(garbage)
        assert pages == 0 and not has_text and error
    print("All document tests passed.")


def inspect_listing(ticker: str) -> None:
    """List-only: shows what Screener offers without downloading anything."""
    listing = screener_docs.fetch_document_listing(ticker)
    reports, calls = listing["annual_reports"], listing["concalls"]
    print(f"Annual reports listed: {len(reports)} | sources: {sorted({r['source'] for r in reports})}")
    for r in reports[:3]:
        print("  ", r["label"], r["source"], r["url"])
    with_transcript = [c for c in calls if c["transcript_url"]]
    print(f"Concalls listed: {len(calls)} | with a transcript link: {len(with_transcript)}")
    for c in with_transcript[:3]:
        print("  ", c["label"], c["transcript_url"])
    hosts = sorted({urlparse(c["transcript_url"]).hostname for c in with_transcript})
    print("Transcript hosts:", hosts)


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "inspect":
        inspect_listing(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "collect":
        result = collect_documents(sys.argv[2])
        print({k: v for k, v in result.items() if k != "problems"})
        for problem in result["problems"]:
            print("PROBLEM:", problem)
        table = list_documents(sys.argv[2])
        if not table.empty:
            cols = ["doc_type", "period", "status", "pages", "size_bytes", "status_detail"]
            print(table[cols].to_string(index=False))
    elif len(sys.argv) >= 3 and sys.argv[1] == "local":
        print("Registered:", register_local_pdfs(sys.argv[2]))
    else:
        run_self_test()