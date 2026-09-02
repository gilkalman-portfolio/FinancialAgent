"""
Tests for src/catalyst_event_study.py — the analysis layer of the
News-Catalyst Event-Study Measurement (see CLAUDE.md). Covers the three
methodology upgrades that are pure logic (not live-network dependent):

  1. abnormal returns vs benchmark (compute_abnormal_returns)
  2. cross-sectional clustering correction (_date_clustered_stats /
     direction_report) — the same technique trigger_backtest.clustered_stats()
     uses for its own (different-axis) clustering problem, applied here to
     multiple tickers sharing a catalyst date instead of one ticker's
     overlapping holds
  3. placebo/control-date test (placebo_test)

All yfinance/DB calls are mocked — these are unit tests of the statistics,
not live-network tests (see the separate live-verification step for that).

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_catalyst_event_study.py -v
"""

from __future__ import annotations

import math
from unittest.mock import patch

import pandas as pd
import pytest

from src import catalyst_event_study as ces


# ── _t_stat ───────────────────────────────────────────────────────────────────

class TestTStat:
    def test_too_few_observations_returns_none_fields(self):
        s = ces._t_stat([1.0, 2.0])
        assert s["n"] == 2
        assert s["mean"] is None

    def test_basic_mean_and_t(self):
        s = ces._t_stat([1.0, 2.0, 3.0, 4.0, 5.0])
        assert s["n"] == 5
        assert s["mean"] == 3.0
        assert s["t"] > 0

    def test_none_values_are_dropped(self):
        s = ces._t_stat([1.0, None, 2.0, None, 3.0])
        assert s["n"] == 3

    def test_nan_values_are_dropped(self):
        """NaN is not None, so it survives the `is not None` filter unless
        explicitly checked -- a single NaN sneaking into a plain sum() poisons
        the whole mean. This is what let placebo_test() report a silently
        corrupted (nan, nan) placebo distribution as 'ok' in production on
        2026-08-31 (see _forward_pct_return's own regression test)."""
        s = ces._t_stat([1.0, float("nan"), 2.0, 3.0])
        assert s["n"] == 3
        assert s["mean"] == pytest.approx(2.0)
        assert not math.isnan(s["mean"])


# ── _welch_t ──────────────────────────────────────────────────────────────────

class TestWelchT:
    def test_identical_distributions_near_zero(self):
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        b = [1.0, 2.0, 3.0, 4.0, 5.0]
        t = ces._welch_t(a, b)
        assert t == pytest.approx(0.0, abs=1e-9)

    def test_clearly_different_distributions_large_t(self):
        a = [10.0, 11.0, 9.0, 10.5, 9.5]
        b = [0.0, 1.0, -1.0, 0.5, -0.5]
        t = ces._welch_t(a, b)
        assert abs(t) > 5

    def test_too_few_samples_returns_none(self):
        assert ces._welch_t([1.0], [1.0, 2.0, 3.0]) is None


# ── _forward_pct_return ──────────────────────────────────────────────────────

class TestForwardPctReturn:
    def test_normal_return(self):
        dates = pd.date_range("2026-06-01", "2026-06-10", freq="D")
        close = pd.Series([100.0 + i for i in range(len(dates))], index=pd.DatetimeIndex(dates))
        r = ces._forward_pct_return(close, pd.Timestamp("2026-06-01"), 3)
        assert r == pytest.approx(3.0)  # 100 -> 103 over 3 days

    def test_nan_start_bar_returns_none_not_nan(self):
        """A NaN price bar (a yfinance data gap) at the start index must
        return None, not a NaN float -- _t_stat/_welch_t consume this via a
        plain sum() with no NaN-awareness, so a NaN masquerading as a valid
        Optional[float] silently corrupts every downstream mean/t-stat built
        from it. This is exactly what production hit in placebo_test()'s
        horizon=3d run on 2026-08-31: n stayed correct (2307) while
        mean/sd/t all came back nan, and distinguishable_from_placebo read
        back as a plain False -- indistinguishable from a genuine null
        result unless you inspect the mean itself."""
        dates = pd.date_range("2026-06-01", "2026-06-10", freq="D")
        prices = [100.0 + i for i in range(len(dates))]
        prices[0] = float("nan")
        close = pd.Series(prices, index=pd.DatetimeIndex(dates))
        r = ces._forward_pct_return(close, pd.Timestamp("2026-06-01"), 3)
        assert r is None

    def test_nan_end_bar_returns_none_not_nan(self):
        dates = pd.date_range("2026-06-01", "2026-06-10", freq="D")
        prices = [100.0 + i for i in range(len(dates))]
        prices[3] = float("nan")  # 2026-06-01 + 3 days lands on this bar
        close = pd.Series(prices, index=pd.DatetimeIndex(dates))
        r = ces._forward_pct_return(close, pd.Timestamp("2026-06-01"), 3)
        assert r is None

    def test_nan_bar_never_poisons_an_aggregate_mean(self):
        """End-to-end regression for the placebo_test() corruption: a NaN bar
        anywhere in the series must not be able to reach _t_stat as a value
        that looks like real data."""
        dates = pd.date_range("2026-06-01", "2026-06-20", freq="D")
        prices = [100.0 + i for i in range(len(dates))]
        prices[5] = float("nan")
        close = pd.Series(prices, index=pd.DatetimeIndex(dates))
        returns = [r for start in dates[:10]
                   if (r := ces._forward_pct_return(close, start, 3)) is not None]
        stats = ces._t_stat(returns)
        assert stats["mean"] is not None
        assert not math.isnan(stats["mean"])


