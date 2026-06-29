"""Page: Short Squeeze Scanner"""
import html as _html_mod
import streamlit as st
from datetime import datetime


def _html(raw: str) -> str:
    return " ".join(raw.split())


@st.cache_data(ttl=3600)
def _load_sectors(index_name: str) -> list:
    from src.index_loader import get_sectors
    return get_sectors(index_name)


@st.cache_data(ttl=3600)
def _load_tickers_by_sector(index_name: str, sector: str, max_stocks: int) -> list:
    from src.index_loader import get_tickers_by_sector
    return get_tickers_by_sector(index_name, sector, max_stocks)


def _fetch_insider_buyers(days: int, min_value: float) -> list:
    """Fetch insider buyers — no cache here, cache handled via session_state key."""
    try:
        from src.sec_api_client import get_recent_insider_buyers
        # limit=200 to get enough P-code transactions after filtering vesting/awards
        return get_recent_insider_buyers(days=days, min_value=min_value)
    except Exception:
        return []


def render():
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=15 * 60 * 1000, key="squeeze_autorefresh")  # 15 min

    st.markdown("### 🗜️ Short Squeeze Scanner")

    st.markdown(_html("""
        <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;
                    padding:12px 16px;margin-bottom:16px;font-size:13px;color:#0369a1;">
          You're not looking for a good company — you're looking for a
          <strong>trapped giant</strong>.
          High SI% + high DTC + rising borrow fee + volume ignition = potential squeeze.
          <br><strong>Warning:</strong> Most squeezes fail and become bull traps.
          Always confirm with volume before entering.
        </div>
    """), unsafe_allow_html=True)

    # ── Insider Buyers Panel ───────────────────────────────────────────────────
    _render_insider_buyers_panel()

    # ── Mode toggle ────────────────────────────────────────────────────────────
    scan_mode = st.radio(
        "Scan mode", ["Manual tickers", "By sector"],
        horizontal=True, label_visibility="collapsed", key="sq_mode"
    )

    tickers = []

    if scan_mode == "Manual tickers":
        col1, col2, col3 = st.columns([3, 1, 1])
        with col1:
            tickers_raw = st.text_input(
                "Tickers (comma separated)",
                "GME, AMC, PLTR, IONQ, RGTI, AIRS, NVAX, SAVA, UPST, HIMS, RIVN, LCID",
                key="sq_tickers"
            )
        with col2:
            min_score = st.slider("Min Score", 0, 100, 25, key="sq_min")
        with col3:
            st.markdown("<br>", unsafe_allow_html=True)
            run = st.button("🔍 Scan", key="sq_run")

        if run:
            tickers = [t.strip().upper() for t in tickers_raw.split(",") if t.strip()]

    else:  # By sector
        from src.index_loader import list_indices
        indices = list_indices()

        col1, col2, col3, col4 = st.columns([2, 2, 1, 1])
        with col1:
            sel_index = st.selectbox("Index", indices, key="sq_index")
        with col2:
            sectors = _load_sectors(sel_index)
            sel_sectors = st.multiselect(
                "Sectors", sectors,
                default=sectors[:1] if sectors else [],
                key="sq_sectors"
            )
        with col3:
            max_stocks = st.number_input("Max per sector", 10, 200, 50, step=10, key="sq_max")
        with col4:
            min_score = st.slider("Min Score", 0, 100, 25, key="sq_min_sector")

        run = st.button("🔍 Scan Sectors", key="sq_run_sector")

        if run and sel_sectors:
            for sec in sel_sectors:
                batch = _load_tickers_by_sector(sel_index, sec, int(max_stocks))
                tickers.extend(batch)
            tickers = list(dict.fromkeys(tickers))
            if tickers:
                st.caption(f"Loaded {len(tickers)} tickers from {len(sel_sectors)} sector(s)")
        elif run and not sel_sectors:
            st.warning("Select at least one sector.")

    # ── Run scan with progress bar ─────────────────────────────────────────────
    if run and tickers:
        from src.squeeze_scanner import scan_tickers

        st.markdown(f"**Scanning {len(tickers)} tickers...**")
        progress_bar  = st.progress(0)
        status_text   = st.empty()
        results_holder = []

        def _on_progress(done: int, total: int, ticker: str):
            pct = done / total
            progress_bar.progress(pct)
            status_text.caption(f"Analyzed {done}/{total} — {ticker}")

        results = scan_tickers(tickers, min_score=min_score, progress_callback=_on_progress)

        progress_bar.progress(1.0)
        status_text.caption(f"✅ Done — {len(results)} candidates above score {min_score}")

        st.session_state["sq_results"] = results
        st.session_state["sq_ts"]      = datetime.now().strftime("%H:%M:%S")

    # ── Display (persists across reruns) ───────────────────────────────────────
    results = st.session_state.get("sq_results")

    if not results:
        _render_legend()
        return

    st.caption(f"Last scan: {st.session_state.get('sq_ts', '')} · {len(results)} candidates found")

    # ── Insider + Squeeze overlap alert ────────────────────────────────────────
    insider_tickers = {b["ticker"] for b in st.session_state.get("sq_insider_buyers", [])}
    overlap = [r for r in results if r["ticker"] in insider_tickers]
    if overlap:
        names = ", ".join(r["ticker"] for r in overlap)
        st.markdown(_html(f"""
            <div style="background:linear-gradient(135deg,#064e3b,#065f46);
                        border:2px solid #34d399;border-radius:12px;
                        padding:14px 20px;margin-bottom:16px;">
              <div style="font-size:14px;color:#6ee7b7;font-weight:700;margin-bottom:4px;">
                🥇 INSIDER + SQUEEZE OVERLAP
              </div>
              <div style="font-size:13px;color:#a7f3d0;">
                {names} — High short interest AND insider buying this week.
                This combination historically precedes sharp moves.
              </div>
            </div>
        """), unsafe_allow_html=True)

    # ── Critical Alert Banner ──────────────────────────────────────────────────
    critical = [r for r in results if r.get("critical_alert")]
    if critical:
        names = ", ".join(r["ticker"] for r in critical)
        st.markdown(_html(f"""
            <div style="background:linear-gradient(135deg,#4c1d95,#7c3aed);
                        border-radius:12px;padding:14px 20px;margin-bottom:16px;
                        border:2px solid #a78bfa;">
              <div style="font-size:14px;color:#e9d5ff;font-weight:700;margin-bottom:4px;">
                🚨 ALL-IN SIGNAL DETECTED
              </div>
              <div style="font-size:13px;color:#ddd6fe;">
                {names} — Distance &lt;5% from breakout AND all pressure metrics in Top 10%.
                Confirm with live volume before acting.
              </div>
            </div>
        """), unsafe_allow_html=True)

    # ── KPI summary ────────────────────────────────────────────────────────────
    extreme   = sum(1 for r in results if r["score"] >= 80)
    high_p    = sum(1 for r in results if 65 <= r["score"] < 80)
    ignitions = sum(1 for r in results if r.get("ignition"))
    near_bo   = sum(1 for r in results if 0 <= r["dist_to_breakout_pct"] <= 3)

    st.markdown(_html(f"""
        <div style="display:flex;gap:10px;margin-bottom:16px;">
          <div style="flex:1;background:linear-gradient(135deg,#4c1d95,#7c3aed);
                      border-radius:10px;padding:12px 16px;">
            <div style="font-size:11px;color:#c4b5fd;">Extreme Pressure</div>
            <div style="font-size:26px;font-weight:700;color:#fff;">{extreme}</div>
          </div>
          <div style="flex:1;background:linear-gradient(135deg,#7f1d1d,#dc2626);
                      border-radius:10px;padding:12px 16px;">
            <div style="font-size:11px;color:#fca5a5;">High Pressure</div>
            <div style="font-size:26px;font-weight:700;color:#fff;">{high_p}</div>
          </div>
          <div style="flex:1;background:linear-gradient(135deg,#1e3a8a,#1d4ed8);
                      border-radius:10px;padding:12px 16px;">
            <div style="font-size:11px;color:#93c5fd;">Ignition Signals</div>
            <div style="font-size:26px;font-weight:700;color:#fff;">{ignitions}</div>
          </div>
          <div style="flex:1;background:linear-gradient(135deg,#064e3b,#059669);
                      border-radius:10px;padding:12px 16px;">
            <div style="font-size:11px;color:#6ee7b7;">Near Breakout (&lt;3%)</div>
            <div style="font-size:26px;font-weight:700;color:#fff;">{near_bo}</div>
          </div>
        </div>
    """), unsafe_allow_html=True)

    _render_table(results, insider_tickers)

    st.markdown("---")
    st.markdown("#### Detailed Cards")
    for r in results:
        _render_card(r, insider_tickers)


