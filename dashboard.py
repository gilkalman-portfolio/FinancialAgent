"""
Financial Agent Dashboard
Run: streamlit run dashboard.py
"""
import sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

import streamlit as st
from src.database import init_db
from src.ui_theme import GLOBAL_CSS

st.set_page_config(page_title="Financial Agent", layout="wide", page_icon="📈")
st.markdown(GLOBAL_CSS, unsafe_allow_html=True)

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## Financial Agent")
    st.markdown("---")
    page = st.radio(
        "Navigation",
        ["Scan", "Research", "Watchlist", "Market", "News Impact",
         "Short Squeeze", "Catalyst Scanner", "Options Flow",
         "Backtest", "History", "Scheduler"],
        label_visibility="collapsed",
        key="sidebar_nav"
    )

init_db()

# ── Page Routing ───────────────────────────────────────────────────────────────
if page == "Scan":
    from _pages_modules.page_scan import render
elif page == "Research":
    from _pages_modules.page_research import render
elif page == "Watchlist":
    from _pages_modules.page_watchlist import render
elif page == "Market":
    from _pages_modules.page_market import render
elif page == "News Impact":
    from _pages_modules.page_news_impact import render
elif page == "Short Squeeze":
    from _pages_modules.page_squeeze import render
elif page == "Catalyst Scanner":
    from _pages_modules.page_catalyst import render
elif page == "Options Flow":
    from _pages_modules.page_options_flow import render
elif page == "Backtest":
    from _pages_modules.page_backtest import render
elif page == "History":
    from _pages_modules.page_history import render
elif page == "Scheduler":
    from _pages_modules.page_scheduler import render
else:
    from _pages_modules.page_scan import render

render()
