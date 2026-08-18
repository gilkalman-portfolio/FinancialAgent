"""
Tests for src/gap_scanner.py (scan_premarket_gaps / scan_opening_prints) and
their scheduler.py wrappers (run_premarket_gap_alert / run_opening_print_alert).

Regression anchor: NMAX on 2026-08-14 had a nominal premarket price wobble
but ZERO premarket volume — the whole move happened in the first few minutes
of the regular session instead. scan_premarket_gaps must exclude that case
(test_scan_premarket_gaps_excludes_zero_premarket_volume); scan_opening_prints
must catch it (test_scan_opening_prints_detects_move). See CLAUDE.md Incident
Archive, 2026-08-15.

Per CLAUDE.md ("Never seed a time-filtered query with a literal date"), every
synthetic timestamp below is built relative to "now" — never a hardcoded date.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_gap_scanner.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import scheduler  # noqa: E402


# ── Synthetic frame builders ────────────────────────────────────────────────

def _premarket_index(n_bars: int = 6, start_hour: int = 8, start_minute: int = 0) -> pd.DatetimeIndex:
    """n 5-min bars starting today at `start_hour`:`start_minute` ET — genuinely
    inside the 04:00-09:30 premarket window."""
    et_now = pd.Timestamp.now(tz="America/New_York")
    start = et_now.normalize() + pd.Timedelta(hours=start_hour, minutes=start_minute)
    return pd.date_range(start=start, periods=n_bars, freq="5min", tz="America/New_York")


def _opening_index(n_bars: int = 7) -> pd.DatetimeIndex:
    """n 1-min bars starting today at the 09:30 ET open print."""
    et_now = pd.Timestamp.now(tz="America/New_York")
    start = et_now.normalize() + pd.Timedelta(hours=9, minutes=30)
    return pd.date_range(start=start, periods=n_bars, freq="1min", tz="America/New_York")


def _prior_daily_index(n_bars: int = 4) -> pd.DatetimeIndex:
    """n business-day bars ending yesterday (tz-naive, matching yfinance daily bars)."""
    et_today = pd.Timestamp.now(tz="America/New_York").normalize().tz_localize(None)
    return pd.bdate_range(end=et_today - pd.Timedelta(days=1), periods=n_bars)


def _make_frame(index: pd.DatetimeIndex, closes_by_ticker: dict, volumes_by_ticker: dict = None) -> pd.DataFrame:
    """yf.download(...)-shaped MultiIndex frame: columns=(field, ticker).
    Open is set equal to Close so tests can treat closes_by_ticker[t][0] as
    both 'the open print' and 'the first close' — exactly what the code reads."""
    tickers = list(closes_by_ticker.keys())
    close_df = pd.DataFrame({t: pd.Series(v, index=index) for t, v in closes_by_ticker.items()})
    if volumes_by_ticker is None:
        volumes_by_ticker = {t: [0] * len(index) for t in tickers}
    volume_df = pd.DataFrame({t: pd.Series(v, index=index) for t, v in volumes_by_ticker.items()})
    fields = {
        "Close": close_df,
        "Open": close_df,
        "High": close_df * 1.001,
        "Low": close_df * 0.999,
        "Volume": volume_df,
    }
    return pd.concat(fields, axis=1)


# ══════════════════════════════════════════════════════════════════════════
# scan_premarket_gaps — Massive/Polygon-based (rewritten 2026-08-16; see
# CLAUDE.md Incident Archive for why yfinance was replaced)
# ══════════════════════════════════════════════════════════════════════════

def _bar(c: float, v: float) -> dict:
    """A single already-filtered premarket bar as _fetch_premarket_bars
    returns it (t/o/h/l omitted — the orchestration layer only reads c/v)."""
    return {"t": 0, "o": c, "h": c, "l": c, "c": c, "v": v}


class TestScanPremarketGaps:
    """Mocks at the _fetch_prior_close / _fetch_premarket_bars / preflight
    seam — the same mid-level-helper mocking style the old tests used for
    _download_with_retry. MASSIVE_API_KEY is patched to a dummy non-empty
    value so tests are deterministic regardless of the real .env."""

    def test_detects_real_gap(self):
        from src.gap_scanner import scan_premarket_gaps

        bars = [_bar(11.0, 5000), _bar(11.2, 4000), _bar(11.6, 2000)]
        with patch("src.gap_scanner.MASSIVE_API_KEY", "test-key"), \
             patch("src.gap_scanner._preflight_check_api_key", return_value=True), \
             patch("src.gap_scanner._fetch_prior_close", return_value=10.0), \
             patch("src.gap_scanner._fetch_premarket_bars", return_value=bars):
            results = scan_premarket_gaps(["GAPPER"], gap_pct_min=8.0, min_premarket_dollar_volume=50_000)

        assert len(results) == 1
        r = results[0]
        assert r["ticker"] == "GAPPER"
        assert r["prior_close"] == pytest.approx(10.0)
        assert r["gap_pct"] == pytest.approx((11.6 - 10.0) / 10.0 * 100, abs=0.5)
        assert r["premarket_dollar_volume"] > 50_000

    def test_excludes_zero_premarket_volume(self):
        """The literal NMAX regression case: price differs from prior close but
        every premarket bar has zero volume — must be excluded regardless of
        the nominal price move."""
        from src.gap_scanner import scan_premarket_gaps

        bars = [_bar(9.72, 0), _bar(9.56, 0), _bar(9.79, 0)]
        with patch("src.gap_scanner.MASSIVE_API_KEY", "test-key"), \
             patch("src.gap_scanner._preflight_check_api_key", return_value=True), \
             patch("src.gap_scanner._fetch_prior_close", return_value=9.51), \
             patch("src.gap_scanner._fetch_premarket_bars", return_value=bars):
            results = scan_premarket_gaps(["NMAX"], gap_pct_min=8.0, min_premarket_dollar_volume=200_000)

        assert results == []

    def test_no_premarket_bars_excludes_ticker(self):
        """The WEST 2026-07-27 case: the fetch succeeds but there were
        genuinely zero premarket bars — must be excluded silently, not
        treated as an error."""
        from src.gap_scanner import scan_premarket_gaps

        with patch("src.gap_scanner.MASSIVE_API_KEY", "test-key"), \
             patch("src.gap_scanner._preflight_check_api_key", return_value=True), \
             patch("src.gap_scanner._fetch_prior_close", return_value=7.82), \
             patch("src.gap_scanner._fetch_premarket_bars", return_value=[]):
            results = scan_premarket_gaps(["WEST"], gap_pct_min=8.0, min_premarket_dollar_volume=1000)

        assert results == []

    def test_below_pct_threshold_excluded(self):
        from src.gap_scanner import scan_premarket_gaps

        bars = [_bar(10.1, 5000), _bar(10.2, 5000), _bar(10.3, 5000)]
        with patch("src.gap_scanner.MASSIVE_API_KEY", "test-key"), \
             patch("src.gap_scanner._preflight_check_api_key", return_value=True), \
             patch("src.gap_scanner._fetch_prior_close", return_value=10.0), \
             patch("src.gap_scanner._fetch_premarket_bars", return_value=bars):
            results = scan_premarket_gaps(["MILD"], gap_pct_min=8.0, min_premarket_dollar_volume=1000)

        assert results == []  # only ~3% gap, below the 8% floor

    def test_below_dollar_volume_threshold_excluded(self):
        from src.gap_scanner import scan_premarket_gaps

        bars = [_bar(12.0, 10), _bar(12.0, 10), _bar(12.0, 10)]  # tiny real volume
        with patch("src.gap_scanner.MASSIVE_API_KEY", "test-key"), \
             patch("src.gap_scanner._preflight_check_api_key", return_value=True), \
             patch("src.gap_scanner._fetch_prior_close", return_value=10.0), \
             patch("src.gap_scanner._fetch_premarket_bars", return_value=bars):
            results = scan_premarket_gaps(["THIN"], gap_pct_min=8.0, min_premarket_dollar_volume=200_000)

        assert results == []  # ~20% gap but nowhere near $200k of premarket $vol

    def test_per_ticker_failure_does_not_abort_batch(self):
        """One ticker's prior-close fetch fails; the other, valid ticker must
        still come back — a semantic shift from the old shared-batch-call
        model, where any single failure nuked the whole scan."""
        from src.gap_scanner import scan_premarket_gaps

        def fake_prior_close(ticker):
            if ticker == "BROKEN":
                raise RuntimeError("network down")
            return 10.0

        bars = [_bar(11.0, 5000), _bar(11.6, 5000)]
        with patch("src.gap_scanner.MASSIVE_API_KEY", "test-key"), \
             patch("src.gap_scanner._preflight_check_api_key", return_value=True), \
             patch("src.gap_scanner._fetch_prior_close", side_effect=fake_prior_close), \
             patch("src.gap_scanner._fetch_premarket_bars", return_value=bars):
            results = scan_premarket_gaps(["BROKEN", "GOOD"], gap_pct_min=8.0, min_premarket_dollar_volume=1000)

        assert [r["ticker"] for r in results] == ["GOOD"]

    def test_missing_api_key_skips_scan_without_any_requests(self):
        from src.gap_scanner import scan_premarket_gaps
        with patch("src.gap_scanner.MASSIVE_API_KEY", ""), \
             patch("src.gap_scanner._fetch_prior_close") as fpc, \
             patch("src.gap_scanner._fetch_premarket_bars") as fpb:
            results = scan_premarket_gaps(["AAPL"])
        assert results == []
        fpc.assert_not_called()
        fpb.assert_not_called()

    def test_invalid_api_key_preflight_aborts_scan(self):
        from src.gap_scanner import scan_premarket_gaps
        with patch("src.gap_scanner.MASSIVE_API_KEY", "bad-key"), \
             patch("src.gap_scanner._preflight_check_api_key", return_value=False), \
             patch("src.gap_scanner._fetch_prior_close") as fpc:
            results = scan_premarket_gaps(["AAPL"])
        assert results == []
        fpc.assert_not_called()

    def test_empty_tickers_returns_empty(self):
        from src.gap_scanner import scan_premarket_gaps
        assert scan_premarket_gaps([]) == []


class TestMassiveHttpHelpers:
    """Mocks requests.Session.get (the thread-local pooled session every call
    routes through as of 2026-08-18) — the lower-level parsing/window-filter
    behavior that's newly meaningful now that there's an HTTP layer at all."""

    def test_fetch_premarket_bars_filters_to_true_window(self):
        from src.gap_scanner import _fetch_premarket_bars, _ET

        et_today = datetime.now(_ET).replace(hour=0, minute=0, second=0, microsecond=0)

        def _ms(hour, minute):
            ts = et_today.replace(hour=hour, minute=minute).astimezone(timezone.utc)
            return int(ts.timestamp() * 1000)

        raw_bars = [
            {"t": _ms(2, 0), "c": 9.0, "v": 100},    # before premarket — excluded
            {"t": _ms(6, 0), "c": 9.5, "v": 200},    # genuine premarket
            {"t": _ms(9, 25), "c": 9.7, "v": 300},   # genuine premarket
            {"t": _ms(9, 30), "c": 9.9, "v": 400},   # regular session open — excluded
            {"t": _ms(15, 0), "c": 10.5, "v": 500},  # regular session — excluded
        ]
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"status": "OK", "results": raw_bars}

        with patch("requests.Session.get", return_value=mock_resp):
            bars = _fetch_premarket_bars("AAPL", et_today.strftime("%Y-%m-%d"))

        assert [b["v"] for b in bars] == [200, 300]

    def test_fetch_prior_close_parses_response(self):
        from src.gap_scanner import _fetch_prior_close

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"status": "OK", "results": [{"c": 42.5}]}
        with patch("requests.Session.get", return_value=mock_resp):
            assert _fetch_prior_close("AAPL") == pytest.approx(42.5)

    def test_fetch_prior_close_non_200_returns_none_without_raising(self):
        from src.gap_scanner import _fetch_prior_close

        mock_resp = MagicMock(status_code=500)
        with patch("requests.Session.get", return_value=mock_resp):
            assert _fetch_prior_close("AAPL") is None

    def test_preflight_fails_only_on_401_403(self):
        from src.gap_scanner import _preflight_check_api_key

        for status in (401, 403):
            mock_resp = MagicMock(status_code=status)
            with patch("requests.Session.get", return_value=mock_resp):
                assert _preflight_check_api_key() is False

        for status in (200, 404, 429, 500):
            mock_resp = MagicMock(status_code=status)
            mock_resp.json.return_value = {"status": "OK", "results": [{"c": 1.0}]}
            with patch("requests.Session.get", return_value=mock_resp):
                assert _preflight_check_api_key() is True  # fail-open


# ══════════════════════════════════════════════════════════════════════════
# scan_opening_prints
# ══════════════════════════════════════════════════════════════════════════

class TestScanOpeningPrints:
    def test_detects_move(self):
        """Mirrors NMAX's actual first four 1-min bars: heavy volume, big move."""
        from src.gap_scanner import scan_opening_prints

        idx = _opening_index(n_bars=4)
        frame = _make_frame(
            idx,
            closes_by_ticker={"NMAX": [9.895, 10.57, 10.74, 11.11]},
            volumes_by_ticker={"NMAX": [245016, 69234, 67817, 57442]},
        )
        with patch("src.gap_scanner._download_with_retry", return_value=frame):
            results = scan_opening_prints(["NMAX"], pct_move_min=10.0, min_dollar_volume=500_000)

        assert len(results) == 1
        r = results[0]
        assert r["open_print"] == pytest.approx(9.895)
        assert r["move_pct"] == pytest.approx((11.11 - 9.895) / 9.895 * 100, abs=0.1)
        assert r["dollar_volume"] > 500_000

    def test_below_pct_threshold_excluded(self):
        from src.gap_scanner import scan_opening_prints
        idx = _opening_index(n_bars=4)
        frame = _make_frame(
            idx,
            closes_by_ticker={"FLAT": [20.0, 20.1, 20.2, 20.3]},
            volumes_by_ticker={"FLAT": [100_000] * 4},
        )
        with patch("src.gap_scanner._download_with_retry", return_value=frame):
            results = scan_opening_prints(["FLAT"], pct_move_min=10.0, min_dollar_volume=1000)
        assert results == []

    def test_below_dollar_volume_excluded(self):
        from src.gap_scanner import scan_opening_prints
        idx = _opening_index(n_bars=4)
        frame = _make_frame(
            idx,
            closes_by_ticker={"THIN": [10.0, 11.0, 12.0, 13.0]},  # +30% move
            volumes_by_ticker={"THIN": [5, 5, 5, 5]},              # but tiny volume
        )
        with patch("src.gap_scanner._download_with_retry", return_value=frame):
            results = scan_opening_prints(["THIN"], pct_move_min=10.0, min_dollar_volume=500_000)
        assert results == []

    def test_download_failure_returns_empty(self):
        from src.gap_scanner import scan_opening_prints
        with patch("src.gap_scanner._download_with_retry", side_effect=RuntimeError("network down")):
            assert scan_opening_prints(["AAPL"]) == []

    def test_empty_tickers_returns_empty(self):
        from src.gap_scanner import scan_opening_prints
        assert scan_opening_prints([]) == []

    def test_sorted_by_move_desc(self):
        from src.gap_scanner import scan_opening_prints
        idx = _opening_index(n_bars=3)
        frame = _make_frame(
            idx,
            closes_by_ticker={
                "SMALL": [10.0, 10.5, 11.0],   # +10%
                "BIG":   [10.0, 11.5, 13.0],   # +30%
            },
            volumes_by_ticker={"SMALL": [100_000] * 3, "BIG": [100_000] * 3},
        )
        with patch("src.gap_scanner._download_with_retry", return_value=frame):
            results = scan_opening_prints(["SMALL", "BIG"], pct_move_min=5.0, min_dollar_volume=10_000)
        assert [r["ticker"] for r in results] == ["BIG", "SMALL"]


