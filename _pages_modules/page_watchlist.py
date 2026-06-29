"""Page: Watchlist & Portfolio"""
import html as _html_mod
import streamlit as st
import pandas as pd
from datetime import datetime
from src.ui_theme import badge
from src.stock_scorer import signal_label
from src.database import (
    watchlist_get_all, watchlist_add, watchlist_remove, watchlist_update,
    watchlist_get_alerts, portfolio_get_all, portfolio_add,
    portfolio_remove, portfolio_update,
)


def _html(raw: str) -> str:
    return " ".join(raw.split())


@st.cache_data(ttl=300)  # cache 5 min — מחיר נוכחי בלי סריקה מלאה
def _fetch_price(ticker: str) -> float | None:
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return info.get("currentPrice") or info.get("regularMarketPrice")
    except Exception:
        return None


def render():
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=5 * 60 * 1000, key="watchlist_autorefresh")  # 5 min

    st.markdown("### Watchlist & Portfolio")
    tab_watch, tab_port, tab_alerts = st.tabs(["Watchlist", "Portfolio", "Alert History"])
    with tab_watch:
        _render_watchlist()
    with tab_port:
        _render_portfolio()
    with tab_alerts:
        _render_alerts()


# ── Watchlist ──────────────────────────────────────────────────────────────────

def _render_watchlist():
    with st.expander("Add to Watchlist", expanded=not watchlist_get_all()):
        c1, c2, c3 = st.columns([2, 1, 3])
        with c1: new_ticker  = st.text_input("Ticker", placeholder="AAPL", key="wl_ticker").upper().strip()
        with c2: alert_score = st.number_input("Alert score", 0, 100, 60, key="wl_score")
        with c3: notes       = st.text_input("Notes", placeholder="Optional", key="wl_notes")

        c4, c5, c6, c7 = st.columns(4)
        with c4: alert_pct    = st.number_input("Alert % change", 0.0, 50.0, 5.0, step=0.5, key="wl_pct")
        with c5: price_target = st.number_input("🎯 Price target $", 0.0, 99999.0, 0.0, step=0.01, key="wl_target",
                                                 help="Alert every 5 min when price reaches this level (via Scheduler)")
        with c6: price_above  = st.number_input("Price above $", 0.0, 99999.0, 0.0, step=0.01, key="wl_above")
        with c7: price_below  = st.number_input("Price below $", 0.0, 99999.0, 0.0, step=0.01, key="wl_below")

        c8, c9 = st.columns(2)
        with c8: volume_spike_x   = st.number_input("📊 Volume Spike ×", 0.0, 20.0, 0.0, step=0.5, key="wl_vol",
                                                      help="Alert when volume > X × 10d avg. 0 = disabled (בדיקה כל 5 דקות)")
        with c9: supertrend_alert = st.checkbox("📈 Supertrend Alert", key="wl_st",
                                                 help="Telegram alert כשהסופרטרנד מתהפך BUY/SELL (ATR 10, mult 3.0)")

        if st.button("Add", key="wl_add") and new_ticker:
            watchlist_add(
                new_ticker,
                notes           = notes,
                alert_score     = int(alert_score),
                alert_pct       = float(alert_pct),
                price_target    = float(price_target)  if price_target    > 0 else None,
                price_above     = float(price_above)   if price_above     > 0 else None,
                price_below     = float(price_below)   if price_below     > 0 else None,
                volume_spike_x  = float(volume_spike_x) if volume_spike_x > 0 else 0,
                supertrend_alert= int(supertrend_alert),
            )
            st.success(f"{new_ticker} added to watchlist")
            st.rerun()

    watchlist = watchlist_get_all()
    if not watchlist:
        st.info("Watchlist is empty.")
        return

    # Auto-scan once per session on first load — only on trading days (Mon-Fri)
    from zoneinfo import ZoneInfo
    _trading_day = datetime.now(ZoneInfo("America/New_York")).weekday() < 5
    if "wl_results" not in st.session_state:
        if not _trading_day:
            st.info("📅 סוף שבוע — הסריקה האוטומטית מושבתת. לחץ 'Scan All' לסריקה ידנית.")
            st.session_state["wl_results"] = {}
            st.session_state["wl_news"]    = {}
            st.session_state["wl_ts"]      = "—"
        else:
            with st.spinner("Scanning watchlist..."):
                from src.watchlist_manager import scan_watchlist, get_watchlist_news
                results  = scan_watchlist()
                tickers  = [w["ticker"] for w in watchlist]
                news_map = get_watchlist_news(tickers, days=2)
                st.session_state["wl_results"] = {r["ticker"]: r for r in results}
                st.session_state["wl_news"]    = news_map
                st.session_state["wl_ts"]      = datetime.now().strftime("%H:%M:%S")

    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("Scan All", key="wl_scan_all"):
            with st.spinner("Scanning..."):
                from src.watchlist_manager import scan_watchlist, get_watchlist_news
                results  = scan_watchlist()
                tickers  = [w["ticker"] for w in watchlist]
                news_map = get_watchlist_news(tickers, days=2)
                st.session_state["wl_results"] = {r["ticker"]: r for r in results}
                st.session_state["wl_news"]    = news_map
                st.session_state["wl_ts"]      = datetime.now().strftime("%H:%M:%S")
            st.rerun()
    with c2:
        if st.session_state.get("wl_ts"):
            st.caption(f"Last scan: {st.session_state['wl_ts']} · alerts sent to Telegram on threshold breach")

    results_map = st.session_state.get("wl_results", {})
    news_map    = st.session_state.get("wl_news", {})

    # Render cards in rows of 3
    items = watchlist
    for i in range(0, len(items), 3):
        row = items[i:i+3]
        cols = st.columns(3)
        for col, item in zip(cols, row):
            with col:
                _render_watch_card_v2(item, results_map.get(item["ticker"]), news_map.get(item["ticker"], []))


