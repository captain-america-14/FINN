import logging
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from src import chunker, documents, embedder
from src.config import CHUNK_OVERLAP_WORDS, CHUNK_WORDS, CHUNKER_VERSION, DATA_DIR, EMBED_BATCH_SIZE, RAG_DIR, RAG_TOP_K
from src.db import get_connection

logger = logging.getLogger(__name__)

RAG_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id        TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    doc_type      TEXT NOT NULL,
    period        TEXT NOT NULL,
    period_label  TEXT,
    page_start    INTEGER,
    page_end      INTEGER,
    section       TEXT,
    text          TEXT NOT NULL,
    word_count    INTEGER,
    embedding     BLOB,
    embed_model   TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_ticker ON chunks (ticker);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks (doc_id);
CREATE TABLE IF NOT EXISTS ingest_log (
    doc_id           TEXT PRIMARY KEY,
    sha256           TEXT,
    chunker_version  TEXT,
    n_chunks         INTEGER,
    n_pages          INTEGER,
    ingested_at      TEXT
);
"""

CHUNK_COLUMNS = [
    "doc_id", "ticker", "doc_type", "period", "period_label",
    "page_start", "page_end", "section", "text", "word_count",
]
INSERT_CHUNKS_SQL = (
    f"INSERT INTO chunks ({', '.join(CHUNK_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in CHUNK_COLUMNS)})"
)

DOC_LABELS = {"annual_report": "Annual report", "transcript": "Earnings call transcript"}
_index_cache = {}


@contextmanager
def _db():
    with get_connection() as conn:
        conn.executescript(RAG_SCHEMA)
        yield conn


# ---------- Ingesting documents ----------


def ingest_document(doc: dict, progress=None) -> dict:
    """Read one PDF, chunk it and store the chunks. Skips documents that haven't changed."""
    doc_id = doc["doc_id"]
    with _db() as conn:
        row = conn.execute(
            "SELECT sha256, chunker_version FROM ingest_log WHERE doc_id = ?", (doc_id,)
        ).fetchone()
    if row and row[0] == (doc.get("sha256") or None) and row[1] == CHUNKER_VERSION:
        return {"status": "skipped", "chunks": 0, "pages": 0}

    path = documents.document_path(doc["local_path"])
    pages = chunker.clean_pages(chunker.extract_pages(path, progress=progress))
    chunks = chunker.make_chunks(pages, doc["doc_type"], CHUNK_WORDS, CHUNK_OVERLAP_WORDS)
    rows = [
        {
            "doc_id": doc_id,
            "ticker": doc["ticker"],
            "doc_type": doc["doc_type"],
            "period": doc["period"],
            "period_label": doc["period_label"],
            "page_start": c["page_start"],
            "page_end": c["page_end"],
            "section": c["section"],
            "text": c["text"],
            "word_count": c["word_count"],
        }
        for c in chunks
    ]
    with _db() as conn:
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.executemany(INSERT_CHUNKS_SQL, rows)
        conn.execute(
            "INSERT OR REPLACE INTO ingest_log VALUES (?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                doc.get("sha256") or None,
                CHUNKER_VERSION,
                len(rows),
                len(pages),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
    return {"status": "ingested", "chunks": len(rows), "pages": len(pages)}


# ---------- Embedding and indexing ----------


def embedding_text(ticker: str, doc_type: str, period_label: str, section: str, text: str) -> str:
    """What gets embedded: a short context header plus the passage."""
    return f"{ticker} | {DOC_LABELS.get(doc_type, doc_type)} {period_label} | {section}\n{text}"


def embed_pending(ticker: str, progress=None) -> int:
    """Embed every chunk of this company that has no vector from the current model."""
    model_id = embedder.backend_name()
    with _db() as conn:
        rows = conn.execute(
            "SELECT chunk_id, ticker, doc_type, period_label, section, text FROM chunks "
            "WHERE ticker = ? AND (embedding IS NULL OR embed_model IS NULL OR embed_model != ?)",
            (ticker, model_id),
        ).fetchall()
    total = len(rows)
    step = EMBED_BATCH_SIZE * 4
    for start in range(0, total, step):
        batch = rows[start : start + step]
        vectors = embedder.embed_documents([embedding_text(*row[1:]) for row in batch])
        with _db() as conn:
            conn.executemany(
                "UPDATE chunks SET embedding = ?, embed_model = ? WHERE chunk_id = ?",
                [(vec.astype("float32").tobytes(), model_id, row[0]) for row, vec in zip(batch, vectors)],
            )
        if progress:
            progress(min(start + step, total), total)
    return total


def build_index(ticker: str) -> int:
    """Build this company's FAISS index from its stored embeddings."""
    import faiss

    with _db() as conn:
        rows = conn.execute(
            "SELECT chunk_id, embedding FROM chunks WHERE ticker = ? AND embedding IS NOT NULL ORDER BY chunk_id",
            (ticker,),
        ).fetchall()
    if not rows:
        return 0
    ids = np.array([r[0] for r in rows], dtype="int64")
    vectors = np.vstack([np.frombuffer(r[1], dtype="float32") for r in rows])
    index = faiss.IndexIDMap(faiss.IndexFlatIP(vectors.shape[1]))  # inner product on unit vectors = cosine
    index.add_with_ids(vectors, ids)
    folder = RAG_DIR / ticker
    folder.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(folder / "faiss.index"))
    _index_cache.pop(ticker, None)
    return len(rows)


