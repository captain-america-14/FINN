import html

import pandas as pd
import streamlit as st

from src.alerts import (
    evaluate_alerts,
    load_alert_config,
    related_filings,
    reset_alert_config,
    save_alert_config,
)
from src.announcements import get_announcements, refresh_announcements
from src.classifier import classify_pending, short_title
from src.config import ALERT_FILING_LOOKBACK_DAYS
from src.market_data import build_snapshot_table, fetch_prices
from src.metrics import portfolio_totals
from src.portfolio import get_portfolio, get_research_extras

st.set_page_config(
    page_title="FINN - Financial Investment Agent",
    page_icon="📈",
    layout="centered",
    initial_sidebar_state="collapsed",
)

MAX_FEED_ITEMS = 40
MAX_HOME_DEVELOPMENTS = 5
MAX_HOME_ALERTS = 6
PRIORITY_OPTIONS = ["High", "Medium", "Low"]

CUSTOM_CSS = """
<style>
.block-container { max-width: 480px; padding-top: 1rem; padding-bottom: 3rem; }
.finn-header {
    background: linear-gradient(135deg, #0B5FFF, #0A2A66);
    color: white; padding: 16px 18px; border-radius: 14px; margin-bottom: 14px;
}
.finn-header h1 { margin: 0; font-size: 1.5rem; letter-spacing: 1px; }
.finn-header p { margin: 2px 0 0 0; font-size: 0.85rem; opacity: 0.85; }
.finn-card {
    background: white; border: 1px solid #E2E8F0; border-radius: 12px;
    padding: 12px 14px; margin-bottom: 10px;
}
.finn-tag {
    display: inline-block; font-size: 0.7rem; font-weight: 600;
    padding: 2px 8px; border-radius: 999px; background: #FEF3C7; color: #92400E;
}
.finn-badge {
    display: inline-block; font-size: 0.68rem; font-weight: 700;
    padding: 2px 8px; border-radius: 999px; margin-right: 6px;
}
.finn-high { background: #FEE2E2; color: #991B1B; }
.finn-medium { background: #FEF3C7; color: #92400E; }
.finn-low { background: #E2E8F0; color: #475569; }
</style>
"""


def inject_css():
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


