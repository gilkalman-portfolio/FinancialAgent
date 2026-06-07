"""Page: Research"""
import streamlit as st
import pandas as pd
import os
import requests
import html as _html_mod
from datetime import datetime, timedelta
from src.ui_theme import badge, tooltip
from src.stock_scorer import score_stock, signal_label


def render():
    st.markdown("### Stock Research")
    mode = st.radio("Mode", ["Deep Dive", "Compare Side-by-Side"], horizontal=True, label_visibility="collapsed")
    if mode == "Compare Side-by-Side":
        _render_compare()
    else:
        _render_deep_dive()


# ── Compare ────────────────────────────────────────────────────────────────────

def _render_compare():
    st.markdown("#### Compare Stocks")
    tickers_input = st.text_input("Tickers (2–4, comma separated)", "PLTR, IONQ, NVDA", key="compare_input")
    run_compare   = st.button("Compare", key="compare_btn")

    if run_compare and tickers_input.strip():
        tickers = [t.strip().upper() for t in tickers_input.split(",") if t.strip()][:4]
        if len(tickers) < 2:
            st.warning("Enter at least 2 tickers.")
            return

        import yfinance as _yf
        with st.spinner("Fetching data..."):
            stocks_data = {}
            for ticker in tickers:
                stock       = _yf.Ticker(ticker)
                info        = stock.info
                r           = score_stock(ticker, 30)
                api_key     = os.getenv("FINNHUB_API_KEY", "")
                analyst_rec, apt = {}, {}
                try:
                    resp = requests.get(f"https://finnhub.io/api/v1/stock/recommendation?symbol={ticker}&token={api_key}", timeout=5)
                    if resp.status_code == 200 and resp.json():
                        analyst_rec = resp.json()[0]
                except Exception:
                    pass
                try:
                    apt = stock.analyst_price_targets or {}
                except Exception:
                    pass
                hist = stock.history(period="3mo")
                stocks_data[ticker] = {"info": info, "r": r, "analyst_rec": analyst_rec, "apt": apt, "hist": hist}

        st.session_state["compare_data"]    = stocks_data
        st.session_state["compare_tickers"] = tickers

    stocks_data = st.session_state.get("compare_data")
    tickers     = st.session_state.get("compare_tickers")
    if not stocks_data or not tickers:
        return

    _render_compare_chart(tickers, stocks_data)
    _render_compare_table(tickers, stocks_data)


