"""
DCF card helper — imported by page_research.py _render_ticker_card
Call at the end of _render_ticker_card:
    from _pages_modules.page_research_dcf_append import render_dcf_card
    render_dcf_card(ticker, r)
"""
import streamlit as st


def render_dcf_card(ticker: str, r: dict):
    if not r:
        return
    dcf = r.get("dcf")
    if not dcf:
        return   # silently skip — no cash flow data

    mos   = dcf.get("margin_of_safety", 0)
    iv    = dcf.get("intrinsic_value", 0)
    price = dcf.get("current_price", 0)

    if mos >= 20:    lc = "#16a34a"
    elif mos >= 0:   lc = "#d97706"
    elif mos >= -20: lc = "#f97316"
    else:            lc = "#dc2626"

    bar_pct = min(max(price / iv * 100, 2), 98) if iv > 0 else 50

    st.markdown(f"""<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:14px 18px;margin:8px 0;">
      <div style="font-size:12px;font-weight:600;color:#64748b;margin-bottom:10px;">📊 DCF VALUATION — 5 Year Horizon</div>
      <div style="display:flex;gap:12px;margin-bottom:12px;">
        <div style="flex:1;background:white;border:1px solid #e2e8f0;border-radius:8px;padding:10px;text-align:center;">
          <div style="font-size:11px;color:#64748b;">Intrinsic Value</div>
          <div style="font-size:22px;font-weight:700;color:#1e3a8a;">${iv:.2f}</div>
        </div>
        <div style="flex:1.5;background:#1e3a8a;border-radius:8px;padding:10px;text-align:center;">
          <div style="font-size:11px;color:#93c5fd;">Margin of Safety</div>
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
        Growth {dcf.get('growth_rate_used',0):.1f}% · WACC {dcf.get('wacc_used',0):.1f}% · Terminal 2.5% · FCF(TTM) ${dcf.get('fcf_ttm',0):.0f}M
      </div>
    </div>""", unsafe_allow_html=True)
