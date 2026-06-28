"""Page: Options Flow"""
import html
import streamlit as st
from datetime import datetime


def _html(raw: str) -> str:
    return " ".join(raw.split())


def _rtl(text: str) -> str:
    """Wrap markdown text in an RTL Hebrew container."""
    return (
        '<div dir="rtl" style="text-align:right;font-family:\'Segoe UI\',Arial,sans-serif;'
        'font-size:14px;line-height:1.8;color:#1e293b;background:#f8fafc;'
        'border:1px solid #e2e8f0;border-radius:10px;padding:16px 20px;">'
        + html.escape(text).replace("\n", "<br>")
        + "</div>"
    )


@st.cache_data(ttl=300)
def _fetch_summary(ticker: str) -> dict | None:
    from src.options_flow import get_options_summary
    return get_options_summary(ticker, max_expirations=6)


def render():
    st.markdown("### 🎯 Options Flow")

    st.markdown(_html("""
        <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;
                    padding:12px 16px;margin-bottom:16px;font-size:13px;color:#0369a1;">
          Options flow reveals <strong>where the big money is positioning</strong>.
          A PCR &lt; 0.7 signals bullish sentiment; PCR &gt; 1.2 signals defensive/bearish hedging.
          Unusual activity (vol &gg; OI) often precedes sharp moves — it means <em>new</em> positions,
          not just rolls. Always confirm with price action and volume.
        </div>
    """), unsafe_allow_html=True)

    tab1, tab2 = st.tabs(["🔍 Single Ticker Deep Dive", "📡 Unusual Activity Scanner"])

    with tab1:
        _render_single_ticker()

    with tab2:
        _render_scanner()


# ── Single Ticker ──────────────────────────────────────────────────────────────

def _render_single_ticker():
    col1, col2 = st.columns([3, 1])
    with col1:
        ticker = st.text_input("Ticker", "AAPL", key="of_ticker").strip().upper()
    with col2:
        st.markdown("<br>", unsafe_allow_html=True)
        analyze = st.button("🔍 Analyze", key="of_analyze")

    if analyze and ticker:
        with st.spinner(f"Fetching options chain for {ticker}…"):
            data = _fetch_summary(ticker)
        if data:
            st.session_state["of_data"]         = data
            st.session_state["of_last_ticker"]  = ticker
            st.session_state["of_ts"]           = datetime.now().strftime("%H:%M:%S")
        else:
            st.error(f"No options data found for **{ticker}**. Check the ticker or try another.")
            st.session_state.pop("of_data", None)

    data = st.session_state.get("of_data")
    if not data:
        _render_legend()
        return

    if st.session_state.get("of_last_ticker") != ticker and not analyze:
        st.info("Press **Analyze** to load data for this ticker.")
        return

    ts = st.session_state.get("of_ts", "")
    st.caption(f"Last fetched: {ts} · {len(data['expirations'])} expirations loaded")

    _render_kpi_cards(data)
    st.markdown("---")
    _render_unusual_table(data)
    st.markdown("---")
    _render_strike_heatmap(data)
    st.markdown("---")
    _render_expiry_chart(data)
    st.markdown("---")
    _render_ai_verdict(data)


# ── KPI Cards ─────────────────────────────────────────────────────────────────

