import pandas as pd
import pytest

from src import daily_download, massive_client


def _wide(tickers, n=3):
    idx = pd.date_range("2026-10-01", periods=n)
    cols = {f: pd.DataFrame({t: [1.0] * n for t in tickers}, index=idx)
            for f in ["Open", "High", "Low", "Close", "Volume"]}
    return pd.concat(cols, axis=1)


def test_massive_serves_all_no_yfinance(monkeypatch):
    monkeypatch.setattr(massive_client, "download_daily", lambda t, p, **k: _wide(t))
    monkeypatch.setattr(daily_download.yf, "download", lambda *a, **k: pytest.fail("yf must not be called"))
    out = daily_download.download_daily(["A", "B"], "1y")
    assert set(out["Close"].columns) == {"A", "B"}


def test_missing_tickers_fall_back_to_yfinance(monkeypatch):
    monkeypatch.setattr(massive_client, "download_daily", lambda t, p, **k: _wide(["A"]))
    seen = {}
    def fake_yf(tickers, **k):
        seen["t"] = tickers
        return _wide(tickers)
    monkeypatch.setattr(daily_download.yf, "download", fake_yf)
    out = daily_download.download_daily(["A", "B"], "1y")
    assert seen["t"] == ["B"] and set(out["Close"].columns) == {"A", "B"}


def test_no_massive_key_uses_yfinance_for_all(monkeypatch):
    monkeypatch.setattr(massive_client, "download_daily", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(daily_download.yf, "download", lambda t, **k: _wide(t))
    out = daily_download.download_daily(["A", "B"], "1y")
    assert set(out["Close"].columns) == {"A", "B"}


def test_single_ticker_yfinance_flat_columns_become_multiindex(monkeypatch):
    monkeypatch.setattr(massive_client, "download_daily", lambda *a, **k: pd.DataFrame())
    flat = _wide(["A"])
    flat.columns = flat.columns.get_level_values(0)
    monkeypatch.setattr(daily_download.yf, "download", lambda t, **k: flat)
    out = daily_download.download_daily(["A"], "1y")
    assert isinstance(out.columns, pd.MultiIndex) and "A" in out["Close"].columns


def test_both_fail_returns_empty(monkeypatch):
    monkeypatch.setattr(massive_client, "download_daily", lambda *a, **k: pd.DataFrame())
    def boom(*a, **k): raise RuntimeError("429")
    monkeypatch.setattr(daily_download.yf, "download", boom)
    assert daily_download.download_daily(["A"], "1y").empty
