"""
Unit Tests - Stock Scorer
Tests: signal_label, _score_rsi, _score_short_interest, _score_ma,
       score boundaries, squeeze detection, score_cache, market_feed sentiment
"""
import pytest
import sys
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.stock_scorer import signal_label, _score_rsi, _score_short_interest, _score_institutional, WEIGHTS
from src.score_cache import get, put, clear, _key
from src.market_feed import _keyword_sentiment


# ══════════════════════════════════════════════════════════════════════════════
# signal_label
# ══════════════════════════════════════════════════════════════════════════════

class TestSignalLabel:
    def test_strong_buy(self):
        assert signal_label(75) == "STRONG BUY"
        assert signal_label(100) == "STRONG BUY"
        assert signal_label(80) == "STRONG BUY"

    def test_buy(self):
        assert signal_label(60) == "BUY"
        assert signal_label(74.9) == "BUY"

    def test_watch(self):
        assert signal_label(45) == "WATCH"
        assert signal_label(59.9) == "WATCH"

    def test_neutral(self):
        assert signal_label(35) == "NEUTRAL"
        assert signal_label(44.9) == "NEUTRAL"

    def test_skip(self):
        assert signal_label(0) == "SKIP"
        assert signal_label(34.9) == "SKIP"

    def test_boundary_exactly_75(self):
        assert signal_label(75) == "STRONG BUY"

    def test_boundary_exactly_60(self):
        assert signal_label(60) == "BUY"

    def test_boundary_exactly_45(self):
        assert signal_label(45) == "WATCH"

    def test_boundary_exactly_35(self):
        assert signal_label(35) == "NEUTRAL"


# ══════════════════════════════════════════════════════════════════════════════
# _score_rsi
# ══════════════════════════════════════════════════════════════════════════════

class TestScoreRsi:
    def test_none_returns_half_weight(self):
        assert _score_rsi(None) == WEIGHTS['rsi'] // 2

    def test_sweet_spot_returns_full(self):
        assert _score_rsi(50) == WEIGHTS['rsi']
        assert _score_rsi(40) == WEIGHTS['rsi']
        assert _score_rsi(65) == WEIGHTS['rsi']

    def test_oversold_below_30(self):
        score = _score_rsi(25)
        assert score == int(WEIGHTS['rsi'] * 0.5)

    def test_mild_oversold_30_40(self):
        score = _score_rsi(35)
        assert score == int(WEIGHTS['rsi'] * 0.7)

    def test_mild_overbought_65_70(self):
        score = _score_rsi(68)
        assert score == int(WEIGHTS['rsi'] * 0.7)

    def test_overbought_above_75_returns_zero(self):
        assert _score_rsi(80) == 0
        assert _score_rsi(90) == 0

    def test_score_never_exceeds_weight(self):
        for rsi in [0, 10, 30, 45, 60, 75, 90, 100]:
            assert _score_rsi(rsi) <= WEIGHTS['rsi']

    def test_score_never_negative(self):
        for rsi in [0, 10, 30, 45, 60, 75, 90, 100]:
            assert _score_rsi(rsi) >= 0


# ══════════════════════════════════════════════════════════════════════════════
# _score_short_interest
# ══════════════════════════════════════════════════════════════════════════════

class TestScoreShortInterest:
    def test_zero_short_returns_zero(self):
        assert _score_short_interest(0, 0) == 0

    def test_low_short_below_8pct(self):
        assert _score_short_interest(0.05, 0) == 0

    def test_mild_short_8_to_15(self):
        score = _score_short_interest(0.10, 0)
        assert score == int(WEIGHTS['short_interest'] * 0.3)

    def test_squeeze_zone_15_to_40(self):
        score = _score_short_interest(0.25, 0)
        assert score == int(WEIGHTS['short_interest'] * 0.6)

    def test_extreme_short_above_40(self):
        score = _score_short_interest(0.50, 0)
        assert score == int(WEIGHTS['short_interest'] * 0.4)

    def test_high_dtc_bonus(self):
        score_no_dtc   = _score_short_interest(0.25, 0)
        score_high_dtc = _score_short_interest(0.25, 6)
        assert score_high_dtc > score_no_dtc

    def test_medium_dtc_bonus(self):
        score_no_dtc  = _score_short_interest(0.25, 0)
        score_med_dtc = _score_short_interest(0.25, 3)
        assert score_med_dtc > score_no_dtc

    def test_score_never_exceeds_weight(self):
        assert _score_short_interest(1.0, 100) <= WEIGHTS['short_interest']

    def test_score_never_negative(self):
        assert _score_short_interest(0, 0) >= 0


# ══════════════════════════════════════════════════════════════════════════════
# _score_institutional
# ══════════════════════════════════════════════════════════════════════════════

class TestScoreInstitutional:
    def test_zero_institutional(self):
        assert _score_institutional(0, None) == 0

    def test_small_institutional_presence(self):
        score = _score_institutional(0.10, None)
        assert score == int(WEIGHTS['institutional'] * 0.3)

    def test_medium_institutional(self):
        score = _score_institutional(0.30, None)
        assert score == int(WEIGHTS['institutional'] * 0.5)

    def test_high_institutional(self):
        score = _score_institutional(0.60, None)
        assert score == int(WEIGHTS['institutional'] * 0.6)

    def test_positive_change_adds_bonus(self):
        score_no_change  = _score_institutional(0.30, None)
        score_pos_change = _score_institutional(0.30, 0.05)
        assert score_pos_change > score_no_change

    def test_negative_change_no_bonus(self):
        score_no_change  = _score_institutional(0.30, None)
        score_neg_change = _score_institutional(0.30, -0.05)
        assert score_neg_change == score_no_change

    def test_never_exceeds_weight(self):
        assert _score_institutional(1.0, 1.0) <= WEIGHTS['institutional']


