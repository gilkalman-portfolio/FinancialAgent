"""
Tests for src/catalyst_scanner.py

Covers:
  - Individual scoring components (_urgency_pts, _si_pts, _float_pts, _volume_pts, _momentum_pts)
  - Combined explosion_score (caps at 100, correct weighting)
  - score_label mapping
  - scan_catalysts integration (mocked network I/O)
"""

import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.catalyst_scanner import (
    _urgency_pts,
    _si_pts,
    _float_pts,
    _volume_pts,
    _momentum_pts,
    explosion_score,
    score_label,
    scan_catalysts,
)


# ══════════════════════════════════════════════════════════════════════════════
# _urgency_pts
# ══════════════════════════════════════════════════════════════════════════════

class TestUrgencyPts:
    def test_today_max(self):
        assert _urgency_pts(0) == 30

    def test_tomorrow(self):
        assert _urgency_pts(1) == 27

    def test_two_days(self):
        assert _urgency_pts(2) == 22

    def test_three_days(self):
        assert _urgency_pts(3) == 17

    def test_five_days(self):
        assert _urgency_pts(5) == 12

    def test_seven_days(self):
        assert _urgency_pts(7) == 8

    def test_beyond_week(self):
        assert _urgency_pts(14) == 4
        assert _urgency_pts(30) == 4

    def test_decreasing(self):
        """Urgency must decrease monotonically as days increase."""
        scores = [_urgency_pts(d) for d in range(0, 15)]
        assert scores == sorted(scores, reverse=True)


# ══════════════════════════════════════════════════════════════════════════════
# _si_pts
# ══════════════════════════════════════════════════════════════════════════════

class TestSiPts:
    def test_zero_si(self):
        assert _si_pts(0) == 0

    def test_below_threshold(self):
        assert _si_pts(4.9) == 0

    def test_low_si(self):
        assert _si_pts(5) == 5
        assert _si_pts(9.9) == 5

    def test_medium_si(self):
        assert _si_pts(10) == 11
        assert _si_pts(14.9) == 11

    def test_elevated_si(self):
        assert _si_pts(15) == 18
        assert _si_pts(19.9) == 18

    def test_high_si(self):
        assert _si_pts(20) == 25
        assert _si_pts(50) == 25
        assert _si_pts(100) == 25

    def test_max_is_25(self):
        assert _si_pts(999) == 25


# ══════════════════════════════════════════════════════════════════════════════
# _float_pts
# ══════════════════════════════════════════════════════════════════════════════

class TestFloatPts:
    def test_none_float(self):
        assert _float_pts(None) == 3

    def test_tiny_float(self):
        assert _float_pts(1) == 20
        assert _float_pts(5) == 20

    def test_small_float(self):
        assert _float_pts(6) == 16
        assert _float_pts(15) == 16

    def test_medium_float(self):
        assert _float_pts(16) == 11
        assert _float_pts(40) == 11

    def test_large_float(self):
        assert _float_pts(41) == 6
        assert _float_pts(100) == 6

    def test_very_large_float(self):
        assert _float_pts(101) == 2
        assert _float_pts(1000) == 2

    def test_max_is_20(self):
        assert _float_pts(0.1) == 20


# ══════════════════════════════════════════════════════════════════════════════
# _volume_pts
# ══════════════════════════════════════════════════════════════════════════════

class TestVolumePts:
    def test_no_volume_increase(self):
        assert _volume_pts(0.5) == 0
        assert _volume_pts(1.0) == 0
        assert _volume_pts(1.19) == 0

    def test_slight_increase(self):
        assert _volume_pts(1.2) == 2
        assert _volume_pts(1.4) == 2

    def test_moderate_increase(self):
        assert _volume_pts(1.5) == 4
        assert _volume_pts(1.9) == 4

    def test_strong_increase(self):
        assert _volume_pts(2.0) == 7
        assert _volume_pts(2.9) == 7

    def test_extreme_increase(self):
        assert _volume_pts(3.0) == 10
        assert _volume_pts(10.0) == 10

    def test_max_is_10(self):
        assert _volume_pts(100) == 10


# ══════════════════════════════════════════════════════════════════════════════
# _momentum_pts
# ══════════════════════════════════════════════════════════════════════════════