def _render_kpi_cards(data: dict):
    pcr        = data.get("pcr_vol")
    call_vol   = data.get("total_call_vol", 0)
    put_vol    = data.get("total_put_vol",  0)
    call_prem  = data.get("call_premium",   0)
    put_prem   = data.get("put_premium",    0)
    price      = data.get("price")

    # PCR sentiment
    if pcr is None:
        pcr_label, pcr_color = "N/A", "#6b7280"
    elif pcr < 0.7:
        pcr_label, pcr_color = "BULLISH", "#16a34a"
    elif pcr < 1.0:
        pcr_label, pcr_color = "NEUTRAL", "#d97706"
    elif pcr < 1.2:
        pcr_label, pcr_color = "CAUTIOUS", "#f97316"
    else:
        pcr_label, pcr_color = "BEARISH", "#dc2626"

    pcr_str = f"{pcr:.2f}" if pcr is not None else "N/A"

    # Net flow
    net = call_prem - put_prem
    if net > 0:
        flow_label, flow_color = "CALL-HEAVY", "#16a34a"
    elif net < 0:
        flow_label, flow_color = "PUT-HEAVY", "#dc2626"
    else:
        flow_label, flow_color = "NEUTRAL", "#6b7280"
    net_str  = f"${abs(net)/1_000_000:.1f}M" if abs(net) >= 1_000_000 else f"${abs(net)/1_000:.0f}K"
    call_p_str = f"${call_prem/1_000_000:.1f}M" if call_prem >= 1_000_000 else f"${call_prem/1_000:.0f}K"
    put_p_str  = f"${put_prem/1_000_000:.1f}M"  if put_prem  >= 1_000_000 else f"${put_prem/1_000:.0f}K"

    price_str  = f"${price:.2f}" if price else "N/A"

    st.markdown(_html(f"""
        <div style="display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;">
          <div style="flex:1;min-width:140px;background:linear-gradient(135deg,#1e3a8a,#1d4ed8);
                      border-radius:10px;padding:12px 16px;">
            <div style="font-size:11px;color:#93c5fd;">Put/Call Ratio (Vol)</div>
            <div style="font-size:28px;font-weight:800;color:#fff;">{pcr_str}</div>
            <div style="font-size:12px;font-weight:700;color:{pcr_color};"
                 style="background:rgba(255,255,255,.1);border-radius:4px;padding:1px 6px;">{pcr_label}</div>
          </div>
          <div style="flex:1;min-width:140px;background:linear-gradient(135deg,#064e3b,#059669);
                      border-radius:10px;padding:12px 16px;">
            <div style="font-size:11px;color:#6ee7b7;">Call Volume</div>
            <div style="font-size:24px;font-weight:800;color:#fff;">{call_vol:,}</div>
            <div style="font-size:12px;color:#a7f3d0;">Premium: {call_p_str}</div>
          </div>
          <div style="flex:1;min-width:140px;background:linear-gradient(135deg,#7f1d1d,#dc2626);
                      border-radius:10px;padding:12px 16px;">
            <div style="font-size:11px;color:#fca5a5;">Put Volume</div>
            <div style="font-size:24px;font-weight:800;color:#fff;">{put_vol:,}</div>
            <div style="font-size:12px;color:#fecaca;">Premium: {put_p_str}</div>
          </div>
          <div style="flex:1;min-width:140px;background:linear-gradient(135deg,#312e81,#4338ca);
                      border-radius:10px;padding:12px 16px;">
            <div style="font-size:11px;color:#c7d2fe;">Net Premium Flow</div>
            <div style="font-size:24px;font-weight:800;color:#fff;">{net_str}</div>
            <div style="font-size:12px;font-weight:700;color:{flow_color};">{flow_label}</div>
          </div>
          <div style="flex:1;min-width:140px;background:linear-gradient(135deg,#292524,#44403c);
                      border-radius:10px;padding:12px 16px;">
            <div style="font-size:11px;color:#d6d3d1;">Last Price</div>
            <div style="font-size:24px;font-weight:800;color:#fff;">{price_str}</div>
            <div style="font-size:12px;color:#a8a29e;">{data['ticker']}</div>
          </div>
        </div>
    """), unsafe_allow_html=True)


# ── Unusual Activity Table ─────────────────────────────────────────────────────