# ── Insider Buyers Panel ───────────────────────────────────────────────────────

def _render_insider_buyers_panel():
    import os
    if not os.getenv("SEC_API_KEY"):
        return

    with st.expander("🏦 Recent Insider Buyers (SEC Form 4)", expanded=False):
        col1, col2, col3 = st.columns([1, 1, 1])
        with col1:
            days = st.selectbox(
                "Lookback", [1, 3, 7], index=1,
                format_func=lambda x: f"Last {x} day{'s' if x > 1 else ''}",
                key="sq_insider_days",
            )
        with col2:
            min_val = st.selectbox(
                "Min purchase value", [25_000, 50_000, 100_000, 250_000],
                index=1, format_func=lambda x: f"${x:,.0f}",
                key="sq_insider_min_val",
            )
        with col3:
            st.markdown("<br>", unsafe_allow_html=True)
            refresh = st.button("🔄 Load", key="sq_insider_refresh")

        # Cache key encodes the parameters — refresh or param change triggers new fetch
        cache_key = f"sq_insider_buyers_{days}_{int(min_val)}"

        if refresh or cache_key not in st.session_state:
            # Clear old cache keys to avoid stale data showing
            for k in list(st.session_state.keys()):
                if k.startswith("sq_insider_buyers_") and k != cache_key:
                    del st.session_state[k]
            # Also clear the shared key used by overlap detection
            st.session_state.pop("sq_insider_buyers", None)

            with st.spinner("Fetching insider transactions..."):
                buyers = _fetch_insider_buyers(days=days, min_value=min_val)
                st.session_state[cache_key]        = buyers
                st.session_state["sq_insider_buyers"] = buyers  # shared for overlap detection

        buyers = st.session_state.get(cache_key, [])

        if not buyers:
            st.caption("No significant insider purchases found for this period.")
            return

        st.caption(f"{len(buyers)} insider purchase(s) found")

        rows_html = ""
        for b in buyers[:20]:
            val_str   = f"${b['value']:,.0f}"
            t_ticker  = _html_mod.escape(str(b.get("ticker", "")))
            t_insider = _html_mod.escape(str(b.get("insider", "")))
            t_role    = _html_mod.escape(str(b.get("role", "")))
            rows_html += _html(f"""
                <tr>
                  <td style='font-weight:700;color:#1e3a8a;padding:6px 8px;'>{t_ticker}</td>
                  <td style='padding:6px 8px;'>{t_insider}</td>
                  <td style='color:#64748b;font-size:11px;padding:6px 8px;'>{t_role}</td>
                  <td style='color:#16a34a;font-weight:600;padding:6px 8px;'>{val_str}</td>
                  <td style='color:#64748b;padding:6px 8px;'>{f"${b['price']:.2f}" if b['price'] else "N/A"}</td>
                  <td style='color:#64748b;font-size:11px;padding:6px 8px;'>{b['date']}</td>
                </tr>
            """)

        st.markdown(_html(f"""
            <table style="width:100%;border-collapse:collapse;font-size:13px;">
              <thead style="background:#1e3a8a;color:#bfdbfe;font-size:11px;">
                <tr>
                  <th style='padding:6px 8px;text-align:left;'>Ticker</th>
                  <th style='padding:6px 8px;text-align:left;'>Insider</th>
                  <th style='padding:6px 8px;text-align:left;'>Role</th>
                  <th style='padding:6px 8px;text-align:left;'>Value</th>
                  <th style='padding:6px 8px;text-align:left;'>Price</th>
                  <th style='padding:6px 8px;text-align:left;'>Date</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
        """), unsafe_allow_html=True)


