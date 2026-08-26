"""
Unit tests for src/weight_tuning_backtest.py — pure math only, no network or DB.

These pin the formula reconstruction (counterfactual_total must exactly
reproduce stock_scorer.score_stock()'s composite formula) and the
Fama-MacBeth-style monthly IC discipline, so a future refactor of either
this module or stock_scorer.py's scoring formula gets caught immediately
instead of silently producing wrong weight-tuning conclusions.
"""

import numpy as np
import pandas as pd
import pytest

from src.weight_tuning_backtest import (
    CAP_ONLY, CORE_COMPONENTS, EXACT_LINEAR, HARDCODED,
    component_frac, counterfactual_total, fit_ic_proportional_weights,
    fm_stats, monthly_ic, reconstruction_sanity_check,
)

# Matches src/stock_scorer.py's WEIGHTS dict exactly (duplicated here rather
# than imported, since importing stock_scorer.py pulls in a heavy
# dependency chain — TechnicalIndicators/`ta`, sklearn, statsmodels — that
# this module's own logic never touches).
WEIGHTS = {
    "rsi": 15, "macd": 15, "ma": 20, "volume": 10, "momentum": 10,
    "forecast": 15, "short_interest": 10, "institutional": 5, "insider": 5,
    "fundamentals": 10, "dcf": 15, "news_sentiment": 5, "trends": 5,
}


def _row(**overrides) -> pd.Series:
    """A 'full marks' row (every core component maxed) unless overridden."""
    base = {
        "ticker": "TEST", "date": pd.Timestamp("2026-01-15"), "score": 100.0,
        "raw_rsi": 15, "raw_macd": 15, "raw_ma": 20, "raw_volume": 10,
        "raw_momentum": 10, "raw_forecast": 0, "raw_short": 10,
        "raw_institutional": 5, "raw_insider": 5, "raw_fundamentals": 10,
        "raw_dcf": 15, "raw_news": 5, "raw_trends": 5, "squeeze_bonus": 0,
    }
    base.update(overrides)
    return pd.Series(base)


class TestComponentFrac:
    def test_exact_linear_full_marks_is_one(self):
        row = _row()
        for key in EXACT_LINEAR:
            assert component_frac(row, key, WEIGHTS) == pytest.approx(1.0)

    def test_exact_linear_half_marks_is_half(self):
        row = _row(raw_rsi=7.5)
        assert component_frac(row, "rsi", WEIGHTS) == pytest.approx(0.5)

    def test_cap_only_conservative_when_at_ceiling(self):
        """momentum/insider: sitting exactly at the scan-time weight could mean
        the true raw value was higher — frac must not claim more than 1.0."""
        row = _row(raw_momentum=WEIGHTS["momentum"])
        assert component_frac(row, "momentum", WEIGHTS) == 1.0

    def test_frac_never_exceeds_one(self):
        row = _row(raw_rsi=999)  # pathological input
        assert component_frac(row, "rsi", WEIGHTS) == 1.0

    def test_frac_never_negative(self):
        row = _row(raw_dcf=-5)  # pathological input
        assert component_frac(row, "dcf", WEIGHTS) == 0.0


