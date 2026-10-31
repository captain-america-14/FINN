from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UNIVERSE_CSV = DATA_DIR / "universe.csv"

# yfinance uses ".NS" for NSE-listed stocks (e.g. INFY.NS)
EXCHANGE_SUFFIX = ".NS"