def _load_index(ticker: str):
    import faiss

    path = RAG_DIR / ticker / "faiss.index"
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    cached = _index_cache.get(ticker)
    if cached and cached[0] == mtime:
        return cached[1]
    index = faiss.read_index(str(path))
    _index_cache[ticker] = (mtime, index)
    return index


def ingest_company(ticker: str, doc_types=None, log=None) -> dict:
    """Ingest all usable documents for a company, embed new chunks and rebuild its index."""
    ticker = ticker.strip().upper()
    log = log or (lambda message: None)
    docs = documents.list_documents(ticker)
    usable = docs[docs["status"] == "ok"] if not docs.empty else docs
    if doc_types and not usable.empty:
        usable = usable[usable["doc_type"].isin(doc_types)]

    summary = {"documents": len(usable), "ingested": 0, "skipped": 0, "chunks": 0,
               "embedded": 0, "indexed": 0, "problems": []}
    for _, doc in usable.iterrows():
        label = f"{doc['doc_type']} {doc['period_label']}"
        try:
            result = ingest_document(
                doc.to_dict(), progress=lambda done, total, l=label: log(f"  reading {l}: page {done}/{total}")
            )
        except Exception as err:
            summary["problems"].append(f"{label}: {err}")
            continue
        if result["status"] == "skipped":
            summary["skipped"] += 1
        else:
            summary["ingested"] += 1
            summary["chunks"] += result["chunks"]
            log(f"{label}: {result['pages']} pages -> {result['chunks']} passages")

    log("Embedding passages (the first run downloads the embedding model)...")
    summary["embedded"] = embed_pending(ticker, progress=lambda done, total: log(f"  embedded {done}/{total}"))
    summary["indexed"] = build_index(ticker)
    return summary


def index_status(ticker: str) -> dict:
    ticker = ticker.strip().upper()
    with _db() as conn:
        chunks, docs = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT doc_id) FROM chunks WHERE ticker = ?", (ticker,)
        ).fetchone()
    return {"chunks": chunks, "documents": docs, "indexed": (RAG_DIR / ticker / "faiss.index").exists()}


# ---------- Searching ----------


def make_citation(result: dict) -> str:
    start, end = result["page_start"], result["page_end"]
    pages = f"p. {start}" if start == end else f"pp. {start}-{end}"
    label = DOC_LABELS.get(result["doc_type"], result["doc_type"])
    citation = f"{label} {result['period_label']} · PDF {pages}"
    return f"{citation} · {result['section']}" if result["section"] else citation


def search_documents(ticker: str, query: str, k: int = RAG_TOP_K, doc_types=None) -> list:
    """Top-k passages for a question, each with a citation. Searches wide, then filters by document type."""
    ticker = ticker.strip().upper()
    index = _load_index(ticker)
    if index is None or index.ntotal == 0 or not query.strip():
        return []

    query_vector = embedder.embed_query(query).astype("float32")
    n = min(index.ntotal, max(k * 10, 60))
    scores, ids = index.search(query_vector, n)
    pairs = [(int(i), float(s)) for i, s in zip(ids[0], scores[0]) if i != -1]
    if not pairs:
        return []

    with _db() as conn:
        rows = conn.execute(
            "SELECT chunk_id, doc_id, doc_type, period, period_label, page_start, page_end, section, text "
            f"FROM chunks WHERE chunk_id IN ({','.join('?' * len(pairs))})",
            [chunk_id for chunk_id, _ in pairs],
        ).fetchall()
    by_id = {row[0]: row for row in rows}

    docs = documents.list_documents(ticker)
    urls = dict(zip(docs["doc_id"], docs["url"])) if not docs.empty else {}

    results = []
    for chunk_id, score in pairs:
        row = by_id.get(chunk_id)
        if row is None or (doc_types and row[2] not in doc_types):
            continue
        result = {
            "chunk_id": chunk_id,
            "score": score,
            "doc_id": row[1],
            "doc_type": row[2],
            "period": row[3],
            "period_label": row[4],
            "page_start": row[5],
            "page_end": row[6],
            "section": row[7],
            "text": row[8],
            "url": urls.get(row[1], ""),
        }
        result["citation"] = make_citation(result)
        results.append(result)
        if len(results) == k:
            break
    return results