def _render_unusual_table(data: dict):
    unusual = data.get("unusual", [])
    st.markdown(f"#### ⚡ Unusual Activity — {len(unusual)} signal{'s' if len(unusual) != 1 else ''}")

    if not unusual:
        st.caption("No unusual activity detected (vol/OI < 3× and volume < 5,000).")
        return

    rows_html = ""
    for u in unusual[:25]:
        side_color  = "#16a34a" if u["side"] == "CALL" else "#dc2626"
        itm_badge   = "<span style='background:#fef9c3;color:#92400e;border:1px solid #fde68a;padding:1px 5px;border-radius:3px;font-size:10px;'>ITM</span>" if u["in_the_money"] else ""
        ratio_str   = f"{u['vol_oi_ratio']}×" if u["vol_oi_ratio"] < 999 else "NEW"
        ratio_color = "#7c3aed" if u["vol_oi_ratio"] >= 10 or u["vol_oi_ratio"] == 9999 else "#d97706"

        rows_html += _html(f"""
            <tr>
              <td style="padding:6px 8px;font-weight:700;color:{side_color};">{u['side']}</td>
              <td style="padding:6px 8px;">{u['expiration']}</td>
              <td style="padding:6px 8px;font-weight:600;">${u['strike']:.0f} {itm_badge}</td>
              <td style="padding:6px 8px;font-weight:700;color:#1e3a8a;">{u['volume']:,}</td>
              <td style="padding:6px 8px;color:#64748b;">{u['open_interest']:,}</td>
              <td style="padding:6px 8px;font-weight:700;color:{ratio_color};">{ratio_str}</td>
              <td style="padding:6px 8px;color:#6b7280;">{u['iv']:.1f}%</td>
              <td style="padding:6px 8px;color:#374151;">${u['last_price']:.2f}</td>
            </tr>
        """)

    st.markdown(_html(f"""
        <div style="overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <thead style="background:#1e3a8a;color:#bfdbfe;font-size:11px;">
            <tr>
              <th style="padding:6px 8px;text-align:left;">Side</th>
              <th style="padding:6px 8px;text-align:left;">Expiry</th>
              <th style="padding:6px 8px;text-align:left;">Strike</th>
              <th style="padding:6px 8px;text-align:left;">Volume</th>
              <th style="padding:6px 8px;text-align:left;">Open Interest</th>
              <th style="padding:6px 8px;text-align:left;">Vol/OI</th>
              <th style="padding:6px 8px;text-align:left;">IV</th>
              <th style="padding:6px 8px;text-align:left;">Last</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        </div>
    """), unsafe_allow_html=True)
    st.caption("Vol/OI ≥ 3× = new positioning. 'NEW' = no prior OI (opening sweep).")


# ── Strike OI Heatmap ─────────────────────────────────────────────────────────

def _render_strike_heatmap(data: dict):
    strikes = data.get("strikes", [])
    if not strikes:
        return

    st.markdown("#### 📊 Open Interest by Strike")
    try:
        import plotly.graph_objects as go
        import pandas as pd

        df = pd.DataFrame(strikes)
        # Focus on strikes near the current price (±30%)
        price = data.get("price")
        if price:
            df = df[(df["strike"] >= price * 0.7) & (df["strike"] <= price * 1.3)]

        if df.empty:
            st.caption("No strike data in range.")
            return

        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=df["strike"], y=df["call_oi"],
            name="Call OI", marker_color="#16a34a", opacity=0.8,
        ))
        fig.add_trace(go.Bar(
            x=df["strike"], y=-df["put_oi"],
            name="Put OI", marker_color="#dc2626", opacity=0.8,
        ))

        if price:
            fig.add_vline(
                x=price, line_dash="dash", line_color="#d97706", line_width=2,
                annotation_text=f"${price:.2f}",
                annotation_font_color="#d97706",
                annotation_position="top right",
            )

        fig.update_layout(
            barmode="overlay",
            height=280,
            margin=dict(l=0, r=0, t=10, b=0),
            plot_bgcolor="white", paper_bgcolor="white",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            xaxis=dict(title="Strike", tickprefix="$", gridcolor="#f1f5f9"),
            yaxis=dict(title="OI (calls ▲  puts ▼)", gridcolor="#f1f5f9",
                       tickformat=","),
        )
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        st.caption("Green = Call OI  ·  Red = Put OI (mirrored)  ·  High OI = key support/resistance")
    except Exception as e:
        st.caption(f"Heatmap unavailable: {e}")


