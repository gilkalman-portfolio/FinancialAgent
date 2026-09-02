"""
Test: Fear & Greed Index widget on the Market page.

Background: page_market.py previously only showed a raw VIX gauge — no
composite sentiment index existed anywhere in the app (Open Backlog item).
src.market_regime.fear_greed_score()/get_fear_greed_index() build a 0-100
composite from signals this app already computes (VIX level + SPY-vs-SMA200
trend, both sourced from market_regime.get_regime()) — a good-faith
approximation of CNN's Fear & Greed Index, not the real 7-factor methodology.

Two things are covered, per this repo's post-2026-08-18 convention of
verifying pages actually render (see CLAUDE.md Incident Archive,
"Three dashboard pages crashing on every load"):

(a) the Market page still renders without crashing with the new widget
    present, exercised via Streamlit's AppTest framework (no committed
    AppTest-based test existed yet in this repo — this is the first one);
(b) the composite-score computation itself has basic unit coverage: a
    known-fear input combination and a known-greed input combination must
    land on the correct side of 50.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_page_market_fear_greed.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.market_regime import (  # noqa: E402
    fear_greed_level,
    fear_greed_score,
    get_fear_greed_index,
)


# ── fear_greed_score() — pure function, no network/DB ───────────────────────

class TestFearGreedScore:
    def test_known_fear_combination_lands_below_50(self):
        """High VIX (well above the 'fully fear' anchor) + SPY well below its
        200-day SMA must score on the fear side of the scale."""
        score = fear_greed_score(vix=45.0, spy_vs_sma200_pct=-20.0)
        assert score < 50
        assert fear_greed_level(score)["label"] in ("Extreme Fear", "Fear")

    def test_known_greed_combination_lands_above_50(self):
        """Low VIX (well below the 'fully fear' anchor) + SPY well above its
        200-day SMA must score on the greed side of the scale."""
        score = fear_greed_score(vix=8.0, spy_vs_sma200_pct=20.0)
        assert score > 50
        assert fear_greed_level(score)["label"] in ("Extreme Greed", "Greed")

    def test_neutral_inputs_land_near_50(self):
        """VIX and SPY exactly at their midpoints should land close to neutral."""
        score = fear_greed_score(vix=25.0, spy_vs_sma200_pct=0.0)
        assert 40 <= score <= 60

    def test_score_bounded_0_to_100(self):
        """Extreme/out-of-range inputs must clamp, never go outside [0, 100]."""
        assert fear_greed_score(vix=999.0, spy_vs_sma200_pct=-999.0) == 0
        assert fear_greed_score(vix=-10.0, spy_vs_sma200_pct=999.0) == 100

    def test_monotonic_in_vix(self):
        """Lower VIX (holding SPY fixed) must never produce a lower score."""
        lo = fear_greed_score(vix=12.0, spy_vs_sma200_pct=5.0)
        hi = fear_greed_score(vix=35.0, spy_vs_sma200_pct=5.0)
        assert lo > hi

    def test_monotonic_in_spy(self):
        """Higher SPY-vs-SMA200 (holding VIX fixed) must never produce a lower score."""
        lo = fear_greed_score(vix=18.0, spy_vs_sma200_pct=-10.0)
        hi = fear_greed_score(vix=18.0, spy_vs_sma200_pct=10.0)
        assert hi > lo


class TestFearGreedLevel:
    def test_band_boundaries_partition_0_to_100(self):
        for score in range(0, 101):
            level = fear_greed_level(score)
            assert level["label"] in (
                "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed",
            )
            assert level["color"].startswith("#")


class TestGetFearGreedIndex:
    def test_falls_back_to_unavailable_on_regime_fetch_failure(self):
        """get_regime()'s own _caution_fallback() reports vix=0.0 as a sentinel
        for 'the fetch failed', not a real reading of extreme calm. That
        sentinel must not be interpreted as a real (fabricated) greed score."""
        fallback_regime = {
            "regime": "CAUTION", "vix": 0.0, "spy_price": 0.0, "spy_sma200": 0.0,
            "spy_vs_sma200_pct": 0.0, "multiplier": 0.5, "cached_at": "irrelevant",
        }
        with patch("src.market_regime.get_regime", return_value=fallback_regime):
            result = get_fear_greed_index()
        assert result["score"] is None
        assert result["label"] == "Unavailable"

    def test_real_regime_data_produces_numeric_score(self):
        real_regime = {
            "regime": "BULL", "vix": 14.0, "spy_price": 520.0, "spy_sma200": 490.0,
            "spy_vs_sma200_pct": 6.12, "multiplier": 1.0, "cached_at": "irrelevant",
        }
        with patch("src.market_regime.get_regime", return_value=real_regime):
            result = get_fear_greed_index()
        assert isinstance(result["score"], int)
        assert 0 <= result["score"] <= 100
        assert result["label"] != "Unavailable"


# ── Page-render coverage (AppTest) ───────────────────────────────────────────

FAKE_INDICES = [
    {"name": "S&P 500", "symbol": "^GSPC", "category": "index", "price": 5000.0, "change": 0.5, "up": True},
    {"name": "VIX",     "symbol": "^VIX",  "category": "vix",   "price": 18.5,   "change": -1.2, "up": False},
]
FAKE_MOOD = {"label": "Neutral", "bullish": 0, "bearish": 0, "neutral": 0, "score": 50}
FAKE_REGIME_BULL = {
    "regime": "BULL", "vix": 14.0, "spy_price": 520.0, "spy_sma200": 490.0,
    "spy_vs_sma200_pct": 6.12, "multiplier": 1.0, "cached_at": "irrelevant",
}
FAKE_REGIME_BEAR = {
    "regime": "BEAR", "vix": 42.0, "spy_price": 400.0, "spy_sma200": 480.0,
    "spy_vs_sma200_pct": -16.67, "multiplier": 0.3, "cached_at": "irrelevant",
}

@pytest.fixture
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="page_market_fg_")
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


def _render_market_page(regime: dict, indices: list, mood: dict):
    """Calls page_market.render() directly instead of AppTest.from_file() on
    the whole dashboard.py, which was found to leave Streamlit internal state
    that breaks a LATER AppTest.from_function() test in the same pytest
    process (order-dependent, reproducible with just this file plus
    tests/test_page_scheduler_save.py). Mirrors that file's own pattern.

    AppTest.from_function() re-executes only this function's own extracted
    source as a standalone script - it does NOT carry over this module's
    top-level imports or globals, so everything used below must be either a
    local import or an explicit argument (hence indices/mood are passed in
    instead of read from the module-level FAKE_INDICES/FAKE_MOOD constants).

    NOTE: keep this docstring and this function body plain ASCII. A non-ASCII
    character here (an em dash was here originally) breaks AppTest.from_function()
    on this Windows/Python setup: it round-trips the function's source through
    a temp file using a non-UTF-8 default write encoding, then Streamlit's
    script cache reads that file back as UTF-8 and throws UnicodeDecodeError."""
    from unittest.mock import patch
    import _pages_modules.page_market as page_market

    with patch("src.market_feed.get_market_indices", return_value=indices), \
         patch("src.market_feed.get_futures", return_value=[]), \
         patch("src.market_feed.get_market_news", return_value=[]), \
         patch("src.market_feed.get_market_mood", return_value=mood), \
         patch("src.market_feed.get_upcoming_macro", return_value=[]), \
         patch("src.market_feed.get_earnings_calendar", return_value=[]), \
         patch("src.market_regime.get_regime", return_value=regime), \
         patch("yfinance.download") as mock_dl:
        import pandas as pd
        mock_dl.return_value = pd.DataFrame()  # sector heatmap: no data, handled gracefully
        page_market.render()


def _run_market_page(regime: dict):
    """Returns the AppTest instance for assertions."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_render_market_page, args=(regime, FAKE_INDICES, FAKE_MOOD))
    at.run(timeout=30)
    return at


class TestMarketPageRenders:
    def test_market_page_renders_without_crashing(self, temp_db):
        at = _run_market_page(FAKE_REGIME_BULL)
        assert not at.exception, f"Market page raised: {at.exception}"

    def test_fear_greed_card_present_in_bull_scenario(self, temp_db):
        at = _run_market_page(FAKE_REGIME_BULL)
        assert not at.exception
        rendered = "\n".join(md.value for md in at.markdown)
        assert "Fear" in rendered and "Greed" in rendered

    def test_market_page_renders_without_crashing_bear_scenario(self, temp_db):
        """Same page, opposite-extreme inputs — must render distinctly, not crash."""
        at = _run_market_page(FAKE_REGIME_BEAR)
        assert not at.exception, f"Market page raised: {at.exception}"
        rendered = "\n".join(md.value for md in at.markdown)
        assert "Fear" in rendered and "Greed" in rendered
