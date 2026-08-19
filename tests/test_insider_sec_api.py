"""
Tests: InsiderTracker + sec_api_client + massive_insider_client
Run: python -m pytest tests/test_insider_sec_api.py -v
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.insider_tracker import InsiderTracker, InsiderScore
from src.sec_api_client import get_insider_transactions, get_recent_insider_buyers
import src.massive_insider_client as massive_insider_client


# ── Fixtures — flat transactions (post-parse) ─────────────────────────────────

MOCK_TRANSACTIONS = [
    {"date": "2026-03-01", "insider": "John CEO",  "role": "CEO", "type": "BUY",  "shares": 10000, "price": 20.0, "value": 200_000},
    {"date": "2026-03-05", "insider": "Jane CFO",  "role": "CFO", "type": "BUY",  "shares": 5000,  "price": 20.0, "value": 100_000},
    {"date": "2026-03-10", "insider": "Bob Dir",   "role": "Dir", "type": "BUY",  "shares": 2000,  "price": 20.0, "value": 40_000},
    {"date": "2026-03-15", "insider": "Alice Dir", "role": "Dir", "type": "SELL", "shares": 1000,  "price": 20.0, "value": 20_000},
]

MOCK_TRANSACTIONS_ONLY_SELLS = [
    {"date": "2026-03-01", "insider": "John CEO", "role": "CEO", "type": "SELL", "shares": 50000, "price": 10.0, "value": 500_000},
    {"date": "2026-03-02", "insider": "Jane CFO", "role": "CFO", "type": "SELL", "shares": 20000, "price": 10.0, "value": 200_000},
]

MOCK_TRANSACTIONS_EMPTY = []


# ── Raw API filing fixtures (verified field names from live response) ──────────

def _make_filing(ticker, code, shares, price, owner="CEO Name", is_officer=True, title="CEO"):
    """Helper: build a realistic Form 4 filing dict matching actual API structure."""
    return {
        "issuer": {"tradingSymbol": ticker, "name": f"{ticker} Corp"},
        "periodOfReport": "2026-03-01",
        "reportingOwner": {
            "name": owner,
            "relationship": {
                "isOfficer": is_officer,
                "officerTitle": title,
                "isDirector": False,
                "isTenPercentOwner": False,
            }
        },
        "nonDerivativeTable": {
            "transactions": [
                {
                    "coding": {"code": code, "formType": "4"},
                    "amounts": {"shares": shares, "pricePerShare": price, "acquiredDisposedCode": "A"},
                }
            ]
        },
        "derivativeTable": {"transactions": []},
    }


# ══════════════════════════════════════════════════════════════════════════════
# InsiderScore dataclass
# ══════════════════════════════════════════════════════════════════════════════

class TestInsiderScore:
    def test_fields_exist(self):
        s = InsiderScore("AAPL", 75.0, 3, 1, 2, True, 2, 300_000, [])
        assert s.ticker == "AAPL"
        assert s.conviction_score == 75.0
        assert s.clustered_buying is True
        assert s.large_purchases == 2

    def test_zero_score(self):
        s = InsiderScore("TEST", 0, 0, 0, 0, False, 0, 0, [])
        assert s.conviction_score == 0
        assert s.net_buying_90d == 0


# ══════════════════════════════════════════════════════════════════════════════
# InsiderTracker._build_score
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildScore:
    def test_mostly_buys(self):
        score = InsiderTracker._build_score("GME", MOCK_TRANSACTIONS)
        assert score.total_purchases_90d == 3
        assert score.total_sales_90d == 1
        assert score.net_buying_90d == 2
        assert score.clustered_buying is True
        assert score.large_purchases == 1       # only $200k > 100k (CFO=$100k is not >)
        assert score.total_value_bought == 340_000
        assert score.conviction_score > 50

    def test_only_sells(self):
        score = InsiderTracker._build_score("MSFT", MOCK_TRANSACTIONS_ONLY_SELLS)
        assert score.total_purchases_90d == 0
        assert score.total_sales_90d == 2
        assert score.conviction_score == 0.0
        assert score.clustered_buying is False
        assert score.large_purchases == 0

    def test_empty_transactions(self):
        score = InsiderTracker._build_score("EMPTY", MOCK_TRANSACTIONS_EMPTY)
        assert score.conviction_score == 0.0
        assert score.total_purchases_90d == 0
        assert score.net_buying_90d == 0

    def test_clustered_threshold(self):
        txns = [
            {"type": "BUY", "insider": "A", "value": 10_000, "shares": 100, "price": 100},
            {"type": "BUY", "insider": "B", "value": 10_000, "shares": 100, "price": 100},
            {"type": "BUY", "insider": "C", "value": 10_000, "shares": 100, "price": 100},
        ]
        assert InsiderTracker._build_score("X", txns).clustered_buying is True

    def test_clustered_below_threshold(self):
        txns = [
            {"type": "BUY", "insider": "A", "value": 10_000, "shares": 100, "price": 100},
            {"type": "BUY", "insider": "B", "value": 10_000, "shares": 100, "price": 100},
        ]
        assert InsiderTracker._build_score("X", txns).clustered_buying is False

    def test_large_purchase_threshold(self):
        txns = [
            {"type": "BUY", "insider": "A", "value": 100_000, "shares": 100, "price": 100},  # not large
            {"type": "BUY", "insider": "B", "value": 100_001, "shares": 100, "price": 100},  # large
        ]
        assert InsiderTracker._build_score("X", txns).large_purchases == 1

    def test_conviction_score_range(self):
        score = InsiderTracker._build_score("X", MOCK_TRANSACTIONS)
        assert 0 <= score.conviction_score <= 100


# ══════════════════════════════════════════════════════════════════════════════
# InsiderTracker routing
# ══════════════════════════════════════════════════════════════════════════════

class TestCalculateConvictionScoreMassive:
    """Massive/Polygon is now the top priority tier — CLAUDE.md Incident
    Archive 2026-08-19 (sec-api.io's free tier was observed hitting 429s
    under normal scan load)."""

    @patch("src.insider_tracker._MASSIVE_AVAILABLE", True)
    @patch("src.insider_tracker.InsiderTracker._score_via_massive")
    def test_uses_massive_when_available(self, mock_massive):
        mock_massive.return_value = InsiderScore("AAPL", 90.0, 5, 0, 5, True, 3, 900_000, [])
        tracker = InsiderTracker()
        result  = tracker.calculate_conviction_score("AAPL")
        mock_massive.assert_called_once_with("AAPL")
        assert result.conviction_score == 90.0

    @patch("src.massive_insider_client.get_insider_transactions", return_value=MOCK_TRANSACTIONS)
    def test_massive_path_returns_correct_score(self, mock_get):
        tracker = InsiderTracker()
        result  = tracker._score_via_massive("GME")
        mock_get.assert_called_once_with("GME", days=90)
        assert result.total_purchases_90d == 3
        assert result.clustered_buying is True

    @patch("src.insider_tracker._SEC_API_AVAILABLE", True)
    @patch("src.massive_insider_client.get_insider_transactions", side_effect=Exception("Massive down"))
    @patch("src.insider_tracker.InsiderTracker._score_via_sec_api")
    def test_falls_back_to_sec_api_on_massive_error(self, mock_sec, mock_get):
        mock_sec.return_value = InsiderScore("GME", 70.0, 3, 1, 2, True, 1, 300_000, [])
        tracker = InsiderTracker()
        result  = tracker._score_via_massive("GME")
        mock_sec.assert_called_once_with("GME")
        assert result.conviction_score == 70.0

    @patch("src.insider_tracker._SEC_API_AVAILABLE", False)
    @patch("src.massive_insider_client.get_insider_transactions", side_effect=Exception("Massive down"))
    @patch("src.insider_tracker.InsiderTracker._score_via_edgar_xml")
    def test_falls_back_to_edgar_when_both_massive_and_sec_api_fail(self, mock_edgar, mock_get):
        mock_edgar.return_value = InsiderScore("GME", 50.0, 2, 2, 0, False, 1, 200_000, [])
        tracker = InsiderTracker()
        result  = tracker._score_via_massive("GME")
        mock_edgar.assert_called_once_with("GME")
        assert result.conviction_score == 50.0


class TestCalculateConvictionScoreSecApi:
    @patch("src.insider_tracker._MASSIVE_AVAILABLE", False)
    @patch("src.insider_tracker._SEC_API_AVAILABLE", True)
    @patch("src.insider_tracker.InsiderTracker._score_via_sec_api")
    def test_uses_sec_api_when_available(self, mock_sec):
        mock_sec.return_value = InsiderScore("AAPL", 80.0, 4, 1, 3, True, 2, 500_000, [])
        tracker = InsiderTracker()
        result  = tracker.calculate_conviction_score("AAPL")
        mock_sec.assert_called_once_with("AAPL")
        assert result.conviction_score == 80.0

    @patch("src.insider_tracker._SEC_API_AVAILABLE", True)
    @patch("src.sec_api_client.get_insider_transactions", return_value=MOCK_TRANSACTIONS)
    def test_sec_api_path_returns_correct_score(self, mock_get):
        tracker = InsiderTracker()
        result  = tracker._score_via_sec_api("GME")
        mock_get.assert_called_once_with("GME", days=90)
        assert result.total_purchases_90d == 3
        assert result.clustered_buying is True

    @patch("src.insider_tracker._SEC_API_AVAILABLE", True)
    @patch("src.sec_api_client.get_insider_transactions", side_effect=Exception("API down"))
    @patch("src.insider_tracker.InsiderTracker._score_via_edgar_xml")
    def test_fallback_to_edgar_on_sec_api_error(self, mock_edgar, mock_get):
        mock_edgar.return_value = InsiderScore("GME", 50.0, 2, 2, 0, False, 1, 200_000, [])
        tracker = InsiderTracker()
        result  = tracker._score_via_sec_api("GME")
        mock_edgar.assert_called_once_with("GME")
        assert result.conviction_score == 50.0


class TestCalculateConvictionScoreEdgar:
    @patch("src.insider_tracker._MASSIVE_AVAILABLE", False)
    @patch("src.insider_tracker._SEC_API_AVAILABLE", False)
    @patch("src.insider_tracker.InsiderTracker._score_via_edgar_xml")
    def test_uses_edgar_when_no_api_key(self, mock_edgar):
        mock_edgar.return_value = InsiderScore("TSLA", 60.0, 3, 2, 1, True, 1, 300_000, [])
        tracker = InsiderTracker()
        result  = tracker.calculate_conviction_score("TSLA")
        mock_edgar.assert_called_once_with("TSLA")
        assert result.conviction_score == 60.0

    @patch("src.insider_tracker._MASSIVE_AVAILABLE", False)
    @patch("src.insider_tracker._SEC_API_AVAILABLE", False)
    @patch("src.insider_tracker.InsiderTracker._load_ticker_map", return_value={})
    def test_unknown_ticker_returns_zero(self, mock_map):
        tracker = InsiderTracker()
        result  = tracker.calculate_conviction_score("ZZZZ")
        assert result.conviction_score == 0
        assert result.total_purchases_90d == 0


# ══════════════════════════════════════════════════════════════════════════════
# sec_api_client._extract_transactions (actual field names)
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractTransactions:
    def test_buy_parsed_correctly(self):
        from src.sec_api_client import _extract_transactions
        filing = _make_filing("AAPL", "P", 1000, 50.0)
        result = _extract_transactions(filing, {"P", "S"})
        assert len(result) == 1
        assert result[0]["type"] == "BUY"
        assert result[0]["ticker"] == "AAPL"
        assert result[0]["shares"] == 1000
        assert result[0]["price"] == 50.0
        assert result[0]["value"] == 50_000.0

    def test_sell_parsed_correctly(self):
        from src.sec_api_client import _extract_transactions
        filing = _make_filing("TSLA", "S", 500, 200.0)
        result = _extract_transactions(filing, {"P", "S"})
        assert len(result) == 1
        assert result[0]["type"] == "SELL"

    def test_vesting_filtered_out(self):
        from src.sec_api_client import _extract_transactions
        filing = _make_filing("GME", "M", 5000, 0.0)  # M = vesting
        result = _extract_transactions(filing, {"P", "S"})
        assert result == []

    def test_award_filtered_out(self):
        from src.sec_api_client import _extract_transactions
        filing = _make_filing("GME", "A", 5000, 0.0)  # A = award
        result = _extract_transactions(filing, {"P", "S"})
        assert result == []

    def test_role_parsing_officer(self):
        from src.sec_api_client import _extract_transactions
        filing = _make_filing("AAPL", "P", 100, 10.0, is_officer=True, title="CFO")
        result = _extract_transactions(filing)
        assert "CFO" in result[0]["role"]

    def test_role_parsing_director(self):
        from src.sec_api_client import _extract_transactions
        filing = _make_filing("AAPL", "P", 100, 10.0, is_officer=False, title="")
        filing["reportingOwner"]["relationship"]["isDirector"] = True
        result = _extract_transactions(filing)
        assert "Director" in result[0]["role"]


# ══════════════════════════════════════════════════════════════════════════════
# sec_api_client.get_insider_transactions (mocked HTTP)
# ══════════════════════════════════════════════════════════════════════════════

class TestGetInsiderTransactions:
    def _mock_response(self, filings: list):
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = {"transactions": filings}
        mock.raise_for_status = MagicMock()
        return mock

    @patch("src.sec_api_client._API_KEY", "fake_key")
    @patch("requests.Session.post")
    def test_returns_buy_and_sell(self, mock_post):
        filings = [
            _make_filing("AAPL", "P", 1000, 50.0, owner="CEO"),
            _make_filing("AAPL", "S", 500,  52.0, owner="CFO"),
        ]
        mock_post.return_value = self._mock_response(filings)
        result = get_insider_transactions("AAPL", days=90)
        assert len(result) == 2
        assert result[0]["type"] == "BUY"
        assert result[1]["type"] == "SELL"
        assert result[0]["value"] == 50_000.0

    @patch("src.sec_api_client._API_KEY", "fake_key")
    @patch("requests.Session.post")
    def test_filters_vesting(self, mock_post):
        filings = [
            _make_filing("AAPL", "M", 5000, 0.0),
            _make_filing("AAPL", "A", 3000, 0.0),
        ]
        mock_post.return_value = self._mock_response(filings)
        result = get_insider_transactions("AAPL")
        assert result == []

    @patch("src.sec_api_client._API_KEY", "")
    def test_no_api_key_returns_empty(self):
        assert get_insider_transactions("AAPL") == []

    @patch("src.sec_api_client._API_KEY", "fake_key")
    @patch("requests.Session.post")
    def test_api_error_returns_empty(self, mock_post):
        mock_post.side_effect = Exception("timeout")
        assert get_insider_transactions("AAPL") == []

    @patch("src.sec_api_client._API_KEY", "fake_key")
    @patch("requests.Session.post")
    def test_empty_response_returns_empty(self, mock_post):
        mock_post.return_value = self._mock_response([])
        assert get_insider_transactions("AAPL") == []


# ══════════════════════════════════════════════════════════════════════════════
# sec_api_client.get_recent_insider_buyers (mocked HTTP)
# ══════════════════════════════════════════════════════════════════════════════

class TestGetRecentInsiderBuyers:
    def _mock_response(self, filings: list):
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = {"transactions": filings}
        mock.raise_for_status = MagicMock()
        return mock

    @patch("src.sec_api_client._API_KEY", "fake_key")
    @patch("requests.Session.post")
    def test_filters_below_min_value(self, mock_post):
        # value = 100 * 10 = 1000 < 50_000
        mock_post.return_value = self._mock_response([_make_filing("GME", "P", 100, 10.0)])
        result = get_recent_insider_buyers(days=1, min_value=50_000)
        assert result == []

    @patch("src.sec_api_client._API_KEY", "fake_key")
    @patch("requests.Session.post")
    def test_returns_above_min_value(self, mock_post):
        # value = 5000 * 20 = 100_000 >= 50_000
        mock_post.return_value = self._mock_response([_make_filing("GME", "P", 5000, 20.0)])
        result = get_recent_insider_buyers(days=1, min_value=50_000)
        assert len(result) == 1
        assert result[0]["ticker"] == "GME"
        assert result[0]["value"] == 100_000.0

    @patch("src.sec_api_client._API_KEY", "fake_key")
    @patch("requests.Session.post")
    def test_excludes_sells(self, mock_post):
        mock_post.return_value = self._mock_response([_make_filing("GME", "S", 5000, 20.0)])
        result = get_recent_insider_buyers(days=1, min_value=0)
        assert result == []

    @patch("src.sec_api_client._API_KEY", "")
    def test_no_api_key_returns_empty(self):
        assert get_recent_insider_buyers() == []


# ══════════════════════════════════════════════════════════════════════════════
# reportingOwner as list (bug fix: multi-owner filings)
# ══════════════════════════════════════════════════════════════════════════════

def _make_filing_multi_owner(ticker, code, shares, price):
    """Filing where reportingOwner is a list (two owners — real API edge case)."""
    return {
        "issuer": {"tradingSymbol": ticker, "name": f"{ticker} Corp"},
        "periodOfReport": "2026-03-01",
        "reportingOwner": [
            {
                "name": "Primary Owner",
                "relationship": {
                    "isOfficer": True,
                    "officerTitle": "CEO",
                    "isDirector": False,
                    "isTenPercentOwner": False,
                }
            },
            {
                "name": "Secondary Owner",
                "relationship": {
                    "isOfficer": False,
                    "officerTitle": "",
                    "isDirector": True,
                    "isTenPercentOwner": False,
                }
            },
        ],
        "nonDerivativeTable": {
            "transactions": [
                {
                    "coding": {"code": code, "formType": "4"},
                    "amounts": {"shares": shares, "pricePerShare": price, "acquiredDisposedCode": "A"},
                }
            ]
        },
        "derivativeTable": {"transactions": []},
    }


class TestReportingOwnerList:
    """Regression tests for multi-owner filings (reportingOwner as list)."""

    def test_list_owner_name_resolved(self):
        from src.sec_api_client import _extract_transactions
        filing = _make_filing_multi_owner("AAPL", "P", 1000, 50.0)
        result = _extract_transactions(filing, {"P", "S"})
        assert len(result) == 1
        # Should use [0] — "Primary Owner", not crash or return "Unknown"
        assert result[0]["insider"] == "Primary Owner"
        assert result[0]["insider"] != "Unknown"

    def test_list_owner_role_resolved(self):
        from src.sec_api_client import _extract_transactions
        filing = _make_filing_multi_owner("AAPL", "P", 1000, 50.0)
        result = _extract_transactions(filing, {"P", "S"})
        assert "CEO" in result[0]["role"]

    def test_list_owner_transaction_values_correct(self):
        from src.sec_api_client import _extract_transactions
        filing = _make_filing_multi_owner("GME", "P", 2000, 25.0)
        result = _extract_transactions(filing, {"P", "S"})
        assert result[0]["shares"] == 2000
        assert result[0]["price"] == 25.0
        assert result[0]["value"] == 50_000.0

    def test_dict_owner_still_works(self):
        """Ensure dict (single owner) path wasn't broken by the fix."""
        from src.sec_api_client import _extract_transactions
        filing = _make_filing("MSFT", "P", 500, 100.0, owner="CFO Name", title="CFO")
        result = _extract_transactions(filing, {"P", "S"})
        assert result[0]["insider"] == "CFO Name"
        assert "CFO" in result[0]["role"]