# ── Expiry Volume Chart ────────────────────────────────────────────────────────

def _render_expiry_chart(data: dict):
    exp_data = data.get("exp_breakdown", [])
    if not exp_data:
        return

    st.markdown("#### 📅 Volume by Expiration")
    try:
        import plotly.graph_objects as go
        import pandas as pd

        df = pd.DataFrame(exp_data)
        fig = go.Figure()
        fig.add_trace(go.Bar(x=df["expiration"], y=df["call_volume"],
                             name="Call Vol", marker_color="#16a34a", opacity=0.85))
        fig.add_trace(go.Bar(x=df["expiration"], y=df["put_volume"],
                             name="Put Vol", marker_color="#dc2626", opacity=0.85))

        fig.update_layout(
            barmode="group",
            height=220,
            margin=dict(l=0, r=0, t=10, b=0),
            plot_bgcolor="white", paper_bgcolor="white",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            xaxis=dict(title="Expiration", gridcolor="#f1f5f9"),
            yaxis=dict(title="Volume", gridcolor="#f1f5f9", tickformat=","),
        )
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        st.caption("Near-term spike = directional bet  ·  Far-out dominance = longer-term hedging")
    except Exception as e:
        st.caption(f"Expiry chart unavailable: {e}")


# ── AI Verdict ────────────────────────────────────────────────────────────────

def _render_ai_verdict(data: dict):
    ticker = data.get("ticker", "")
    ai_key = f"of_ai_{ticker}"
    with st.expander(f"🤖 AI Verdict — {ticker}", expanded=False):
        if ai_key in st.session_state:
            st.markdown(_rtl(st.session_state[ai_key]), unsafe_allow_html=True)
            if st.button("🔄 Refresh", key=f"of_ai_refresh_{ticker}"):
                del st.session_state[ai_key]
                st.rerun()
        else:
            if st.button("Generate AI Verdict", key=f"of_ai_btn_{ticker}"):
                with st.spinner("Analyzing options flow…"):
                    from src.options_flow import get_ai_options_verdict
                    verdict = get_ai_options_verdict(ticker, data)
                    st.session_state[ai_key] = verdict
                st.rerun()


# ── Scanner Tab ───────────────────────────────────────────────────────────────

