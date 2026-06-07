"""
Database Module - Historical Tracking
Stores scan results over time for trend analysis and dashboard

Concurrency model (after WAL hardening):
  - journal_mode = WAL              → readers don't block writers; one writer at a time
  - synchronous  = FULL             → durability gate; corruption-safe on Windows Docker / network FS
  - busy_timeout = 30000ms          → per-connection wait when another writer holds the lock
  - wal_autocheckpoint = 4000 pages → bound WAL file growth (~16 MB)
  - auto_vacuum = INCREMENTAL       → reclaim space without exclusive VACUUM lock

Default `isolation_level` is intentionally KEPT (not set to None). This preserves
transactional semantics of `with conn:` blocks throughout the codebase. Do not
add isolation_level=None — it silently enables autocommit and breaks atomicity.
"""

import functools
import sqlite3
import time
import json
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any, Callable, TypeVar
from loguru import logger

DB_PATH = Path(__file__).parent.parent / "data" / "financial_agent.db"

# ── Connection PRAGMAs ────────────────────────────────────────────────────
_BUSY_TIMEOUT_MS = 30_000

# Applied once per process (WAL mode is persistent on the file once set, but
# checkpoint thresholds and synchronous level are per-connection).
def _apply_runtime_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")


def get_connection() -> sqlite3.Connection:
    """Open a connection with the runtime PRAGMAs always applied."""
    conn = sqlite3.connect(DB_PATH, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    _apply_runtime_pragmas(conn)
    return conn


# ── Retry-on-busy decorator ───────────────────────────────────────────────
F = TypeVar("F", bound=Callable)


def retry_on_busy(max_attempts: int = 5, backoff_base: float = 0.1) -> Callable[[F], F]:
    """
    Retry a function on sqlite3.OperationalError 'database is locked'.

    busy_timeout=30s handles 99% of contention internally; this decorator is
    the last-resort net for the rare case where the timeout actually fires
    (e.g. during a long checkpoint or backup).
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    if "locked" not in msg and "busy" not in msg:
                        raise
                    last_exc = e
                    sleep_for = backoff_base * (2 ** attempt)
                    logger.warning(
                        f"[db] {fn.__name__} hit lock on attempt {attempt+1}/{max_attempts} — "
                        f"retrying in {sleep_for:.2f}s"
                    )
                    time.sleep(sleep_for)
            logger.error(f"[db] {fn.__name__} gave up after {max_attempts} attempts")
            raise last_exc  # type: ignore[misc]
        return wrapper
    return deco


@retry_on_busy()
def prune_old_data(days_to_keep: int = 90):
    """
    Delete old rows + reclaim space via incremental_vacuum.

    Uses PRAGMA incremental_vacuum (works in WAL without an exclusive lock)
    instead of full VACUUM, which would block all writers for seconds.
    """
    with get_connection() as conn:
        deleted = conn.execute(
            "DELETE FROM scan_results WHERE scanned_at < datetime('now', ? || ' days')",
            (f"-{days_to_keep}",)
        ).rowcount
        conn.execute(
            "DELETE FROM scan_runs WHERE run_at < datetime('now', ? || ' days')"
            " AND id NOT IN (SELECT DISTINCT run_id FROM scan_results WHERE run_id IS NOT NULL)",
            (f"-{days_to_keep}",)
        )
        conn.execute(
            "DELETE FROM scan_jobs WHERE created_at < datetime('now', '-30 days')"
        )

    if deleted > 0:
        # incremental_vacuum requires autocommit; safe in WAL mode and
        # does not take an exclusive lock (auto_vacuum must be INCREMENTAL)
        vac_conn = sqlite3.connect(DB_PATH, isolation_level=None)
        try:
            vac_conn.execute("PRAGMA incremental_vacuum(1000)")
        except sqlite3.OperationalError as e:
            logger.debug(f"[db] incremental_vacuum skipped: {e}")
        finally:
            vac_conn.close()
        logger.info(f"Pruned {deleted} old scan results (>{days_to_keep} days)")


def _migrate(conn: sqlite3.Connection):
    """Add new columns to existing tables without breaking existing data."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(watchlist)")}
    for col, definition in [
        ("price_above",      "REAL"),
        ("price_below",      "REAL"),
        ("price_target",     "REAL"),    # exact price target — monitored every N minutes
        ("volume_spike_x",   "REAL DEFAULT 0"),    # 0=disabled, >0 = X× avg-volume threshold
        ("supertrend_alert", "INTEGER DEFAULT 0"),  # 0=disabled, 1=enabled (ATR10, mult3.0)
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE watchlist ADD COLUMN {col} {definition}")
            logger.info(f"Migrated watchlist: added column {col}")

    fs_cols = {row[1] for row in conn.execute("PRAGMA table_info(forward_signals)")}
    if "data_quality_flag" not in fs_cols:
        conn.execute("ALTER TABLE forward_signals ADD COLUMN data_quality_flag TEXT")
        logger.info("Migrated forward_signals: added column data_quality_flag")
    for col, definition in [
        ("fill_price",  "REAL"),
        ("fill_source", "TEXT"),
    ]:
        if col not in fs_cols:
            conn.execute(f"ALTER TABLE forward_signals ADD COLUMN {col} {definition}")
            logger.info(f"Migrated forward_signals: added column {col}")

    # telegram_command_state — persists getUpdates offset across restarts
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_command_state (
            key          TEXT PRIMARY KEY,
            value        INTEGER NOT NULL
        )
    """)



_PERSISTENT_PRAGMAS_DONE = False


def _apply_persistent_pragmas_once():
    """
    PRAGMAs that persist with the DB file (journal_mode, auto_vacuum) or
    only need to be set once per process at startup (wal_autocheckpoint).

    Must run BEFORE any user-data writes happen. auto_vacuum cannot be
    changed once tables contain data without a full VACUUM, so we attempt
    to set it and tolerate "cannot change" errors silently — existing DBs
    keep their current setting; new DBs get INCREMENTAL.
    """
    global _PERSISTENT_PRAGMAS_DONE
    if _PERSISTENT_PRAGMAS_DONE:
        return

    # Use autocommit specifically for PRAGMAs that must run outside a transaction.
    pragma_conn = sqlite3.connect(DB_PATH, isolation_level=None)
    try:
        # journal_mode is persistent in the file once set
        result = pragma_conn.execute("PRAGMA journal_mode=WAL").fetchone()
        mode = result[0] if result else "unknown"
        logger.info(f"[db] journal_mode={mode}")

        # auto_vacuum requires VACUUM if changing on a non-empty DB; best-effort
        try:
            pragma_conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        except sqlite3.OperationalError as e:
            logger.debug(f"[db] auto_vacuum unchanged: {e}")

        pragma_conn.execute("PRAGMA wal_autocheckpoint=4000")
    finally:
        pragma_conn.close()

    _PERSISTENT_PRAGMAS_DONE = True


def init_db():
    """Create tables if they don't exist, run migrations."""
    _apply_persistent_pragmas_once()
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker       TEXT NOT NULL UNIQUE,
                added_at     TEXT NOT NULL,
                notes        TEXT,
                alert_score  INTEGER DEFAULT 60,
                alert_pct    REAL DEFAULT 5.0,
                price_above  REAL,
                price_below  REAL,
                price_target REAL,
                list_type    TEXT NOT NULL DEFAULT 'watch'
            );

            CREATE TABLE IF NOT EXISTS portfolio (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker       TEXT NOT NULL UNIQUE,
                added_at     TEXT NOT NULL,
                entry_price  REAL NOT NULL,
                shares       REAL DEFAULT 0,
                notes        TEXT,
                stop_loss    REAL,
                target_price REAL
            );

            CREATE TABLE IF NOT EXISTS watchlist_alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker      TEXT NOT NULL,
                alert_type  TEXT NOT NULL,
                message     TEXT,
                sent_at     TEXT NOT NULL,
                score       REAL,
                price       REAL
            );

            CREATE TABLE IF NOT EXISTS scan_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at        TEXT NOT NULL,
                scan_type     TEXT NOT NULL DEFAULT 'manual',
                total_scanned INTEGER DEFAULT 0,
                notes         TEXT
            );

            CREATE TABLE IF NOT EXISTS scan_results (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id            INTEGER NOT NULL REFERENCES scan_runs(id),
                ticker            TEXT NOT NULL,
                scanned_at        TEXT NOT NULL,
                price             REAL,
                explosion_score   REAL,
                recommendation    TEXT,
                confidence        TEXT,
                rsi               REAL,
                macd_signal       TEXT,
                ma_trend          TEXT,
                pattern_sentiment TEXT,
                bullish_score     INTEGER,
                bearish_score     INTEGER,
                fundamental_score REAL,
                catalyst          TEXT,
                raw_data          TEXT
            );

            CREATE TABLE IF NOT EXISTS alerts_sent (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker     TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                sent_at    TEXT NOT NULL,
                score      REAL,
                message    TEXT
            );

            CREATE TABLE IF NOT EXISTS scan_jobs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending',
                params     TEXT NOT NULL,
                run_id     INTEGER,
                done       INTEGER DEFAULT 0,
                total      INTEGER DEFAULT 0,
                error      TEXT
            );

            CREATE TABLE IF NOT EXISTS news_seen (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                headline_key   TEXT NOT NULL UNIQUE,
                ticker         TEXT NOT NULL DEFAULT '',
                seen_at        TEXT NOT NULL,
                catalyst_score INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_news_seen_key ON news_seen(headline_key);
            CREATE INDEX IF NOT EXISTS idx_news_seen_at  ON news_seen(seen_at);

            CREATE TABLE IF NOT EXISTS alert_trades (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker           TEXT NOT NULL,
                entry_alert_type TEXT NOT NULL,
                entry_price      REAL NOT NULL,
                entry_time       TEXT NOT NULL,
                hold_days_min    INTEGER,
                hold_days_max    INTEGER,
                exit_price       REAL,
                exit_time        TEXT,
                exit_reason      TEXT,
                exit_alert_type  TEXT,
                pnl_pct          REAL,
                status           TEXT DEFAULT 'open'
            );
            CREATE INDEX IF NOT EXISTS idx_alert_trades_ticker ON alert_trades(ticker);
            CREATE INDEX IF NOT EXISTS idx_alert_trades_status ON alert_trades(status);

            CREATE TABLE IF NOT EXISTS forward_signals (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker             TEXT NOT NULL,
                signal_ts          TEXT NOT NULL,
                signal_type        TEXT NOT NULL,
                entry_price        REAL NOT NULL,
                composite_score    REAL,
                catalyst_summary   TEXT,
                supertrend_level   REAL,
                supertrend_atr     REAL,
                ai_verdict         TEXT,
                telegram_sent_at   TEXT,
                price_after_7d     REAL,
                price_after_14d    REAL,
                price_after_30d    REAL,
                return_7d_pct      REAL,
                return_14d_pct     REAL,
                return_30d_pct     REAL,
                status             TEXT NOT NULL DEFAULT 'open'
            );
            CREATE INDEX IF NOT EXISTS idx_forward_ticker ON forward_signals(ticker);
            CREATE INDEX IF NOT EXISTS idx_forward_status ON forward_signals(status);
            CREATE INDEX IF NOT EXISTS idx_forward_signal_ts ON forward_signals(signal_ts);

            CREATE TABLE IF NOT EXISTS monitoring_queue_snapshot (
                ticker   TEXT NOT NULL,
                saved_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ibkr_positions (
                ticker          TEXT PRIMARY KEY,
                shares          REAL,
                avg_cost        REAL,
                unrealized_pnl  REAL,
                market_value    REAL,
                last_synced     TEXT
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                date            TEXT PRIMARY KEY,
                day_pnl         REAL,
                net_liquidation REAL,
                recorded_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS order_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker        TEXT NOT NULL,
                action        TEXT NOT NULL,
                shares        INTEGER NOT NULL,
                entry_price   REAL NOT NULL,
                stop_price    REAL NOT NULL,
                target_price  REAL NOT NULL,
                status        TEXT NOT NULL,
                fill_price    REAL,
                ibkr_order_id INTEGER,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                notes         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_order_log_ticker ON order_log(ticker);
            CREATE INDEX IF NOT EXISTS idx_order_log_status ON order_log(status);

            CREATE INDEX IF NOT EXISTS idx_results_ticker     ON scan_results(ticker);
            CREATE INDEX IF NOT EXISTS idx_results_scanned_at ON scan_results(scanned_at);
        """)
        _migrate(conn)

    # Opportunity tracker table (separate module to avoid circular imports)
    try:
        from src.opportunity_tracker import ensure_table as _ensure_opp_table
        _ensure_opp_table()
    except Exception as _e:
        logger.warning(f"[db] opportunity_tracker table init skipped: {_e}")

    logger.info(f"Database initialized at {DB_PATH}")


def save_scan_run(scan_type: str = "manual", total_scanned: int = 0, notes: str = "") -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO scan_runs (run_at, scan_type, total_scanned, notes) VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(), scan_type, total_scanned, notes)
        )
        return cursor.lastrowid


@retry_on_busy()
def save_result(run_id: int, result: Dict[str, Any]):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO scan_results (
                run_id, ticker, scanned_at, price, explosion_score,
                recommendation, confidence, rsi, macd_signal, ma_trend,
                pattern_sentiment, bullish_score, bearish_score,
                fundamental_score, catalyst, raw_data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            result.get("ticker", ""),
            datetime.now().isoformat(),
            result.get("price"),
            result.get("explosion_score") or result.get("score"),
            result.get("recommendation"),
            result.get("confidence"),
            result.get("rsi"),
            result.get("macd_signal") or result.get("macd"),
            result.get("ma_trend"),
            result.get("pattern_sentiment"),
            result.get("bullish_score"),
            result.get("bearish_score"),
            result.get("fundamental_score"),
            result.get("catalyst"),
            json.dumps(result)
        ))


def save_alert(ticker: str, alert_type: str, score: float, message: str):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO alerts_sent (ticker, alert_type, sent_at, score, message) VALUES (?, ?, ?, ?, ?)",
            (ticker, alert_type, datetime.now().isoformat(), score, message)
        )


def get_ticker_history(ticker: str, limit: int = 30) -> List[Dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM scan_results WHERE ticker = ?
            ORDER BY scanned_at DESC LIMIT ?
        """, (ticker, limit)).fetchall()
        return [dict(r) for r in rows]


def get_latest_scan(limit: int = 50) -> List[Dict]:
    with get_connection() as conn:
        run = conn.execute(
            "SELECT id FROM scan_runs ORDER BY run_at DESC LIMIT 1"
        ).fetchone()
        if not run:
            return []
        rows = conn.execute("""
            SELECT * FROM scan_results WHERE run_id = ?
            ORDER BY explosion_score DESC LIMIT ?
        """, (run["id"], limit)).fetchall()
        return [dict(r) for r in rows]


def get_top_tickers(days: int = 7, min_score: float = 70.0, limit: int = 20) -> List[Dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ticker, COUNT(*) AS appearances,
                   AVG(explosion_score) AS avg_score, MAX(explosion_score) AS max_score,
                   MAX(scanned_at) AS last_seen, MAX(price) AS last_price
            FROM scan_results
            WHERE scanned_at >= datetime('now', ? || ' days') AND explosion_score >= ?
            GROUP BY ticker ORDER BY appearances DESC, avg_score DESC LIMIT ?
        """, (f"-{days}", min_score, limit)).fetchall()
        return [dict(r) for r in rows]


def get_score_trend(ticker: str, limit: int = 14) -> List[Dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT scanned_at, explosion_score, price, recommendation
            FROM scan_results WHERE ticker = ?
            ORDER BY scanned_at DESC LIMIT ?
        """, (ticker, limit)).fetchall()
        return [dict(r) for r in rows]


def get_recent_scan_scores(ticker: str, limit: int = 3) -> list:
    """Return the last N explosion_scores for ticker, newest first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT explosion_score FROM scan_results WHERE ticker = ? "
            "ORDER BY scanned_at DESC LIMIT ?",
            (ticker, limit)
        ).fetchall()
        return [row[0] for row in rows if row[0] is not None]


def get_last_saved_score(ticker: str) -> Optional[float]:
    """Return the most recently saved explosion_score for ticker, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT explosion_score FROM scan_results WHERE ticker = ? "
            "ORDER BY scanned_at DESC LIMIT 1",
            (ticker,)
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None


def get_db_stats() -> Dict:
    with get_connection() as conn:
        return {
            "total_runs":     conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0],
            "total_results":  conn.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0],
            "total_alerts":   conn.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0],
            "unique_tickers": conn.execute("SELECT COUNT(DISTINCT ticker) FROM scan_results").fetchone()[0],
        }


