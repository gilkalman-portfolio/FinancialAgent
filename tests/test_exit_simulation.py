"""
Tests for the exit-regime simulator in src/trigger_backtest.py.

A simulator that is optimistic about fills produces a strategy that looks
profitable and is not. These tests pin the pessimistic assumptions in place.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.trigger_backtest import (
    ExitConfig, clustered_stats, simulate_exits,
)


def _bars(closes, index=None, spread=0.0):
    """OHLC where High/Low straddle Close by `spread` fraction."""
    c = np.asarray(closes, dtype=float)
    idx = index if index is not None else pd.date_range("2024-01-02 14:30", periods=len(c), freq="h")
    return pd.DataFrame(
        {"Open": c, "High": c * (1 + spread), "Low": c * (1 - spread),
         "Close": c, "Volume": np.full(len(c), 1e6)}, index=idx)


def _events(ts, ticker="AAA", entry=100.0):
    return pd.DataFrame([{"ticker": ticker, "ts": ts, "entry": entry, "spy_bull": True}])


def _bench(index):
    return {"IWM": _bars(np.full(len(index), 100.0), index=index)}


class TestStopFills:
    def test_gap_through_stop_fills_at_open_not_stop(self):
        """A stop is not a guaranteed price. If the bar opens below it, that is
        the fill — assuming otherwise invents money that does not exist."""
        # flat, then a violent gap down
        closes = [100.0] * 40 + [70.0] + [70.0] * 10
        h = _bars(closes)
        h.loc[h.index[40], ["Open", "High"]] = [70.0, 70.0]
        hourly = {"AAA": h}
        ev = _events(h.index[39])
        bd = _bench(pd.date_range("2024-01-01", periods=60, freq="D"))

        tr = simulate_exits(ev, hourly, bd, ExitConfig("t", stop_timeframe="hourly", stop_atr=2.0, max_hold_days=30))
        assert len(tr) == 1
        r = tr.iloc[0]
        assert r["reason"] == "stop"
        assert r["exit"] == pytest.approx(70.0), "must fill at the gapped open"

    def test_stop_beats_target_when_both_touched_in_one_bar(self):
        """Intrabar order is unknowable, so resolve against yourself."""
        closes = [100.0] * 40 + [100.0] * 5
        h = _bars(closes)
        # one bar spanning far above and far below
        h.loc[h.index[41], ["Open", "High", "Low", "Close"]] = [100.0, 200.0, 1.0, 100.0]
        hourly = {"AAA": h}
        ev = _events(h.index[39])
        bd = _bench(pd.date_range("2024-01-01", periods=60, freq="D"))

        tr = simulate_exits(ev, hourly, bd,
                            ExitConfig("t", stop_timeframe="hourly", stop_atr=1.0, target_atr=1.0, max_hold_days=30))
        assert tr.iloc[0]["reason"] == "stop"


class TestExitReasons:
    def _ramp(self, n=400, rate=0.001):
        return _bars(100.0 * np.exp(np.arange(n) * rate))

    def test_time_exit_fires_at_the_deadline(self):
        h = self._ramp()
        hourly = {"AAA": h}
        ev = _events(h.index[50])
        bd = _bench(pd.date_range("2024-01-01", periods=90, freq="D"))
        tr = simulate_exits(ev, hourly, bd, ExitConfig("t", stop_timeframe="hourly", stop_atr=99.0, max_hold_days=5))
        r = tr.iloc[0]
        assert r["reason"] == "time"
        assert r["hold_days"] == pytest.approx(5, abs=0.2)

    def test_target_exit_fires_on_a_rally(self):
        h = self._ramp(rate=0.004)
        hourly = {"AAA": h}
        ev = _events(h.index[50], entry=float(h["Close"].iloc[50]))
        bd = _bench(pd.date_range("2024-01-01", periods=90, freq="D"))
        tr = simulate_exits(ev, hourly, bd,
                            ExitConfig("t", stop_timeframe="hourly", stop_atr=99.0, target_atr=2.0, max_hold_days=30))
        assert tr.iloc[0]["reason"] == "target"

    def test_trailing_stop_never_moves_down(self):
        """Up then down. The trail must lock in gains, not follow price lower."""
        up = 100.0 * np.exp(np.arange(200) * 0.002)
        down = up[-1] * np.exp(-np.arange(200) * 0.002)
        h = _bars(np.concatenate([up, down]))
        hourly = {"AAA": h}
        ev = _events(h.index[50], entry=float(h["Close"].iloc[50]))
        bd = _bench(pd.date_range("2024-01-01", periods=120, freq="D"))
        tr = simulate_exits(ev, hourly, bd,
                            ExitConfig("t", stop_timeframe="hourly", stop_atr=3.0, trail_atr=3.0,
                                       trail_arm_pct=2.0, max_hold_days=365))
        r = tr.iloc[0]
        assert r["reason"] == "stop"
        assert r["exit"] > r["entry"], "an armed trail must exit above entry after a rally"


class TestMaturity:
    def test_unmatured_trade_is_excluded_not_counted_flat(self):
        """A signal near the end of the data has not finished. Counting it at the
        last close would silently fill the sample with truncated winners."""
        h = _bars(100.0 * np.exp(np.arange(60) * 0.0005))
        hourly = {"AAA": h}
        ev = _events(h.index[55])  # only ~4 hours of data left
        bd = _bench(pd.date_range("2024-01-01", periods=30, freq="D"))
        tr = simulate_exits(ev, hourly, bd, ExitConfig("t", stop_timeframe="hourly", stop_atr=99.0, max_hold_days=30))
        assert tr.empty


class TestAccounting:
    def test_friction_is_deducted_from_gross(self):
        h = _bars(100.0 * np.exp(np.arange(400) * 0.001))
        hourly = {"AAA": h}
        ev = _events(h.index[50], entry=float(h["Close"].iloc[50]))
        bd = _bench(pd.date_range("2024-01-01", periods=90, freq="D"))
        tr = simulate_exits(ev, hourly, bd,
                            ExitConfig("t", stop_timeframe="hourly", stop_atr=99.0, max_hold_days=5, friction_pct=0.25))
        r = tr.iloc[0]
        assert r["net_pct"] == pytest.approx(r["gross_pct"] - 0.25)

    def test_excess_uses_the_realised_holding_window(self):
        """Benchmark must span entry→exit, not a fixed horizon, or the comparison
        is between two different holding periods."""
        idx = pd.date_range("2024-01-02 14:30", periods=400, freq="h")
        h = _bars(np.full(400, 100.0), index=idx)
        hourly = {"AAA": h}
        bidx = pd.date_range("2024-01-01", periods=60, freq="D")
        bench = {"IWM": _bars(100.0 * np.exp(np.arange(60) * 0.01), index=bidx)}
        ev = _events(h.index[50])
        tr = simulate_exits(ev, hourly, bench,
                            ExitConfig("t", stop_timeframe="hourly", stop_atr=99.0, max_hold_days=5, friction_pct=0.0))
        r = tr.iloc[0]
        b0 = bench["IWM"]["Close"].index.searchsorted(r["ts"].normalize())
        b1 = bench["IWM"]["Close"].index.searchsorted(r["exit_ts"].normalize())
        expected = (bench["IWM"]["Close"].iloc[b1] / bench["IWM"]["Close"].iloc[b0] - 1) * 100
        assert r["bench_pct"] == pytest.approx(expected)
        assert r["excess_pct"] == pytest.approx(r["net_pct"] - expected)


class TestStopTimeframeFidelity:
    """Production sets stops from DAILY ATR (execution_engine._get_atr uses
    history(period="30d") at yfinance's default daily interval). Sizing from
    hourly ATR yields a far tighter stop that ordinary noise takes out — an
    artifact that would masquerade as 'stops destroy the edge'."""

    def _setup(self):
        idx_h = pd.date_range("2024-06-03 14:30", periods=400, freq="h")
        h = _bars(np.full(400, 100.0), index=idx_h, spread=0.002)   # quiet hourly
        didx = pd.date_range("2024-01-01", periods=200, freq="B")
        # volatile daily: 4% range per session
        d = _bars(np.full(200, 100.0), index=didx, spread=0.02)
        return {"AAA": h}, {"AAA": d}, _bench(pd.date_range("2024-01-01", periods=260, freq="D"))

    def test_daily_atr_gives_a_wider_stop_than_hourly(self):
        hourly, daily, bd = self._setup()
        ev = _events(hourly["AAA"].index[50])

        hr = simulate_exits(ev, hourly, bd,
                            ExitConfig("h", stop_timeframe="hourly", stop_atr=2.0,
                                       max_hold_days=5, friction_pct=0.0))
        dl = simulate_exits(ev, hourly, bd,
                            ExitConfig("d", stop_timeframe="daily", stop_atr=2.0,
                                       max_hold_days=5, friction_pct=0.0),
                            daily=daily)
        assert not hr.empty and not dl.empty
        # Both survive to the time exit here; assert the ATR itself differs by
        # checking a stop tight enough to trigger on hourly noise but not daily.
        tight = simulate_exits(ev, hourly, bd,
                               ExitConfig("h", stop_timeframe="hourly", stop_atr=0.5,
                                          max_hold_days=5, friction_pct=0.0))
        wide = simulate_exits(ev, hourly, bd,
                              ExitConfig("d", stop_timeframe="daily", stop_atr=0.5,
                                         max_hold_days=5, friction_pct=0.0),
                              daily=daily)
        assert tight.iloc[0]["reason"] == "stop"
        assert wide.iloc[0]["reason"] == "time", "daily ATR stop must survive hourly noise"

    def test_daily_mode_skips_when_daily_bars_are_missing(self):
        """Silently falling back to hourly ATR would reintroduce the artifact."""
        hourly, _, bd = self._setup()
        ev = _events(hourly["AAA"].index[50])
        out = simulate_exits(ev, hourly, bd,
                             ExitConfig("d", stop_timeframe="daily", stop_atr=2.0,
                                        max_hold_days=5), daily={})
        assert out.empty


class TestClusteredStats:
    def test_clustering_reduces_t_versus_naive(self):
        """The whole point: overlapping trades must not each count as evidence."""
        ts = pd.date_range("2024-01-01", periods=300, freq="D")
        tr = pd.DataFrame({
            "ticker": ["AAA"] * 300, "ts": ts,
            "hold_days": [30] * 300, "excess_pct": [1.0] * 300,
        })
        cs = clustered_stats(tr)
        assert cs["n"] < 300, "overlapping same-ticker trades must be thinned"
        assert cs["months"] <= 11

    def test_empty_input_is_handled(self):
        assert clustered_stats(pd.DataFrame())["n"] == 0

    def test_thinning_does_not_select_on_outcome(self):
        """Regression: spacing by the CURRENT trade's hold admits fast losers and
        drops slow winners, biasing the mean downward. Non-overlap must be defined
        by the previous kept trade's EXIT."""
        ts, rows = pd.Timestamp("2024-01-01"), []
        for i in range(40):
            # alternating: quick 2-day loser, slow 30-day winner, same ticker
            quick = i % 2 == 0
            hold = 2 if quick else 30
            rows.append({
                "ticker": "AAA", "ts": ts, "exit_ts": ts + pd.Timedelta(days=hold),
                "hold_days": hold, "excess_pct": -5.0 if quick else +5.0,
            })
            ts = ts + pd.Timedelta(days=5)
        tr = pd.DataFrame(rows)

        kept = clustered_stats(tr)
        # Truth is 0.0 (equal numbers of +5 and -5). Outcome-blind thinning cannot
        # land anywhere near the all-losers extreme.
        assert kept["n"] > 0
        assert kept["mean"] > -4.0, (
            f"thinning is selecting losers: mean={kept['mean']:.2f}")

    def test_kept_trades_never_overlap(self):
        ts, rows = pd.Timestamp("2024-01-01"), []
        for _ in range(20):
            rows.append({"ticker": "AAA", "ts": ts,
                         "exit_ts": ts + pd.Timedelta(days=30),
                         "hold_days": 30, "excess_pct": 1.0})
            ts = ts + pd.Timedelta(days=3)
        tr = pd.DataFrame(rows)
        # 20 trades every 3 days each held 30 days -> at most ~7 can be disjoint
        assert clustered_stats(tr)["n"] <= 7
