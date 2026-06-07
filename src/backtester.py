"""
Backtester - בודק כמה מדויקות ההמלצות שלנו
רץ אוטומטית ובודק: מניות שקיבלו BUY לפני X ימים - כמה עלו בפועל?
שומר תוצאות ב-DB ומציג דוח דיוק.
"""

import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict
from loguru import logger
from src.database import get_connection, init_db


# ── DB setup ──────────────────────────────────────────────────────────────────

def init_backtest_tables():
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS backtest_results (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker        TEXT NOT NULL,
                signal        TEXT NOT NULL,
                score         REAL,
                price_at_signal REAL,
                price_after   REAL,
                pct_change    REAL,
                days_ahead    INTEGER,
                signal_date   TEXT,
                checked_at    TEXT,
                correct       INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_bt_ticker ON backtest_results(ticker);
            CREATE INDEX IF NOT EXISTS idx_bt_date   ON backtest_results(signal_date);
        """)


# ── Core logic ────────────────────────────────────────────────────────────────

def _signal_label(score: float) -> str:
    if score >= 75: return "STRONG BUY"
    if score >= 60: return "BUY"
    if score >= 45: return "WATCH"
    if score >= 35: return "NEUTRAL"
    return "SKIP"


def _get_price_at_date(ticker: str, target_date: datetime) -> float | None:
    """מחיר סגירה של מניה בתאריך מסוים"""
    try:
        start = target_date - timedelta(days=3)
        end   = target_date + timedelta(days=3)
        hist  = yf.Ticker(ticker).history(start=start, end=end)
        if hist.empty:
            return None
        # הקרוב ביותר לתאריך המבוקש
        idx = hist.index.searchsorted(pd.Timestamp(target_date, tz='America/New_York'))
        idx = min(idx, len(hist)-1)
        return float(hist['Close'].iloc[idx])
    except Exception:
        return None


def run_backtest(days_ahead: int = 7) -> Dict:
    """
    בודק את כל המניות שקיבלו BUY/STRONG BUY לפני X ימים
    ומשווה לביצועים בפועל.
    """
    init_backtest_tables()

    cutoff = (datetime.now() - timedelta(days=days_ahead)).isoformat()
    now_str = datetime.now().isoformat()

    with get_connection() as conn:
        # Most recent BUY/STRONG BUY scan per ticker before cutoff — all columns from the same row.
        #
        # NOTE on column naming: scan_results.explosion_score stores the composite stock_scorer
        # score (0–100) for rows written by scheduler.py / scan_worker.py / scan_sector.py
        # (all do: save_result(run_id, {**r, "explosion_score": r["score"]})).
        # automated_scanner.py writes a different metric (catalyst explosion score) under the
        # same column name. We filter to scan_type='scheduled' via the scan_runs join so that
        # only stock_scorer composite-score rows are included in the backtest.
        candidates = conn.execute("""
            SELECT sr.ticker, sr.explosion_score as score, sr.price as price_signal,
                   sr.scanned_at as scan_date
            FROM scan_results sr
            INNER JOIN scan_runs run ON sr.run_id = run.id
            INNER JOIN (
                SELECT sr2.ticker, MAX(sr2.scanned_at) as max_date
                FROM scan_results sr2
                INNER JOIN scan_runs run2 ON sr2.run_id = run2.id
                WHERE sr2.scanned_at <= ?
                  AND sr2.explosion_score >= 60
                  AND run2.scan_type = 'scheduled'
                GROUP BY sr2.ticker
            ) latest ON sr.ticker = latest.ticker AND sr.scanned_at = latest.max_date
            WHERE run.scan_type = 'scheduled'
        """, (cutoff,)).fetchall()

    logger.info(f"Backtesting {len(candidates)} BUY signals from {days_ahead}d ago")

    results = []
    correct = 0

    for row in candidates:
        ticker     = row["ticker"]
        score      = row["score"]
        price_sig  = row["price_signal"]
        scan_date  = datetime.fromisoformat(row["scan_date"])
        target_date = scan_date + timedelta(days=days_ahead)

        if target_date > datetime.now():
            continue  # עדיין לא עבר מספיק זמן

        price_after = _get_price_at_date(ticker, target_date)
        if not price_sig or not price_after:
            continue

        pct = ((price_after - price_sig) / price_sig) * 100
        signal = _signal_label(score)
        is_correct = 1 if pct > 0 else 0
        correct += is_correct

        results.append({
            "ticker":           ticker,
            "signal":           signal,
            "score":            score,
            "price_at_signal":  price_sig,
            "price_after":      price_after,
            "pct_change":       round(pct, 2),
            "days_ahead":       days_ahead,
            "signal_date":      scan_date.isoformat(),
            "checked_at":       now_str,
            "correct":          is_correct,
        })

    # שמור ב-DB
    if results:
        with get_connection() as conn:
            for r in results:
                # בדוק אם כבר קיים
                exists = conn.execute(
                    "SELECT id FROM backtest_results WHERE ticker=? AND signal_date=? AND days_ahead=?",
                    (r["ticker"], r["signal_date"], r["days_ahead"])
                ).fetchone()
                if not exists:
                    conn.execute("""
                        INSERT INTO backtest_results
                        (ticker,signal,score,price_at_signal,price_after,pct_change,
                         days_ahead,signal_date,checked_at,correct)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                    """, tuple(r.values()))

    accuracy = (correct / len(results) * 100) if results else 0
    avg_return = (sum(r["pct_change"] for r in results) / len(results)) if results else 0

    summary = {
        "days_ahead":  days_ahead,
        "total":       len(results),
        "correct":     correct,
        "accuracy_pct": round(accuracy, 1),
        "avg_return":  round(avg_return, 2),
        "results":     results,
    }

    logger.info(f"Backtest {days_ahead}d: {len(results)} signals, {accuracy:.1f}% accuracy, avg {avg_return:+.2f}%")
    return summary


def get_backtest_stats() -> Dict:
    """סטטיסטיקות כלליות מה-DB"""
    init_backtest_tables()
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT days_ahead,
                   COUNT(*) as total,
                   SUM(correct) as correct,
                   AVG(pct_change) as avg_return,
                   MAX(pct_change) as best,
                   MIN(pct_change) as worst
            FROM backtest_results
            GROUP BY days_ahead
            ORDER BY days_ahead
        """).fetchall()
        return [dict(r) for r in rows]


def get_top_signals(limit: int = 20) -> List[Dict]:
    """המניות שקיבלו BUY והכי עלו — one row per ticker (best result)"""
    init_backtest_tables()
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ticker, signal, score, MAX(pct_change) as pct_change, days_ahead, signal_date
            FROM backtest_results
            GROUP BY ticker
            ORDER BY pct_change DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_worst_signals(limit: int = 20) -> List[Dict]:
    """המניות שקיבלו BUY והכי ירדו — one row per ticker (worst result)"""
    init_backtest_tables()
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ticker, signal, score, MIN(pct_change) as pct_change, days_ahead, signal_date
            FROM backtest_results
            WHERE signal IN ('BUY', 'STRONG BUY')
            GROUP BY ticker
            ORDER BY pct_change ASC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    for days in [7, 14, 30]:
        summary = run_backtest(days)
        print(f"\n{days}d backtest: {summary['total']} signals | "
              f"accuracy: {summary['accuracy_pct']}% | "
              f"avg return: {summary['avg_return']:+.2f}%")
