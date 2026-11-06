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
            assert "make_room=_exit_room(conn)" in body, \
                f"{fn} does not let submit_exit shrink orphaned legs too"


# ── YOU, 2026-11-04: partial exit stacked on a resting bracket ──────────────
class _FakeBroker:
    """Minimal broker: resting orders, confirmed in-place resize, plain LMT exits."""

    def __init__(self, held_ticker="YOU", qty=278, confirm=True):
        self.confirm = confirm
        self.orders = [
            {"order_id": 85954, "perm_id": 1, "ticker": held_ticker, "action": "SELL",
             "qty": qty, "order_type": "STP", "status": "Submitted", "lmt_price": None,
             "aux_price": 42.0, "parent_id": 85953, "oca_group": "", "tif": "GTC"},
            {"order_id": 85955, "perm_id": 2, "ticker": held_ticker, "action": "SELL",
             "qty": qty, "order_type": "LMT", "status": "Submitted", "lmt_price": 50.04,
             "aux_price": None, "parent_id": 85953, "oca_group": "", "tif": "GTC"},
        ]
        self.calls: list[tuple] = []
        self._next = 90000

    def get_open_orders(self):
        return [dict(o) for o in self.orders]

    def resize_order(self, order_id, new_qty):
        self.calls.append(("resize", order_id, new_qty))
        if not self.confirm:
            return False
        for o in list(self.orders):
            if o["order_id"] == order_id:
                if new_qty <= 0:
                    self.orders.remove(o)
                else:
                    o["qty"] = new_qty
        return True

    def place_limit_order(self, ticker, action, shares, limit_price):
        self.calls.append(("place", ticker, shares))
        self._next += 1
        self.orders.append({"order_id": self._next, "perm_id": None, "ticker": ticker,
                            "action": action, "qty": shares, "order_type": "LMT",
                            "status": "Submitted", "lmt_price": limit_price,
                            "aux_price": None, "parent_id": 0, "oca_group": "",
                            "tif": "DAY"})
        return self._next

    def worst_case_sold(self, ticker):
        """Shares sold if every resting SELL fills (an OCA pair only once)."""
        from src import resting_sells
        claims = resting_sells.build_claims(
            [(self, o) for o in self.orders if o["ticker"] == ticker])
        return sum(c.qty for c in claims)


class TestYOUBracketStacking:
    """278 held, bracket target LMT 278 @ 50.04 + STP 278 resting, T1 sells 111.
    Before the fix both the T1 and the target filled at the 09:30 print of
    $51.97: 389 sold against 278, short 111."""

    def test_old_state_was_a_389_share_claim(self, _db):
        b = _FakeBroker()
        b.place_limit_order("YOU", "SELL", 111, 48.74)   # what 11-04 did
        assert b.worst_case_sold("YOU") == 389

    def test_t1_shrinks_bracket_legs_before_selling(self, _db):
        from src import order_manager
        _pos(_db, "YOU", 278)
        b = _FakeBroker()

        r = order_manager.submit_exit(b, "YOU", 111, 48.74, "T1 partial exit (+7.3%)")

        assert r["status"] == "SUBMITTED" and r["shares"] == 111
        # Both legs resized to 167 BEFORE the exit was placed.
        assert b.calls[:2] == [("resize", 85954, 167), ("resize", 85955, 167)]
        assert b.calls[2] == ("place", "YOU", 111)
        assert {o["qty"] for o in b.orders if o["order_id"] in (85954, 85955)} == {167}
        assert b.worst_case_sold("YOU") == 278, "everything filling must not exceed held"

    def test_unconfirmed_leg_resize_blocks_the_t1(self, _db):
        from src import order_manager
        _pos(_db, "YOU", 278)
        b = _FakeBroker(confirm=False)

        r = order_manager.submit_exit(b, "YOU", 111, 48.74, "T1 partial exit")

        assert r["status"] == "VETOED" and r.get("retry") is True
        assert not any(c[0] == "place" for c in b.calls), "fail closed, never stack"
        assert b.worst_case_sold("YOU") == 278
        with _db() as c:
            row = c.execute("SELECT status, shares, notes FROM order_log").fetchone()
        assert row["status"] == "VETOED" and row["shares"] == 0
        assert "could not confirm" in row["notes"]

    def test_partially_confirmed_resize_still_blocks(self, _db):
        """STP shrank but the target leg didn't: the pair still claims 278."""
        from src import order_manager
        _pos(_db, "YOU", 278)
        b = _FakeBroker()
        real = b.resize_order
        b.resize_order = lambda oid, q: real(oid, q) if oid == 85954 else False

        r = order_manager.submit_exit(b, "YOU", 111, 48.74, "T1 partial exit")

        assert r["status"] == "VETOED"
        assert not any(c[0] == "place" for c in b.calls)
        assert b.worst_case_sold("YOU") <= 278

    def test_open_order_read_failure_blocks(self, _db):
        from src import order_manager
        _pos(_db, "YOU", 278)
        b = _FakeBroker()
        b.get_open_orders = MagicMock(side_effect=RuntimeError("timeout"))
        r = order_manager.submit_exit(b, "YOU", 111, 48.74, "T1 partial exit")
        assert r["status"] == "VETOED"
        assert not any(c[0] == "place" for c in b.calls)

    def test_full_exit_cancels_the_bracket_first(self, _db):
        """Same invariant for a full exit (FUBO 10-16 shape): the bracket legs
        go to zero before the 278sh exit is sent."""
        from src import order_manager
        _pos(_db, "YOU", 278)
        b = _FakeBroker()
        r = order_manager.submit_exit(b, "YOU", 278, 48.74, "TIME STOP")
        assert r["status"] == "SUBMITTED" and r["shares"] == 278
        assert ("resize", 85954, 0) in b.calls and ("resize", 85955, 0) in b.calls
        assert b.worst_case_sold("YOU") == 278

    def test_t2_after_t1_shrinks_only_by_the_new_amount(self, _db):
        from src import order_manager
        _pos(_db, "YOU", 167)
        b = _FakeBroker(qty=167)
        r = order_manager.submit_exit(b, "YOU", 83, 52.0, "T2 partial exit")
        assert r["status"] == "SUBMITTED"
        assert ("resize", 85954, 84) in b.calls and ("resize", 85955, 84) in b.calls
        assert b.worst_case_sold("YOU") == 167


