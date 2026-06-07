"""Page: Catalyst Scanner — find the next explosion before it happens."""
import html as _html_mod
import streamlit as st
from loguru import logger


def _html(raw: str) -> str:
    return " ".join(raw.split())


def render():
    st.markdown("### Catalyst Scanner")

    st.markdown(_html("""
        <div style="background:#fefce8;border:1px solid #fde68a;border-radius:10px;
                    padding:12px 16px;margin-bottom:16px;font-size:13px;color:#92400e;">
          מוצא מניות small/mid-cap עם <strong>קטליזטור קרוב</strong> ופוטנציאל פיצוץ גבוה —
          earnings, שדרוג אנליסט, או הגשת 8-K מהותית — בשילוב עם SI% גבוה, float נמוך, ועלייה בנפח.
          <br><strong>אזהרה:</strong> מניות קטנות יכולות לנוע ±30% בזמן קצר. נהל גודל פוזיציה בהתאם.
        </div>
    """), unsafe_allow_html=True)

    # ── Source mode ────────────────────────────────────────────────────────────
    st.markdown("**מקור מניות**")
    source_mode = st.radio(
        "source",
        ["Nasdaq Calendar", "Watchlist + Portfolio", "Manual Tickers", "Index / Sector"],
        horizontal=True,
        label_visibility="collapsed",
        key="cat_source",
    )

    tickers: list = []
    index_name = sector = None

    if source_mode == "Nasdaq Calendar":
        st.caption("סורק את כל המניות שמדווחות earnings בחלון הזמן שנבחר.")
        tickers = []

    elif source_mode == "Watchlist + Portfolio":
        st.caption("בודק אם יש קטליזטור קרוב לכל מניה ב-Watchlist ו-Portfolio שלך.")
        from src.database import watchlist_get_all, portfolio_get_all
        wl  = [r["ticker"] for r in watchlist_get_all()]
        pt  = [r["ticker"] for r in portfolio_get_all()]
        tickers = list(dict.fromkeys(wl + pt))
        if tickers:
            st.info(f"נמצאו {len(tickers)} מניות: {', '.join(tickers[:15])}{'…' if len(tickers)>15 else ''}")
        else:
            st.warning("Watchlist ו-Portfolio ריקים. הוסף מניות תחילה.")
            return

    elif source_mode == "Manual Tickers":
        raw = st.text_input("Tickers (מופרדים בפסיקים)", "GME, AMC, UPST, HIMS, IONQ, SAVA, NVAX")
        tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
        if not tickers:
            st.warning("הזן לפחות ticker אחד.")
            return

    elif source_mode == "Index / Sector":
        from src.index_loader import list_indices, get_sectors
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            indices    = list_indices()
            index_name = st.selectbox("Index", indices, key="cat_index")
        with col_b:
            sectors = get_sectors(index_name)
            sector  = st.selectbox("Sector", ["All"] + sectors, key="cat_sector")
        with col_c:
            max_idx = st.number_input("Max stocks", 10, 300, 80, step=10, key="cat_max_idx")
        from src.index_loader import get_tickers_by_sector
        if sector == "All":
            tickers_raw = []
            for s in sectors:
                tickers_raw.extend(get_tickers_by_sector(index_name, s, int(max_idx)))
            tickers = list(dict.fromkeys(tickers_raw))
        else:
            tickers = get_tickers_by_sector(index_name, sector, int(max_idx))
        st.caption(f"{len(tickers)} מניות ב-{index_name} / {sector}")
        st.info("💡 לסריקת ביוטק: בחר **Russell 2000** → Sector: **Health Care** (~150 מניות)", icon=None)

    # ── Catalyst types ─────────────────────────────────────────────────────────
    st.markdown(_html("""
        <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;
                    padding:8px 16px 2px;margin-bottom:4px;">
          <span style="font-weight:600;font-size:13px;color:#1e40af;">🎯 סוגי קטליזטור</span>
        </div>
    """), unsafe_allow_html=True)
    col_t1, col_t2, col_t3, col_t4 = st.columns(4)
    with col_t1:
        use_earnings = st.checkbox("📅 Earnings", value=True, key="cat_t_earn")
        st.caption("דיווחי רבעוניים — Nasdaq Calendar.")
    with col_t2:
        use_analyst  = st.checkbox("📈 Analyst Upgrade", value=False, key="cat_t_ana")
        st.caption("Finnhub: שדרוגי אנליסטים. דורש FINNHUB_API_KEY.")
    with col_t3:
        use_8k       = st.checkbox("📋 SEC 8-K", value=False, key="cat_t_8k")
        st.caption("הגשות 8-K מהותיות ב-EDGAR.")
    with col_t4:
        use_pdufa    = st.checkbox("💊 FDA/PDUFA", value=False, key="cat_t_pdufa")
        st.caption("BioPharma Catalyst: תאריכי PDUFA/AdCom. ללא API key.")

    catalyst_types = []
    if use_earnings:  catalyst_types.append("earnings")
    if use_analyst:   catalyst_types.append("analyst")
    if use_8k:        catalyst_types.append("sec_8k")
    if use_pdufa:     catalyst_types.append("pdufa")

    if not catalyst_types:
        st.warning("בחר לפחות סוג קטליזטור אחד.")
        return

    # ── Filters ────────────────────────────────────────────────────────────────
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        days_ahead = st.selectbox("חלון זמן", [3, 5, 7, 14, 21, 30], index=2,
                                  format_func=lambda x: f"Next {x}d", key="cat_days")
    with col2:
        max_cap = st.selectbox("Max Cap",
                               [0.5, 1.0, 2.0, 5.0, 10.0, 20.0], index=3,
                               format_func=lambda x: f"<${x:.0f}B" if x >= 1 else f"<${x*1000:.0f}M",
                               key="cat_cap")
    with col3:
        min_si = st.slider("Min SI%", 0, 30, 0, step=5, key="cat_si")
    with col4:
        min_score = st.slider("Min Score", 0, 70, 70, step=5, key="cat_score")
    with col5:
        check_insider = st.checkbox("Insider check", value=False, key="cat_insider",
                                    help="~5s per ticker without SEC_API_KEY. Slow for large lists.")

    run = st.button("Scan for Catalysts", type="primary", key="cat_run")

    # ── Cache key ──────────────────────────────────────────────────────────────
    ticker_key  = ",".join(sorted(tickers)) if tickers else "calendar"
    cache_key   = f"{ticker_key}|{days_ahead}|{max_cap}|{min_si}|{min_score}|{check_insider}|{'_'.join(sorted(catalyst_types))}"

    if run:
        st.session_state.pop("catalyst_results",   None)
        st.session_state.pop("catalyst_cache_key", None)

    results = None
    if not run and st.session_state.get("catalyst_cache_key") == cache_key:
        results = st.session_state.get("catalyst_results", [])

    if run:
        from src.catalyst_scanner import scan_catalysts

        prog = st.progress(0.0, text="מאתחל...")
        stat = st.empty()

        def _phase(msg: str):
            prog.progress(0.0, text=msg)
            stat.caption(msg)

        def _cb(current, total, ticker):
            prog.progress(min(current / total, 1.0), text=f"בודק {ticker} ({current}/{total})")
            stat.caption(f"סורק {ticker}…")

        results = scan_catalysts(
            days_ahead=int(days_ahead),
            max_market_cap_b=float(max_cap),
            min_si_pct=float(min_si),
            min_explosion_score=float(min_score),
            check_insider=check_insider,
            catalyst_types=catalyst_types,
            tickers=tickers if tickers else None,
            progress_cb=_cb,
            phase_cb=_phase,
            watchlist_mode=(source_mode == "Watchlist + Portfolio"),
        )
        prog.empty()
        stat.empty()
        logger.info(f"[catalyst_page] scan done: {len(results)} candidates")
        news_hits    = sum(1 for r in results if r.get("news_catalyst"))
        insider_hits = sum(1 for r in results if r.get("has_insider"))
        logger.info(f"[catalyst_page] news_catalyst hits={news_hits}, insider hits={insider_hits}")
        st.session_state["catalyst_results"]   = results
        st.session_state["catalyst_cache_key"] = cache_key

    # ── Display ────────────────────────────────────────────────────────────────
    if results is None:
        st.info("בחר מקור מניות וסוג קטליזטור, לאחר מכן לחץ **Scan for Catalysts**.")
        return

    if not results:
        st.info("לא נמצאו מועמדים. נסה להרחיב את הפילטרים.")
        return

    _render_summary(results)
    _render_table(results)
    _render_ai_section(results)