def _render_scanner():
    st.markdown(_html("""
        <div style="background:#fefce8;border:1px solid #fde68a;border-radius:10px;
                    padding:12px 16px;margin-bottom:16px;font-size:13px;color:#92400e;">
          Scans each ticker for unusual options contracts (vol/OI ≥ 3× or vol ≥ 5,000).
          Fetches the nearest 3 expirations per ticker. Keep lists short for speed.
        </div>
    """), unsafe_allow_html=True)

    col1, col2, col3 = st.columns([4, 1, 1])
    with col1:
        tickers_raw = st.text_input(
            "Tickers (comma separated)",
            "AAPL, TSLA, NVDA, AMD, SPY, QQQ, META, AMZN, GOOGL, MSFT",
            key="of_scan_tickers",
        )
    with col2:
        min_vol = st.number_input("Min volume", 100, 10000, 300, step=100, key="of_scan_minvol")
    with col3:
        st.markdown("<br>", unsafe_allow_html=True)
        run_scan = st.button("📡 Scan", key="of_scan_run")

    if run_scan:
        tickers = [t.strip().upper() for t in tickers_raw.split(",") if t.strip()]
        if not tickers:
            st.warning("Enter at least one ticker.")
        else:
            st.markdown(f"**Scanning {len(tickers)} tickers…**")
            progress_bar = st.progress(0)
            status_text  = st.empty()

            def _on_progress(done: int, total: int, ticker: str):
                progress_bar.progress(done / total)
                status_text.caption(f"Scanned {done}/{total} — {ticker}")

            from src.options_flow import scan_unusual_activity
            results = scan_unusual_activity(tickers, min_volume=min_vol,
                                             progress_callback=_on_progress)

            progress_bar.progress(1.0)
            status_text.caption(f"✅ Done — {len(results)} ticker(s) with unusual activity")

            st.session_state["of_scan_results"] = results
            st.session_state["of_scan_ts"]      = datetime.now().strftime("%H:%M:%S")
            # Clear stale AI analysis from previous scan
            st.session_state.pop("of_scan_ai_analyst", None)
            for k in list(st.session_state.keys()):
                if k.startswith("of_ticker_ai_"):
                    del st.session_state[k]

    results = st.session_state.get("of_scan_results")
    if not results:
        return

    st.caption(f"Last scan: {st.session_state.get('of_scan_ts', '')} · {len(results)} ticker(s)")

    # KPI summary
    bullish = sum(1 for r in results if r.get("pcr_vol") and r["pcr_vol"] < 0.7)
    bearish = sum(1 for r in results if r.get("pcr_vol") and r["pcr_vol"] > 1.2)
    call_sw = sum(1 for r in results if r["top_side"] == "CALL")
    put_sw  = sum(1 for r in results if r["top_side"] == "PUT")

    st.markdown(_html(f"""
        <div style="display:flex;gap:10px;margin-bottom:16px;">
          <div style="flex:1;background:linear-gradient(135deg,#064e3b,#059669);
                      border-radius:10px;padding:12px 16px;">
            <div style="font-size:11px;color:#6ee7b7;">Bullish PCR (&lt;0.7)</div>
            <div style="font-size:26px;font-weight:700;color:#fff;">{bullish}</div>
          </div>
          <div style="flex:1;background:linear-gradient(135deg,#7f1d1d,#dc2626);
                      border-radius:10px;padding:12px 16px;">
            <div style="font-size:11px;color:#fca5a5;">Bearish PCR (&gt;1.2)</div>
            <div style="font-size:26px;font-weight:700;color:#fff;">{bearish}</div>
          </div>
          <div style="flex:1;background:linear-gradient(135deg,#1e3a8a,#1d4ed8);
                      border-radius:10px;padding:12px 16px;">
            <div style="font-size:11px;color:#93c5fd;">Top Signal: CALL</div>
            <div style="font-size:26px;font-weight:700;color:#fff;">{call_sw}</div>
          </div>
          <div style="flex:1;background:linear-gradient(135deg,#4c1d95,#7c3aed);
                      border-radius:10px;padding:12px 16px;">
            <div style="font-size:11px;color:#c4b5fd;">Top Signal: PUT</div>
            <div style="font-size:26px;font-weight:700;color:#fff;">{put_sw}</div>
          </div>
        </div>
    """), unsafe_allow_html=True)

    _render_scan_table(results)
    st.markdown("---")
    _render_scan_ai_analyst(results)
    st.markdown("---")
    _render_per_ticker_ai(results)


