"""
IBKR-side outage (Gateway <-> IBKR link down, Error 1100) must not look like a
worker stall.

2026-11-07 21:31 -> 11-08 02:01 IL: IBKR's servers were unreachable (weekend
maintenance). The Gateway kept accepting API connections but repeated Error
1100 and data requests hung. The stall guard read each hang as a displaced
session and restarted the worker every ~13 min: 20 restarts and a Telegram
pair per restart, for an outage nobody could act on. Same pattern on 10-17
(32 restarts) and 11-01/02 (74).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import ibkr_worker as w


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(w, "_link_down_first", None)
    monkeypatch.setattr(w, "_link_last_1100", 0.0)
    monkeypatch.setattr(w, "_link_alerted", False)
    monkeypatch.setattr(w, "_last_heartbeat", 0.0)
    sent = []
    monkeypatch.setattr(w, "_send_telegram", lambda m: sent.append(m) or True)
    clock = [10_000.0]
    monkeypatch.setattr(w.time, "monotonic", lambda: clock[0])
    yield sent, clock


def _tick(clock, secs):
    clock[0] += secs


class TestLinkState:
    def test_1100_marks_down_and_1102_restores(self, _reset):
        w._on_ib_error(-1, 1100, "Connectivity between IBKR and TWS has been lost.")
        assert w._link_is_down()
        w._on_ib_error(-1, 1102, "Connectivity ... restored - data maintained.")
        assert not w._link_is_down()

    def test_1101_also_restores(self, _reset):
        w._on_ib_error(-1, 1100)
        w._on_ib_error(-1, 1101)
        assert not w._link_is_down()

    def test_unrelated_codes_ignored(self, _reset):
        for code in (2104, 2106, 2158, 201, 399, "x"):
            w._on_ib_error(-1, code)
        assert not w._link_is_down()

    def test_restore_message_missed_expires_after_ttl(self, _reset):
        """The worker sleeps disconnected between cycles, so a 1102 can be
        missed. No 1100 for the TTL counts as restored, never stuck down."""
        _, clock = _reset
        w._on_ib_error(-1, 1100)
        _tick(clock, w.LINK_DOWN_TTL_SECS - 1)
        assert w._link_is_down()
        _tick(clock, 2)
        assert not w._link_is_down()
        w._link_watch()
        assert w._link_down_first is None

    def test_repeating_1100_keeps_it_down(self, _reset):
        """Observed gaps between repeats during the 11-07 outage were <=153s."""
        _, clock = _reset
        for _ in range(40):
            w._on_ib_error(-1, 1100)
            _tick(clock, 153)
            assert w._link_is_down()


class TestAlerts:
    def test_brief_blip_sends_nothing(self, _reset):
        """11-07 12:31:46 -> 12:32:11: a 25s blip must stay silent."""
        sent, clock = _reset
        w._on_ib_error(-1, 1100)
        _tick(clock, 25)
        w._link_watch()
        w._on_ib_error(-1, 1102)
        w._link_watch()
        assert sent == []

    def test_one_message_down_one_message_up(self, _reset):
        sent, clock = _reset
        w._on_ib_error(-1, 1100)
        for _ in range(60):                  # 4.5h of 1100s every ~90s, guard every 30s
            _tick(clock, 30)
            w._on_ib_error(-1, 1100)
            w._link_watch()
        assert len(sent) == 1 and "unreachable" in sent[0]
        w._on_ib_error(-1, 1102)
        w._link_watch()
        w._link_watch()
        assert len(sent) == 2 and "reachable again" in sent[1]

    def test_restored_by_ttl_also_sends_the_up_message(self, _reset):
        sent, clock = _reset
        w._on_ib_error(-1, 1100)
        for _ in range(4):                   # 1100 every 100s, past the 300s grace
            _tick(clock, 100)
            w._on_ib_error(-1, 1100)
        w._link_watch()
        assert len(sent) == 1
        _tick(clock, w.LINK_DOWN_TTL_SECS + 1)   # 1100s stop, 1102 never seen
        w._link_watch()
        assert len(sent) == 2 and "reachable again" in sent[1]


class TestStallClock:
    def test_stall_clock_held_while_link_down(self, _reset):
        """A request hung by the outage is not a stall: the reference keeps
        moving forward with each 1100."""
        _, clock = _reset
        w._last_heartbeat = clock[0]
        for _ in range(30):                  # 30 * 100s = 50 min, well past 720s
            _tick(clock, 100)
            w._on_ib_error(-1, 1100)
        assert clock[0] - w._stall_reference() < w.STALL_TIMEOUT_SECS

    def test_real_stall_still_detected_once_link_is_back(self, _reset):
        _, clock = _reset
        w._last_heartbeat = clock[0]
        w._on_ib_error(-1, 1100)
        w._on_ib_error(-1, 1102)
        _tick(clock, w.STALL_TIMEOUT_SECS + 1)
        assert clock[0] - w._stall_reference() > w.STALL_TIMEOUT_SECS

    def test_no_outage_reference_is_heartbeat(self, _reset):
        _, clock = _reset
        w._last_heartbeat = clock[0] - 5
        assert w._stall_reference() == w._last_heartbeat


class TestMainLoopSkipsCycle:
    def test_cycle_skipped_while_link_down(self, _reset, monkeypatch):
        """Connect during an outage: the Gateway sends 1100, run_once must not
        run (it would hang), and the loop just sleeps until the next cycle."""
        conns = []

        class _Conn:
            def __init__(self, *a, **k):
                self.ib = MagicMock()
                self.ib.errorEvent = w_events = []

                class _Ev(list):
                    def __iadd__(s, fn):
                        s.append(fn)
                        return s
                self.ib.errorEvent = _Ev()
                self.disconnected = False
                conns.append(self)

            def connect(self, timeout=15.0, **k):
                for fn in self.ib.errorEvent:
                    fn(-1, 1100, "Connectivity between IBKR and TWS has been lost.")

            def disconnect(self):
                self.disconnected = True

        run_once = MagicMock(return_value=0)
        monkeypatch.setattr(w, "IBKRConnection", _Conn)
        monkeypatch.setattr(w, "run_once", run_once)
        monkeypatch.setattr(w, "init_db", lambda: None)
        monkeypatch.setattr(w, "_start_stall_guard", lambda: None)
        monkeypatch.setattr(w, "_reconcile_orders_on_startup", lambda c: None)
        monkeypatch.setitem(sys.modules, "src.telegram_command_handler", None)

        def _stop(_secs):
            raise KeyboardInterrupt
        monkeypatch.setattr(w.time, "sleep", _stop)

        assert w._main_body() == 0
        run_once.assert_not_called()
        assert conns[-1].disconnected

    def test_cycle_runs_when_link_up(self, _reset, monkeypatch):
        class _Ev(list):
            def __iadd__(s, fn):
                s.append(fn)
                return s

            def __isub__(s, fn):
                return s

        class _Conn:
            def __init__(self, *a, **k):
                self.ib = MagicMock()
                self.ib.errorEvent = _Ev()
                self.ib.orderStatusEvent = _Ev()

            def connect(self, timeout=15.0, **k):
                pass

            def disconnect(self):
                pass

        run_once = MagicMock(return_value=0)
        monkeypatch.setattr(w, "IBKRConnection", _Conn)
        monkeypatch.setattr(w, "run_once", run_once)
        monkeypatch.setattr(w, "init_db", lambda: None)
        monkeypatch.setattr(w, "_start_stall_guard", lambda: None)
        monkeypatch.setattr(w, "_reconcile_orders_on_startup", lambda c: None)
        monkeypatch.setitem(sys.modules, "src.telegram_command_handler", None)
        monkeypatch.setattr(w.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt))

        w._main_body()
        run_once.assert_called_once()
