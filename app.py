import streamlit as st

st.set_page_config(
    page_title="FINN - Financial Investment Agent",
    page_icon="📈",
    layout="centered",
    initial_sidebar_state="collapsed",
)

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


def render_home():
    st.subheader("Home")
    st.markdown('<span class="finn-tag">PLACEHOLDER</span>', unsafe_allow_html=True)
    col1, col2 = st.columns(2)
    col1.metric("Portfolio stocks", "18")
    col2.metric("High-priority alerts", "-")
    st.markdown(
        '<div class="finn-card">Portfolio overview, key developments and quick research will appear here.</div>',
        unsafe_allow_html=True,
    )


def render_feed():
    st.subheader("Intelligence Feed")
    st.markdown(
        '<div class="finn-card">Prioritised corporate announcements with AI explanations will appear here.</div>',
        unsafe_allow_html=True,
    )


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