class TestWorkerMakeRoom:
    """The worker's make_room also sees orphaned (clientId-0) legs and fails
    closed when it can't."""

    @staticmethod
    def _wire(monkeypatch, orphan):
        from src import ibkr_worker
        monkeypatch.setattr(ibkr_worker, "ORPHAN_CLIENT_ID", 0)
        monkeypatch.setattr(ibkr_worker, "_open_orphan_connection", lambda conn: orphan)
        own = _FakeBroker()
        own.orders = []
        own.client_id = 1
        return ibkr_worker, own

    def test_orphan_session_unavailable_blocks_the_exit(self, _db, monkeypatch):
        from src import order_manager
        w, own = self._wire(monkeypatch, None)
        _pos(_db, "YOU", 278)
        r = order_manager.submit_exit(own, "YOU", 111, 48.74, "T1",
                                      make_room=w._exit_room(own))
        assert r["status"] == "VETOED" and r["retry"]
        assert not any(c[0] == "place" for c in own.calls)

    def test_orphaned_legs_resized_through_the_orphan_session(self, _db, monkeypatch):
        """RIG shape: the bracket legs are orphaned and have lost their parent
        link; an equal-size GTC STP+LMT is still read as one OCA claim."""
        from src import order_manager
        orphan = _FakeBroker()
        for i, o in enumerate(orphan.orders):
            o["order_id"], o["parent_id"] = -2 - i, 0
        orphan.disconnect = MagicMock()
        w, own = self._wire(monkeypatch, orphan)
        _pos(_db, "YOU", 278)

        r = order_manager.submit_exit(own, "YOU", 111, 48.74, "T1",
                                      make_room=w._exit_room(own))

        assert r["status"] == "SUBMITTED"
        assert ("resize", -2, 167) in orphan.calls and ("resize", -3, 167) in orphan.calls
        orphan.disconnect.assert_called_once()
        assert orphan.worst_case_sold("YOU") + own.worst_case_sold("YOU") == 278