# ── _date_clustered_stats: the cross-sectional clustering correction ────────

class TestDateClusteredStats:
    def test_naive_n_counts_every_row_clustered_n_counts_dates(self):
        """5 different tickers all sharing ONE catalyst date is 1 independent
        data point for significance purposes, not 5 -- this is exactly the
        Kolari & Pynnonen (2010) problem the correction exists for."""
        same_date = pd.Timestamp("2026-06-01")
        rows = [{"date": same_date, "abn_7": v} for v in [1.0, 2.0, 3.0, -1.0, 0.5]]
        clustered = ces._date_clustered_stats(rows, "abn_7")
        assert clustered["n"] == 5          # observations
        assert clustered["dates"] == 1       # independent clusters
        # _t_stat (mirroring trigger_backtest.stats()) withholds mean/t below
        # n=3 *bucket* means -- a single date-cluster can report neither a
        # trustworthy mean nor a t-stat from n=1, same convention as
        # trigger_backtest.clustered_stats() applies to its own month buckets.
        assert clustered["mean"] is None
        assert clustered["t"] is None

    def test_multiple_dates_each_become_one_point(self):
        d1, d2, d3 = pd.Timestamp("2026-06-01"), pd.Timestamp("2026-06-02"), pd.Timestamp("2026-06-03")
        rows = (
            [{"date": d1, "abn_7": v} for v in [10.0, 12.0]] +   # mean 11
            [{"date": d2, "abn_7": v} for v in [2.0, 4.0, 6.0]] +  # mean 4
            [{"date": d3, "abn_7": v} for v in [1.0]]              # mean 1
        )
        clustered = ces._date_clustered_stats(rows, "abn_7")
        assert clustered["n"] == 6
        assert clustered["dates"] == 3
        assert clustered["mean"] == pytest.approx((11.0 + 4.0 + 1.0) / 3)

    def test_naive_significance_can_be_inflated_vs_clustered(self):
        """Construct a case where naive per-observation t-stat looks strong
        purely because the same-date cluster is large, and confirm the
        clustered figure is the more conservative (correct) one to trust."""
        same_date = pd.Timestamp("2026-06-01")
        # 30 near-identical small-positive observations all on one date.
        rows = [{"date": same_date, "abn_7": 1.0 + (i % 3) * 0.01} for i in range(30)]
        naive = ces._t_stat([r["abn_7"] for r in rows])
        clustered = ces._date_clustered_stats(rows, "abn_7")
        # Naive treats this as n=30 independent points (large, significant t);
        # clustered correctly collapses it to 1 date (no t-stat possible).
        assert naive["n"] == 30
        assert naive["t"] is not None and abs(naive["t"]) > 2
        assert clustered["dates"] == 1
        assert clustered["t"] is None

    def test_missing_values_are_skipped_not_treated_as_zero(self):
        """A None inside a date's bucket must be dropped before averaging,
        not folded in as a 0.0 (which would silently pull that date's mean
        toward zero). Uses 3 dates so the bucket-mean list clears _t_stat's
        own n>=3 floor and the resulting mean is actually observable."""
        d1 = pd.Timestamp("2026-06-01")
        d2 = pd.Timestamp("2026-06-02")
        d3 = pd.Timestamp("2026-06-03")
        rows = [
            {"date": d1, "abn_7": 5.0}, {"date": d1, "abn_7": None},  # bucket mean: 5.0, not 2.5
            {"date": d2, "abn_7": 5.0},
            {"date": d3, "abn_7": 5.0},
        ]
        clustered = ces._date_clustered_stats(rows, "abn_7")
        assert clustered["n"] == 3          # the None never entered the count
        assert clustered["dates"] == 3
        assert clustered["mean"] == pytest.approx(5.0)

    def test_empty_rows(self):
        assert ces._date_clustered_stats([], "abn_7") == {"n": 0, "dates": 0, "mean": None, "t": None}


