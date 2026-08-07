"""
Tests for the LLM-curated vs quant-only comparison in forward_signals'
weekly digest (src/forward_signals.py: _llm_curation_comparison,
_safe_llm_curation_comparison, weekly_digest, format_digest_message).

Safety property under test: this section is purely additive and must be
invisible until llm_universe_curator has produced at least one week of
curated data — an empty/missing table, or a table lookup failure, must
never break weekly_digest() or print a meaningless empty comparison.
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    p = tmp_path / "t.db"

    def _conn():
        c = sqlite3.connect(str(p))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=5000")
        return c

    import src.forward_signals  # noqa: F401
    monkeypatch.setattr("src.database.get_connection", _conn)
    monkeypatch.setattr("src.forward_signals.get_connection", _conn)

    c = _conn()
    c.executescript("""
        CREATE TABLE forward_signals (id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT, signal_ts TEXT, signal_type TEXT, entry_price REAL,
            composite_score REAL, return_7d_pct REAL, return_14d_pct REAL,
            return_30d_pct REAL, fill_price REAL, data_quality_flag TEXT);
        CREATE TABLE llm_curated_universe (id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_of TEXT NOT NULL, ticker TEXT NOT NULL, action TEXT NOT NULL,
            rationale TEXT, created_at TEXT NOT NULL);
    """)
    c.commit()
    c.close()
    yield _conn


def _signal(db, ticker, ret, signal_type="BUY", days_ago=2):
    with db() as c:
        c.execute(
            "INSERT INTO forward_signals (ticker, signal_ts, signal_type, entry_price, return_7d_pct) "
            "VALUES (?,?,?,10,?)",
            (ticker, (datetime.now() - timedelta(days=days_ago)).isoformat(), signal_type, ret),
        )


def _curated(db, week_of, ticker, action):
    with db() as c:
        c.execute(
            "INSERT INTO llm_curated_universe (week_of, ticker, action, rationale, created_at) "
            "VALUES (?,?,?,'x',?)",
            (week_of, ticker, action, datetime.now().isoformat()),
        )


class TestNoDataOmitsSection:
    def test_empty_curated_table_returns_none(self, _db):
        from src import forward_signals as fs
        _signal(_db, "AAA", 5.0)
        assert fs._safe_llm_curation_comparison() is None

    def test_digest_message_omits_section_when_none(self, _db):
        from src import forward_signals as fs
        _signal(_db, "AAA", 5.0)
        d = fs.weekly_digest(7)
        assert d["llm_curation_comparison"] is None
        msg = fs.format_digest_message(d)
        assert "LLM-curated" not in msg

    def test_lookup_failure_never_breaks_the_digest(self, _db):
        from src import forward_signals as fs
        _signal(_db, "AAA", 5.0)
        with patch.object(fs, "_llm_curation_comparison", side_effect=RuntimeError("db locked")):
            d = fs.weekly_digest(7)
        assert d["llm_curation_comparison"] is None
        assert isinstance(fs.format_digest_message(d), str)


class TestComparisonSplitsCorrectly:
    def test_splits_curated_vs_quant_only_and_computes_stats(self, _db):
        from src import forward_signals as fs
        week = datetime.now().date().isoformat()
        _curated(_db, week, "AAA", "keep")
        _curated(_db, week, "BBB", "remove")  # excluded — not keep/add

        _signal(_db, "AAA", 10.0)   # curated, win
        _signal(_db, "AAA", -2.0)   # curated, loss
        _signal(_db, "CCC", 4.0)    # quant-only, win
        _signal(_db, "DDD", -6.0)   # quant-only, loss

        cmp = fs._safe_llm_curation_comparison()
        assert cmp is not None
        assert cmp["llm_curated"]["measured"] == 2
        assert cmp["llm_curated"]["avg_return_pct"] == pytest.approx(4.0)
        assert cmp["llm_curated"]["win_rate_pct"] == pytest.approx(50.0)
        assert cmp["quant_only"]["measured"] == 2
        assert cmp["quant_only"]["avg_return_pct"] == pytest.approx(-1.0)

    def test_sell_win_is_direction_aware(self, _db):
        from src import forward_signals as fs
        week = datetime.now().date().isoformat()
        _curated(_db, week, "AAA", "keep")
        _signal(_db, "AAA", -3.0, signal_type="SELL")  # price fell -> SELL win
        cmp = fs._safe_llm_curation_comparison()
        assert cmp["llm_curated"]["win_rate_pct"] == pytest.approx(100.0)

    def test_add_action_counts_as_curated(self, _db):
        from src import forward_signals as fs
        week = datetime.now().date().isoformat()
        _curated(_db, week, "AAA", "add")
        _signal(_db, "AAA", 1.0)
        cmp = fs._safe_llm_curation_comparison()
        assert cmp["llm_curated"]["measured"] == 1
        assert cmp["quant_only"]["measured"] == 0

    def test_uses_most_recent_week_only(self, _db):
        from src import forward_signals as fs
        old_week = (datetime.now() - timedelta(days=14)).date().isoformat()
        new_week = datetime.now().date().isoformat()
        _curated(_db, old_week, "OLD", "keep")
        _curated(_db, new_week, "NEW", "keep")
        _signal(_db, "OLD", 5.0)
        _signal(_db, "NEW", 5.0)
        cmp = fs._safe_llm_curation_comparison()
        # OLD is not in the most recent week's curated set -> falls into quant_only
        assert cmp["quant_only"]["measured"] == 1
        assert cmp["llm_curated"]["measured"] == 1

    def test_digest_message_includes_both_groups(self, _db):
        from src import forward_signals as fs
        week = datetime.now().date().isoformat()
        _curated(_db, week, "AAA", "keep")
        _signal(_db, "AAA", 8.0)
        _signal(_db, "BBB", 2.0)
        d = fs.weekly_digest(7)
        msg = fs.format_digest_message(d)
        assert "LLM-curated vs quant-only" in msg
        assert "LLM-curated:" in msg
        assert "Quant-only:" in msg
