"""
Look-ahead bias regression test for StockForecaster.

Audit Risk #2: src/stock_forecaster.py was suspected of letting "future" rows
leak into model fitting. We now expose a ``point_in_time`` parameter that
strictly truncates input data at construction. This test proves that, when the
caller asks for a forecast as of day 194 (one day BEFORE an artificial +30%
spike on day 195), the forecaster's prediction for day 195 stays near the
pre-spike price level — i.e. the spike was not "seen" during training.
"""

import sys
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# Make `src` importable when running directly.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.stock_forecaster import StockForecaster


def _build_synthetic_ohlcv(n_days: int = 200, spike_day: int = 195,
                           spike_pct: float = 0.30, seed: int = 42) -> pd.DataFrame:
    """Mean-reverting noise around $100 with a known +30% spike at ``spike_day``."""
    rng = np.random.default_rng(seed)
    base = 100.0
    # Ornstein-Uhlenbeck style mean-reverting series
    series = [base]
    for _ in range(1, n_days):
        prev = series[-1]
        mean_revert = 0.05 * (base - prev)
        noise = rng.normal(0, 0.8)
        series.append(prev + mean_revert + noise)
    closes = np.array(series, dtype=float)

    # Artificial spike on spike_day (0-indexed)
    closes[spike_day] = closes[spike_day - 1] * (1.0 + spike_pct)

    dates = pd.date_range(end=datetime(2026, 5, 19), periods=n_days, freq="B")
    df = pd.DataFrame({
        "Open":   closes,
        "High":   closes * 1.005,
        "Low":    closes * 0.995,
        "Close":  closes,
        "Volume": rng.integers(1_000_000, 5_000_000, size=n_days),
    }, index=dates)
    return df


def test_forecaster_excludes_future_when_point_in_time_set():
    df = _build_synthetic_ohlcv()
    spike_day = 195
    pre_spike_price = float(df["Close"].iloc[spike_day - 1])
    spike_price     = float(df["Close"].iloc[spike_day])

    # Sanity: the synthetic spike is large and detectable.
    assert spike_price > pre_spike_price * 1.20, \
        f"Synthetic spike too small: {pre_spike_price:.2f} -> {spike_price:.2f}"

    # Predict day 195 using ONLY data through day 194.
    cutoff = df.index[spike_day - 1]
    forecaster = StockForecaster("TEST", df, point_in_time=cutoff)

    # Sanity: truncation took effect.
    assert len(forecaster.data) == spike_day, \
        f"Expected {spike_day} rows after truncation, got {len(forecaster.data)}"
    assert forecaster.data["Close"].iloc[-1] == pre_spike_price, \
        "Last visible close should be pre-spike price"

    summary = forecaster.run_all_forecasts(days_ahead=1)
    assert summary is not None, "Forecaster returned None summary"

    predicted = summary["predicted_price"]
    # The prediction must stay anchored near the pre-spike level — well below
    # the actual spike. Allow ±15% wiggle around pre-spike to absorb model noise.
    lower_ok = pre_spike_price * 0.85
    upper_ok = pre_spike_price * 1.15

    # Hard guard: must NOT predict anywhere near the spike.
    assert predicted < spike_price * 0.95, (
        f"Look-ahead suspected: predicted {predicted:.2f} is too close to "
        f"unseen spike price {spike_price:.2f} (pre-spike was {pre_spike_price:.2f})"
    )

    # Soft guard: prediction should hover around the pre-spike regime.
    assert lower_ok <= predicted <= upper_ok, (
        f"Prediction {predicted:.2f} outside reasonable band "
        f"[{lower_ok:.2f}, {upper_ok:.2f}] around pre-spike {pre_spike_price:.2f}"
    )

    print(f"PASS: pre_spike={pre_spike_price:.2f}, spike={spike_price:.2f}, "
          f"predicted={predicted:.2f} (band [{lower_ok:.2f}, {upper_ok:.2f}])")


def test_forecaster_default_uses_all_data():
    """Backward compat: with no point_in_time, behavior is unchanged."""
    df = _build_synthetic_ohlcv()
    forecaster = StockForecaster("TEST", df)
    assert len(forecaster.data) == len(df), \
        "Without point_in_time, all rows must be retained"
    summary = forecaster.run_all_forecasts(days_ahead=5)
    assert summary is not None
    assert "predicted_price" in summary


if __name__ == "__main__":
    test_forecaster_excludes_future_when_point_in_time_set()
    test_forecaster_default_uses_all_data()
    print("All tests passed.")
