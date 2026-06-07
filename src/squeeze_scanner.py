"""
Short Squeeze Scanner
Scores stocks on squeeze potential:

  Component        Weight   Penalty/Bonus
  SI% of Float      50%     —
  Days to Cover     20%     —
  Borrow Fee        20%     None → -20pts penalty | >20% → +15pts bonus
  Volume Ratio      10%     —

Additional analysis:
  - Breakout price, distance, exit target
  - 7-day price + volume sparkline data
  - Critical Alert detection
  - AI Verdict (via LLM)
"""

import yfinance as yf
from src.yf_cache import get_info as _yf_info, get_history as _yf_hist
import pandas as pd
import numpy as np
from typing import Optional, Callable
from loguru import logger

from src.borrow_fee import get_borrow_fee

# ── Weights ────────────────────────────────────────────────────────────────────
W_SI     = 0.50
W_DTC    = 0.20
W_BORROW = 0.20
W_VOLUME = 0.10

BORROW_UNKNOWN_PENALTY = 20
BORROW_HIGH_BONUS      = 15
BORROW_HIGH_THRESHOLD  = 20


# ── Component scorers ──────────────────────────────────────────────────────────

def _score_si(si_pct: float) -> float:
    if si_pct <= 5:   return 0
    if si_pct <= 10:  return 10
    if si_pct <= 20:  return 30
    if si_pct <= 35:  return 55
    if si_pct <= 50:  return 75
    if si_pct <= 75:  return 90
    return 100


def _score_dtc(dtc: float) -> float:
    if dtc <= 1:   return 0
    if dtc <= 2:   return 15
    if dtc <= 3:   return 35
    if dtc <= 5:   return 60
    if dtc <= 8:   return 80
    if dtc <= 12:  return 92
    return 100


def _score_borrow(fee: Optional[float]) -> float:
    if fee is None: return 0
    if fee <= 5:    return 10
    if fee <= 15:   return 35
    if fee <= 25:   return 60
    if fee <= 50:   return 80
    if fee <= 100:  return 92
    return 100


def _score_volume(vol_ratio: float) -> float:
    if vol_ratio <= 0.8:  return 0
    if vol_ratio <= 1.2:  return 15
    if vol_ratio <= 2.0:  return 40
    if vol_ratio <= 3.0:  return 70
    if vol_ratio <= 5.0:  return 88
    return 100


def _label(score: float) -> tuple[str, str]:
    if score >= 80:  return "EXTREME PRESSURE", "#7c3aed"
    if score >= 65:  return "HIGH PRESSURE",    "#dc2626"
    if score >= 50:  return "BUILDING UP",      "#d97706"
    if score >= 35:  return "WATCH",            "#2563eb"
    return "LOW",                               "#6b7280"


def _ignition_label(vol_ratio: float, price_change_2d: float) -> Optional[str]:
    if vol_ratio >= 3.0 and price_change_2d >= 5.0:
        return "🔥 IGNITION STARTING"
    if vol_ratio >= 2.0 and price_change_2d >= 3.0:
        return "⚡ HEATING UP"
    return None


# ── Critical Alert ─────────────────────────────────────────────────────────────

def is_critical_alert(r: dict, all_results: list[dict]) -> bool:
    if r["dist_to_breakout_pct"] >= 5.0:
        return False
    # Absolute floor — SI must be meaningful regardless of relative ranking.
    # Prevents a list of all-low-SI tickers from marking each other as critical.
    if r.get("si_pct", 0) < 10:
        return False

    si_vals     = [x["si_pct"]      for x in all_results if x["si_pct"] > 0]
    dtc_vals    = [x["dtc"]         for x in all_results if x["dtc"] > 0]
    borrow_vals = [x["borrow_fee"]  for x in all_results if x["borrow_fee"] is not None]

    def _top10(val, vals):
        if not vals:
            return False
        return val >= np.percentile(vals, 90)

    si_ok     = _top10(r["si_pct"], si_vals)
    dtc_ok    = _top10(r["dtc"], dtc_vals)
    borrow_ok = _top10(r["borrow_fee"] or 0, borrow_vals) if borrow_vals else True

    return si_ok and dtc_ok and borrow_ok


