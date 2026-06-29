"""Page: Backtest"""
import html as _html_mod
import math
import streamlit as st
import pandas as pd
from src.database import get_connection


def _compute_supertrend_series(hist: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.Series:
    high  = hist["High"]
    low   = hist["Low"]
    close = hist["Close"]

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    hl2   = (high + low) / 2.0
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    final_lower = lower.copy()
    final_upper = upper.copy()

    for i in range(1, len(hist)):
        if close.iloc[i - 1] >= final_lower.iloc[i - 1]:
            final_lower.iloc[i] = max(lower.iloc[i], final_lower.iloc[i - 1])
        else:
            final_lower.iloc[i] = lower.iloc[i]

        if close.iloc[i - 1] <= final_upper.iloc[i - 1]:
            final_upper.iloc[i] = min(upper.iloc[i], final_upper.iloc[i - 1])
        else:
            final_upper.iloc[i] = upper.iloc[i]

    trend = pd.Series(1, index=hist.index, dtype=int)
    for i in range(1, len(hist)):
        prev_trend = trend.iloc[i - 1]
        if prev_trend == 1:
            trend.iloc[i] = -1 if close.iloc[i] < final_lower.iloc[i] else 1
        else:
            trend.iloc[i] = 1  if close.iloc[i] > final_upper.iloc[i] else -1

    return trend


def _render_supertrend_backtest():
    st.markdown("### Supertrend P&L Backtest")
    st.caption("Simulates BUY/SELL trades based on Supertrend flips over the last 6 months. Commissions: Meitav Trade (min ₪7.50 / $7.50 per side). Tax: 25% on profits (Israel).")

    from src.database import watchlist_get_all
    items = watchlist_get_all()
    all_tickers = sorted({w["ticker"] for w in items}) if items else []

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        ticker = st.selectbox("Select ticker", all_tickers, key="st_bt_ticker") if all_tickers else st.text_input("Ticker", key="st_bt_ticker_input")
    with col2:
        position_size = st.number_input("Position size ($)", min_value=100, max_value=1_000_000, value=5_000, step=500, key="st_bt_pos")
    with col3:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        run_btn = st.button("Run Supertrend Backtest", key="st_bt_run")

    if not run_btn:
        return

    ticker = ticker.strip().upper() if ticker else ""
    if not ticker:
        st.warning("Please select or enter a ticker.")
        return

    with st.spinner(f"Fetching 6 months of data for {ticker}..."):
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="6mo", interval="1d", auto_adjust=True)
        except Exception as e:
            st.error(f"Failed to fetch data: {e}")
            return

    if hist is None or len(hist) < 15:
        st.warning(f"Not enough data for {ticker} (got {len(hist) if hist is not None else 0} bars, need at least 15).")
        return

    trend = _compute_supertrend_series(hist)

    buy_date = None
    buy_price = None
    trades = []

    for i in range(1, len(trend)):
        t_now  = trend.iloc[i]
        t_prev = trend.iloc[i - 1]
        date   = hist.index[i]
        price  = float(hist["Close"].iloc[i])

        if t_now == 1 and t_prev == -1 and buy_date is None:
            buy_date  = date
            buy_price = price

        elif t_now == -1 and t_prev == 1 and buy_date is not None:
            sell_date  = date
            sell_price = price
            shares     = math.floor(position_size / buy_price)
            if shares < 1:
                buy_date = buy_price = None
                continue
            buy_comm   = max(shares * 0.01, 7.50)
            sell_comm  = max(shares * 0.01, 7.50)
            gross      = (sell_price - buy_price) * shares
            tax        = max(0.0, gross * 0.25)
            net        = gross - buy_comm - sell_comm - tax
            gross_pct  = (sell_price - buy_price) / buy_price * 100
            hold_days  = (sell_date.date() if hasattr(sell_date, "date") else sell_date) - \
                         (buy_date.date()  if hasattr(buy_date,  "date") else buy_date)
            trades.append({
                "Date In":    str(buy_date.date()  if hasattr(buy_date,  "date") else buy_date)[:10],
                "Date Out":   str(sell_date.date() if hasattr(sell_date, "date") else sell_date)[:10],
                "Buy Price":  round(buy_price,  2),
                "Sell Price": round(sell_price, 2),
                "Shares":     shares,
                "Gross %":    round(gross_pct, 2),
                "Net $":      round(net,        2),
                "Hold Days":  hold_days.days if hasattr(hold_days, "days") else int(hold_days),
            })
            buy_date = buy_price = None

    if not trades:
        st.info("No completed BUY→SELL cycles found in the last 6 months. The trend may not have flipped enough times.")
        return

    df_trades = pd.DataFrame(trades)
    total_trades = len(df_trades)
    winners      = (df_trades["Net $"] > 0).sum()
    win_rate     = winners / total_trades * 100
    total_net    = df_trades["Net $"].sum()
    best_trade   = df_trades.loc[df_trades["Net $"].idxmax()]
    worst_trade  = df_trades.loc[df_trades["Net $"].idxmin()]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Trades", total_trades)
    c2.metric("Win Rate", f"{win_rate:.0f}%")
    c3.metric("Total Net Profit", f"${total_net:,.2f}")
    c4.metric("Avg per Trade", f"${total_net/total_trades:,.2f}")

    col_b, col_w = st.columns(2)
    with col_b:
        st.markdown(
            f"**Best trade:** {best_trade['Date In']} → {best_trade['Date Out']} "
            f"| **${best_trade['Net $']:,.2f}** ({best_trade['Gross %']:+.1f}%)"
        )
    with col_w:
        st.markdown(
            f"**Worst trade:** {worst_trade['Date In']} → {worst_trade['Date Out']} "
            f"| **${worst_trade['Net $']:,.2f}** ({worst_trade['Gross %']:+.1f}%)"
        )

    st.dataframe(
        df_trades.style.applymap(
            lambda v: "color:#16a34a;font-weight:600" if isinstance(v, (int, float)) and v > 0 else
                      "color:#dc2626;font-weight:600" if isinstance(v, (int, float)) and v < 0 else "",
            subset=["Net $", "Gross %"]
        ),
        use_container_width=True,
        hide_index=True,
    )

    df_trades["Cumulative Net $"] = df_trades["Net $"].cumsum()
    chart_df = df_trades[["Date Out", "Cumulative Net $"]].set_index("Date Out")
    st.line_chart(chart_df)