@st.cache_data(ttl=300)
def _last_db_score(ticker: str):
    """Fetch last score + scanned_at from scan_results DB for display when not in session."""
    try:
        import sqlite3, json
        from pathlib import Path
        db = Path("data/financial_agent.db")
        if not db.exists():
            return None, None
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT raw_data, scanned_at FROM scan_results WHERE ticker=? ORDER BY scanned_at DESC LIMIT 1",
            (ticker,)
        ).fetchone()
        conn.close()
        if not row:
            return None, None
        data = json.loads(row[0])
        return data.get("score"), row[1][:16] if row[1] else None
    except Exception:
        return None, None


def _render_watch_card_v2(item, r, news):
    ticker       = item["ticker"]
    editing_key  = f"wl_editing_{ticker}"
    is_editing   = st.session_state.get(editing_key, False)
    price_target = item.get("price_target")
    price_above  = item.get("price_above")
    price_below  = item.get("price_below")

    if is_editing:
        st.markdown(f"**Edit: {ticker}**")
        e_score = st.number_input("Alert score", 0, 100, int(item.get("alert_score", 60)), key=f"we_score_{ticker}")
        e_pct   = st.number_input("Alert % change", 0.0, 50.0, float(item.get("alert_pct", 5.0)), step=0.5, key=f"we_pct_{ticker}")
        e_notes = st.text_input("Notes", value=item.get("notes", ""), key=f"we_notes_{ticker}")
        e_target = st.number_input("🎯 Target $", 0.0, 99999.0, float(item.get("price_target") or 0), step=0.01, key=f"we_target_{ticker}")
        e_above  = st.number_input("Above $", 0.0, 99999.0, float(item.get("price_above") or 0), step=0.01, key=f"we_above_{ticker}")
        e_below  = st.number_input("Below $", 0.0, 99999.0, float(item.get("price_below") or 0), step=0.01, key=f"we_below_{ticker}")
        e_vol_x  = st.number_input("📊 Volume Spike ×", 0.0, 20.0, float(item.get("volume_spike_x") or 0), step=0.5, key=f"we_vol_{ticker}",
                                    help="Alert when volume > X × avg. 0 = disabled")
        e_st     = st.checkbox("📈 Supertrend Alert", value=bool(item.get("supertrend_alert")), key=f"we_st_{ticker}",
                               help="Alert on BUY/SELL trend flip (ATR 10, mult 3.0)")
        cs, cc = st.columns(2)
        with cs:
            if st.button("Save", key=f"wl_save_{ticker}"):
                watchlist_update(ticker,
                    alert_score     = int(e_score),
                    alert_pct       = float(e_pct),
                    notes           = e_notes,
                    price_target    = float(e_target) if e_target > 0 else None,
                    price_above     = float(e_above)  if e_above  > 0 else None,
                    price_below     = float(e_below)  if e_below  > 0 else None,
                    volume_spike_x  = float(e_vol_x)  if e_vol_x  > 0 else 0,
                    supertrend_alert= int(e_st),
                )
                st.session_state[editing_key] = False
                st.rerun()
        with cc:
            if st.button("Cancel", key=f"wl_cancel_{ticker}"):
                st.session_state[editing_key] = False
                st.rerun()
        return

    if r:
        score       = r["score"]
        price       = r["price"]
        score_color = "#16a34a" if score >= 60 else "#d97706" if score >= 45 else "#dc2626"
        rsi         = f"{r['rsi']:.0f}" if r.get("rsi") else "N/A"
        macd        = r.get("macd", "N/A")
        short_pct   = r.get("short_pct", 0)
        sig         = signal_label(score)
        sig_color   = "#16a34a" if score >= 60 else "#d97706" if score >= 45 else "#dc2626"
        sig_bg      = "#dcfce7" if score >= 60 else "#fef3c7" if score >= 45 else "#fee2e2"
        squeeze     = "🔥 " if r.get("squeeze_active") else ""

        target_html = ""
        if price_target and price_target > 0:
            pct_away  = (price_target - price) / price * 100
            t_color   = "#16a34a" if abs(pct_away) <= 1 else "#d97706" if abs(pct_away) <= 5 else "#64748b"
            direction = "▲" if price < price_target else "▼"
            target_html = f"<div style='margin-top:6px;font-size:12px;'>🎯 <b>${price_target:.2f}</b> <span style='color:{t_color};font-weight:600;'>{direction} {abs(pct_away):.1f}%</span></div>"

        alerts_parts = []
        if item.get("alert_score"): alerts_parts.append(f"Score≥{item['alert_score']}")
        if price_above:             alerts_parts.append(f"↑${price_above:.2f}")
        if price_below:             alerts_parts.append(f"↓${price_below:.2f}")
        alerts_str = " · ".join(alerts_parts)

        news_html = ""
        for a in news[:1]:
            h = _html_mod.escape(str(a.get("headline", ""))[:70])
            u = a.get("url", "")
            if not u.startswith(("http://", "https://")):
                u = "#"
            news_html = f'<div style="font-size:11px;margin-top:6px;"><a href="{_html_mod.escape(u)}" target="_blank" style="color:#1d4ed8;text-decoration:none;">{h}</a></div>'

        st.markdown(_html(f"""
            <div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;
                        padding:14px;box-shadow:0 1px 3px rgba(0,0,0,0.05);margin-bottom:4px;">
              <div style="display:flex;justify-content:space-between;align-items:flex-start;">
                <div>
                  <span style="font-size:16px;font-weight:800;color:#0f172a;">{squeeze}{ticker}</span>
                  <span style="font-size:10px;font-weight:700;background:{sig_bg};color:{sig_color};
                               padding:2px 6px;border-radius:4px;margin-left:6px;">{sig}</span>
                </div>
                <span style="font-size:22px;font-weight:800;color:{score_color};">{score:.0f}</span>
              </div>
              <div style="font-size:20px;font-weight:700;color:#1e293b;margin:6px 0 2px;">${price:.2f}</div>
              <div style="font-size:11px;color:#64748b;">RSI {rsi} · MACD {macd} · Short {short_pct:.1f}%</div>
              {target_html}
              {f'<div style="font-size:10px;color:#94a3b8;margin-top:4px;">{alerts_str}</div>' if alerts_str else ''}
              {news_html}
            </div>
        """), unsafe_allow_html=True)
    else:
        live_price = _fetch_price(ticker)
        price_str  = f"${live_price:.2f}" if live_price else "—"

        db_score, db_ts = _last_db_score(ticker)
        if db_score is not None:
            sig   = signal_label(db_score)
            score_html = f'<span style="font-size:11px;color:#64748b;">Score {db_score:.0f} · {sig} · {db_ts}</span>'
        else:
            score_html = '<span style="font-size:10px;color:#94a3b8;">not scanned</span>'

        target_html = ""
        if price_target and live_price and price_target > 0:
            pct_away  = (price_target - live_price) / live_price * 100
            t_color   = "#16a34a" if abs(pct_away) <= 1 else "#d97706" if abs(pct_away) <= 5 else "#64748b"
            direction = "▲" if live_price < price_target else "▼"
            target_html = f"<div style='margin-top:6px;font-size:12px;'>🎯 <b>${price_target:.2f}</b> <span style='color:{t_color};font-weight:600;'>{direction} {abs(pct_away):.1f}%</span></div>"

        st.markdown(_html(f"""
            <div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;
                        padding:14px;box-shadow:0 1px 3px rgba(0,0,0,0.05);margin-bottom:4px;">
              <div style="display:flex;justify-content:space-between;align-items:center;">
                <span style="font-size:16px;font-weight:800;color:#0f172a;">{ticker}</span>
                {score_html}
              </div>
              <div style="font-size:20px;font-weight:700;color:#1e293b;margin:6px 0 2px;">{price_str}</div>
              {target_html}
              {f'<div style="font-size:10px;color:#94a3b8;margin-top:4px;">{_html_mod.escape(str(item["notes"]))}</div>' if item.get("notes") else ''}
            </div>
        """), unsafe_allow_html=True)

    bc1, bc2 = st.columns(2)
    with bc1:
        if st.button("✏️ Edit", key=f"wl_edit_{ticker}", use_container_width=True):
            st.session_state[editing_key] = True
            st.rerun()
    with bc2:
        if st.button("🗑 Remove", key=f"wl_del_{ticker}", use_container_width=True):
            watchlist_remove(ticker)
            st.session_state.get("wl_results", {}).pop(ticker, None)
            st.rerun()


