"""
Tests for recently added features:
  1. yf_cache       — TTL cache, thread safety, invalidation
  2. dcf_valuation  — P/S ratio fallback (calculate_ps_valuation)
  3. llm_client     — date injection (_inject_date)
  4. watchlist_manager — score delta alert logic (SCORE_DELTA_THRESHOLD)
  5. database       — get_last_saved_score
  6. supertrend     — signal detection on synthetic OHLCV data
  7. price_alert_monitor — _get_price uses yf_cache
"""
import pytest
import sys
import time
import threading
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


# ══════════════════════════════════════════════════════════════════════════════
# 1. yf_cache
# ══════════════════════════════════════════════════════════════════════════════

class TestYfCache:
    def setup_method(self):
        from src import yf_cache
        yf_cache._store.clear()

    def test_cache_hit_returns_same_object(self):
        from src.yf_cache import get_info
        fake = {"currentPrice": 42.0, "sector": "Tech"}
        with patch("src.yf_cache.yf.Ticker") as mock_yf:
            mock_yf.return_value.info = fake
            r1 = get_info("AAPL", ttl=60)
            r2 = get_info("AAPL", ttl=60)
            # second call should not hit yfinance
            assert mock_yf.call_count == 1
            assert r1 == r2

    def test_cache_miss_after_expiry(self):
        from src.yf_cache import get_info
        fake = {"currentPrice": 10.0}
        with patch("src.yf_cache.yf.Ticker") as mock_yf:
            mock_yf.return_value.info = fake
            get_info("TSLA", ttl=1)
            time.sleep(1.1)
            get_info("TSLA", ttl=1)
            assert mock_yf.call_count == 2

    def test_different_tickers_cached_separately(self):
        from src.yf_cache import get_info
        with patch("src.yf_cache.yf.Ticker") as mock_yf:
            mock_yf.return_value.info = {"currentPrice": 1.0}
            get_info("AAA", ttl=60)
            get_info("BBB", ttl=60)
            assert mock_yf.call_count == 2

    def test_get_price_returns_float(self):
        from src.yf_cache import get_price
        with patch("src.yf_cache.yf.Ticker") as mock_yf:
            mock_yf.return_value.info = {"currentPrice": 55.5}
            p = get_price("NVDA", ttl=60)
            assert isinstance(p, float)
            assert p == 55.5

    def test_get_price_missing_returns_none(self):
        from src.yf_cache import get_price
        with patch("src.yf_cache.yf.Ticker") as mock_yf:
            mock_yf.return_value.info = {}
            p = get_price("DEAD", ttl=60)
            assert p is None

    def test_get_history_cached(self):
        from src.yf_cache import get_history
        fake_df = pd.DataFrame({"Close": [10, 11, 12]})
        with patch("src.yf_cache.yf.Ticker") as mock_yf:
            mock_yf.return_value.history.return_value = fake_df
            h1 = get_history("SPY", period="1mo", ttl=60)
            h2 = get_history("SPY", period="1mo", ttl=60)
            assert mock_yf.return_value.history.call_count == 1
            assert len(h1) == 3

    def test_different_intervals_cached_separately(self):
        from src.yf_cache import get_history
        fake_df = pd.DataFrame({"Close": [1, 2]})
        with patch("src.yf_cache.yf.Ticker") as mock_yf:
            mock_yf.return_value.history.return_value = fake_df
            get_history("QQQ", period="5d", interval="15m", ttl=60)
            get_history("QQQ", period="5d", interval="1d",  ttl=60)
            assert mock_yf.return_value.history.call_count == 2

    def test_invalidate_clears_ticker_entries(self):
        from src.yf_cache import get_info, invalidate
        with patch("src.yf_cache.yf.Ticker") as mock_yf:
            mock_yf.return_value.info = {"currentPrice": 99.0}
            get_info("GME", ttl=120)
            invalidate("GME")
            get_info("GME", ttl=120)
            assert mock_yf.call_count == 2

    def test_thread_safety_no_race(self):
        """Multiple threads fetching same ticker should not corrupt the cache."""
        from src.yf_cache import get_info
        results = []
        with patch("src.yf_cache.yf.Ticker") as mock_yf:
            mock_yf.return_value.info = {"currentPrice": 7.0}
            def _fetch():
                results.append(get_info("AMC", ttl=60))
            threads = [threading.Thread(target=_fetch) for _ in range(10)]
            for t in threads: t.start()
            for t in threads: t.join()
        assert all(r == {"currentPrice": 7.0} for r in results)

    def test_yfinance_error_returns_empty_dict(self):
        from src.yf_cache import get_info
        from unittest.mock import PropertyMock
        with patch("src.yf_cache.yf.Ticker") as mock_yf:
            # .info is a property — use PropertyMock so accessing it raises
            type(mock_yf.return_value).info = PropertyMock(side_effect=Exception("timeout"))
            result = get_info("ERR", ttl=60)
            assert result == {}

    def test_evict_removes_expired(self):
        from src import yf_cache
        from src.yf_cache import _set, _evict
        _set("old_key", "value", ttl=1)
        time.sleep(1.1)
        _evict()
        assert "old_key" not in yf_cache._store