class TestCounterfactualTotal:
    def test_full_marks_row_scores_100(self):
        row = _row()
        assert counterfactual_total(row, {}, WEIGHTS) == 100.0

    def test_matches_hand_computed_formula(self):
        """RSI at half quality, everything else maxed. core=107.5/115*90+10=94.13.."""
        row = _row(raw_rsi=7.5)
        assert counterfactual_total(row, {}, WEIGHTS) == pytest.approx(94.1, abs=0.05)

    def test_raising_weight_on_a_weak_component_dilutes_the_total(self):
        """The exact dynamic documented in CLAUDE.md: raising WEIGHTS['rsi']
        while RSI itself is weak (frac=0.5) LOWERS the composite, because
        core_max grows faster than the weak component's own contribution."""
        row = _row(raw_rsi=7.5)
        control = counterfactual_total(row, {}, WEIGHTS)
        raised = counterfactual_total(row, {"rsi": 30}, WEIGHTS)
        assert raised < control
        assert raised == pytest.approx(89.6, abs=0.05)

    def test_raising_weight_on_a_maxed_component_is_a_no_op(self):
        """All fracs = 1.0 at baseline, so core/core_max stays 1.0 regardless
        of how the budget is reallocated among fully-maxed components."""
        row = _row()
        assert counterfactual_total(row, {"rsi": 30}, WEIGHTS) == 100.0

    def test_never_exceeds_100(self):
        row = _row()
        assert counterfactual_total(row, {"rsi": 1000}, WEIGHTS) <= 100.0

    def test_bonus_capped_at_20(self):
        """news(5) + squeeze(15) + trends(5) = 25 raw, must clip to 20."""
        row = _row(raw_rsi=0, raw_macd=0, raw_ma=0, raw_volume=0, raw_momentum=0,
                    raw_short=0, raw_institutional=0, raw_insider=0,
                    raw_fundamentals=0, raw_dcf=0, squeeze_bonus=15)
        # core=0 → total is just the (capped) bonus.
        assert counterfactual_total(row, {}, WEIGHTS) == 20.0

    def test_missing_candidate_keys_fall_back_to_scan_time_weights(self):
        """Omitting a key from `candidate_weights` must be identical to
        explicitly re-specifying its current weights_at_scan value."""
        row = _row(raw_rsi=7.5)
        omitted = counterfactual_total(row, {"macd": 30}, WEIGHTS)
        explicit = counterfactual_total(row, {"macd": 30, "rsi": WEIGHTS["rsi"]}, WEIGHTS)
        assert omitted == pytest.approx(explicit)


class TestReconstructionSanityCheck:
    def test_zero_diff_when_weights_match_scan_time(self):
        df = pd.DataFrame([_row(), _row(raw_rsi=7.5, score=94.1)])
        result = reconstruction_sanity_check(df, WEIGHTS)
        assert result["mean_abs_diff"] < 0.1
        assert result["pct_within_1pt"] == 100.0

    def test_flags_mismatch_when_weights_dont_match_scan_time(self):
        """If WEIGHTS was different at scan time than what's passed in, the
        sanity check must catch it rather than silently producing garbage
        reweighting conclusions. Must use an assumed weight LARGER than the
        row's raw score (30, vs the true scan-time weight of 15) — a smaller
        assumed weight is absorbed by component_frac()'s [0,1] clip on this
        fully-maxed row and would not, by itself, prove anything."""
        df = pd.DataFrame([_row(score=100.0)])
        wrong_weights = {**WEIGHTS, "rsi": 30}  # row was actually scored assuming rsi=15
        result = reconstruction_sanity_check(df, wrong_weights)
        assert result["mean_abs_diff"] > 1.0
        assert result["pct_within_1pt"] < 100.0