def _render_watch_actions(item, r, news):
    ticker      = item["ticker"]
    editing_key = f"wl_editing_{ticker}"
    is_editing  = st.session_state.get(editing_key, False)

    if is_editing:
        st.markdown(f"**Edit watchlist: {ticker}**")
        c1, c2, c3 = st.columns([1, 1, 2])
        with c1: e_score = st.number_input("Alert score",    0, 100, int(item.get("alert_score", 60)),           key=f"we_score_{ticker}")
        with c2: e_pct   = st.number_input("Alert % change", 0.0, 50.0, float(item.get("alert_pct", 5.0)), step=0.5, key=f"we_pct_{ticker}")
        with c3: e_notes = st.text_input("Notes", value=item.get("notes", ""), key=f"we_notes_{ticker}")
        c4, c5, c6 = st.columns(3)
        with c4: e_target = st.number_input("🎯 Price target $", 0.0, 99999.0, float(item.get("price_target") or 0), step=0.01, key=f"we_target_{ticker}")
        with c5: e_above  = st.number_input("Price above $",    0.0, 99999.0, float(item.get("price_above") or 0),  step=0.01, key=f"we_above_{ticker}")
        with c6: e_below  = st.number_input("Price below $",    0.0, 99999.0, float(item.get("price_below") or 0),  step=0.01, key=f"we_below_{ticker}")
        cs, cc = st.columns([1, 5])
        with cs:
            if st.button("Save", key=f"wl_save_{ticker}"):
                watchlist_update(
                    ticker,
                    alert_score  = int(e_score),
                    alert_pct    = float(e_pct),
                    notes        = e_notes,
                    price_target = float(e_target) if e_target > 0 else None,
                    price_above  = float(e_above)  if e_above  > 0 else None,
                    price_below  = float(e_below)  if e_below  > 0 else None,
                )
                st.session_state[editing_key] = False
                st.rerun()
        with cc:
            if st.button("Cancel", key=f"wl_cancel_{ticker}"):
                st.session_state[editing_key] = False
                st.rerun()
        st.markdown("---")
        return

    btn_col1, btn_col2, btn_spacer = st.columns([1, 1, 8])
    with btn_col1:
        if st.button("✏️ Edit", key=f"wl_edit_{ticker}"):
            st.session_state[editing_key] = True
            st.rerun()
    with btn_col2:
        if st.button("🗑 Remove", key=f"wl_del_{ticker}"):
            watchlist_remove(ticker)
            st.session_state.get("wl_results", {}).pop(ticker, None)
            st.rerun()