class TestMomentumPts:
    def test_none(self):
        assert _momentum_pts(None) == 0

    def test_negative_momentum(self):
        assert _momentum_pts(-5) == 0
        assert _momentum_pts(-0.1) == 0

    def test_flat(self):
        assert _momentum_pts(0) == 1
        assert _momentum_pts(1.9) == 1

    def test_moderate_up(self):
        assert _momentum_pts(2) == 3
        assert _momentum_pts(4.9) == 3

    def test_strong_up(self):
        assert _momentum_pts(5) == 5
        assert _momentum_pts(20) == 5

    def test_max_is_5(self):
        assert _momentum_pts(100) == 5


# ══════════════════════════════════════════════════════════════════════════════
# explosion_score
# ══════════════════════════════════════════════════════════════════════════════

class TestExplosionScore:
    def test_zero_scenario(self):
        """No catalyst urgency, no SI, big float, flat volume, no insider."""
        score = explosion_score(
            days_to_event=30,
            si_pct=0,
            float_m=500,
            vol_ratio=1.0,
            has_insider=False,
            pct_5d=None,
        )
        # urgency=4, si=0, float=2, vol=0, insider=0, momentum=0 → 6
        assert score == 6.0

    def test_max_scenario(self):
        """Everything maxed out — should cap at 100."""
        score = explosion_score(
            days_to_event=0,
            si_pct=50,
            float_m=2,
            vol_ratio=5.0,
            has_insider=True,
            pct_5d=10.0,
        )
        # urgency=30, si=25, float=20, vol=10, insider=10, momentum=5 → 100
        assert score == 100.0

    def test_caps_at_100(self):
        """Score never exceeds 100."""
        score = explosion_score(0, 100, 1, 10.0, True, 50.0)
        assert score <= 100.0

    def test_insider_bonus(self):
        """Insider buying adds exactly 10 points."""
        base = explosion_score(7, 10, 50, 1.0, False)
        with_insider = explosion_score(7, 10, 50, 1.0, True)
        assert with_insider - base == 10.0

    def test_deterministic(self):
        """Same inputs always return same output."""
        s1 = explosion_score(3, 15, 20, 1.8, False, 3.0)
        s2 = explosion_score(3, 15, 20, 1.8, False, 3.0)
        assert s1 == s2

    def test_higher_si_higher_score(self):
        """More short interest → higher score, all else equal."""
        low  = explosion_score(5, 3, 30, 1.0, False)
        high = explosion_score(5, 25, 30, 1.0, False)
        assert high > low

    def test_lower_float_higher_score(self):
        """Lower float → higher score, all else equal."""
        big_float   = explosion_score(5, 10, 200, 1.0, False)
        small_float = explosion_score(5, 10, 5, 1.0, False)
        assert small_float > big_float

    def test_closer_event_higher_score(self):
        """Closer earnings date → higher score."""
        far   = explosion_score(14, 10, 30, 1.0, False)
        close = explosion_score(1, 10, 30, 1.0, False)
        assert close > far


# ══════════════════════════════════════════════════════════════════════════════
# score_label
# ══════════════════════════════════════════════════════════════════════════════

class TestScoreLabel:
    def test_high(self):
        label, color = score_label(75)
        assert label == "HIGH"
        assert color == "#7c3aed"

    def test_medium(self):
        label, color = score_label(60)
        assert label == "MEDIUM"
        assert color == "#dc2626"

    def test_low(self):
        label, color = score_label(40)
        assert label == "LOW"
        assert color == "#d97706"

    def test_watch(self):
        label, color = score_label(10)
        assert label == "WATCH"
        assert color == "#6b7280"

    def test_boundaries(self):
        assert score_label(70)[0] == "HIGH"
        assert score_label(69.9)[0] == "MEDIUM"
        assert score_label(50)[0] == "MEDIUM"
        assert score_label(49.9)[0] == "LOW"
        assert score_label(30)[0] == "LOW"
        assert score_label(29.9)[0] == "WATCH"


# ══════════════════════════════════════════════════════════════════════════════
# scan_catalysts (integration — mocked I/O)
# ══════════════════════════════════════════════════════════════════════════════

