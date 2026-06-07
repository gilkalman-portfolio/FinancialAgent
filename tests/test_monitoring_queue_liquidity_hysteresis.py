"""Liquidity hysteresis test for `src.monitoring_queue.build_queue`.

Verifies that a ticker oscillating around the $5M ADV boundary does not
churn in/out of the monitoring queue each cycle. Entry requires
>= $5M ADV; once in, the ticker holds its slot until ADV drops below $3M.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from src import monitoring_queue


def _make_hist(adv_dollars: float) -> pd.DataFrame:
    """Build a 20-row OHLCV history whose Close*Volume averages to adv_dollars."""
    close = 10.0
    volume = adv_dollars / close
    return pd.DataFrame({
        "Close": [close] * 20,
        "Volume": [volume] * 20,
    })


@pytest.fixture(autouse=True)
def _reset_snapshot():
    monitoring_queue._previous_queue = set()
    yield
    monitoring_queue._previous_queue = set()


def _build_with_adv(adv_dollars: float) -> list[str]:
    """Run build_queue with FOO as the only candidate ticker at the given ADV."""
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = _make_hist(adv_dollars)

    with patch.object(monitoring_queue, "_scanner_tickers", return_value=[]), \
         patch.object(monitoring_queue, "_manual_tickers",
                      return_value=[("FOO", "2026-05-19T00:00:00")]), \
         patch.object(monitoring_queue, "_recent_buy_tickers", return_value=[]), \
         patch.object(monitoring_queue.yf, "Ticker", return_value=fake_ticker):
        entries = monitoring_queue.build_queue()
    return [e.ticker for e in entries]


def test_liquidity_hysteresis_full_cycle():
    # 1. ADV $6M → above entry threshold ($5M) → IN
    assert _build_with_adv(6_000_000) == ["FOO"]
    assert "FOO" in monitoring_queue._previous_queue

    # 2. ADV $4M (deadband) → already in queue, stays IN (>= $3M exit)
    assert _build_with_adv(4_000_000) == ["FOO"]
    assert "FOO" in monitoring_queue._previous_queue

    # 3. ADV $2.5M → below exit threshold → DROPS OUT
    assert _build_with_adv(2_500_000) == []
    assert "FOO" not in monitoring_queue._previous_queue

    # 4. ADV $4M again → not in queue, below entry threshold ($5M) → stays OUT
    assert _build_with_adv(4_000_000) == []
    assert "FOO" not in monitoring_queue._previous_queue