# ── Table ──────────────────────────────────────────────────────────────────────

def _render_table(results: list, insider_tickers: set = None):
    insider_tickers = insider_tickers or set()
    st.markdown("#### Ranked by Squeeze Score")

    header = _html("""
        <tr>
          <th>Ticker</th><th>Score</th><th>Label</th>
          <th title="Short Interest as % of float shares. ≥15% = elevated, ≥20% = squeeze zone, ≥50% = extreme." style="cursor:help;">SI% Float</th>
          <th title="Days to Cover: shares sold short ÷ avg daily volume. ≥5 = trapped shorts, ≥10 = severe pressure." style="cursor:help;">DTC</th>
          <th title="Estimated annualised cost to borrow shares for short selling. ≥20% = strong short pressure confirmed." style="cursor:help;">Borrow Fee</th>
          <th title="Recent 5-day avg volume ÷ 30-day avg volume. ≥2.0x = volume spike. Classic squeeze trigger signal." style="cursor:help;">Vol Ratio</th>
          <th>Price</th>
          <th>Breakout $</th><th>Distance</th><th>Exit Target</th>
        </tr>
    """)

    rows_html = ""
    for r in results:
        sc         = r["label_color"]
        dist       = r["dist_to_breakout_pct"]
        borrow     = f"{r['borrow_fee']:.1f}%*" if r["borrow_fee"] is not None else "⚠️ N/A"
        is_insider = r["ticker"] in insider_tickers

        if dist < 0:
            dist_str = "<span style='color:#16a34a;font-weight:700;'>✅ Above</span>"
        elif dist <= 3:
            dist_str = f"<span style='color:#dc2626;font-weight:700;'>🔴 {dist:.1f}%</span>"
        elif dist <= 8:
            dist_str = f"<span style='color:#d97706;font-weight:700;'>{dist:.1f}%</span>"
        else:
            dist_str = f"<span style='color:#6b7280;'>{dist:.1f}%</span>"

        alert_icon   = "🚨 " if r.get("critical_alert") else ""
        insider_tag  = " 🥇" if is_insider else ""
        ignition_tag = f"<br><small>{r['ignition']}</small>" if r.get("ignition") else ""

        rows_html += _html(f"""
            <tr>
              <td><strong>{alert_icon}{r['ticker']}{insider_tag}</strong>{ignition_tag}</td>
              <td><span style='font-size:18px;font-weight:700;color:{sc};'>{r['score']:.0f}</span></td>
              <td><span style='background:{sc};color:white;padding:2px 8px;
                  border-radius:4px;font-size:11px;font-weight:600;'>{r['label']}</span></td>
              <td style='font-weight:700;color:{"#7c3aed" if r["si_pct"]>=50 else "#dc2626" if r["si_pct"]>=20 else "#374151"};'>{r['si_pct']:.1f}%</td>
              <td style='color:{"#dc2626" if r["dtc"]>=5 else "#d97706" if r["dtc"]>=3 else "#374151"};'>{r['dtc']:.1f}</td>
              <td style='color:{"#7c3aed" if r["borrow_fee"] and r["borrow_fee"]>=50 else "#dc2626" if r["borrow_fee"] and r["borrow_fee"]>=20 else "#94a3b8" if r["borrow_fee"] is None else "#374151"};'>{borrow}</td>
              <td style='color:{"#16a34a" if r["vol_ratio"]>=2 else "#374151"};'>{r['vol_ratio']:.2f}x</td>
              <td>${r['price']:.2f}</td>
              <td style='font-weight:600;'>${r['breakout_price']:.2f}</td>
              <td>{dist_str}</td>
              <td style='color:#16a34a;font-weight:600;'>${r['exit_target']:.2f}</td>
            </tr>
        """)

    st.markdown(f"""<div style="overflow-x:auto;">
      <table style="width:100%;border-collapse:collapse;font-size:13px;">
        <thead style="background:#1e3a8a;color:#bfdbfe;font-size:11px;">{header}</thead>
        <tbody>{rows_html}</tbody>
      </table></div>""", unsafe_allow_html=True)

    if insider_tickers:
        st.caption("🥇 = insider purchase detected · * Est. Borrow Fee via SI% approximation")
    else:
        st.caption("* Est. Borrow Fee derived from SI% via industry-calibrated scale (Finviz data)")


