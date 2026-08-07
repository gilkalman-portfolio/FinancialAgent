"""
Tests for src/llm_universe_curator.py — the weekly LLM curation layer.

Mandate under test (defined 2026-08-07):
  - Curates a pre-filtered shortlist only — never trusts an off-list ticker
    from the LLM, even if the LLM invents one.
  - Price floor excludes penny/manipulation-prone tickers before the LLM
    ever sees them.
  - Any LLM failure (call error, malformed JSON, empty/invalid output) must
    resolve to "no change" (None / ok=False), never a partial or corrupted
    write.
  - Stale or absent curation data must not silently filter the live queue —
    current_curated_tickers() returns None (no filtering) in that case.
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"

    def _get_conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    import src.llm_universe_curator  # noqa: F401
    import src.monitoring_queue  # noqa: F401
    monkeypatch.setattr("src.database.get_connection", _get_conn)
    monkeypatch.setattr("src.llm_universe_curator.get_connection", _get_conn)
    # build_universe_digest() delegates ranking to monitoring_queue._scanner_tickers(),
    # which binds its own get_connection reference at module load — must be patched too.
    monkeypatch.setattr("src.monitoring_queue.get_connection", _get_conn)

    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE scan_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, scanned_at TEXT NOT NULL,
            price REAL, explosion_score REAL, catalyst TEXT, raw_data TEXT
        );
        CREATE TABLE llm_curated_universe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_of TEXT NOT NULL, ticker TEXT NOT NULL, action TEXT NOT NULL,
            rationale TEXT, created_at TEXT NOT NULL
        );
    """)
    conn.close()
    yield _get_conn


def _scan_row(get_conn, ticker, score, price, scanned_at, raw=None, catalyst=None):
    with get_conn() as c:
        c.execute(
            "INSERT INTO scan_results (ticker, scanned_at, price, explosion_score, catalyst, raw_data) "
            "VALUES (?,?,?,?,?,?)",
            (ticker, scanned_at, price, score, catalyst, json.dumps(raw) if raw else None),
        )


def _curated_row(get_conn, week_of, ticker, action, rationale="x", created_at=None):
    with get_conn() as c:
        c.execute(
            "INSERT INTO llm_curated_universe (week_of, ticker, action, rationale, created_at) "
            "VALUES (?,?,?,?,?)",
            (week_of, ticker, action, rationale, created_at or datetime.now().isoformat()),
        )


# ── build_universe_digest ────────────────────────────────────────────────────
#
# Ranking is delegated to monitoring_queue._scanner_tickers() (already covered
# by tests/test_monitoring_queue_liquidity_hysteresis.py and its own scan_type
# handling) — tests here mock it directly for ranking/top_n behavior, and use
# real scan_results rows only for the enrichment (price floor, raw_data
# extraction, latest-row-per-ticker) that build_universe_digest() itself owns.

class TestBuildDigest:
    def test_empty_when_scanner_has_no_candidates(self, _db):
        from src.llm_universe_curator import build_universe_digest
        with patch("src.monitoring_queue._scanner_tickers", return_value=[]):
            assert build_universe_digest() == []

    def test_ticker_with_no_matching_scan_results_row_is_skipped(self, _db):
        """_scanner_tickers() and the enrichment lookup query scan_results
        independently — a ticker present in one but not the other (a race, or
        a row pruned between the two queries) must not crash or fabricate
        a price."""
        from src.llm_universe_curator import build_universe_digest
        with patch("src.monitoring_queue._scanner_tickers",
                   return_value=[("GHOST", 90, "t")]):
            assert build_universe_digest() == []

    def test_price_floor_excludes_penny_stocks(self, _db):
        from src.llm_universe_curator import build_universe_digest
        now = datetime.now().isoformat()
        _scan_row(_db, "PENNY", 90, 2.50, now)
        _scan_row(_db, "REAL", 90, 25.0, now)
        with patch("src.monitoring_queue._scanner_tickers",
                   return_value=[("PENNY", 90, now), ("REAL", 90, now)]):
            digest = build_universe_digest()
        tickers = {d["ticker"] for d in digest}
        assert "PENNY" not in tickers
        assert "REAL" in tickers

    def test_respects_top_n_and_preserves_scanner_order(self, _db):
        """Score-descending order is _scanner_tickers()'s contract, not
        re-derived here — build_universe_digest() must not reorder it."""
        from src.llm_universe_curator import build_universe_digest
        now = datetime.now().isoformat()
        ranked = [("T1", 90, now), ("T2", 80, now), ("T3", 70, now), ("T4", 60, now)]
        for t, score, ts in ranked:
            _scan_row(_db, t, score, 20.0, ts)
        with patch("src.monitoring_queue._scanner_tickers", return_value=ranked):
            digest = build_universe_digest(top_n=2)
        assert [d["ticker"] for d in digest] == ["T1", "T2"]

    def test_extracts_raw_data_fields_from_latest_row(self, _db):
        from src.llm_universe_curator import build_universe_digest
        now = datetime.now()
        _scan_row(_db, "AAA", 60, 25.0, (now - timedelta(hours=6)).isoformat())  # stale row
        raw = {"momentum": 5.5, "short_pct": 12.0, "dcf": {"margin_of_safety_pct": 22.0}}
        _scan_row(_db, "AAA", 80, 30.0, now.isoformat(), raw=raw, catalyst="earnings 8/15")
        with patch("src.monitoring_queue._scanner_tickers",
                   return_value=[("AAA", 80, now.isoformat())]):
            digest = build_universe_digest()
        assert digest[0]["price"] == 30.0  # latest row's price, not the stale one
        assert digest[0]["momentum"] == 5.5
        assert digest[0]["short_pct"] == 12.0
        assert digest[0]["dcf_margin_of_safety_pct"] == 22.0
        assert digest[0]["catalyst"] == "earnings 8/15"

    def test_malformed_raw_data_json_is_tolerated(self, _db):
        from src.llm_universe_curator import build_universe_digest
        now = datetime.now().isoformat()
        with _db() as c:
            c.execute(
                "INSERT INTO scan_results (ticker, scanned_at, price, explosion_score, raw_data) "
                "VALUES ('AAA', ?, 30.0, 80, 'not valid json')", (now,))
        with patch("src.monitoring_queue._scanner_tickers", return_value=[("AAA", 80, now)]):
            digest = build_universe_digest()
        assert digest == [{"ticker": "AAA", "score": 80, "price": 30.0}]


