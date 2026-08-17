"""
Test: earnings_sentiment.py no longer scores "no data" the same as a
confirmed bad result.

Background (2026-08-17): _edgar_eps_fallback() returned score=0 both when
EDGAR genuinely had no EPS data (yoy is None) and in the worst real case
(a >15% YoY decline) — the same number for two very different situations.
stock_scorer.py feeds `_es["score"]` straight into the composite without
checking `_es["source"]` (which already correctly distinguishes them), so a
ticker with simply no earnings data was penalized as if it had consistently
disappointing earnings. The fix: "no data" now returns score=2 (the
already-existing "neutral/inline" tier), not 0. source stays "none".

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_earnings_sentiment_no_data.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.earnings_sentiment import _edgar_eps_fallback, get_earnings_sentiment


class TestEdgarFallbackNoData:
    def test_no_yoy_data_returns_neutral_not_zero(self):
        with patch("src.edgar_fcf.get_eps_yoy_growth", return_value=None):
            result = _edgar_eps_fallback("TEST")
        assert result["score"] == 2, "no data must score neutral (2), not worst-case (0)"
        assert result["sentiment"] == "neutral"
        assert result["source"] == "none"

    def test_edgar_exception_returns_neutral_not_zero(self):
        with patch("src.edgar_fcf.get_eps_yoy_growth", side_effect=RuntimeError("EDGAR down")):
            result = _edgar_eps_fallback("TEST")
        assert result["score"] == 2
        assert result["source"] == "none"

    def test_genuinely_bad_earnings_still_scores_zero(self):
        """A real, confirmed >15% YoY decline must still score 0 — this fix
        is about not-fabricating a bad score, not about being lenient on
        real bad news."""
        with patch("src.edgar_fcf.get_eps_yoy_growth", return_value=-0.30):
            result = _edgar_eps_fallback("TEST")
        assert result["score"] == 0
        assert result["sentiment"] == "bearish"
        assert result["source"] == "edgar_eps_yoy"

    def test_genuinely_good_earnings_scores_high(self):
        with patch("src.edgar_fcf.get_eps_yoy_growth", return_value=0.35):
            result = _edgar_eps_fallback("TEST")
        assert result["score"] == 5
        assert result["sentiment"] == "bullish"


class TestGetEarningsSentimentTopLevelFallback:
    def test_no_finnhub_key_returns_neutral_not_zero(self):
        result = get_earnings_sentiment("TEST", finnhub_key="")
        assert result["score"] == 2
        assert result["source"] == "none"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