class TestTieredExitReplay:
    """_check_tiered_exits end to end on the YOU position."""

    @pytest.fixture
    def worker(self, _db, monkeypatch):
        from src import ibkr_worker
        monkeypatch.setattr("src.ibkr_worker.get_connection", _db)
        monkeypatch.setattr(ibkr_worker, "ORPHAN_CLIENT_ID", -1)
        monkeypatch.setattr(ibkr_worker, "_send_telegram", lambda m: True)
        monkeypatch.setattr(ibkr_worker, "_lookup_stop_order_id", lambda t: 85954)
        monkeypatch.setattr(ibkr_worker, "_exit_block_alerted", {})
        with _db() as c:
            # avg 45.42, last 48.79 -> +7.4%, past TIER1_PCT
            c.execute("INSERT INTO ibkr_positions (ticker, shares, avg_cost, unrealized_pnl,"
                      " market_value, last_synced, exit_tier) VALUES (?,?,?,?,?,?,0)",
                      ("YOU", 278, 45.42, 278 * (48.79 - 45.42), 278 * 48.79,
                       datetime.now().isoformat()))
        return ibkr_worker

    def test_t1_never_leaves_more_than_held_resting(self, worker, _db):
        b = _FakeBroker()
        b.client_id = 1
        b.modify_stop_order = MagicMock(return_value=False)  # 11-04: not confirmed
        worker._check_tiered_exits(b)
        assert ("place", "YOU", 111) in b.calls
        assert b.worst_case_sold("YOU") == 278

    def test_blocked_t1_is_rearmed_and_sends_nothing(self, worker, _db):
        b = _FakeBroker(confirm=False)
        b.client_id = 1
        b.modify_stop_order = MagicMock(return_value=True)
        worker._check_tiered_exits(b)
        assert not any(c[0] == "place" for c in b.calls)
        b.modify_stop_order.assert_not_called()
        with _db() as c:
            tier = c.execute(
                "SELECT exit_tier FROM ibkr_positions WHERE ticker='YOU'").fetchone()[0]
        assert tier == 0, "nothing sold, so T1 must retry next cycle"


class TestScoreExitRetriesWhenBlocked:
    """A score-deterioration exit writes its 24h dedup row before submitting.
    If submit_exit blocks it (legs couldn't be shrunk), nothing was sold, so
    the dedup row must go: otherwise the exit is silently skipped for a day."""

    @pytest.fixture
    def worker(self, _db, monkeypatch):
        from src import ibkr_worker
        monkeypatch.setattr("src.ibkr_worker.get_connection", _db)
        monkeypatch.setattr(ibkr_worker, "ORPHAN_CLIENT_ID", -1)
        monkeypatch.setattr(ibkr_worker, "_send_telegram", lambda m: True)
        with _db() as c:
            c.executescript("""
                CREATE TABLE forward_signals (ticker TEXT, signal_type TEXT,
                    composite_score REAL, signal_ts TEXT);
                CREATE TABLE scan_runs (id INTEGER PRIMARY KEY, scan_type TEXT);
                CREATE TABLE scan_results (ticker TEXT, run_id INTEGER,
                    explosion_score REAL, scanned_at TEXT);
                CREATE TABLE watchlist_alerts (id INTEGER PRIMARY KEY, ticker TEXT,
                    alert_type TEXT, message TEXT, sent_at TEXT, score REAL, price REAL);
            """)
            now = datetime.now().isoformat()
            c.execute("INSERT INTO forward_signals VALUES ('YOU','BUY',80,?)", (now,))
            c.execute("INSERT INTO scan_runs VALUES (1,'scheduled')")
            c.execute("INSERT INTO scan_results VALUES ('YOU',1,40,?)", (now,))
            c.execute("INSERT INTO ibkr_positions (ticker, shares, avg_cost, unrealized_pnl,"
                      " market_value, last_synced) VALUES ('YOU',278,48,0,?,?)",
                      (278 * 48.0, now))
        return ibkr_worker

    def _dedup_rows(self, _db):
        with _db() as c:
            return c.execute("SELECT COUNT(*) FROM watchlist_alerts WHERE "
                             "alert_type='score_deterioration_exit'").fetchone()[0]

    def test_blocked_exit_removes_dedup_row(self, worker, _db):
        b = _FakeBroker(confirm=False)
        worker._check_score_deterioration(b)
        assert not any(c[0] == "place" for c in b.calls)
        assert self._dedup_rows(_db) == 0, "must retry next cycle"

    def test_sent_exit_keeps_dedup_row(self, worker, _db):
        b = _FakeBroker()
        worker._check_score_deterioration(b)
        assert ("place", "YOU", 278) in b.calls
        assert b.worst_case_sold("YOU") == 278
        assert self._dedup_rows(_db) == 1
