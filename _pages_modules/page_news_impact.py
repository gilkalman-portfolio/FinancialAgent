"""Page: News Impact"""
import html as _html_mod
import streamlit as st
from src.stock_scorer import score_stock, signal_label


def _html(raw: str) -> str:
    return " ".join(raw.split())


def render():
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=20 * 60 * 1000, key="news_autorefresh")  # 20 min

    from src.news_impact_analyzer import run_full_analysis, get_ticker_news

    st.markdown("### News Impact Analysis")
    st.markdown(_html("""<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px 16px;margin-bottom:16px;">
      <div style="font-size:12px;font-weight:600;color:#374151;margin-bottom:8px;">Detection layers:</div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;">
        <span style="background:#1e3a8a;color:white;padding:4px 12px;border-radius:5px;font-size:13px;">Direct mention</span>
        <span style="background:#6d28d9;color:white;padding:4px 12px;border-radius:5px;font-size:13px;">Competitor</span>
        <span style="background:#0891b2;color:white;padding:4px 12px;border-radius:5px;font-size:13px;">Supply Chain</span>
        <span style="background:#b45309;color:white;padding:4px 12px;border-radius:5px;font-size:13px;">Macro Signal</span>
        <span style="background:#6b7280;color:white;padding:4px 12px;border-radius:5px;font-size:13px;">Same Sector</span>
      </div>
    </div>"""), unsafe_allow_html=True)

    tab1, tab2, tab3 = st.tabs(["Article Analysis", "Stock News", "📅 Upcoming Events"])

    # ── Tab 1: Article Analysis ────────────────────────────────────────────────
    with tab1:
        input_type   = st.radio("Input type", ["URL", "Free text"], horizontal=True, key="news_input_type")
        article_text = ""

        if input_type == "URL":
            url_input = st.text_input("Article URL", placeholder="https://...", key="url_input")
            if url_input:
                with st.spinner("Loading article..."):
                    try:
                        import requests
                        from html.parser import HTMLParser
                        from loguru import logger as _log
                        class _TE(HTMLParser):
                            def __init__(self): super().__init__(); self.parts=[]; self._skip=False
                            def handle_starttag(self,t,a):
                                if t in ("script","style","nav","header","footer","aside","form"): self._skip=True
                            def handle_endtag(self,t):
                                if t in ("script","style","nav","header","footer","aside","form"): self._skip=False
                            def handle_data(self,d):
                                if not self._skip and d.strip(): self.parts.append(d.strip())
                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.5",
                        }
                        r2 = requests.get(url_input, headers=headers, timeout=10, allow_redirects=True)
                        _log.info(f"[NewsImpact] URL fetch: status={r2.status_code} final_url={r2.url} content_length={len(r2.text)}")
                        p  = _TE(); p.feed(r2.text)
                        raw_parts = " ".join(p.parts)
                        article_text = raw_parts[:6000]
                        char_count = len(article_text.strip())
                        _log.info(f"[NewsImpact] Extracted text: {char_count} chars, parts={len(p.parts)}, status={r2.status_code}")

                        # Paywall/redirect detection
                        paywall_hints = ["subscribe", "sign in", "log in", "create an account", "paywall", "premium content", "access denied"]
                        text_lower = article_text.lower()
                        likely_paywall = any(h in text_lower for h in paywall_hints) and char_count < 1000
                        if likely_paywall:
                            _log.warning(f"[NewsImpact] Paywall detected for {url_input}")
                            st.warning(f"⚠️ Paywall detected — only {char_count} chars loaded. Try pasting the article text manually.")
                        elif char_count < 200:
                            st.warning(f"⚠️ Loaded text is too short ({char_count} chars). Possible paywall or JS-rendered page.")
                        else:
                            st.success(f"Loaded {char_count} chars")
                    except Exception as e:
                        _log.error(f"[NewsImpact] URL fetch error: {e}")
                        st.error(f"Error: {e}")
        else:
            article_text = st.text_area("Paste article text", height=200, key="article_textarea")

        if st.button("Analyze Impact", key="analyze_btn") and article_text.strip():
            if len(article_text.strip()) < 200:
                st.warning(f"⚠️ Loaded text is too short ({len(article_text.strip())} chars). Possible paywall.")
            else:
                with st.spinner("Analyzing..."):
                    from loguru import logger as _log
                    _log.info(f"[NewsImpact] Starting analysis, text length={len(article_text.strip())}")
                    analysis = run_full_analysis(article_text)
                    _log.info(f"[NewsImpact] Analysis complete: error={analysis.get('error')}, affected={len(analysis.get('affected', []))}")
                st.session_state["news_analysis"] = analysis

        analysis = st.session_state.get("news_analysis")
        if analysis:
            _render_analysis(analysis)

    # ── Tab 2: Stock News ──────────────────────────────────────────────────────
    with tab2:
        ticker_news = st.text_input("Ticker", placeholder="AAPL, TSLA, NVDA...", key="news_ticker").upper().strip()
        days_news   = st.slider("Days back", 1, 30, 1, key="news_days")

        if ticker_news:
            news_key = f"stock_news_{ticker_news}_{days_news}"
            if news_key not in st.session_state or st.button("🔄 Refresh news", key="news_refresh"):
                with st.spinner(f"Loading news for {ticker_news}..."):
                    st.session_state[news_key] = get_ticker_news(ticker_news, days=days_news)

            articles = st.session_state[news_key]
            if not articles:
                st.info("No news found.")
            else:
                pos = sum(1 for a in articles if a['sentiment'] == 'positive')
                neg = sum(1 for a in articles if a['sentiment'] == 'negative')
                neu = len(articles) - pos - neg
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Total", len(articles))
                c2.metric("Positive", pos)
                c3.metric("Negative", neg)
                c4.metric("Neutral",  neu)
                st.markdown("---")
                for a in articles:
                    s_color = "#16a34a" if a['sentiment']=='positive' else "#dc2626" if a['sentiment']=='negative' else "#d97706"
                    s_label = "↑" if a['sentiment']=='positive' else "↓" if a['sentiment']=='negative' else "→"
                    age_str = ""
                    if a.get("ts") and a["ts"] > 0:
                        from datetime import datetime as _dt
                        hours_ago = ((_dt.now().timestamp() - a["ts"]) / 3600)
                        if hours_ago < 1:
                            age_str = f"{int(hours_ago*60)}m ago"
                        elif hours_ago < 24:
                            age_str = f"{int(hours_ago)}h ago"
                        else:
                            age_str = f"{int(hours_ago/24)}d ago"
                    _origin = _html_mod.escape(str(a.get("origin", "")))
                    origin_badge = f'<span style="font-size:10px;background:#e2e8f0;color:#475569;padding:1px 5px;border-radius:3px;margin-left:4px;">{_origin}</span>' if a.get("origin") else ""
                    _url = a.get("url", "") or ""
                    if not _url.startswith(("http://", "https://")):
                        _url = "#"
                    _headline = _html_mod.escape(str(a.get("headline", "")))
                    _source   = _html_mod.escape(str(a.get("source", "")))
                    _published = _html_mod.escape(str(a.get("published", "")))
                    st.markdown(_html(f"""<div style="border-left:4px solid {s_color};padding:10px 14px;margin-bottom:8px;background:#f8fafc;border-radius:0 6px 6px 0;">
                      <div style="display:flex;justify-content:space-between;">
                        <a href="{_html_mod.escape(_url)}" target="_blank" style="font-size:14px;font-weight:600;color:#1e293b;text-decoration:none;">{_headline}</a>
                        <span style="color:{s_color};font-weight:700;margin-left:12px;">{s_label}</span>
                      </div>
                      <div style="font-size:12px;color:#6b7280;margin-top:4px;">{_source} · {_published} <span style="color:#94a3b8;">{age_str}</span>{origin_badge}</div>
                    </div>"""), unsafe_allow_html=True)

    # ── Tab 3: Upcoming Events ─────────────────────────────────────────────────
    with tab3:
        _render_upcoming_events()