# ── Portfolio ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def _fetch_sector(ticker: str) -> str:
    try:
        import yfinance as yf
        return yf.Ticker(ticker).info.get("sector", "Unknown") or "Unknown"
    except Exception:
        return "Unknown"


def _build_portfolio_rows(holdings: list, results_map: dict) -> list[dict]:
    """Build enriched rows for each holding using scan results or live prices."""
    rows = []
    for h in holdings:
        ticker      = h["ticker"]
        entry_price = h.get("entry_price") or 0
        shares      = h.get("shares") or 0
        r           = results_map.get(ticker)

        if r:
            price = r.get("price") or 0
            score = r.get("score")
        else:
            price = _fetch_price(ticker) or 0
            score = None

        invested    = entry_price * shares
        curr_val    = price * shares
        pnl_val     = curr_val - invested
        pnl_pct     = (price - entry_price) / entry_price * 100 if entry_price else 0
        sector      = _fetch_sector(ticker)

        rows.append({
            "ticker":       ticker,
            "price":        price,
            "entry_price":  entry_price,
            "shares":       shares,
            "invested":     invested,
            "curr_val":     curr_val,
            "pnl_val":      pnl_val,
            "pnl_pct":      pnl_pct,
            "score":        score,
            "sector":       sector,
            "stop_loss":    h.get("stop_loss"),
            "target_price": h.get("target_price"),
            "notes":        h.get("notes", ""),
        })
    return rows