# ── Card ───────────────────────────────────────────────────────────────────────

def _render_card(r: dict, insider_tickers: set = None):
    insider_tickers = insider_tickers or set()
    sc         = r["label_color"]
    borrow_str = f"~{r['borrow_fee']:.1f}%*" if r["borrow_fee"] is not None else "⚠️ N/A (-20pts)"
    float_str  = f"{r['float_shares_m']:.1f}M" if r["float_shares_m"] else "N/A"
    mcap_str   = f"${r['market_cap_b']:.2f}B" if r["market_cap_b"] else "N/A"
    sma200_str = f"${r['sma200']:.2f}" if r["sma200"] else "N/A"
    dist       = r["dist_to_breakout_pct"]
    is_insider = r["ticker"] in insider_tickers

    if dist < 0:
        dist_display = f"✅ Already above breakout (+{abs(dist):.1f}%)"
        dist_color   = "#16a34a"
    elif dist <= 3:
        dist_display = f"🔴 MONEY TIME — {dist:.1f}% from breakout"
        dist_color   = "#dc2626"
    elif dist <= 8:
        dist_display = f"⚠️ {dist:.1f}% from breakout"
        dist_color   = "#d97706"
    else:
        dist_display = f"{dist:.1f}% from breakout"
        dist_color   = "#6b7280"

    ignition_html = ""
    if r.get("ignition"):
        ignition_html = f"<span style='background:#fef3c7;color:#92400e;border:1px solid #fcd34d;padding:2px 10px;border-radius:4px;font-size:12px;font-weight:600;margin-left:8px;'>{r['ignition']}</span>"

    critical_html = ""
    if r.get("critical_alert"):
        critical_html = "<span style='background:#7c3aed;color:white;padding:2px 10px;border-radius:4px;font-size:12px;font-weight:700;margin-left:8px;'>🚨 ALL-IN SIGNAL</span>"

    insider_html = ""
    if is_insider:
        insider_html = "<span style='background:#065f46;color:#6ee7b7;border:1px solid #34d399;padding:2px 10px;border-radius:4px;font-size:12px;font-weight:700;margin-left:8px;'>🥇 INSIDER BUY</span>"

    bar_pct = min(r["score"], 100)

    st.markdown(_html(f"""
        <div style="background:#f8fafc;border:1px solid {"#34d399" if is_insider else "#e2e8f0"};
                    border-radius:12px;padding:16px;margin-bottom:6px;
                    {"box-shadow:0 0 0 2px #34d399;" if is_insider else ""}">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap;">
            <span style="font-size:22px;font-weight:800;color:#0f172a;">{r['ticker']}</span>
            <span style="font-size:26px;font-weight:800;color:{sc};">{r['score']:.0f}</span>
            <span style="background:{sc};color:white;padding:3px 12px;border-radius:6px;
                         font-size:12px;font-weight:700;">{r['label']}</span>
            {ignition_html}{critical_html}{insider_html}
            <span style="font-size:15px;color:#374151;margin-left:auto;">
              Price: <strong>${r['price']:.2f}</strong>
            </span>
          </div>
          <div style="background:#e2e8f0;border-radius:4px;height:6px;margin-bottom:12px;">
            <div style="background:{sc};height:6px;border-radius:4px;width:{bar_pct:.0f}%;"></div>
          </div>
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px;">
            <div style="background:white;border:1px solid #e2e8f0;border-radius:8px;padding:8px;text-align:center;">
              <div style="font-size:10px;color:#64748b;"><span title="Short Interest as % of float shares. ≥15% = elevated, ≥20% = squeeze zone, ≥50% = extreme pressure." style="cursor:help;text-decoration:underline dotted #94a3b8;">SI% of Float</span></div>
              <div style="font-size:18px;font-weight:700;color:{"#7c3aed" if r["si_pct"]>=50 else "#dc2626" if r["si_pct"]>=20 else "#374151"};">{r['si_pct']:.1f}%</div>
            </div>
            <div style="background:white;border:1px solid #e2e8f0;border-radius:8px;padding:8px;text-align:center;">
              <div style="font-size:10px;color:#64748b;"><span title="Shares sold short ÷ average daily volume. How many days it would take all shorts to cover. ≥5 = trapped, ≥10 = severe." style="cursor:help;text-decoration:underline dotted #94a3b8;">Days to Cover</span></div>
              <div style="font-size:18px;font-weight:700;color:{"#dc2626" if r["dtc"]>=5 else "#d97706" if r["dtc"]>=3 else "#374151"};">{r['dtc']:.1f}</div>
            </div>
            <div style="background:white;border:1px solid #e2e8f0;border-radius:8px;padding:8px;text-align:center;">
              <div style="font-size:10px;color:#64748b;"><span title="Estimated annualised cost to borrow shares for short selling, derived from SI% via Finviz. ≥20% confirms strong short pressure." style="cursor:help;text-decoration:underline dotted #94a3b8;">Est. Borrow Fee</span></div>
              <div style="font-size:{"15px" if r["borrow_fee"] is None else "18px"};font-weight:700;color:{"#7c3aed" if r["borrow_fee"] and r["borrow_fee"]>=50 else "#dc2626" if r["borrow_fee"] and r["borrow_fee"]>=20 else "#94a3b8" if r["borrow_fee"] is None else "#374151"};">{borrow_str}</div>
            </div>
            <div style="background:white;border:1px solid #e2e8f0;border-radius:8px;padding:8px;text-align:center;">
              <div style="font-size:10px;color:#64748b;"><span title="Recent 5-day avg volume ÷ 30-day avg volume. ≥2x = significant spike. Rising price + volume spike = classic squeeze ignition signal." style="cursor:help;text-decoration:underline dotted #94a3b8;">Vol Ratio</span></div>
              <div style="font-size:18px;font-weight:700;color:{"#16a34a" if r["vol_ratio"]>=2 else "#374151"};">{r['vol_ratio']:.2f}x</div>
            </div>
            <div style="background:white;border:1px solid #e2e8f0;border-radius:8px;padding:8px;text-align:center;">
              <div style="font-size:10px;color:#64748b;">Float</div>
              <div style="font-size:14px;font-weight:600;color:#374151;">{float_str}</div>
            </div>
            <div style="background:white;border:1px solid #e2e8f0;border-radius:8px;padding:8px;text-align:center;">
              <div style="font-size:10px;color:#64748b;">Market Cap</div>
              <div style="font-size:14px;font-weight:600;color:#374151;">{mcap_str}</div>
            </div>
            <div style="background:white;border:1px solid #e2e8f0;border-radius:8px;padding:8px;text-align:center;">
              <div style="font-size:10px;color:#64748b;">60D High</div>
              <div style="font-size:14px;font-weight:600;color:#374151;">${r['high_60d']:.2f}</div>
            </div>
            <div style="background:white;border:1px solid #e2e8f0;border-radius:8px;padding:8px;text-align:center;">
              <div style="font-size:10px;color:#64748b;">SMA200</div>
              <div style="font-size:14px;font-weight:600;color:#374151;">{sma200_str}</div>
            </div>
          </div>
          <div style="background:linear-gradient(135deg,#1e3a8a,#1d4ed8);border-radius:8px;padding:10px 14px;">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
              <div>
                <div style="font-size:10px;color:#93c5fd;">Breakout Price = max(60D High, SMA200) × 1.01</div>
                <div style="font-size:20px;font-weight:800;color:white;">${r['breakout_price']:.2f}</div>
                <div style="font-size:12px;color:{dist_color};font-weight:600;">{dist_display}</div>
              </div>
              <div style="text-align:right;">
                <div style="font-size:10px;color:#93c5fd;">Exit Target (squeeze pop)</div>
                <div style="font-size:20px;font-weight:800;color:#4ade80;">${r['exit_target']:.2f}</div>
                <div style="font-size:11px;color:#86efac;">+{((r["exit_target"]/r["price"])-1)*100:.1f}% from current</div>
              </div>
            </div>
          </div>
        </div>
    """), unsafe_allow_html=True)

    _render_sparkline(r)
    _render_ai_verdict(r)
    st.markdown("<div style='margin-bottom:20px;'></div>", unsafe_allow_html=True)