def _render_upcoming_events():
    """Macro events + earnings calendar for the next 7 days."""
    st.markdown("#### 📅 Upcoming Market Events")

    events_key = "upcoming_events_data"
    import time as _time
    now_ts    = _time.time()
    cached_ts = st.session_state.get("upcoming_events_ts", 0)

    if now_ts - cached_ts > 600 or events_key not in st.session_state:
        with st.spinner("Loading events..."):
            from src.market_feed import get_upcoming_macro, get_earnings_calendar
            macro    = get_upcoming_macro(7)
            earnings = get_earnings_calendar(7)
        st.session_state[events_key]          = (macro, earnings)
        st.session_state["upcoming_events_ts"] = now_ts
    else:
        macro, earnings = st.session_state[events_key]

    col_refresh = st.columns([1, 5])[0]
    with col_refresh:
        if st.button("🔄 Refresh", key="events_refresh"):
            st.session_state.pop(events_key, None)
            st.session_state.pop("upcoming_events_ts", None)
            st.rerun()

    left, right = st.columns(2)

    # ── Macro Events ──────────────────────────────────────────────────────────
    with left:
        st.markdown("#### 🏛️ Macro Events — Next 7 Days")
        if macro:
            for m in macro:
                impact_colors = {"high": "#dc2626", "medium": "#d97706", "low": "#6b7280"}
                color = impact_colors.get(m.get("impact", "low"), "#6b7280")
                st.markdown(_html(f"""<div style="border-left:4px solid {color};padding:10px 14px;margin-bottom:8px;background:#f8fafc;border-radius:0 8px 8px 0;">
                  <div style="display:flex;justify-content:space-between;align-items:center;">
                    <div>
                      <span style="font-weight:700;font-size:14px;color:#0f172a;">{m['name']}</span>
                      <span style="font-size:11px;background:{color};color:white;padding:1px 8px;border-radius:4px;margin-left:8px;">{m.get('impact','').upper()}</span>
                    </div>
                    <div style="text-align:right;font-size:12px;color:#6b7280;">
                      <div>{m['date']}</div>
                      <div>{m.get('time','')}</div>
                    </div>
                  </div>
                </div>"""), unsafe_allow_html=True)
        else:
            st.info("No macro events found for next 7 days.")

    # ── Earnings Calendar ─────────────────────────────────────────────────────
    with right:
        st.markdown("#### 📊 Earnings — This Week")
        if earnings:
            from itertools import groupby
            sorted_earnings = sorted(earnings, key=lambda x: x["date"])
            for date_label, group in groupby(sorted_earnings, key=lambda x: x["date"]):
                items = list(group)
                st.markdown(f"**{date_label}**")
                rows_html = ""
                for e in items[:10]:
                    eps = f"EPS est. {e['estimate']}" if e.get("estimate") else ""
                    time_color = "#1d4ed8" if "Pre" in e.get("time","") else "#d97706" if "After" in e.get("time","") else "#6b7280"
                    rows_html += f"""<tr>
                      <td style="font-weight:700;font-size:13px;padding:6px 8px;">{e['symbol']}</td>
                      <td style="font-size:12px;color:#374151;padding:6px 8px;">{e['name'][:18]}</td>
                      <td style="font-size:11px;color:{time_color};font-weight:600;padding:6px 8px;">{e.get('time','')}</td>
                      <td style="font-size:11px;color:#6b7280;padding:6px 8px;">{e.get('mcap','')}</td>
                      <td style="font-size:11px;color:#16a34a;padding:6px 8px;">{eps}</td>
                    </tr>"""
                st.markdown(_html(f"""<div style="border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;margin-bottom:12px;">
                <table style="width:100%;border-collapse:collapse;background:#fff;">
                  <thead><tr style="background:#f8fafc;border-bottom:1px solid #e2e8f0;">
                    <th style="text-align:left;padding:6px 8px;font-size:11px;color:#6b7280;">Symbol</th>
                    <th style="text-align:left;padding:6px 8px;font-size:11px;color:#6b7280;">Name</th>
                    <th style="text-align:left;padding:6px 8px;font-size:11px;color:#6b7280;">When</th>
                    <th style="text-align:left;padding:6px 8px;font-size:11px;color:#6b7280;">Cap</th>
                    <th style="text-align:left;padding:6px 8px;font-size:11px;color:#6b7280;">EPS est.</th>
                  </tr></thead><tbody>{rows_html}</tbody>
                </table></div>"""), unsafe_allow_html=True)
        else:
            st.info("No earnings data available.")