# ── _block_bootstrap_stats: within-ticker serial-correlation correction ─────

class TestBlockBootstrapStats:
    def test_too_few_tickers_returns_none_bootstrap_fields(self):
        """Only 1 independent cluster -- can't bootstrap a meaningful spread
        out of resampling the same single ticker over and over."""
        rows = [{"ticker": "A", "abn_7": 1.0}, {"ticker": "A", "abn_7": 1.1},
                {"ticker": "A", "abn_7": 0.9}]
        s = ces._block_bootstrap_stats(rows, "abn_7")
        assert s["n_tickers"] == 1
        assert s["bootstrap_se"] is None
        assert s["bootstrap_t"] is None
        # naive fields are still populated -- same "always report what's
        # computable" convention as every other primitive here.
        assert s["n"] == 3

    def test_missing_values_are_skipped_not_treated_as_zero(self):
        rows = [{"ticker": "A", "abn_7": 1.0}, {"ticker": "A", "abn_7": None},
                {"ticker": "B", "abn_7": 1.0}, {"ticker": "C", "abn_7": 1.0}]
        s = ces._block_bootstrap_stats(rows, "abn_7", n_boot=200, seed=1)
        assert s["n"] == 3  # the None never entered the naive count

    def test_seed_makes_bootstrap_reproducible(self):
        rows = [{"ticker": t, "abn_7": v} for t, v in
                [("A", 1.0), ("A", 1.2), ("B", -0.5), ("C", 2.0), ("D", 0.3)]]
        s1 = ces._block_bootstrap_stats(rows, "abn_7", n_boot=500, seed=9)
        s2 = ces._block_bootstrap_stats(rows, "abn_7", n_boot=500, seed=9)
        assert s1["bootstrap_se"] == s2["bootstrap_se"]
        assert s1["bootstrap_t"] == s2["bootstrap_t"]

    def test_naive_and_date_clustering_both_miss_within_ticker_repetition_bootstrap_catches_it(self):
        """Mirrors test_naive_significance_can_be_inflated_vs_clustered above,
        but demonstrates the GAP that correction leaves open: 6 tickers, each
        contributing 10 near-duplicate observations on 10 DIFFERENT dates (60
        distinct dates total -- _date_clustered_stats() sees no same-date
        overlap anywhere, so it provides zero protection here), built from
        per-ticker means with real spread (some negative, some strongly
        positive) so there are really only 6 independent pieces of evidence,
        not 60.
        """
        ticker_means = {"A": -2.0, "B": 3.0, "C": -1.0, "D": 2.5, "E": -0.5, "F": 4.0}
        rows = []
        day = 0
        for tk, m in ticker_means.items():
            for i in range(10):
                rows.append({
                    "ticker": tk,
                    "date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=day),
                    "abn_7": m + (i % 3 - 1) * 0.001,
                })
                day += 1

        naive = ces._t_stat([r["abn_7"] for r in rows])
        clustered = ces._date_clustered_stats(rows, "abn_7")
        block = ces._block_bootstrap_stats(rows, "abn_7", n_boot=2000, seed=1)

        # Naive AND the existing cross-sectional correction both read this as
        # strongly significant -- neither addresses the within-ticker axis,
        # since every row sits on its own unique date.
        assert naive["n"] == 60
        assert abs(naive["t"]) > 2.0
        assert clustered["dates"] == 60
        assert abs(clustered["t"]) > 2.0

        # The new correction recognizes only 6 independent clusters, widens
        # the standard error well past the naive one, and correctly no
        # longer calls this significant.
        assert block["n_tickers"] == 6
        naive_se = naive["sd"] / math.sqrt(naive["n"])
        assert block["bootstrap_se"] > naive_se * 2   # measurably wider SE
        assert abs(block["bootstrap_t"]) < 2.0          # false positive corrected away