def _make_earnings_event(symbol, mcap_val=500_000_000, days_offset=3):
    from datetime import datetime, timedelta
    dt = datetime.now() + timedelta(days=days_offset)
    return {
        "symbol":   symbol,
        "name":     f"{symbol} Corp",
        "date":     dt.strftime("%a %b %d"),
        "time":     "After-hours",
        "estimate": "0.45",
        "mcap":     f"${mcap_val/1e6:.0f}M",
        "mcap_val": mcap_val,
        "ts":       dt.timestamp(),
    }


def _make_yf_info(market_cap=800_000_000, float_shares=20_000_000, si_pct=0.18, price=12.5):
    return {
        "currentPrice":        price,
        "marketCap":           market_cap,
        "floatShares":         float_shares,
        "shortPercentOfFloat": si_pct,
        "shortName":           "Test Corp",
        "sector":              "Technology",
    }


class TestScanCatalysts:
    def _mock_ticker(self, info, hist_len=30):
        import pandas as pd
        import numpy as np

        mock_ticker = MagicMock()
        mock_ticker.info = info

        dates  = pd.date_range(end=pd.Timestamp.now(), periods=hist_len, freq="B")
        prices = 10 + np.random.rand(hist_len) * 5
        volume = np.full(hist_len, 1_000_000, dtype=float)
        # Spike last 5 days to create vol_ratio > 1
        volume[-5:] = 2_500_000
        hist = pd.DataFrame({
            "Close":  prices,
            "Volume": volume,
        }, index=dates)
        mock_ticker.history.return_value = hist
        return mock_ticker

    @patch("src.catalyst_scanner.get_earnings_calendar")
    @patch("yfinance.Ticker")
    def test_returns_sorted_by_score(self, mock_yf, mock_earnings):
        """Results are sorted descending by explosion_score."""
        mock_earnings.return_value = [
            _make_earnings_event("AAA", mcap_val=500_000_000, days_offset=1),
            _make_earnings_event("BBB", mcap_val=2_000_000_000, days_offset=6),
        ]
        info_high = _make_yf_info(market_cap=500_000_000, float_shares=5_000_000, si_pct=0.25, price=8.0)
        info_low  = _make_yf_info(market_cap=2_000_000_000, float_shares=200_000_000, si_pct=0.02, price=45.0)

        def side_effect(ticker):
            if ticker == "AAA":
                return self._mock_ticker(info_high)
            return self._mock_ticker(info_low)

        mock_yf.side_effect = side_effect

        results = scan_catalysts(days_ahead=7, check_insider=False, min_explosion_score=0)
        assert len(results) == 2
        assert results[0]["ticker"] == "AAA"
        assert results[0]["explosion_score"] >= results[1]["explosion_score"]

    @patch("src.catalyst_scanner.get_earnings_calendar")
    @patch("yfinance.Ticker")
    def test_market_cap_filter(self, mock_yf, mock_earnings):
        """Tickers above max_market_cap_b are excluded."""
        mock_earnings.return_value = [
            _make_earnings_event("BIG", mcap_val=50_000_000_000),   # $50B — over limit
            _make_earnings_event("SML", mcap_val=500_000_000),      # $0.5B — under limit
        ]

        def side_effect(ticker):
            if ticker == "BIG":
                return self._mock_ticker(_make_yf_info(market_cap=50_000_000_000))
            return self._mock_ticker(_make_yf_info(market_cap=500_000_000))

        mock_yf.side_effect = side_effect

        results = scan_catalysts(days_ahead=7, max_market_cap_b=5.0, check_insider=False, min_explosion_score=0)
        tickers = [r["ticker"] for r in results]
        assert "BIG" not in tickers
        assert "SML" in tickers

    @patch("src.catalyst_scanner.get_earnings_calendar")
    @patch("yfinance.Ticker")
    def test_si_filter(self, mock_yf, mock_earnings):
        """Tickers below min_si_pct are excluded."""
        mock_earnings.return_value = [
            _make_earnings_event("HSI", mcap_val=500_000_000),
            _make_earnings_event("LSI", mcap_val=500_000_000),
        ]

        def side_effect(ticker):
            if ticker == "HSI":
                return self._mock_ticker(_make_yf_info(si_pct=0.25))   # 25%
            return self._mock_ticker(_make_yf_info(si_pct=0.02))       # 2%

        mock_yf.side_effect = side_effect

        results = scan_catalysts(days_ahead=7, min_si_pct=10.0, check_insider=False, min_explosion_score=0)
        tickers = [r["ticker"] for r in results]
        assert "HSI" in tickers
        assert "LSI" not in tickers

    @patch("src.catalyst_scanner.get_earnings_calendar")
    @patch("yfinance.Ticker")
    def test_min_score_filter(self, mock_yf, mock_earnings):
        """Tickers below min_explosion_score are excluded."""
        mock_earnings.return_value = [_make_earnings_event("ZZZ", days_offset=20)]
        # Far-away event + big float + no SI = low score
        info = _make_yf_info(market_cap=500_000_000, float_shares=500_000_000, si_pct=0.0)
        mock_yf.return_value = self._mock_ticker(info)

        results = scan_catalysts(days_ahead=30, min_explosion_score=50.0, check_insider=False)
        assert len(results) == 0

    @patch("src.catalyst_scanner.get_earnings_calendar")
    def test_empty_calendar(self, mock_earnings):
        """Empty earnings calendar returns empty list without error."""
        mock_earnings.return_value = []
        results = scan_catalysts(check_insider=False)
        assert results == []

    @patch("src.catalyst_scanner.get_earnings_calendar")
    @patch("yfinance.Ticker")
    def test_bad_ticker_skipped(self, mock_yf, mock_earnings):
        """Tickers that fail yfinance fetch are silently skipped."""
        mock_earnings.return_value = [
            _make_earnings_event("FAIL"),
            _make_earnings_event("GOOD"),
        ]

        def side_effect(ticker):
            if ticker == "FAIL":
                m = MagicMock()
                m.info = {}   # no price → should be skipped
                m.history.return_value = MagicMock(__len__=lambda s: 0)
                return m
            return self._mock_ticker(_make_yf_info())

        mock_yf.side_effect = side_effect

        results = scan_catalysts(check_insider=False, min_explosion_score=0)
        tickers = [r["ticker"] for r in results]
        assert "FAIL" not in tickers
        assert "GOOD" in tickers

    @patch("src.catalyst_scanner.get_earnings_calendar")
    @patch("yfinance.Ticker")
    def test_result_fields_present(self, mock_yf, mock_earnings):
        """Every result dict contains the required keys."""
        mock_earnings.return_value = [_make_earnings_event("TST", days_offset=2)]
        mock_yf.return_value = self._mock_ticker(_make_yf_info(si_pct=0.20))

        results = scan_catalysts(check_insider=False, min_explosion_score=0)
        assert len(results) == 1
        r = results[0]
        required_keys = [
            "ticker", "name", "sector", "catalyst", "catalyst_date", "catalyst_time",
            "days_to_event", "price", "market_cap_disp", "float_disp",
            "si_pct", "vol_ratio", "has_insider", "explosion_score", "label", "label_color",
        ]
        for key in required_keys:
            assert key in r, f"Missing key: {key}"

    @patch("src.catalyst_scanner.get_earnings_calendar")
    @patch("yfinance.Ticker")
    def test_deduplication(self, mock_yf, mock_earnings):
        """Duplicate tickers in calendar appear only once in results."""
        mock_earnings.return_value = [
            _make_earnings_event("DUP", days_offset=1),
            _make_earnings_event("DUP", days_offset=2),   # same ticker, different day
        ]
        mock_yf.return_value = self._mock_ticker(_make_yf_info())

        results = scan_catalysts(check_insider=False, min_explosion_score=0)
        assert len([r for r in results if r["ticker"] == "DUP"]) == 1

    @patch("src.catalyst_scanner.get_earnings_calendar")
    @patch("yfinance.Ticker")
    def test_progress_callback_called(self, mock_yf, mock_earnings):
        """Progress callback is called once per ticker."""
        mock_earnings.return_value = [
            _make_earnings_event("A1"),
            _make_earnings_event("A2"),
        ]
        mock_yf.return_value = self._mock_ticker(_make_yf_info())

        calls = []
        def cb(current, total, ticker):
            calls.append((current, total, ticker))

        scan_catalysts(check_insider=False, min_explosion_score=0, progress_cb=cb)
        assert len(calls) == 2
        assert calls[0][0] == 1
        assert calls[1][0] == 2