# ══════════════════════════════════════════════════════════════════════════
# scheduler.run_premarket_gap_alert / run_opening_print_alert — no-op gates
# ══════════════════════════════════════════════════════════════════════════

class TestPremarketGapAlertNoOps:
    def test_missing_config_key_is_noop(self):
        with patch.object(scheduler, "load_config", return_value={"enabled": True, "telegram": True}), \
             patch.object(scheduler, "_is_trading_day", return_value=True), \
             patch("src.gap_scanner.scan_premarket_gaps") as scan:
            scheduler.run_premarket_gap_alert()
        scan.assert_not_called()

    def test_globally_disabled_is_noop(self):
        cfg = {"enabled": False, "gap_scanner": {"enabled": True}}
        with patch.object(scheduler, "load_config", return_value=cfg), \
             patch.object(scheduler, "_is_trading_day", return_value=True), \
             patch("src.gap_scanner.scan_premarket_gaps") as scan:
            scheduler.run_premarket_gap_alert()
        scan.assert_not_called()

    def test_gap_scanner_disabled_is_noop(self):
        cfg = {"enabled": True, "telegram": True, "gap_scanner": {"enabled": False}}
        with patch.object(scheduler, "load_config", return_value=cfg), \
             patch.object(scheduler, "_is_trading_day", return_value=True), \
             patch("src.gap_scanner.scan_premarket_gaps") as scan:
            scheduler.run_premarket_gap_alert()
        scan.assert_not_called()

    def test_outside_window_is_noop(self):
        cfg = {"enabled": True, "telegram": True, "gap_scanner": {"enabled": True}}
        with patch.object(scheduler, "load_config", return_value=cfg), \
             patch.object(scheduler, "_is_trading_day", return_value=True), \
             patch.object(scheduler, "_in_premarket_gap_window", return_value=False), \
             patch("src.gap_scanner.scan_premarket_gaps") as scan:
            scheduler.run_premarket_gap_alert()
        scan.assert_not_called()