# ══════════════════════════════════════════════════════════════════════════════
# pricePerShare missing / zero (value display)
# ══════════════════════════════════════════════════════════════════════════════

class TestMissingPrice:
    """Form 4 often omits pricePerShare for certain transaction types."""

    def test_missing_price_gives_zero_value(self):
        from src.sec_api_client import _extract_transactions
        filing = {
            "issuer": {"tradingSymbol": "TEST"},
            "periodOfReport": "2026-03-01",
            "reportingOwner": {"name": "CEO", "relationship": {"isOfficer": True, "officerTitle": "CEO", "isDirector": False, "isTenPercentOwner": False}},
            "nonDerivativeTable": {
                "transactions": [{"coding": {"code": "P"}, "amounts": {"shares": 1000, "pricePerShare": None}}]
            },
        }
        result = _extract_transactions(filing, {"P"})
        assert len(result) == 1
        assert result[0]["price"] == 0.0
        assert result[0]["value"] == 0.0   # 1000 * 0 — expected, UI shows "N/A"

    def test_absent_price_field_gives_zero(self):
        from src.sec_api_client import _extract_transactions
        filing = {
            "issuer": {"tradingSymbol": "TEST"},
            "periodOfReport": "2026-03-01",
            "reportingOwner": {"name": "CEO", "relationship": {"isOfficer": True, "officerTitle": "CEO", "isDirector": False, "isTenPercentOwner": False}},
            "nonDerivativeTable": {
                "transactions": [{"coding": {"code": "P"}, "amounts": {"shares": 500}}]  # pricePerShare key absent
            },
        }
        result = _extract_transactions(filing, {"P"})
        assert result[0]["price"] == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# massive_insider_client (HTTP layer) — mocks requests.Session.get, same
