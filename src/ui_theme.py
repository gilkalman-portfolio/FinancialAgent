"""
UI Theme - Design System
All colors, CSS, and reusable HTML components in one place.
Import this in every page module.
"""

# ── Color Tokens ───────────────────────────────────────────────────────────────
PRIMARY       = "#1d4ed8"
PRIMARY_DARK  = "#1e3a8a"
SUCCESS       = "#16a34a"
WARNING       = "#d97706"
DANGER        = "#dc2626"
NEUTRAL       = "#6b7280"
BG_LIGHT      = "#f8fafc"
BORDER        = "#e2e8f0"
TEXT_PRIMARY  = "#0f172a"
TEXT_MUTED    = "#64748b"

# ── Global CSS ─────────────────────────────────────────────────────────────────
GLOBAL_CSS = """
<style>
/* ── Sidebar ─────────────────────────────────────────────────────────────── */
[data-testid="stSidebar"] { background: #1a56db; }
[data-testid="stSidebar"] * { color: #ffffff !important; }
[data-testid="stSidebar"] .stButton > button {
    background: rgba(255,255,255,0.2) !important;
    border: 1px solid rgba(255,255,255,0.4) !important;
    color: white !important; width: 100%; font-weight: 500;
}
[data-testid="stSidebar"] .stButton > button:hover { background: rgba(255,255,255,0.35) !important; }
[data-testid="stSidebar"] .stRadio label {
    color: #e0eaff !important; font-size: 15px;
    padding: 6px 10px; border-radius: 6px; transition: background 0.15s;
}
[data-testid="stSidebar"] .stRadio label:hover { background: rgba(255,255,255,0.15) !important; }
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
    font-size: 11px !important; color: #bfdbfe !important;
    text-transform: uppercase; letter-spacing: 1px; margin: 12px 0 4px 0;
}

/* ── Buttons ─────────────────────────────────────────────────────────────── */
.stButton > button {
    border-radius: 8px !important; font-weight: 600 !important;
    transition: all 0.2s !important;
}
.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #1d4ed8, #1e40af) !important;
    border: none !important; color: white !important;
    padding: 10px 20px !important; font-size: 14px !important;
}
.stButton > button[kind="primary"]:hover {
    background: linear-gradient(135deg, #1e40af, #1e3a8a) !important;
    box-shadow: 0 4px 12px rgba(29,78,216,0.4) !important;
    transform: translateY(-1px) !important;
}

/* ── Typography ──────────────────────────────────────────────────────────── */
h3 { font-size: 24px !important; font-weight: 700 !important; color: #0f172a !important; margin-bottom: 16px !important; }
h4 { font-size: 18px !important; font-weight: 600 !important; color: #1e293b !important; margin-bottom: 10px !important; }

/* ── Metric cards ────────────────────────────────────────────────────────── */
.metric-row { display: flex; gap: 12px; margin-bottom: 1.5rem; }
.metric-card {
    flex: 1; background: linear-gradient(135deg, #1e3a8a 0%, #1d4ed8 100%);
    border-radius: 10px; padding: 16px 18px; border: none;
    box-shadow: 0 2px 8px rgba(29,78,216,0.2);
}
.metric-num { font-size: 30px; font-weight: 600; color: #ffffff; }
.metric-lbl { font-size: 12px; color: #93c5fd; margin-top: 3px; }

/* ── Score color cells ───────────────────────────────────────────────────── */
.score-strong  { color: #059669 !important; font-weight: 700 !important; }
.score-buy     { color: #1d4ed8 !important; font-weight: 700 !important; }
.score-watch   { color: #d97706 !important; font-weight: 700 !important; }
.score-neutral { color: #6b7280 !important; font-weight: 600 !important; }
.score-skip    { color: #dc2626 !important; font-weight: 600 !important; }

/* ── Signal badges ───────────────────────────────────────────────────────── */
.badge-buy     { background: #16a34a; color: #fff; padding: 3px 10px; border-radius: 5px; font-size: 12px; font-weight: 600; }
.badge-watch   { background: #d97706; color: #fff; padding: 3px 10px; border-radius: 5px; font-size: 12px; font-weight: 600; }
.badge-neutral { background: #6b7280; color: #fff; padding: 3px 10px; border-radius: 5px; font-size: 12px; font-weight: 600; }
.badge-strong  { background: #059669; color: #fff; padding: 3px 10px; border-radius: 5px; font-size: 12px; font-weight: 600; }
.badge-skip    { background: #dc2626; color: #fff; padding: 3px 10px; border-radius: 5px; font-size: 12px; font-weight: 600; }

/* ── Tables ──────────────────────────────────────────────────────────────── */
table { width: 100%; border-collapse: collapse; font-size: 14px; }
thead tr { background: #1e3a8a; color: #bfdbfe; font-size: 12px; font-weight: 500; }
thead th { text-align: left; padding: 10px 8px; }
tbody tr:nth-child(even) { background: #eff6ff; }
tbody tr:hover { background: #dbeafe; transition: background 0.1s; }
tbody td { padding: 9px 8px; color: #0f172a; border-bottom: 1px solid #e2e8f0; font-weight: 500; }
td.fc-cell { color: #94a3b8 !important; font-style: italic; }

/* ── Alerts / RTL ────────────────────────────────────────────────────────── */
.stAlert p { direction: rtl; text-align: right; font-size: 15px; line-height: 1.8; }

/* ── Tooltips ────────────────────────────────────────────────────────────── */
.tooltip-term { border-bottom: 2px dashed #93c5fd; cursor: help; color: #ffffff; font-weight: 600; }
.tooltip-wrap { position: relative; display: inline-block; }
.tooltip-wrap:hover .tooltip-box { display: block; }
.tooltip-box {
    display: none; position: absolute; top: 100%; right: 0;
    background: #1e3a8a; color: #e0eaff; padding: 10px 14px;
    border-radius: 8px; font-size: 13px; max-width: 320px; min-width: 200px;
    z-index: 9999; line-height: 1.6; text-align: right; direction: rtl;
    border: 1px solid #3b82f6; white-space: normal; font-weight: 400;
    box-shadow: 0 4px 16px rgba(0,0,0,0.2);
}

/* ── Section card ────────────────────────────────────────────────────────── */
.section-card {
    background: #ffffff; border: 3px solid #000000;
    border-radius: 16px; padding: 16px 18px; margin-bottom: 16px;
}
.section-label {
    font-size: 11px; font-weight: 700; letter-spacing: 1.5px;
    color: #94a3b8; text-transform: uppercase; margin-bottom: 8px;
}

/* ── Progress bar ────────────────────────────────────────────────────────── */
.stProgress > div > div > div { background: linear-gradient(90deg, #1d4ed8, #059669) !important; border-radius: 4px; }

/* ── Danger buttons (Remove) ─────────────────────────────────────────────── */
.btn-danger > div > button,
.btn-danger button {
    background: transparent !important;
    border: 1.5px solid #dc2626 !important;
    color: #dc2626 !important;
}
.btn-danger > div > button:hover,
.btn-danger button:hover {
    background: #dc2626 !important;
    color: white !important;
    box-shadow: 0 2px 8px rgba(220,38,38,0.3) !important;
}

/* ── Primary action buttons ──────────────────────────────────────────────── */
.btn-primary > div > button,
.btn-primary button {
    background: linear-gradient(135deg, #1d4ed8, #1e40af) !important;
    border: none !important;
    color: white !important;
    font-weight: 600 !important;
}
.btn-primary > div > button:hover,
.btn-primary button:hover {
    background: linear-gradient(135deg, #1e40af, #1e3a8a) !important;
    box-shadow: 0 4px 12px rgba(29,78,216,0.4) !important;
    transform: translateY(-1px) !important;
}

/* ── Misc ────────────────────────────────────────────────────────────────── */
.scan-info-box {
    background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 8px;
    padding: 10px 14px; margin-bottom: 12px; font-size: 13px; color: #0369a1;
}

/* ── Inputs / Selects ────────────────────────────────────────────────────── */
.stTextInput > div > div > input, .stNumberInput > div > div > input {
    border-radius: 8px !important; border: 1.5px solid #e2e8f0 !important;
}
.stTextInput > div > div > input:focus, .stNumberInput > div > div > input:focus {
    border-color: #1d4ed8 !important; box-shadow: 0 0 0 3px rgba(29,78,216,0.1) !important;
}

/* ── Tabs ────────────────────────────────────────────────────────────────── */
.stTabs [data-baseweb="tab"] { font-weight: 600; font-size: 14px; }
.stTabs [aria-selected="true"] { color: #1d4ed8 !important; border-bottom-color: #1d4ed8 !important; }
</style>
"""

