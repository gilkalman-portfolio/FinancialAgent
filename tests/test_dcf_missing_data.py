"""
Test: dcf_valuation.py no longer treats a missing debt/cash field from
yfinance as a confirmed zero.

Background (2026-08-17): `info.get("totalDebt") or 0` collapsed three
distinct cases (key absent, key present as None, key present as 0) into the
same fallback — silently pricing a leveraged company as debt-free and
inflating its margin of safety. calculate_dcf() now returns None (falls
through to the P/S valuation, same as every other insufficient-data case in
this function) when debtToEquity/totalDebt/totalCash are genuinely missing.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_dcf_missing_data.py -v
"""

from __future__ import annotations

import pytest

from src.dcf_valuation import calculate_dcf


def _base_info(**overrides) -> dict:
    """A minimal info dict that clears every earlier gate in calculate_dcf
    (FCF, shares, price) so the test actually reaches the debt/cash check."""
    info = {
        "sector": "Technology",
        "freeCashflow": 1_000_000_000.0,
        "sharesOutstanding": 100_000_000.0,
        "currentPrice": 50.0,
        "revenueGrowth": 0.10,
        "earningsGrowth": 0.10,
        "debtToEquity": 50.0,       # yfinance: D/E × 100
        "totalDebt": 2_000_000_000.0,
        "totalCash": 500_000_000.0,
        "interestExpense": 100_000_000.0,
        "effectiveTaxRate": 0.21,
        "beta": 1.1,
    }
    info.update(overrides)
    return info


class TestMissingDebtDataReturnsNone:
    def test_missing_debt_to_equity_returns_none(self):
        info = _base_info(debtToEquity=None)
        assert calculate_dcf(info, ticker="TEST") is None

    def test_missing_total_debt_returns_none(self):
        info = _base_info(totalDebt=None)
        assert calculate_dcf(info, ticker="TEST") is None

    def test_missing_total_cash_returns_none(self):
        info = _base_info(totalCash=None)
        assert calculate_dcf(info, ticker="TEST") is None

    def test_absent_key_treated_same_as_none(self):
        """.get() on a genuinely absent key also returns None — must be
        caught the same way as an explicit None value."""
        info = _base_info()
        del info["totalDebt"]
        assert calculate_dcf(info, ticker="TEST") is None


class TestRealDataStillProducesAValuation:
    def test_confirmed_zero_debt_is_not_treated_as_missing(self):
        """A company that genuinely has zero debt (key present, value 0) must
        still get a real DCF — 0 is a legitimate value, only a missing field
        should bail out."""
        info = _base_info(debtToEquity=0.0, totalDebt=0.0)
        result = calculate_dcf(info, ticker="TEST")
        assert result is not None
        assert "intrinsic_value" in result

    def test_full_data_produces_a_valuation(self):
        info = _base_info()
        result = calculate_dcf(info, ticker="TEST")
        assert result is not None
        assert "margin_of_safety" in result


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
