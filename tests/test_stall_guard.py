"""
The worker must not be able to hang silently.

Two IBKR calls have blocked it indefinitely: reqExecutions on 2026-08-05 (25 min)
and something inside the position sync on 2026-08-06 (26 min, no log output at
all). Both were only reachable once the API session was already displaced by a
competing login, which is exactly where a timeout was least likely to have been
written. ib_async is single-threaded asyncio, so a blocked request blocks
everything while the process stays alive and the watchdog sees a healthy child.
"""

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import ibkr_worker  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_heartbeat():
    original = ibkr_worker._last_heartbeat
    yield
    ibkr_worker._last_heartbeat = original


class TestStallGuard:
    def test_kills_the_process_when_the_heartbeat_goes_stale(self):
        exits = []
        with patch.object(ibkr_worker, "STALL_CHECK_SECS", 0.05), \
             patch.object(ibkr_worker, "STALL_TIMEOUT_SECS", 0.2), \
             patch.object(ibkr_worker, "_send_telegram"), \
             patch.object(ibkr_worker.os, "_exit", side_effect=lambda c: exits.append(c)), \
             patch.object(ibkr_worker.logging, "shutdown"):
            ibkr_worker._last_heartbeat = time.monotonic() - 5.0   # long stalled
            ibkr_worker._start_stall_guard()
            time.sleep(0.6)

        assert exits, "a stalled cycle must terminate the process"
        assert exits[0] == 3

    def test_does_not_kill_while_cycles_are_progressing(self):
        exits = []
        with patch.object(ibkr_worker, "STALL_CHECK_SECS", 0.05), \
             patch.object(ibkr_worker, "STALL_TIMEOUT_SECS", 2.0), \
             patch.object(ibkr_worker, "_send_telegram"), \
             patch.object(ibkr_worker.os, "_exit", side_effect=lambda c: exits.append(c)), \
             patch.object(ibkr_worker.logging, "shutdown"):
            ibkr_worker._last_heartbeat = time.monotonic()
            ibkr_worker._start_stall_guard()
            for _ in range(8):            # keep beating, as a healthy loop would
                time.sleep(0.1)
                ibkr_worker._last_heartbeat = time.monotonic()
            time.sleep(0.2)

        assert not exits, "a healthy worker must never be killed"

    def test_waits_for_the_first_heartbeat_before_arming(self):
        """Startup work (reconciliation, first connect) must not trip the guard."""
        exits = []
        with patch.object(ibkr_worker, "STALL_CHECK_SECS", 0.05), \
             patch.object(ibkr_worker, "STALL_TIMEOUT_SECS", 0.1), \
             patch.object(ibkr_worker, "_send_telegram"), \
             patch.object(ibkr_worker.os, "_exit", side_effect=lambda c: exits.append(c)), \
             patch.object(ibkr_worker.logging, "shutdown"):
            ibkr_worker._last_heartbeat = 0.0     # no cycle has run yet
            ibkr_worker._start_stall_guard()
            time.sleep(0.4)

        assert not exits

    def test_alerts_before_exiting(self):
        sent = []
        with patch.object(ibkr_worker, "STALL_CHECK_SECS", 0.05), \
             patch.object(ibkr_worker, "STALL_TIMEOUT_SECS", 0.2), \
             patch.object(ibkr_worker, "_send_telegram", side_effect=lambda m: sent.append(m)), \
             patch.object(ibkr_worker.os, "_exit"), \
             patch.object(ibkr_worker.logging, "shutdown"):
            ibkr_worker._last_heartbeat = time.monotonic() - 5.0
            ibkr_worker._start_stall_guard()
            time.sleep(0.4)

        assert sent, "the operator must be told why it restarted"
        assert "STALLED" in sent[0]

    def test_telegram_failure_does_not_prevent_the_exit(self):
        exits = []
        with patch.object(ibkr_worker, "STALL_CHECK_SECS", 0.05), \
             patch.object(ibkr_worker, "STALL_TIMEOUT_SECS", 0.2), \
             patch.object(ibkr_worker, "_send_telegram", side_effect=RuntimeError("no net")), \
             patch.object(ibkr_worker.os, "_exit", side_effect=lambda c: exits.append(c)), \
             patch.object(ibkr_worker.logging, "shutdown"):
            ibkr_worker._last_heartbeat = time.monotonic() - 5.0
            ibkr_worker._start_stall_guard()
            time.sleep(0.4)

        assert exits, "alerting is best-effort; the exit is not"


class TestConfiguration:
    def test_timeout_is_longer_than_the_poll_interval(self):
        """Otherwise a normal idle gap between cycles reads as a stall."""
        assert ibkr_worker.STALL_TIMEOUT_SECS > ibkr_worker.POLL_INTERVAL_SECS

    def test_check_interval_is_shorter_than_the_timeout(self):
        assert ibkr_worker.STALL_CHECK_SECS < ibkr_worker.STALL_TIMEOUT_SECS
