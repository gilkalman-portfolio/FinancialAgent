"""Page: History"""
import streamlit as st
import pandas as pd
from src.database import get_db_stats, get_ticker_history, get_top_tickers, alert_trade_get_all


def render():
    st.markdown("### History")

    tab_scores, tab_bt = st.tabs(["📊 Score History", "🎯 Alert Backtest"])

    with tab_bt:
        _render_alert_backtest()

    with tab_scores:
        _render_score_history()


def _render_alert_backtest():
    from src.yf_cache import get_price as _cached_price

    trades = alert_trade_get_all(limit=300)
    if not trades:
        st.info("עדיין אין נתונים — ההתראות יתחילו להיאסף מרגע שה-scheduler רץ.")
        return

    df = pd.DataFrame(trades)
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"]  = pd.to_datetime(df["exit_time"],  errors="coerce")

    # Enrich open trades with current price / unrealized P&L
    open_mask = df["status"] == "open"
    for idx, row in df[open_mask].iterrows():
        curr = _cached_price(row["ticker"], ttl=180) or 0.0
        if curr and row["entry_price"]:
            df.at[idx, "pnl_pct"]    = round((curr - row["entry_price"]) / row["entry_price"] * 100, 2)
            df.at[idx, "exit_price"] = curr

    closed = df[df["status"] == "closed"]
    total  = len(df)
    n_open = int(open_mask.sum())
    n_closed = len(closed)

    # ── Stats strip ───────────────────────────────────────────────────────────
    if n_closed > 0:
        winners   = closed[closed["pnl_pct"] > 0]
        win_rate  = len(winners) / n_closed * 100
        avg_pnl   = closed["pnl_pct"].mean()
        best      = closed["pnl_pct"].max()
        worst     = closed["pnl_pct"].min()
    else:
        win_rate = avg_pnl = best = worst = 0.0

    def _kpi(label, value, color="#1d4ed8"):
        return (
            f"<div style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;"
            f"padding:10px 14px;text-align:center;'>"
            f"<div style='font-size:10px;color:#94a3b8;'>{label}</div>"
            f"<div style='font-size:18px;font-weight:700;color:{color};'>{value}</div>"
            f"</div>"
        )

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.markdown(_kpi("סה״כ עסקאות", total), unsafe_allow_html=True)
    c2.markdown(_kpi("פתוחות", n_open, "#7c3aed"), unsafe_allow_html=True)
    c3.markdown(_kpi("Win Rate", f"{win_rate:.0f}%", "#16a34a" if win_rate >= 50 else "#dc2626"), unsafe_allow_html=True)
    c4.markdown(_kpi("P&L ממוצע", f"{avg_pnl:+.1f}%", "#16a34a" if avg_pnl >= 0 else "#dc2626"), unsafe_allow_html=True)
    c5.markdown(_kpi("עסקה הטובה", f"{best:+.1f}%", "#16a34a"), unsafe_allow_html=True)
    c6.markdown(_kpi("עסקה הגרועה", f"{worst:+.1f}%", "#dc2626"), unsafe_allow_html=True)

    st.markdown("<div style='margin-bottom:12px'></div>", unsafe_allow_html=True)

    # ── P&L by alert type ─────────────────────────────────────────────────────
    if n_closed > 0:
        type_stats = (
            closed.groupby("entry_alert_type")["pnl_pct"]
            .agg(trades="count", avg_pnl="mean", win_rate=lambda x: (x > 0).mean() * 100)
            .reset_index()
            .sort_values("avg_pnl", ascending=False)
        )
        type_stats.columns = ["סוג התראה", "עסקאות", "P&L ממוצע %", "Win Rate %"]
        type_stats["P&L ממוצע %"] = type_stats["P&L ממוצע %"].round(1)
        type_stats["Win Rate %"]  = type_stats["Win Rate %"].round(0).astype(int)
        st.markdown("##### ביצועים לפי סוג התראה")
        st.dataframe(type_stats, use_container_width=True, hide_index=True)
        st.markdown("---")

    # ── Trades table ──────────────────────────────────────────────────────────
    st.markdown("##### כל העסקאות")

    disp = df.copy()
    disp["כניסה"]      = disp["entry_time"].dt.strftime("%d/%m %H:%M")
    disp["יציאה"]      = disp["exit_time"].dt.strftime("%d/%m %H:%M").fillna("—")
    disp["ימי החזקה"]  = (
        (disp["exit_time"].fillna(pd.Timestamp.now()) - disp["entry_time"])
        .dt.total_seconds().div(86400).round(1)
    )
    disp["יחס R"]      = (disp["hold_days_min"].astype(str) + "–" + disp["hold_days_max"].astype(str) + "d")
    disp["P&L %"]      = disp["pnl_pct"].map(lambda v: f"{v:+.1f}%" if pd.notna(v) else "—")
    disp["סטטוס"]      = disp["status"].map({"open": "🟡 פתוח", "closed": "✅ סגור"})
    disp["סיבת יציאה"] = disp["exit_reason"].fillna("—").map(
        {"sell_alert": "📉 התראת מכירה", "expired": "⏰ פג תוקף", "—": "—"}
    )

    _ALERT_HE = {
        "rsi_oversold": "RSI Oversold", "macd_bullish": "MACD ×", "supertrend_1h_flip": "ST 1h",
        "supertrend_flip": "ST Daily", "supertrend_triple_bull": "ST Triple", "breakout_alert": "Breakout",
    }
    disp["סוג התראה"] = disp["entry_alert_type"].map(lambda x: _ALERT_HE.get(x, x))

    table = disp[["ticker", "סוג התראה", "כניסה", "entry_price", "יציאה", "exit_price",
                  "ימי החזקה", "יחס R", "P&L %", "סטטוס", "סיבת יציאה"]].rename(columns={
        "ticker": "Ticker", "entry_price": "מחיר כניסה $", "exit_price": "מחיר יציאה $",
    })

    def _color_row(row):
        pnl_str = row["P&L %"]
        if pnl_str == "—":
            return [""] * len(row)
        val = float(pnl_str.replace("%", "").replace("+", ""))
        color = "background-color:#dcfce7" if val > 0 else "background-color:#fee2e2" if val < 0 else ""
        return [color] * len(row)

    styled = table.style.apply(_color_row, axis=1)
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _render_score_history():
    stats = get_db_stats()
    st.markdown(f"""<div class="metric-row">
      <div class="metric-card"><div class="metric-num">{stats['total_runs']}</div><div class="metric-lbl">Scan runs</div></div>
      <div class="metric-card"><div class="metric-num">{stats['total_results']}</div><div class="metric-lbl">Total results</div></div>
      <div class="metric-card"><div class="metric-num">{stats['unique_tickers']}</div><div class="metric-lbl">Unique tickers</div></div>
      <div class="metric-card"><div class="metric-num">{stats['total_alerts']}</div><div class="metric-lbl">Alerts sent</div></div>
    </div>""", unsafe_allow_html=True)

    st.markdown("#### Top recurring tickers (last 7 days)")
    top = get_top_tickers(days=7, min_score=40, limit=20)
    if top:
        df = pd.DataFrame(top)[["ticker","appearances","avg_score","max_score","last_seen","last_price"]]
        df["avg_score"] = df["avg_score"].round(1)
        df["max_score"] = df["max_score"].round(1)
        df["last_seen"] = pd.to_datetime(df["last_seen"]).dt.strftime("%b %d, %H:%M")
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No history yet. Run a scan first.")

    st.markdown("---")
    st.markdown("#### Score & Price History")

    col_a, col_b = st.columns([2, 1])
    with col_a:
        ticker_input = st.text_input("Ticker", "", placeholder="AAPL").upper().strip()
    with col_b:
        limit = st.selectbox("Show last N scans", [20, 30, 50, 100], index=1)

    if ticker_input:
        hist = get_ticker_history(ticker_input, limit=int(limit))
        if hist:
            _render_score_price_chart(ticker_input, hist)
        else:
            st.info(f"No history for {ticker_input} — run scans to accumulate data")