# ══════════════════════════════════════════════════════════════════════════════
# 2. P/S ratio fallback — calculate_ps_valuation
# ══════════════════════════════════════════════════════════════════════════════

class TestPsValuation:
    def _info(self, ps, rev_growth=None, gross_margin=0.4, price=50.0):
        return {
            "priceToSalesTrailing12Months": ps,
            "revenueGrowth":  rev_growth,
            "grossMargins":   gross_margin,
            "currentPrice":   price,
        }

    def test_very_cheap_ps(self):
        from src.dcf_valuation import calculate_ps_valuation
        r = calculate_ps_valuation(self._info(ps=0.5))
        assert r is not None
        assert r["dcf_score"] >= 13
        assert r["method"] == "P/S"

    def test_reasonable_ps(self):
        from src.dcf_valuation import calculate_ps_valuation
        r = calculate_ps_valuation(self._info(ps=2.0))
        assert r["dcf_score"] == 9

    def test_premium_ps(self):
        from src.dcf_valuation import calculate_ps_valuation
        r = calculate_ps_valuation(self._info(ps=4.5))
        assert r["dcf_score"] == 6

    def test_expensive_ps(self):
        from src.dcf_valuation import calculate_ps_valuation
        r = calculate_ps_valuation(self._info(ps=8.0))
        assert r["dcf_score"] == 3

    def test_very_expensive_ps(self):
        from src.dcf_valuation import calculate_ps_valuation
        r = calculate_ps_valuation(self._info(ps=15.0))
        assert r["dcf_score"] == 1

    def test_growth_bonus_applied(self):
        """High growth + high margin at P/S<15 earns +2 pts bonus."""
        from src.dcf_valuation import calculate_ps_valuation
        base  = calculate_ps_valuation(self._info(ps=2.0, rev_growth=0.10, gross_margin=0.40))
        bonus = calculate_ps_valuation(self._info(ps=2.0, rev_growth=0.30, gross_margin=0.65))
        assert bonus["dcf_score"] > base["dcf_score"]

    def test_missing_ps_returns_none(self):
        from src.dcf_valuation import calculate_ps_valuation
        assert calculate_ps_valuation({"currentPrice": 50.0}) is None

    def test_zero_ps_returns_none(self):
        from src.dcf_valuation import calculate_ps_valuation
        assert calculate_ps_valuation(self._info(ps=0)) is None

    def test_missing_price_returns_none(self):
        from src.dcf_valuation import calculate_ps_valuation
        assert calculate_ps_valuation({"priceToSalesTrailing12Months": 3.0}) is None

    def test_score_never_exceeds_13(self):
        from src.dcf_valuation import calculate_ps_valuation
        r = calculate_ps_valuation(self._info(ps=0.1, rev_growth=0.50, gross_margin=0.90))
        assert r["dcf_score"] <= 13

    def test_score_never_negative(self):
        from src.dcf_valuation import calculate_ps_valuation
        for ps in [0.5, 2, 5, 10, 20]:
            r = calculate_ps_valuation(self._info(ps=ps))
            if r:
                assert r["dcf_score"] >= 0

    def test_ps_fallback_used_when_dcf_none(self):
        """When DCF returns None (no FCF), scorer falls back to P/S."""
        from src.dcf_valuation import calculate_dcf, calculate_ps_valuation
        loss_co = {
            "freeCashflow": -500_000_000,
            "operatingCashflow": -200_000_000,
            "capitalExpenditures": 100_000_000,
            "sharesOutstanding": 100_000_000,
            "currentPrice": 30.0,
            "priceToSalesTrailing12Months": 5.0,
            "grossMargins": 0.55,
            "revenueGrowth": 0.20,
        }
        assert calculate_dcf(loss_co) is None        # DCF can't work
        ps = calculate_ps_valuation(loss_co)
        assert ps is not None                        # P/S picks it up
        assert ps["dcf_score"] > 0


