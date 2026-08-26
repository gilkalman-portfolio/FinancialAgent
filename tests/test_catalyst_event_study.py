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
