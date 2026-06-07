"""
Smoke test: connect to Docker IB Gateway, pull AAPL bars, run Supertrend.

Run with:
    python -m tests.test_ibkr_connection
"""

import logging
import sys

from src.ibkr_realtime import ibkr_session, PAPER_PORT
from src.supertrend import supertrend


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    print("=" * 70)
    print("IBKR Connection Smoke Test")
    print("=" * 70)
    print(f"Target: localhost:{PAPER_PORT} (paper)")
    print()

    try:
        with ibkr_session(port=PAPER_PORT, client_id=42) as conn:
            print(f"[OK] Connected. isConnected={conn.is_connected()}")
            print()

            ticker = "AAPL"
            print(f"Fetching 1H bars for {ticker} (last 10 days)...")
            df = conn.historical_bars(ticker, bar_size="1 hour", duration="10 D")

            if df.empty:
                print(f"[FAIL] No bars returned for {ticker}")
                return 1

            print(f"[OK] Got {len(df)} bars")
            print()
            print("Last 5 bars:")
            print(df.tail(5).to_string())
            print()

            print(f"Running Supertrend on {ticker} (ATR=10, Mult=3.0)...")
            result = supertrend(df, period=10, multiplier=3.0)
            print()
            print(f"  Direction : {result['direction']}")
            print(f"  Signal    : {result['signal']}")
            print(f"  Level     : ${result['level']:.2f}" if result.get('level') else "  Level     : N/A")
            print()

            try:
                last_price = conn.live_price(ticker)
                print(f"Live snapshot price for {ticker}: ${last_price}")
            except Exception as e:
                print(f"[WARN] live_price failed (not critical): {e}")

            print()
            print("=" * 70)
            print("Test PASSED")
            print("=" * 70)
            return 0

    except ConnectionRefusedError:
        print()
        print("[FAIL] Connection refused on port 4002.")
        print("  - Is Docker IB Gateway running?  docker ps")
        print("  - Did login succeed in the Gateway?")
        return 2
    except Exception as e:
        print()
        print(f"[FAIL] Unexpected error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