def _render_portfolio_kpis(rows: list[dict], label_suffix: str = ""):
    total_invested = sum(r["invested"] for r in rows)
    total_value    = sum(r["curr_val"] for r in rows)
    total_pnl      = total_value - total_invested
    total_ret_pct  = (total_pnl / total_invested * 100) if total_invested else 0
    winners        = sum(1 for r in rows if r["pnl_pct"] > 0)
    losers         = sum(1 for r in rows if r["pnl_pct"] < 0)

    pnl_color = "#4ade80" if total_pnl >= 0 else "#f87171"
    ret_color = "#4ade80" if total_ret_pct >= 0 else "#f87171"

    metrics = [
        ("Invested Capital",  f"${total_invested:,.0f}",              "#93c5fd"),
        ("Portfolio Value",   f"${total_value:,.0f}",                 "#93c5fd"),
        (f"Total P&amp;L{label_suffix}", f"${total_pnl:+,.0f}",      pnl_color),
        ("Return %",          f"{total_ret_pct:+.1f}%",               ret_color),
        ("Winners",           str(winners),                            "#4ade80"),
        ("Losers",            str(losers),                             "#f87171"),
    ]

    cols = st.columns(6)
    for col, (label, value, color) in zip(cols, metrics):
        with col:
            st.markdown(_html(f"""
                <div style="background:linear-gradient(135deg,#1e3a8a,#1d4ed8);
                            border-radius:10px;padding:12px 14px;text-align:center;">
                  <div style="font-size:10px;color:#93c5fd;margin-bottom:4px;">{label}</div>
                  <div style="font-size:18px;font-weight:700;color:{color};">{value}</div>
                </div>
            """), unsafe_allow_html=True)
    st.markdown("<div style='margin-bottom:12px;'></div>", unsafe_allow_html=True)


def _render_portfolio_table(rows: list[dict]):
    """Sortable P&L breakdown table."""
    if not rows:
        return

    df_data = []
    for r in rows:
        score_str = f"{r['score']:.0f}" if r["score"] is not None else "—"
        df_data.append({
            "Ticker":    r["ticker"],
            "Price":     r["price"],
            "Entry":     r["entry_price"],
            "Shares":    r["shares"],
            "Invested":  r["invested"],
            "Value":     r["curr_val"],
            "P&L $":     r["pnl_val"],
            "P&L %":     r["pnl_pct"],
            "Score":     r["score"] if r["score"] is not None else float("nan"),
            "Sector":    r["sector"],
            "Stop":      r["stop_loss"] or "",
            "Target":    r["target_price"] or "",
        })

    df = pd.DataFrame(df_data)

    styled = df.style \
        .format({
            "Price":    "${:.2f}",
            "Entry":    "${:.2f}",
            "Shares":   "{:.0f}",
            "Invested": "${:,.0f}",
            "Value":    "${:,.0f}",
            "P&L $":    "${:+,.0f}",
            "P&L %":    "{:+.1f}%",
            "Score":    lambda v: f"{v:.0f}" if pd.notna(v) else "—",
            "Stop":     lambda v: f"${v:.2f}" if v != "" else "—",
            "Target":   lambda v: f"${v:.2f}" if v != "" else "—",
        }) \
        .applymap(
            lambda v: "color: #16a34a" if isinstance(v, float) and v > 0
                      else ("color: #dc2626" if isinstance(v, float) and v < 0 else ""),
            subset=["P&L $", "P&L %"]
        )

    st.dataframe(styled, use_container_width=True, hide_index=True)


def _render_sector_chart(rows: list[dict]):
    """Sector allocation bar chart by current value."""
    if not rows:
        return

    sector_vals: dict[str, float] = {}
    for r in rows:
        s = r["sector"]
        sector_vals[s] = sector_vals.get(s, 0) + r["curr_val"]

    if not sector_vals:
        return

    total = sum(sector_vals.values()) or 1
    df = pd.DataFrame([
        {"Sector": s, "Value ($)": v, "Weight (%)": round(v / total * 100, 1)}
        for s, v in sorted(sector_vals.items(), key=lambda x: -x[1])
    ])

    st.markdown("**Sector Allocation**")
    st.bar_chart(df.set_index("Sector")["Weight (%)"])

    # Also show table
    st.dataframe(
        df.style.format({"Value ($)": "${:,.0f}", "Weight (%)": "{:.1f}%"}),
        use_container_width=True,
        hide_index=True,
    )


