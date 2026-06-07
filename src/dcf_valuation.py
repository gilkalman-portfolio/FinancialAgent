"""
DCF Valuation — Discounted Cash Flow
Calculates intrinsic value and margin of safety vs current price.
Integrates into fundamentals score in stock_scorer.py.

Formula:
  Intrinsic Value = sum(FCF_t / (1+r)^t) + Terminal Value / (1+r)^n
  Terminal Value  = FCF_n * (1 + g) / (r - g)
  Margin of Safety = (Intrinsic - Price) / Intrinsic * 100

Data from yfinance:
  - freeCashflow (TTM)
  - revenueGrowth (YoY)
  - earningsGrowth (YoY)
  - sharesOutstanding
  - currentPrice
"""

import numpy as np
from typing import Optional
from loguru import logger


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def calculate_dcf(info: dict, cashflow_df=None) -> Optional[dict]:
    """
    Run DCF on a yfinance info dict.
    Returns dict with intrinsic_value, margin_of_safety, dcf_score, and details.
    Returns None if data is insufficient.
    """
    try:
        # ── Base FCF ───────────────────────────────────────────────────────────
        fcf = info.get("freeCashflow")
        if not fcf or fcf <= 0:
            # fallback: operatingCashflow - capex
            ocf  = info.get("operatingCashflow") or 0
            capex = info.get("capitalExpenditures") or 0
            fcf  = ocf - abs(capex)
            if fcf <= 0:
                return None

        shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
        if not shares or shares <= 0:
            return None

        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price or price <= 0:
            return None

        # ── Growth rates ───────────────────────────────────────────────────────
        rev_growth      = info.get("revenueGrowth")     # e.g. 0.25 = 25%
        earnings_growth = info.get("earningsGrowth")
        if rev_growth is not None and earnings_growth is not None:
            raw_growth_proxy = (rev_growth + earnings_growth) / 2
        else:
            raw_growth_proxy = rev_growth if rev_growth is not None else (earnings_growth or 0)

        # ── Historical FCF growth from cashflow DataFrame (more accurate) ──────
        historical_fcf_growth = None
        growth_source = "proxy"
        if cashflow_df is not None and not cashflow_df.empty:
            try:
                if "Free Cash Flow" in cashflow_df.index:
                    fcf_series = (cashflow_df.loc["Free Cash Flow"]
                                  .dropna().sort_index())
                    if len(fcf_series) >= 2:
                        fcf_vals = fcf_series.values   # oldest → newest
                        yoy = [
                            (fcf_vals[i] - fcf_vals[i - 1]) / abs(fcf_vals[i - 1])
                            for i in range(1, len(fcf_vals))
                            if fcf_vals[i - 1] != 0 and fcf_vals[i] > 0
                        ]
                        if yoy:
                            historical_fcf_growth = float(np.median(yoy))
                            growth_source = "historical_fcf"
            except Exception:
                pass

        # Blend: historical FCF trend 60% + info proxy 40% (fall back if unavailable)
        if historical_fcf_growth is not None:
            raw_growth = historical_fcf_growth * 0.6 + raw_growth_proxy * 0.4
        else:
            raw_growth = raw_growth_proxy

        # Conservative growth: clamp to 3%-25% for projection
        growth_rate = _clamp(raw_growth, 0.03, 0.25)

        # Terminal growth: 2-3% (long-run GDP)
        terminal_growth = 0.025

        # Discount rate: WACC proxy — 10% base
        # Adjust up for high debt/equity
        de = info.get("debtToEquity") or 0
        wacc = 0.10 + _clamp(de / 100 * 0.02, 0, 0.03)   # max +3% for very high leverage

        # ── 5-year DCF projection ──────────────────────────────────────────────
        n = 5
        pv_fcfs = []
        fcf_t   = float(fcf)
        for t in range(1, n + 1):
            fcf_t *= (1 + growth_rate)
            pv     = fcf_t / (1 + wacc) ** t
            pv_fcfs.append(pv)

        # Terminal value at year 5
        fcf_terminal = fcf_t * (1 + terminal_growth)
        if wacc <= terminal_growth:
            wacc = terminal_growth + 0.01   # safety: avoid division by zero
        terminal_value    = fcf_terminal / (wacc - terminal_growth)
        pv_terminal       = terminal_value / (1 + wacc) ** n

        # ── Intrinsic value per share ──────────────────────────────────────────
        intrinsic_equity  = sum(pv_fcfs) + pv_terminal
        intrinsic_per_share = intrinsic_equity / shares

        # ── Margin of safety ───────────────────────────────────────────────────
        mos_pct = (intrinsic_per_share - price) / intrinsic_per_share * 100

        # ── DCF Score (0-15 pts, replaces part of fundamentals) ───────────────
        # > 40% MoS = deeply undervalued  → max score
        # 20-40%     = undervalued        → good score
        # 0-20%      = fair value         → moderate
        # negative   = overvalued         → low or 0
        if mos_pct >= 40:   dcf_score = 15
        elif mos_pct >= 20: dcf_score = 11
        elif mos_pct >= 5:  dcf_score = 7
        elif mos_pct >= -10: dcf_score = 3
        else:               dcf_score = 0

        return {
            "intrinsic_value":   round(intrinsic_per_share, 2),
            "current_price":     round(price, 2),
            "margin_of_safety":  round(mos_pct, 1),
            "dcf_score":         dcf_score,
            "fcf_ttm":           round(fcf / 1e6, 1),       # in millions
            "growth_rate_used":  round(growth_rate * 100, 1),
            "wacc_used":         round(wacc * 100, 1),
            "terminal_growth":   round(terminal_growth * 100, 1),
            "upside_pct":        round(mos_pct, 1),
            "valuation":         _valuation_label(mos_pct),
            "growth_source":     growth_source,
        }

    except Exception as e:
        logger.debug(f"DCF failed: {e}")
        return None