def _render_sparkline(r: dict):
    spark = r.get("sparkline")
    if not spark or len(spark.get("dates", [])) < 2:
        return
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        dates   = spark["dates"]
        prices  = spark["prices"]
        volumes = spark["volumes"]

        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            row_heights=[0.65, 0.35], vertical_spacing=0.05)

        price_color = "#16a34a" if prices[-1] >= prices[0] else "#dc2626"
        rv, gv, bv  = _hex_to_rgb(price_color)

        fig.add_trace(go.Scatter(
            x=dates, y=prices, mode="lines+markers",
            line=dict(color=price_color, width=2), marker=dict(size=5),
            fill="tozeroy", fillcolor=f"rgba({rv},{gv},{bv},0.1)",
            showlegend=False,
        ), row=1, col=1)

        fig.add_hline(
            y=r["breakout_price"], line_dash="dash", line_color="#d97706", line_width=1.5,
            annotation_text=f"Breakout ${r['breakout_price']:.2f}",
            annotation_position="top right",
            annotation_font_size=10, annotation_font_color="#d97706",
            row=1, col=1,
        )

        vol_colors = [
            "#16a34a" if volumes[i] >= volumes[max(0, i-1)] else "#dc2626"
            for i in range(len(volumes))
        ]
        fig.add_trace(go.Bar(x=dates, y=volumes, marker_color=vol_colors, showlegend=False), row=2, col=1)

        fig.update_layout(
            height=220, margin=dict(l=0, r=0, t=6, b=0),
            plot_bgcolor="white", paper_bgcolor="white",
            xaxis2=dict(showgrid=False, color="#94a3b8"),
            yaxis=dict(showgrid=True, gridcolor="#f1f5f9", color="#94a3b8", tickprefix="$"),
            yaxis2=dict(showgrid=False, color="#94a3b8"),
        )
        st.caption("📈 7-Day Price / Volume — rising price + falling volume = bull trap warning")
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    except Exception as e:
        st.caption(f"Sparkline unavailable: {e}")