def render():
    from src.backtester import run_backtest, get_backtest_stats, get_top_signals, get_worst_signals, init_backtest_tables
    import plotly.graph_objects as go

    st.markdown("### Backtesting — Recommendation Accuracy")
    st.caption("Checks how many BUY signals actually went up after X days.")
    init_backtest_tables()

    col1, col2 = st.columns([1, 2])
    with col1:
        days_opt = st.selectbox("Check after", [7, 14, 30], format_func=lambda x: f"{x} days")
        if st.button("Run Backtest"):
            with st.spinner(f"Checking {days_opt} days back..."):
                summary = run_backtest(days_opt)
            if summary["total"] == 0:
                st.warning(f"No data from {days_opt} days ago.")
            else:
                st.success(f"Checked {summary['total']} signals")

    stats = get_backtest_stats()
    if stats:
        st.markdown("#### Accuracy by Horizon")
        cols = st.columns(len(stats))
        for i, s in enumerate(stats):
            acc   = round(s["correct"] / s["total"] * 100, 1) if s["total"] else 0
            color = "#16a34a" if acc >= 55 else "#d97706" if acc >= 45 else "#dc2626"
            benchmark_note = "Random = 50%" if s["total"] >= 10 else "Need more data"
            with cols[i]:
                st.markdown(f"""<div style="background:{color}18;border:1.5px solid {color};border-radius:8px;padding:16px 20px;max-width:320px;">
                  <div style="font-size:36px;font-weight:700;color:{color};line-height:1;">{acc:.0f}%</div>
                  <div style="font-size:12px;color:#6b7280;margin-top:2px;">accuracy · {s['days_ahead']} days · {s['total']} signals</div>
                  <div style="font-size:12px;color:{color};margin-top:6px;font-weight:600;">Avg return: {s['avg_return']:+.1f}%</div>
                  <div style="font-size:11px;color:#9ca3af;margin-top:4px;">Benchmark: {benchmark_note}</div>
                </div>""", unsafe_allow_html=True)

        top = get_top_signals(30)
        if top:
            st.markdown("#### Return Distribution")
            st.caption("Price % change from signal date to N days later — one bar per ticker (best result shown).")
            df_top = pd.DataFrame(top)
            colors = ["#16a34a" if x > 0 else "#dc2626" for x in df_top["pct_change"]]
            fig = go.Figure(go.Bar(x=df_top["ticker"], y=df_top["pct_change"], marker_color=colors,
                                   text=[f"{v:+.1f}%" for v in df_top["pct_change"]], textposition="outside"))
            fig.update_layout(height=300, margin=dict(l=0,r=0,t=20,b=0),
                              plot_bgcolor="white", paper_bgcolor="white",
                              xaxis=dict(showgrid=False, color="#64748b"),
                              yaxis=dict(showgrid=True, gridcolor="#f1f5f9", color="#64748b", zeroline=True,
                                         title="% Price Move"),
                              showlegend=False)
            st.plotly_chart(fig, use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("#### Best Stocks")
            top5 = get_top_signals(10)
            if top5:
                rows = ""
                for r in top5:
                    color    = "#16a34a" if r["pct_change"] > 0 else "#dc2626"
                    sig_date = r["signal_date"][:10] if r["signal_date"] else "—"
                    t_ticker = _html_mod.escape(str(r["ticker"]))
                    rows += (f"<tr><td><strong>{t_ticker}</strong></td><td>{r['score']:.0f}</td>"
                             f"<td style='color:{color};font-weight:600;'>{r['pct_change']:+.1f}%</td>"
                             f"<td>{r['days_ahead']}d</td><td style='color:#9ca3af;'>{sig_date}</td></tr>")
                st.markdown(f"""<table style="width:100%;font-size:13px;border-collapse:collapse;">
                <thead><tr style="color:#6b7280;font-size:12px;"><th style="text-align:left;">Stock</th><th>Score</th><th>% Move</th><th>Horizon</th><th>Signal Date</th></tr></thead>
                <tbody>{rows}</tbody></table>""", unsafe_allow_html=True)
        with c2:
            st.markdown("#### Worst Stocks")
            worst5 = get_worst_signals(10)
            if worst5:
                rows = ""
                for r in worst5:
                    sig_date = r["signal_date"][:10] if r["signal_date"] else "—"
                    t_ticker = _html_mod.escape(str(r["ticker"]))
                    rows += (f"<tr><td><strong>{t_ticker}</strong></td><td>{r['score']:.0f}</td>"
                             f"<td style='color:#dc2626;font-weight:600;'>{r['pct_change']:+.1f}%</td>"
                             f"<td>{r['days_ahead']}d</td><td style='color:#9ca3af;'>{sig_date}</td></tr>")
                st.markdown(f"""<table style="width:100%;font-size:13px;border-collapse:collapse;">
                <thead><tr style="color:#6b7280;font-size:12px;"><th style="text-align:left;">Stock</th><th>Score</th><th>% Move</th><th>Horizon</th><th>Signal Date</th></tr></thead>
                <tbody>{rows}</tbody></table>""", unsafe_allow_html=True)
    else:
        st.info("No backtest data yet. Run scans first, wait 7 days, then run backtest.")

    st.markdown("---")
    _render_supertrend_backtest()

    st.divider()
    st.subheader("📊 Forward Signal Win-Rate (Live)")
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT signal_type,
                       COUNT(*) as total,
                       ROUND(AVG(CASE WHEN return_7d_pct > 0 THEN 1.0 ELSE 0.0 END)*100, 1) as win_rate_7d,
                       ROUND(AVG(return_7d_pct), 2) as avg_return_7d,
                       ROUND(AVG(CASE WHEN return_30d_pct > 0 THEN 1.0 ELSE 0.0 END)*100, 1) as win_rate_30d
                FROM forward_signals
                WHERE return_7d_pct IS NOT NULL
                  AND data_quality_flag != 'SUSPECT'
                GROUP BY signal_type
            """).fetchall()
        if rows:
            df_fs = pd.DataFrame([dict(r) for r in rows])
            st.dataframe(df_fs, use_container_width=True)
        else:
            st.info("No matured forward signals yet.")
    except Exception as e:
        st.warning(f"Forward signals unavailable: {e}")
