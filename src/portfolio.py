import pandas as pd

from src.config import EXCHANGE_SUFFIX, UNIVERSE_CSV

REQUIRED_COLUMNS = ["ticker", "name", "sector", "group", "shares"]
VALID_GROUPS = {"portfolio", "research"}


def to_yf_symbol(ticker: str) -> str:
    """Convert an NSE ticker like 'INFY' into a yfinance symbol like 'INFY.NS'.
    Works for ANY NSE ticker, not just the ones in our CSV."""
    return ticker.strip().upper() + EXCHANGE_SUFFIX


def load_universe() -> pd.DataFrame:
    """Load and validate the full company universe (portfolio + research)."""
    if not UNIVERSE_CSV.exists():
        raise FileNotFoundError(f"Universe file not found at {UNIVERSE_CSV}")

    df = pd.read_csv(UNIVERSE_CSV)

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"universe.csv is missing columns: {missing}")

    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()

    duplicates = df.loc[df["ticker"].duplicated(), "ticker"].tolist()
    if duplicates:
        raise ValueError(f"Duplicate tickers in universe.csv: {duplicates}")

    bad_groups = set(df["group"]) - VALID_GROUPS
    if bad_groups:
        raise ValueError(f"Invalid group values: {bad_groups}. Use {VALID_GROUPS}")

    df["yf_symbol"] = df["ticker"].apply(to_yf_symbol)
    return df


def get_portfolio() -> pd.DataFrame:
    df = load_universe()
    return df[df["group"] == "portfolio"].reset_index(drop=True)


def get_research_extras() -> pd.DataFrame:
    df = load_universe()
    return df[df["group"] == "research"].reset_index(drop=True)


def get_company(ticker: str):
    """Return one company as a dict, or None if it's not in our universe."""
    df = load_universe()
    match = df[df["ticker"] == ticker.strip().upper()]
    if match.empty:
        return None
    return match.iloc[0].to_dict()


if __name__ == "__main__":
    portfolio = get_portfolio()
    extras = get_research_extras()
    print(f"Portfolio companies: {len(portfolio)}")
    print(f"Research extras:     {len(extras)}")
    print(portfolio[["ticker", "yf_symbol", "sector"]].to_string(index=False))
    print("Lookup test:", get_company("infy"))
    print("Unknown ticker:", get_company("NOTREAL"))
    print("Any NSE ticker:", to_yf_symbol("polycab"))