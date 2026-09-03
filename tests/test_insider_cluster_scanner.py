"""
Tests for src/insider_cluster_scanner.py — the free, market-wide Form 4
cluster-buying detector built to forward-validate the QuantConnect insider-
cluster-buying research lead (CLAUDE.md / project_quantconnect_cross_check
memory) without paying for QC's Quiver Insider Trading dataset.

Covers: the SEC daily-index fixed-width-with-internal-spaces line parser,
the per-filing XML parser (ticker resolution independent of which CIK the
row was indexed under), the universe filter, the raw purchase-event log's
idempotency, the trailing-window cluster query, WATCH-row dedup, and the
full orchestration (cluster + universe filter + dedup all combined).

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_insider_cluster_scanner.py -v
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="insider_cluster_")
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


# Real sample lines captured live from
# https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260828.idx
# (2026-08-30) — includes the "1-A POS" two-word form type edge case that a
# naive single-whitespace split would misparse.
_REAL_INDEX_SAMPLE = "\n".join([
    "Description:           Daily Index of EDGAR Dissemination Feed by Form Type",
    "Last Data Received:    Aug 28, 2026",
    "Comments:              webmaster@sec.gov",
    "Anonymous FTP:         ftp://ftp.sec.gov/edgar/",
    " ",
    "Form Type   Company Name                                                  CIK",
    "      Date Filed  File Name",
    "-" * 130,
    "1-A POS          Modern Mining Technology Corp.                                1898722     20260828    edgar/data/1898722/0001213900-26-094936.txt",
    "4                10x Genomics, Inc.                                            1770787     20260828    edgar/data/1770787/0001610717-26-000393.txt",
    "4                ABERNETHY JAMES S                                             1244172     20260828    edgar/data/1244172/0001654954-26-007957.txt",
    "8-K              Some Company Inc.                                            9999999     20260828    edgar/data/9999999/0001234567-26-000001.txt",
])

_REAL_FORM4_SUBMISSION = """<SEC-DOCUMENT>0001610717-26-000393.txt
<SEC-HEADER>ignored</SEC-HEADER>
<DOCUMENT>
<TYPE>4
<XML>
<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <issuer>
        <issuerCik>0001770787</issuerCik>
        <issuerName>10x Genomics, Inc.</issuerName>
        <issuerTradingSymbol>TXG</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0001786720</rptOwnerCik>
            <rptOwnerName>Saxonov Serge</rptOwnerName>
        </reportingOwnerId>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <transactionDate><value>2026-08-26</value></transactionDate>
            <transactionCoding>
                <transactionCode>P</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>1000</value></transactionShares>
                <transactionPricePerShare><value>63.02</value></transactionPricePerShare>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>