def _valuation_label(mos_pct: float) -> str:
    if mos_pct >= 40:    return "DEEPLY UNDERVALUED"
    if mos_pct >= 20:    return "UNDERVALUED"
    if mos_pct >= 5:     return "FAIR VALUE"
    if mos_pct >= -15:   return "SLIGHTLY OVERVALUED"
    if mos_pct >= -35:   return "OVERVALUED"
    return "SIGNIFICANTLY OVERVALUED"


def dcf_score_only(info: dict) -> int:
    """Convenience — returns just the score (0-15) for use in stock_scorer."""
    result = calculate_dcf(info)
    return result["dcf_score"] if result else 0


def calculate_ps_valuation(info: dict) -> dict | None:
    """
    Price-to-Sales fallback for loss-making / negative-FCF companies where DCF returns None.
    Uses priceToSalesTrailing12Months from yfinance.

    Scoring (0-15 pts, same scale as DCF):
      P/S < 1    → 13 pts  (very cheap — rare for growth names)
      P/S 1–3    →  9 pts  (reasonable)
      P/S 3–6    →  6 pts  (premium)
      P/S 6–12   →  3 pts  (expensive)
      P/S > 12   →  1 pt   (very expensive — price in perfection)
      unavailable →  None
    """
    try:
        ps = info.get("priceToSalesTrailing12Months")
        if ps is None or ps <= 0:
            return None

        rev_growth = info.get("revenueGrowth")          # e.g. 0.35 = 35% YoY
        gross_margin = info.get("grossMargins") or 0     # e.g. 0.70 = 70%
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price:
            return None

        if   ps < 1:   score = 13; label = "VERY CHEAP (P/S)"
        elif ps < 3:   score = 9;  label = "REASONABLE (P/S)"
        elif ps < 6:   score = 6;  label = "PREMIUM (P/S)"
        elif ps < 12:  score = 3;  label = "EXPENSIVE (P/S)"
        else:          score = 1;  label = "VERY EXPENSIVE (P/S)"

        # Bonus: high-growth + high-margin justifies premium → +2 pts
        if rev_growth and rev_growth >= 0.25 and gross_margin >= 0.60 and ps < 15:
            score = min(score + 2, 13)
            label += " +growth"

        return {
            "ps_ratio":      round(ps, 1),
            "dcf_score":     score,
            "valuation":     label,
            "revenue_growth": round(rev_growth * 100, 1) if rev_growth else None,
            "gross_margin":  round(gross_margin * 100, 1),
            "current_price": round(price, 2),
            "method":        "P/S",
        }

    except Exception as e:
        logger.debug(f"P/S valuation failed: {e}")
        return None