def _render_portfolio():
    with st.expander("Add Position", expanded=not portfolio_get_all()):
        c1, c2, c3, c4, c5 = st.columns([2, 1, 1, 1, 1])
        with c1: pt_ticker = st.text_input("Ticker", placeholder="NVDA", key="pt_ticker").upper().strip()
        with c2: pt_entry  = st.number_input("Entry price $", 0.01, 99999.0, 1.0, step=0.01, key="pt_entry")
        with c3: pt_shares = st.number_input("Shares", 0.0, 1e6, 0.0, step=1.0, key="pt_shares")
        with c4: pt_stop   = st.number_input("Stop loss $", 0.0, 99999.0, 0.0, step=0.01, key="pt_stop")
        with c5: pt_target = st.number_input("Target price $", 0.0, 99999.0, 0.0, step=0.01, key="pt_target")
        pt_notes = st.text_input("Notes", placeholder="Optional", key="pt_notes")
        if st.button("Add Position", key="pt_add") and pt_ticker:
            portfolio_add(
                pt_ticker,
                entry_price  = float(pt_entry),
                shares       = float(pt_shares),
                notes        = pt_notes,
                stop_loss    = float(pt_stop)   if pt_stop   > 0 else None,
                target_price = float(pt_target) if pt_target > 0 else None,
            )
            st.success(f"{pt_ticker} added to portfolio")
            st.rerun()

    holdings = portfolio_get_all()
    if not holdings:
        st.info("Portfolio is empty.")
        return

    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("Scan Portfolio", key="pt_scan"):
            with st.spinner("Scanning portfolio..."):
                from src.watchlist_manager import scan_portfolio
                results = scan_portfolio()
                st.session_state["pt_results"] = {r["ticker"]: r for r in results}
                st.session_state["pt_ts"]      = datetime.now().strftime("%H:%M:%S")
            st.rerun()
    with c2:
        if st.session_state.get("pt_ts"):
            st.caption(f"Last scan: {st.session_state['pt_ts']} · stop loss / target / score-drop alerts sent to Telegram")

    results_map = st.session_state.get("pt_results", {})
    rows        = _build_portfolio_rows(holdings, results_map)
    label       = "" if results_map else " (live)"
    _render_portfolio_kpis(rows, label_suffix=label)

    tab_table, tab_sectors, tab_cards = st.tabs(["P&L Table", "Sector Allocation", "Position Cards"])

    with tab_table:
        _render_portfolio_table(rows)

    with tab_sectors:
        _render_sector_chart(rows)

    with tab_cards:
        for item in holdings:
            _render_portfolio_card(item, results_map.get(item["ticker"]))