def _render_compare_chart(tickers, stocks_data):
    st.markdown("**Price Performance (normalized, 3 months)**")
    try:
        import plotly.graph_objects as go
        fig    = go.Figure()
        COLORS = ["#1d4ed8", "#16a34a", "#dc2626", "#d97706"]
        for i, ticker in enumerate(tickers):
            hist = stocks_data[ticker]["hist"]
            if not hist.empty:
                normalized = hist["Close"] / hist["Close"].iloc[0] * 100
                fig.add_trace(go.Scatter(x=hist.index, y=normalized, mode="lines",
                    name=ticker, line=dict(color=COLORS[i % len(COLORS)], width=2)))
        fig.update_layout(height=280, margin=dict(l=0, r=0, t=10, b=0),
            plot_bgcolor="white", paper_bgcolor="white",
            xaxis=dict(showgrid=False, color="#94a3b8"),
            yaxis=dict(showgrid=True, gridcolor="#f1f5f9", color="#94a3b8"),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
            hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.caption(f"Chart unavailable: {e}")


def _render_compare_table(tickers, stocks_data):
    st.markdown("**Head-to-Head Comparison**")

    def _fmt(val, fmt="{:.1f}", fallback="N/A"):
        try:
            return fmt.format(val) if val is not None else fallback
        except:
            return fallback

    def _fmt_mcap(v):
        if not v: return "N/A"
        if v >= 1e12: return f"${v/1e12:.1f}T"
        if v >= 1e9:  return f"${v/1e9:.1f}B"
        if v >= 1e6:  return f"${v/1e6:.1f}M"
        return f"${v:,.0f}"

    def _fmt_upside(info, apt):
        try:
            price  = info.get("currentPrice") or info.get("regularMarketPrice")
            target = apt.get("mean")
            if price and target:
                return f"{(target - price) / price * 100:+.1f}%"
        except:
            pass
        return "N/A"

    def _fmt_dcf(r):
        dcf = (r or {}).get("dcf")
        if not dcf:
            return "N/A"
        mos = dcf.get("margin_of_safety", 0)
        iv  = dcf.get("intrinsic_value", 0)
        return f"${iv:.2f} ({mos:+.0f}%)"

    METRICS = [
        ("Score",           lambda d: _fmt(d["r"]["score"] if d["r"] else None)),
        ("Signal",          lambda d: signal_label(d["r"]["score"]) if d["r"] else "N/A"),
        ("Price",           lambda d: _fmt(d["info"].get("currentPrice") or d["info"].get("regularMarketPrice"), "${:.2f}")),
        ("RSI",             lambda d: _fmt(d["r"]["rsi"] if d["r"] else None, "{:.0f}")),
        ("MACD",            lambda d: (d["r"]["macd"] or "N/A").capitalize() if d["r"] else "N/A"),
        ("MA Trend",        lambda d: (d["r"]["ma_trend"] or "N/A").replace("_", " ").title() if d["r"] else "N/A"),
        ("SI% of Float",    lambda d: _fmt(d["r"]["short_pct"] if d["r"] else None, "{:.1f}%")),
        ("Momentum 5D",     lambda d: _fmt(d["r"]["momentum"] if d["r"] else None, "{:+.1f}%")),
        ("Inst. Holdings",  lambda d: _fmt(d["r"]["inst_pct"] if d["r"] else None, "{:.0f}%")),
        ("P/E",             lambda d: _fmt(d["r"].get("fund_pe") if d["r"] else None, "{:.1f}x")),
        ("Revenue Growth",  lambda d: _fmt((d["r"].get("fund_rev_growth") or 0) * 100 if d["r"] else None, "{:.0f}%")),
        ("Profit Margin",   lambda d: _fmt((d["r"].get("fund_margin") or 0) * 100 if d["r"] else None, "{:.0f}%")),
        ("D/E Ratio",       lambda d: _fmt(d["r"].get("fund_de") if d["r"] else None, "{:.2f}")),
        ("DCF / MoS",       lambda d: _fmt_dcf(d["r"])),
        ("Market Cap",      lambda d: _fmt_mcap(d["info"].get("marketCap"))),
        ("Analyst Target",  lambda d: _fmt(d["apt"].get("mean"), "${:.2f}")),
        ("Upside %",        lambda d: _fmt_upside(d["info"], d["apt"])),
        ("Analyst SB/B/H",  lambda d: f"{d['analyst_rec'].get('strongBuy',0)}/{d['analyst_rec'].get('buy',0)}/{d['analyst_rec'].get('hold',0)}" if d["analyst_rec"] else "N/A"),
    ]

    table_rows = []
    for label, fn in METRICS:
        row = {"Metric": label}
        for ticker in tickers:
            row[ticker] = fn(stocks_data[ticker])
        table_rows.append(row)

    df_compare = pd.DataFrame(table_rows).set_index("Metric")

    header_colors = []
    for ticker in tickers:
        r = stocks_data[ticker]["r"]
        if r:
            s = r["score"]
            if s >= 75:   header_colors.append("#166534")
            elif s >= 60: header_colors.append("#1d4ed8")
            elif s >= 45: header_colors.append("#92400e")
            else:         header_colors.append("#991b1b")
        else:
            header_colors.append("#374151")

    header_cells = "<th style='padding:8px 12px;text-align:left;font-size:11px;color:#6b7280;background:#f8fafc;'>Metric</th>"
    for ticker, color in zip(tickers, header_colors):
        r = stocks_data[ticker]["r"]
        score_txt = f" · {r['score']:.0f}" if r else ""
        header_cells += f"<th style='padding:8px 12px;font-size:13px;font-weight:700;color:{color};background:#f8fafc;text-align:center;'>{ticker}{score_txt}</th>"

    HIGHLIGHT_ROWS = {"Score", "Signal", "Upside %", "Momentum 5D", "DCF / MoS"}
    body_rows_html = ""
    for i, row in df_compare.iterrows():
        bg   = "#fff" if list(df_compare.index).index(i) % 2 == 0 else "#f8fafc"
        bold = "font-weight:600;" if i in HIGHLIGHT_ROWS else ""
        _METRIC_TIPS = {
            "DCF / MoS":    "DCF Intrinsic Value + Margin of Safety. MoS = (Intrinsic − Price) ÷ Intrinsic × 100. ≥20% = undervalued.",
            "RSI":          "Relative Strength Index (0-100). Below 30 = oversold, above 70 = overbought. Sweet spot: 40-65.",
            "MACD":         "Moving Average Convergence Divergence. Bullish = MACD line above signal line.",
            "MA Trend":     "Moving average trend. Strong uptrend = price above SMA20/50/200.",
            "SI% of Float": "Short Interest as % of float. ≥15% = elevated, ≥20% = squeeze zone, ≥50% = extreme.",
            "Score":        "Composite score 0-100: RSI + MACD + MA Trend + Volume + Momentum + SI + Institutional + Insider + Fundamentals.",
            "Signal":       "Trading signal: ≥75 = STRONG BUY · 60-74 = BUY · 45-59 = WATCH · 35-44 = NEUTRAL · <35 = SKIP.",
        }
        _tip = _METRIC_TIPS.get(i, "")
        _label_html = (f"<span title='{_tip}' style='cursor:help;border-bottom:1px dotted #94a3b8;'>{i}</span>"
                       if _tip else i)
        cells = f"<td style='padding:7px 12px;font-size:12px;color:#374151;{bold}border-top:1px solid #e2e8f0;'>{_label_html}</td>"
        for ticker in tickers:
            val   = row[ticker]
            color = "#374151"
            if i == "Signal":
                color = {"STRONG BUY": "#166534", "BUY": "#1d4ed8", "WATCH": "#92400e", "NEUTRAL": "#6b7280", "SKIP": "#991b1b"}.get(val, "#374151")
            elif i in ("Momentum 5D", "Upside %", "Revenue Growth") and val not in ("N/A", ""):
                try:
                    color = "#16a34a" if float(val.replace("%", "").replace("+", "")) > 0 else "#dc2626"
                except:
                    pass
            elif i == "DCF / MoS" and val != "N/A":
                try:
                    mos_val = float(val.split("(")[1].replace("%)", "").replace("+", ""))
                    color = "#16a34a" if mos_val >= 20 else "#d97706" if mos_val >= 0 else "#dc2626"
                except:
                    pass
            cells += f"<td style='padding:7px 12px;font-size:12px;color:{color};text-align:center;{bold}border-top:1px solid #e2e8f0;'>{val}</td>"
        body_rows_html += f"<tr style='background:{bg};'>{cells}</tr>"

    st.markdown(f"""<div style="border:3px solid #000;border-radius:12px;overflow:hidden;margin-bottom:16px;">
    <table style="width:100%;border-collapse:collapse;">
      <thead><tr>{header_cells}</tr></thead>
      <tbody>{body_rows_html}</tbody>
    </table></div>""", unsafe_allow_html=True)


# ── Deep Dive ──────────────────────────────────────────────────────────────────

def _build_score_str(r) -> str:
    if not r:
        return ""
    rsi_str    = f"{r['rsi']:.0f}" if r.get('rsi') else "N/A"
    fc_str     = f"{r['forecast_change']:+.1f}%" if r.get('forecast_change') else "N/A"
    pe_str     = f"{r.get('fund_pe'):.1f}" if r.get('fund_pe') else "N/A"
    rev_str    = f"{r.get('fund_rev_growth', 0)*100:.0f}%" if r.get('fund_rev_growth') is not None else "N/A"
    margin_str = f"{r.get('fund_margin', 0)*100:.0f}%" if r.get('fund_margin') is not None else "N/A"
    de_str     = f"{r.get('fund_de'):.2f}" if r.get('fund_de') is not None else "N/A"
    dcf        = r.get('dcf') or {}
    dcf_str    = f"DCF intrinsic ${dcf.get('intrinsic_value',0):.2f} (MoS {dcf.get('margin_of_safety',0):+.0f}%)." if dcf else ""
    return (f"Score: {r['score']:.1f}/100 ({signal_label(r['score'])}). "
            f"RSI: {rsi_str}. MACD: {r['macd']}. MA: {r['ma_trend']}. "
            f"Forecast: {fc_str}. "
            f"Short Interest (% of Float): {r['short_pct']:.1f}% ({r['days_to_cover']:.1f} days to cover). "
            f"Inst: {r['inst_pct']:.0f}%. "
            f"P/E: {pe_str}. Revenue Growth: {rev_str}. Profit Margin: {margin_str}. D/E: {de_str}. {dcf_str}")


def _render_deep_dive():
    tickers_input = st.text_input("Tickers (comma separated)", "PLTR, GME, IONQ", key="deep_dive_input")
    st.markdown('<div class="btn-primary">', unsafe_allow_html=True)
    run_research  = st.button("Analyze", key="deep_dive_run")
    st.markdown('</div>', unsafe_allow_html=True)

    if run_research and tickers_input.strip():
        tickers = [t.strip().upper() for t in tickers_input.split(",") if t.strip()]
        st.session_state["deep_dive_tickers"] = tickers
        for t in tickers:
            st.session_state.pop(f"deep_dive_data_{t}", None)
            st.session_state.pop(f"debate_{t}", None)
            st.session_state.pop(f"ai_summary_{t}", None)

    tickers = st.session_state.get("deep_dive_tickers")
    if not tickers:
        return

    for ticker in tickers:
        st.markdown("---")
        st.markdown(f"#### {ticker}")

        data_key = f"deep_dive_data_{ticker}"
        if data_key not in st.session_state:
            with st.spinner(f"Analyzing {ticker}..."):
                import yfinance as _yf
                stock    = _yf.Ticker(ticker)
                info     = stock.info
                price    = info.get("currentPrice") or info.get("regularMarketPrice", 0)
                name     = info.get("longName", ticker)
                r        = score_stock(ticker, 30)
                api_key  = os.getenv("FINNHUB_API_KEY", "")
                today    = datetime.now().strftime("%Y-%m-%d")
                week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                news, analyst_rec, insiders = [], {}, []
                try:
                    resp = requests.get(f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={week_ago}&to={today}&token={api_key}", timeout=5)
                    if resp.status_code == 200:
                        news = resp.json()[:8]
                except Exception:
                    pass
                try:
                    resp = requests.get(f"https://finnhub.io/api/v1/stock/recommendation?symbol={ticker}&token={api_key}", timeout=5)
                    if resp.status_code == 200 and resp.json():
                        analyst_rec = resp.json()[0]
                except Exception:
                    pass
                try:
                    resp = requests.get(f"https://finnhub.io/api/v1/stock/insider-transactions?symbol={ticker}&token={api_key}", timeout=5)
                    if resp.status_code == 200:
                        all_ins  = resp.json().get("data", [])
                        priority = {"P": 0, "S": 1, "M": 2, "A": 3, "F": 4, "G": 5, "D": 6}
                        all_ins.sort(key=lambda x: priority.get(x.get("transactionCode", ""), 9))
                        insiders = all_ins[:6]
                except Exception:
                    pass
                from src.google_trends import trends_score as _trends_score
                trends_data = _trends_score(ticker)
                hist        = stock.history(period="6mo")

                st.session_state[data_key] = {
                    "info": info, "price": price, "name": name, "r": r,
                    "news": news, "analyst_rec": analyst_rec, "insiders": insiders,
                    "trends_data": trends_data, "hist": hist,
                    "inst":  stock.institutional_holders,
                    "funds": stock.mutualfund_holders,
                    "apt":   stock.analyst_price_targets,
                }

        d = st.session_state[data_key]
        _render_ticker_card(ticker, d)


def _render_ticker_card(ticker: str, d: dict):
    info        = d["info"]
    price       = d["price"]
    name        = d["name"]
    r           = d["r"]
    news        = d["news"]
    analyst_rec = d["analyst_rec"]
    insiders    = d["insiders"]
    trends_data = d["trends_data"]
    hist        = d["hist"]
    inst        = d["inst"]
    funds       = d["funds"]
    apt         = d["apt"]

    # Chart
    try:
        if not hist.empty:
            import plotly.graph_objects as go
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=hist.index, y=hist['Close'], mode='lines',
                line=dict(color='#1d4ed8', width=2), fill='tozeroy', fillcolor='rgba(29,78,216,0.07)'))
            fig.update_layout(height=280, margin=dict(l=0, r=0, t=10, b=0),
                plot_bgcolor='white', paper_bgcolor='white',
                xaxis=dict(showgrid=False, color='#94a3b8'),
                yaxis=dict(showgrid=True, gridcolor='#f1f5f9', color='#94a3b8', tickprefix='$'),
                showlegend=False, hovermode='x unified')
            st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.caption(f"Chart unavailable: {e}")

    # Metric cards
    score_val  = f"{r['score']:.1f}" if r else "N/A"
    rsi_val    = f"{r['rsi']:.0f}" if r and r.get('rsi') else "N/A"
    short_val  = f"{r['short_pct']:.1f}%{' 🔥 SQUEEZE' if r.get('squeeze_active') else ''}" if r and r.get('short_pct') else "N/A"
    trends_val = "N/A" if not trends_data or trends_data.get('interest', 0) == 0 else f"{trends_data['interest']}/100"
    st.markdown(f"""<div style="display:flex;gap:8px;margin-bottom:1rem;">
      <div style="flex:1;background:#1e3a8a;border-radius:8px;padding:12px 14px;"><div style="font-size:12px;color:#93c5fd;margin-bottom:4px;">Price</div><div style="font-size:22px;font-weight:700;color:#fff;">${price:.2f}</div></div>
      <div style="flex:1;background:#1e3a8a;border-radius:8px;padding:12px 14px;"><div style="font-size:12px;color:#93c5fd;margin-bottom:4px;">Score</div><div style="font-size:22px;font-weight:700;color:#fff;">{score_val}</div></div>
      <div style="flex:1;background:#1e3a8a;border-radius:8px;padding:12px 14px;"><div style="font-size:12px;color:#93c5fd;margin-bottom:4px;">RSI</div><div style="font-size:22px;font-weight:700;color:#fff;">{rsi_val}</div></div>
      <div style="flex:1;background:#1e3a8a;border-radius:8px;padding:12px 14px;"><div style="font-size:12px;color:#93c5fd;margin-bottom:4px;">SI% of Float</div><div style="font-size:22px;font-weight:700;color:#fff;">{short_val}</div></div>
      <div style="flex:1;background:#1e3a8a;border-radius:8px;padding:12px 14px;"><div style="font-size:12px;color:#93c5fd;margin-bottom:4px;">Trends</div><div style="font-size:22px;font-weight:700;color:#fff;">{trends_val}</div></div>
    </div>""", unsafe_allow_html=True)

    # Analyst consensus
    if analyst_rec:
        st.markdown("**Analyst consensus**")
        a1, a2, a3, a4, a5 = st.columns(5)
        a1.metric("Strong Buy",  analyst_rec.get("strongBuy", 0))
        a2.metric("Buy",         analyst_rec.get("buy", 0))
        a3.metric("Hold",        analyst_rec.get("hold", 0))
        a4.metric("Sell",        analyst_rec.get("sell", 0))
        a5.metric("Strong Sell", analyst_rec.get("strongSell", 0))

    # Analyst Price Targets
    try:
        mean_target = apt.get('mean') if apt else None
        high_target = apt.get('high') if apt else None
        low_target  = apt.get('low')  if apt else None
        n_analysts  = info.get('numberOfAnalystOpinions', 0)
        if mean_target and price:
            upside       = (mean_target - price) / price * 100
            upside_color = "#16a34a" if upside > 0 else "#dc2626"
            bar_pct      = min(max((price - low_target) / (high_target - low_target) * 100, 2), 98) if high_target and low_target and high_target != low_target else 50
            st.markdown(f"""<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:14px 18px;margin:8px 0;">
              <div style="font-size:12px;font-weight:600;color:#64748b;margin-bottom:10px;">ANALYST PRICE TARGETS — {n_analysts} analysts</div>
              <div style="display:flex;gap:12px;margin-bottom:12px;">
                <div style="flex:1;background:white;border:1px solid #e2e8f0;border-radius:8px;padding:10px;text-align:center;"><div style="font-size:11px;color:#64748b;">Low</div><div style="font-size:18px;font-weight:700;color:#dc2626;">${low_target:.2f}</div></div>
                <div style="flex:1.5;background:#1e3a8a;border-radius:8px;padding:10px;text-align:center;"><div style="font-size:11px;color:#93c5fd;">Mean Target</div><div style="font-size:22px;font-weight:700;color:white;">${mean_target:.2f}</div><div style="font-size:13px;color:{upside_color};font-weight:600;">{upside:+.1f}% upside</div></div>
                <div style="flex:1;background:white;border:1px solid #e2e8f0;border-radius:8px;padding:10px;text-align:center;"><div style="font-size:11px;color:#64748b;">High</div><div style="font-size:18px;font-weight:700;color:#16a34a;">${high_target:.2f}</div></div>
              </div>
              <div style="font-size:11px;color:#94a3b8;margin-bottom:4px;">Current price position in range</div>
              <div style="background:#e2e8f0;border-radius:4px;height:8px;position:relative;margin-bottom:4px;"><div style="background:#1d4ed8;height:8px;border-radius:4px;width:{bar_pct:.0f}%;"></div><div style="position:absolute;top:-3px;left:calc({bar_pct:.0f}% - 7px);width:14px;height:14px;background:#1d4ed8;border:2px solid white;border-radius:50%;"></div></div>
              <div style="display:flex;justify-content:space-between;font-size:10px;color:#94a3b8;"><span>${low_target:.2f}</span><span>Current ${price:.2f}</span><span>${high_target:.2f}</span></div>
            </div>""", unsafe_allow_html=True)
    except Exception:
        pass

    # DCF Valuation
    _render_dcf_card(r)

    # Insiders
    if insiders:
        INSIDER_CODES = {"P": "Buy", "A": "Award", "M": "Option Exercise",
                         "S": "Sale", "F": "Tax withholding", "G": "Gift", "D": "Disposition"}
        st.markdown("**Recent insider transactions**")
        ins_rows = [{"Name": i.get("name", ""), "Position": i.get("position") or "—",
                     "Date": i.get("transactionDate", ""),
                     "Type": INSIDER_CODES.get(i.get("transactionCode", ""), i.get("transactionCode", "")),
                     "Shares": f"{i.get('share', 0):,}",
                     "Value $": f"{i.get('value', 0):,.0f}" if i.get('value') else "—"} for i in insiders]
        st.dataframe(pd.DataFrame(ins_rows), use_container_width=True, hide_index=True)

    # Smart Money
    try:
        if (inst is not None and not inst.empty) or (funds is not None and not funds.empty):
            st.markdown("**Smart Money**")
            sm1, sm2 = st.columns(2)
            with sm1:
                if inst is not None and not inst.empty:
                    st.caption("Institutional Holders")
                    rows_inst = ""
                    for _, row in inst.head(8).iterrows():
                        chg   = round(row.get("pctChange", 0) or 0, 1)
                        color = "#16a34a" if chg > 0.05 else "#dc2626" if chg < -0.05 else "#6b7280"
                        arrow = "↑" if chg > 0.05 else "↓" if chg < -0.05 else "→"
                        val   = f"${row.get('Value', 0)/1e6:.1f}M" if row.get('Value') else "N/A"
                        rows_inst += f"<tr><td>{_html_mod.escape(str(row['Holder']))}</td><td style='color:{color};'>{arrow}{abs(chg):.1f}%</td><td style='color:#64748b;'>{val}</td></tr>"
                    st.markdown(f"""<table style="width:100%;border-collapse:collapse;"><thead><tr style="color:#6b7280;font-size:11px;"><th style="text-align:left;">Institution</th><th>Change</th><th>Value</th></tr></thead><tbody>{rows_inst}</tbody></table>""", unsafe_allow_html=True)
            with sm2:
                if funds is not None and not funds.empty:
                    st.caption("Mutual Fund Holders")
                    rows_funds = ""
                    for _, row in funds.head(8).iterrows():
                        chg   = round(row.get("pctChange", 0) or 0, 1)
                        color = "#16a34a" if chg > 0.05 else "#dc2626" if chg < -0.05 else "#6b7280"
                        arrow = "↑" if chg > 0.05 else "↓" if chg < -0.05 else "→"
                        val   = f"${row.get('Value', 0)/1e6:.1f}M" if row.get('Value') else "N/A"
                        rows_funds += f"<tr><td>{_html_mod.escape(str(row['Holder']))}</td><td style='color:{color};'>{arrow}{abs(chg):.1f}%</td><td style='color:#64748b;'>{val}</td></tr>"
                    st.markdown(f"""<table style="width:100%;border-collapse:collapse;"><thead><tr style="color:#6b7280;font-size:11px;"><th style="text-align:left;">Fund</th><th>Change</th><th>Value</th></tr></thead><tbody>{rows_funds}</tbody></table>""", unsafe_allow_html=True)
    except Exception:
        pass

    # News
    from src.news_fetcher import fetch_yfinance_news, fetch_alpha_vantage_news
    yf_news = fetch_yfinance_news(ticker, limit=10)
    av_news = fetch_alpha_vantage_news(ticker, days=7)
    seen_headlines = set()
    all_news = []
    for a in yf_news + news + av_news:
        key = (a.get('headline') or a.get('title', ''))[:50].lower()
        if key and key not in seen_headlines:
            seen_headlines.add(key)
            all_news.append(a)
    if all_news:
        st.markdown("**News this week**")
        for a in all_news[:6]:
            headline = a.get('headline') or a.get('title', '')
            url      = a.get('url', '')
            source   = a.get('source', '')
            pub      = a.get('published', '')
            if not pub and a.get('datetime'):
                pub = datetime.fromtimestamp(a['datetime']).strftime("%b %d")
            safe_headline = _html_mod.escape(str(headline))
            safe_source   = _html_mod.escape(str(source))
            safe_pub      = _html_mod.escape(str(pub)) if pub else ""
            if not url or not url.lower().startswith(("http://", "https://")):
                url = "#"
            st.markdown(f"- [{safe_headline}]({url}) — *{safe_source}{', ' + safe_pub if safe_pub else ''}*")

    # Bull vs Bear Debate
    debate_key = f"debate_{ticker}"
    if debate_key not in st.session_state:
        with st.spinner("Generating Bull vs Bear debate..."):
            try:
                from src.llm_client import llm_complete as _llm
                score_str   = _build_score_str(r)
                analyst_str = f"Analysts: {analyst_rec.get('strongBuy',0)} SB, {analyst_rec.get('buy',0)} B, {analyst_rec.get('hold',0)} H, {analyst_rec.get('sell',0)} Sell." if analyst_rec else ""
                insider_str = f"Insiders: {len([i for i in insiders if i.get('transactionCode')=='P'])} buys, {len([i for i in insiders if i.get('transactionCode')=='S'])} sales." if insiders else ""
                news_str    = "Headlines: " + " | ".join([a.get("headline", "") for a in news[:4]]) if news else ""
                debate_prompt = f"""You are a financial analysis moderator. Based on the data below, write a structured Bull vs Bear debate for {ticker} ({name}).

Data: {score_str} {analyst_str} {insider_str} {news_str}

You MUST use EXACTLY these section headers (with the emojis), nothing else:

🐂 BULL CASE
[write 3 bullish arguments in Hebrew here]

🐻 BEAR CASE
[write 3 bearish arguments in Hebrew here]

⚖️ VERDICT
[write 1-2 sentence conclusion in Hebrew with Buy/Hold/Sell]

Rules: Keep the headers in English exactly as shown. Content under each header in Hebrew. No Markdown bold (**). Plain text only. No preamble before 🐂."""
                st.session_state[debate_key] = _llm(debate_prompt, max_tokens=800)
            except Exception as e:
                st.session_state[debate_key] = f"Error: {e}"

    st.markdown("**🐂🐻 BULL vs BEAR DEBATE**")
    raw_debate = st.session_state[debate_key]

    import re as _re

    def _extract_section(text, emoji):
        """Extract content after an emoji header until the next emoji header or end."""
        # Match the emoji (possibly followed by text on the same line), capture everything until next emoji
        pattern = rf'{_re.escape(emoji)}[^\n]*\n(.*?)(?=🐂|🐻|⚖️|\Z)'
        m = _re.search(pattern, text, _re.DOTALL)
        if not m:
            return ""
        return m.group(1).strip()

    bull_text    = _extract_section(raw_debate, "🐂")
    bear_text    = _extract_section(raw_debate, "🐻")
    verdict_text = _extract_section(raw_debate, "⚖️")

    col_bull, col_bear = st.columns(2)
    with col_bull:
        st.markdown('<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:10px 14px;margin-bottom:8px;"><strong style="color:#16a34a;">🐂 BULL CASE</strong></div>', unsafe_allow_html=True)
        if bull_text:
            safe_bull = _html_mod.escape(bull_text).replace("\n", "<br>")
            st.markdown(f'<div dir="rtl" style="text-align:right;line-height:1.8;font-size:14px;">{safe_bull}</div>', unsafe_allow_html=True)
        else:
            st.caption("No bull arguments found.")
    with col_bear:
        st.markdown('<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:10px 14px;margin-bottom:8px;"><strong style="color:#dc2626;">🐻 BEAR CASE</strong></div>', unsafe_allow_html=True)
        if bear_text:
            safe_bear = _html_mod.escape(bear_text).replace("\n", "<br>")
            st.markdown(f'<div dir="rtl" style="text-align:right;line-height:1.8;font-size:14px;">{safe_bear}</div>', unsafe_allow_html=True)
        else:
            st.caption("No bear arguments found.")
    if verdict_text:
        st.markdown("**⚖️ VERDICT**")
        safe_verdict = _html_mod.escape(verdict_text).replace("\n", "<br>")
        st.markdown(f'<div dir="rtl" style="text-align:right;line-height:1.8;font-size:14px;">{safe_verdict}</div>', unsafe_allow_html=True)

    # AI Analysis
    ai_key = f"ai_summary_{ticker}"
    if ai_key not in st.session_state:
        with st.spinner("Generating AI analysis..."):
            try:
                from src.llm_client import llm_complete as _llm
                score_str   = _build_score_str(r)
                analyst_str = f"Analysts: {analyst_rec.get('strongBuy',0)} SB, {analyst_rec.get('buy',0)} B, {analyst_rec.get('hold',0)} H." if analyst_rec else ""
                insider_str = f"Company insiders: {len([i for i in insiders if i.get('transactionCode')=='P'])} open-market purchases, {len([i for i in insiders if i.get('transactionCode')=='S'])} sales." if insiders else ""
                news_str    = "Headlines: " + " | ".join([a.get("headline", "") for a in news[:4]]) if news else ""
                ai_prompt   = f"""Analyze {ticker} ({name}). Write 4 sentences on the most important signals. End with Buy/Hold/Sell and main reason.
{score_str} {analyst_str} {insider_str} {news_str}
Trends: {trends_data.get('interest', 0)}/100. Be direct. No disclaimers. Write in Hebrew. Do NOT use Markdown bold (**). Plain text only."""
                st.session_state[ai_key] = _llm(ai_prompt, max_tokens=600)
            except Exception as e:
                st.session_state[ai_key] = f"Error: {e}"

    st.markdown("**AI ANALYSIS — GEMINI / GROQ**")
    safe_ai = _html_mod.escape(st.session_state[ai_key]).replace("\n", "<br>")
    st.markdown(f'<div dir="rtl" style="text-align:right;line-height:1.9;font-size:14px;">{safe_ai}</div>', unsafe_allow_html=True)


