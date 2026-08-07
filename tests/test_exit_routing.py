"""
Tests for order_manager.submit_exit — the single guarded path for internal exits.

Context: on 2026-08-03 _check_tiered_exits called place_limit_order() directly,
bypassing the trading pause and all share arithmetic. It re-submitted the same
40% partial hundreds of times (BMY 34sh x 224, IT 14sh x 242) and sold straight
through zero into a -$2.37M short book. These tests pin the bypass shut.
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

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

    import src.order_manager  # noqa: F401
    monkeypatch.setattr("src.database.get_connection", _conn)
    monkeypatch.setattr("src.order_manager.get_connection", _conn)

    c = _conn()
    c.executescript("""
        CREATE TABLE ibkr_positions (ticker TEXT PRIMARY KEY, shares REAL,
            avg_cost REAL, unrealized_pnl REAL, market_value REAL,
            last_synced TEXT, exit_tier INTEGER DEFAULT 0);
        CREATE TABLE order_log (id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, action TEXT NOT NULL, shares INTEGER NOT NULL,
            entry_price REAL, stop_price REAL, target_price REAL,
            status TEXT NOT NULL, fill_price REAL, ibkr_order_id INTEGER,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, notes TEXT);
    """)
    c.commit()
    c.close()

    from src import order_manager
    order_manager.set_paused(False)
    yield _conn
    order_manager.set_paused(False)


def _pos(db, ticker, shares):
    with db() as c:
        c.execute("INSERT INTO ibkr_positions (ticker, shares, avg_cost, unrealized_pnl,"
                  " market_value, last_synced) VALUES (?,?,10,0,?,?)",
                  (ticker, shares, shares * 10, datetime.now().isoformat()))


def _ibkr():
    m = MagicMock()
    m.place_limit_order.return_value = 4242
    return m


class TestPauseBlocksExits:
    def test_paused_bot_does_not_sell(self, _db):
        """A halt must stop exits too. The broker-side GTC bracket still
        protects the position, so this costs nothing and stops runaway loops."""
        from src import order_manager
        _pos(_db, "BMY", 86)
        order_manager.set_paused(True)
        ib = _ibkr()

        r = order_manager.submit_exit(ib, "BMY", 34, 66.11, "T1 partial exit")

        assert r["status"] == "PAUSED"
        ib.place_limit_order.assert_not_called()
        with _db() as c:
            row = c.execute("SELECT status, shares FROM order_log").fetchone()
        assert row["status"] == "PAUSED" and row["shares"] == 0


class TestLongOnlyClamp:
    def test_cannot_sell_more_than_held(self, _db):
        from src import order_manager
        _pos(_db, "IT", 35)
        ib = _ibkr()
        r = order_manager.submit_exit(ib, "IT", 500, 153.42, "T1 partial exit")
        assert r["status"] == "SUBMITTED"
        assert r["shares"] == 35, "must clamp to the held size"
        assert ib.place_limit_order.call_args.kwargs["shares"] == 35

    def test_second_exit_cannot_cross_zero(self, _db):
        """The exact 2026-08-03 shape: fire the same partial twice."""
        from src import order_manager
        _pos(_db, "BMY", 86)
        ib = _ibkr()

        first = order_manager.submit_exit(ib, "BMY", 34, 66.11, "T1 partial exit")
        assert first["status"] == "SUBMITTED" and first["shares"] == 34

        second = order_manager.submit_exit(ib, "BMY", 34, 66.11, "T1 partial exit")
        assert second["status"] == "SUBMITTED"
        assert second["shares"] == 34, "86 held - 34 working = 52 sellable, 34 fits"

        third = order_manager.submit_exit(ib, "BMY", 34, 66.11, "T1 partial exit")
        assert third["shares"] == 18, "86 - 68 working = 18 left, not 34"

        fourth = order_manager.submit_exit(ib, "BMY", 34, 66.11, "T1 partial exit")
        assert fourth["status"] == "VETOED", "nothing left — must refuse"

        with _db() as c:
            total = c.execute(
                "SELECT COALESCE(SUM(shares),0) n FROM order_log "
                "WHERE action='SELL' AND status='SUBMITTED'").fetchone()["n"]
        assert total == 86, f"never sell more than held; sold {total} of 86"

    def test_runaway_loop_cannot_short_the_account(self, _db):
        """224 identical submissions — the BMY case. Total sold must equal held."""
        from src import order_manager
        _pos(_db, "BMY", 86)
        ib = _ibkr()
        for _ in range(224):
            order_manager.submit_exit(ib, "BMY", 34, 66.11, "T1 partial exit")
        with _db() as c:
            sold = c.execute(
                "SELECT COALESCE(SUM(shares),0) n FROM order_log "
                "WHERE action='SELL' AND status='SUBMITTED'").fetchone()["n"]
            vetoed = c.execute(
                "SELECT COUNT(*) n FROM order_log WHERE status='VETOED'").fetchone()["n"]
        assert sold == 86, f"sold {sold} against 86 held — this is the short bug"
        assert vetoed > 200, "the rest must be refused, not sent"
        assert ib.place_limit_order.call_count == 3

    def test_no_position_is_vetoed(self, _db):
        from src import order_manager
        ib = _ibkr()
        r = order_manager.submit_exit(ib, "GONE", 100, 10.0, "TIME STOP")
        assert r["status"] == "VETOED"
        ib.place_limit_order.assert_not_called()

    def test_short_position_is_vetoed_not_deepened(self, _db):
        from src import order_manager
        _pos(_db, "KSS", -13247)
        ib = _ibkr()
        r = order_manager.submit_exit(ib, "KSS", 298, 19.62, "T1 partial exit")
        assert r["status"] == "VETOED"
        ib.place_limit_order.assert_not_called()


class TestOrderLogBeforePlace:
    def test_row_is_written_before_the_broker_call(self, _db):
        """A crash mid-flight must leave a trackable row, not an invisible order."""
        from src import order_manager
        _pos(_db, "AAA", 100)

        seen = {}
        ib = MagicMock()

        def _place(**kwargs):
            with _db() as c:
                seen["rows"] = c.execute(
                    "SELECT COUNT(*) n FROM order_log WHERE status='SUBMITTED'"
                ).fetchone()["n"]
            return 99

        ib.place_limit_order.side_effect = _place
        order_manager.submit_exit(ib, "AAA", 50, 10.0, "TIME STOP")
        assert seen["rows"] == 1, "order_log row must exist before the broker call"

    def test_broker_failure_marks_error(self, _db):
        from src import order_manager
        _pos(_db, "AAA", 100)
        ib = MagicMock()
        ib.place_limit_order.side_effect = RuntimeError("no route")

        r = order_manager.submit_exit(ib, "AAA", 50, 10.0, "TIME STOP")
        assert r["status"] == "ERROR"
        with _db() as c:
            row = c.execute("SELECT status, notes FROM order_log").fetchone()
        assert row["status"] == "ERROR"
        assert "no route" in row["notes"]

    def test_order_id_is_recorded_on_success(self, _db):
        from src import order_manager
        _pos(_db, "AAA", 100)
        order_manager.submit_exit(_ibkr(), "AAA", 50, 10.0, "TIME STOP")
        with _db() as c:
            row = c.execute("SELECT ibkr_order_id, shares, notes FROM order_log").fetchone()
        assert row["ibkr_order_id"] == 4242
        assert row["shares"] == 50
        assert row["notes"] == "TIME STOP"


class TestWorkerUsesTheGuardedPath:
    def test_worker_exits_import_submit_exit(self):
        """Regression guard: if someone reintroduces a direct place_limit_order
        call in an exit path, this is the tripwire."""
        src = (Path(__file__).parent.parent / "src" / "ibkr_worker.py").read_text(encoding="utf-8")
        assert "from src.order_manager import submit_exit" in src

        # No exit function may call place_limit_order directly any more.
        for fn in ("_check_time_stops", "_check_tiered_exits", "_check_score_deterioration"):
            start = src.index(f"def {fn}(")
            nxt = src.find("\ndef ", start + 1)
            body = src[start:nxt if nxt != -1 else len(src)]
            assert "place_limit_order" not in body, \
                f"{fn} bypasses order_manager.submit_exit"
            assert "submit_exit(" in body, f"{fn} does not use the guarded path"