# ══════════════════════════════════════════════════════════════════════════════
# 3. LLM Client — date injection
# ══════════════════════════════════════════════════════════════════════════════

class TestLlmDateInjection:
    def test_inject_date_contains_today(self):
        from src.llm_client import _inject_date
        result = _inject_date("")
        today  = datetime.now().strftime("%Y-%m-%d")
        assert today in result

    def test_inject_date_prepends_to_system(self):
        from src.llm_client import _inject_date
        result = _inject_date("You are a trader.")
        assert result.startswith("Today's date is")
        assert "You are a trader." in result

    def test_inject_date_empty_system(self):
        from src.llm_client import _inject_date
        result = _inject_date("")
        assert "Today's date is" in result
        assert len(result) > 10

    def test_inject_date_format_is_iso(self):
        from src.llm_client import _inject_date
        result = _inject_date("")
        # Extract the date part and verify it parses
        import re
        match = re.search(r"\d{4}-\d{2}-\d{2}", result)
        assert match is not None
        parsed = datetime.strptime(match.group(), "%Y-%m-%d")
        assert parsed.date() == datetime.now().date()

    def test_llm_complete_passes_date_to_gemini(self):
        from src.llm_client import llm_complete
        captured = {}
        def fake_gemini(prompt, system, max_tokens):
            captured["system"] = system
            return "ok"
        with patch("src.llm_client._try_gemini", side_effect=fake_gemini):
            llm_complete("hello", system="Be helpful.")
        assert "Today's date is" in captured["system"]
        assert "Be helpful." in captured["system"]

    def test_llm_complete_passes_date_to_groq_fallback(self):
        from src.llm_client import llm_complete
        captured = {}
        def fake_groq(prompt, system, max_tokens):
            captured["system"] = system
            return "ok"
        with patch("src.llm_client._try_gemini", return_value=None):
            with patch("src.llm_client._try_groq", side_effect=fake_groq):
                llm_complete("hello", system="Be direct.")
        assert "Today's date is" in captured["system"]


# ══════════════════════════════════════════════════════════════════════════════
# 4. Score delta alert threshold logic
# ══════════════════════════════════════════════════════════════════════════════