# convention as tests/test_gap_scanner.py::TestMassiveHttpHelpers and
# tests/test_catalyst_scanner.py::TestFetchSec8kEvents.
# ══════════════════════════════════════════════════════════════════════════════

def _make_massive_form4(ticker, code, shares, price, owner="CEO Name",
                         is_officer=True, title="CEO", is_director=False,
                         record_type="transaction", security_type="non_derivative",
                         date="2026-08-01"):
    return {
        "tickers": [ticker],
        "owner_name": owner,
        "is_officer": is_officer,
        "officer_title": title,
        "is_director": is_director,
        "is_ten_percent_owner": False,
        "record_type": record_type,
        "security_type": security_type,
        "transaction_code": code,
        "transaction_shares": shares,
        "transaction_price_per_share": price,
        "transaction_value": shares * price,
        "transaction_date": date,
    }


class TestMassiveGetInsiderTransactions:
    def test_no_api_key_returns_empty_without_network_call(self, monkeypatch):
        monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
        with patch("requests.Session.get") as mock_get:
            result = massive_insider_client.get_insider_transactions("AAPL")
        mock_get.assert_not_called()
        assert result == []

    def test_buy_and_sell_parsed_correctly(self, monkeypatch):
        monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"results": [
            _make_massive_form4("AAPL", "P", 1000, 50.0, owner="CEO"),
            _make_massive_form4("AAPL", "S", 500, 52.0, owner="CFO"),
        ]}
        with patch("requests.Session.get", return_value=mock_resp):
            result = massive_insider_client.get_insider_transactions("AAPL", days=90)
        assert len(result) == 2
        assert result[0]["type"] == "BUY"
        assert result[1]["type"] == "SELL"
        assert result[0]["value"] == 50_000.0

    def test_holding_records_excluded(self, monkeypatch):
        """record_type='holding' is a reported position, not a trade."""
        monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"results": [
            _make_massive_form4("AAPL", "P", 1000, 50.0, record_type="holding"),
        ]}
        with patch("requests.Session.get", return_value=mock_resp):
            result = massive_insider_client.get_insider_transactions("AAPL")
        assert result == []

    def test_derivative_records_excluded(self, monkeypatch):
        """Only non_derivative matches sec_api_client's nonDerivativeTable-only scope."""
        monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"results": [
            _make_massive_form4("AAPL", "P", 1000, 50.0, security_type="derivative"),
        ]}
        with patch("requests.Session.get", return_value=mock_resp):
            result = massive_insider_client.get_insider_transactions("AAPL")
        assert result == []

    def test_vesting_code_filtered_out(self, monkeypatch):
        monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"results": [
            _make_massive_form4("GME", "M", 5000, 0.0),  # M = vesting
        ]}
        with patch("requests.Session.get", return_value=mock_resp):
            result = massive_insider_client.get_insider_transactions("GME")
        assert result == []

    def test_role_parsing(self, monkeypatch):
        monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"results": [
            _make_massive_form4("AAPL", "P", 100, 10.0, is_officer=True, title="CFO"),
        ]}
        with patch("requests.Session.get", return_value=mock_resp):
            result = massive_insider_client.get_insider_transactions("AAPL")
        assert "CFO" in result[0]["role"]

    def test_http_error_returns_empty(self, monkeypatch):
        monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
        mock_resp = MagicMock(status_code=500)
        with patch("requests.Session.get", return_value=mock_resp):
            result = massive_insider_client.get_insider_transactions("AAPL")
        assert result == []

    def test_uses_tickers_param_not_singular_ticker(self, monkeypatch):
        """The endpoint's own docs say `ticker` (singular) — verified live
        2026-08-19 that this is wrong and silently returns unfiltered
        results. Must use `tickers` (plural)."""
        monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"results": []}
        with patch("requests.Session.get", return_value=mock_resp) as mock_get:
            massive_insider_client.get_insider_transactions("AAPL")
        called_params = mock_get.call_args.kwargs["params"]
        assert called_params.get("tickers") == "AAPL"
        assert "ticker" not in called_params