def _sanitize(text: str) -> str:
    """Strip any HTML tags the LLM might have included in text fields."""
    import re
    return re.sub(r'<[^>]+>', '', str(text)).strip()


def _render_analysis(analysis: dict):
    if analysis.get('error'):
        st.error(analysis['error'])
        return

    sent_color   = "#16a34a" if analysis['sentiment']=='positive' else "#dc2626" if analysis['sentiment']=='negative' else "#d97706"
    sent_label   = "Positive" if analysis['sentiment']=='positive' else "Negative" if analysis['sentiment']=='negative' else "Neutral"
    surprise_badge = '<span style="background:#f59e0b;color:white;padding:3px 10px;border-radius:5px;font-size:12px;margin-left:8px;">⚡ Surprise</span>' if analysis.get('surprise') else ''
    summary = _sanitize(analysis.get('summary', ''))

    st.markdown(_html(f"""<div style="background:#1e3a8a;border-radius:8px;padding:14px 18px;margin-bottom:1.2rem;">
      <div style="display:flex;justify-content:space-between;margin-bottom:8px;">
        <div style="font-size:12px;color:#93c5fd;">Summary</div>
        <div>
          <span style="background:{sent_color};color:white;padding:3px 10px;border-radius:5px;font-size:12px;">{sent_label}</span>
          {surprise_badge}
        </div>
      </div>
      <div style="font-size:15px;color:#ffffff;line-height:1.8;">{_sanitize(summary)}</div>
    </div>"""), unsafe_allow_html=True)

    macro_signals = analysis.get('macro_signals', [])
    if macro_signals:
        from src.macro_signals import MACRO_CORRELATION_MATRIX
        signals_html = "".join(
            f'<span style="background:#b45309;color:white;padding:3px 10px;border-radius:5px;font-size:12px;margin-right:6px;">'
            f'{_html_mod.escape(str(MACRO_CORRELATION_MATRIX.get(s.get("signal",""), {}).get("description", s.get("signal",""))))}'
            f'{"⚡" if s.get("surprise") else ""}</span>'
            for s in macro_signals
        )
        st.markdown(f'<div style="margin-bottom:12px;"><span style="font-size:12px;font-weight:600;color:#374151;">Macro Signals: </span>{signals_html}</div>', unsafe_allow_html=True)

    affected = analysis.get("affected", [])
    layer_colors = {'direct':'#1e3a8a','competitor':'#6d28d9','supply_chain':'#0891b2','macro':'#b45309','sector':'#6b7280'}
    layer_labels = {'direct':'Direct','competitor':'Competitor','supply_chain':'Supply Chain','macro':'Macro Signal','sector':'Sector'}

    if affected:
        st.markdown("#### Affected Stocks")
        progress = st.progress(0, text="Checking scores...")
        results_data = []
        for i, item in enumerate(affected):
            progress.progress((i+1)/len(affected), text=f"Checking {item.get('ticker','')}...")
            tkr = item.get("ticker","").split(":")[0].split(".")[0].strip().upper()
            if not tkr:
                continue
            score_key = f"ni_score_{tkr}"
            if score_key not in st.session_state:
                tech, price_v = None, None
                try:
                    rv = score_stock(tkr, 30)
                    if rv:
                        tech, price_v = rv["score"], rv["price"]
                except Exception:
                    pass
                st.session_state[score_key] = (tech, price_v)
            tech, price_v = st.session_state[f"ni_score_{tkr}"]
            results_data.append({**item, "tech_score": tech, "price": price_v, "ticker": tkr})
        progress.empty()

        for item in results_data:
            impact  = item.get("impact", "neutral")
            mag     = item.get("magnitude", 1)
            layer   = item.get("layer", "sector")
            color   = "#16a34a" if impact=="positive" else "#dc2626" if impact=="negative" else "#d97706"
            arrow   = "↑" if impact=="positive" else "↓" if impact=="negative" else "→"
            stars   = "●" * mag + "○" * (5 - mag)
            sc_txt  = f"{item['tech_score']:.0f}" if item['tech_score'] else "N/A"
            sc_sig  = f"<span style='font-size:11px;background:#1e3a8a;color:white;padding:2px 7px;border-radius:3px;'>{signal_label(item['tech_score'])}</span>" if item['tech_score'] else ""
            pr_txt  = f"${item['price']:.2f}" if item['price'] else ""
            l_color = layer_colors.get(layer, '#6b7280')
            l_label = layer_labels.get(layer, layer)
            reason  = _sanitize(item.get('reason', ''))
            company = _sanitize(item.get('company', ''))
            st.markdown(_html(f"""<div style="border:1px solid {color};border-right:5px solid {color};border-radius:8px;padding:14px 18px;margin-bottom:10px;direction:rtl;text-align:right;">
              <div style="display:flex;justify-content:space-between;margin-bottom:8px;">
                <div>
                  <span style="font-size:18px;font-weight:700;color:{color};">{arrow} {item.get('ticker','')}</span>
                  <span style="font-size:13px;color:#6b7280;margin-right:8px;">{company}</span>
                  <span style="background:{l_color};color:white;padding:1px 7px;border-radius:4px;font-size:11px;">{l_label}</span>
                </div>
                <div style="direction:ltr;">{sc_sig} <span style="font-weight:600;color:#1e3a8a;">{sc_txt}</span> <span style="color:#6b7280;">{pr_txt}</span></div>
              </div>
              <div style="font-size:14px;color:#1e293b;margin-bottom:6px;">{_sanitize(reason)}</div>
              <div style="font-size:12px;color:{color};">Magnitude: {stars}</div>
            </div>"""), unsafe_allow_html=True)
