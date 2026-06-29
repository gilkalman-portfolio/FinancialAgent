"""
DCF Valuation — Discounted Cash Flow
Calculates intrinsic value and margin of safety vs current price.
Integrates into fundamentals score in stock_scorer.py.

Formula:
  Enterprise Value = Σ FCF_t/(1+WACC)^t  +  TV/(1+WACC)^n
  Terminal Value   = FCF_n*(1+g) / (WACC-g)
  Equity Value     = Enterprise Value − Net Debt
  Intrinsic/share  = Equity Value / sharesOutstanding
  Margin of Safety = (Intrinsic − Price) / Intrinsic * 100

FCF source priority:
  1. SEC EDGAR XBRL (median of last 4 annual 10-K values) — audited, free
  2. yfinance cashflow DataFrame "Free Cash Flow" row
  3. yfinance info.freeCashflow (TTM)
  4. operatingCashflow − |capitalExpenditures|

WACC:
  Cost of equity — CAPM: Rf (10Y Treasury) + Beta × 5.5% ERP
  Cost of debt   — interestExpense / totalDebt (actual); falls back to tier estimate
  WACC = E/(D+E)×Ke  +  D/(D+E)×Kd×(1−tax)
"""

import numpy as np
from typing import Optional
from loguru import logger


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _capm_cost_of_equity(info: dict) -> float:
    """Ke = Rf + Beta × ERP. Rf from 10Y Treasury via yfinance; ERP = 5.5% (Damodaran)."""
    try:
        import yfinance as yf
        beta = _clamp(float(info.get("beta") or 1.0), 0.5, 3.0)
        tnx  = yf.Ticker("^TNX").history(period="5d")
        rf   = float(tnx["Close"].iloc[-1]) / 100 if not tnx.empty else 0.045
        return _clamp(rf + beta * 0.055, 0.07, 0.20)
    except Exception:
        return 0.10