def _render_portfolio_card(item, r):
    ticker       = item["ticker"]
    entry_price  = item["entry_price"]
    shares       = item.get("shares", 0)
    stop_loss    = item.get("stop_loss")
    target_price = item.get("target_price")
    editing_key  = f"pt_editing_{ticker}"
    is_editing   = st.session_state.get(editing_key, False)

    if is_editing:
        st.markdown(f"**Edit position: {ticker}**")
        c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 1, 1])
        with c1: e_entry  = st.number_input("Entry $",  0.01, 99999.0, float(entry_price),      step=0.01, key=f"e_entry_{ticker}")
        with c2: e_shares = st.number_input("Shares",   0.0,  1e6,     float(shares),            step=1.0,  key=f"e_shares_{ticker}")
        with c3: e_stop   = st.number_input("Stop $",   0.0,  99999.0, float(stop_loss or 0),    step=0.01, key=f"e_stop_{ticker}")
        with c4: e_target = st.number_input("Target $", 0.0,  99999.0, float(target_price or 0), step=0.01, key=f"e_target_{ticker}")
        with c5: e_notes  = st.text_input("Notes", value=item.get("notes", ""),                  key=f"e_notes_{ticker}")
        cs, cc = st.columns([1, 5])
        with cs:
            if st.button("Save", key=f"pt_save_{ticker}"):
                portfolio_update(
                    ticker,
                    entry_price  = float(e_entry),
                    shares       = float(e_shares),
                    stop_loss    = float(e_stop)   if e_stop   > 0 else None,
                    target_price = float(e_target) if e_target > 0 else None,
                    notes        = e_notes,
                )
                st.session_state[editing_key] = False
                st.session_state.get("pt_results", {}).pop(ticker, None)
                st.rerun()
        with cc:
            if st.button("Cancel", key=f"pt_cancel_{ticker}"):
                st.session_state[editing_key] = False
                st.rerun()
        st.markdown("---")
        return

    shares_html = f"<span style='font-size:13px;color:#6b7280;'>{int(shares)} shares</span>" if shares else ""
    stop_html   = f"<span style='font-size:12px;color:#dc2626;'>Stop ${stop_loss:.2f}</span>"      if stop_loss   else ""
    target_html = f"<span style='font-size:12px;color:#16a34a;'>Target ${target_price:.2f}</span>" if target_price else ""

    if r:
        # ── לאחר סריקה ───────────────────────────────────────────────────────
        price        = r["price"]
        score        = r["score"]
        pnl_pct      = r.get("pnl_pct", 0)
        pnl_val      = r.get("pnl_val")
        pnl_color    = "#16a34a" if pnl_pct >= 0 else "#dc2626"
        score_color  = "#16a34a" if score >= 60 else "#d97706" if score >= 45 else "#dc2626"
        badge_html   = badge(signal_label(score))
        pnl_val_html = f" (${pnl_val:+,.0f})" if pnl_val else ""
        notes_html   = f"<div style='font-size:11px;color:#94a3b8;margin-top:4px;'>{_html_mod.escape(str(item['notes']))}</div>" if item.get("notes") else ""

        bar_html = ""
        if stop_loss and target_price and target_price > stop_loss:
            pct = min(max((price - stop_loss) / (target_price - stop_loss) * 100, 2), 98)
            bar_html = (
                f"<div style='margin:8px 0 4px;'>"
                f"<div style='background:#e2e8f0;border-radius:4px;height:6px;'>"
                f"<div style='background:#1d4ed8;height:6px;border-radius:4px;width:{pct:.0f}%;'></div>"
                f"</div>"
                f"<div style='display:flex;justify-content:space-between;font-size:10px;color:#94a3b8;margin-top:2px;'>"
                f"<span>Stop ${stop_loss:.2f}</span><span>Now ${price:.2f}</span><span>Target ${target_price:.2f}</span>"
                f"</div></div>"
            )

        html = _html(f"""
            <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px 16px;margin-bottom:4px;">
              <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px;">
                <span style="font-size:18px;font-weight:700;color:#0f172a;">{ticker}</span>
                <span style="font-size:15px;font-weight:600;color:#374151;">${price:.2f}</span>
                <span style="font-size:15px;font-weight:700;color:{pnl_color};">{pnl_pct:+.1f}%{pnl_val_html}</span>
                <span style="font-size:13px;color:#6b7280;">Entry ${entry_price:.2f}</span>
                {shares_html}
                <span style="font-size:16px;font-weight:700;color:{score_color};">{score:.0f}</span>
                {badge_html}
              </div>
              <div style="display:flex;gap:12px;">{stop_html}{target_html}</div>
              {bar_html}
              {notes_html}
            </div>
        """)
    else:
        # ── לפני סריקה — live price + P&L ────────────────────────────────────
        live_price = _fetch_price(ticker)

        if live_price and entry_price:
            pnl_pct   = (live_price - entry_price) / entry_price * 100
            pnl_val   = (live_price - entry_price) * shares if shares else None
            pnl_color = "#16a34a" if pnl_pct >= 0 else "#dc2626"
            pnl_val_html = f" (${pnl_val:+,.0f})" if pnl_val else ""
            price_section = (
                f"<span style='font-size:15px;font-weight:600;color:#374151;'>${live_price:.2f}</span>"
                f"<span style='font-size:15px;font-weight:700;color:{pnl_color};'>{pnl_pct:+.1f}%{pnl_val_html}</span>"
                f"<span style='font-size:13px;color:#6b7280;'>Entry ${entry_price:.2f}</span>"
            )
        else:
            price_section = (
                f"<span style='font-size:13px;color:#374151;'>Entry ${entry_price:.2f}</span>"
                f"<span style='font-size:13px;color:#94a3b8;'>price unavailable</span>"
            )

        bar_html = ""
        if stop_loss and target_price and target_price > stop_loss and live_price:
            pct = min(max((live_price - stop_loss) / (target_price - stop_loss) * 100, 2), 98)
            bar_html = (
                f"<div style='margin:8px 0 4px;'>"
                f"<div style='background:#e2e8f0;border-radius:4px;height:6px;'>"
                f"<div style='background:#1d4ed8;height:6px;border-radius:4px;width:{pct:.0f}%;'></div>"
                f"</div>"
                f"<div style='display:flex;justify-content:space-between;font-size:10px;color:#94a3b8;margin-top:2px;'>"
                f"<span>Stop ${stop_loss:.2f}</span><span>Now ${live_price:.2f}</span><span>Target ${target_price:.2f}</span>"
                f"</div></div>"
            )

        notes_html = f"<div style='font-size:11px;color:#94a3b8;margin-top:4px;'>{_html_mod.escape(str(item['notes']))}</div>" if item.get("notes") else ""

        html = _html(f"""
            <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px 16px;margin-bottom:4px;">
              <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px;">
                <span style="font-size:18px;font-weight:700;color:#0f172a;">{ticker}</span>
                {price_section}
                {shares_html}
                <span style="font-size:11px;color:#94a3b8;margin-left:auto;">not yet scanned</span>
              </div>
              <div style="display:flex;gap:12px;">{stop_html}{target_html}</div>
              {bar_html}
              {notes_html}
            </div>
        """)

    st.markdown(html, unsafe_allow_html=True)

    btn_col1, btn_col2, btn_spacer = st.columns([1, 1, 8])
    with btn_col1:
        if st.button("✏️ Edit", key=f"pt_edit_{ticker}"):
            st.session_state[editing_key] = True
            st.rerun()
    with btn_col2:
        st.markdown('<div class="btn-danger">', unsafe_allow_html=True)
        if st.button("🗑 Remove", key=f"pt_del_{ticker}"):
            portfolio_remove(ticker)
            st.session_state.get("pt_results", {}).pop(ticker, None)
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("<div style='margin-bottom:12px;'></div>", unsafe_allow_html=True)