# ── curate_universe ──────────────────────────────────────────────────────────

class TestCurateUniverse:
    DIGEST = [
        {"ticker": "AAA", "score": 90, "price": 30.0},
        {"ticker": "BBB", "score": 80, "price": 25.0},
    ]

    def test_empty_digest_skips_llm_entirely(self, _db):
        from src.llm_universe_curator import curate_universe
        with patch("src.llm_client.llm_complete") as m:
            assert curate_universe([], []) is None
        m.assert_not_called()

    def test_happy_path_returns_verified_selections(self, _db):
        from src.llm_universe_curator import curate_universe
        response = json.dumps({
            "selections": [
                {"ticker": "AAA", "action": "keep", "rationale": "strong momentum"},
                {"ticker": "BBB", "action": "add", "rationale": "new catalyst"},
            ],
            "removed": [{"ticker": "ZZZ", "rationale": "no longer relevant"}],
        })
        with patch("src.llm_client.llm_complete", return_value=response):
            result = curate_universe(self.DIGEST, ["ZZZ"])
        assert result is not None
        assert {s["ticker"] for s in result["selections"]} == {"AAA", "BBB"}
        assert result["removed"][0]["ticker"] == "ZZZ"

    def test_strips_markdown_fences(self, _db):
        from src.llm_universe_curator import curate_universe
        response = "```json\n" + json.dumps({
            "selections": [{"ticker": "AAA", "action": "keep", "rationale": "x"}],
            "removed": [],
        }) + "\n```"
        with patch("src.llm_client.llm_complete", return_value=response):
            result = curate_universe(self.DIGEST, [])
        assert result is not None
        assert result["selections"][0]["ticker"] == "AAA"

    def test_lowercased_ticker_from_llm_still_matches(self, _db):
        """The shortlist is uppercase; the LLM echoing 'aapl' is a normalization
        quirk, not an off-list ticker, and must not be silently dropped."""
        from src.llm_universe_curator import curate_universe
        response = json.dumps({
            "selections": [{"ticker": "aaa", "action": "keep", "rationale": "x"}],
            "removed": [],
        })
        with patch("src.llm_client.llm_complete", return_value=response):
            result = curate_universe(self.DIGEST, [])
        assert result is not None
        assert result["selections"][0]["ticker"] == "AAA"

    def test_off_list_ticker_is_dropped_not_trusted(self, _db):
        """The LLM must never be able to introduce a ticker outside the shortlist."""
        from src.llm_universe_curator import curate_universe
        response = json.dumps({
            "selections": [
                {"ticker": "AAA", "action": "keep", "rationale": "x"},
                {"ticker": "MADEUP", "action": "add", "rationale": "hallucinated"},
            ],
            "removed": [],
        })
        with patch("src.llm_client.llm_complete", return_value=response):
            result = curate_universe(self.DIGEST, [])
        tickers = {s["ticker"] for s in result["selections"]}
        assert tickers == {"AAA"}
        assert "MADEUP" not in tickers

    def test_malformed_json_returns_none(self, _db):
        from src.llm_universe_curator import curate_universe
        with patch("src.llm_client.llm_complete", return_value="not json at all"):
            assert curate_universe(self.DIGEST, []) is None

    def test_llm_exception_returns_none(self, _db):
        from src.llm_universe_curator import curate_universe
        with patch("src.llm_client.llm_complete", side_effect=RuntimeError("Groq failed")):
            assert curate_universe(self.DIGEST, []) is None

    def test_all_off_list_selections_returns_none(self, _db):
        """If every selection is invented, the whole response is a failure, not
        a curation that silently zeroes out the monitoring universe."""
        from src.llm_universe_curator import curate_universe
        response = json.dumps({
            "selections": [{"ticker": "MADEUP", "action": "add", "rationale": "x"}],
            "removed": [],
        })
        with patch("src.llm_client.llm_complete", return_value=response):
            assert curate_universe(self.DIGEST, []) is None

    def test_missing_selections_key_returns_none(self, _db):
        from src.llm_universe_curator import curate_universe
        with patch("src.llm_client.llm_complete", return_value=json.dumps({"foo": "bar"})):
            assert curate_universe(self.DIGEST, []) is None