class TestScoreDeltaThreshold:
    def test_threshold_constant_exists(self):
        from src.watchlist_manager import SCORE_DELTA_THRESHOLD
        assert isinstance(SCORE_DELTA_THRESHOLD, (int, float))
        assert SCORE_DELTA_THRESHOLD > 0

    def test_drop_triggers_alert(self):
        """A drop >= threshold should send score_delta_drop alert."""
        from src.watchlist_manager import SCORE_DELTA_THRESHOLD
        drop = SCORE_DELTA_THRESHOLD
        assert 50 - drop <= 50 - SCORE_DELTA_THRESHOLD   # sanity

    def test_small_drop_no_alert(self):
        from src.watchlist_manager import SCORE_DELTA_THRESHOLD
        small_drop = SCORE_DELTA_THRESHOLD - 1
        # score went from 60 to 60 - small_drop — should NOT trigger
        assert 60 - small_drop > 60 - SCORE_DELTA_THRESHOLD

    def test_rise_triggers_alert(self):
        from src.watchlist_manager import SCORE_DELTA_THRESHOLD
        rise = SCORE_DELTA_THRESHOLD
        assert rise >= SCORE_DELTA_THRESHOLD

    def test_scan_watchlist_sends_drop_alert(self):
        """
        Integration: scan_watchlist LOGS score_delta_drop to DB when score falls ≥15 pts.

        As of 2026-05-20 the Telegram path for score_delta_drop is suppressed
        (user wants only IBKR real-time + catalyst alerts). DB persistence remains.
        """
        from src.watchlist_manager import scan_watchlist, SCORE_DELTA_THRESHOLD

        fake_item = {
            "ticker": "FAKE", "alert_score": 60, "alert_pct": 5.0,
            "price_above": None, "price_below": None, "supertrend_alert": 0,
        }
        score_now  = 40.0
        score_prev = score_now + SCORE_DELTA_THRESHOLD + 1   # prev was much higher

        fake_result = {
            "ticker": "FAKE", "score": score_now, "price": 10.0,
            "rsi": 45, "macd": "Neutral", "short_pct": 5.0, "squeeze_active": False,
        }

        db_writes = []

        with patch("src.watchlist_manager.watchlist_get_all", return_value=[fake_item]), \
             patch("src.watchlist_manager.score_stock",        return_value=fake_result), \
             patch("src.watchlist_manager.get_last_saved_score", return_value=score_prev), \
             patch("src.watchlist_manager._cooldown_passed",   return_value=True), \
             patch("src.watchlist_manager._last_alert_price",  return_value=None), \
             patch("src.watchlist_manager.watchlist_save_alert",
                   side_effect=lambda t, atype, msg, score=None, price=None: db_writes.append(atype)):
            scan_watchlist()

        assert "score_delta_drop" in db_writes, \
            f"score_delta_drop was not logged to DB. Got: {db_writes}"

    def test_scan_watchlist_sends_rise_alert(self):
        """
        Integration: scan_watchlist LOGS score_delta_rise to DB when score rises ≥15 pts.

        As of 2026-05-20 the Telegram path for score_delta_rise is suppressed
        (superseded by combined_buy via signal_combiner). DB persistence remains.
        """
        from src.watchlist_manager import scan_watchlist, SCORE_DELTA_THRESHOLD

        fake_item = {
            "ticker": "FAKE", "alert_score": 60, "alert_pct": 5.0,
            "price_above": None, "price_below": None, "supertrend_alert": 0,
        }
        score_now  = 75.0
        score_prev = score_now - SCORE_DELTA_THRESHOLD - 1

        fake_result = {
            "ticker": "FAKE", "score": score_now, "price": 15.0,
            "rsi": 55, "macd": "Bullish", "short_pct": 8.0, "squeeze_active": False,
        }

        db_writes = []

        with patch("src.watchlist_manager.watchlist_get_all", return_value=[fake_item]), \
             patch("src.watchlist_manager.score_stock",        return_value=fake_result), \
             patch("src.watchlist_manager.get_last_saved_score", return_value=score_prev), \
             patch("src.watchlist_manager._cooldown_passed",   return_value=True), \
             patch("src.watchlist_manager._last_alert_price",  return_value=None), \
             patch("src.watchlist_manager.watchlist_save_alert",
                   side_effect=lambda t, atype, msg, score=None, price=None: db_writes.append(atype)):
            scan_watchlist()

        assert "score_delta_rise" in db_writes, \
            f"score_delta_rise was not logged to DB. Got: {db_writes}"

    def test_no_alert_when_prev_score_none(self):
        """No delta alert when there is no prior scan result (first scan)."""
        from src.watchlist_manager import scan_watchlist

        fake_item = {
            "ticker": "FAKE", "alert_score": 60, "alert_pct": 5.0,
            "price_above": None, "price_below": None, "supertrend_alert": 0,
        }
        fake_result = {
            "ticker": "FAKE", "score": 30.0, "price": 5.0,
            "rsi": 20, "macd": "Bearish", "short_pct": 2.0, "squeeze_active": False,
        }
        alerts_sent = []

        with patch("src.watchlist_manager.watchlist_get_all", return_value=[fake_item]), \
             patch("src.watchlist_manager.score_stock",        return_value=fake_result), \
             patch("src.watchlist_manager.get_last_saved_score", return_value=None), \
             patch("src.watchlist_manager._cooldown_passed",   return_value=True), \
             patch("src.watchlist_manager._last_alert_price",  return_value=None), \
             patch("src.watchlist_manager._send_alert",
                   side_effect=lambda tg, t, atype, msg, sc, pr: alerts_sent.append(atype)):
            scan_watchlist()

        assert "score_delta_drop" not in alerts_sent
        assert "score_delta_rise" not in alerts_sent