TOOLTIPS = {
    "RSI":              "Relative Strength Index (0-100). Below 30 = oversold, above 70 = overbought. Sweet spot: 40-65.",
    "MACD":             "Moving Average Convergence Divergence. Bullish = MACD line above signal line.",
    "MA Trend":         "Moving average trend. Strong uptrend = price above SMA20/50/200.",
    "SI% Float":        "Short Interest as % of Float (shares available to trade). Above 15% = squeeze potential. Above 20% + volume spike = active squeeze risk. Source: yfinance shortPercentOfFloat.",
    "FC%":              "30-day price forecast (ARIMA/MA/EXP) - indicative only, not part of score.",
    "Score":            "Score 0-100: RSI(15) + MACD(15) + MA(20) + Volume(10) + Momentum(10) + Short Interest(10) + Institutional(5) + Insider(5) + Fundamentals(10) + Trends bonus.",
    "Margin of Safety": "(Intrinsic Value − Current Price) ÷ Intrinsic Value × 100. Positive = stock is trading BELOW its DCF value (undervalued). Negative = stock is trading ABOVE intrinsic value (overvalued). ≥20% is considered a safe entry buffer.",
    "Intrinsic Value":  "Estimated fair value per share from DCF model: sum of 5 years of discounted free cash flows + terminal value, divided by shares outstanding.",
    "WACC":             "Weighted Average Cost of Capital — the discount rate applied to future cash flows. Higher WACC = higher risk = lower intrinsic value. Derived from 10% base + leverage adjustment.",
    "Days to Cover":    "Shares sold short ÷ average daily volume. How many trading days it would take all shorts to buy back their shares. ≥5 days = trapped shorts, ≥10 = severe pressure.",
    "SI% of Float":     "Short Interest as % of float shares. ≥15% = elevated, ≥20% = squeeze zone, ≥50% = extreme. High SI% means many traders are betting the stock falls.",
    "Est. Borrow Fee":  "Estimated annualised cost to borrow shares for short selling. Approximated from SI% (Finviz data). ≥20% = high demand to borrow = strong short pressure confirmed.",
    "Vol Ratio":        "Recent 5-day average volume ÷ 30-day average volume. ≥2.0x = significant spike. Volume ignition with rising price is the classic squeeze trigger signal.",
    "DCF / MoS":        "DCF Intrinsic Value (fair value per share) and Margin of Safety. MoS = (Intrinsic − Price) ÷ Intrinsic × 100. Green ≥20% = undervalued, Red = overvalued. N/A for loss-making companies.",
    "Signal":           "Trading signal based on composite score: ≥75 = STRONG BUY · 60–74 = BUY · 45–59 = WATCH · 35–44 = NEUTRAL · <35 = SKIP.",
}