</XML>
</DOCUMENT>
</SEC-DOCUMENT>
"""

_SALE_ONLY_SUBMISSION = _REAL_FORM4_SUBMISSION.replace(
    "<transactionCode>P</transactionCode>", "<transactionCode>S</transactionCode>"
)


def _mock_response(text: str, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    return resp


# ── daily index parsing ──────────────────────────────────────────────────────

class TestFetchDailyForm4Filings:
    def test_parses_real_sample_filters_to_form4_only(self):
        from src.insider_cluster_scanner import fetch_daily_form4_filings

        with patch("requests.Session.get", return_value=_mock_response(_REAL_INDEX_SAMPLE)):
            results = fetch_daily_form4_filings(date(2026, 8, 28))

        assert len(results) == 2
        tickers_or_names = {r["company"] for r in results}
        assert "10x Genomics, Inc." in tickers_or_names
        assert "ABERNETHY JAMES S" in tickers_or_names
        # "1-A POS" (two-word form type) and "8-K" rows must be excluded
        assert not any("Modern Mining" in r["company"] for r in results)
        assert not any("Some Company" in r["company"] for r in results)

    def test_form4_row_file_path_correct(self):
        from src.insider_cluster_scanner import fetch_daily_form4_filings

        with patch("requests.Session.get", return_value=_mock_response(_REAL_INDEX_SAMPLE)):
            results = fetch_daily_form4_filings(date(2026, 8, 28))

        txg_row = next(r for r in results if r["company"] == "10x Genomics, Inc.")
        assert txg_row["file"] == "edgar/data/1770787/0001610717-26-000393.txt"
        assert txg_row["cik"] == "1770787"

    def test_http_failure_returns_empty_list(self):
        from src.insider_cluster_scanner import fetch_daily_form4_filings

        with patch("requests.Session.get", return_value=_mock_response("", status=404)):
            results = fetch_daily_form4_filings(date(2026, 1, 1))
        assert results == []

    def test_network_exception_returns_empty_list_not_raises(self):
        from src.insider_cluster_scanner import fetch_daily_form4_filings

        with patch("requests.Session.get", side_effect=ConnectionError("boom")):
            results = fetch_daily_form4_filings(date(2026, 1, 1))
        assert results == []

    def test_missing_header_separator_returns_empty_list(self):
        from src.insider_cluster_scanner import fetch_daily_form4_filings

        with patch("requests.Session.get", return_value=_mock_response("garbage\nno dashes here")):
            results = fetch_daily_form4_filings(date(2026, 1, 1))
        assert results == []


# ── per-filing XML parsing ───────────────────────────────────────────────────

class TestParseForm4Filing:
    def test_requests_the_correct_url_no_doubled_edgar_segment(self):
        """Regression test for a real bug found live 2026-09-03: file_path
        from the daily index already starts with "edgar/...", so the base
        URL must be https://www.sec.gov/Archives/ (NOT .../Archives/edgar/)
        or every request 404s on a doubled "/edgar/edgar/" path. Every other
        test in this class mocks requests.Session.get by VALUE alone and
        would pass even with the doubled-segment bug present — this is the
        one that actually inspects what URL was requested."""
        from src.insider_cluster_scanner import _parse_form4_filing

        mock_get = MagicMock(return_value=_mock_response(_REAL_FORM4_SUBMISSION))
        with patch("requests.Session.get", mock_get):
            _parse_form4_filing("edgar/data/1770787/0001610717-26-000393.txt")

        called_url = mock_get.call_args[0][0]
        assert called_url == "https://www.sec.gov/Archives/edgar/data/1770787/0001610717-26-000393.txt"
        assert "/edgar/edgar/" not in called_url

    def test_extracts_ticker_and_purchase(self):
        from src.insider_cluster_scanner import _parse_form4_filing

        with patch("requests.Session.get", return_value=_mock_response(_REAL_FORM4_SUBMISSION)):
            result = _parse_form4_filing("edgar/data/1770787/0001610717-26-000393.txt")

        assert result is not None
        assert result["ticker"] == "TXG"
        assert len(result["purchases"]) == 1
        p = result["purchases"][0]
        assert p["insider"] == "Saxonov Serge"
        assert p["shares"] == 1000.0
        assert p["price"] == 63.02
        assert p["date"] == "2026-08-26"

    def test_ticker_resolved_correctly_even_though_indexed_under_a_different_cik(self):
        """The daily index row for this same filing could have been listed
        under the insider's OWN cik (ABERNETHY JAMES S, cik 1244172 in the
        sample above) rather than the issuer's (1770787) — the parser must
        not depend on which CIK the row came from; issuerTradingSymbol in
        the filing's own XML is always authoritative."""
        from src.insider_cluster_scanner import _parse_form4_filing

        with patch("requests.Session.get", return_value=_mock_response(_REAL_FORM4_SUBMISSION)):
            result = _parse_form4_filing("edgar/data/1244172/0001654954-26-007957.txt")

        assert result["ticker"] == "TXG"  # from the XML, not the URL/CIK

    def test_sale_only_filing_returns_none(self):
        from src.insider_cluster_scanner import _parse_form4_filing

        with patch("requests.Session.get", return_value=_mock_response(_SALE_ONLY_SUBMISSION)):
            result = _parse_form4_filing("edgar/data/1770787/0001610717-26-000393.txt")
        assert result is None

    def test_no_xml_block_returns_none(self):
        from src.insider_cluster_scanner import _parse_form4_filing

        with patch("requests.Session.get", return_value=_mock_response("not a real filing")):
            result = _parse_form4_filing("edgar/data/x/y.txt")
        assert result is None

    def test_http_error_returns_none_not_raises(self):
        from src.insider_cluster_scanner import _parse_form4_filing

        with patch("requests.Session.get", side_effect=TimeoutError("slow")):
            result = _parse_form4_filing("edgar/data/x/y.txt")
        assert result is None

    def test_missing_issuer_ticker_returns_none(self):
        from src.insider_cluster_scanner import _parse_form4_filing

        broken = _REAL_FORM4_SUBMISSION.replace(
            "<issuerTradingSymbol>TXG</issuerTradingSymbol>", "<issuerTradingSymbol></issuerTradingSymbol>"
        )
        with patch("requests.Session.get", return_value=_mock_response(broken)):
            result = _parse_form4_filing("edgar/data/1770787/x.txt")
        assert result is None


