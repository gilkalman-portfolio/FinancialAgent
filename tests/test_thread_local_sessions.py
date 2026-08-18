"""
Test: sec_api_client and InsiderTracker each hand out one requests.Session
per calling thread, not a single Session shared across threads.

Background (2026-08-18): both modules held a module-level (or singleton
instance-level) `requests.Session()` reused by every caller. Once
watchlist_manager.py started scoring tickers concurrently via
ThreadPoolExecutor, every worker thread would have hit the same shared
Session object simultaneously — the exact pattern that crashed the scheduler
process with a native STATUS_HEAP_CORRUPTION fault earlier that same day
(src/gap_scanner.py, fixed there with the same threading.local() pattern
these two modules now reuse). This test asserts the actual property that
prevents a repeat: distinct threads get distinct Session objects, and a
single thread reuses its own Session across calls (so the fix doesn't just
avoid sharing by re-creating a fresh Session — and its connection pool —
on every single call).

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_thread_local_sessions.py -v
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sec_api_client import _thread_session as sec_api_thread_session  # noqa: E402
from src.insider_tracker import InsiderTracker  # noqa: E402


def _collect_across_threads(get_session, n=5):
    sessions = {}

    def worker(name):
        sessions[name] = get_session()

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sessions


def test_sec_api_client_session_is_thread_local():
    sessions = _collect_across_threads(sec_api_thread_session)
    distinct_ids = {id(s) for s in sessions.values()}
    assert len(distinct_ids) == 5, "each thread must get its own Session instance"


def test_sec_api_client_session_reused_within_same_thread():
    first = sec_api_thread_session()
    second = sec_api_thread_session()
    assert first is second


def test_insider_tracker_session_is_thread_local():
    tracker = InsiderTracker()
    sessions = _collect_across_threads(tracker._thread_session)
    distinct_ids = {id(s) for s in sessions.values()}
    assert len(distinct_ids) == 5, "each thread must get its own Session instance"


def test_insider_tracker_session_reused_within_same_thread():
    tracker = InsiderTracker()
    first = tracker._thread_session()
    second = tracker._thread_session()
    assert first is second


def test_insider_tracker_has_no_shared_instance_session():
    """The specific defect: InsiderTracker used to set self.session in
    __init__, which — combined with it being used as a module-level
    singleton (stock_scorer.py's `_insider`) — meant every thread hit the
    same object. Assert that attribute is simply gone."""
    tracker = InsiderTracker()
    assert not hasattr(tracker, "session")