# ── Reusable HTML Components ───────────────────────────────────────────────────

def badge(sig: str) -> str:
    m = {"STRONG BUY": "strong", "BUY": "buy", "WATCH": "watch", "NEUTRAL": "neutral", "SKIP": "skip"}
    return f'<span class="badge-{m.get(sig, "neutral")}">{sig}</span>'


def score_cell(score: float) -> str:
    if score >= 75:   css = "score-strong"
    elif score >= 60: css = "score-buy"
    elif score >= 45: css = "score-watch"
    elif score >= 35: css = "score-neutral"
    else:             css = "score-skip"
    return f'<span class="{css}">{score:.1f}</span>'


def tooltip(term: str) -> str:
    tip = TOOLTIPS.get(term, "")
    return (f'<span class="tooltip-wrap"><span class="tooltip-term">{term}</span>'
            f'<div class="tooltip-box">{tip}</div></span>')


def metric_cards_html(**kwargs) -> str:
    cards = "".join(
        f'<div class="metric-card"><div class="metric-num">{v}</div><div class="metric-lbl">{k}</div></div>'
        for k, v in kwargs.items()
    )
    return f'<div class="metric-row">{cards}</div>'


def section_card(content_html: str, border_color: str = "#000000", radius: int = 16) -> str:
    return (f'<div style="border:3px solid {border_color};border-radius:{radius}px;'
            f'padding:14px 16px;background:#ffffff;">{content_html}</div>')


def dark_card(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div style="font-size:12px;color:#93c5fd;margin-top:2px;">{sub}</div>' if sub else ""
    return f"""<div style="flex:1;background:#1e3a8a;border-radius:8px;padding:12px 14px;">
      <div style="font-size:12px;color:#93c5fd;margin-bottom:4px;">{label}</div>
      <div style="font-size:22px;font-weight:700;color:#fff;">{value}</div>
      {sub_html}
    </div>"""