# ── Summary ───────────────────────────────────────────────────────────────────

def _render_summary(results):
    from src.stock_scorer import signal_label
    high       = sum(1 for r in results if r["explosion_score"] >= 70)
    medium     = sum(1 for r in results if 50 <= r["explosion_score"] < 70)
    insider_ct = sum(1 for r in results if r["has_insider"])
    news_ct    = sum(1 for r in results if r.get("news_catalyst"))
    top        = results[0]
    earn_ct    = sum(1 for r in results if r["catalyst"] == "Earnings")
    ana_ct     = sum(1 for r in results if r["catalyst"] == "Analyst Upgrade")
    k8_ct      = sum(1 for r in results if r["catalyst"] == "SEC 8-K")
    pdufa_ct   = sum(1 for r in results if r["catalyst"] in ("FDA PDUFA", "FDA AdCom"))
    opt_ct     = sum(1 for r in results if r.get("has_unusual_calls"))

    st.markdown(_html(f"""
        <div class="metric-row">
          <div class="metric-card">
            <div class="metric-num">{len(results)}</div>
            <div class="metric-lbl">מועמדים</div>
          </div>
          <div class="metric-card">
            <div class="metric-num" style="color:#7c3aed;">{high}</div>
            <div class="metric-lbl">HIGH potential</div>
          </div>
          <div class="metric-card">
            <div class="metric-num" style="color:#dc2626;">{medium}</div>
            <div class="metric-lbl">MEDIUM potential</div>
          </div>
          <div class="metric-card">
            <div class="metric-num">{insider_ct}</div>
            <div class="metric-lbl">Insider buying 🥇</div>
          </div>
          <div class="metric-card">
            <div class="metric-num" style="color:#0ea5e9;">{news_ct}</div>
            <div class="metric-lbl">News catalyst 🔵</div>
          </div>
          <div class="metric-card">
            <div class="metric-num" style="color:{top['label_color']};">{top['explosion_score']:.0f}</div>
            <div class="metric-lbl">Top score ({top['ticker']})</div>
          </div>
        </div>
        <div style="font-size:12px;color:#64748b;margin-bottom:12px;">
          📅 Earnings: {earn_ct} &nbsp;|&nbsp; 📈 Analyst: {ana_ct} &nbsp;|&nbsp; 📋 SEC 8-K: {k8_ct}
          {f'&nbsp;|&nbsp; 💊 FDA/PDUFA: {pdufa_ct}' if pdufa_ct else ''}
          {f'&nbsp;|&nbsp; 📊 Unusual Options: {opt_ct}' if opt_ct else ''}
        </div>
    """), unsafe_allow_html=True)


