"""
Test: scan_supertrend_universe() and the "supertrend" auto-watchlist source.

Coverage gap this feature closes: the IBKR real-time queue only checks
Supertrend on tickers that already score >= 65 (src/monitoring_queue.py:39).
scan_supertrend_universe() batch-scans the WHOLE index universe with no score
gate, mirroring a bare TradingView alertcondition(buySignal) — any fresh
bullish flip qualifies regardless of composite score.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_supertrend_universe.py -v
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest


# ── Synthetic price series ──────────────────────────────────────────────────

def _reversal_series(n: int = 25) -> pd.Series:
    """Steady decline for n-1 bars, then a sharp +15% spike on the last bar —
    reliably produces a fresh bullish Supertrend flip (bars_ago == 1)."""
    close = list(np.linspace(100, 80, n - 1))
    close.append(close[-1] * 1.15)
    return pd.Series(close)


def _decline_series(n: int = 25) -> pd.Series:
    """Pure decline, no reversal — stays Bearish, signal=None."""
    return pd.Series(list(np.linspace(100, 80, n)))


def _short_series(n: int = 5) -> pd.Series:
    """Too few bars for period=10 — must be skipped, not raise."""
    return pd.Series(list(np.linspace(100, 90, n)))


def _make_multi_ticker_frame(series_by_ticker: dict, volume: float = 5_000_000) -> pd.DataFrame:
    """Build a yf.download(..., group_by default)-shaped MultiIndex frame:
    columns = (field, ticker), matching how momentum_scanner.py and
    scan_supertrend_universe() both index it (raw["Close"], raw["High"], ...)."""
    fields = {}
    for field, mult_low, mult_high in [("High", 1.01, None), ("Low", 0.99, None), ("Close", 1.0, None)]:
        cols = {}
        for ticker, close in series_by_ticker.items():
            if field == "Close":
                cols[ticker] = close
            elif field == "High":
                cols[ticker] = close * 1.01
            else:
                cols[ticker] = close * 0.99
        fields[field] = pd.DataFrame(cols)
    fields["Volume"] = pd.DataFrame({t: pd.Series([volume] * len(c)) for t, c in series_by_ticker.items()})
    return pd.concat(fields, axis=1)


# ── scan_supertrend_universe() ──────────────────────────────────────────────

def test_scan_supertrend_universe_finds_only_fresh_flips():
    from src.supertrend import scan_supertrend_universe

    frame = _make_multi_ticker_frame({
        "FLIP":    _reversal_series(),
        "NOFLIP":  _decline_series(),
        "TOOSHORT": _short_series(),
    })

    with patch("src.supertrend.yf.download", return_value=frame):
        results = scan_supertrend_universe(["FLIP", "NOFLIP", "TOOSHORT"])

    tickers = {r["ticker"] for r in results}
    assert tickers == {"FLIP"}, f"expected only FLIP to have a fresh bullish flip, got {tickers}"

    flip_result = results[0]
    assert flip_result["price"] > 0
    assert flip_result["level"] > 0
    assert flip_result["avg_volume"] == pytest.approx(5_000_000)


def test_scan_supertrend_universe_empty_tickers_returns_empty():
    from src.supertrend import scan_supertrend_universe
    assert scan_supertrend_universe([]) == []


def test_scan_supertrend_universe_handles_download_failure():
    from src.supertrend import scan_supertrend_universe
    with patch("src.supertrend.yf.download", side_effect=RuntimeError("network down")):
        assert scan_supertrend_universe(["AAPL"]) == []


def test_scan_supertrend_universe_handles_empty_response():
    from src.supertrend import scan_supertrend_universe
    with patch("src.supertrend.yf.download", return_value=pd.DataFrame()):
        assert scan_supertrend_universe(["AAPL"]) == []


# ── _filter_supertrend — no composite-score gate ────────────────────────────

def test_filter_supertrend_rejects_below_price_floor():
    from src.auto_watchlist_agent import _filter_supertrend
    r = {"ticker": "PENNY", "price": 2.50, "avg_volume": 10_000_000}
    src_cfg = {"price_floor": 5.0}
    assert _filter_supertrend(r, src_cfg, req_liquidity=False) is False


def test_filter_supertrend_accepts_fresh_flip_with_no_score_field():
    """Core of 'option A': no composite score is required or read at all."""
    from src.auto_watchlist_agent import _filter_supertrend
    r = {"ticker": "GOOD", "price": 42.0, "level": 39.5, "avg_volume": 2_000_000}
    assert "score" not in r and "explosion_score" not in r
    src_cfg = {"price_floor": 5.0}
    assert _filter_supertrend(r, src_cfg, req_liquidity=False) is True


def test_filter_supertrend_liquidity_gate():
    from src.auto_watchlist_agent import _filter_supertrend
    illiquid = {"ticker": "THIN", "price": 10.0, "avg_volume": 100}  # $1,000/day
    src_cfg = {"price_floor": 5.0, "min_avg_dollar_volume": 1_000_000}
    assert _filter_supertrend(illiquid, src_cfg, req_liquidity=True) is False


# ── End-to-end via auto_watchlist_agent.run() ───────────────────────────────

@pytest.fixture
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="st_universe_")
    os.close(fd)
    db_path = Path(path)
    import src.database as db
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    yield db_path
    try:
        db_path.unlink()
    except Exception:
        pass


def test_auto_watchlist_run_adds_fresh_flip_no_score_needed(temp_db, monkeypatch):
    from src.auto_watchlist_agent import run as aw_run
    from src.database import watchlist_get_all

    class _StubTg:
        def send_message(self, msg):
            self.sent = msg
            return True

    stub = _StubTg()
    monkeypatch.setattr("src.auto_watchlist_agent.TelegramNotifier", lambda: stub)

    results = [{"ticker": "FLIPCO", "price": 50.0, "level": 46.0, "avg_volume": 5_000_000}]
    cfg = {
        "telegram": True,
        "auto_watchlist": {
            "enabled": True,
            "sources": {"supertrend": {"enabled": True, "price_floor": 5.0}},
            "deduplication": {"cooldown_minutes": 1440},
            "watchlist_policy": {"max_items_per_source": 10, "max_items_total": 30, "require_liquidity_check": False},
        },
    }

    added = aw_run(results, "supertrend", cfg)

    assert [r["ticker"] for r in added] == ["FLIPCO"]
    assert any(w["ticker"] == "FLIPCO" for w in watchlist_get_all())
    assert "FLIPCO" in stub.sent
    assert "46.00" in stub.sent  # stop level surfaced in the Telegram message


def test_auto_watchlist_run_respects_dedup_cooldown(temp_db, monkeypatch):
    """A second flip on the same ticker within the cooldown window must not
    re-fire Telegram or duplicate the watchlist add."""
    from src.auto_watchlist_agent import run as aw_run

    sent_count = {"n": 0}

    class _StubTg:
        def send_message(self, msg):
            sent_count["n"] += 1
            return True

    monkeypatch.setattr("src.auto_watchlist_agent.TelegramNotifier", lambda: _StubTg())

    results = [{"ticker": "FLIPCO", "price": 50.0, "level": 46.0, "avg_volume": 5_000_000}]
    cfg = {
        "telegram": True,
        "auto_watchlist": {
            "enabled": True,
            "sources": {"supertrend": {"enabled": True, "price_floor": 5.0}},
            "deduplication": {"cooldown_minutes": 1440},
            "watchlist_policy": {"max_items_per_source": 10, "max_items_total": 30, "require_liquidity_check": False},
        },
    }

    first = aw_run(results, "supertrend", cfg)
    second = aw_run(results, "supertrend", cfg)  # simulates the next 30-min cycle

    assert len(first) == 1
    assert len(second) == 0, "expected cooldown to suppress the repeat flip within 24h"
    assert sent_count["n"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