# ── universe filter ───────────────────────────────────────────────────────────

class TestUniverseFilter:
    def _info(self, price=5.0, market_cap=500_000_000, avg_volume=1_000_000, forward_pe=None):
        return {
            "currentPrice": price,
            "marketCap": market_cap,
            "averageVolume": avg_volume,
            "forwardPE": forward_pe,
        }

    def test_passes_when_all_conditions_met(self):
        from src.insider_cluster_scanner import _passes_universe_filter

        with patch("src.insider_cluster_scanner._yf_info", return_value=self._info()):
            assert _passes_universe_filter("SMLL", 2_000_000_000, 1.0, 300_000) is True

    def test_fails_above_market_cap_ceiling(self):
        from src.insider_cluster_scanner import _passes_universe_filter

        with patch("src.insider_cluster_scanner._yf_info", return_value=self._info(market_cap=5_000_000_000)):
            assert _passes_universe_filter("BIG", 2_000_000_000, 1.0, 300_000) is False

    def test_fails_below_min_price(self):
        from src.insider_cluster_scanner import _passes_universe_filter

        with patch("src.insider_cluster_scanner._yf_info", return_value=self._info(price=0.50)):
            assert _passes_universe_filter("PENNY", 2_000_000_000, 1.0, 300_000) is False

    def test_fails_below_min_dollar_volume(self):
        from src.insider_cluster_scanner import _passes_universe_filter

        with patch("src.insider_cluster_scanner._yf_info", return_value=self._info(avg_volume=1000)):
            assert _passes_universe_filter("THIN", 2_000_000_000, 1.0, 300_000) is False

    def test_fails_with_positive_forward_pe_has_analyst_coverage(self):
        from src.insider_cluster_scanner import _passes_universe_filter

        with patch("src.insider_cluster_scanner._yf_info", return_value=self._info(forward_pe=15.0)):
            assert _passes_universe_filter("COVERED", 2_000_000_000, 1.0, 300_000) is False

    def test_negative_forward_pe_still_passes_no_coverage_proxy(self):
        from src.insider_cluster_scanner import _passes_universe_filter

        with patch("src.insider_cluster_scanner._yf_info", return_value=self._info(forward_pe=-5.0)):
            assert _passes_universe_filter("UNPROF", 2_000_000_000, 1.0, 300_000) is True

    def test_lookup_exception_fails_closed(self):
        from src.insider_cluster_scanner import _passes_universe_filter

        with patch("src.insider_cluster_scanner._yf_info", side_effect=Exception("yfinance down")):
            assert _passes_universe_filter("X", 2_000_000_000, 1.0, 300_000) is False


# ── raw purchase-event log ───────────────────────────────────────────────────