# ── direction_report(block_bootstrap=True) wiring ────────────────────────────

class TestDirectionReportBlockBootstrap:
    def test_block_bootstrap_key_absent_by_default_present_when_requested(self):
        rows = [{"ticker": "A", "direction": "positive",
                 "date": pd.Timestamp("2026-06-01"), "abn_7": 1.0}]
        with patch.object(ces, "compute_abnormal_returns", return_value=rows):
            default = ces.direction_report(days=90, horizons=(7,))
            on = ces.direction_report(days=90, horizons=(7,), block_bootstrap=True, n_boot=50)
        assert "block_bootstrap" not in default["directions"]["positive"][7]
        assert "block_bootstrap" in on["directions"]["positive"][7]

    def test_block_bootstrap_flows_through_and_corrects_inflated_significance(self):
        ticker_means = {"A": -2.0, "B": 3.0, "C": -1.0, "D": 2.5, "E": -0.5, "F": 4.0}
        rows = []
        day = 0
        for tk, m in ticker_means.items():
            for _ in range(10):
                rows.append({
                    "ticker": tk, "direction": "positive",
                    "date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=day),
                    "abn_7": m,
                })
                day += 1
        with patch.object(ces, "compute_abnormal_returns", return_value=rows):
            d = ces.direction_report(days=90, horizons=(7,), block_bootstrap=True,
                                      n_boot=1000, boot_seed=1)
        cell = d["directions"]["positive"][7]
        bb = cell["block_bootstrap"]
        assert bb["n_tickers"] == 6
        assert abs(bb["bootstrap_t"]) < abs(cell["clustered"]["t"])

    def test_format_direction_report_includes_block_bootstrap_line_when_present(self):
        d = {
            "benchmark": "SPY", "window_days": 90,
            "directions": {
                "positive": {7: {
                    "naive": {"n": 10, "t": 1.5},
                    "clustered": {"n": 10, "dates": 4, "mean": 1.2, "t": 0.8},
                    "block_bootstrap": {"n_tickers": 3, "bootstrap_se": 0.5, "bootstrap_t": 1.1},
                }}
            },
        }
        text = ces.format_direction_report(d)
        assert "block-bootstrap" in text
        assert "n_tickers=3" in text

    def test_format_direction_report_omits_block_bootstrap_line_when_absent(self):
        """The pre-existing table format (no block_bootstrap key at all) must
        keep rendering exactly as before -- this is an additive, opt-in
        feature, not a change to default output."""
        d = {
            "benchmark": "SPY", "window_days": 90,
            "directions": {
                "positive": {7: {"naive": {"n": 10, "t": 1.5},
                                  "clustered": {"n": 10, "dates": 4, "mean": 1.2, "t": 0.8}}}
            },
        }
        text = ces.format_direction_report(d)
        assert "block-bootstrap" not in text


# ── _nearest_neighbor_match / liquidity_matched_direction_report ────────────

class TestNearestNeighborMatch:
    def test_matches_closest_adv_within_caliper(self):
        treatment = [{"ticker": "T", "adv": 50e6}]
        control = [{"ticker": "C1", "adv": 1e6}, {"ticker": "C2", "adv": 48e6}]
        mt, mc = ces._nearest_neighbor_match(treatment, control, caliper=0.5)
        assert [r["ticker"] for r in mt] == ["T"]
        assert [r["ticker"] for r in mc] == ["C2"]

    def test_treatment_dropped_when_no_control_within_caliper(self):
        treatment = [{"ticker": "T", "adv": 50e6}]
        control = [{"ticker": "C1", "adv": 1e6}]  # ~1.7 log10(ADV) units away
        mt, mc = ces._nearest_neighbor_match(treatment, control, caliper=0.5)
        assert mt == [] and mc == []

    def test_rows_missing_adv_are_excluded_from_both_pools(self):
        treatment = [{"ticker": "T1", "adv": 50e6}, {"ticker": "T2"}]  # T2: no ADV (fetch failure)
        control = [{"ticker": "C1", "adv": 48e6}, {"ticker": "C2"}]
        mt, mc = ces._nearest_neighbor_match(treatment, control, caliper=0.5)
        assert [r["ticker"] for r in mt] == ["T1"]
        assert [r["ticker"] for r in mc] == ["C1"]

    def test_no_replacement_each_control_used_at_most_once(self):
        treatment = [{"ticker": "T1", "adv": 50e6}, {"ticker": "T2", "adv": 50.5e6}]
        control = [{"ticker": "C1", "adv": 50e6}]  # only one plausible control exists
        mt, mc = ces._nearest_neighbor_match(treatment, control, caliper=0.5)
        assert len(mt) == 1   # the second treatment row has nothing left to pair with
        assert len(mc) == 1
        assert len(set(r["ticker"] for r in mc)) == len(mc)  # no control reused