def _render_dcf_card(r: dict):
    """DCF intrinsic value card."""
    if not r:
        return
    dcf = r.get("dcf")
    if not dcf:
        return

    mos   = dcf.get("margin_of_safety", 0)
    iv    = dcf.get("intrinsic_value", 0)
    price = dcf.get("current_price", 0)

    if mos >= 20:    lc = "#16a34a"
    elif mos >= 0:   lc = "#d97706"
    elif mos >= -20: lc = "#f97316"
    else:            lc = "#dc2626"

    bar_pct = min(max(price / iv * 100, 2), 98) if iv > 0 else 50

    from src.ui_theme import tooltip as _tip
    st.markdown(f"""<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:14px 18px;margin:8px 0;">
      <div style="font-size:12px;font-weight:600;color:#64748b;margin-bottom:10px;">📊 DCF VALUATION — 5 Year Horizon</div>
      <div style="display:flex;gap:12px;margin-bottom:12px;">
        <div style="flex:1;background:white;border:1px solid #e2e8f0;border-radius:8px;padding:10px;text-align:center;">
          <div style="font-size:11px;color:#64748b;"><span title="Estimated fair value per share from DCF: 5 years of discounted free cash flows + terminal value ÷ shares outstanding." style="cursor:help;text-decoration:underline dotted #94a3b8;">Intrinsic Value</span></div>
          <div style="font-size:22px;font-weight:700;color:#1e3a8a;">${iv:.2f}</div>
        </div>
        <div style="flex:1.5;background:#1e3a8a;border-radius:8px;padding:10px;text-align:center;">
          <div style="font-size:11px;color:#93c5fd;">{_tip('Margin of Safety')}</div>
          <div style="font-size:24px;font-weight:800;color:white;">{mos:+.1f}%</div>
          <div style="font-size:12px;color:{lc};font-weight:600;">{dcf.get('valuation','')}</div>
        </div>
        <div style="flex:1;background:white;border:1px solid #e2e8f0;border-radius:8px;padding:10px;text-align:center;">
          <div style="font-size:11px;color:#64748b;">Current Price</div>
          <div style="font-size:22px;font-weight:700;color:#374151;">${price:.2f}</div>
        </div>
      </div>
      <div style="background:#e2e8f0;border-radius:4px;height:8px;margin-bottom:4px;">
        <div style="background:{lc};height:8px;border-radius:4px;width:{bar_pct:.0f}%;"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:#94a3b8;margin-bottom:6px;">
        <span>$0</span><span>Current ${price:.2f}</span><span>Intrinsic ${iv:.2f}</span>
      </div>
      <div style="font-size:11px;color:#94a3b8;">
        Growth {dcf.get('growth_rate_used',0):.1f}% · <span title="Weighted Average Cost of Capital — discount rate for future cash flows. 10% base + leverage adjustment." style="cursor:help;text-decoration:underline dotted #94a3b8;">WACC</span> {dcf.get('wacc_used',0):.1f}% · Terminal 2.5% · FCF(TTM) ${dcf.get('fcf_ttm',0):.0f}M
      </div>
    </div>""", unsafe_allow_html=True)