def _render_scan_table(results: list):
    rows_html = ""
    for r in results:
        pcr = r.get("pcr_vol")
        if pcr is None:
            pcr_str, pcr_color = "N/A", "#6b7280"
        elif pcr < 0.7:
            pcr_str, pcr_color = f"{pcr:.2f} 🟢", "#16a34a"
        elif pcr < 1.0:
            pcr_str, pcr_color = f"{pcr:.2f}", "#d97706"
        elif pcr < 1.2:
            pcr_str, pcr_color = f"{pcr:.2f} 🟠", "#f97316"
        else:
            pcr_str, pcr_color = f"{pcr:.2f} 🔴", "#dc2626"

        side_color = "#16a34a" if r["top_side"] == "CALL" else "#dc2626"
        price_str  = f"${r['price']:.2f}" if r.get("price") else "N/A"
        ratio_str  = f"{r['top_vol_oi_ratio']}×" if r["top_vol_oi_ratio"] < 999 else "NEW"

        rows_html += _html(f"""
            <tr>
              <td style="padding:6px 8px;font-weight:700;color:#1e3a8a;">{r['ticker']}</td>
              <td style="padding:6px 8px;">{price_str}</td>
              <td style="padding:6px 8px;color:{pcr_color};font-weight:600;">{pcr_str}</td>
              <td style="padding:6px 8px;">{r['call_vol']:,}</td>
              <td style="padding:6px 8px;">{r['put_vol']:,}</td>
              <td style="padding:6px 8px;font-weight:700;color:#7c3aed;">{r['unusual_count']}</td>
              <td style="padding:6px 8px;font-weight:700;color:{side_color};">{r['top_side']}</td>
              <td style="padding:6px 8px;">${r['top_strike']:.0f}</td>
              <td style="padding:6px 8px;color:#64748b;font-size:11px;">{r['top_expiry']}</td>
              <td style="padding:6px 8px;">{r['top_volume']:,}</td>
              <td style="padding:6px 8px;color:#7c3aed;font-weight:600;">{ratio_str}</td>
              <td style="padding:6px 8px;color:#6b7280;">{r['top_iv']:.1f}%</td>
            </tr>
        """)

    st.markdown(_html(f"""
        <div style="overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <thead style="background:#1e3a8a;color:#bfdbfe;font-size:11px;">
            <tr>
              <th style="padding:6px 8px;text-align:left;">Ticker</th>
              <th style="padding:6px 8px;text-align:left;">Price</th>
              <th style="padding:6px 8px;text-align:left;">PCR</th>
              <th style="padding:6px 8px;text-align:left;">Call Vol</th>
              <th style="padding:6px 8px;text-align:left;">Put Vol</th>
              <th style="padding:6px 8px;text-align:left;">Unusual #</th>
              <th style="padding:6px 8px;text-align:left;">Top Side</th>
              <th style="padding:6px 8px;text-align:left;">Strike</th>
              <th style="padding:6px 8px;text-align:left;">Expiry</th>
              <th style="padding:6px 8px;text-align:left;">Volume</th>
              <th style="padding:6px 8px;text-align:left;">Vol/OI</th>
              <th style="padding:6px 8px;text-align:left;">IV</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        </div>
    """), unsafe_allow_html=True)
    st.caption("Sorted by unusual contract count · Click ticker → Single Ticker tab for full analysis")


# ── Scan AI Analyst ───────────────────────────────────────────────────────────

def _render_scan_ai_analyst(results: list):
    st.markdown("#### 🤖 AI Analyst — What Does This All Mean?")

    ai_key = "of_scan_ai_analyst"
    with st.expander("Analyze all scanned stocks", expanded=False):
        if ai_key in st.session_state:
            st.markdown(_rtl(st.session_state[ai_key]), unsafe_allow_html=True)
            if st.button("🔄 Refresh Analysis", key="of_scan_ai_refresh"):
                del st.session_state[ai_key]
                st.rerun()
        else:
            st.markdown(_html("""
                <div style="font-size:13px;color:#64748b;margin-bottom:8px;">
                  The AI Analyst reads <strong>all scanned tickers together</strong> and explains:
                  what the collective PCR tells you, what each unusual contract implies,
                  what high IV signals, and what smart money may be anticipating.
                </div>
            """), unsafe_allow_html=True)
            if st.button("🤖 Analyze All Results", key="of_scan_ai_run"):
                with st.spinner("Reading the options tape…"):
                    from src.options_flow import get_ai_scan_analyst
                    analysis = get_ai_scan_analyst(results)
                    st.session_state[ai_key] = analysis
                st.rerun()


