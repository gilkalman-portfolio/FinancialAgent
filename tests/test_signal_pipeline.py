"""
End-to-end smoke test for the signal pipeline WITHOUT IBKR.

Picks a real ticker from the current monitoring_queue, fabricates a
SupertrendEvent, runs it through the combiner, and verifies that:
  - if it passes filters, a CombinedAlert is returned
  - dedup blocks the second identical event
  - forward_signals row is persisted

Run:
    .venv\\Scripts\\python.exe -m tests.test_signal_pipeline
"""

import logging
import sys

from src.database import init_db, get_connection
from src.monitoring_queue import build_queue
from src.signal_combiner import (
    evaluate,
    SupertrendEvent,
    ALERT_TYPE_BUY,
)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    init_db()

    print("=" * 70)
    print("Signal Pipeline Smoke Test")
    print("=" * 70)

    queue = build_queue(apply_liquidity_gate=False)
    if not queue:
        print("[SKIP] Monitoring queue is empty — run the scanner first to populate it.")
        return 0

    # pick any queue entry (no score gate — Supertrend flip is the sole trigger)
    candidate = queue[0]

    print(f"Candidate: {candidate.ticker}  score={candidate.composite_score}  src={candidate.source}")

    event = SupertrendEvent(
        ticker=candidate.ticker,
        direction="Bullish",
        signal="BUY",
        level=100.0,
        last_price=105.0,
    )

    # purge any prior dedup row for this ticker/type so the test is deterministic
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM watchlist_alerts WHERE ticker = ? AND alert_type = ?",
            (candidate.ticker, ALERT_TYPE_BUY),
        )
        conn.execute(
            "DELETE FROM forward_signals WHERE ticker = ? AND entry_price = 105.0",
            (candidate.ticker,),
        )

    print("\n--- First evaluation (should fire) ---")
    result = evaluate(event)
    if result is None:
        print("[FAIL] First evaluate() returned None — pipeline rejected the event.")
        return 1
    print(f"[OK] CombinedAlert fired for {result.ticker}")
    print("Message preview:")
    print("-" * 60)
    print(result.message)
    print("-" * 60)

    print("\n--- Second evaluation (should dedup) ---")
    result2 = evaluate(event)
    if result2 is not None:
        print(f"[FAIL] Dedup did not block second event for {result2.ticker}")
        return 2
    print("[OK] dedup correctly suppressed second event")

    print("\n--- Verify forward_signals row was persisted ---")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, ticker, signal_type, entry_price, composite_score, status "
            "FROM forward_signals WHERE ticker = ? AND entry_price = 105.0",
            (candidate.ticker,),
        ).fetchone()
    if row is None:
        print("[FAIL] forward_signals row not found.")
        return 3
    print(f"[OK] forward_signals id={row['id']} {row['signal_type']} "
          f"entry=${row['entry_price']} score={row['composite_score']} status={row['status']}")

    print("\n" + "=" * 70)
    print("Pipeline test PASSED")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