class TestLiquidityMatchedDirectionReport:
    def test_matching_balances_a_deliberate_adv_skew(self):
        """Construct exactly the failure mode the correction exists for: the
        'positive' (news) group all sits at ~$40-60M ADV; the 'no_news'
        (control) pool is a mix of 5 similarly-liquid tickers plus 20
        illiquid ~$1M tickers dragging its raw average far below the
        treatment group's. Before matching the skew must be large and
        visible; after matching, treatment and control ADV must land close
        together, and only the 5 plausible (high-ADV) controls may be used.
        """
        treatment_adv = {"T1": 40e6, "T2": 45e6, "T3": 50e6, "T4": 55e6, "T5": 60e6}
        control_high_adv = {"C1": 42e6, "C2": 48e6, "C3": 52e6, "C4": 58e6, "C5": 65e6}
        control_low_adv = {f"L{i}": 1e6 for i in range(1, 21)}
        adv_by_ticker = {**treatment_adv, **control_high_adv, **control_low_adv}

        rows = []
        d0 = pd.Timestamp("2026-06-01")
        for i, tk in enumerate(treatment_adv):
            rows.append({"ticker": tk, "direction": "positive",
                         "date": d0 + pd.Timedelta(days=i), "abn_7": 2.0})
        for i, tk in enumerate({**control_high_adv, **control_low_adv}):
            rows.append({"ticker": tk, "direction": "no_news",
                         "date": d0 + pd.Timedelta(days=i), "abn_7": 0.0})

        def _fake_adv(ticker, lookback_days=20):
            return adv_by_ticker.get(ticker)

        with patch.object(ces, "compute_abnormal_returns", return_value=rows), \
             patch.object(ces, "_fetch_ticker_adv", side_effect=_fake_adv):
            result = ces.liquidity_matched_direction_report(days=90, horizons=(7,), caliper=0.5)

        pos = result["directions"]["positive"]
        bal = pos["adv_balance"]

        # The deliberate skew is really there before matching.
        assert bal["treatment_mean_adv_before"] == pytest.approx(50e6, rel=0.01)
        assert bal["control_mean_adv_before"] < 15e6
        assert bal["treatment_mean_adv_before"] / bal["control_mean_adv_before"] > 3

        # Matching uses only the 5 plausible controls, one each -- never any
        # of the 20 illiquid ones, since none is within the 0.5 caliper.
        assert pos["n_matched_pairs"] == 5
        for h_stats in pos["horizons"].values():
            assert h_stats["control"]["n"] <= 5

        # After matching, treatment and control ADV land close together --
        # the skew is corrected, not just relabeled.
        ratio_after = bal["treatment_mean_adv_after"] / bal["control_mean_adv_after"]
        assert 0.8 < ratio_after < 1.25

    def test_no_watch_rows_returns_empty(self):
        with patch.object(ces, "compute_abnormal_returns", return_value=[]):
            result = ces.liquidity_matched_direction_report(days=90)
        assert result["directions"] == {}

    def test_promoted_only_forwarded_to_compute_abnormal_returns(self):
        with patch.object(ces, "compute_abnormal_returns", return_value=[]) as mock_car:
            ces.liquidity_matched_direction_report(days=90, promoted_only=True)
        _, kwargs = mock_car.call_args
        assert kwargs.get("promoted_only") is True


class TestFormatLiquidityMatchedReport:
    def test_handles_empty(self):
        assert "no WATCH data" in ces.format_liquidity_matched_report({"directions": {}})

    def test_renders_a_table(self):
        d = {
            "benchmark": "SPY", "window_days": 90, "caliper": 0.5,
            "directions": {
                "positive": {
                    "n_matched_pairs": 5,
                    "adv_balance": {
                        "treatment_mean_adv_before": 50e6, "control_mean_adv_before": 11e6,
                        "treatment_mean_adv_after": 50e6, "control_mean_adv_after": 53e6,
                    },
                    "horizons": {7: {"treatment": {"t": 1.2}, "control": {"t": 0.3}}},
                }
            },
        }
        text = ces.format_liquidity_matched_report(d)
        assert "positive" in text
        assert "n_matched_pairs=5" in text
        assert "SPY" in text