class TestOpeningPrintAlertNoOps:
    def test_missing_config_key_is_noop(self):
        with patch.object(scheduler, "load_config", return_value={"enabled": True, "telegram": True}), \
             patch.object(scheduler, "_is_trading_day", return_value=True), \
             patch("src.gap_scanner.scan_opening_prints") as scan:
            scheduler.run_opening_print_alert()
        scan.assert_not_called()

    def test_outside_window_is_noop(self):
        cfg = {"enabled": True, "telegram": True, "gap_scanner": {"enabled": True}}
        with patch.object(scheduler, "load_config", return_value=cfg), \
             patch.object(scheduler, "_is_trading_day", return_value=True), \
             patch.object(scheduler, "_in_opening_print_window", return_value=False), \
             patch("src.gap_scanner.scan_opening_prints") as scan:
            scheduler.run_opening_print_alert()
        scan.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════
# scheduler.run_premarket_gap_alert / run_opening_print_alert — DB behavior
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="gap_scanner_")
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


def _base_cfg():
    return {
        "enabled": True,
        "telegram": True,
        "gap_scanner": {"enabled": True, "indices": ["Russell 2000"], "price_floor": 5.0,
                         "max_alerts_per_run": 8},
    }


class TestPremarketGapAlertDbBehavior:
    def test_sends_and_writes_cooldown_on_success(self, temp_db):
        from src.database import watchlist_get_alerts

        fake_results = [{"ticker": "GAPPER", "price": 11.6, "prior_close": 10.0,
                          "gap_pct": 16.0, "premarket_dollar_volume": 300_000}]

        with patch.object(scheduler, "load_config", return_value=_base_cfg()), \
             patch.object(scheduler, "_is_trading_day", return_value=True), \
             patch.object(scheduler, "_in_premarket_gap_window", return_value=True), \
             patch.object(scheduler, "init_db"), \
             patch("src.index_loader.get_index", return_value=pd.DataFrame({"ticker": ["GAPPER"]})), \
             patch("src.gap_scanner.scan_premarket_gaps", return_value=fake_results), \
             patch("src.catalyst_scanner.fetch_sec_8k_events", return_value=[]), \
             patch.object(scheduler, "TelegramNotifier") as tg:
            tg.return_value.send_message.return_value = True
            scheduler.run_premarket_gap_alert()

        alerts = watchlist_get_alerts(ticker="GAPPER", limit=10)
        assert any(a["alert_type"] == "premarket_gap_alert" for a in alerts)

    def test_failed_send_does_not_write_cooldown(self, temp_db):
        from src.database import watchlist_get_alerts

        fake_results = [{"ticker": "GAPPER", "price": 11.6, "prior_close": 10.0,
                          "gap_pct": 16.0, "premarket_dollar_volume": 300_000}]

        with patch.object(scheduler, "load_config", return_value=_base_cfg()), \
             patch.object(scheduler, "_is_trading_day", return_value=True), \
             patch.object(scheduler, "_in_premarket_gap_window", return_value=True), \
             patch.object(scheduler, "init_db"), \
             patch("src.index_loader.get_index", return_value=pd.DataFrame({"ticker": ["GAPPER"]})), \
             patch("src.gap_scanner.scan_premarket_gaps", return_value=fake_results), \
             patch("src.catalyst_scanner.fetch_sec_8k_events", return_value=[]), \
             patch.object(scheduler, "TelegramNotifier") as tg:
            tg.return_value.send_message.return_value = False
            scheduler.run_premarket_gap_alert()

        alerts = watchlist_get_alerts(ticker="GAPPER", limit=10)
        assert not any(a["alert_type"] == "premarket_gap_alert" for a in alerts), \
            "a failed Telegram send must never write the cooldown row"

    def test_cooldown_suppresses_repeat_within_24h(self, temp_db):
        from src.database import watchlist_save_alert

        watchlist_save_alert("GAPPER", "premarket_gap_alert", "prior alert", score=16.0, price=11.6)

        fake_results = [{"ticker": "GAPPER", "price": 11.6, "prior_close": 10.0,
                          "gap_pct": 16.0, "premarket_dollar_volume": 300_000}]

        with patch.object(scheduler, "load_config", return_value=_base_cfg()), \
             patch.object(scheduler, "_is_trading_day", return_value=True), \
             patch.object(scheduler, "_in_premarket_gap_window", return_value=True), \
             patch.object(scheduler, "init_db"), \
             patch("src.index_loader.get_index", return_value=pd.DataFrame({"ticker": ["GAPPER"]})), \
             patch("src.gap_scanner.scan_premarket_gaps", return_value=fake_results), \
             patch.object(scheduler, "TelegramNotifier") as tg:
            scheduler.run_premarket_gap_alert()

        tg.return_value.send_message.assert_not_called()

    def test_price_floor_filters_candidates(self, temp_db):
        from src.database import watchlist_get_alerts

        fake_results = [
            {"ticker": "PENNY", "price": 2.0, "prior_close": 1.8, "gap_pct": 11.0, "premarket_dollar_volume": 300_000},
            {"ticker": "GOODCO", "price": 11.6, "prior_close": 10.0, "gap_pct": 16.0, "premarket_dollar_volume": 300_000},
        ]

        with patch.object(scheduler, "load_config", return_value=_base_cfg()), \
             patch.object(scheduler, "_is_trading_day", return_value=True), \
             patch.object(scheduler, "_in_premarket_gap_window", return_value=True), \
             patch.object(scheduler, "init_db"), \
             patch("src.index_loader.get_index", return_value=pd.DataFrame({"ticker": ["PENNY", "GOODCO"]})), \
             patch("src.gap_scanner.scan_premarket_gaps", return_value=fake_results), \
             patch("src.catalyst_scanner.fetch_sec_8k_events", return_value=[]), \
             patch.object(scheduler, "TelegramNotifier") as tg:
            tg.return_value.send_message.return_value = True
            scheduler.run_premarket_gap_alert()

        alerts = watchlist_get_alerts(ticker="PENNY", limit=10)
        assert not any(a["alert_type"] == "premarket_gap_alert" for a in alerts), \
            "sub-price-floor candidate must be filtered out"
        alerts = watchlist_get_alerts(ticker="GOODCO", limit=10)
        assert any(a["alert_type"] == "premarket_gap_alert" for a in alerts)