# ── Table ─────────────────────────────────────────────────────────────────────

_CATALYST_ICON = {
    "Earnings":        "📅",
    "Analyst Upgrade": "📈",
    "SEC 8-K":         "📋",
    "Watchlist":       "👁",
    "FDA PDUFA":       "💊",
    "FDA AdCom":       "🏛",
}

def _render_table(results):
    st.markdown("#### מועמדים — ממויין לפי ציון פיצוץ")

    header = _html("""
        <tr>
          <th>Ticker</th>
          <th title="ציון פוטנציאל פיצוץ 0-100: urgency+SI%+float+volume+insider" style="cursor:help;">Score</th>
          <th title="סוג הקטליזטור ותיאור הארוע" style="cursor:help;">Catalyst</th>
          <th title="ימים עד לאירוע" style="cursor:help;">Days</th>
          <th>Price</th>
          <th>Mkt Cap</th>
          <th title="Float shares (מיליונים). Float נמוך = תנועות חדות יותר." style="cursor:help;">Float</th>
          <th title="Short Interest % of float. גבוה + קטליזטור = squeeze fuel." style="cursor:help;">SI%</th>
          <th title="5d avg volume ÷ 30d avg. >1.5x = ביקוש מצטבר." style="cursor:help;">Vol×</th>
          <th title="RSI 14 — מתחת ל-30 = oversold, מעל 70 = overbought." style="cursor:help;">RSI</th>
          <th title="MACD crossover direction." style="cursor:help;">MACD</th>
          <th title="מחיר ביחס ל-SMA20/50/200." style="cursor:help;">MA Trend</th>
          <th>5D%</th>
          <th>Insider</th>
          <th title="חדשות קטליזטור: FDA/merger/deal/partnership זוהה ב-headlines אחרונים">News</th>
        </tr>
    """)

    rows_html = ""
    for r in results:
        lc       = r["label_color"]
        days_d   = str(r["days_to_event"]) + "d" if r["days_to_event"] > 0 else "Today"
        day_col  = "#dc2626" if r["days_to_event"] <= 2 else "#d97706" if r["days_to_event"] <= 5 else "#374151"
        si_col   = "#7c3aed" if r["si_pct"] >= 20 else "#dc2626" if r["si_pct"] >= 10 else "#374151"
        vr_col   = "#16a34a" if r["vol_ratio"] >= 2 else "#374151"
        p5       = r.get("pct_5d")
        p5_s     = f"{p5:+.1f}%" if p5 is not None else "N/A"
        p5_col   = "#16a34a" if p5 and p5 > 0 else "#dc2626" if p5 and p5 < 0 else "#374151"
        _ins = r.get("insider_detail")
        if _ins:
            _net = _ins["net"]
            _b, _s = _ins["buys"], _ins["sells"]
            _cl = " ★" if _ins.get("clustered") else ""
            if _net > 0:
                insider = (
                    f"<span style='display:inline-block;background:#dcfce7;border:1.5px solid #16a34a;"
                    f"color:#15803d;font-weight:800;font-size:13px;padding:3px 8px;"
                    f"border-radius:6px;'>🟢 {_b}B{_cl}</span>"
                    + (f"<br><small style='color:#94a3b8;'>{_s}S</small>" if _s else "")
                )
            else:
                insider = (
                    f"<span style='display:inline-block;background:#fee2e2;border:1.5px solid #dc2626;"
                    f"color:#b91c1c;font-weight:800;font-size:13px;padding:3px 8px;"
                    f"border-radius:6px;'>🔴 {_s}S{_cl}</span>"
                    + (f"<br><small style='color:#94a3b8;'>{_b}B</small>" if _b else "")
                )
        else:
            insider = "<span style='color:#cbd5e1;'>—</span>"
        _nc_raw  = r.get("news_catalyst") or []
        nc_list  = _nc_raw if isinstance(_nc_raw, list) else ([_nc_raw] if _nc_raw else [])
        nc_html  = ("".join(
                        f"<span style='display:inline-block;background:#0ea5e9;color:#fff;"
                        f"padding:1px 5px;border-radius:3px;font-size:10px;margin:1px 1px;'>{kw}</span>"
                        for kw in nc_list
                    ) if nc_list else "—")
        icon     = _CATALYST_ICON.get(r["catalyst"], "🔔")
        cat_html = (f"{icon} <strong>{_html_mod.escape(str(r.get('catalyst', '')))}</strong>"
                    f"<br><small style='color:#64748b;'>{_html_mod.escape(str(r.get('catalyst_detail', ''))[:38])}</small>"
                    f"<br><small style='color:#374151;'>{_html_mod.escape(str(r.get('catalyst_date', '')))} {_html_mod.escape(str(r.get('catalyst_time', '')))}</small>")

        rsi_val  = f"{r['rsi']:.0f}" if r["rsi"] is not None else "N/A"
        rsi_col  = "#dc2626" if r["rsi"] and r["rsi"] > 70 else "#16a34a" if r["rsi"] and r["rsi"] < 35 else "#374151"
        macd_col = "#16a34a" if r["macd"] == "Bullish" else "#dc2626" if r["macd"] == "Bearish" else "#374151"
        mat_col  = "#16a34a" if r["ma_trend"] == "Uptrend" else "#dc2626" if r["ma_trend"] == "Downtrend" else "#374151"
        fl_col   = "#7c3aed" if r["float_m"] and r["float_m"] <= 15 else "#374151"

        _opt_badge = ""
        if r.get("has_unusual_calls"):
            _opt_badge = "<br><span style='background:#0ea5e9;color:#fff;padding:1px 4px;border-radius:3px;font-size:9px;'>📊 Options</span>"
        elif r.get("unusual_options_pts", 0) >= 4:
            _opt_badge = "<br><span style='background:#7dd3fc;color:#0c4a6e;padding:1px 4px;border-radius:3px;font-size:9px;'>📊 PCR↓</span>"

        rows_html += _html(f"""
            <tr>
              <td><strong>{_html_mod.escape(str(r.get('ticker', '')))}</strong><br>
                <small style='color:#64748b;font-size:10px;'>{_html_mod.escape(str(r.get('name', ''))[:20])}</small><br>
                <small style='color:#94a3b8;font-size:10px;'>{_html_mod.escape(str(r.get('sector', '')))}</small>
                {_opt_badge}
              </td>
              <td>
                <span style='font-size:20px;font-weight:800;color:{lc};'>{r['explosion_score']:.0f}</span><br>
                <span style='font-size:10px;background:{lc};color:#fff;padding:1px 6px;border-radius:3px;'>{r['label']}</span>
              </td>
              <td style='font-size:12px;'>{cat_html}</td>
              <td style='font-weight:700;color:{day_col};'>{days_d}</td>
              <td>${r['price']:.2f}</td>
              <td style='font-size:12px;'>{r['market_cap_disp']}</td>
              <td style='color:{fl_col};font-weight:{"700" if r["float_m"] and r["float_m"]<=15 else "400"};'>{r['float_disp']}</td>
              <td style='font-weight:700;color:{si_col};'>{r['si_pct']:.1f}%</td>
              <td style='color:{vr_col};font-weight:{"700" if r["vol_ratio"]>=2 else "400"};'>{r['vol_ratio']:.2f}x</td>
              <td style='color:{rsi_col};font-weight:600;'>{rsi_val}<br>
                <small style='font-size:9px;color:{rsi_col};'>{r.get("rsi_signal","")}</small>
              </td>
              <td style='color:{macd_col};font-size:12px;'>{r['macd']}</td>
              <td style='color:{mat_col};font-size:12px;'>{r['ma_trend']}</td>
              <td style='color:{p5_col};'>{p5_s}</td>
              <td style='font-size:16px;'>{insider}</td>
              <td style='font-size:11px;'>{nc_html}</td>
            </tr>
        """)

    st.markdown(_html(f"""
        <div style="overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;font-size:12px;">
          <thead style="background:#1e3a8a;color:#bfdbfe;font-size:11px;">{header}</thead>
          <tbody>{rows_html}</tbody>
        </table></div>
    """), unsafe_allow_html=True)
    st.caption("🟢 NB = N insider buys · 🔴 NS = N insider sells · ★ = cluster (3+ insiders) · Float <15M סגול · RSI<35 oversold · 🔵 news catalyst · 📊 Options = unusual call activity (yfinance)")


