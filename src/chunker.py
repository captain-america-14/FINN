import logging
import re
from collections import Counter

from src.config import (
    CHUNK_OVERLAP_WORDS,
    CHUNK_WORDS,
    MIN_ALPHA_RATIO,
    MIN_CHUNK_CHARS,
    RAG_MAX_PAGES,
)

logging.getLogger("pypdf").setLevel(logging.ERROR)

# Words that end in a full stop without ending a sentence
ABBREVIATIONS = {"rs.", "no.", "ltd.", "mr.", "mrs.", "ms.", "dr.", "inc.", "pvt.", "co.", "vs.", "approx."}

QA_PATTERN = re.compile(r"question[- ]and[- ]answer|q\s*&\s*a session", re.IGNORECASE)

# Headings looked for in the first lines of an annual-report page. First match wins.
SECTION_PATTERNS = [
    (re.compile(r"management discussion (and|&) analysis", re.I), "Management Discussion & Analysis"),
    (re.compile(r"directors['’]?\s*report|board['’]?s report", re.I), "Directors' Report"),
    (re.compile(r"corporate governance report", re.I), "Corporate Governance"),
    (re.compile(r"business responsibility|\bbrsr\b", re.I), "Business Responsibility & Sustainability"),
    (re.compile(r"independent auditor['’]?s report", re.I), "Auditor's Report"),
    (re.compile(r"notes (forming part of|to) the (consolidated |standalone )?financial statements", re.I), "Notes to Financial Statements"),
    (re.compile(r"consolidated financial statements", re.I), "Consolidated Financial Statements"),
    (re.compile(r"standalone financial statements", re.I), "Standalone Financial Statements"),
    (re.compile(r"risk management", re.I), "Risk Management"),
    (re.compile(r"chairman['’]?s (message|statement|letter)|letter to (the )?shareholders", re.I), "Chairman's Message"),
]


# ---------- Reading and cleaning ----------


def extract_pages(path, max_pages: int = RAG_MAX_PAGES, progress=None) -> list:
    """Text of each page (index 0 = PDF page 1). A page that fails to read becomes ''."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("encrypted PDF")
    total = min(len(reader.pages), max_pages)
    pages = []
    for index in range(total):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:
            text = ""
        pages.append(text)
        if progress and (index + 1) % 50 == 0:
            progress(index + 1, total)
    return pages


def _normalise_line(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip().lower())


def strip_repeated_lines(pages: list, min_pages: int = 10, ratio: float = 0.3, max_len: int = 100) -> list:
    """Remove running headers, footers and page numbers: short lines on at least 30% of pages."""
    if len(pages) < min_pages:
        return pages
    counts = Counter()
    for text in pages:
        counts.update({_normalise_line(line) for line in text.splitlines() if 0 < len(line.strip()) <= max_len})
    repeated = {line for line, count in counts.items() if count >= ratio * len(pages)}
    return [
        "\n".join(line for line in text.splitlines() if _normalise_line(line) not in repeated)
        for text in pages
    ]


def clean_pages(pages: list) -> list:
    pages = strip_repeated_lines(pages)
    return [re.sub(r"(\w)-\n(\w)", r"\1\2", text) for text in pages]  # join words split across lines


# ---------- Sections ----------


def detect_section(text: str, doc_type: str, current: str) -> str:
    """Section label in force on this page. Sticky: a page with no heading keeps the previous one."""
    if doc_type == "transcript":
        return "Q&A session" if QA_PATTERN.search(text) else current
    head = "\n".join(text.splitlines()[:12])
    for pattern, label in SECTION_PATTERNS:
        if pattern.search(head):
            return label
    return current


# ---------- Chunking ----------


def _snap_to_sentence_end(tokens: list, start: int, end: int) -> int:
    """Move `end` back to the nearest sentence end within the last 40 words (but keep >= 60% of the chunk)."""
    lowest = max(start + int((end - start) * 0.6), end - 40)
    for i in range(end - 1, lowest - 1, -1):
        word = tokens[i][0]
        if word.endswith((".", "?", "!")) and word.lower() not in ABBREVIATIONS:
            return i + 1
    return end


def _keep_chunk(text: str) -> bool:
    if len(text) < MIN_CHUNK_CHARS:
        return False
    return sum(ch.isalpha() for ch in text) / len(text) >= MIN_ALPHA_RATIO


def make_chunks(
    pages: list,
    doc_type: str,
    words_per_chunk: int = CHUNK_WORDS,
    overlap: int = CHUNK_OVERLAP_WORDS,
) -> list:
    """Split cleaned pages into overlapping word-window chunks that remember their pages and section."""
    section = "Opening remarks" if doc_type == "transcript" else "Front matter"
    tokens = []  # (word, page number starting at 1, section)
    for page_number, text in enumerate(pages, start=1):
        section = detect_section(text, doc_type, section)
        for word in text.split():
            tokens.append((word, page_number, section))

    chunks = []
    start, total = 0, len(tokens)
    while start < total:
        end = min(start + words_per_chunk, total)
        if end < total:
            end = _snap_to_sentence_end(tokens, start, end)
        text = " ".join(token[0] for token in tokens[start:end])
        if _keep_chunk(text):
            chunks.append(
                {
                    "text": text,
                    "page_start": tokens[start][1],
                    "page_end": tokens[end - 1][1],
                    "section": tokens[start][2],
                    "word_count": end - start,
                }
            )
        if end >= total:
            break
        start = max(end - overlap, start + 1)
    return chunks


# ---------- Self-test (no PDFs needed) ----------


def _fake_page(number: int, heading: str = "") -> str:
    sentence = "Revenue grew strongly during the year under review."  # 8 words
    body = " ".join([sentence] * 15)
    return f"ACME Annual Report 2025 Page {number}\n{heading}\n{body}"


def run_self_test() -> None:
    pages = [_fake_page(n, "Management Discussion and Analysis" if n == 5 else "") for n in range(1, 13)]
    cleaned = clean_pages(pages)
    assert all("ACME Annual Report" not in page for page in cleaned), "running header was not removed"

    chunks = make_chunks(cleaned, "annual_report")
    assert len(chunks) >= 5
    assert all(c["page_start"] <= c["page_end"] for c in chunks)
    assert all(1 <= c["page_start"] and c["page_end"] <= 12 for c in chunks)
    assert chunks[0]["section"] == "Front matter"
    assert all(c["section"] == "Management Discussion & Analysis" for c in chunks if c["page_start"] >= 5)
    assert all(c["text"].endswith(".") for c in chunks), "chunks should end on a sentence boundary"
    assert chunks[1]["page_start"] <= chunks[0]["page_end"], "chunks should overlap"

    assert make_chunks(["1234 5678 " * 100], "annual_report") == [], "number-only chunks must be dropped"

    assert detect_section("Question-and-Answer Session\nOperator: hello", "transcript", "Opening remarks") == "Q&A session"
    assert detect_section("Just ordinary text", "transcript", "Opening remarks") == "Opening remarks"
    assert detect_section("Directors' Report\nbody", "annual_report", "Front matter") == "Directors' Report"
    assert detect_section("no heading here", "annual_report", "Directors' Report") == "Directors' Report"
    print(f"All chunker tests passed ({len(chunks)} chunks from the synthetic report).")


if __name__ == "__main__":
    run_self_test()