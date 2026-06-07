"""
UI Design Constants — single source of truth for all pages.
Import and use these instead of hardcoding colors/sizes inline.
"""

# ── Colors ─────────────────────────────────────────────────────────────────────
GREEN        = "#16a34a"   # positive / bullish
RED          = "#dc2626"   # negative / bearish
ORANGE       = "#f97316"   # warning / somewhat-bearish
AMBER        = "#d97706"   # caution
BLUE_DARK    = "#1e3a8a"   # primary accent (card headers, badges)
BLUE_MID     = "#1d4ed8"   # links, highlights
BLUE_LIGHT   = "#93c5fd"   # subtext on dark backgrounds
GRAY_TEXT    = "#374151"   # primary text
GRAY_SUB     = "#64748b"   # secondary / label text
GRAY_MUTED   = "#94a3b8"   # muted / caption text
BORDER       = "#e2e8f0"   # card borders
BG_PAGE      = "#f8fafc"   # page background tint
BG_CARD      = "#ffffff"   # card background

# ── Card style (copy-paste into f-string) ──────────────────────────────────────
CARD_STYLE   = f"background:{BG_CARD};border:1px solid {BORDER};border-radius:10px;padding:12px 14px;"

# ── Font sizes ─────────────────────────────────────────────────────────────────
FS_LABEL     = "11px"   # small label above value
FS_CAPTION   = "11px"   # caption / muted text
FS_BODY      = "13px"   # regular body text
FS_VALUE     = "18px"   # metric value (indices, prices)
FS_VALUE_LG  = "22px"   # large metric (score cards)
FS_CHANGE    = "13px"   # % change line

# ── Helpers ────────────────────────────────────────────────────────────────────
def change_color(value: float) -> str:
    """Return green or red based on sign."""
    return GREEN if value >= 0 else RED

def sentiment_color(sentiment: str) -> str:
    mapping = {
        "Bullish":          GREEN,
        "Somewhat-Bullish": "#4ade80",
        "Neutral":          GRAY_MUTED,
        "Somewhat-Bearish": ORANGE,
        "Bearish":          RED,
    }
    return mapping.get(sentiment, GRAY_MUTED)