class TestMassiveGetRecentInsiderBuyers:
    def test_no_api_key_returns_empty(self, monkeypatch):
        monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
        assert massive_insider_client.get_recent_insider_buyers() == []

    def test_filters_below_min_value(self, monkeypatch):
        monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"results": [_make_massive_form4("GME", "P", 100, 10.0)]}
        with patch("requests.Session.get", return_value=mock_resp):
            result = massive_insider_client.get_recent_insider_buyers(days=1, min_value=50_000)
        assert result == []

    def test_returns_above_min_value(self, monkeypatch):
        monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"results": [_make_massive_form4("GME", "P", 5000, 20.0)]}
        with patch("requests.Session.get", return_value=mock_resp):
            result = massive_insider_client.get_recent_insider_buyers(days=1, min_value=50_000)
        assert len(result) == 1
        assert result[0]["ticker"] == "GME"
        assert result[0]["value"] == 100_000.0

    def test_excludes_sells(self, monkeypatch):
        monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"results": [_make_massive_form4("GME", "S", 5000, 20.0)]}
        with patch("requests.Session.get", return_value=mock_resp):
            result = massive_insider_client.get_recent_insider_buyers(days=1, min_value=0)
        assert result == []

    def test_sorted_descending_by_value(self, monkeypatch):
        monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"results": [
            _make_massive_form4("SMALL", "P", 100, 10.0),   # $1,000
            _make_massive_form4("BIG", "P", 10000, 10.0),    # $100,000
        ]}
        with patch("requests.Session.get", return_value=mock_resp):
            result = massive_insider_client.get_recent_insider_buyers(days=1, min_value=0)
        assert [r["ticker"] for r in result] == ["BIG", "SMALL"]
