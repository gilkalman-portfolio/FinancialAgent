"""
Market Regime Throttle — BULL / CAUTION / BEAR

Returns a regime dict that downstream layers use to:
  - Scale position size (multiplier)
  - Widen/narrow stops
  - Block new longs in BEAR

Regime is NOT a binary kill switch. It's a multiplier on all downstream decisions.

Thresholds (20 / 28 VIX, SMA200) are starting points.
Walk-forward calibration should validate these per strategy before live use.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import TypedDict

import yfinance as yf

logger = logging.getLogger(__name__)

# ── Thresholds (starting points — calibrate via walk-forward) ──────────────
_VIX_CAUTION = 20.0   # VIX above this → CAUTION territory
_VIX_BEAR    = 28.0   # VIX above this (AND SPY below SMA200) → BEAR
_SPY_HISTORY = "1y"   # ~252 trading days → SMA200 accurate


class RegimeResult(TypedDict):
    regime: str          # "BULL" | "CAUTION" | "BEAR"
    vix: float
    spy_price: float
    spy_sma200: float
    spy_vs_sma200_pct: float   # (price - sma200) / sma200 * 100
    multiplier: float   # 1.0 BULL · 0.5 CAUTION · 0.3 BEAR
    cached_at: str      # ISO timestamp


# Cache for 15 minutes — regime doesn't flip intraday
_cache: dict[str, RegimeResult | None] = {}
_cache_ts: dict[str, datetime] = {}
_CACHE_TTL = timedelta(minutes=15)


def get_regime(force_refresh: bool = False) -> RegimeResult:
    """
    Fetch current market regime.

    Returns RegimeResult with regime string, raw inputs, and position multiplier.
    Falls back to CAUTION on any data failure — conservative default.
    """
    cache_key = "regime"
    now = datetime.now(timezone.utc)

    if not force_refresh and cache_key in _cache:
        age = now - _cache_ts.get(cache_key, datetime.min)
        if age < _CACHE_TTL and _cache[cache_key] is not None:
            return _cache[cache_key]  # type: ignore[return-value]

    try:
        result = _fetch_regime()
    except Exception as exc:
        logger.warning("market_regime: fetch failed, defaulting to CAUTION — %s", exc)
        result = _caution_fallback()

    _cache[cache_key] = result
    _cache_ts[cache_key] = now
    return result


def _fetch_regime() -> RegimeResult:
    vix = _get_vix()
    spy_price, spy_sma200 = _get_spy_vs_sma200()

    spy_vs_pct = (spy_price - spy_sma200) / spy_sma200 * 100 if spy_sma200 else 0.0

    if spy_price > spy_sma200 and vix < _VIX_CAUTION:
        regime = "BULL"
        multiplier = 1.0
    elif spy_price < spy_sma200 and vix > _VIX_BEAR:
        regime = "BEAR"
        multiplier = 0.3
    else:
        regime = "CAUTION"
        multiplier = 0.5

    return RegimeResult(
        regime=regime,
        vix=round(vix, 2),
        spy_price=round(spy_price, 2),
        spy_sma200=round(spy_sma200, 2),
        spy_vs_sma200_pct=round(spy_vs_pct, 2),
        multiplier=multiplier,
        cached_at=datetime.now(timezone.utc).isoformat(),
    )


def _get_vix() -> float:
    hist = yf.Ticker("^VIX").history(period="2d")
    if hist.empty:
        raise ValueError("VIX history empty")
    return float(hist["Close"].iloc[-1])


def _get_spy_vs_sma200() -> tuple[float, float]:
    hist = yf.Ticker("SPY").history(period=_SPY_HISTORY)
    if hist.empty or len(hist) < 20:
        raise ValueError("SPY history too short")
    closes = hist["Close"]
    price = float(closes.iloc[-1])
    # Use available bars for SMA — up to 200
    sma_period = min(200, len(closes))
    sma200 = float(closes.tail(sma_period).mean())
    return price, sma200


def _caution_fallback() -> RegimeResult:
    return RegimeResult(
        regime="CAUTION",
        vix=0.0,
        spy_price=0.0,
        spy_sma200=0.0,
        spy_vs_sma200_pct=0.0,
        multiplier=0.5,
        cached_at=datetime.now(timezone.utc).isoformat(),
    )


def regime_label_emoji(regime: str) -> str:
    return {"BULL": "🟢", "CAUTION": "🟡", "BEAR": "🔴"}.get(regime, "⚪")


def regime_summary(r: RegimeResult) -> str:
    emoji = regime_label_emoji(r["regime"])
    sign = "+" if r["spy_vs_sma200_pct"] >= 0 else ""
    return (
        f"{emoji} Regime: {r['regime']} | "
        f"VIX: {r['vix']} | "
        f"SPY vs SMA200: {sign}{r['spy_vs_sma200_pct']:.1f}%"
    )


_FG_VIX_FULL_GREED = 10.0
_FG_VIX_FULL_FEAR  = 40.0
_FG_SPY_FULL_FEAR  = -15.0
_FG_SPY_FULL_GREED = 15.0


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def fear_greed_score(vix: float, spy_vs_sma200_pct: float) -> int:
    # Approximates CNN's Fear & Greed Index using only signals this app already
    # computes elsewhere (VIX level + SPY-vs-SMA200 trend, both sourced from
    # get_regime()) — NOT CNN's real 7-factor methodology (junk bond demand,
    # safe haven demand, etc. aren't available in this codebase); a good-faith
    # in-house approximation only.
    vix_score = _clamp01((_FG_VIX_FULL_FEAR - vix) / (_FG_VIX_FULL_FEAR - _FG_VIX_FULL_GREED)) * 100
    spy_score = _clamp01((spy_vs_sma200_pct - _FG_SPY_FULL_FEAR) / (_FG_SPY_FULL_GREED - _FG_SPY_FULL_FEAR)) * 100
    return round((vix_score + spy_score) / 2)


def fear_greed_level(score: int) -> dict:
    """Classify a fear/greed score into a named band with color and description."""
    if score < 25:
        return {"label": "Extreme Fear", "color": "#dc2626",
                "desc": "Broad-based fear — volatility elevated and/or SPY well below its 200-day trend"}
    if score < 45:
        return {"label": "Fear", "color": "#f97316",
                "desc": "Cautious tape — volatility or trend leaning bearish"}
    if score < 56:
        return {"label": "Neutral", "color": "#6b7280",
                "desc": "Balanced — no strong fear or greed signal"}
    if score < 76:
        return {"label": "Greed", "color": "#4ade80",
                "desc": "Optimistic tape — low volatility and/or SPY above its 200-day trend"}
    return {"label": "Extreme Greed", "color": "#16a34a",
            "desc": "Broad-based greed — low volatility and strong trend; watch for complacency"}


def get_fear_greed_index(force_refresh: bool = False) -> dict:
    """
    Composite 0-100 Fear/Greed gauge for page_market.py, built on get_regime()'s
    cached VIX + SPY-vs-SMA200 inputs. Returns score=None (label="Unavailable")
    instead of a fabricated score when the underlying regime fetch failed and
    fell back to _caution_fallback()'s sentinel zeros.
    """
    regime = get_regime(force_refresh=force_refresh)
    if regime["vix"] <= 0:
        return {"score": None, "label": "Unavailable", "color": "#6b7280",
                "desc": "Market data temporarily unavailable",
                "vix": 0.0, "spy_vs_sma200_pct": 0.0}
    score = fear_greed_score(regime["vix"], regime["spy_vs_sma200_pct"])
    level = fear_greed_level(score)
    return {"score": score, "vix": regime["vix"],
            "spy_vs_sma200_pct": regime["spy_vs_sma200_pct"], **level}