class TestRecordPurchaseEvents:
    def test_inserts_and_dedupes_on_rerun(self, temp_db):
        from src.insider_cluster_scanner import _record_purchase_events, _distinct_insiders_in_window

        purchases = [
            {"insider": "Alice A", "shares": 100.0, "price": 5.0, "date": date.today().isoformat()},
            {"insider": "Bob B", "shares": 200.0, "price": 5.1, "date": date.today().isoformat()},
        ]
        n1 = _record_purchase_events("ABCD", purchases)
        assert n1 == 2

        n2 = _record_purchase_events("ABCD", purchases)  # exact re-run
        assert n2 == 0  # UNIQUE constraint makes this idempotent

        distinct = _distinct_insiders_in_window("ABCD", 72)
        assert set(distinct) == {"Alice A", "Bob B"}

    def test_window_excludes_stale_purchases(self, temp_db):
        from src.insider_cluster_scanner import _record_purchase_events, _distinct_insiders_in_window

        stale_date = (datetime.now() - timedelta(days=30)).date().isoformat()
        _record_purchase_events("OLD1", [
            {"insider": "Old Insider", "shares": 10.0, "price": 1.0, "date": stale_date},
        ])
        distinct = _distinct_insiders_in_window("OLD1", 72)
        assert distinct == []

    def test_same_insider_multiple_filings_counts_once(self, temp_db):
        """Multiple filings from the SAME insider must NOT count as a
        cluster — mirrors the QC POC's MIN_DISTINCT_INSIDERS logic."""
        from src.insider_cluster_scanner import _record_purchase_events, _distinct_insiders_in_window

        today = date.today().isoformat()
        _record_purchase_events("REPEAT", [
            {"insider": "Same Person", "shares": 10.0, "price": 1.0, "date": today},
        ])
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        _record_purchase_events("REPEAT", [
            {"insider": "Same Person", "shares": 20.0, "price": 1.1, "date": yesterday},
        ])
        distinct = _distinct_insiders_in_window("REPEAT", 72)
        assert distinct == ["Same Person"]


# ── WATCH dedup ───────────────────────────────────────────────────────────────

class TestClusterWatchDedup:
    def test_no_prior_watch_returns_none(self, temp_db):
        from src.insider_cluster_scanner import _last_cluster_watch_recorded_at
        assert _last_cluster_watch_recorded_at("NEVERSEEN") is None

    def test_finds_prior_insider_cluster_watch_not_other_sources(self, temp_db):
        from src.forward_signals import SignalRecord, record_signal
        from src.insider_cluster_scanner import _last_cluster_watch_recorded_at, WATCH_SOURCE_TAG

        # A different WATCH source (news-catalyst) for the same ticker must not match.
        record_signal(SignalRecord(
            ticker="DUAL", signal_type="WATCH", entry_price=5.0,
            catalyst_summary="[momentum] some unrelated news catalyst",
        ))
        assert _last_cluster_watch_recorded_at("DUAL") is None

        record_signal(SignalRecord(
            ticker="DUAL", signal_type="WATCH", entry_price=5.0,
            catalyst_summary=f"{WATCH_SOURCE_TAG} 2 distinct insiders in 72h: ['A', 'B']",
        ))
        assert _last_cluster_watch_recorded_at("DUAL") is not None


# ── full orchestration ───────────────────────────────────────────────────────