# ── Main analyzer ──────────────────────────────────────────────────────────────

def analyze_ticker(ticker: str) -> Optional[dict]:
    try:
        info = _yf_info(ticker, ttl=900)  # 15-min cache

        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price or price <= 0:
            return None

        hist = _yf_hist(ticker, period="1y", ttl=1800)  # 30-min cache
        if hist.empty or len(hist) < 10:
            return None

        si_pct       = (info.get("shortPercentOfFloat") or 0) * 100
        dtc          = info.get("shortRatio") or 0
        float_shares = info.get("floatShares") or 0
        market_cap   = info.get("marketCap") or 0

        borrow_fee = get_borrow_fee(ticker)

        vol_30d   = hist["Volume"].rolling(30).mean().iloc[-1]
        vol_5d    = hist["Volume"].iloc[-5:].mean()
        vol_ratio = vol_5d / vol_30d if vol_30d > 0 else 1.0

        # RVOL: today's volume vs 30-day average (confirmed entry signal uses this)
        rvol = hist["Volume"].iloc[-1] / vol_30d if vol_30d > 0 else 1.0

        price_2d_ago    = hist["Close"].iloc[-3] if len(hist) >= 3 else price
        price_change_2d = (price - price_2d_ago) / price_2d_ago * 100

        spark_days = min(7, len(hist))
        spark_hist = hist.iloc[-spark_days:]
        sparkline = {
            "dates":   [d.strftime("%m/%d") for d in spark_hist.index],
            "prices":  [round(float(p), 2) for p in spark_hist["Close"].tolist()],
            "volumes": [int(v) for v in spark_hist["Volume"].tolist()],
        }

        sma200 = None
        if len(hist) >= 200:
            sma200 = hist["Close"].rolling(200).mean().iloc[-1]
        elif len(hist) >= 50:
            sma200 = hist["Close"].rolling(len(hist)).mean().iloc[-1]

        week_high_52 = info.get("fiftyTwoWeekHigh") or hist["High"].max()
        dist_to_52w_pct = (week_high_52 - price) / price * 100

        # Confirmed Entry Signal: SI>20% + DTC>5 + RVOL>3 + price up >5% in last 2 days
        entry_signal = (
            si_pct >= 20.0 and
            dtc >= 5.0 and
            rvol >= 3.0 and
            price_change_2d >= 5.0
        )

        high_60d = hist["High"].iloc[-60:].max() if len(hist) >= 60 else hist["High"].max()

        candidates = [float(high_60d)]
        if sma200 and not np.isnan(float(sma200)):
            candidates.append(float(sma200))
        breakout_price = max(candidates) * 1.01

        dist_to_breakout_pct = (breakout_price - price) / price * 100

        if float_shares > 0:
            float_m   = float_shares / 1e6
            bonus_pct = max(10, min(50, 50 - float_m * 0.8))
        else:
            bonus_pct = 20
        exit_target = breakout_price * (1 + bonus_pct / 100)

        si_s     = _score_si(si_pct)
        dtc_s    = _score_dtc(dtc)
        borrow_s = _score_borrow(borrow_fee)
        vol_s    = _score_volume(vol_ratio)

        raw = si_s * W_SI + dtc_s * W_DTC + borrow_s * W_BORROW + vol_s * W_VOLUME

        if borrow_fee is None:
            raw -= BORROW_UNKNOWN_PENALTY
        elif borrow_fee >= BORROW_HIGH_THRESHOLD:
            raw += BORROW_HIGH_BONUS

        score = round(min(max(raw, 0), 100), 1)
        label, label_color = _label(score)
        ignition           = _ignition_label(vol_ratio, price_change_2d)

        return {
            "ticker":               ticker,
            "price":                round(price, 2),
            "score":                score,
            "label":                label,
            "label_color":          label_color,
            "ignition":             ignition,
            "critical_alert":       False,
            "si_pct":               round(si_pct, 1),
            "dtc":                  round(dtc, 1),
            "borrow_fee":           round(borrow_fee, 1) if borrow_fee is not None else None,
            "borrow_fee_unknown":   borrow_fee is None,
            "float_shares_m":       round(float_shares / 1e6, 1) if float_shares else None,
            "market_cap_b":         round(market_cap / 1e9, 2) if market_cap else None,
            "vol_ratio":            round(vol_ratio, 2),
            "rvol":                 round(rvol, 2),
            "price_change_2d":      round(price_change_2d, 1),
            "entry_signal":         entry_signal,
            "week_high_52":         round(float(week_high_52), 2),
            "dist_to_52w_pct":      round(dist_to_52w_pct, 1),
            "sparkline":            sparkline,
            "high_60d":             round(float(high_60d), 2),
            "sma200":               round(float(sma200), 2) if sma200 and not np.isnan(float(sma200)) else None,
            "breakout_price":       round(breakout_price, 2),
            "dist_to_breakout_pct": round(dist_to_breakout_pct, 1),
            "exit_target":          round(exit_target, 2),
            "_scores": {
                "si":     round(si_s, 1),
                "dtc":    round(dtc_s, 1),
                "borrow": round(borrow_s, 1),
                "volume": round(vol_s, 1),
            },
        }

    except Exception as e:
        logger.debug(f"squeeze_scanner {ticker}: {e}")
        return None