# ══════════════════════════════════════════════════════════════════════════════
# Score boundaries (total score)
# ══════════════════════════════════════════════════════════════════════════════

class TestScoreBoundaries:
    """Test that score cannot exceed 100 or go below 0"""

    def test_signal_label_accepts_100(self):
        assert signal_label(100) == "STRONG BUY"

    def test_signal_label_accepts_0(self):
        assert signal_label(0) == "SKIP"

    def test_all_scoring_functions_bounded(self):
        """No individual scorer returns above its weight"""
        assert _score_rsi(50) <= WEIGHTS['rsi']
        assert _score_short_interest(0.5, 10) <= WEIGHTS['short_interest']
        assert _score_institutional(1.0, 1.0) <= WEIGHTS['institutional']


# ══════════════════════════════════════════════════════════════════════════════
# Score Cache
# ══════════════════════════════════════════════════════════════════════════════

class TestScoreCache:
    def setup_method(self):
        clear()

    def test_put_and_get(self):
        result = {"ticker": "AAPL", "score": 72.5}
        put("AAPL", result, 30)
        cached = get("AAPL", 30)
        assert cached is not None
        assert cached["score"] == 72.5

    def test_different_forecast_days_different_keys(self):
        put("AAPL", {"score": 70}, 30)
        put("AAPL", {"score": 65}, 60)
        assert get("AAPL", 30)["score"] == 70
        assert get("AAPL", 60)["score"] == 65

    def test_miss_returns_none(self):
        assert get("NONEXISTENT", 30) is None

    def test_clear_empties_cache(self):
        put("AAPL", {"score": 70}, 30)
        clear()
        assert get("AAPL", 30) is None

    def test_expired_entry_returns_none(self):
        from src import score_cache as _sc
        import src.score_cache as sc_module
        original_ttl = sc_module.CACHE_TTL
        sc_module.CACHE_TTL = timedelta(seconds=-1)  # already expired
        try:
            put("AAPL", {"score": 70}, 30)
            assert get("AAPL", 30) is None
        finally:
            sc_module.CACHE_TTL = original_ttl

    def test_key_format(self):
        assert _key("AAPL", 30) == "AAPL:30"
        assert _key("TSLA", 60) == "TSLA:60"

    def test_different_tickers_independent(self):
        put("AAPL", {"score": 80}, 30)
        put("TSLA", {"score": 55}, 30)
        assert get("AAPL", 30)["score"] == 80
        assert get("TSLA", 30)["score"] == 55

    def test_overwrite_updates_value(self):
        put("AAPL", {"score": 70}, 30)
        put("AAPL", {"score": 85}, 30)
        assert get("AAPL", 30)["score"] == 85


# ══════════════════════════════════════════════════════════════════════════════
# Keyword Sentiment (market_feed)
# ══════════════════════════════════════════════════════════════════════════════

class TestKeywordSentiment:
    def test_bullish_headline(self):
        assert _keyword_sentiment("Stock surges to record high on strong earnings beat") == "Bullish"

    def test_bearish_headline(self):
        assert _keyword_sentiment("Market crash wipes out gains as recession fears grow") == "Bearish"

    def test_neutral_headline(self):
        assert _keyword_sentiment("Company announces new product launch next quarter") == "Neutral"

    def test_mixed_headline(self):
        # "drops" (bearish) vs "strong" + "growth" (2x bullish) → bullish wins
        result = _keyword_sentiment("Stock drops despite strong growth outlook")
        assert result == "Bullish"

    def test_case_insensitive(self):
        assert _keyword_sentiment("STOCK SURGES TO NEW HIGH") == "Bullish"
        assert _keyword_sentiment("market CRASH fears") == "Bearish"

    def test_empty_string(self):
        assert _keyword_sentiment("") == "Neutral"

    def test_upgrade_is_bullish(self):
        assert _keyword_sentiment("Analyst upgrades stock to strong buy") == "Bullish"

    def test_downgrade_is_bearish(self):
        assert _keyword_sentiment("Analyst downgrades stock on weak outlook") == "Bearish"

    def test_layoff_is_bearish(self):
        assert _keyword_sentiment("Company announces mass layoffs amid declining sales") == "Bearish"

    def test_recovery_is_bullish(self):
        assert _keyword_sentiment("Markets show recovery signs with positive momentum") == "Bullish"


# ══════════════════════════════════════════════════════════════════════════════
# DB Prune (smoke test - no actual DB mutation)
# ══════════════════════════════════════════════════════════════════════════════

class TestDbPrune:
    def test_prune_function_exists(self):
        from src.database import prune_old_data
        assert callable(prune_old_data)

    def test_prune_accepts_days_param(self):
        from src.database import prune_old_data
        import inspect
        sig = inspect.signature(prune_old_data)
        assert 'days_to_keep' in sig.parameters
        assert sig.parameters['days_to_keep'].default == 90
