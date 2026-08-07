"""
get_executions() must never hang the worker.

On 2026-08-05, after a paper-account reset, IB Gateway accepted the API
connection but answered data requests with "Positions info is not available yet"
(warning 2151) and never replied. reqExecutions() has no built-in timeout, so
the worker sat inside startup reconciliation for 25 minutes — connected, silent,
placing nothing and syncing nothing. A missed execution sweep is recoverable; a
hung worker is not.
"""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("ib_async", reason="ib_async is only installed in .venv313")

from src.ibkr_realtime import IBKRConnection  # noqa: E402


def _conn():
    c = IBKRConnection()
    c.ib = MagicMock()
    return c


class TestTimeout:
    def test_hanging_broker_does_not_block_forever(self):
        c = _conn()

        async def _never_returns(_f):
            await asyncio.sleep(30)

        c.ib.reqExecutionsAsync.side_effect = _never_returns
        c.ib.fills.return_value = []

        t0 = time.monotonic()
        out = c.get_executions(timeout=0.3)
        elapsed = time.monotonic() - t0

        assert out == {}
        assert elapsed < 5.0, f"returned after {elapsed:.1f}s — timeout not enforced"

    def test_session_fills_still_returned_when_request_times_out(self):
        """Degrade, don't fail: in-session fills are still usable."""
        c = _conn()

        async def _never_returns(_f):
            await asyncio.sleep(30)

        c.ib.reqExecutionsAsync.side_effect = _never_returns
        f = MagicMock()
        f.execution.orderId = 77
        f.execution.avgPrice = 12.5
        c.ib.fills.return_value = [f]

        out = c.get_executions(timeout=0.3)
        assert out == {77: 12.5}

    def test_broker_error_is_swallowed(self):
        c = _conn()
        c.ib.reqExecutionsAsync.side_effect = RuntimeError("not connected")
        c.ib.fills.return_value = []
        assert c.get_executions(timeout=1.0) == {}


class TestMergeSemantics:
    def test_broker_and_session_results_merge(self):
        c = _conn()
        broker = MagicMock()
        broker.execution.orderId = 1
        broker.execution.avgPrice = 10.0

        async def _ok(_f):
            return [broker]

        c.ib.reqExecutionsAsync.side_effect = _ok
        sess = MagicMock()
        sess.execution.orderId = 2
        sess.execution.avgPrice = 20.0
        c.ib.fills.return_value = [sess]

        assert c.get_executions(timeout=2.0) == {1: 10.0, 2: 20.0}

    def test_session_fill_wins_on_conflict(self):
        """In-session data is fresher than the broker snapshot."""
        c = _conn()
        broker = MagicMock()
        broker.execution.orderId = 1
        broker.execution.avgPrice = 10.0

        async def _ok(_f):
            return [broker]

        c.ib.reqExecutionsAsync.side_effect = _ok
        sess = MagicMock()
        sess.execution.orderId = 1
        sess.execution.avgPrice = 11.0
        c.ib.fills.return_value = [sess]

        assert c.get_executions(timeout=2.0) == {1: 11.0}
