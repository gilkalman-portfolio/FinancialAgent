import pandas as pd

from src import performance_report as pr


def _nlv():
    idx = pd.date_range("2026-08-05", periods=15)
    return pd.Series([100000 + i * 100 for i in range(15)], index=idx, dtype=float)


def test_report_math(monkeypatch):
    monkeypatch.setattr(pr, "_positions_summary", lambda: (2, 20000.0, 150.0))
    monkeypatch.setattr(pr, "_bench_return", lambda t, a, b: 1.0)
    d = pr.build_report(nlv=_nlv(), bench={"SPY": 1.0, "IWM": -2.0})
    assert round(d["ret_total"], 2) == 1.40 and d["days"] == 14
    assert d["max_drawdown"] == 0.0 and d["positions"] == 2
    assert round(d["invested_pct"], 1) == round(20000 / 101400 * 100, 1)
    msg = pr.format_report(d)
    assert "Excess vs SPY: +0.40pp" in msg and "IWM -2.00%" in msg


def test_too_little_history_returns_none():
    assert pr.build_report(nlv=pd.Series([1.0], index=[pd.Timestamp("2026-08-05")])) is None


def test_missing_benchmark_is_na_not_zero(monkeypatch):
    monkeypatch.setattr(pr, "_positions_summary", lambda: (0, 0.0, 0.0))
    monkeypatch.setattr(pr, "_bench_return", lambda t, a, b: None)
    d = pr.build_report(nlv=_nlv(), bench={"SPY": None, "IWM": None})
    msg = pr.format_report(d)
    assert "SPY n/a" in msg and "Excess" not in msg