# ── run_weekly_curation ───────────────────────────────────────────────────────

class TestRunWeeklyCuration:
    def test_happy_path_persists_and_summarizes(self, _db):
        from src import llm_universe_curator as luc
        now = datetime.now().isoformat()
        _scan_row(_db, "AAA", 90, 30.0, now)
        response = json.dumps({
            "selections": [{"ticker": "AAA", "action": "keep", "rationale": "x"}],
            "removed": [],
        })
        with patch("src.llm_client.llm_complete", return_value=response):
            result = luc.run_weekly_curation()
        assert result["ok"] is True
        assert result["curated"] == ["AAA"]
        with _db() as c:
            rows = c.execute("SELECT * FROM llm_curated_universe").fetchall()
        assert len(rows) == 1
        assert rows[0]["ticker"] == "AAA"

    def test_failure_persists_nothing(self, _db):
        from src import llm_universe_curator as luc
        now = datetime.now().isoformat()
        _scan_row(_db, "AAA", 90, 30.0, now)
        with patch("src.llm_client.llm_complete", side_effect=RuntimeError("down")):
            result = luc.run_weekly_curation()
        assert result["ok"] is False
        with _db() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM llm_curated_universe").fetchone()["n"]
        assert n == 0

    def test_previous_week_tickers_anchors_next_run(self, _db):
        from src.llm_universe_curator import _previous_week_tickers
        _curated_row(_db, "2026-07-27", "AAA", "keep")
        _curated_row(_db, "2026-07-27", "BBB", "remove")
        assert _previous_week_tickers("2026-08-03") == ["AAA"]

    def test_rerun_same_week_replaces_not_accumulates(self, _db):
        """A scheduler crash/restart retrying the same Monday's job must not
        UNION a stale attempt's rows with the retry's — current_curated_tickers()
        would otherwise resurrect a superseded selection."""
        from src import llm_universe_curator as luc
        now = datetime.now().isoformat()
        _scan_row(_db, "AAA", 90, 30.0, now)
        _scan_row(_db, "BBB", 80, 25.0, now)

        first_response = json.dumps({
            "selections": [{"ticker": "AAA", "action": "add", "rationale": "first attempt"}],
            "removed": [],
        })
        with patch("src.llm_client.llm_complete", return_value=first_response):
            luc.run_weekly_curation()

        second_response = json.dumps({
            "selections": [{"ticker": "BBB", "action": "add", "rationale": "retry, different pick"}],
            "removed": [],
        })
        with patch("src.llm_client.llm_complete", return_value=second_response):
            result = luc.run_weekly_curation()

        assert result["curated"] == ["BBB"]
        with _db() as c:
            rows = c.execute(
                "SELECT ticker FROM llm_curated_universe WHERE week_of = ?", (result["week_of"],)
            ).fetchall()
        tickers = {r["ticker"] for r in rows}
        assert tickers == {"BBB"}, f"expected only the retry's picks, got {tickers}"


# ── current_curated_tickers ───────────────────────────────────────────────────

class TestCurrentCuratedTickers:
    def test_none_when_table_empty(self, _db):
        from src.llm_universe_curator import current_curated_tickers
        assert current_curated_tickers() is None

    def test_returns_keep_and_add_excludes_remove(self, _db):
        from src.llm_universe_curator import current_curated_tickers
        week = datetime.now().date().isoformat()
        _curated_row(_db, week, "AAA", "keep")
        _curated_row(_db, week, "BBB", "add")
        _curated_row(_db, week, "CCC", "remove")
        assert current_curated_tickers() == {"AAA", "BBB"}

    def test_stale_curation_returns_none(self, _db):
        from src.llm_universe_curator import current_curated_tickers
        old_week = (datetime.now() - timedelta(days=20)).date().isoformat()
        _curated_row(_db, old_week, "AAA", "keep")
        assert current_curated_tickers() is None
