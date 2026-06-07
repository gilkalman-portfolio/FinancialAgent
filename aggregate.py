"""
Aggregator - Unified stock scoring across all scan sources
Combines: Russell sectors + Meme/Squeeze + Watchlist
Outputs ranked list to terminal.

Usage:
    python aggregate.py                         # scan all sources
    python aggregate.py --sectors Tech Health   # specific sectors only
    python aggregate.py --no-meme               # skip meme scanner
    python aggregate.py --top 20 --min 50       # top 20, min score 50
    python aggregate.py --max 30                # limit stocks per sector
"""

import sys
sys.path.insert(0, 'src')

import argparse
import pandas as pd
from tqdm import tqdm
from loguru import logger
from datetime import datetime

from src.stock_scorer import score_stock, signal_label
from src.database import init_db, save_scan_run, save_result
from src.telegram_notifier import TelegramNotifier
from src.score_alert import check_alerts

CSV_PATH = 'russell_holdings.csv'

MEME_WATCHLIST = [
    'GME', 'AMC', 'IONQ', 'RGTI', 'PLTR', 'SOFI', 'HOOD',
    'QBTS', 'LUNR', 'RKLB', 'SPCE', 'ARQQ', 'DJT', 'BBAI'
]

# ══════════════════════════════════════════════════════════════════════════════

def load_csv():
    for skip in [10, 9, 11, 0, 1, 2]:
        try:
            df = pd.read_csv(CSV_PATH, skiprows=skip, encoding='utf-8', on_bad_lines='skip')
            ticker_col = next((c for c in df.columns if 'ticker' in c.lower() or 'symbol' in c.lower()), None)
            sector_col = next((c for c in df.columns if 'sector' in c.lower()), None)
            if ticker_col and sector_col:
                out = df[[ticker_col, sector_col]].copy()
                out.columns = ['ticker', 'sector']
                out = out.dropna(subset=['ticker', 'sector'])
                out = out[out['ticker'].str.len() <= 5]
                return out
        except Exception:
            continue
    return pd.DataFrame()


def collect_tickers(sectors: list, df: pd.DataFrame, max_per_sector: int) -> dict:
    """Returns {source_label: [tickers]}"""
    sources = {}
    for sector in sectors:
        mask = df['sector'].str.lower().str.contains(sector.lower(), na=False)
        tickers = df[mask]['ticker'].tolist()
        if not tickers:
            print(f"  No sector matching '{sector}' - skipping")
            continue
        actual = df[mask]['sector'].iloc[0]
        if max_per_sector:
            tickers = tickers[:max_per_sector]
        sources[actual] = tickers
    return sources


def scan_tickers(label: str, tickers: list, forecast_days: int) -> list:
    results = []
    for ticker in tqdm(tickers, desc=f"{label[:30]:<30}", ncols=80):
        data = score_stock(ticker, forecast_days=forecast_days)
        if data:
            data['source'] = label
            results.append(data)
    return results


def display(results: list, top: int = None):
    if not results:
        print("\nNo stocks found above minimum score. Try lowering --min.")
        return

    show = results[:top] if top else results
    print(f"\n{'='*110}")
    print(f" AGGREGATED RESULTS  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  {len(show)} stocks shown")
    print(f"{'='*110}")
    print(f"{'#':<4} {'Ticker':<8} {'Score':<7} {'Signal':<12} {'Price':<9} {'FC%':<8} {'RSI':<6} {'MACD':<10} {'MA Trend':<16} {'Short%':<8} {'DTC':<6} {'Inst%':<7} {'Reddit':<9} {'Trends':<6} Source")
    print(f"{'-'*120}")

    for i, s in enumerate(show, 1):
        fc   = f"{s['forecast_change']:+.1f}%" if s['forecast_change'] is not None else "N/A"
        rsi  = f"{s['rsi']:.0f}" if s['rsi'] else "N/A"
        gx   = "[GX]" if s.get('golden_cross') else ""
        ma   = f"{s['ma_trend']} {gx}".strip()
        si   = f"{s['short_pct']:.1f}%" if s.get('short_pct') is not None else "N/A"
        dtc  = f"{s['days_to_cover']:.1f}" if s.get('days_to_cover') else "N/A"
        inst = f"{s['inst_pct']:.0f}%" if s.get('inst_pct') is not None else "N/A"
        rd  = f"{s.get('reddit_mentions', 0)}m/{s.get('reddit_velocity', 0):.1f}x"
        tr  = f"{s.get('trends_interest', 0)}{'*' if s.get('trends_spike') else ''}"
        print(
            f"{i:<4} {s['ticker']:<8} {s['score']:<7.1f} {signal_label(s['score']):<12} "
            f"${s['price']:<8.2f} {fc:<8} {rsi:<6} {s['macd']:<10} {ma:<16} "
            f"{si:<8} {dtc:<6} {inst:<7} {rd:<9} {tr:<6} {s['source']}"
        )

    print(f"\nTotal above threshold: {len(results)}  |  Showing: {len(show)}")
    if top and len(results) > top:
        print(f"Use --top {len(results)} to see all")


# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Aggregated Stock Scanner')
    parser.add_argument('--sectors', nargs='+', default=None,
                        help='Sector names to scan (default: all)')
    parser.add_argument('--no-meme', action='store_true',
                        help='Skip meme/watchlist scan')
    parser.add_argument('--min', type=float, default=45.0,
                        help='Minimum score to show (default: 45)')
    parser.add_argument('--top', type=int, default=25,
                        help='Show top N results (default: 25)')
    parser.add_argument('--max', type=int, default=50,
                        help='Max stocks per sector (default: 50)')
    parser.add_argument('--days', type=int, default=30,
                        help='Forecast days (default: 30)')
    args = parser.parse_args()

    init_db()
    all_results = []

    # ── Russell sectors ──────────────────────────────────────────────────────
    df = load_csv()
    if df.empty:
        print(f"WARNING: Could not load {CSV_PATH} - skipping Russell sectors")
    else:
        available_sectors = df['sector'].unique().tolist()
        sectors_to_scan = args.sectors if args.sectors else available_sectors

        print(f"\nSectors to scan: {len(sectors_to_scan)}")
        sources = collect_tickers(sectors_to_scan, df, args.max)

        run_id = save_scan_run(
            scan_type="aggregate",
            total_scanned=sum(len(v) for v in sources.values())
        )

        for label, tickers in sources.items():
            print(f"\n[{label}] - {len(tickers)} stocks")
            results = scan_tickers(label, tickers, args.days)
            for r in results:
                save_result(run_id, {**r, 'explosion_score': r['score']})
            all_results.extend(results)

    # ── Meme / watchlist ─────────────────────────────────────────────────────
    if not args.no_meme:
        print(f"\n[Watchlist] - {len(MEME_WATCHLIST)} stocks")
        results = scan_tickers("Watchlist", MEME_WATCHLIST, args.days)
        for r in results:
            save_result(run_id if 'run_id' in dir() else 0, {**r, 'explosion_score': r['score']})
        all_results.extend(results)

    # ── Score alerts ─────────────────────────────────────────────────────────
    if all_results:
        jumped = check_alerts(all_results)
        if jumped:
            logger.info(f"Score alerts sent: {len(jumped)}")

    # ── Display ──────────────────────────────────────────────────────────────
    filtered = [r for r in all_results if r['score'] >= args.min]
    filtered.sort(key=lambda x: x['score'], reverse=True)
    display(filtered, top=args.top)

    # ── Telegram summary ─────────────────────────────────────────────────────
    filtered = [r for r in all_results if r['score'] >= args.min]
    filtered.sort(key=lambda x: x['score'], reverse=True)
    if filtered:
        telegram = TelegramNotifier()
        top_lines = "\n".join(
            f"{i+1}. {r['ticker']} {r['score']:.0f} - {signal_label(r['score'])} | {r['macd']} | {r['ma_trend']}"
            for i, r in enumerate(filtered[:10])
        )
        telegram.send_message(
            f"Aggregate Scan {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"Top {min(10, len(filtered))} of {len(filtered)} above {args.min}\n\n"
            f"{top_lines}",
            parse_mode='HTML'
        )


if __name__ == '__main__':
    main()