def render_header():
    st.markdown(
        """
        <div class="finn-header">
            <h1>FINN</h1>
            <p>Financial Investment Agent</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------- Shared pieces ----------


def announcement_card_html(row) -> str:
    title = html.escape(row["title"] or "(no title)")
    meta = html.escape(f"{row['ticker']} · {row['filing_dt'] or 'date n/a'}")
    category = html.escape(row.get("fin_category") or "Unclassified")
    reason = html.escape(row.get("reason") or "")

    priority = row.get("priority") or ""
    badge = ""
    if priority in PRIORITY_OPTIONS:
        badge = f'<span class="finn-badge finn-{priority.lower()}">{priority.upper()}</span>'

    url = row["filing_url"] or row["page_url"] or ""
    link = ""
    if url.startswith(("https://", "http://")):
        link = f'<a href="{html.escape(url, quote=True)}" target="_blank">Original filing ↗</a>'

    return (
        '<div class="finn-card">'
        f'<div>{badge}<span style="font-size:0.75rem;color:#64748B">{meta}</span></div>'
        f'<div style="font-weight:600;margin:4px 0">{title}</div>'
        f'<div style="font-size:0.8rem;color:#475569;margin-bottom:4px">{category} · {reason}</div>'
        f"{link}</div>"
    )


def alert_card_html(alert: dict, filings: pd.DataFrame) -> str:
    ticker = html.escape(alert["ticker"])
    name = html.escape(alert["name"])
    reasons = "<br>".join(html.escape(r) for r in alert["reasons"])

    filing_html = ""
    if not filings.empty:
        lines = []
        for _, f in filings.head(2).iterrows():
            text = html.escape(short_title(f["title"])[:90])
            url = f["filing_url"] or f["page_url"] or ""
            if url.startswith(("https://", "http://")):
                text = f'<a href="{html.escape(url, quote=True)}" target="_blank">{text}</a>'
            tag = html.escape(f["priority"] or "")
            lines.append(f'<div style="font-size:0.8rem;margin-top:2px">[{tag}] {text}</div>')
        filing_html = (
            '<div style="font-size:0.72rem;color:#64748B;margin-top:6px">'
            f"Filings in the last {ALERT_FILING_LOOKBACK_DAYS} days (not necessarily the cause):</div>"
            + "".join(lines)
        )

    return (
        '<div class="finn-card">'
        f'<div style="font-weight:700">{ticker} '
        f'<span style="font-weight:400;color:#64748B">· {name}</span></div>'
        f'<div style="font-size:0.85rem;margin-top:4px">{reasons}</div>'
        f"{filing_html}</div>"
    )


@st.cache_data(ttl=900, show_spinner="Fetching market data...")
def load_prices():
    portfolio = get_portfolio()
    return fetch_prices(portfolio["ticker"].tolist())


@st.cache_data
def load_research_extras():
    return get_research_extras()


def format_flags(row):
    flags = []
    if row["price_alert"]:
        flags.append("⚡")
    if row["volume_alert"]:
        flags.append("📊")
    if row["stale"]:
        flags.append("🕒")
    return " ".join(flags)


# ---------- Screens ----------


def render_alerts(alerts: list):
    st.markdown(f"**Market alerts · {len(alerts)} stock(s) flagged**")
    if not alerts:
        st.caption("No stock crossed its price or volume threshold in the latest session.")
        return
    shown = alerts[:MAX_HOME_ALERTS]
    filings = related_filings([a["ticker"] for a in shown])
    cards = "".join(
        alert_card_html(a, filings[filings["ticker"] == a["ticker"]]) for a in shown
    )
    st.markdown(cards, unsafe_allow_html=True)
    if len(alerts) > len(shown):
        st.caption(f"+ {len(alerts) - len(shown)} more flagged stocks (see the table below).")


def render_alert_settings(portfolio: pd.DataFrame, cfg: dict):
    with st.expander("⚙️ Alert settings"):
        price_pct = st.slider(
            "Price move alert (± % in a day)", 0.5, 15.0, float(cfg["price_pct"]), 0.5
        )
        volume_ratio = st.slider(
            "Volume alert (× average volume)", 1.2, 10.0, float(cfg["volume_ratio"]), 0.1
        )
        avg_window = st.slider(
            "Average volume window (trading days)", 5, 60, int(cfg["avg_window"]), 5
        )

        col_a, col_b = st.columns(2)
        if col_a.button("Save thresholds"):
            save_alert_config(
                {**cfg, "price_pct": price_pct, "volume_ratio": volume_ratio, "avg_window": avg_window}
            )
            st.rerun()
        if col_b.button("Reset to defaults"):
            reset_alert_config()
            st.rerun()

        st.markdown("**Per-stock override** (optional)")
        ticker = st.selectbox("Stock", portfolio["ticker"].tolist(), key="ov_ticker")
        current = cfg["overrides"].get(ticker, {})
        ov_price = st.number_input(
            "Price alert for this stock (± %)",
            min_value=0.5,
            max_value=15.0,
            value=float(current.get("price_pct", cfg["price_pct"])),
            step=0.5,
            key=f"ov_price_{ticker}",
        )
        ov_volume = st.number_input(
            "Volume alert for this stock (×)",
            min_value=1.2,
            max_value=10.0,
            value=float(current.get("volume_ratio", cfg["volume_ratio"])),
            step=0.1,
            key=f"ov_volume_{ticker}",
        )
        col_c, col_d = st.columns(2)
        if col_c.button("Save override"):
            overrides = dict(cfg["overrides"])
            overrides[ticker] = {"price_pct": ov_price, "volume_ratio": ov_volume}
            save_alert_config({**cfg, "overrides": overrides})
            st.rerun()
        if col_d.button("Remove override"):
            overrides = dict(cfg["overrides"])
            overrides.pop(ticker, None)
            save_alert_config({**cfg, "overrides": overrides})
            st.rerun()

        if cfg["overrides"]:
            parts = [
                f"{t} (±{thresholds['price_pct']:g}%, {thresholds['volume_ratio']:g}×)"
                for t, thresholds in (
                    (t, {"price_pct": o.get("price_pct", cfg["price_pct"]),
                         "volume_ratio": o.get("volume_ratio", cfg["volume_ratio"])})
                    for t, o in cfg["overrides"].items()
                )
            ]
            st.caption("Active overrides: " + ", ".join(parts))


def render_top_developments():
    try:
        portfolio = get_portfolio()
        df = get_announcements(portfolio["ticker"].tolist(), days=7, priorities=["High"])
    except Exception as err:
        st.caption(f"Filings unavailable: {err}")
        return

    st.markdown("**High-priority developments · last 7 days**")
    if df.empty:
        st.caption("None stored yet. Open the Feed tab and fetch the latest filings.")
        return
    cards = "".join(
        announcement_card_html(r) for _, r in df.head(MAX_HOME_DEVELOPMENTS).iterrows()
    )
    st.markdown(cards, unsafe_allow_html=True)


def render_home():
    st.subheader("Home")
    try:
        portfolio = get_portfolio()
        prices = load_prices()
        cfg = load_alert_config()
        table = build_snapshot_table(portfolio, prices, cfg)
        extras = load_research_extras()
    except (FileNotFoundError, ValueError) as err:
        st.error(f"Could not load the company list: {err}")
        return

    st.markdown(
        '<span class="finn-tag">DEMO HOLDINGS · MARKET PRICES</span>',
        unsafe_allow_html=True,
    )

    ok = table[table["has_data"]]
    if ok.empty:
        st.error("No market data available right now. Check your connection and retry.")
        if st.button("Retry"):
            st.cache_data.clear()
            st.rerun()
        return

    alerts = evaluate_alerts(table, cfg)
    totals = portfolio_totals(table)
    col1, col2 = st.columns(2)
    col1.metric(
        "Demo portfolio value",
        f"₹{totals['value']:,.0f}",
        f"{totals['day_change_pct']:+.2f}% last session",
    )
    col2.metric("Alerts", len(alerts))

    missing = table.loc[~table["has_data"], "ticker"].tolist()
    if missing:
        st.warning("No data for: " + ", ".join(missing))
    if ok["stale"].any():
        st.info("Some prices come from a saved copy because live data was unavailable.")

    render_alerts(alerts)
    render_alert_settings(portfolio, cfg)
    render_top_developments()

    display = pd.DataFrame(
        {
            "Ticker": ok["ticker"],
            "Close ₹": ok["close"],
            "Day %": ok["change_pct"],
            "Vol ×": ok["volume_ratio"],
            "Flags": ok.apply(format_flags, axis=1),
        }
    )
    st.dataframe(
        display,
        hide_index=True,
        width="stretch",
        column_config={
            "Close ₹": st.column_config.NumberColumn(format="%.2f"),
            "Day %": st.column_config.NumberColumn(format="%+.2f"),
            "Vol ×": st.column_config.NumberColumn(format="%.1f"),
        },
    )
    st.caption(
        f"As of {ok['as_of'].max()} close · daily data via Yahoo Finance. "
        f"⚡ move ≥ {cfg['price_pct']:g}% · 📊 volume ≥ {cfg['volume_ratio']:g}× "
        f"{cfg['avg_window']}-day average · 🕒 saved copy"
        + (" · per-stock overrides active" if cfg["overrides"] else "")
    )
    if st.button("Refresh prices"):
        st.cache_data.clear()
        st.rerun()
    st.caption("Also researchable: " + ", ".join(extras["name"]))


def render_feed():
    st.subheader("Intelligence Feed")
    try:
        portfolio = get_portfolio()
    except (FileNotFoundError, ValueError) as err:
        st.error(f"Could not load the company list: {err}")
        return

    choice = st.selectbox("Company", ["All portfolio"] + portfolio["name"].tolist())
    days = st.radio(
        "Period", [1, 7, 30], index=1, horizontal=True, format_func=lambda d: f"Last {d}d"
    )
    priorities = st.multiselect("Priority", PRIORITY_OPTIONS, default=["High", "Medium"])

    if st.button("Fetch latest filings from BSE"):
        with st.spinner("Contacting BSE and classifying..."):
            summary = refresh_announcements(portfolio)
            classify_pending()
        st.success(
            f"{summary['new']} new filings stored from {summary['checked']} companies "
            f"({summary['skipped_fresh']} refreshed recently)."
        )
        for ticker, message in summary["errors"]:
            st.warning(f"{ticker}: {message}")

    if choice == "All portfolio":
        tickers = portfolio["ticker"].tolist()
    else:
        tickers = portfolio.loc[portfolio["name"] == choice, "ticker"].tolist()

    everything = get_announcements(tickers, days)
    if everything.empty:
        st.info("No stored filings for this selection. Fetch from BSE, or widen the period.")
        return

    counts = everything["priority"].value_counts()
    c1, c2, c3 = st.columns(3)
    c1.metric("High", int(counts.get("High", 0)))
    c2.metric("Medium", int(counts.get("Medium", 0)))
    c3.metric("Low", int(counts.get("Low", 0)))

    df = everything[everything["priority"].isin(priorities)] if priorities else everything
    hidden = len(everything) - len(df)
    if df.empty:
        st.info("Nothing matches the selected priorities. Add Low to see routine filings.")
        return

    shown = min(len(df), MAX_FEED_ITEMS)
    st.caption(
        f"Showing {shown} of {len(df)} filings ({hidden} hidden by the priority filter) · "
        "source: BSE · sorted by priority, then newest"
    )
    cards = "".join(announcement_card_html(r) for _, r in df.head(MAX_FEED_ITEMS).iterrows())
    st.markdown(cards, unsafe_allow_html=True)


def render_research():
    st.subheader("Company Research")
    st.markdown(
        '<div class="finn-card">Business, financials, trends and bull/bear analysis will appear here.</div>',
        unsafe_allow_html=True,
    )


def render_ask():
    st.subheader("Ask FINN")
    st.text_input("Ask a question about a company", placeholder="e.g. What did Infosys guide for FY26?")
    st.caption("Grounded answers with source citations will appear here.")


def main():
    inject_css()
    render_header()
    try:
        classify_pending()
    except Exception as err:
        st.warning(f"Classification skipped: {err}")
    tab_home, tab_feed, tab_research, tab_ask = st.tabs(
        ["Home", "Feed", "Research", "Ask FINN"]
    )
    with tab_home:
        render_home()
    with tab_feed:
        render_feed()
    with tab_research:
        render_research()
    with tab_ask:
        render_ask()


main()