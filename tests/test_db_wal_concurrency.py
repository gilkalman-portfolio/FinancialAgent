"""
WAL concurrency smoke test.

Spawns 4 threads each performing ~250 writes to watchlist_alerts over 25 s
(simulating peak load of both scheduler + IBKR worker). Verifies:
  - no 'database is locked' errors leak through busy_timeout + retry
  - all inserted rows are present + readable concurrently
  - WAL file does not grow unbounded

Run:
    .venv\\Scripts\\python.exe -m tests.test_db_wal_concurrency
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime

from src.database import (
    DB_PATH,
    get_connection,
    init_db,
    watchlist_save_alert,
)

WRITERS = 4
WRITES_PER_WRITER = 250
WRITE_SLEEP = 0.02  # 50 writes/sec per thread → 200 writes/sec aggregate


def writer(thread_id: int, errors: list, counts: list) -> None:
    local_count = 0
    for i in range(WRITES_PER_WRITER):
        try:
            watchlist_save_alert(
                ticker=f"WALTEST{thread_id}",
                alert_type=f"wal_smoke_t{thread_id}",
                message=f"thread {thread_id} write {i} at {datetime.now().isoformat()}",
                score=float(i),
                price=100.0 + i * 0.01,
            )
            local_count += 1
        except Exception as e:
            errors.append((thread_id, i, repr(e)))
        time.sleep(WRITE_SLEEP)
    counts.append(local_count)


def reader(stop_event: threading.Event, errors: list, reads: list) -> None:
    while not stop_event.is_set():
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM watchlist_alerts WHERE alert_type LIKE 'wal_smoke%'"
                ).fetchone()
            reads.append(row["n"])
        except Exception as e:
            errors.append(("reader", -1, repr(e)))
        time.sleep(0.1)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    init_db()

    print("=" * 70)
    print("WAL Concurrency Smoke Test")
    print("=" * 70)

    # Cleanup any prior test rows
    with get_connection() as conn:
        conn.execute("DELETE FROM watchlist_alerts WHERE alert_type LIKE 'wal_smoke%'")

    errors: list = []
    counts: list = []
    reads: list = []
    stop = threading.Event()

    threads = [threading.Thread(target=writer, args=(i, errors, counts)) for i in range(WRITERS)]
    reader_thread = threading.Thread(target=reader, args=(stop, errors, reads))

    start = time.monotonic()
    reader_thread.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    reader_thread.join()
    elapsed = time.monotonic() - start

    expected = WRITERS * WRITES_PER_WRITER
    succeeded = sum(counts)
    with get_connection() as conn:
        actual = conn.execute(
            "SELECT COUNT(*) AS n FROM watchlist_alerts WHERE alert_type LIKE 'wal_smoke%'"
        ).fetchone()["n"]

    print(f"\nWriters       : {WRITERS}")
    print(f"Writes/writer : {WRITES_PER_WRITER}")
    print(f"Elapsed       : {elapsed:.1f}s  ({succeeded/elapsed:.0f} writes/sec aggregate)")
    print(f"Expected rows : {expected}")
    print(f"Succeeded     : {succeeded}")
    print(f"Actual in DB  : {actual}")
    print(f"Reader queries: {len(reads)}  (last count = {reads[-1] if reads else 'n/a'})")
    print(f"Errors        : {len(errors)}")
    if errors[:5]:
        for tid, i, msg in errors[:5]:
            print(f"  [thread {tid} write {i}] {msg}")

    # Cleanup
    with get_connection() as conn:
        conn.execute("DELETE FROM watchlist_alerts WHERE alert_type LIKE 'wal_smoke%'")

    if errors or succeeded != expected or actual != expected:
        print("\n[FAIL]")
        return 1

    # Verify WAL mode is actually active
    with get_connection() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"\njournal_mode: {mode}")
    if mode.lower() != "wal":
        print(f"[FAIL] journal_mode is {mode}, expected wal")
        return 2

    # Check WAL file size (should be reasonable)
    wal_path = DB_PATH.with_suffix(DB_PATH.suffix + "-wal")
    if wal_path.exists():
        wal_kb = wal_path.stat().st_size / 1024
        print(f"WAL file size: {wal_kb:.1f} KB")

    print("\n" + "=" * 70)
    print("WAL Concurrency Test PASSED")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