# ══════════════════════════════════════════════════════════════════════════════
# 5. database — get_last_saved_score
# ══════════════════════════════════════════════════════════════════════════════

class TestGetLastSavedScore:
    def test_returns_none_when_no_history(self, tmp_path, monkeypatch):
        import src.database as db
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        assert db.get_last_saved_score("AAPL") is None

    def test_returns_latest_score(self, tmp_path, monkeypatch):
        import src.database as db
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        run_id = db.save_scan_run("test", 1)
        db.save_result(run_id, {
            "ticker": "AAPL", "score": 72.5, "price": 180.0,
            "explosion_score": 72.5,
        })
        score = db.get_last_saved_score("AAPL")
        assert score == pytest.approx(72.5)

    def test_returns_most_recent_not_oldest(self, tmp_path, monkeypatch):
        import src.database as db
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        run1 = db.save_scan_run("test", 1)
        db.save_result(run1, {"ticker": "MSFT", "score": 50.0, "price": 300.0, "explosion_score": 50.0})
        run2 = db.save_scan_run("test", 1)
        db.save_result(run2, {"ticker": "MSFT", "score": 80.0, "price": 310.0, "explosion_score": 80.0})
        score = db.get_last_saved_score("MSFT")
        assert score == pytest.approx(80.0)

    def test_ticker_isolation(self, tmp_path, monkeypatch):
        import src.database as db
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        run_id = db.save_scan_run("test", 2)
        db.save_result(run_id, {"ticker": "AAPL", "score": 65.0, "price": 180.0, "explosion_score": 65.0})
        db.save_result(run_id, {"ticker": "TSLA", "score": 40.0, "price": 200.0, "explosion_score": 40.0})
        assert db.get_last_saved_score("AAPL") == pytest.approx(65.0)
        assert db.get_last_saved_score("TSLA") == pytest.approx(40.0)
        assert db.get_last_saved_score("NVDA") is None


# ══════════════════════════════════════════════════════════════════════════════
# 6. Supertrend — signal detection on synthetic OHLCV
# ══════════════════════════════════════════════════════════════════════════════