class TestMonthlyIc:
    def _panel(self, n_months=6, n_tickers=8, correlated=True, seed=0):
        rng = np.random.default_rng(seed)
        rows = []
        for m in range(n_months):
            month_start = pd.Timestamp("2026-01-01") + pd.DateOffset(months=m)
            score = rng.uniform(0, 15, n_tickers)
            noise = rng.normal(0, 3.0, n_tickers)  # enough to vary IC month-to-month, not just ~1.0 every time
            ret = (score if correlated else rng.uniform(0, 15, n_tickers)) + noise
            for i in range(n_tickers):
                rows.append({
                    "ticker": f"T{i}", "date": month_start + pd.Timedelta(days=i),
                    "raw_x": score[i], "ret30": ret[i],
                })
        return pd.DataFrame(rows)

    def test_recovers_strong_positive_correlation(self):
        df = self._panel(correlated=True)
        m = monthly_ic(df, "raw_x", "ret30")
        s = fm_stats(m)
        assert s["months"] >= 5
        assert s["mean"] > 0.8   # near-perfect rank correlation by construction
        assert s["t"] > 2

    def test_no_correlation_gives_ic_near_zero(self):
        df = self._panel(correlated=False, seed=1)
        m = monthly_ic(df, "raw_x", "ret30")
        s = fm_stats(m)
        assert abs(s["mean"]) < 0.6  # noisy but should not look strongly directional

    def test_one_row_per_ticker_per_month(self):
        """Duplicate a ticker's row many times within one month — the IC for
        that month must be computed on ONE row per ticker, not inflated by
        near-duplicate observations."""
        df = self._panel(n_months=1, n_tickers=4, correlated=True)
        dup = pd.concat([df, df, df, df, df], ignore_index=True)  # x5 duplicates
        m_once = monthly_ic(df, "raw_x", "ret30", min_obs=3)
        m_dup = monthly_ic(dup, "raw_x", "ret30", min_obs=3)
        assert m_once["n"].iloc[0] == m_dup["n"].iloc[0] == 4

    def test_too_few_months_returns_empty_stats(self):
        df = self._panel(n_months=1)
        s = fm_stats(monthly_ic(df, "raw_x", "ret30"))
        assert s["months"] < 3


class TestFitIcProportionalWeights:
    def _train(self):
        rng = np.random.default_rng(2)
        rows = []
        for m in range(6):
            month_start = pd.Timestamp("2026-01-01") + pd.DateOffset(months=m)
            for i in range(10):
                good = rng.uniform(0, 15)
                rows.append({
                    "ticker": f"T{i}", "date": month_start + pd.Timedelta(days=i),
                    "raw_rsi": good, "raw_macd": rng.uniform(0, 15),
                    "ret30": good + rng.normal(0, 0.01),
                })
        return pd.DataFrame(rows)

    def test_allocates_more_budget_to_the_predictive_component(self):
        train = self._train()
        fitted = fit_ic_proportional_weights(
            train, ("rsi", "macd"), WEIGHTS, ret_col="ret30"
        )
        assert fitted["rsi"] > fitted["macd"]

    def test_preserves_total_budget(self):
        train = self._train()
        fitted = fit_ic_proportional_weights(train, ("rsi", "macd"), WEIGHTS, ret_col="ret30")
        expected_budget = WEIGHTS["rsi"] + WEIGHTS["macd"]
        assert sum(fitted.values()) == pytest.approx(expected_budget, abs=0.2)

    def test_falls_back_to_equal_weight_with_no_signal(self):
        """Both components unambiguously anti-correlated with the return (by
        construction, not just noisy-near-zero) — both monthly ICs should
        clip to 0 reliably, every month, landing deterministically in the
        equal-split fallback branch rather than the proportional-allocation
        one."""
        rng = np.random.default_rng(3)
        rows = []
        for m in range(6):
            month_start = pd.Timestamp("2026-01-01") + pd.DateOffset(months=m)
            for i in range(10):
                r, mc = rng.uniform(0, 15), rng.uniform(0, 15)
                rows.append({
                    "ticker": f"T{i}", "date": month_start + pd.Timedelta(days=i),
                    "raw_rsi": r, "raw_macd": mc,
                    "ret30": -(r + mc),  # unambiguously anti-correlated with BOTH
                })
        train = pd.DataFrame(rows)
        fitted = fit_ic_proportional_weights(train, ("rsi", "macd"), WEIGHTS, ret_col="ret30")
        assert fitted["rsi"] == pytest.approx(fitted["macd"], abs=0.1)


class TestComponentCategorization:
    def test_categories_are_disjoint_and_cover_core(self):
        all_core = set(EXACT_LINEAR) | set(CAP_ONLY) | set(HARDCODED)
        assert all_core == set(CORE_COMPONENTS)
        assert len(all_core) == len(EXACT_LINEAR) + len(CAP_ONLY) + len(HARDCODED)