# ── Alert History ──────────────────────────────────────────────────────────────

def _render_alerts():
    alerts = watchlist_get_alerts(limit=50)
    if not alerts:
        st.info("No alerts yet.")
        return

    type_colors = {
        "score_threshold": "#1d4ed8",
        "price_change":    "#d97706",
        "price_target":    "#7c3aed",
        "price_above":     "#16a34a",
        "price_below":     "#dc2626",
        "stop_loss":       "#dc2626",
        "target_hit":      "#16a34a",
        "score_drop":      "#f97316",
        "volume_spike":    "#0ea5e9",
        "supertrend_flip":          "#8b5cf6",
        "supertrend_intraday_flip": "#a855f7",
        "score_delta_drop":         "#f97316",
        "score_delta_rise":         "#10b981",
    }
    type_labels = {
        "score_threshold":          "Score",
        "price_change":             "% Change",
        "price_target":             "🎯 Price Target",
        "price_above":              "Above Target",
        "price_below":              "Below Target",
        "stop_loss":                "Stop Loss",
        "target_hit":               "Target Hit",
        "score_drop":               "Score Drop",
        "volume_spike":             "📊 Volume Spike",
        "supertrend_flip":          "📈 Supertrend Daily",
        "supertrend_intraday_flip": "📈 Supertrend 15m",
        "score_delta_drop":         "⚠️ Score Drop",
        "score_delta_rise":         "🚀 Score Surge",
    }

    for a in alerts:
        color  = type_colors.get(a["alert_type"], "#6b7280")
        label  = _html_mod.escape(type_labels.get(a["alert_type"], a["alert_type"]))
        ts     = a["sent_at"][:16].replace("T", " ")
        lines  = a["message"].split("\n")
        detail = _html_mod.escape(lines[1] if len(lines) > 1 else lines[0])
        t_ticker = _html_mod.escape(str(a["ticker"]))
        st.markdown(_html(f"""
            <div style="border-left:4px solid {color};padding:8px 14px;margin-bottom:6px;
                        background:#f8fafc;border-radius:0 8px 8px 0;">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px;">
                <div style="display:flex;gap:8px;align-items:center;">
                  <span style="font-weight:700;font-size:14px;">{t_ticker}</span>
                  <span style="font-size:11px;background:{color};color:white;padding:1px 8px;border-radius:4px;">{label}</span>
                </div>
                <span style="font-size:11px;color:#94a3b8;">{ts}</span>
              </div>
              <div style="font-size:13px;color:#374151;">{detail}</div>
            </div>
        """), unsafe_allow_html=True)
