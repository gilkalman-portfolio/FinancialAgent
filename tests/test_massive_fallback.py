"""yf_cache falls back to Massive only when yfinance fails, and only with
fields Massive can answer faithfully."""
import pandas as pd
import pytest

from src import yf_cache, massive_client


@pytest.fixture(autouse=True)
def _clear():
    yf_cache._store.clear()
    yield
    yf_cache._store.clear()


class _Boom:
    def __init__(self, *_a, **_k):
        pass

    @property
    def info(self):
        raise RuntimeError("Too Many Requests. Rate limited.")

    def history(self, **_k):
        raise RuntimeError("Too Many Requests. Rate limited.")


class _Empty(_Boom):
    @property
    def info(self):
        return {}

    def history(self, **_k):
        return pd.DataFrame()


def test_info_falls_back_with_safe_fields_only(monkeypatch):
    monkeypatch.setattr(yf_cache.yf, "Ticker", _Boom)
    monkeypatch.setattr(massive_client, "_get", lambda p, params=None: {
        "results": {"market_cap": 4.3e9, "weighted_shares_outstanding": 5e7, "name": "Par Pacific"}})
    info = yf_cache.get_info("PARR")
    assert info["marketCap"] == 4.3e9 and info["_source"] == "massive"
    assert "currentPrice" not in info and "shortPercentOfFloat" not in info
    assert yf_cache.get_price("PARR") is None  # no stale price leaks into alerts


def test_info_not_called_when_yfinance_ok(monkeypatch):
    class _OK(_Boom):
        @property
        def info(self):
            return {"marketCap": 1}
    monkeypatch.setattr(yf_cache.yf, "Ticker", _OK)
    monkeypatch.setattr(massive_client, "_get", lambda *a, **k: pytest.fail("Massive must not be called"))
    assert yf_cache.get_info("X") == {"marketCap": 1}


def test_info_failure_everywhere_returns_empty(monkeypatch):
    monkeypatch.setattr(yf_cache.yf, "Ticker", _Boom)
    monkeypatch.setattr(massive_client, "_get", lambda *a, **k: None)
    assert yf_cache.get_info("X") == {}


def test_empty_yfinance_result_still_cached(monkeypatch):
    monkeypatch.setattr(yf_cache.yf, "Ticker", _Empty)
    calls = []
    monkeypatch.setattr(massive_client, "_get", lambda *a, **k: calls.append(1) or None)
    yf_cache.get_info("X"); yf_cache.get_info("X")
    assert len(calls) == 1


def test_history_falls_back_to_daily_bars(monkeypatch):
    monkeypatch.setattr(yf_cache.yf, "Ticker", _Boom)
    monkeypatch.setattr(massive_client, "_get", lambda p, params=None: {"results": [
        {"t": 1791244800000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100},
        {"t": 1791331200000, "o": 1.5, "h": 2.5, "l": 1, "c": 2.0, "v": 200}]})
    df = yf_cache.get_history("X", "1y")
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert list(df["Close"]) == [1.5, 2.0] and df.index.tz is not None


def test_history_no_fallback_for_intraday(monkeypatch):
    monkeypatch.setattr(yf_cache.yf, "Ticker", _Boom)
    monkeypatch.setattr(massive_client, "_get", lambda *a, **k: pytest.fail("no intraday fallback"))
    assert yf_cache.get_history("X", "5d", "15m").empty