def _render_score_price_chart(ticker: str, hist: list):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    from src.stock_scorer import signal_label

    df = pd.DataFrame(hist)
    df["scanned_at"] = pd.to_datetime(df["scanned_at"])
    df = df.sort_values("scanned_at").reset_index(drop=True)
    df["explosion_score"] = pd.to_numeric(df["explosion_score"], errors="coerce")
    df["price"]           = pd.to_numeric(df["price"],           errors="coerce")

    has_price = df["price"].notna().any()

    # ── KPI strip ────────────────────────────────────────────────────────────
    latest = df.iloc[-1]
    prev   = df.iloc[-2] if len(df) > 1 else latest
    score_now   = latest["explosion_score"]
    score_delta = score_now - prev["explosion_score"] if pd.notna(prev["explosion_score"]) else 0
    price_now   = latest["price"]
    price_first = df["price"].dropna().iloc[0] if has_price else None
    price_chg   = ((price_now - price_first) / price_first * 100) if (has_price and price_first) else None
    score_max   = df["explosion_score"].max()
    score_min   = df["explosion_score"].min()

    delta_col  = "#16a34a" if score_delta >= 0 else "#dc2626"
    pchg_col   = "#16a34a" if (price_chg or 0) >= 0 else "#dc2626"

    kpis = [
        ("Score Now",    f"{score_now:.0f}",                    f"{score_delta:+.0f} vs prev", delta_col),
        ("Signal",       signal_label(score_now),               "",                            "#1d4ed8"),
        ("Score Max",    f"{score_max:.0f}",                    f"Min {score_min:.0f}",        "#7c3aed"),
        ("Price Now",    f"${price_now:.2f}" if has_price else "—",
                         f"{price_chg:+.1f}% period" if price_chg is not None else "",        pchg_col),
        ("Scans",        str(len(df)),                           f"last {(df['scanned_at'].iloc[-1]-df['scanned_at'].iloc[0]).days}d", "#64748b"),
    ]

    cols = st.columns(5)
    for col, (label, value, sub, color) in zip(cols, kpis):
        with col:
            st.markdown(
                f"<div style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;"
                f"padding:10px 12px;text-align:center;'>"
                f"<div style='font-size:10px;color:#94a3b8;'>{label}</div>"
                f"<div style='font-size:18px;font-weight:700;color:{color};'>{value}</div>"
                f"<div style='font-size:10px;color:#94a3b8;'>{sub}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
    st.markdown("<div style='margin-bottom:8px;'></div>", unsafe_allow_html=True)

    # ── Dual-axis chart ───────────────────────────────────────────────────────
    if has_price:
        fig = make_subplots(specs=[[{"secondary_y": True}]])
    else:
        fig = go.Figure()

    # Score trace
    score_colors = [
        "#16a34a" if s >= 75 else "#84cc16" if s >= 60 else "#f59e0b" if s >= 45 else "#dc2626"
        for s in df["explosion_score"].fillna(0)
    ]
    fig.add_trace(
        go.Scatter(
            x=df["scanned_at"], y=df["explosion_score"],
            name="Score", mode="lines+markers",
            line=dict(color="#1d4ed8", width=2.5),
            marker=dict(size=8, color=score_colors, line=dict(color="#fff", width=1.5)),
            hovertemplate="<b>%{x|%b %d %H:%M}</b><br>Score: %{y:.0f}<extra></extra>",
        ),
        **({"secondary_y": False} if has_price else {}),
    )

    # Price trace (secondary axis)
    if has_price:
        fig.add_trace(
            go.Scatter(
                x=df["scanned_at"], y=df["price"],
                name="Price $", mode="lines",
                line=dict(color="#f59e0b", width=2, dash="dot"),
                hovertemplate="<b>%{x|%b %d %H:%M}</b><br>Price: $%{y:.2f}<extra></extra>",
            ),
            secondary_y=True,
        )

    # Signal threshold lines
    for y, color, label in [(75, "#16a34a", "STRONG BUY"), (60, "#84cc16", "BUY"), (45, "#f59e0b", "WATCH")]:
        fig.add_hline(
            y=y, line_dash="dot", line_color=color, line_width=1,
            annotation_text=label, annotation_position="right",
            annotation_font_color=color, annotation_font_size=10,
            **({"secondary_y": False} if has_price else {}),
        )

    # Signal change annotations on the score line
    for i in range(1, len(df)):
        prev_sig = signal_label(df["explosion_score"].iloc[i - 1])
        curr_sig = signal_label(df["explosion_score"].iloc[i])
        if prev_sig != curr_sig:
            fig.add_annotation(
                x=df["scanned_at"].iloc[i],
                y=df["explosion_score"].iloc[i],
                text=curr_sig,
                showarrow=True, arrowhead=2, arrowsize=0.8,
                ax=0, ay=-28, font=dict(size=9, color="#1d4ed8"),
                bgcolor="#eff6ff", bordercolor="#1d4ed8", borderwidth=1,
            )

    layout_kwargs = dict(
        height=340, margin=dict(l=0, r=80, t=20, b=0),
        plot_bgcolor="white", paper_bgcolor="white",
        legend=dict(orientation="h", y=1.08, x=0),
        xaxis=dict(showgrid=False, color="#64748b"),
        yaxis=dict(
            title="Score", showgrid=True, gridcolor="#f1f5f9",
            color="#1d4ed8", range=[0, 105],
        ),
        hovermode="x unified",
    )
    if has_price:
        layout_kwargs["yaxis2"] = dict(
            title="Price $", color="#f59e0b",
            showgrid=False, tickprefix="$",
        )
    fig.update_layout(**layout_kwargs)

    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        f"🔵 Score (left axis)  {'·  🟡 Price $ (right axis, dotted)  ' if has_price else ''}"
        "·  Markers colored by signal: 🟢 STRONG BUY  🟡 BUY  🟠 WATCH  🔴 SKIP  "
        "·  Annotations mark signal changes"
    )