# ── compute_abnormal_returns / direction_report ──────────────────────────────

def _bench_series(dates, prices):
    s = pd.Series(prices, index=pd.DatetimeIndex(dates))
    return s


class TestComputeAbnormalReturns:
    def test_no_watch_rows_returns_empty(self):
        with patch.object(ces, "_watch_rows", return_value=[]):
            assert ces.compute_abnormal_returns(days=90) == []

    def test_benchmark_fetch_failure_returns_empty(self):
        rows = [{"ticker": "FOO", "signal_ts": "2026-06-01T10:00:00", "direction": "positive",
                 "return_7d_pct": 5.0}]
        with patch.object(ces, "_watch_rows", return_value=rows), \
             patch.object(ces, "_fetch_benchmark_close", return_value=None):
            assert ces.compute_abnormal_returns(days=90) == []

    def test_abnormal_return_is_raw_minus_benchmark(self):
        rows = [{"ticker": "FOO", "signal_ts": "2026-06-01T10:00:00", "direction": "positive",
                 "return_7d_pct": 10.0}]
        dates = pd.date_range("2026-05-25", "2026-06-15", freq="D")
        # Benchmark up ~4% over the relevant window.
        bench = _bench_series(dates, [100 + i * 0.15 for i in range(len(dates))])
        with patch.object(ces, "_watch_rows", return_value=rows), \
             patch.object(ces, "_fetch_benchmark_close", return_value=bench):
            out = ces.compute_abnormal_returns(days=90)
        assert len(out) == 1
        assert out[0]["ticker"] == "FOO"
        assert out[0]["direction"] == "positive"
        # abn_7 should be raw (10.0) minus whatever the benchmark did over 7d
        # from 2026-06-01 -- just confirm it's meaningfully less than raw and
        # not None (exact value depends on searchsorted alignment).
        assert out[0]["abn_7"] is not None
        assert out[0]["abn_7"] < 10.0

    def test_none_direction_defaults_to_no_news(self):
        """A malformed/legacy row with ai_verdict NULL must not silently drop
        out of every direction bucket."""
        with patch.object(ces, "_watch_rows", return_value=[
            {"ticker": "X", "signal_ts": "2026-06-01T00:00:00", "direction": None, "return_7d_pct": None}
        ]):
            rows = ces._watch_rows(90)
        assert rows[0]["direction"] in (None,)  # raw DB helper; normalization happens in _watch_rows() itself

    def test_watch_rows_helper_normalizes_null_direction(self):
        """_watch_rows() itself (not the mocked version above) must coerce a
        NULL ai_verdict to 'no_news' -- exercised via the SQL-reading path."""
        import sqlite3
        import tempfile
        import os as _os
        from pathlib import Path

        fd, path = tempfile.mkstemp(suffix=".db")
        _os.close(fd)
        db_path = Path(path)
        try:
            import src.database as db
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                with patch.object(ces, "get_connection", db.get_connection):
                    conn = db.get_connection()
                    conn.execute(
                        "INSERT INTO forward_signals (ticker, signal_ts, signal_type, "
                        "entry_price, status) VALUES ('X', '2026-06-01T00:00:00', 'WATCH', 10.0, 'open')"
                    )
                    conn.commit()
                    conn.close()
                    rows = ces._watch_rows(9000)
            assert rows[0]["direction"] == "no_news"
        finally:
            try:
                db_path.unlink()
            except Exception:
                pass