def calculate_dcf(info: dict, cashflow_df=None, ticker: str = "") -> Optional[dict]:
    """
    Run DCF on a yfinance info dict.
    Returns dict with intrinsic_value, margin_of_safety, dcf_score, and details.
    Returns None if data is insufficient.
    """
    try:
        # ── Financial sector exclusion — DCF (FCFF) is methodologically invalid
        #    for banks and insurance: operating cash flow reflects balance-sheet
        #    flows (loans, deposits) not business operations. Fall through to P/S.
        sector = info.get("sector", "")
        if sector in ("Financial Services", "Banks", "Insurance"):
            return None

        # ── Base FCF — tiered sourcing ────────────────────────────────────────
        # Tier 1: SEC EDGAR XBRL (median of 4 annual 10-K values — most reliable)
        fcf = None
        fcf_source = "yfinance"
        if ticker:
            try:
                from src.edgar_fcf import get_edgar_fcf_median
                edgar_fcf = get_edgar_fcf_median(ticker)
                if edgar_fcf and edgar_fcf > 0:
                    fcf = edgar_fcf
                    fcf_source = "edgar_xbrl"
            except Exception:
                pass

        # Tier 2: yfinance cashflow DataFrame "Free Cash Flow" row (multi-year median)
        if fcf is None and cashflow_df is not None and not cashflow_df.empty:
            try:
                if "Free Cash Flow" in cashflow_df.index:
                    vals = cashflow_df.loc["Free Cash Flow"].dropna().values
                    pos  = [v for v in vals if v > 0]
                    if pos:
                        import statistics
                        fcf = statistics.median(pos)
                        fcf_source = "yf_cashflow_df"
            except Exception:
                pass

        # Tier 3: yfinance info.freeCashflow (TTM single value)
        if fcf is None:
            fcf = info.get("freeCashflow")
            if fcf and fcf > 0:
                fcf_source = "yf_info"
            else:
                # Tier 4: operatingCashflow - capex
                ocf   = info.get("operatingCashflow") or 0
                capex = info.get("capitalExpenditures") or 0
                fcf   = ocf - abs(capex)
                fcf_source = "yf_ocf_capex"

        if not fcf or fcf <= 0:
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
        elif rev_growth is not None:
            raw_growth_proxy = rev_growth
        elif earnings_growth is not None:
            raw_growth_proxy = earnings_growth
        else:
            raw_growth_proxy = None   # both unavailable — defer to historical FCF growth

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
        if historical_fcf_growth is not None and raw_growth_proxy is not None:
            raw_growth = historical_fcf_growth * 0.6 + raw_growth_proxy * 0.4
        elif historical_fcf_growth is not None:
            raw_growth = historical_fcf_growth
            growth_source = "historical_fcf_only"
        elif raw_growth_proxy is not None:
            raw_growth = raw_growth_proxy
        else:
            logger.debug(f"DCF {ticker}: growth data unavailable — using 0% (conservative)")
            raw_growth = 0.0

        # Allow mild negative growth (-10%) for declining businesses; 25% ceiling.
        growth_rate = _clamp(raw_growth, -0.10, 0.25)

        # Terminal growth: 2-3% (long-run GDP)
        terminal_growth = 0.025

        # ── WACC — CAPM cost of equity + actual cost of debt ─────────────────
        de_ratio      = (info.get("debtToEquity") or 0) / 100   # yfinance: D/E×100
        weight_debt   = de_ratio / (1 + de_ratio)
        weight_equity = 1 - weight_debt

        cost_of_equity = _capm_cost_of_equity(info)   # Rf + Beta × 5.5% ERP

        # Actual cost of debt = interestExpense / totalDebt (from filings).
        # Falls back to leverage-tiered estimate when unavailable.
        interest_expense = abs(info.get("interestExpense") or 0)
        total_debt_raw   = info.get("totalDebt") or 0
        if interest_expense > 0 and total_debt_raw > 0:
            cost_of_debt = _clamp(interest_expense / total_debt_raw, 0.02, 0.15)
        elif de_ratio >= 2.0:   cost_of_debt = 0.08
        elif de_ratio >= 1.0:   cost_of_debt = 0.06
        else:                   cost_of_debt = 0.05

        tax_rate = _clamp(info.get("effectiveTaxRate") or 0.21, 0.0, 0.40)
        wacc = weight_equity * cost_of_equity + weight_debt * cost_of_debt * (1 - tax_rate)
        wacc = _clamp(wacc, 0.07, 0.15)

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
        terminal_value = fcf_terminal / (wacc - terminal_growth)
        pv_terminal    = terminal_value / (1 + wacc) ** n

        # ── Enterprise Value → Equity Value (subtract net debt) ───────────────
        # sum(pv_fcfs) + pv_terminal is the FCFF-based Enterprise Value.
        # Converting to equity value requires subtracting net financial debt.
        # Cash-rich companies (negative net debt) get a positive adjustment.
        total_debt    = info.get("totalDebt") or 0
        total_cash    = info.get("totalCash") or 0
        net_debt      = total_debt - total_cash
        equity_value  = sum(pv_fcfs) + pv_terminal - net_debt
        if equity_value <= 0:
            return None   # negative equity → company is over-leveraged; use P/S fallback

        intrinsic_per_share = equity_value / shares
        if intrinsic_per_share == 0:
            return None  # guard: tiny equity / huge share count → MoS undefined

        # ── Margin of safety ───────────────────────────────────────────────────
        mos_pct = (intrinsic_per_share - price) / intrinsic_per_share * 100

        # ── DCF Score (0-15 pts) ───────────────────────────────────────────────
        if mos_pct >= 40:    dcf_score = 15
        elif mos_pct >= 20:  dcf_score = 11
        elif mos_pct >= 5:   dcf_score = 7
        elif mos_pct >= -10: dcf_score = 3
        else:                dcf_score = 0

        ev_total = sum(pv_fcfs) + pv_terminal
        tv_pct = round(pv_terminal / ev_total * 100, 1) if ev_total != 0 else 0.0

        return {
            "intrinsic_value":    round(intrinsic_per_share, 2),
            "current_price":      round(price, 2),
            "margin_of_safety":   round(mos_pct, 1),
            "dcf_score":          dcf_score,
            "fcf_used_m":         round(fcf / 1e6, 1),
            "fcf_source":         fcf_source,
            "growth_rate_used":   round(growth_rate * 100, 1),
            "wacc_used":          round(wacc * 100, 1),
            "cost_of_equity_pct": round(cost_of_equity * 100, 1),
            "cost_of_debt_pct":   round(cost_of_debt * 100, 1),
            "terminal_growth":    round(terminal_growth * 100, 1),
            "terminal_value_pct": tv_pct,
            "net_debt_m":         round(net_debt / 1e6, 1),
            "upside_pct":         round(mos_pct, 1),
            "valuation":          _valuation_label(mos_pct),
            "growth_source":      growth_source,
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