# ---------- Tests, evaluation and command line ----------


def run_self_test() -> None:
    """Plumbing test with fake embeddings and a throw-away database: no PDFs, no model download."""
    import tempfile

    from src import db as db_module

    global RAG_DIR
    embedder.use_fake_backend()
    with tempfile.TemporaryDirectory() as folder:
        db_module.DB_PATH = Path(folder) / "test.db"
        RAG_DIR = Path(folder) / "index"

        def row(doc_id, doc_type, text, page):
            return {
                "doc_id": doc_id, "ticker": "TEST", "doc_type": doc_type, "period": "X",
                "period_label": "FY2025", "page_start": page, "page_end": page,
                "section": "Test section", "text": text, "word_count": len(text.split()),
            }

        rows = [
            row("TEST:transcript:1", "transcript", "management expects operating margin guidance of 21 to 23 percent for the coming year", 3),
            row("TEST:annual_report:1", "annual_report", "the company paid a final dividend and announced the record date", 10),
            row("TEST:transcript:1", "transcript", "attrition declined and utilization improved across delivery centers", 4),
        ]
        with _db() as conn:
            conn.executemany(INSERT_CHUNKS_SQL, rows)

        assert embed_pending("TEST") == 3
        assert embed_pending("TEST") == 0, "embeddings must be cached, not recomputed"
        assert build_index("TEST") == 3
        assert index_status("TEST") == {"chunks": 3, "documents": 2, "indexed": True}

        top = search_documents("TEST", "operating margin guidance", k=3)
        assert top and "operating margin guidance" in top[0]["text"], top
        assert top[0]["citation"].endswith("Test section") and "PDF p. 3" in top[0]["citation"]

        only_reports = search_documents("TEST", "operating margin guidance", k=3, doc_types=["annual_report"])
        assert len(only_reports) == 1 and only_reports[0]["doc_type"] == "annual_report"
        assert search_documents("NOPE", "anything") == []
    print("All RAG plumbing tests passed.")


def run_eval() -> None:
    """Retrieval hit-rate: for each question, does a top-5 passage contain the phrase you expect?"""
    path = DATA_DIR / "eval" / "questions.csv"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "ticker,question,doc_type,expected_phrase\n"
            "INFY,What did management say about operating margin guidance?,transcript,EDIT_ME\n"
            "INFY,What are the main risks the company describes?,annual_report,EDIT_ME\n",
            encoding="utf-8",
        )
        print(f"Created a template at {path}. Replace EDIT_ME with a distinctive phrase you saw in the PDF.")
        return

    questions = pd.read_csv(path).fillna("")
    hits = checked = 0
    for _, q in questions.iterrows():
        phrase = " ".join(str(q["expected_phrase"]).lower().split())
        if not phrase or phrase == "edit_me":
            print("SKIP (no expected phrase):", q["question"])
            continue
        types = [q["doc_type"]] if q["doc_type"] else None
        results = search_documents(q["ticker"], q["question"], k=5, doc_types=types)
        found = any(phrase in " ".join(r["text"].lower().split()) for r in results)
        checked += 1
        hits += found
        print("HIT " if found else "MISS", "|", q["ticker"], "|", q["question"])
    if checked:
        print(f"\nRetrieval hit-rate @5: {hits}/{checked} = {hits / checked:.0%}")


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if args[:1] == ["ingest"] and len(args) >= 2:
        types = [args[2]] if len(args) >= 3 else None
        start = time.time()
        print(ingest_company(args[1], doc_types=types, log=print))
        print(f"Done in {time.time() - start:.0f}s")
    elif args[:1] == ["search"] and len(args) >= 3:
        for result in search_documents(args[1], " ".join(args[2:]), k=5):
            print(f"\n[{result['score']:.2f}] {result['citation']}")
            print(result["text"][:350], "...")
    elif args[:1] == ["status"] and len(args) >= 2:
        print(index_status(args[1]))
    elif args[:1] == ["eval"]:
        run_eval()
    else:
        run_self_test()