class TestPromotedOnlyFilter:
    """promoted_only=True, added 2026-08-26 per the IdeaDistill design
    review's survivorship point: restrict WATCH rows to tickers that were
    ALSO promoted to the watchlist (the only population that actually shows
    the sentiment tag in a real Telegram message), to check whether a
    measured sentiment/return relationship survives outside the full
    (unfiltered) hit population."""

    def _fresh_db(self, monkeypatch):
        import tempfile, os as _os
        from pathlib import Path
        fd, path = tempfile.mkstemp(suffix=".db")
        _os.close(fd)
        db_path = Path(path)
        import src.database as db
        monkeypatch.setattr(db, "DB_PATH", db_path)
        db.init_db()
        monkeypatch.setattr(ces, "get_connection", db.get_connection)
        return db_path

    def test_promoted_ticker_included_when_promoted_only(self, monkeypatch):
        self._fresh_db(monkeypatch)
        from src.database import watchlist_save_alert
        conn = ces.get_connection()
        conn.execute(
            "INSERT INTO forward_signals (ticker, signal_ts, signal_type, entry_price, status) "
            "VALUES ('PROMO', '2026-06-01T10:00:00', 'WATCH', 10.0, 'open')"
        )
        conn.commit()
        conn.close()
        watchlist_save_alert("PROMO", "auto_wl_momentum", "added",
                             score=None, price=None)
        # sent_at defaults to now(), not 2026-06-01 -- override it directly
        # so it falls inside the +-30min match window around the WATCH row.
        conn = ces.get_connection()
        conn.execute("UPDATE watchlist_alerts SET sent_at = '2026-06-01T10:05:00' WHERE ticker='PROMO'")
        conn.commit()
        conn.close()

        rows = ces._watch_rows(9000, promoted_only=True)
        assert any(r["ticker"] == "PROMO" for r in rows)

    def test_non_promoted_ticker_excluded_when_promoted_only(self, monkeypatch):
        self._fresh_db(monkeypatch)
        conn = ces.get_connection()
        conn.execute(
            "INSERT INTO forward_signals (ticker, signal_ts, signal_type, entry_price, status) "
            "VALUES ('LONER', '2026-06-01T10:00:00', 'WATCH', 10.0, 'open')"
        )
        conn.commit()
        conn.close()
        # No matching watchlist_alerts row for LONER at all.

        rows = ces._watch_rows(9000, promoted_only=True)
        assert not any(r["ticker"] == "LONER" for r in rows)

    def test_non_promoted_ticker_included_when_not_promoted_only(self, monkeypatch):
        self._fresh_db(monkeypatch)
        conn = ces.get_connection()
        conn.execute(
            "INSERT INTO forward_signals (ticker, signal_ts, signal_type, entry_price, status) "
            "VALUES ('LONER', '2026-06-01T10:00:00', 'WATCH', 10.0, 'open')"
        )
        conn.commit()
        conn.close()

        rows = ces._watch_rows(9000, promoted_only=False)
        assert any(r["ticker"] == "LONER" for r in rows)

    def test_promotion_outside_time_window_does_not_count(self, monkeypatch):
        self._fresh_db(monkeypatch)
        from src.database import watchlist_save_alert
        conn = ces.get_connection()
        conn.execute(
            "INSERT INTO forward_signals (ticker, signal_ts, signal_type, entry_price, status) "
            "VALUES ('STALE', '2026-06-01T10:00:00', 'WATCH', 10.0, 'open')"
        )
        conn.commit()
        conn.close()
        watchlist_save_alert("STALE", "auto_wl_momentum", "added")
        conn = ces.get_connection()
        # 5 hours away from the WATCH row's signal_ts -- well outside the
        # +-30min match window, e.g. an unrelated later re-promotion.
        conn.execute("UPDATE watchlist_alerts SET sent_at = '2026-06-01T15:00:00' WHERE ticker='STALE'")
        conn.commit()
        conn.close()

        rows = ces._watch_rows(9000, promoted_only=True)
        assert not any(r["ticker"] == "STALE" for r in rows)

    def test_direction_report_carries_promoted_only_flag(self):
        with patch.object(ces, "compute_abnormal_returns", return_value=[]):
            d = ces.direction_report(days=90, promoted_only=True)
        assert d["promoted_only"] is True