# ── Scan Jobs ──────────────────────────────────────────────────────────────────

def create_scan_job(params: dict) -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO scan_jobs (created_at, status, params) VALUES (?, 'pending', ?)",
            (datetime.now().isoformat(), json.dumps(params))
        )
        return cursor.lastrowid


def get_scan_job(job_id: int) -> Optional[Dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def get_latest_scan_job() -> Optional[Dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM scan_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def update_scan_job(job_id: int, **kwargs):
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [job_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE scan_jobs SET {fields} WHERE id = ?", values)


# ── Watchlist ──────────────────────────────────────────────────────────────────

def watchlist_add(ticker: str, notes: str = "", alert_score: int = 60,
                  alert_pct: float = 5.0, price_above: float = None,
                  price_below: float = None, price_target: float = None,
                  volume_spike_x: float = 0, supertrend_alert: int = 0):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist "
            "(ticker, added_at, notes, alert_score, alert_pct, price_above, price_below, price_target, volume_spike_x, supertrend_alert) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ticker.upper(), datetime.now().isoformat(), notes,
             alert_score, alert_pct, price_above, price_below, price_target,
             volume_spike_x, supertrend_alert)
        )


def watchlist_remove(ticker: str):
    with get_connection() as conn:
        conn.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker.upper(),))


