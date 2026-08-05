"""
Fidelity and point-in-time tests for src/trigger_backtest.py.

A backtest is only worth its weakest assumption. The two that matter here:
  1. the flips it finds are the flips production would have fired on
  2. no information from after the signal leaks into the entry decision
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.supertrend import supertrend
from src.trigger_backtest import (
    BacktestConfig, daily_features, find_flips, run_backtest, stats, trend_series,
)


def _synthetic_bars(n=400, seed=7):
    """Deterministic OHLC with real trend reversals, so flips actually occur.

    Drift alternates every 100 bars; tiling rather than hardcoding four segments
    keeps the helper valid for any n, including n smaller than one segment.
    """
    rng = np.random.default_rng(seed)
    cycle = np.concatenate([
        np.full(100, 0.004), np.full(100, -0.005),
        np.full(100, 0.003), np.full(100, -0.004),
    ])
    drift = np.tile(cycle, n // len(cycle) + 1)[:n]
    ret = drift + rng.normal(0, 0.012, n)
    close = 50 * np.exp(np.cumsum(ret))
    high = close * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.006, n)))
    idx = pd.date_range("2024-01-01 14:30", periods=n, freq="h")
    return pd.DataFrame(
        {"Open": close, "High": high, "Low": low, "Close": close,
         "Volume": rng.integers(1e5, 1e6, n)},
        index=idx,
    )


# ── 1. The backtest indicator must equal the production indicator ──────────

class TestFidelityToProduction:
    def test_last_bar_direction_matches_supertrend(self):
        bars = _synthetic_bars()
        trend, _, _ = trend_series(bars)
        prod = supertrend(bars)
        expected = "Bullish" if trend.iloc[-1] == 1 else "Bearish"
        assert prod["direction"] == expected

    def test_last_bar_signal_matches_supertrend(self):
        """Walk many end points; at each, the production one-bar signal must agree
        with the flip the series reports at that same bar."""
        bars = _synthetic_bars()
        trend, _, _ = trend_series(bars)
        checked = 0
        for cut in range(150, len(bars)):
            window = bars.iloc[: cut + 1]
            prod = supertrend(window, lookback=1)
            flipped_here = trend.iloc[cut] != trend.iloc[cut - 1]
            if flipped_here:
                expected = "BUY" if trend.iloc[cut] == 1 else "SELL"
                assert prod["signal"] == expected, f"disagree at bar {cut}"
                checked += 1
            else:
                assert prod["signal"] is None, f"phantom signal at bar {cut}"
        assert checked >= 3, f"only {checked} flips exercised — test is not meaningful"

    def test_level_matches_supertrend(self):
        bars = _synthetic_bars()
        trend, fl, fu = trend_series(bars)
        prod = supertrend(bars)
        expected = fl.iloc[-1] if trend.iloc[-1] == 1 else fu.iloc[-1]
        assert prod["level"] == pytest.approx(float(expected), rel=1e-9)


# ── 2. Flip detection ──────────────────────────────────────────────────────

class TestFindFlips:
    def test_finds_buy_flips_and_they_are_real_reversals(self):
        bars = _synthetic_bars()
        flips = find_flips(bars, "BUY")
        assert not flips.empty
        trend, _, _ = trend_series(bars)
        for ts in flips["ts"]:
            i = bars.index.get_loc(ts)
            assert trend.iloc[i] == 1 and trend.iloc[i - 1] == -1

    def test_buy_and_sell_flips_are_disjoint(self):
        bars = _synthetic_bars()
        buys = set(find_flips(bars, "BUY")["ts"])
        sells = set(find_flips(bars, "SELL")["ts"])
        assert not (buys & sells)

    def test_insufficient_data_returns_empty(self):
        assert find_flips(_synthetic_bars(n=5)).empty
        assert find_flips(None).empty


# ── 3. No look-ahead ───────────────────────────────────────────────────────

class TestNoLookAhead:
    def _fixture(self, n=1600):
        # 1600 continuous hourly bars ~= 66 calendar days, so early signals mature
        # at 30d while recent ones cannot — which is what the maturity test needs.
        hourly = {"AAA": _synthetic_bars(n=n, seed=3)}
        didx = pd.date_range("2022-06-01", periods=700, freq="B")
        rng = np.random.default_rng(11)
        dclose = 50 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, 700)))
        daily_df = pd.DataFrame(
            {"Open": dclose, "High": dclose * 1.01, "Low": dclose * 0.99,
             "Close": dclose, "Volume": np.full(700, 2_000_000)}, index=didx)
        daily = {"AAA": daily_df}
        bench = {b: daily_df.copy() for b in ("IWM", "SPY")}
        return hourly, daily, bench

    def test_daily_features_come_from_before_the_signal_day(self):
        """The signal day's own daily bar must not inform the entry — it contains
        the rest of that session."""
        hourly, daily, bench = self._fixture()
        ev = run_backtest(hourly, daily, bench, BacktestConfig())
        assert not ev.empty
        feats = daily_features(daily["AAA"])
        for _, r in ev.head(20).iterrows():
            prior = feats[feats.index < r["date"]]
            assert r["d_close"] == pytest.approx(float(prior["Close"].iloc[-1]))
            assert r["sma200"] == pytest.approx(float(prior["sma200"].iloc[-1]))

    def test_forward_return_starts_at_the_flip_bar(self):
        hourly, daily, bench = self._fixture()
        ev = run_backtest(hourly, daily, bench, BacktestConfig(horizons=(7,)))
        assert not ev.empty
        close = hourly["AAA"]["Close"]
        r = ev.iloc[0]
        i0 = close.index.searchsorted(r["ts"])
        assert float(close.iloc[i0]) == pytest.approx(r["entry"])

    def test_signal_hours_gate_excludes_overnight_bars(self):
        hourly, daily, bench = self._fixture()
        ev = run_backtest(hourly, daily, bench, BacktestConfig(signal_hours_only=True))
        assert not ev.empty
        assert ev.ts.dt.hour.between(13, 19).all()

    def test_unmatured_signals_are_dropped_not_zero_filled(self):
        """A signal too recent to have a 30d outcome must be absent from the 30d
        column, not counted as a 0% return."""
        hourly, daily, bench = self._fixture()
        # signal_hours_only is exercised by its own test; keeping it on here would
        # discard ~70% of the synthetic bars and leave too few flips to compare
        # matured against unmatured.
        ev = run_backtest(hourly, daily, bench,
                          BacktestConfig(horizons=(7, 30), signal_hours_only=False))
        assert not ev.empty
        last_ts = hourly["AAA"].index[-1]
        recent = ev[ev["ts"] > last_ts - pd.Timedelta(days=30)]
        assert len(recent) > 0, "fixture must contain signals too recent to mature"
        assert recent["ret30"].isna().all()
        matured = ev[ev["ts"] < last_ts - pd.Timedelta(days=35)]
        assert matured["ret30"].notna().any(), "older signals should have matured"


# ── 4. Statistics ──────────────────────────────────────────────────────────

class TestStats:
    def test_t_stat_matches_hand_calculation(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        s = stats(x)
        assert s["n"] == 5
        assert s["mean"] == pytest.approx(3.0)
        assert s["sd"] == pytest.approx(np.std(x, ddof=1))
        assert s["t"] == pytest.approx(3.0 / (np.std(x, ddof=1) / np.sqrt(5)))
        assert s["win"] == pytest.approx(100.0)

    def test_nones_are_excluded_not_treated_as_zero(self):
        assert stats([1.0, None, 3.0, np.nan])["n"] == 2

    def test_too_few_returns_nan(self):
        assert np.isnan(stats([1.0, 2.0])["t"])