# ── AI Verdicts ───────────────────────────────────────────────────────────────

def _render_ai_section(results):
    st.markdown("---")
    st.markdown("#### AI Verdict — למה הוא עלול לפוצוץ?")
    st.caption("פתח ticker לניתוח AI on-demand.")

    for r in results:
        icon  = _CATALYST_ICON.get(r["catalyst"], "🔔")
        label = (f"{r['ticker']} · {r['explosion_score']:.0f}pts · "
                 f"{icon} {r['catalyst']} {r['catalyst_date']} · "
                 f"SI {r['si_pct']:.1f}% · Float {r['float_disp']} · {r['market_cap_disp']}")
        with st.expander(label):
            ai_key = f"catalyst_ai_{r['ticker']}_{r['catalyst']}"
            if ai_key in st.session_state:
                _show_verdict(st.session_state[ai_key])
            else:
                if st.button(f"Generate AI Analysis — {r['ticker']}", key=f"cat_ai_{r['ticker']}_{r['catalyst']}"):
                    with st.spinner("מנתח..."):
                        verdict = _get_ai_verdict(r)
                    st.session_state[ai_key] = verdict
                    _show_verdict(verdict)


def _show_verdict(text: str):
    safe = (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>"))
    st.markdown(
        f'<div style="font-size:14px;line-height:1.9;color:#1e293b;padding:4px 0;">{safe}</div>',
        unsafe_allow_html=True,
    )


def _get_ai_verdict(r: dict) -> str:
    try:
        from src.llm_client import llm_complete

        float_s   = f"{r['float_m']:.1f}M shares" if r["float_m"] else "unknown"
        insider_s = "YES — recent insider purchases (SEC Form 4)" if r["has_insider"] else "No"
        p5_s      = f"{r['pct_5d']:+.1f}%" if r.get("pct_5d") is not None else "N/A"

        prompt = f"""You are a quantitative trader specializing in catalyst-driven small cap setups.

Ticker:          {r['ticker']} ({r['name']})
Sector:          {r['sector']}
Market Cap:      {r['market_cap_disp']}
Price:           ${r['price']:.2f}
Float:           {float_s}
Short Interest:  {r['si_pct']:.1f}% of float
Volume Ratio:    {r['vol_ratio']:.2f}x (5d vs 30d avg)
5D Price Move:   {p5_s}
RSI:             {r['rsi']} ({r.get('rsi_signal','')})
MACD:            {r['macd']}
MA Trend:        {r['ma_trend']}
Insider Buying:  {insider_s}

Catalyst:        {r['catalyst']}
Catalyst Detail: {r['catalyst_detail']}
Catalyst Date:   {r['catalyst_date']} {r['catalyst_time']}
Explosion Score: {r['explosion_score']:.0f}/100

Write a concise 4-6 bullet analysis:
• Bull case — why this could squeeze/explode on the catalyst
• Bear/risk case — what could go wrong
• Technical setup — what RSI/MACD/MA trend says
• Key triggers to watch (price level, volume threshold)
• Suggested approach (if any — size small, wait for confirmation, etc.)

Be specific and direct. No fluff."""

        return llm_complete(prompt, max_tokens=500)
    except Exception as e:
        return f"AI analysis unavailable: {e}"
