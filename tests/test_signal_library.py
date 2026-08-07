"""
Tests for src/signal_library.py and the generic signal interface.

Each candidate must fire on the pattern it claims to detect and stay silent
otherwise — a signal that fires on everything will always look average, and one
that never fires looks like it has no edge. Both failure modes are silent.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import signal_library as sl
from src.trigger_backtest import BacktestConfig, run_backtest


def _daily(closes, volumes=None, opens=None, index=None):
    c = np.asarray(closes, float)
    idx = index if index is not None else pd.date_range("2022-01-03", periods=len(c), freq="B")
    o = np.asarray(opens, float) if opens is not None else c
    v = np.asarray(volumes, float) if volumes is not None else np.full(len(c), 1e6)
    return pd.DataFrame(
        {"Open": o, "High": np.maximum(o, c) * 1.001, "Low": np.minimum(o, c) * 0.999,
         "Close": c, "Volume": v}, index=idx)


class TestGoldenCross:
    def test_fires_when_sma50_crosses_sma200(self):
        # long decline then a sustained rally — forces one clean crossover
        down = np.linspace(200, 100, 300)
        up = np.linspace(100, 260, 300)
        d = _daily(np.concatenate([down, up]))
        ev = sl.golden_cross(None, d)
        assert len(ev) >= 1
        c = d["Close"]
        for ts in ev["ts"]:
            i = d.index.get_loc(ts)
            s50, s200 = c.rolling(50).mean(), c.rolling(200).mean()
            assert s50.iloc[i] > s200.iloc[i]
            assert s50.iloc[i - 1] <= s200.iloc[i - 1]

    def test_silent_on_a_flat_series(self):
        assert sl.golden_cross(None, _daily(np.full(600, 100.0))).empty


class TestBreakout52w:
    def test_requires_both_new_high_and_volume(self):
        base = np.concatenate([np.full(300, 100.0), np.full(20, 100.0)])
        c = base.copy()
        c[-1] = 130.0                     # new high
        vol = np.full(len(c), 1e6)

        quiet = sl.breakout_52w(None, _daily(c, volumes=vol))
        assert quiet.empty, "a new high on average volume must not fire"

        vol[-1] = 5e6
        loud = sl.breakout_52w(None, _daily(c, volumes=vol))
        assert len(loud) == 1
        assert loud.iloc[0]["entry"] == pytest.approx(130.0)

    def test_does_not_fire_below_the_prior_high(self):
        c = np.concatenate([np.full(300, 100.0), [99.0]])
        v = np.full(len(c), 1e6); v[-1] = 9e6
        assert sl.breakout_52w(None, _daily(c, volumes=v)).empty


class TestDonchian:
    def test_fires_on_first_close_above_20d_high(self):
        c = np.concatenate([np.full(50, 100.0), [110.0], [111.0]])
        ev = sl.donchian_20(None, _daily(c))
        assert len(ev) == 1, "only the breakout bar fires, not every bar above"
        assert ev.iloc[0]["entry"] == pytest.approx(110.0)


class TestMomentum121:
    def test_only_fires_at_month_boundaries(self):
        c = 100 * np.exp(np.linspace(0, 0.9, 400))  # strong sustained uptrend
        d = _daily(c)
        ev = sl.momentum_12_1(None, d)
        assert len(ev) > 0
        months = pd.DatetimeIndex(ev["ts"]).to_period("M")
        assert len(months) == len(months.unique()), "at most one signal per month"

    def test_silent_when_momentum_is_weak(self):
        assert sl.momentum_12_1(None, _daily(np.full(400, 100.0))).empty


class TestRsiDip:
    def test_requires_price_above_sma200(self):
        # downtrend with an RSI bounce — must stay silent (falling knife)
        rng = np.random.default_rng(4)
        c = 200 * np.exp(np.cumsum(rng.normal(-0.003, 0.01, 400)))
        ev = sl.rsi_dip_in_uptrend(None, _daily(c))
        d = _daily(c)
        s200 = d["Close"].rolling(200).mean()
        for ts in ev["ts"]:
            i = d.index.get_loc(ts)
            assert d["Close"].iloc[i] > s200.iloc[i]


class TestGapUp:
    def test_needs_gap_hold_and_volume(self):
        c = list(np.full(60, 100.0))
        o = list(np.full(60, 100.0))
        v = list(np.full(60, 1e6))
        # gap up 5%, closes higher, heavy volume
        o.append(105.0); c.append(107.0); v.append(5e6)
        ev = sl.gap_up_continuation(None, _daily(c, volumes=v, opens=o))
        assert len(ev) == 1

    def test_gap_that_fades_does_not_fire(self):
        c = list(np.full(60, 100.0)); o = list(np.full(60, 100.0)); v = list(np.full(60, 1e6))
        o.append(105.0); c.append(103.0); v.append(5e6)   # closed below the open
        assert sl.gap_up_continuation(None, _daily(c, volumes=v, opens=o)).empty


class TestGenericInterface:
    def test_run_backtest_accepts_a_custom_signal(self):
        """The harness must be signal-agnostic — that is the whole point."""
        didx = pd.date_range("2022-01-03", periods=600, freq="B")
        c = 100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0.0005, 0.01, 600)))
        d = _daily(c, index=didx)
        hidx = pd.date_range("2024-01-02 14:30", periods=3000, freq="h")
        h = _daily(np.full(3000, 100.0), index=hidx)

        fired = {"n": 0}

        def custom(hourly_df, daily_df):
            fired["n"] += 1
            i = daily_df.index.searchsorted(pd.Timestamp("2024-02-01"))
            return pd.DataFrame({"ts": [daily_df.index[i]],
                                 "entry": [float(daily_df["Close"].iloc[i])]})

        ev = run_backtest({"AAA": h}, {"AAA": d}, {"IWM": d, "SPY": d},
                          BacktestConfig(signal_hours_only=False), signal_fn=custom)
        assert fired["n"] == 1
        assert len(ev) == 1

    def test_signal_without_level_column_is_accepted(self):
        """Only the Supertrend signal has a 'level'; others must not be forced to."""
        didx = pd.date_range("2022-01-03", periods=600, freq="B")
        d = _daily(100 * np.exp(np.cumsum(np.random.default_rng(2).normal(0.0005, 0.01, 600))),
                   index=didx)
        hidx = pd.date_range("2024-01-02 14:30", periods=3000, freq="h")
        h = _daily(np.full(3000, 100.0), index=hidx)

        def no_level(hourly_df, daily_df):
            i = daily_df.index.searchsorted(pd.Timestamp("2024-02-01"))
            return pd.DataFrame({"ts": [daily_df.index[i]],
                                 "entry": [float(daily_df["Close"].iloc[i])]})

        ev = run_backtest({"AAA": h}, {"AAA": d}, {"IWM": d, "SPY": d},
                          BacktestConfig(signal_hours_only=False), signal_fn=no_level)
        assert len(ev) == 1
        assert np.isnan(ev.iloc[0]["st_level"])

    def test_daily_signals_survive_the_signal_hours_gate(self):
        """Regression: daily bars are timestamped at midnight, so the intraday
        09:30-20:00 ET gate rejected every one of them and six candidate signals
        reported zero events instead of being correctly evaluated."""
        didx = pd.date_range("2022-01-03", periods=700, freq="B")
        c = 100 * np.exp(np.cumsum(np.random.default_rng(5).normal(0.0006, 0.01, 700)))
        d = _daily(c, index=didx)
        hidx = pd.date_range("2023-01-02 14:30", periods=6000, freq="h")
        h = _daily(np.full(6000, 100.0), index=hidx)

        def daily_signal(hourly_df, daily_df):
            i = daily_df.index.searchsorted(pd.Timestamp("2023-06-01"))
            return pd.DataFrame({"ts": [daily_df.index[i]],
                                 "entry": [float(daily_df["Close"].iloc[i])]})

        ev = run_backtest({"AAA": h}, {"AAA": d}, {"IWM": d, "SPY": d},
                          BacktestConfig(signal_hours_only=True), signal_fn=daily_signal)
        assert len(ev) == 1, "a midnight-stamped daily signal must not be gated out"

    def test_intraday_signal_outside_session_is_still_gated(self):
        didx = pd.date_range("2022-01-03", periods=700, freq="B")
        d = _daily(100 * np.exp(np.cumsum(np.random.default_rng(6).normal(0.0006, 0.01, 700))),
                   index=didx)
        hidx = pd.date_range("2023-01-02 00:00", periods=6000, freq="h")
        h = _daily(np.full(6000, 100.0), index=hidx)

        def overnight(hourly_df, daily_df):
            ts = pd.Timestamp("2023-06-01 03:00")   # 03:00 UTC — outside the session
            return pd.DataFrame({"ts": [ts], "entry": [100.0]})

        ev = run_backtest({"AAA": h}, {"AAA": d}, {"IWM": d, "SPY": d},
                          BacktestConfig(signal_hours_only=True), signal_fn=overnight)
        assert ev.empty

    def test_broken_signal_does_not_abort_the_run(self):
        didx = pd.date_range("2022-01-03", periods=600, freq="B")
        d = _daily(np.full(600, 100.0), index=didx)
        hidx = pd.date_range("2024-01-02 14:30", periods=800, freq="h")
        h = _daily(np.full(800, 100.0), index=hidx)

        def broken(hourly_df, daily_df):
            raise ValueError("boom")

        ev = run_backtest({"AAA": h}, {"AAA": d}, {"IWM": d, "SPY": d},
                          BacktestConfig(), signal_fn=broken)
        assert ev.empty
