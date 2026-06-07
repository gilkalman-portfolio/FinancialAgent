"""
Monitoring Queue — decides which tickers deserve real-time Supertrend
monitoring via IBKR.

Two sources feed the queue (per user choice "שניהם"):

  A. SCANNER  — every ticker with composite_score >= SCANNER_MIN_SCORE
                from the most recent scan run.
  B. MANUAL   — explicit watchlist entries flagged as monitored (any
                ticker in the existing `watchlist` table).

A ticker enters the queue when at least one of these is true, AND it
passes the liquidity gate (avg daily $ volume >= $5M, reusing the
threshold from execution_engine.py).

This module is the SOURCE OF TRUTH for "which tickers should the
IBKR real-time loop subscribe to?".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import yfinance as yf

from src.database import get_connection
from src.execution_engine import _MIN_DAILY_DOLLAR_VOL
from src.hysteresis import passes_hysteresis, LIQUIDITY_ADV_ENTRY, LIQUIDITY_ADV_EXIT

logger = logging.getLogger(__name__)

SCANNER_MIN_SCORE = 65
SCANNER_LOOKBACK_HOURS = 24

# Module-level snapshot of the previously-accepted queue. Powers liquidity
# hysteresis: a ticker already in the queue gets the looser $3M exit threshold,
# while new entrants must clear the $5M entry threshold.
#
# Persisted to `monitoring_queue_snapshot` DB table so the accepted set
# survives process restarts and is visible to the IBKR worker process.
_previous_queue: set[str] = set()
_previous_queue_loaded = False


@dataclass(frozen=True)
class QueueEntry:
    ticker: str
    source: str               # "scanner" | "manual"
    composite_score: Optional[float]
    last_seen: str            # iso timestamp


def _load_previous_queue() -> set[str]:
    """Restore the accepted ticker set from DB on first call."""
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT ticker FROM monitoring_queue_snapshot"
            ).fetchall()
        return {r["ticker"] for r in rows}
    except Exception as e:
        logger.warning(f"[queue] failed to load snapshot: {e}")
        return set()


def _persist_queue(tickers: set[str]) -> None:
    """Replace the snapshot table with the current accepted set."""
    now = datetime.now().isoformat()
    try:
        with get_connection() as conn:
            conn.execute("DELETE FROM monitoring_queue_snapshot")
            if tickers:
                conn.executemany(
                    "INSERT INTO monitoring_queue_snapshot (ticker, saved_at) VALUES (?, ?)",
                    [(tk, now) for tk in tickers],
                )
    except Exception as e:
        logger.warning(f"[queue] failed to persist snapshot: {e}")


def _liquid(ticker: str, already_in_queue: bool = False) -> bool:
    """Hysteresis liquidity gate. Enter at >= $5M ADV, only drop when < $3M.

    `already_in_queue` lets tickers already being monitored keep their slot
    through a small dip in volume rather than churning in/out at the boundary.
    """
    try:
        hist = yf.Ticker(ticker).history(period="1mo", auto_adjust=False)
        if hist is None or hist.empty:
            return False
        adv = float((hist["Close"] * hist["Volume"]).tail(20).mean())
        return passes_hysteresis(adv, already_in_queue,
                                 LIQUIDITY_ADV_ENTRY, LIQUIDITY_ADV_EXIT)
    except Exception as e:
        logger.warning(f"[queue] liquidity check failed for {ticker}: {e}")
        return False


def _scanner_tickers(min_score: int = SCANNER_MIN_SCORE) -> list[tuple[str, float, str]]:
    """Recent scan results whose explosion_score >= min_score."""
    cutoff = (datetime.now() - timedelta(hours=SCANNER_LOOKBACK_HOURS)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT ticker, MAX(explosion_score) AS score, MAX(scanned_at) AS last_seen
            FROM scan_results
            WHERE scanned_at >= ? AND explosion_score >= ?
            GROUP BY ticker
            ORDER BY score DESC
            """,
            (cutoff, min_score),
        ).fetchall()
    return [(r["ticker"], float(r["score"]), r["last_seen"]) for r in rows]


def _manual_tickers() -> list[tuple[str, str]]:
    """Tickers from watchlist (entire list — watchlist IS the manual queue)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ticker, added_at FROM watchlist ORDER BY added_at DESC"
        ).fetchall()
    return [(r["ticker"], r["added_at"]) for r in rows]


def _recent_buy_tickers(hours: int = 72) -> list[tuple[str, str]]:
    """Tickers with a combined_buy alert in the last `hours` hours.

    Bypasses SCANNER_MIN_SCORE so that tickers which dropped below 65 but
    are still in the combiner's hold-band (composite >= 50) remain in the
    monitoring queue.  Fixes the "queue cliff" caveat (Gap I-1).
    """
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ticker, MAX(sent_at) AS last_seen "
            "FROM watchlist_alerts "
            "WHERE alert_type = 'combined_buy' AND sent_at >= ? "
            "GROUP BY ticker",
            (cutoff,),
        ).fetchall()
    return [(r["ticker"], r["last_seen"]) for r in rows]


def build_queue(min_score: int = SCANNER_MIN_SCORE, apply_liquidity_gate: bool = True) -> list[QueueEntry]:
    """
    Build the monitoring queue from scanner + manual sources.
    Liquidity-gate is applied unless caller opts out (e.g. for testing).
    """
    global _previous_queue, _previous_queue_loaded
    if not _previous_queue_loaded:
        _previous_queue = _load_previous_queue()
        _previous_queue_loaded = True
        logger.info(f"[queue] restored {len(_previous_queue)} tickers from snapshot")

    entries: dict[str, QueueEntry] = {}

    for tk, score, ts in _scanner_tickers(min_score=min_score):
        entries[tk] = QueueEntry(ticker=tk, source="scanner", composite_score=score, last_seen=ts)

    for tk, ts in _manual_tickers():
        if tk not in entries:
            entries[tk] = QueueEntry(ticker=tk, source="manual", composite_score=None, last_seen=ts)

    for tk, ts in _recent_buy_tickers():
        if tk not in entries:
            entries[tk] = QueueEntry(ticker=tk, source="recent_buy", composite_score=None, last_seen=ts)

    if apply_liquidity_gate:
        before = len(entries)
        entries = {
            tk: e for tk, e in entries.items()
            if _liquid(tk, already_in_queue=(tk in _previous_queue))
        }
        dropped = before - len(entries)
        if dropped:
            logger.info(f"[queue] dropped {dropped} ticker(s) below ${_MIN_DAILY_DOLLAR_VOL/1e6:.0f}M ADV")

    result = sorted(
        entries.values(),
        key=lambda e: (e.composite_score is None, -(e.composite_score or 0), e.ticker),
    )
    # Update snapshot for next call's hysteresis decisions.
    # Only persist to DB when the liquidity gate was applied — unfiltered calls
    # (e.g. from signal_combiner.evaluate()) must not overwrite the snapshot
    # with non-gated tickers and corrupt hysteresis state.
    _previous_queue = {e.ticker for e in result}
    if apply_liquidity_gate:
        _persist_queue(_previous_queue)
        logger.info(f"[queue] built {len(result)} entries (scanner+manual+recent_buy after liquidity gate)")
    else:
        logger.debug(f"[queue] built {len(result)} entries (no liquidity gate — snapshot not updated)")
    return result


def queue_as_tickers(min_score: int = SCANNER_MIN_SCORE) -> list[str]:
    """Plain ticker list — convenience for the IBKR subscribe loop."""
    return [e.ticker for e in build_queue(min_score=min_score)]
