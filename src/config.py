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
# --- Corporate announcements ---
DB_PATH = DATA_DIR / "finn.db"
BSE_CACHE_DIR = DATA_DIR / "cache" / "bse"
BSE_ATTACHMENT_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
ANNOUNCEMENT_LOOKBACK_DAYS = 30     # how far back each refresh looks
ANNOUNCEMENT_REFRESH_MINUTES = 30   # skip companies fetched more recently than this
ANNOUNCEMENT_MAX_PAGES = 5          # safety cap on pagination per company
BSE_REQUEST_PAUSE_SEC = 0.5         # polite pause between companies
# --- Alert settings: bounds for the user-configurable thresholds ---
PRICE_PCT_RANGE = (0.5, 15.0)
VOLUME_RATIO_RANGE = (1.2, 10.0)
AVG_WINDOW_RANGE = (5, 60)
ALERT_FILING_LOOKBACK_DAYS = 3   # how far back to look for filings near a flagged stock
# --- Quarterly results (Screener.in) ---
SCREENER_BASE_URL = "https://www.screener.in"
SCREENER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
SCREENER_TIMEOUT_SEC = 20
SCREENER_PAUSE_SEC = 1.5      # polite gap between requests
RESULTS_REFRESH_HOURS = 12    # don't refetch a company more often than this