class TestScanInsiderClusters:
    def _patch_filings(self, ticker_purchases: dict):
        """ticker_purchases: {ticker: [purchase dicts]} — patches
        fetch_daily_form4_filings to return one fake row per ticker and
        _parse_form4_filing to resolve each row to its purchases."""
        filings = [{"cik": "1", "company": t, "file": f"edgar/data/1/{t}.txt"} for t in ticker_purchases]

        def fake_parse(file_path):
            ticker = file_path.split("/")[-1].replace(".txt", "")
            purchases = ticker_purchases.get(ticker)
            if not purchases:
                return None
            return {"ticker": ticker, "purchases": purchases}

        return filings, fake_parse

    def test_two_distinct_insiders_passing_universe_records_watch(self, temp_db):
        from src.insider_cluster_scanner import scan_insider_clusters

        today = date.today().isoformat()
        purchases = {
            "SMLL": [
                {"insider": "Alice A", "shares": 100.0, "price": 5.0, "date": today},
                {"insider": "Bob B", "shares": 200.0, "price": 5.1, "date": today},
            ]
        }
        filings, fake_parse = self._patch_filings(purchases)
        good_info = {"currentPrice": 5.0, "marketCap": 500_000_000, "averageVolume": 1_000_000, "forwardPE": None}

        with patch("src.insider_cluster_scanner.fetch_daily_form4_filings", return_value=filings), \
             patch("src.insider_cluster_scanner._parse_form4_filing", side_effect=fake_parse), \
             patch("src.insider_cluster_scanner._yf_info", return_value=good_info):
            stats = scan_insider_clusters(day=date.today())

        assert stats["cluster_candidates"] == 1
        assert stats["watch_recorded"] == 1

        from src.database import get_connection
        with get_connection() as conn:
            row = conn.execute(
                "SELECT catalyst_summary FROM forward_signals WHERE ticker='SMLL' AND signal_type='WATCH'"
            ).fetchone()
        assert row is not None
        assert "insider_cluster" in row[0]

    def test_single_insider_does_not_record_watch(self, temp_db):
        from src.insider_cluster_scanner import scan_insider_clusters

        today = date.today().isoformat()
        purchases = {"SOLO": [{"insider": "Only One", "shares": 100.0, "price": 5.0, "date": today}]}
        filings, fake_parse = self._patch_filings(purchases)

        with patch("src.insider_cluster_scanner.fetch_daily_form4_filings", return_value=filings), \
             patch("src.insider_cluster_scanner._parse_form4_filing", side_effect=fake_parse):
            stats = scan_insider_clusters(day=date.today())

        assert stats["cluster_candidates"] == 0
        assert stats["watch_recorded"] == 0

    def test_cluster_blocked_by_universe_filter_large_cap(self, temp_db):
        from src.insider_cluster_scanner import scan_insider_clusters

        today = date.today().isoformat()
        purchases = {
            "MEGA": [
                {"insider": "Alice A", "shares": 100.0, "price": 150.0, "date": today},
                {"insider": "Bob B", "shares": 200.0, "price": 151.0, "date": today},
            ]
        }
        filings, fake_parse = self._patch_filings(purchases)
        mega_info = {"currentPrice": 150.0, "marketCap": 500_000_000_000, "averageVolume": 5_000_000, "forwardPE": 20.0}

        with patch("src.insider_cluster_scanner.fetch_daily_form4_filings", return_value=filings), \
             patch("src.insider_cluster_scanner._parse_form4_filing", side_effect=fake_parse), \
             patch("src.insider_cluster_scanner._yf_info", return_value=mega_info):
            stats = scan_insider_clusters(day=date.today())

        assert stats["cluster_candidates"] == 1  # cluster itself is real
        assert stats["watch_recorded"] == 0       # but blocked by market cap

    def test_second_run_within_window_dedupes(self, temp_db):
        from src.insider_cluster_scanner import scan_insider_clusters

        today = date.today().isoformat()
        purchases = {
            "DUPE": [
                {"insider": "Alice A", "shares": 100.0, "price": 5.0, "date": today},
                {"insider": "Bob B", "shares": 200.0, "price": 5.1, "date": today},
            ]
        }
        filings, fake_parse = self._patch_filings(purchases)
        good_info = {"currentPrice": 5.0, "marketCap": 500_000_000, "averageVolume": 1_000_000, "forwardPE": None}

        with patch("src.insider_cluster_scanner.fetch_daily_form4_filings", return_value=filings), \
             patch("src.insider_cluster_scanner._parse_form4_filing", side_effect=fake_parse), \
             patch("src.insider_cluster_scanner._yf_info", return_value=good_info):
            stats1 = scan_insider_clusters(day=date.today())
            stats2 = scan_insider_clusters(day=date.today())

        assert stats1["watch_recorded"] == 1
        assert stats2["watch_recorded"] == 0
        assert stats2["watch_deduped"] == 1

    def test_no_filings_returns_zeroed_stats(self, temp_db):
        from src.insider_cluster_scanner import scan_insider_clusters

        with patch("src.insider_cluster_scanner.fetch_daily_form4_filings", return_value=[]):
            stats = scan_insider_clusters(day=date.today())

        assert stats["filings_checked"] == 0
        assert stats["watch_recorded"] == 0
