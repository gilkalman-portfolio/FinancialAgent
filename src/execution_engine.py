"""
Execution Engine — Layers 2–6

Bridges gap from screening signal → actionable trade decision.

  Layer 2: Hard veto engine          check_hard_vetos()
  Layer 3: Two-track confluence      evaluate_confluence()
  Layer 4: Position sizing           calc_position_size()
  Layer 5: Time-of-day flag          is_noise_window()
  Layer 6: Sector exposure guard     check_sector_exposure()

Public entry point: evaluate_trade()
  Returns a full TradeDecision dict or None if vetoed.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Any, Optional, TypedDict
from zoneinfo import ZoneInfo

import json

import yfinance as yf

from src.market_regime import RegimeResult, get_regime, regime_label_emoji

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ── Hard veto thresholds ──────────────────────────────────────────────────
_MIN_DAILY_DOLLAR_VOL = 5_000_000   # $5M minimum liquidity
_MIN_RR               = 1.5         # minimum reward-to-risk ratio
_MAX_DAILY_LOSS_PCT   = 0.02         # 2% daily loss limit — overridden by scheduler_config.json
_GAP_DOWN_THRESHOLD   = 0.05        # 5% gap-down on earnings = veto bounce trade
_ATR_STOP_MULT        = 2.0         # stop = entry − ATR_MULT × ATR(14)
_ATR_TARGET_MULT      = 3.0         # initial target = entry + TARGET_MULT × ATR(14) if no DCF

# ── Confluence thresholds ─────────────────────────────────────────────────
_TRACK_A_MIN_TOTAL    = 60          # minimum weighted sum across all pillars
_TRACK_A_MIN_PILLAR   = 10          # minimum per individual pillar (soft veto)
_TRACK_B_SI_THRESHOLD = 20.0        # SI% that boosts catalyst weight
_TRACK_B_DAYS_THRESHOLD = 5         # days-to-event that boosts catalyst weight

# ── Position sizing ───────────────────────────────────────────────────────
_RISK_PCT             = 0.01        # risk 1% of portfolio per trade
_MAX_PCT_TRACK_A      = 0.05        # hard cap 5% portfolio (Track A)
_MAX_PCT_TRACK_B      = 0.015       # hard cap 1.5% portfolio (Track B)
_SECTOR_WARN_THRESHOLD = 0.25       # warn if sector > 25% of portfolio
_SECTOR_SIZE_ADJ      = 0.5         # additional size multiplier when sector concentrated

# ── Time-of-day noise windows (ET) ───────────────────────────────────────
_NOISE_OPEN_START  = time(9, 30)
_NOISE_OPEN_END    = time(10, 0)
_NOISE_CLOSE_START = time(15, 45)
_NOISE_CLOSE_END   = time(16, 0)


# ─────────────────────────────────────────────────────────────────────────
# TypedDicts
# ─────────────────────────────────────────────────────────────────────────

class VetoResult(TypedDict):
    passed: bool
    reason: str          # empty string if passed


class PillarScores(TypedDict):
    technical: float     # 0–40
    fundamental: float   # 0–30
    catalyst: float      # 0–30
    total: float


class ConfluenceResult(TypedDict):
    track: str | None        # "A" | "B" | None (no track = vetoed by confluence)
    pillars: PillarScores
    catalyst_weight_boosted: bool
    notes: list[str]


class SizeResult(TypedDict):
    shares: int
    dollar_invested: float
    dollar_risk: float
    stop_price: float
    target_price: float
    rr_ratio: float
    max_pct_cap_applied: bool
    sector_adj_applied: bool


class TradeDecision(TypedDict):
    ticker: str
    track: str                   # "A" | "B"
    regime: RegimeResult
    veto: VetoResult             # passed=True when we get here
    confluence: ConfluenceResult
    sizing: SizeResult
    noise_window: bool
    sector_concentration_pct: float
    entry_price: float
    atr: float
    signal_ts: str               # ISO timestamp


# ─────────────────────────────────────────────────────────────────────────
# Daily loss limit (Layer 0 — runs before all other vetos)
# ─────────────────────────────────────────────────────────────────────────

_position_tracker = None  # injected via set_position_tracker()


def set_position_tracker(tracker) -> None:
    """Inject a PositionTracker instance for daily loss limit checks."""
    global _position_tracker
    _position_tracker = tracker


def _get_max_daily_loss_pct() -> float:
    """Read max_daily_loss_pct from scheduler_config.json, default 0.02."""
    try:
        from pathlib import Path
        cfg_path = Path(__file__).parent.parent / "scheduler_config.json"
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return float(cfg.get("max_daily_loss_pct", _MAX_DAILY_LOSS_PCT))
    except Exception:
        pass
    return _MAX_DAILY_LOSS_PCT


def check_daily_loss_limit() -> VetoResult:
    """Veto if today's P&L exceeds the daily loss limit.

    Returns passed=True if OK to trade, passed=False if daily limit breached.
    Skips (returns passed=True) if no position_tracker is injected.
    """
    if _position_tracker is None:
        logger.debug("check_daily_loss_limit: no position_tracker — skipping")
        return VetoResult(passed=True, reason="")

    try:
        day_pnl = _position_tracker.get_daily_pnl()
        portfolio_value = _position_tracker.get_portfolio_value()
    except Exception as e:
        logger.warning(f"check_daily_loss_limit: data fetch failed: {e} — skipping")
        return VetoResult(passed=True, reason="")

    if portfolio_value <= 0:
        logger.warning("check_daily_loss_limit: portfolio_value=0 — skipping")
        return VetoResult(passed=True, reason="")

    max_loss_pct = _get_max_daily_loss_pct()
    loss_threshold = -(max_loss_pct * portfolio_value)

    if day_pnl < loss_threshold:
        reason = (
            f"DAILY_LOSS_LIMIT: day P&L ${day_pnl:,.2f} exceeds "
            f"-{max_loss_pct*100:.1f}% of ${portfolio_value:,.0f} "
            f"(limit ${loss_threshold:,.2f})"
        )
        logger.warning(f"check_daily_loss_limit: {reason}")
        return VetoResult(passed=False, reason=reason)

    return VetoResult(passed=True, reason="")


# ─────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────

def evaluate_trade(
    ticker: str,
    score_data: dict[str, Any],
    portfolio_value: float = 100_000.0,
    portfolio_tickers: list[str] | None = None,
    signal_type: str | None = None,
) -> TradeDecision | None:
    """
    Full execution evaluation for a ticker that has already passed screening.

    score_data keys used:
        score              float   composite score 0-100
        rsi                float
        macd_signal        str     "bullish"|"bearish"|"neutral"
        ma_trend           str     "strong_uptrend"|"uptrend"|"sideways"|...
        volume_ratio       float
        si_pct             float   short interest % of float
        fundamentals_score float   0-10
        dcf_score          float   0-15
        dcf_intrinsic      float   DCF intrinsic value (None if N/A)
        catalyst_type      str     "earnings"|"pdufa"|"sec_8k"|"analyst"|""
        days_to_event      int     days until nearest catalyst (999 if none)
        explosion_score    float   0-100 (catalyst scanner)
        price              float   current price

    Returns TradeDecision or None if hard-vetoed.
    """
    portfolio_tickers = portfolio_tickers or []

    regime = get_regime()
    price = float(score_data.get("price", 0) or 0)

    if price <= 0:
        logger.warning("evaluate_trade(%s): price=0, skipping", ticker)
        return None

    # Layer -1: SELL requires an open position
    if signal_type == "SELL" and _position_tracker is not None:
        try:
            exposure = _position_tracker.get_current_exposure(ticker)
            if exposure == 0.0:
                logger.info("evaluate_trade(%s): VETOED — No open position to sell", ticker)
                return None
        except Exception as e:
            logger.warning("evaluate_trade(%s): position check failed: %s — allowing", ticker, e)

    # Layer 0: daily loss limit (runs before everything else)
    daily_veto = check_daily_loss_limit()
    if not daily_veto["passed"]:
        logger.info("evaluate_trade(%s): VETOED — %s", ticker, daily_veto["reason"])
        return None

    atr = _get_atr(ticker, price)

    # Layer 2: hard vetos
    veto = check_hard_vetos(ticker, price, atr, score_data, regime, signal_type=signal_type)
    if not veto["passed"]:
        logger.info("evaluate_trade(%s): VETOED — %s", ticker, veto["reason"])
        return None

    # Layer 3: confluence
    confluence = evaluate_confluence(score_data)
    if confluence["track"] is None:
        logger.info("evaluate_trade(%s): confluence not met — %s", ticker, confluence["notes"])
        return None

    # Layer 4: position sizing
    sector = _get_sector(ticker)
    sector_concentration = _sector_concentration(sector, portfolio_tickers)
    sizing = calc_position_size(
        price=price,
        atr=atr,
        portfolio_value=portfolio_value,
        regime_multiplier=regime["multiplier"],
        track=confluence["track"],
        dcf_intrinsic=score_data.get("dcf_intrinsic"),
        sector_concentration_pct=sector_concentration,
    )

    if sizing["rr_ratio"] < _MIN_RR:
        logger.info("evaluate_trade(%s): R:R %.2f < %.1f — vetoed post-sizing", ticker, sizing["rr_ratio"], _MIN_RR)
        return None

    # Layer 5: time-of-day flag
    noise = is_noise_window()

    return TradeDecision(
        ticker=ticker,
        track=confluence["track"],
        regime=regime,
        veto=veto,
        confluence=confluence,
        sizing=sizing,
        noise_window=noise,
        sector_concentration_pct=sector_concentration,
        entry_price=price,
        atr=round(atr, 4),
        signal_ts=datetime.utcnow().isoformat(),
    )


# ─────────────────────────────────────────────────────────────────────────
# Layer 2: Hard vetos
# ─────────────────────────────────────────────────────────────────────────

def check_hard_vetos(
    ticker: str,
    price: float,
    atr: float,
    score_data: dict[str, Any],
    regime: RegimeResult,
    signal_type: Optional[str] = None,
) -> VetoResult:
    """Check all hard vetos. Returns passed=False with reason on first failure."""

    # BEAR regime: no new longs — but SELL (exits) must be allowed
    if regime["regime"] == "BEAR" and signal_type != "SELL":
        return VetoResult(passed=False, reason="Regime BEAR — exits only, no new longs")

    # Liquidity
    try:
        avg_vol = float(score_data.get("avg_volume", 0) or 0)
        dollar_vol = avg_vol * price
        if dollar_vol > 0 and dollar_vol < _MIN_DAILY_DOLLAR_VOL:
            return VetoResult(
                passed=False,
                reason=f"Liquidity ${dollar_vol/1e6:.1f}M < ${_MIN_DAILY_DOLLAR_VOL/1e6:.0f}M daily"
            )
    except Exception:
        pass  # skip liquidity check if data missing

    # Gap-down on earnings bounce
    if _is_gap_down_bounce(ticker, score_data):
        return VetoResult(
            passed=False,
            reason="Gap-down > 5% on earnings — bounce trade vetoed"
        )

    # R:R pre-check using ATR (sizing layer does the final check)
    if atr > 0:
        stop_dist = atr * _ATR_STOP_MULT
        target_dist = atr * _ATR_TARGET_MULT
        rr = target_dist / stop_dist if stop_dist > 0 else 0
        if rr < _MIN_RR:
            return VetoResult(
                passed=False,
                reason=f"R:R {rr:.2f} < {_MIN_RR} (ATR-based pre-check)"
            )

    return VetoResult(passed=True, reason="")


def _is_gap_down_bounce(ticker: str, score_data: dict[str, Any]) -> bool:
    """
    True if: stock gapped down > 5% on an earnings day AND price is rising (bounce).
    Protects against entering dead-cat bounces after earnings disasters.
    """
    try:
        catalyst = score_data.get("catalyst_type", "")
        if catalyst != "earnings":
            return False
        days_to_event = int(score_data.get("days_to_event", 999) or 999)
        if days_to_event > 1:
            return False  # event hasn't happened yet

        hist = yf.Ticker(ticker).history(period="3d")
        if len(hist) < 2:
            return False
        prev_close = float(hist["Close"].iloc[-2])
        open_today = float(hist["Open"].iloc[-1])
        gap_pct = (open_today - prev_close) / prev_close
        return gap_pct < -_GAP_DOWN_THRESHOLD
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────
# Layer 3: Two-track confluence
# ─────────────────────────────────────────────────────────────────────────

def evaluate_confluence(score_data: dict[str, Any]) -> ConfluenceResult:
    """
    Score three independent pillars and assign a track.

    Track B takes priority if special-situation signals are present.
    Track A requires weighted total ≥ 60 with no pillar below 10.
    Neither track → confluence["track"] = None.
    """
    si_pct = float(score_data.get("si_pct", 0) or 0)
    days_to_event = int(score_data.get("days_to_event", 999) or 999)
    catalyst_type = str(score_data.get("catalyst_type", "") or "")
    explosion_score = float(score_data.get("explosion_score", 0) or 0)

    # Determine if Track B conditions are present
    is_special = catalyst_type in ("pdufa", "sec_8k") or (
        catalyst_type == "earnings" and (si_pct >= _TRACK_B_SI_THRESHOLD or days_to_event <= _TRACK_B_DAYS_THRESHOLD)
    )
    catalyst_weight_boosted = is_special and (si_pct >= _TRACK_B_SI_THRESHOLD or days_to_event <= _TRACK_B_DAYS_THRESHOLD)

    # Score pillars
    tech = _score_technical_pillar(score_data)          # 0–40
    fund = _score_fundamental_pillar(score_data)        # 0–30
    cat  = _score_catalyst_pillar(score_data, boosted=catalyst_weight_boosted)  # 0–30

    total = tech + fund + cat
    notes: list[str] = []

    if is_special and explosion_score >= 40:
        # Track B — check minimum pillar floors (lower bar)
        if tech < 8:
            notes.append(f"Track B: Technical pillar too weak ({tech:.0f}/40)")
        else:
            return ConfluenceResult(
                track="B",
                pillars=PillarScores(technical=tech, fundamental=fund, catalyst=cat, total=total),
                catalyst_weight_boosted=catalyst_weight_boosted,
                notes=notes,
            )

    # Track A evaluation
    if tech < _TRACK_A_MIN_PILLAR:
        notes.append(f"Technical {tech:.0f} < {_TRACK_A_MIN_PILLAR} minimum")
    if fund < _TRACK_A_MIN_PILLAR:
        notes.append(f"Fundamental {fund:.0f} < {_TRACK_A_MIN_PILLAR} minimum")
    if cat < _TRACK_A_MIN_PILLAR:
        notes.append(f"Catalyst {cat:.0f} < {_TRACK_A_MIN_PILLAR} minimum")
    if total < _TRACK_A_MIN_TOTAL:
        notes.append(f"Total {total:.0f} < {_TRACK_A_MIN_TOTAL} required")

    if not notes:
        return ConfluenceResult(
            track="A",
            pillars=PillarScores(technical=tech, fundamental=fund, catalyst=cat, total=total),
            catalyst_weight_boosted=False,
            notes=[],
        )

    return ConfluenceResult(
        track=None,
        pillars=PillarScores(technical=tech, fundamental=fund, catalyst=cat, total=total),
        catalyst_weight_boosted=catalyst_weight_boosted,
        notes=notes,
    )


def _score_technical_pillar(sd: dict[str, Any]) -> float:
    """Technical pillar: 0–40 pts."""
    score = 0.0

    # RSI (0–12): sweet spot 40–65
    rsi = float(sd.get("rsi", 50) or 50)
    if 40 <= rsi <= 65:
        score += 12
    elif 35 <= rsi < 40 or 65 < rsi <= 70:
        score += 8
    elif 30 <= rsi < 35 or 70 < rsi <= 75:
        score += 4
    # RSI < 30 or > 75 → 0

    # MACD (0–10)
    macd = str(sd.get("macd_signal", "") or "")
    if macd == "bullish":
        score += 10
    elif macd == "neutral":
        score += 5

    # MA Trend (0–12)
    trend = str(sd.get("ma_trend", "") or "")
    ma_pts = {"strong_uptrend": 12, "uptrend": 9, "sideways": 4, "downtrend": 1, "strong_downtrend": 0}
    score += ma_pts.get(trend, 4)

    # Volume ratio (0–6)
    vol_ratio = float(sd.get("volume_ratio", 1) or 1)
    if vol_ratio >= 2.0:
        score += 6
    elif vol_ratio >= 1.5:
        score += 4
    elif vol_ratio >= 1.0:
        score += 2

    return min(40.0, score)


def _score_fundamental_pillar(sd: dict[str, Any]) -> float:
    """Fundamental pillar: 0–30 pts."""
    score = 0.0

    # Raw fundamentals score from scorer (0–10) → scale to 0–15
    fund_raw = float(sd.get("fundamentals_score", 5) or 5)
    score += (fund_raw / 10.0) * 15

    # DCF margin of safety (0–15)
    dcf_raw = float(sd.get("dcf_score", 0) or 0)
    score += (dcf_raw / 15.0) * 15

    return min(30.0, score)


def _score_catalyst_pillar(sd: dict[str, Any], boosted: bool = False) -> float:
    """
    Catalyst pillar: 0–30 pts.
    When boosted (Track B conditions): explosion_score drives the full score.
    When not boosted: explosion_score contributes partially; time-to-event adds urgency.
    """
    explosion = float(sd.get("explosion_score", 0) or 0)
    days = int(sd.get("days_to_event", 999) or 999)
    catalyst_type = str(sd.get("catalyst_type", "") or "")

    if not catalyst_type:
        return 10.0  # no catalyst — neutral, doesn't block Track A

    if boosted:
        # Track B: explosion_score is the primary driver
        return min(30.0, (explosion / 100.0) * 30)

    # Track A: mix of explosion score + urgency
    base = (explosion / 100.0) * 20
    if days <= 3:
        base += 10
    elif days <= 7:
        base += 7
    elif days <= 14:
        base += 4

    return min(30.0, base)


# ─────────────────────────────────────────────────────────────────────────
# Layer 4: Position sizing
# ─────────────────────────────────────────────────────────────────────────

def calc_position_size(
    price: float,
    atr: float,
    portfolio_value: float,
    regime_multiplier: float,
    track: str,
    dcf_intrinsic: float | None = None,
    sector_concentration_pct: float = 0.0,
) -> SizeResult:
    """
    Kelly-inspired position sizing, volatility-adjusted and regime-multiplied.

    Stop  = entry − ATR_STOP_MULT × ATR(14)
    Target = DCF intrinsic value (if available and above entry + 1 ATR)
             else entry + ATR_TARGET_MULT × ATR(14)
    Size  = risk_budget / stop_distance × regime_multiplier
    """
    risk_budget = portfolio_value * _RISK_PCT * regime_multiplier

    stop_distance = max(atr * _ATR_STOP_MULT, price * 0.01)  # at least 1% stop
    stop_price = price - stop_distance

    # Target: prefer DCF intrinsic if it implies ≥ 1 ATR upside
    if dcf_intrinsic and dcf_intrinsic > price + atr:
        target_price = dcf_intrinsic
    else:
        target_price = price + atr * _ATR_TARGET_MULT

    target_distance = target_price - price
    rr_ratio = round(target_distance / stop_distance, 2) if stop_distance > 0 else 0.0

    # Raw shares from risk budget
    shares = risk_budget / stop_distance if stop_distance > 0 else 0
    dollar_invested = shares * price

    # Hard portfolio caps
    max_pct = _MAX_PCT_TRACK_B if track == "B" else _MAX_PCT_TRACK_A
    max_dollars = portfolio_value * max_pct
    max_cap_applied = False
    if dollar_invested > max_dollars:
        dollar_invested = max_dollars
        shares = dollar_invested / price
        max_cap_applied = True

    # Sector concentration adjustment
    sector_adj_applied = False
    if sector_concentration_pct > _SECTOR_WARN_THRESHOLD:
        dollar_invested *= _SECTOR_SIZE_ADJ
        shares *= _SECTOR_SIZE_ADJ
        sector_adj_applied = True

    shares = max(0, int(shares))
    dollar_invested = round(shares * price, 2)
    dollar_risk = round(shares * stop_distance, 2)

    return SizeResult(
        shares=shares,
        dollar_invested=dollar_invested,
        dollar_risk=dollar_risk,
        stop_price=round(stop_price, 2),
        target_price=round(target_price, 2),
        rr_ratio=rr_ratio,
        max_pct_cap_applied=max_cap_applied,
        sector_adj_applied=sector_adj_applied,
    )


# ─────────────────────────────────────────────────────────────────────────
# Layer 5: Time-of-day noise window
# ─────────────────────────────────────────────────────────────────────────

def is_noise_window() -> bool:
    """True if current ET time is in the opening or closing noise window."""
    now = datetime.now(ET).time()
    opening = _NOISE_OPEN_START <= now <= _NOISE_OPEN_END
    closing  = _NOISE_CLOSE_START <= now <= _NOISE_CLOSE_END
    return opening or closing


# ─────────────────────────────────────────────────────────────────────────
# Layer 6: Sector exposure guard
# ─────────────────────────────────────────────────────────────────────────

def check_sector_exposure(ticker: str, portfolio_tickers: list[str]) -> dict[str, Any]:
    sector = _get_sector(ticker)
    pct = _sector_concentration(sector, portfolio_tickers)
    return {
        "sector": sector,
        "concentration_pct": pct,
        "warn": pct > _SECTOR_WARN_THRESHOLD,
        "size_adj": _SECTOR_SIZE_ADJ if pct > _SECTOR_WARN_THRESHOLD else 1.0,
    }


def _get_sector(ticker: str) -> str:
    try:
        info = yf.Ticker(ticker).info
        return str(info.get("sector") or "Unknown")
    except Exception:
        return "Unknown"


def _sector_concentration(sector: str, portfolio_tickers: list[str]) -> float:
    if not portfolio_tickers or sector in ("Unknown", ""):
        return 0.0
    sectors = []
    for t in portfolio_tickers:
        try:
            s = yf.Ticker(t).info.get("sector") or "Unknown"
            sectors.append(s)
        except Exception:
            sectors.append("Unknown")
    if not sectors:
        return 0.0
    return sectors.count(sector) / len(sectors)


# ─────────────────────────────────────────────────────────────────────────
# Field normalizer — maps score_stock() output to engine's expected keys
# ─────────────────────────────────────────────────────────────────────────

def normalize_score_data(r: dict[str, Any]) -> dict[str, Any]:
    """
    Convert score_stock() result dict to execution engine field names.

    score_stock() → execution_engine mapping:
      macd        → macd_signal
      short_pct   → si_pct
      fund_score  → fundamentals_score
      dcf['intrinsic'] → dcf_intrinsic
      avg_volume: fetched live if absent
    """
    dcf_dict = r.get("dcf") or {}
    return {
        **r,
        "macd_signal":        r.get("macd", ""),
        "si_pct":             r.get("short_pct", 0),
        "fundamentals_score": r.get("fund_score", 5),
        "dcf_intrinsic":      dcf_dict.get("intrinsic") if dcf_dict else None,
        # catalyst fields default to "no catalyst" when not from catalyst scanner
        "catalyst_type":      r.get("catalyst_type", ""),
        "days_to_event":      r.get("days_to_event", 999),
        "explosion_score":    r.get("explosion_score", 0),
    }


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _get_atr(ticker: str, price: float, period: int = 14) -> float:
    """ATR(14) using SMA of True Range (matches TradingView default)."""
    try:
        hist = yf.Ticker(ticker).history(period="30d")
        if len(hist) < period + 1:
            return price * 0.02  # fallback: 2% of price
        high = hist["High"]
        low  = hist["Low"]
        prev_close = hist["Close"].shift(1)
        tr = (high - low).combine(
            (high - prev_close).abs(), max
        ).combine(
            (low - prev_close).abs(), max
        )
        return float(tr.tail(period).mean())
    except Exception:
        return price * 0.02


# ─────────────────────────────────────────────────────────────────────────
# Trade plan builder — standalone, no execution engine required
# ─────────────────────────────────────────────────────────────────────────

def build_trade_plan(ticker: str, price: float, hist_1d=None) -> dict | None:
    """
    Compute a simple trade plan from price + optional 1-year daily history.

    Returns dict with keys:
        entry_low, entry_high   — ±1% entry zone
        stop_loss               — swing low (10d) − 0.5 × ATR14
        target1                 — Bollinger Upper Band (SMA20 + 2σ)
        target2                 — 52-week high
        rr_ratio                — (target1 − price) / (price − stop_loss)
        risk_pct                — (price − stop_loss) / price × 100

    Returns None on any error.
    """
    try:
        import pandas as pd

        if hist_1d is None or len(hist_1d) < 22:
            hist_1d = yf.Ticker(ticker).history(period="1y")
        if hist_1d is None or len(hist_1d) < 22:
            return None

        close = hist_1d["Close"]
        high  = hist_1d["High"]
        low   = hist_1d["Low"]
        prev_close = close.shift(1)

        # ATR14 — Wilder EMA (matches TradingView)
        tr = (high - low).combine(
            (high - prev_close).abs(), max
        ).combine(
            (low - prev_close).abs(), max
        )
        atr14 = float(tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])

        # Stop Loss — tighter of: (SMA20 − 1×ATR) or (swing low 10d − 0.5×ATR)
        # Capped at −12% to avoid absurdly wide stops on volatile small-caps.
        sma20       = float(close.iloc[-20:].mean())
        std20       = float(close.iloc[-20:].std())
        support_10d = float(low.iloc[-10:].min())
        stop_sma    = sma20 - atr14            # SMA-based stop
        stop_swing  = support_10d - atr14 * 0.5  # swing-low stop
        stop_loss   = max(stop_sma, stop_swing, price * 0.88)  # floor: −12%

        # BB Upper — SMA20 + 2σ
        bb_upper = sma20 + 2 * std20

        # 52-week high (exclude today to avoid intraday spike)
        high52w = float(close.iloc[:-1].max()) if len(close) > 1 else float(close.max())

        # Target ordering: T1 = closer target, T2 = further target
        # Both must be above the current price to be valid
        candidates = sorted(
            [t for t in [bb_upper, high52w] if t > price],
            reverse=False  # ascending: T1 closer, T2 further
        )
        if len(candidates) == 0:
            # Price already above both — use ATR extensions
            target1 = price + atr14 * 2
            target2 = price + atr14 * 4
        elif len(candidates) == 1:
            target1 = candidates[0]
            target2 = price + atr14 * 3   # synthetic T2
        else:
            target1, target2 = candidates[0], candidates[1]

        stop_dist   = price - stop_loss
        rr_ratio    = round((target1 - price) / stop_dist, 2) if stop_dist > 0 else 0.0
        risk_pct    = round(stop_dist / price * 100, 2) if price > 0 else 0.0

        return {
            "entry_low":  round(price * 0.99, 2),
            "entry_high": round(price * 1.01, 2),
            "stop_loss":  round(stop_loss, 2),
            "target1":    round(target1, 2),
            "target2":    round(target2, 2),
            "rr_ratio":   rr_ratio,
            "risk_pct":   risk_pct,
        }
    except Exception as e:
        logger.warning(f"build_trade_plan({ticker}): {e}")
        return None


def format_trade_plan_block(ticker: str, price: float, hist_1d=None) -> str:
    """
    Return a human-readable trade-plan block for Telegram BUY alerts.
    Explains the trade in plain language, not just raw numbers.
    Returns empty string on failure (safe to always call).
    """
    try:
        plan = build_trade_plan(ticker, price, hist_1d)
        if not plan:
            return ""

        risk_usd   = round(price - plan["stop_loss"], 2)
        reward_usd = round(plan["target1"] - price, 2)
        t1_pct     = (plan["target1"] - price) / price * 100
        t2_pct     = (plan["target2"] - price) / price * 100
        rr         = plan["rr_ratio"]

        # R:R verdict — plain language
        if rr >= 2.5:
            rr_verdict = f"מצוין — מרוויח ${reward_usd:.2f} על כל $1 בסיכון"
        elif rr >= 1.5:
            rr_verdict = f"טוב — מרוויח ${reward_usd:.2f} על כל $1 בסיכון"
        elif rr >= 1.0:
            rr_verdict = f"גבולי — מרוויח ${reward_usd:.2f} על כל $1 בסיכון"
        else:
            # Entry price that gives R:R 1:1.5 with same stop and target1:
            # (target1 - entry) / (entry - stop) = 1.5  →  entry = (target1 + 1.5*stop) / 2.5
            better_entry = round((plan["target1"] + 1.5 * plan["stop_loss"]) / 2.5, 2)
            rr_verdict = (
                f"גבוה מדי — מסכן ${risk_usd:.2f} כדי להרוויח ${reward_usd:.2f}\n"
                f"   כניסה טובה יותר: סביב ${better_entry:.2f} (R:R יהיה ~1:1.5)"
            )

        # Entry timing guidance
        if price > plan["entry_high"] * 1.02:
            timing = "המתן לפולבק לאזור הכניסה לפני שנכנס"
        else:
            timing = "המחיר בטווח כניסה — ניתן להיכנס"

        return (
            f"\n─────────────────"
            f"\n📍 כניסה: ${plan['entry_low']:.2f}–${plan['entry_high']:.2f} | {timing}"
            f"\n🛑 Stop:  ${plan['stop_loss']:.2f} (מתחת ל-SMA20)"
            f"\n🎯 יעד 1: ${plan['target1']:.2f} (+{t1_pct:.1f}%)"
            f"\n🎯 יעד 2: ${plan['target2']:.2f} (+{t2_pct:.1f}%)"
            f"\n⚖️ סיכון/רווח: {rr_verdict}"
            f"\n─────────────────"
        )
    except Exception as e:
        logger.warning(f"format_trade_plan_block({ticker}): {e}")
        return ""


# ─────────────────────────────────────────────────────────────────────────
# Telegram message formatter
# ─────────────────────────────────────────────────────────────────────────

def format_trade_alert(decision: TradeDecision) -> str:
    """Format a complete trade decision as a Telegram-ready message."""
    r = decision["regime"]
    c = decision["confluence"]
    s = decision["sizing"]
    p = c["pillars"]

    regime_emoji = regime_label_emoji(r["regime"])
    track_label = f"Track {'A — Confluence' if decision['track'] == 'A' else 'B — Special Situation'}"

    sign = "+" if r["spy_vs_sma200_pct"] >= 0 else ""
    pct_str = f"{sign}{r['spy_vs_sma200_pct']:.1f}%"

    lines = [
        f"✅ {decision['ticker']} — {track_label} | {regime_emoji} {r['regime']} (VIX {r['vix']} | SPY {pct_str})",
        f"Technical: {p['technical']:.0f}/40 | Fundamental: {p['fundamental']:.0f}/30 | Catalyst: {p['catalyst']:.0f}/30",
        f"Entry: ${decision['entry_price']:.2f} | Stop: ${s['stop_price']:.2f} ({((s['stop_price'] - decision['entry_price']) / decision['entry_price'] * 100):.1f}%) | Target: ${s['target_price']:.2f} ({((s['target_price'] - decision['entry_price']) / decision['entry_price'] * 100):.1f}%)",
        f"Size: {s['shares']} shares (${s['dollar_invested']:,.0f}) — risks ${s['dollar_risk']:,.0f} | R:R: {s['rr_ratio']:.1f}:1",
    ]

    if s["sector_adj_applied"]:
        lines.append(f"⚠️ Sector concentration {decision['sector_concentration_pct']*100:.0f}% — size reduced 50%")
    if decision["noise_window"]:
        lines.append("⚠️ First/last 30 min — wait for confirmation before entering")
    if s["max_pct_cap_applied"]:
        lines.append(f"ℹ️ Position capped at {'1.5' if decision['track'] == 'B' else '5'}% portfolio max")

    return "\n".join(lines)