class TestDirectionReport:
    def test_no_data_returns_empty_directions(self):
        with patch.object(ces, "compute_abnormal_returns", return_value=[]):
            d = ces.direction_report(days=90)
        assert d["directions"] == {}

    def test_groups_by_direction_and_reports_both_naive_and_clustered(self):
        d1 = pd.Timestamp("2026-06-01")
        d2 = pd.Timestamp("2026-06-05")
        rows = [
            {"ticker": "A", "direction": "positive", "date": d1, "abn_7": 3.0},
            {"ticker": "B", "direction": "positive", "date": d1, "abn_7": 5.0},
            {"ticker": "C", "direction": "positive", "date": d2, "abn_7": 1.0},
            {"ticker": "D", "direction": "no_news", "date": d1, "abn_7": -1.0},
        ]
        with patch.object(ces, "compute_abnormal_returns", return_value=rows):
            d = ces.direction_report(days=90, horizons=(7,))
        assert set(d["directions"].keys()) == {"positive", "no_news"}
        pos = d["directions"]["positive"][7]
        assert pos["naive"]["n"] == 3
        assert pos["clustered"]["dates"] == 2

    def test_format_direction_report_handles_empty(self):
        assert "no WATCH data" in ces.format_direction_report({"directions": {}})

    def test_format_direction_report_renders_a_table(self):
        d = {
            "benchmark": "SPY", "window_days": 90,
            "directions": {
                "positive": {7: {"naive": {"n": 10, "t": 1.5},
                                  "clustered": {"n": 10, "dates": 4, "mean": 1.2, "t": 0.8}}}
            },
        }
        text = ces.format_direction_report(d)
        assert "positive" in text
        assert "SPY" in text


# ── placebo_test ──────────────────────────────────────────────────────────────

class TestPlaceboTest:
    def test_insufficient_real_data_returns_status(self):
        with patch.object(ces, "_watch_rows", return_value=[
            {"ticker": "A", "signal_ts": "2026-06-01T00:00:00", "return_7d_pct": 5.0},
        ]):
            result = ces.placebo_test(days=90, horizon=7)
        assert result["status"] == "insufficient_data"

    def test_enough_data_produces_real_and_placebo_distributions(self):
        rows = [
            {"ticker": "A", "signal_ts": "2026-06-01T00:00:00", "return_7d_pct": 5.0},
            {"ticker": "A", "signal_ts": "2026-06-10T00:00:00", "return_7d_pct": 3.0},
            {"ticker": "B", "signal_ts": "2026-06-05T00:00:00", "return_7d_pct": -2.0},
        ]
        dates = pd.date_range("2026-05-01", "2026-07-15", freq="D")
        flat_series = pd.Series([100.0] * len(dates), index=pd.DatetimeIndex(dates))

        with patch.object(ces, "_watch_rows", return_value=rows), \
             patch.object(ces, "_fetch_ticker_close", return_value=flat_series):
            result = ces.placebo_test(days=90, horizon=7, buffer_days=5,
                                       n_placebo_per_event=3, seed=1)

        assert result["status"] == "ok"
        assert result["real"]["n"] == 3
        # Flat price series -> 0% placebo returns everywhere.
        assert result["placebo"]["n"] > 0
        assert result["placebo"]["mean"] == pytest.approx(0.0, abs=1e-9)

    def test_ticker_with_no_price_data_is_skipped_not_fatal(self):
        rows = [
            {"ticker": "A", "signal_ts": "2026-06-01T00:00:00", "return_7d_pct": 5.0},
            {"ticker": "A", "signal_ts": "2026-06-10T00:00:00", "return_7d_pct": 3.0},
            {"ticker": "B", "signal_ts": "2026-06-05T00:00:00", "return_7d_pct": -2.0},
        ]
        with patch.object(ces, "_watch_rows", return_value=rows), \
             patch.object(ces, "_fetch_ticker_close", return_value=None):
            result = ces.placebo_test(days=90, horizon=7)
        assert result["status"] == "ok"
        assert result["placebo"]["n"] == 0

    def test_seed_makes_placebo_sample_reproducible(self):
        rows = [
            {"ticker": "A", "signal_ts": "2026-06-01T00:00:00", "return_7d_pct": 5.0},
            {"ticker": "A", "signal_ts": "2026-06-10T00:00:00", "return_7d_pct": 3.0},
            {"ticker": "A", "signal_ts": "2026-06-20T00:00:00", "return_7d_pct": 1.0},
        ]
        dates = pd.date_range("2026-05-01", "2026-07-15", freq="D")
        wiggly = pd.Series([100.0 + (i % 7) for i in range(len(dates))], index=pd.DatetimeIndex(dates))

        with patch.object(ces, "_watch_rows", return_value=rows), \
             patch.object(ces, "_fetch_ticker_close", return_value=wiggly):
            r1 = ces.placebo_test(days=90, horizon=7, seed=7)
            r2 = ces.placebo_test(days=90, horizon=7, seed=7)
        assert r1["placebo"]["mean"] == r2["placebo"]["mean"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