def scan_tickers(
    tickers: list[str],
    min_score: float = 30.0,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> list[dict]:
    """
    Scan a list of tickers with optional progress callback.
    progress_callback(done, total, current_ticker) — called after each ticker.
    """
    results = []
    total   = len(tickers)

    for i, ticker in enumerate(tickers):
        t = ticker.upper().strip()
        r = analyze_ticker(t)
        if r:
            results.append(r)
        if progress_callback:
            progress_callback(i + 1, total, t)

    for r in results:
        r["critical_alert"] = is_critical_alert(r, results)

    filtered = [r for r in results if r["score"] >= min_score]
    filtered.sort(key=lambda x: x["score"], reverse=True)
    return filtered


# ── AI Verdict ─────────────────────────────────────────────────────────────────

def get_ai_verdict(r: dict) -> str:
    try:
        from src.llm_client import llm_complete

        borrow_str = f"{r['borrow_fee']:.1f}%" if r["borrow_fee"] is not None else "לא ידוע (קנס הופעל)"
        dist_str   = f"{r['dist_to_breakout_pct']:.1f}% מהפריצה" if r["dist_to_breakout_pct"] >= 0 else f"כבר מעל הפריצה ב-{abs(r['dist_to_breakout_pct']):.1f}%"
        vol_trend  = "עולה" if r["vol_ratio"] >= 1.5 else "יורד" if r["vol_ratio"] < 0.8 else "ניטרלי"
        price_dir  = "עולה" if r["price_change_2d"] > 0 else "יורד"

        prompt = f"""אתה אנליסט Short Squeeze מנוסה. נתח את הנתונים הבאים ותן ורדיקט קצר ואגרסיבי.

מניה: {r['ticker']}
Short Interest: {r['si_pct']:.1f}% מה-Float
Days to Cover: {r['dtc']:.1f} ימים
Borrow Fee: {borrow_str}
Volume Ratio: {r['vol_ratio']:.2f}x הממוצע ({vol_trend})
מחיר נוכחי: ${r['price']:.2f} ({price_dir} ב-2 ימים: {r['price_change_2d']:+.1f}%)
מרחק מהפריצה: {dist_str}
Exit Target: ${r['exit_target']:.2f}
Squeeze Score: {r['score']:.0f}/100

כתוב בדיוק 3 משפטים בעברית:
1. האם יש פה סחורה אמיתית או בול טראפ? מה המדד הכי חזק/חלש?
2. מה הקשר בין המרחק מהפריצה לווליום ולחץ השורטים? האם הם מתואמים?
3. האם להיכנס/לחכות/לדלג? תהיה ישיר ואל תהסס לומר "אין טרייד".

אל תוסיף כותרות, אל תשתמש ב-Markdown. טקסט רגיל בלבד.
וודא שהתשובה שלמה ולא נחתכת."""

        return llm_complete(prompt, max_tokens=600)

    except Exception as e:
        return f"שגיאה בניתוח AI: {e}"
