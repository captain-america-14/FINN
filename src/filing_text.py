import io
import re
from urllib.parse import urlparse

import requests

from src.config import (
    FILING_ALLOWED_HOSTS,
    FILING_MAX_BYTES,
    FILING_MAX_CHARS,
    FILING_MAX_PAGES,
    SCREENER_USER_AGENT,  # a normal browser User-Agent; BSE rejects unknown clients
)


def _allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in FILING_ALLOWED_HOSTS


def fetch_filing_text(url: str):
    """Download a BSE filing PDF and return (text, status).

    Returns ("", reason) on any problem, so the caller can fall back to the headline.
    Only https links on BSE hosts are fetched, files are size-capped, and only the
    first pages are read.
    """
    if not url or not _allowed(url):
        return "", "skipped: not an https BSE link"
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "skipped: pypdf is not installed"

    try:
        response = requests.get(
            url,
            headers={"User-Agent": SCREENER_USER_AGENT, "Referer": "https://www.bseindia.com/"},
            timeout=30,
            stream=True,
        )
        response.raise_for_status()
        if not _allowed(response.url):
            response.close()
            return "", "skipped: redirected away from BSE"

        try:
            declared = int(response.headers.get("Content-Length", "0"))
        except ValueError:
            declared = 0
        if declared > FILING_MAX_BYTES:
            response.close()
            return "", f"skipped: file too large ({declared / 1e6:.1f} MB)"

        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > FILING_MAX_BYTES:
                response.close()
                return "", f"skipped: file too large (over {FILING_MAX_BYTES / 1e6:.0f} MB)"
            chunks.append(chunk)
        data = b"".join(chunks)
    except requests.RequestException as err:
        return "", f"fetch failed: {err}"

    if not data.startswith(b"%PDF"):
        return "", "skipped: the attachment is not a PDF"
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages[:FILING_MAX_PAGES]]
    except Exception as err:
        return "", f"pdf read failed: {err}"

    text = re.sub(r"\s+", " ", " ".join(pages)).strip()
    if len(text) < 200:
        return "", "no extractable text (scanned PDF?)"
    return text[:FILING_MAX_CHARS], "ok"

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m src.filing_text <BSE filing PDF url>")
    else:
        text, status = fetch_filing_text(sys.argv[1])
        print("Status:", status)
        print("Characters:", len(text))
        print(text[:400])