"""
Single-cycle test for ibkr_worker — connects, scans the queue once, disconnects.

Run with .venv313:
    .venv313\\Scripts\\python.exe -m tests.test_ibkr_worker_once
"""

import logging
import sys

from src.database import init_db
from src.ibkr_realtime import IBKRConnection, PAPER_PORT
from src.ibkr_worker import run_once


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    init_db()

    print("=" * 70)
    print("IBKR Worker — single cycle test")
    print("=" * 70)

    conn = IBKRConnection(port=PAPER_PORT, client_id=7)
    try:
        conn.connect(timeout=15.0)
        print(f"[OK] connected. isConnected={conn.is_connected()}")
        fired = run_once(conn)
        print(f"[OK] cycle complete — {fired} alert(s) fired")
        return 0
    finally:
        conn.disconnect()
        print("[OK] disconnected")


if __name__ == "__main__":
    sys.exit(main())
