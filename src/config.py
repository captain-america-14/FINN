from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UNIVERSE_CSV = DATA_DIR / "universe.csv"

# yfinance uses ".NS" for NSE-listed stocks (e.g. INFY.NS)
EXCHANGE_SUFFIX = ".NS"
# --- Market data settings ---
PRICE_CACHE_DIR = DATA_DIR / "cache" / "prices"
PRICE_PERIOD = "6mo"          # history window to download
AVG_VOLUME_WINDOW = 20        # trading days used for the volume baseline
PRICE_ALERT_PCT = 3.0         # flag moves of +/- this % in a day
VOLUME_ALERT_RATIO = 2.0      # flag volume >= this multiple of the average