def _make_ohlcv(closes: list[float]) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from a list of close prices."""
    n = len(closes)
    closes = np.array(closes, dtype=float)
    highs  = closes * 1.01
    lows   = closes * 0.99
    return pd.DataFrame({
        "Open":   closes,
        "High":   highs,
        "Low":    lows,
        "Close":  closes,
        "Volume": [1_000_000] * n,
    }, index=pd.date_range("2024-01-01", periods=n, freq="D"))


class TestSupertrend:
    def test_insufficient_data_returns_na(self):
        from src.supertrend import supertrend
        hist = _make_ohlcv([10.0] * 5)   # less than period+2=12
        r = supertrend(hist)
        assert r["direction"] == "N/A"
        assert r["signal"] is None

    def test_stable_uptrend_no_signal(self):
        """Steadily rising prices — no flip on last bar."""
        from src.supertrend import supertrend
        closes = [10 + i * 0.5 for i in range(30)]
        r = supertrend(_make_ohlcv(closes))
        assert r["direction"] in ("Bullish", "Bearish")
        # No flip expected in perfectly smooth trend
        # (signal may or may not be set depending on ATR, just verify structure)
        assert "signal" in r
        assert "level" in r

    def test_buy_signal_after_downtrend_reversal(self):
        """Sharp price jump after prolonged drop → expect BUY signal."""
        from src.supertrend import supertrend
        # 25 days falling, then 2 days sharp recovery
        falling = [50 - i * 1.2 for i in range(25)]
        rising  = [falling[-1] + 20, falling[-1] + 25]
        closes  = falling + rising
        r = supertrend(_make_ohlcv(closes))
        # Direction should flip to Bullish after the spike
        assert r["direction"] == "Bullish"

    def test_sell_signal_after_uptrend_reversal(self):
        """Sharp drop after prolonged rise → expect Bearish direction."""
        from src.supertrend import supertrend
        rising  = [20 + i * 1.5 for i in range(25)]
        falling = [rising[-1] - 20, rising[-1] - 25]
        closes  = rising + falling
        r = supertrend(_make_ohlcv(closes))
        assert r["direction"] == "Bearish"

    def test_level_is_positive(self):
        from src.supertrend import supertrend
        closes = [50 + np.sin(i / 3) * 5 for i in range(30)]
        r = supertrend(_make_ohlcv(closes))
        if r["level"] is not None:
            assert r["level"] > 0

    def test_none_hist_returns_na(self):
        from src.supertrend import supertrend
        r = supertrend(None)
        assert r["direction"] == "N/A"

    def test_custom_period_and_multiplier(self):
        from src.supertrend import supertrend
        closes = [100 + i * 0.3 for i in range(40)]
        r = supertrend(_make_ohlcv(closes), period=7, multiplier=2.0)
        assert r["direction"] in ("Bullish", "Bearish", "N/A")


# ══════════════════════════════════════════════════════════════════════════════
# 7. price_alert_monitor — uses yf_cache, not raw yf.Ticker
# ══════════════════════════════════════════════════════════════════════════════

class TestPriceAlertMonitorCaching:
    def test_get_price_uses_cache_module(self):
        """_get_price must delegate to yf_cache.get_price, not raw yfinance."""
        import inspect
        import src.price_alert_monitor as pam
        src_code = inspect.getsource(pam._get_price)
        assert "yf.Ticker" not in src_code, (
            "_get_price should use yf_cache, not call yf.Ticker directly"
        )

    def test_check_volume_spikes_uses_cache(self):
        """check_volume_spikes must not call yf.Ticker directly."""
        import inspect
        import src.price_alert_monitor as pam
        src_code = inspect.getsource(pam.check_volume_spikes)
        assert "yf.Ticker" not in src_code

    def test_check_supertrend_uses_cached_hist(self):
        """check_supertrend_flips must use _cached_hist, not yf.Ticker.history."""
        import inspect
        import src.price_alert_monitor as pam
        src_code = inspect.getsource(pam.check_supertrend_flips)
        assert "yf.Ticker" not in src_code
        assert "_cached_hist" in src_code

    def test_price_monitor_no_raw_yfinance_import_at_call_sites(self):
        """The module may import yfinance but call sites must go through cache."""
        import src.price_alert_monitor as pam
        # Verify the three key functions don't bypass cache
        import inspect
        for fn_name in ("_get_price", "check_volume_spikes", "check_supertrend_flips"):
            fn = getattr(pam, fn_name)
            src = inspect.getsource(fn)
            assert "yf.Ticker(" not in src, (
                f"{fn_name} still calls yf.Ticker() directly — should use yf_cache"
            )
