"""Page: Market"""
import html as _html_mod
import streamlit as st
import time as _time
from datetime import datetime


def _html(raw: str) -> str:
    return " ".join(raw.split())


def render():
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=10 * 60 * 1000, key="market_autorefresh")  # 10 min

    from src.market_feed import (
        get_market_news, get_market_mood, get_upcoming_macro,
        get_earnings_calendar, get_market_indices, get_futures, get_vix_level,
    )

    st.markdown("### Market Overview")

    refresh_key = "market_data"
    refresh_ts  = st.session_state.get("market_refresh_ts", 0)
    now_ts      = _time.time()
    if now_ts - refresh_ts > 300 or refresh_key not in st.session_state:
        with st.spinner("Loading market data..."):
            indices  = get_market_indices()
            futures  = get_futures()
            articles = get_market_news(40)
            mood     = get_market_mood(articles)
            macro    = get_upcoming_macro(7)
            earnings = get_earnings_calendar(7)
        st.session_state[refresh_key]         = (indices, futures, articles, mood, macro, earnings)
        st.session_state["market_refresh_ts"] = now_ts
    else:
        indices, futures, articles, mood, macro, earnings = st.session_state[refresh_key]

    col_r, col_t = st.columns([1, 4])
    with col_r:
        if st.button("Refresh"):
            st.session_state.pop(refresh_key, None)
            st.session_state.pop("market_refresh_ts", None)
            st.rerun()
    with col_t:
        last = datetime.fromtimestamp(st.session_state.get("market_refresh_ts", now_ts))
        st.caption(f"Last update: {last.strftime('%H:%M:%S')} · auto-refresh every 5 min")

    # ── Futures Bar ────────────────────────────────────────────────────────────
    if futures:
        fut_html = ""
        for f in futures:
            color = "#16a34a" if f["up"] else "#dc2626"
            # VIX instruments: inverse colors (up = bad = red)
            if "VIX" in f["name"]:
                color = "#dc2626" if f["up"] else "#16a34a"
            arrow = "▲" if f["up"] else "▼"
            sign  = "+" if f["up"] else ""
            price_fmt = f"{f['price']:,.0f}" if f["price"] > 1000 else f"{f['price']:.2f}"
            fut_html += _html(f"""
                <div style="flex:1;min-width:130px;background:#ffffff;border-radius:10px;
                            padding:12px 14px;border:1px solid #e2e8f0;">
                  <div style="font-size:13px;font-weight:600;color:#374151;margin-bottom:4px;">{f['name']}</div>
                  <div style="font-size:18px;font-weight:700;color:#0f172a;margin-bottom:2px;">{price_fmt}</div>
                  <div style="font-size:13px;font-weight:600;color:{color};">{arrow}{sign}{f['change']:.2f}%</div>
                </div>
            """)
        st.markdown(
            _html(f"""
                <div style="margin-bottom:10px;">
                  <div style="font-size:11px;font-weight:600;color:#64748b;
                              letter-spacing:0.05em;margin-bottom:6px;">FUTURES</div>
                  <div style="display:flex;gap:6px;flex-wrap:wrap;">{fut_html}</div>
                </div>
            """),
            unsafe_allow_html=True,
        )

    # ── Market Indices + VIX ──────────────────────────────────────────────────
    if indices:
        vix_item  = next((i for i in indices if i["symbol"] == "^VIX"), None)
        vix_level = get_vix_level(vix_item["price"]) if vix_item else None

        # VIX card — standalone, above the index row
        if vix_item and vix_level:
            bar_pct = min(vix_item["price"] / 50 * 100, 100)
            chg_color = "#dc2626" if vix_item["up"] else "#16a34a"
            chg_arrow = "▲ +" if vix_item["up"] else "▼ "
            st.markdown(_html(f"""
                <div style="background:#ffffff;border:1px solid #e2e8f0;
                            border-radius:10px;padding:14px 20px;margin-bottom:10px;
                            display:flex;align-items:center;gap:24px;">
                  <div style="min-width:200px;">
                    <div style="font-size:13px;font-weight:600;color:#374151;margin-bottom:6px;cursor:help;"
                         title="CBOE Volatility Index — measures market expectation of 30-day volatility. &lt;15 = calm, 15-20 = normal, 20-30 = caution, 30-40 = fear, 40+ = panic.">VIX — CBOE Volatility Index</div>
                    <div style="display:flex;align-items:baseline;gap:10px;">
                      <span style="font-size:32px;font-weight:800;color:{vix_level['color']};">{vix_item['price']:.2f}</span>
                      <span style="font-size:13px;font-weight:700;background:{vix_level['color']}22;
                                  color:{vix_level['color']};padding:3px 10px;border-radius:4px;">
                        {vix_level['level']}
                      </span>
                      <span style="font-size:13px;font-weight:600;color:{chg_color};">
                        {chg_arrow}{vix_item['change']:.2f}%
                      </span>
                    </div>
                    <div style="font-size:12px;color:#64748b;margin-top:4px;">{vix_level['desc']}</div>
                  </div>
                  <div style="flex:1;">
                    <div style="background:#f1f5f9;border-radius:6px;height:10px;margin-bottom:6px;">
                      <div style="background:{vix_level['color']};height:10px;border-radius:6px;
                                  width:{bar_pct:.0f}%;"></div>
                    </div>
                    <div style="display:flex;justify-content:space-between;font-size:11px;color:#374151;font-weight:500;">
                      <span>0 Calm</span><span>15</span><span>20</span>
                      <span>30 Fear</span><span>40</span><span>50+ Panic</span>
                    </div>
                  </div>
                </div>
            """), unsafe_allow_html=True)

        # Regular indices (exclude VIX — shown separately above)
        idx_html = ""
        for item in indices:
            if item["symbol"] in ("^VIX", "VXc1"):
                continue
            color = "#16a34a" if item["up"] else "#dc2626"
            arrow = "+" if item["up"] else ""
            price_fmt = (
                f"{item['price']:,.0f}" if item["price"] > 1000
                else f"{item['price']:.2f}" if item["price"] > 10
                else f"{item['price']:.4f}"
            )
            idx_html += _html(f"""
                <div style="flex:1;min-width:130px;background:#ffffff;border-radius:10px;
                            padding:12px 14px;border:1px solid #e2e8f0;">
                  <div style="font-size:13px;font-weight:600;color:#374151;margin-bottom:4px;">{item['name']}</div>
                  <div style="font-size:18px;font-weight:700;color:#0f172a;margin-bottom:2px;">{price_fmt}</div>
                  <div style="font-size:13px;font-weight:600;color:{color};">{arrow}{item['change']:.2f}%</div>
                </div>
            """)
        st.markdown(
            f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;">{idx_html}</div>',
            unsafe_allow_html=True,
        )

    # ── Sector Heatmap ─────────────────────────────────────────────────────────
    sector_key = "sector_heatmap"
    sector_ts  = st.session_state.get("sector_refresh_ts", 0)
    if now_ts - sector_ts > 300 or sector_key not in st.session_state:
        SECTORS = {
            "XLK":  "Technology",    "XLF":  "Financials",
            "XLV":  "Healthcare",    "XLY":  "Cons. Discret.",
            "XLP":  "Cons. Staples", "XLE":  "Energy",
            "XLI":  "Industrials",   "XLB":  "Materials",
            "XLRE": "Real Estate",   "XLU":  "Utilities",
            "XLC":  "Comm. Svcs",
        }
        try:
            import yfinance as _yf
            tickers_str = " ".join(SECTORS.keys())
            data = _yf.download(tickers_str, period="5d", interval="1d", progress=False, auto_adjust=True)
            sector_data = []
            for etf, name in SECTORS.items():
                try:
                    closes = data["Close"][etf].dropna()
                    if len(closes) >= 2:
                        chg    = (closes.iloc[-1] - closes.iloc[-2]) / closes.iloc[-2] * 100
                        chg_5d = (closes.iloc[-1] - closes.iloc[0])  / closes.iloc[0]  * 100
                        sector_data.append({"name": name, "chg": round(chg, 2), "chg_5d": round(chg_5d, 2)})
                except Exception:
                    pass
            st.session_state[sector_key]            = sector_data
            st.session_state["sector_refresh_ts"]   = now_ts
        except Exception:
            st.session_state[sector_key] = []
    sector_data = st.session_state.get(sector_key, [])

    if sector_data:
        st.markdown("#### Sector Performance")
        tab1d, tab5d = st.tabs(["Today", "5 Days"])
        for tab, field in [(tab1d, "chg"), (tab5d, "chg_5d")]:
            with tab:
                sorted_s = sorted(sector_data, key=lambda x: x[field], reverse=True)
                max_abs  = max(abs(s[field]) for s in sorted_s) or 1
                cells = ""
                for s in sorted_s:
                    v     = s[field]
                    alpha = min(abs(v) / max_abs, 1.0)
                    if v > 0:
                        bg = f"rgba(22,163,74,{0.15 + alpha * 0.65})"
                        tc = "#14532d" if alpha > 0.5 else "#166534"
                    else:
                        bg = f"rgba(220,38,38,{0.15 + alpha * 0.65})"
                        tc = "#7f1d1d" if alpha > 0.5 else "#991b1b"
                    sign = "+" if v > 0 else ""
                    cells += _html(f"""
                        <div style="flex:1;min-width:90px;background:{bg};border-radius:8px;
                                    padding:10px 8px;text-align:center;">
                          <div style="font-size:11px;font-weight:600;color:{tc};margin-bottom:2px;">{s['name']}</div>
                          <div style="font-size:16px;font-weight:700;color:{tc};">{sign}{v:.2f}%</div>
                        </div>
                    """)
                st.markdown(
                    f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;">{cells}</div>',
                    unsafe_allow_html=True,
                )

    # ── Market Mood ────────────────────────────────────────────────────────────
    mood_color = "#16a34a" if mood["label"] == "Bullish" else "#dc2626" if mood["label"] == "Bearish" else "#6b7280"
    st.markdown(_html(f"""
        <div style="background:linear-gradient(135deg,#1e3a8a,#1d4ed8);border-radius:12px;
                    padding:14px 20px;margin-bottom:16px;display:flex;align-items:center;
                    justify-content:space-between;">
          <div>
            <div style="font-size:11px;color:#93c5fd;margin-bottom:2px;">
              MARKET MOOD — {len(articles)} articles
            </div>
            <div style="font-size:22px;font-weight:700;color:white;">{mood['label']}</div>
          </div>
          <div style="display:flex;gap:16px;text-align:center;">
            <div><div style="font-size:18px;font-weight:700;color:#4ade80;">{mood['bullish']}</div>
                 <div style="font-size:10px;color:#93c5fd;">Bullish</div></div>
            <div><div style="font-size:18px;font-weight:700;color:#94a3b8;">{mood['neutral']}</div>
                 <div style="font-size:10px;color:#93c5fd;">Neutral</div></div>
            <div><div style="font-size:18px;font-weight:700;color:#f87171;">{mood['bearish']}</div>
                 <div style="font-size:10px;color:#93c5fd;">Bearish</div></div>
          </div>
          <div style="text-align:center;">
            <div style="font-size:30px;font-weight:700;color:{mood_color};">{mood['score']}%</div>
            <div style="font-size:10px;color:#93c5fd;">bullish score</div>
          </div>
        </div>
    """), unsafe_allow_html=True)

    # ── News + Sidebar ─────────────────────────────────────────────────────────
    left, right = st.columns([2, 1])

    with left:
        st.markdown("#### Breaking News")

        def _news_item(a):
            sent = a['sentiment']
            if   'Bullish'          in sent and 'Somewhat' not in sent: lc = "#16a34a"
            elif 'Somewhat-Bullish' in sent:                             lc = "#4ade80"
            elif 'Somewhat-Bearish' in sent:                             lc = "#f97316"
            elif 'Bearish'          in sent:                             lc = "#dc2626"
            else:                                                        lc = "#94a3b8"
            tickers_html = "".join(
                f"<span style='background:#1e3a8a;color:#93c5fd;padding:1px 6px;"
                f"border-radius:3px;font-size:10px;margin-right:3px;'>{_html_mod.escape(str(t))}</span>"
                for t in a["tickers"][:3]
            )
            h = _html_mod.escape(str(a.get("headline", "")))
            s = _html_mod.escape(str(a.get("source", "")))
            _url = a.get("url", "") or ""
            if not _url.startswith(("http://", "https://")):
                _url = "#"
            return _html(f"""
                <div style="border-left:4px solid {lc};padding:9px 14px;margin-bottom:7px;
                            background:#ffffff;border-radius:0 8px 8px 0;border:1px solid #e2e8f0;border-left:4px solid {lc};">
                  <div style="display:flex;justify-content:space-between;gap:8px;">
                    <a href="{_html_mod.escape(_url)}" target="_blank"
                       style="font-size:13px;font-weight:600;color:#1e293b;text-decoration:none;flex:1;">{h}</a>
                    <span style="color:{lc};font-weight:700;font-size:12px;white-space:nowrap;">
                      {sent.replace('Somewhat-','~')}
                    </span>
                  </div>
                  <div style="font-size:11px;color:#94a3b8;margin-top:4px;">
                    {s} · {a['published']}{'&nbsp;&nbsp;' + tickers_html if tickers_html else ''}
                  </div>
                </div>
            """)

        sent_filter = st.multiselect(
            "Sentiment",
            ["Bullish","Somewhat-Bullish","Neutral","Somewhat-Bearish","Bearish"],
            default=["Bullish","Somewhat-Bullish","Neutral","Somewhat-Bearish","Bearish"],
            key="market_sent_filter", label_visibility="collapsed",
        )
        filtered = [a for a in articles if a["sentiment"] in sent_filter]
        top5     = "".join(_news_item(a) for a in filtered[:5])

        if len(filtered) > 5:
            more = "".join(_news_item(a) for a in filtered[5:25])
            expander = _html(f"""
                <details style="margin-top:4px;">
                  <summary style="cursor:pointer;font-size:13px;color:#374151;padding:8px 4px;
                                  list-style:none;display:flex;align-items:center;gap:6px;
                                  border-top:1px solid #e2e8f0;">
                    &#x203A; Show {min(len(filtered)-5, 20)} more articles
                  </summary>
                  <div style="margin-top:8px;">{more}</div>
                </details>
            """)
            st.markdown(
                f'<div style="border:1px solid #e2e8f0;border-radius:16px;padding:14px 16px;background:#fff;">{top5}{expander}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div style="border:1px solid #e2e8f0;border-radius:16px;padding:14px 16px;background:#fff;">{top5}</div>',
                unsafe_allow_html=True,
            )

    with right:
        st.markdown("#### Earnings This Week")
        if earnings:
            rows_html = ""
            for e in earnings[:15]:
                eps = f"EPS est. {_html_mod.escape(str(e['estimate']))}" if e['estimate'] else ""
                t_symbol = _html_mod.escape(str(e.get("symbol", "")))
                t_name   = _html_mod.escape(str(e.get("name", ""))[:20])
                rows_html += _html(f"""
                    <tr>
                      <td style="font-weight:700;font-size:13px;padding:7px 8px;">{t_symbol}</td>
                      <td style="font-size:12px;color:#374151;padding:7px 8px;">{t_name}</td>
                      <td style="font-size:11px;color:#1d4ed8;font-weight:600;padding:7px 8px;">{e['date']}</td>
                      <td style="font-size:11px;color:#6b7280;padding:7px 8px;">{e['time']}</td>
                      <td style="font-size:11px;color:#6b7280;padding:7px 8px;">{e['mcap']}</td>
                      <td style="font-size:11px;color:#16a34a;padding:7px 8px;">{eps}</td>
                    </tr>
                """)
            st.markdown(_html(f"""
                <div style="border:1px solid #e2e8f0;border-radius:16px;overflow:hidden;margin-bottom:16px;">
                <table style="width:100%;border-collapse:collapse;background:#fff;">
                  <thead><tr style="background:#f8fafc;border-bottom:2px solid #e2e8f0;">
                    <th style="text-align:left;padding:7px 8px;font-size:11px;color:#6b7280;">Symbol</th>
                    <th style="text-align:left;padding:7px 8px;font-size:11px;color:#6b7280;">Name</th>
                    <th style="text-align:left;padding:7px 8px;font-size:11px;color:#6b7280;">Date</th>
                    <th style="text-align:left;padding:7px 8px;font-size:11px;color:#6b7280;">Time</th>
                    <th style="text-align:left;padding:7px 8px;font-size:11px;color:#6b7280;">Cap</th>
                    <th style="text-align:left;padding:7px 8px;font-size:11px;color:#6b7280;">EPS</th>
                  </tr></thead><tbody>{rows_html}</tbody>
                </table></div>
            """), unsafe_allow_html=True)
        else:
            st.caption("No earnings this week")

        st.markdown("#### Macro Events This Week")
        if macro:
            rows_html = ""
            for m in macro:
                t_name   = _html_mod.escape(str(m.get("name", "")))
                t_impact = _html_mod.escape(str(m.get("impact", "")).upper())
                rows_html += _html(f"""
                    <div style="display:flex;justify-content:space-between;padding:7px 10px;
                                border-left:3px solid {m['color']};margin-bottom:5px;
                                background:#fafafa;border-radius:0 6px 6px 0;">
                      <div>
                        <span style="font-weight:600;font-size:13px;color:#1e293b;">{t_name}</span>
                        <span style="font-size:10px;background:{m['color']};color:white;
                                     padding:1px 6px;border-radius:3px;margin-left:6px;">
                          {t_impact}
                        </span>
                      </div>
                      <div style="text-align:right;font-size:11px;color:#6b7280;">
                        <div>{m['date']}</div><div>{m['time']}</div>
                      </div>
                    </div>
                """)
            st.markdown(
                f'<div style="border:1px solid #e2e8f0;border-radius:16px;padding:8px 12px;background:#fff;">{rows_html}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.caption("No macro events this week")