class TestOpeningPrintAlertDbBehavior:
    def test_sends_and_writes_cooldown_on_success(self, temp_db):
        from src.database import watchlist_get_alerts

        fake_results = [{"ticker": "NMAX", "price": 11.11, "open_print": 9.895,
                          "move_pct": 12.3, "dollar_volume": 900_000}]

        with patch.object(scheduler, "load_config", return_value=_base_cfg()), \
             patch.object(scheduler, "_is_trading_day", return_value=True), \
             patch.object(scheduler, "_in_opening_print_window", return_value=True), \
             patch.object(scheduler, "init_db"), \
             patch("src.index_loader.get_index", return_value=pd.DataFrame({"ticker": ["NMAX"]})), \
             patch("src.gap_scanner.scan_opening_prints", return_value=fake_results), \
             patch("src.catalyst_scanner.fetch_sec_8k_events", return_value=[]), \
             patch.object(scheduler, "TelegramNotifier") as tg:
            tg.return_value.send_message.return_value = True
            scheduler.run_opening_print_alert()

        alerts = watchlist_get_alerts(ticker="NMAX", limit=10)
        assert any(a["alert_type"] == "opening_print_alert" for a in alerts)

    def test_failed_send_does_not_write_cooldown(self, temp_db):
        from src.database import watchlist_get_alerts

        fake_results = [{"ticker": "NMAX", "price": 11.11, "open_print": 9.895,
                          "move_pct": 12.3, "dollar_volume": 900_000}]

        with patch.object(scheduler, "load_config", return_value=_base_cfg()), \
             patch.object(scheduler, "_is_trading_day", return_value=True), \
             patch.object(scheduler, "_in_opening_print_window", return_value=True), \
             patch.object(scheduler, "init_db"), \
             patch("src.index_loader.get_index", return_value=pd.DataFrame({"ticker": ["NMAX"]})), \
             patch("src.gap_scanner.scan_opening_prints", return_value=fake_results), \
             patch("src.catalyst_scanner.fetch_sec_8k_events", return_value=[]), \
             patch.object(scheduler, "TelegramNotifier") as tg:
            tg.return_value.send_message.return_value = False
            scheduler.run_opening_print_alert()

        alerts = watchlist_get_alerts(ticker="NMAX", limit=10)
        assert not any(a["alert_type"] == "opening_print_alert" for a in alerts)

    def test_cooldown_suppresses_repeat_within_24h(self, temp_db):
        from src.database import watchlist_save_alert

        watchlist_save_alert("NMAX", "opening_print_alert", "prior alert", score=12.3, price=11.11)

        fake_results = [{"ticker": "NMAX", "price": 11.11, "open_print": 9.895,
                          "move_pct": 12.3, "dollar_volume": 900_000}]

        with patch.object(scheduler, "load_config", return_value=_base_cfg()), \
             patch.object(scheduler, "_is_trading_day", return_value=True), \
             patch.object(scheduler, "_in_opening_print_window", return_value=True), \
             patch.object(scheduler, "init_db"), \
             patch("src.index_loader.get_index", return_value=pd.DataFrame({"ticker": ["NMAX"]})), \
             patch("src.gap_scanner.scan_opening_prints", return_value=fake_results), \
             patch.object(scheduler, "TelegramNotifier") as tg:
            scheduler.run_opening_print_alert()

        tg.return_value.send_message.assert_not_called()

    def test_price_floor_filters_candidates(self, temp_db):
        from src.database import watchlist_get_alerts

        fake_results = [
            {"ticker": "PENNY", "price": 2.0, "open_print": 1.7, "move_pct": 17.0, "dollar_volume": 900_000},
            {"ticker": "GOODCO", "price": 11.11, "open_print": 9.895, "move_pct": 12.3, "dollar_volume": 900_000},
        ]

        with patch.object(scheduler, "load_config", return_value=_base_cfg()), \
             patch.object(scheduler, "_is_trading_day", return_value=True), \
             patch.object(scheduler, "_in_opening_print_window", return_value=True), \
             patch.object(scheduler, "init_db"), \
             patch("src.index_loader.get_index", return_value=pd.DataFrame({"ticker": ["PENNY", "GOODCO"]})), \
             patch("src.gap_scanner.scan_opening_prints", return_value=fake_results), \
             patch("src.catalyst_scanner.fetch_sec_8k_events", return_value=[]), \
             patch.object(scheduler, "TelegramNotifier") as tg:
            tg.return_value.send_message.return_value = True
            scheduler.run_opening_print_alert()

        alerts = watchlist_get_alerts(ticker="PENNY", limit=10)
        assert not any(a["alert_type"] == "opening_print_alert" for a in alerts)
        alerts = watchlist_get_alerts(ticker="GOODCO", limit=10)
        assert any(a["alert_type"] == "opening_print_alert" for a in alerts)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