def watchlist_get_all() -> List[Dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM watchlist ORDER BY added_at DESC").fetchall()
        return [dict(r) for r in rows]


def watchlist_update(ticker: str, **kwargs):
    if not kwargs:
        return
    fields = [f"{k} = ?" for k in kwargs]
    vals   = list(kwargs.values()) + [ticker.upper()]
    with get_connection() as conn:
        conn.execute(f"UPDATE watchlist SET {', '.join(fields)} WHERE ticker = ?", vals)


@retry_on_busy()
def watchlist_save_alert(ticker: str, alert_type: str, message: str,
                         score: float = None, price: float = None):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO watchlist_alerts (ticker, alert_type, message, sent_at, score, price) "
            "VALUES (?,?,?,?,?,?)",
            (ticker.upper(), alert_type, message, datetime.now().isoformat(), score, price)
        )


def watchlist_get_alerts(ticker: str = None, limit: int = 50) -> List[Dict]:
    with get_connection() as conn:
        if ticker:
            rows = conn.execute(
                "SELECT * FROM watchlist_alerts WHERE ticker = ? ORDER BY sent_at DESC LIMIT ?",
                (ticker.upper(), limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM watchlist_alerts ORDER BY sent_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


# ── Portfolio ──────────────────────────────────────────────────────────────────

def portfolio_add(ticker: str, entry_price: float, shares: float = 0,
                  notes: str = "", stop_loss: float = None, target_price: float = None):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO portfolio "
            "(ticker, added_at, entry_price, shares, notes, stop_loss, target_price) "
            "VALUES (?,?,?,?,?,?,?)",
            (ticker.upper(), datetime.now().isoformat(),
             entry_price, shares, notes, stop_loss, target_price)
        )


def portfolio_remove(ticker: str):
    with get_connection() as conn:
        conn.execute("DELETE FROM portfolio WHERE ticker = ?", (ticker.upper(),))


def portfolio_get_all() -> List[Dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM portfolio ORDER BY added_at DESC").fetchall()
        return [dict(r) for r in rows]


def portfolio_update(ticker: str, **kwargs):
    fields = [f"{k} = ?" for k in kwargs]
    vals   = list(kwargs.values()) + [ticker.upper()]
    if not fields:
        return
    with get_connection() as conn:
        conn.execute(f"UPDATE portfolio SET {', '.join(fields)} WHERE ticker = ?", vals)


# ── News Catalyst Monitor ─────────────────────────────────────────────────────

def news_seen_contains(headline_key: str) -> bool:
    """Returns True if this headline was already processed."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM news_seen WHERE headline_key = ?", (headline_key,)
        ).fetchone()
        return row is not None


def news_seen_add(headline_key: str, ticker: str, catalyst_score: int):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO news_seen (headline_key, ticker, seen_at, catalyst_score) "
            "VALUES (?, ?, ?, ?)",
            (headline_key, ticker, datetime.now().isoformat(), catalyst_score)
        )


def news_seen_cleanup(days: int = 7):
    """Remove old seen headlines to keep DB lean."""
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM news_seen WHERE seen_at < datetime('now', ? || ' days')",
            (f"-{days}",)
        )


# ── Alert Trades Backtest ──────────────────────────────────────────────────────

def alert_trade_open(
    ticker: str,
    entry_alert_type: str,
    entry_price: float,
    hold_days_min: int,
    hold_days_max: int,
) -> int:
    """Record a new open trade triggered by a BUY alert. Returns new row id."""
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO alert_trades
               (ticker, entry_alert_type, entry_price, entry_time, hold_days_min, hold_days_max, status)
               VALUES (?, ?, ?, ?, ?, ?, 'open')""",
            (ticker, entry_alert_type, entry_price, datetime.now().isoformat(), hold_days_min, hold_days_max),
        )
        return cursor.lastrowid


def alert_trade_close(
    ticker: str,
    exit_alert_type: str,
    exit_price: float,
    exit_reason: str,
    trade_id: int | None = None,
) -> bool:
    """
    Close the most recent open trade for ticker (or a specific trade_id).
    Calculates P&L and sets status='closed'. Returns True if a trade was found and closed.
    """
    with get_connection() as conn:
        if trade_id:
            row = conn.execute(
                "SELECT id, entry_price FROM alert_trades WHERE id=? AND status='open'",
                (trade_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, entry_price FROM alert_trades WHERE ticker=? AND status='open' ORDER BY entry_time DESC LIMIT 1",
                (ticker,)
            ).fetchone()
        if not row:
            return False
        pnl_pct = (exit_price - row["entry_price"]) / row["entry_price"] * 100 if row["entry_price"] else 0
        conn.execute(
            """UPDATE alert_trades
               SET exit_price=?, exit_time=?, exit_reason=?, exit_alert_type=?, pnl_pct=?, status='closed'
               WHERE id=?""",
            (exit_price, datetime.now().isoformat(), exit_reason, exit_alert_type, round(pnl_pct, 2), row["id"]),
        )
        return True


def alert_trade_get_open() -> List[Dict]:
    """Return all open trades ordered by entry_time ascending (oldest first)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM alert_trades WHERE status='open' ORDER BY entry_time ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def alert_trade_get_all(limit: int = 200) -> List[Dict]:
    """Return all trades (open + closed) sorted by entry_time desc."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM alert_trades ORDER BY entry_time DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    stats = get_db_stats()
    print(f"DB ready | runs={stats['total_runs']} | results={stats['total_results']} | tickers={stats['unique_tickers']}")