def _render_per_ticker_ai(results: list):
    st.markdown("#### 🔎 Per-Ticker AI Breakdown")
    st.caption("Expand any ticker to get a plain-English explanation of its options positioning.")

    for r in results:
        ticker  = r["ticker"]
        ai_key  = f"of_ticker_ai_{ticker}"
        pcr     = r.get("pcr_vol")
        pcr_str = f"{pcr:.2f}" if pcr is not None else "N/A"
        side    = r["top_side"]
        side_color = "#16a34a" if side == "CALL" else "#dc2626"
        unusual = r["unusual_count"]

        label = f"{ticker}  ·  PCR {pcr_str}  ·  {unusual} unusual  ·  top: {side} ${r['top_strike']:.0f}"

        with st.expander(label, expanded=False):
            if ai_key in st.session_state:
                st.markdown(_rtl(st.session_state[ai_key]), unsafe_allow_html=True)
                if st.button("🔄 Refresh", key=f"of_ticker_ai_refresh_{ticker}"):
                    del st.session_state[ai_key]
                    st.rerun()
            else:
                # Show a quick data card before the generate button
                price_str = f"${r['price']:.2f}" if r.get("price") else "N/A"
                ratio_str = f"{r['top_vol_oi_ratio']}×" if r["top_vol_oi_ratio"] < 999 else "NEW"
                st.markdown(_html(f"""
                    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px;">
                      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                                  padding:8px;text-align:center;">
                        <div style="font-size:10px;color:#64748b;">Price</div>
                        <div style="font-size:16px;font-weight:700;color:#0f172a;">{price_str}</div>
                      </div>
                      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                                  padding:8px;text-align:center;">
                        <div style="font-size:10px;color:#64748b;">Put/Call Ratio</div>
                        <div style="font-size:16px;font-weight:700;color:#0f172a;">{pcr_str}</div>
                      </div>
                      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                                  padding:8px;text-align:center;">
                        <div style="font-size:10px;color:#64748b;">Top Contract</div>
                        <div style="font-size:15px;font-weight:700;color:{side_color};">
                          {side} ${r['top_strike']:.0f}
                        </div>
                      </div>
                      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                                  padding:8px;text-align:center;">
                        <div style="font-size:10px;color:#64748b;">Vol/OI · IV</div>
                        <div style="font-size:15px;font-weight:700;color:#7c3aed;">
                          {ratio_str} · {r['top_iv']:.1f}%
                        </div>
                      </div>
                    </div>
                """), unsafe_allow_html=True)

                if st.button(f"🤖 Explain {ticker}", key=f"of_ticker_ai_btn_{ticker}"):
                    with st.spinner(f"Analyzing {ticker}…"):
                        from src.options_flow import get_ai_ticker_quick_verdict
                        verdict = get_ai_ticker_quick_verdict(r)
                        st.session_state[ai_key] = verdict
                    st.rerun()


# ── Legend ────────────────────────────────────────────────────────────────────

def _render_legend():
    st.markdown("#### How to read Options Flow")
    st.markdown(_html("""
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;
                    padding:16px;margin-top:8px;">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
            <div>
              <div style="font-size:12px;font-weight:700;color:#374151;margin-bottom:6px;">
                Put/Call Ratio (PCR)
              </div>
              <div style="font-size:13px;color:#6b7280;line-height:2;">
                🟢 <strong>&lt; 0.7</strong> — Bullish (calls dominate)<br>
                🟡 <strong>0.7–1.0</strong> — Neutral<br>
                🟠 <strong>1.0–1.2</strong> — Cautious / mixed<br>
                🔴 <strong>&gt; 1.2</strong> — Bearish / defensive hedging
              </div>
            </div>
            <div>
              <div style="font-size:12px;font-weight:700;color:#374151;margin-bottom:6px;">
                Unusual Activity
              </div>
              <div style="font-size:13px;color:#6b7280;line-height:2;">
                Vol/OI ≥ 3× → new position (not a roll)<br>
                "NEW" → no prior OI (opening sweep)<br>
                High IV + high vol → directional bet<br>
                Far-dated + high vol → longer-term view
              </div>
            </div>
          </div>
          <div style="margin-top:10px;font-size:12px;color:#94a3b8;
                      border-top:1px solid #e2e8f0;padding-top:8px;">
            Data via yfinance · 5-min cache · Estimated premium = vol × mid × 100 shares
          </div>
        </div>
    """), unsafe_allow_html=True)