def _hex_to_rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _render_ai_verdict(r: dict):
    ai_key = f"sq_ai_{r['ticker']}"

    def _rtl(text: str) -> str:
        safe = _html_mod.escape(text).replace('\n', '<br>')
        return (f'<div style="direction:rtl;text-align:right;font-size:14px;'
                f'line-height:1.9;color:#1e293b;padding:4px 0;">{safe}</div>')

    with st.expander(f"🤖 AI Verdict — {r['ticker']}", expanded=False):
        if ai_key in st.session_state:
            st.markdown(_rtl(st.session_state[ai_key]), unsafe_allow_html=True)
            if st.button("🔄 Refresh", key=f"sq_ai_refresh_{r['ticker']}"):
                del st.session_state[ai_key]
        else:
            if st.button("Generate AI Verdict", key=f"sq_ai_btn_{r['ticker']}"):
                with st.spinner("Analyzing squeeze potential..."):
                    from src.squeeze_scanner import get_ai_verdict
                    verdict = get_ai_verdict(r)
                    st.session_state[ai_key] = verdict
                st.markdown(_rtl(verdict), unsafe_allow_html=True)


def _render_legend():
    st.markdown("#### How the Squeeze Score works")
    st.markdown(_html("""
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;
                    padding:16px;margin-top:8px;">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
            <div>
              <div style="font-size:12px;font-weight:700;color:#374151;margin-bottom:6px;">Score Components</div>
              <div style="font-size:13px;color:#6b7280;line-height:2;">
                🔴 <strong>50%</strong> — SI% of Float (20%+ = high, 50%+ = extreme)<br>
                🟠 <strong>20%</strong> — Days to Cover (3+ = trapped, 8+ = severe)<br>
                🟣 <strong>20%</strong> — Est. Borrow Fee (from SI% via Finviz)<br>
                🟢 <strong>10%</strong> — Volume Ratio (ignition signal)
              </div>
            </div>
            <div>
              <div style="font-size:12px;font-weight:700;color:#374151;margin-bottom:6px;">Penalty / Bonus</div>
              <div style="font-size:13px;color:#6b7280;line-height:2;">
                ⚠️ Borrow Fee = N/A → <strong>-20pts</strong><br>
                ✅ Borrow Fee ≥ 20% → <strong>+15pts</strong> bonus<br>
                🚨 ALL-IN: dist &lt;5% AND SI/DTC/Fee all Top 10%<br>
                🥇 INSIDER BUY: SEC Form 4 purchase this week
              </div>
            </div>
          </div>
          <div style="margin-top:10px;font-size:12px;color:#94a3b8;
                      border-top:1px solid #e2e8f0;padding-top:8px;">
            * Est. Borrow Fee approximated from SI% of Float (industry-calibrated).
            Sector mode pulls tickers from iShares index data.
            🥇 Insider data via SEC Form 4 (sec-api.io).
          </div>
        </div>
    """), unsafe_allow_html=True)
