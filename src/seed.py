import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from src.config import BASE_DIR, DB_PATH, PRICE_CACHE_DIR, RAG_DIR

SEED_DIR = BASE_DIR / "seed"
SEED_DB = SEED_DIR / "finn_seed.sqlite3"
SEED_INFO = SEED_DIR / "seed_info.json"


def is_demo_mode() -> bool:
    return os.environ.get("FINN_DEMO_MODE", "").strip() == "1"


def apply_secrets() -> None:
    """Copy Streamlit Cloud secrets into environment variables (the rest of the code reads os.environ)."""
    try:
        import streamlit as st

        for key in ("GEMINI_API_KEY", "GEMINI_MODEL", "FINN_DEMO_MODE"):
            if key in st.secrets and not os.environ.get(key):
                os.environ[key] = str(st.secrets[key])
    except Exception:
        pass  # no secrets configured, as on a local run


def demo_info():
    try:
        return json.loads(SEED_INFO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _copy_if_missing(source: Path, target: Path) -> bool:
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return True


def seed_if_needed() -> bool:
    """On a fresh machine, copy the bundled demo data into place. Never overwrites existing data."""
    copied = False
    if SEED_DB.exists():
        copied |= _copy_if_missing(SEED_DB, DB_PATH)
    index_root = SEED_DIR / "index"
    if index_root.exists():
        for folder in index_root.iterdir():
            source = folder / "faiss.index"
            if folder.is_dir() and source.exists():
                copied |= _copy_if_missing(source, RAG_DIR / folder.name / "faiss.index")
    prices = SEED_DIR / "prices"
    if prices.exists():
        for source in prices.glob("*.csv"):
            copied |= _copy_if_missing(source, PRICE_CACHE_DIR / source.name)
    return copied


def build_seed(tickers: list) -> None:
    """Package the local data for the given companies into seed/ (run this before committing)."""
    tickers = [t.strip().upper() for t in tickers]
    if not DB_PATH.exists():
        raise SystemExit("No local database found. Run the app and collect some data first.")
    SEED_DIR.mkdir(exist_ok=True)
    SEED_DB.unlink(missing_ok=True)

    source = sqlite3.connect(DB_PATH)
    target = sqlite3.connect(SEED_DB)
    source.backup(target)  # a consistent copy, even if the app is open
    source.close()

    marks = ",".join("?" * len(tickers))
    statements = [
        (f"DELETE FROM chunks WHERE ticker NOT IN ({marks})", tickers),
        ("UPDATE chunks SET embedding = NULL", []),  # the vectors live in the FAISS index
        ("DELETE FROM ingest_log WHERE doc_id NOT IN (SELECT DISTINCT doc_id FROM chunks)", []),
        (f"DELETE FROM answers WHERE ticker NOT IN ({marks})", tickers),
    ]
    for sql, params in statements:
        try:
            target.execute(sql, params)
        except sqlite3.OperationalError as err:
            print("Skipped (table not present):", err)
    target.commit()
    target.execute("VACUUM")
    target.close()

    for ticker in tickers:
        index = RAG_DIR / ticker / "faiss.index"
        if index.exists():
            destination = SEED_DIR / "index" / ticker / "faiss.index"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(index, destination)
        else:
            print(f"WARNING: no index for {ticker}. Run: python -m src.rag ingest {ticker}")
    if PRICE_CACHE_DIR.exists():
        (SEED_DIR / "prices").mkdir(exist_ok=True)
        for csv in PRICE_CACHE_DIR.glob("*.csv"):
            shutil.copyfile(csv, SEED_DIR / "prices" / csv.name)

    SEED_INFO.write_text(
        json.dumps({"built_at": datetime.now().isoformat(timespec="seconds"), "tickers": tickers}),
        encoding="utf-8",
    )
    report_sizes()


def report_sizes() -> None:
    total = 0
    for path in sorted(SEED_DIR.rglob("*")):
        if path.is_file():
            size = path.stat().st_size
            total += size
            flag = "   <-- TOO BIG for GitHub (100 MB limit)" if size > 90_000_000 else ""
            print(f"{size / 1e6:8.1f} MB  {path.relative_to(BASE_DIR)}{flag}")
    print(f"{total / 1e6:8.1f} MB  total")


def run_self_test() -> None:
    global SEED_DIR, SEED_DB, DB_PATH, RAG_DIR, PRICE_CACHE_DIR
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        SEED_DIR = root / "seed"
        SEED_DB = SEED_DIR / "finn_seed.sqlite3"
        (SEED_DIR / "index" / "AAA").mkdir(parents=True)
        (SEED_DIR / "prices").mkdir()
        SEED_DB.write_bytes(b"db")
        (SEED_DIR / "index" / "AAA" / "faiss.index").write_bytes(b"ix")
        (SEED_DIR / "prices" / "AAA.csv").write_text("x")
        DB_PATH = root / "data" / "finn.db"
        RAG_DIR = root / "data" / "index"
        PRICE_CACHE_DIR = root / "data" / "cache" / "prices"

        assert seed_if_needed() is True
        assert DB_PATH.read_bytes() == b"db"
        assert (RAG_DIR / "AAA" / "faiss.index").read_bytes() == b"ix"
        assert (PRICE_CACHE_DIR / "AAA.csv").exists()

        DB_PATH.write_bytes(b"my own data")
        assert seed_if_needed() is False
        assert DB_PATH.read_bytes() == b"my own data", "existing data must never be overwritten"
    os.environ["FINN_DEMO_MODE"] = "1"
    assert is_demo_mode()
    print("All seed tests passed.")


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if args[:1] == ["build"] and len(args) >= 2:
        build_seed(args[1:])
    elif args[:1] == ["sizes"]:
        report_sizes()
    else:
        run_self_test()