"""
Sector Scanner - Scan any Russell 2000 sector with full technical scoring
Usage:
    python scan_sector.py                         # interactive menu
    python scan_sector.py Technology              # scan specific sector
    python scan_sector.py Technology --top 10     # show top 10 only
    python scan_sector.py Technology --min 50     # min score 50
    python scan_sector.py --list                  # list available sectors
"""

import sys
sys.path.insert(0, 'src')

import argparse
import pandas as pd
from tqdm import tqdm
from loguru import logger
from src.stock_scorer import score_stock, signal_label
from src.database import init_db, save_scan_run, save_result

CSV_PATH = 'russell_holdings.csv'

# ══════════════════════════════════════════════════════════════════════════════

def load_csv() -> pd.DataFrame:
    for skip in [10, 9, 11, 0, 1, 2]:
        try:
            df = pd.read_csv(CSV_PATH, skiprows=skip, encoding='utf-8', on_bad_lines='skip')
            ticker_col = next((c for c in df.columns if 'ticker' in c.lower() or 'symbol' in c.lower()), None)
            sector_col = next((c for c in df.columns if 'sector' in c.lower()), None)
            name_col   = next((c for c in df.columns if 'name' in c.lower() and 'file' not in c.lower()), None)
            if ticker_col and sector_col:
                out = df[[ticker_col, name_col or ticker_col, sector_col]].copy()
                out.columns = ['ticker', 'name', 'sector']
                out = out.dropna(subset=['ticker', 'sector'])
                out = out[out['ticker'].str.len() <= 5]
                return out
        except Exception:
            continue
    return pd.DataFrame()


def list_sectors(df: pd.DataFrame):
    print("\nAvailable sectors:")
    print("-" * 50)
    for i, (sector, count) in enumerate(df['sector'].value_counts().items(), 1):
        print(f"  {i:2}. {sector:<40} ({count} stocks)")


def scan(sector: str, df: pd.DataFrame, min_score: float = 40.0, max_stocks: int = None, forecast_days: int = 30):
    mask = df['sector'].str.lower().str.contains(sector.lower(), na=False)
    tickers = df[mask]['ticker'].tolist()

    if not tickers:
        print(f"No sector matching '{sector}'. Use --list to see options.")
        return []

    actual_sector = df[mask]['sector'].iloc[0]
    if max_stocks:
        tickers = tickers[:max_stocks]

    print(f"\nScanning {len(tickers)} stocks in '{actual_sector}'...")
    print(f"Min score: {min_score} | Forecast: {forecast_days}d\n")

    init_db()
    run_id = save_scan_run(scan_type=f"sector:{actual_sector}", total_scanned=len(tickers))

    results = []
    for ticker in tqdm(tickers, desc="Analyzing"):
        data = score_stock(ticker, forecast_days=forecast_days)
        if data and data['score'] >= min_score:
            results.append(data)
            save_result(run_id, {**data, 'explosion_score': data['score']})

    results.sort(key=lambda x: x['score'], reverse=True)
    return results


def display(results: list, top: int = None):
    if not results:
        print("\nNo stocks found above minimum score. Try lowering --min.")
        return

    show = results[:top] if top else results
    print(f"\n{'='*90}")
    print(f"{'#':<4} {'Ticker':<8} {'Score':<7} {'Signal':<12} {'Price':<9} {'Forecast':<12} {'RSI':<7} {'MACD':<10} {'MA Trend':<14} {'Vol':<6} {'Mom'}")
    print("-" * 90)

    for i, s in enumerate(show, 1):
        fc = f"{s['forecast_change']:+.1f}%" if s['forecast_change'] is not None else "  N/A"
        rsi = f"{s['rsi']:.0f}" if s['rsi'] else " N/A"
        gc = " [GX]" if s.get('golden_cross') else ""
        print(
            f"{i:<4} {s['ticker']:<8} {s['score']:<7.1f} {signal_label(s['score']):<12} "
            f"${s['price']:<8.2f} {fc:<12} {rsi:<7} {s['macd']:<10} {s['ma_trend']}{gc:<14} "
            f"{s['volume_ratio']:<6.2f}x {s['momentum']:+.1f}%"
        )

    print(f"\nTotal found: {len(results)} stocks above threshold")
    if top and len(results) > top:
        print(f"(showing top {top} of {len(results)})")


# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Russell 2000 Sector Scanner')
    parser.add_argument('sector', nargs='?', help='Sector name (partial match ok)')
    parser.add_argument('--list', action='store_true', help='List available sectors')
    parser.add_argument('--min', type=float, default=40.0, help='Minimum score (default: 40)')
    parser.add_argument('--top', type=int, default=None, help='Show top N results')
    parser.add_argument('--max', type=int, default=None, help='Max stocks to scan')
    parser.add_argument('--days', type=int, default=30, help='Forecast days (default: 30)')
    args = parser.parse_args()

    df = load_csv()
    if df.empty:
        print(f"\nERROR: Could not load {CSV_PATH}")
        print("Download from: https://www.ishares.com/us/products/239710/")
        print("Click Holdings > Download to Excel > save as russell_holdings.csv")
        return

    if args.list:
        list_sectors(df)
        return

    if not args.sector:
        # Interactive menu
        list_sectors(df)
        print()
        sector = input("Enter sector name (or part of it): ").strip()
        if not sector:
            return
    else:
        sector = args.sector

    results = scan(
        sector=sector,
        df=df,
        min_score=args.min,
        max_stocks=args.max,
        forecast_days=args.days
    )
    display(results, top=args.top)


if __name__ == '__main__':
    main()
