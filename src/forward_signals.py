"""
Forward Signals — record every alert sent and measure its outcome forward
in time. This is the "honest backtest" channel: every signal is captured at
generation time with point-in-time data, then measured against actual
future prices at 7/14/30-day horizons.

Public API:
    record_signal(...)         insert a new signal row, return id
    update_outcomes()          fill price_after_Xd for matured signals
    weekly_digest(days=7)      return aggregate metrics for last N days
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import yfinance as yf

from src.database import get_connection, retry_on_busy

logger = logging.getLogger(__name__)

OPEN = "open"
MATURED = "matured"
# 1/2/3d were added for the News-Catalyst Event-Study Measurement (WATCH
# signals, see record_watch_signals() below) — short horizons a news-driven
# move can reverse within, per Tetlock (2007, JF). Applied uniformly to every
# signal_type here (not just WATCH) rather than special-cased, so an existing
# open BUY/SELL row simply picks up two more (harmless, informational) data
# points on its next update_outcomes() pass instead of the loop branching on
# signal_type. See CLAUDE.md.
HORIZONS_DAYS = (1, 2, 3, 7, 14, 30)
# WATCH rows are a research/measurement observation, not a trade signal
# (signal_type is one of BUY / SELL / WATCH — see SignalRecord below). They
# must be excluded from every trading-relevant aggregate or "last signal"
# lookup (see the `signal_type IN ('BUY', 'SELL')` filters in record_fill(),
# weekly_digest(), and _llm_curation_comparison() below, plus
# telegram_command_handler.py's /status handler) — with WATCH now written at
# full-scanner-hit frequency (hundreds/cycle) they would otherwise dominate
# any unfiltered "most recent forward_signals row" query.


@dataclass
class SignalRecord:
    ticker: str
    signal_type: str            # BUY / SELL / WATCH
    entry_price: float
    composite_score: Optional[float] = None
    catalyst_summary: Optional[str] = None
    supertrend_level: Optional[float] = None
    supertrend_atr: Optional[float] = None
    ai_verdict: Optional[str] = None
    telegram_sent_at: Optional[str] = None
    news_publisher: Optional[str] = None
    news_age_minutes: Optional[float] = None


def _check_entry_price_plausibility(ticker: str, entry_price: float) -> str | None:
    """Compare entry_price against recent scan_results.price.

    Returns 'SUSPECT' if the price looks implausible (>20% divergence or
    exactly 105.0 — a known IBKR paper-account placeholder), else None.
    """
    if entry_price == 105.0:
        logger.warning(
            f"[forward_signals] {ticker} entry_price=105.0 — known IBKR placeholder"
        )
        return "SUSPECT"

    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT price FROM scan_results "
                "WHERE ticker = ? AND scanned_at >= ? AND price IS NOT NULL "
                "ORDER BY scanned_at DESC LIMIT 1",
                (ticker, cutoff),
            ).fetchone()
        if row and row["price"]:
            scan_price = float(row["price"])
            if scan_price > 0:
                divergence = abs(entry_price - scan_price) / scan_price
                if divergence > 0.20:
                    logger.warning(
                        f"[forward_signals] {ticker} entry_price={entry_price:.2f} "
                        f"diverges {divergence:.0%} from scan price {scan_price:.2f}"
                    )
                    return "SUSPECT"
    except Exception as e:
        logger.warning(f"[forward_signals] plausibility check failed for {ticker}: {e}")
    return None


@retry_on_busy()
def record_signal(rec: SignalRecord) -> int:
    """Insert a new forward_signals row. Returns the new id."""
    now = datetime.now().isoformat()
    quality_flag = _check_entry_price_plausibility(rec.ticker, rec.entry_price)
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO forward_signals (
                ticker, signal_ts, signal_type, entry_price, composite_score,
                catalyst_summary, supertrend_level, supertrend_atr, ai_verdict,
                telegram_sent_at, status, data_quality_flag,
                news_publisher, news_age_minutes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.ticker,
                now,
                rec.signal_type,
                rec.entry_price,
                rec.composite_score,
                rec.catalyst_summary,
                rec.supertrend_level,
                rec.supertrend_atr,
                rec.ai_verdict,
                rec.telegram_sent_at or now,
                OPEN,
                quality_flag,
                rec.news_publisher,
                rec.news_age_minutes,
            ),
        )
        signal_id = cur.lastrowid
    logger.info(
        f"[forward_signals] recorded {rec.signal_type} {rec.ticker} id={signal_id}"
        + (f" quality={quality_flag}" if quality_flag else "")
    )
    return signal_id


@retry_on_busy()
def record_fill(ticker: str, actual_fill_price: float, ibkr_order_id: int) -> bool:
    """Update the most recent forward_signal for *ticker* with the real fill price.

    Targets the newest row where fill_price IS NULL and data_quality_flag != 'SUSPECT'.
    Returns True if a row was updated.

    signal_type IN ('BUY','SELL') is required, not incidental: only a real
    trade produces an IBKR fill callback, so the row this fill belongs to is
    always a BUY/SELL row. Without this filter, a WATCH row recorded minutes
    earlier for the same ticker (momentum/supertrend scan every 30 min, no
    relation to order placement) could be newer than the real BUY/SELL row
    and would wrongly receive the fill price instead of it.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id FROM forward_signals
            WHERE ticker = ?
              AND signal_type IN ('BUY', 'SELL')
              AND (data_quality_flag IS NULL OR data_quality_flag != 'SUSPECT')
              AND fill_price IS NULL
            ORDER BY signal_ts DESC LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        if row is None:
            logger.warning(
                f"[forward_signals] record_fill: no eligible row for {ticker} "
                f"(order_id={ibkr_order_id})"
            )
            return False
        # Guard: don't record a fill if the order was CANCELLED in order_log.
        # Bracket order race — cancel callback on a child leg can arrive after
        # the parent fill, leaving order_log CANCELLED while the fill was real.
        # We only skip on explicit CANCELLED; missing rows proceed normally.
        order_row = conn.execute(
            "SELECT status FROM order_log WHERE ibkr_order_id = ? LIMIT 1",
            (ibkr_order_id,),
        ).fetchone()
        if order_row and order_row["status"] == "CANCELLED":
            logger.warning(
                f"[forward_signals] record_fill: order {ibkr_order_id} is CANCELLED"
                f" — skipping fill write for {ticker}"
            )
            return False
        conn.execute(
            "UPDATE forward_signals SET fill_price = ?, fill_source = ? WHERE id = ?",
            (actual_fill_price, "IBKR_CALLBACK", row["id"]),
        )
    logger.info(
        f"[forward_signals] fill recorded: {ticker} id={row['id']} "
        f"fill_price={actual_fill_price:.2f} order_id={ibkr_order_id}"
    )
    return True


def _fetch_price_at(ticker: str, target_dt: datetime) -> Optional[float]:
    """Closing price on target_dt (or the next available trading day).

    Uses auto_adjust=True — same basis as entry_price (a live/real-time price at
    signal time). A split/dividend within the 7/14/30d horizon would otherwise
    corrupt the computed return by comparing a raw historical close to a live
    price (same bug class fixed in src/backtester.py's _get_price_at_date()).
    """
    start = target_dt.date()
    end = (target_dt + timedelta(days=7)).date()
    try:
        hist = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[0])
    except Exception as e:
        logger.warning(f"[forward_signals] price fetch failed for {ticker}@{target_dt}: {e}")
        return None


@retry_on_busy()
def update_outcomes() -> dict:
    """
    Fill price_after_Xd / return_Xd_pct for signals whose horizons have matured.
    A row is marked 'matured' once every horizon in HORIZONS_DAYS is populated.
    Applies uniformly to BUY/SELL/WATCH rows — WATCH rows need the 1/2/3d
    columns backfilled here exactly the same way BUY/SELL rows always got
    7/14/30d backfilled (see HORIZONS_DAYS).
    """
    now = datetime.now()
    stats = {"checked": 0, "filled": 0, "matured": 0}

    # ── Phase 1: read open signals into memory, then close the connection ────
    price_cols = ", ".join(f"price_after_{h}d" for h in HORIZONS_DAYS)
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT id, ticker, signal_ts, entry_price, {price_cols} "
            "FROM forward_signals WHERE status = ?",
            (OPEN,),
        ).fetchall()

    # ── Phase 2: do all yfinance lookups OUTSIDE any DB connection ───────────
    # No DB lock is held during these network calls (can be many seconds each).
    pending_updates: list[tuple[dict, str, int]] = []  # (updates, new_status, row_id)

    for row in rows:
        stats["checked"] += 1
        signal_dt = datetime.fromisoformat(row["signal_ts"])
        updates: dict = {}

        for horizon in HORIZONS_DAYS:
            col_price = f"price_after_{horizon}d"
            col_return = f"return_{horizon}d_pct"

            if row[col_price] is not None:
                continue
            target = signal_dt + timedelta(days=horizon)
            if target > now:
                continue

            price = _fetch_price_at(row["ticker"], target)
            if price is None:
                continue

            ret = ((price - row["entry_price"]) / row["entry_price"]) * 100.0
            updates[col_price] = price
            updates[col_return] = ret

        if not updates:
            continue

        all_filled = all(
            (updates.get(f"price_after_{h}d") is not None) or
            (row[f"price_after_{h}d"] is not None)
            for h in HORIZONS_DAYS
        )
        new_status = MATURED if all_filled else OPEN
        pending_updates.append((updates, new_status, row["id"]))

    # ── Phase 3: single short transaction for ALL updates ────────────────────
    if pending_updates:
        with get_connection() as conn:
            for updates, new_status, row_id in pending_updates:
                set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
                params = list(updates.values()) + [new_status, row_id]
                conn.execute(
                    f"UPDATE forward_signals SET {set_clause}, status = ? WHERE id = ?",
                    params,
                )
                stats["filled"] += 1
                if new_status == MATURED:
                    stats["matured"] += 1

    logger.info(f"[forward_signals] outcomes update: {stats}")
    return stats


def benchmark_excess(lookback_days: int = 90, benchmark: str = "IWM") -> dict | None:
    """BUY-signal return vs the benchmark over identical holding windows.

    Deliberately uses a LONGER lookback than the digest's display window. A
    signal fired in the last 7 days cannot have a matured 7-day outcome, so
    scoping this to the weekly window would measure almost nothing and report a
    t-stat off a handful of rows. The edge question needs accumulated history.

    A long-only signal in a rising tape earns market beta, and a raw win rate
    cannot tell the two apart. Between 2026-05 and 2026-08 this digest reported a
    55.9% win rate while the same signals were measured at -0.25% excess vs IWM
    (t = -0.33) — the number looked like skill and was the market. Excess return
    with an n and a t-stat is the only honest headline.

    Returns None when the benchmark cannot be fetched; callers must degrade to
    the raw numbers rather than silently reporting beta as performance.
    """
    import math

    cutoff = (datetime.now() - timedelta(days=lookback_days)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT signal_ts, return_7d_pct FROM forward_signals "
            "WHERE signal_type = 'BUY' AND return_7d_pct IS NOT NULL AND signal_ts >= ?",
            (cutoff,),
        ).fetchall()
    if not rows:
        return None

    try:
        import yfinance as yf
        import pandas as pd

        start = (datetime.now() - timedelta(days=lookback_days + 45)).strftime("%Y-%m-%d")
        hist = yf.Ticker(benchmark).history(start=start, auto_adjust=True)
        if hist is None or hist.empty:
            return None
        close = hist["Close"]
        # yfinance normally returns a tz-aware index, but not always (and never
        # for a cached/offline frame). tz_localize(None) raises on a naive index,
        # so branch rather than assume.
        idx = pd.DatetimeIndex(close.index)
        close.index = (idx.tz_convert("UTC").tz_localize(None) if idx.tz is not None
                       else idx).normalize()
    except Exception as e:
        logger.warning(f"[forward_signals] benchmark fetch failed: {e}")
        return None

    excess: list[float] = []
    for r in rows:
        try:
            d0 = pd.Timestamp(r["signal_ts"][:10]).normalize()
            i0 = close.index.searchsorted(d0)
            i1 = close.index.searchsorted(d0 + pd.Timedelta(days=7))
            if i0 >= len(close) or i1 >= len(close) or i1 <= i0:
                continue
            bench = (float(close.iloc[i1]) / float(close.iloc[i0]) - 1.0) * 100.0
            excess.append(float(r["return_7d_pct"]) - bench)
        except Exception:
            continue

    n = len(excess)
    if n < 3:
        return None
    mean = sum(excess) / n
    var = sum((x - mean) ** 2 for x in excess) / (n - 1)
    sd = math.sqrt(var)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return {
        "benchmark": benchmark, "n": n, "mean_excess_pct": mean,
        "t_stat": t, "beat_rate_pct": sum(1 for x in excess if x > 0) / n * 100.0,
        "significant": abs(t) > 2.0,
    }


def weekly_digest(days: int = 7) -> dict:
    """Aggregate stats over the last `days` days of BUY/SELL signals.

    WATCH rows (the News-Catalyst Event-Study Measurement, see
    record_watch_signals()) are excluded — they are a research observation,
    not a trade signal, and the win/loss logic below has no meaningful
    direction for them. With WATCH rows now written at full-scanner-hit
    frequency they would otherwise dwarf and corrupt this trading digest.
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT signal_type, return_7d_pct, return_14d_pct, return_30d_pct,
                   composite_score, ticker
            FROM forward_signals
            WHERE signal_ts >= ? AND signal_type IN ('BUY', 'SELL')
            """,
            (cutoff,),
        ).fetchall()

    total = len(rows)
    by_type = {}
    returns_7d, returns_14d, returns_30d = [], [], []
    winners_7d = 0
    measured_7d = 0
    # Direction-aware win counts, broken out by signal type (BUY win: return > 0;
    # SELL win: return < 0 — a SELL is correct when price subsequently falls).
    winners_7d_buy = measured_7d_buy = 0
    winners_7d_sell = measured_7d_sell = 0

    for r in rows:
        t = r["signal_type"]
        by_type[t] = by_type.get(t, 0) + 1
        if r["return_7d_pct"] is not None:
            ret = r["return_7d_pct"]
            returns_7d.append(ret)
            measured_7d += 1
            is_win = (ret < 0) if t == "SELL" else (ret > 0)
            if is_win:
                winners_7d += 1
            if t == "SELL":
                measured_7d_sell += 1
                winners_7d_sell += 1 if is_win else 0
            else:
                measured_7d_buy += 1
                winners_7d_buy += 1 if is_win else 0
        if r["return_14d_pct"] is not None:
            returns_14d.append(r["return_14d_pct"])
        if r["return_30d_pct"] is not None:
            returns_30d.append(r["return_30d_pct"])

    def _avg(xs):
        return sum(xs) / len(xs) if xs else None

    def _rate(winners, measured):
        return (winners / measured * 100.0) if measured else None

    return {
        "window_days": days,
        "total_signals": total,
        "by_type": by_type,
        "avg_return_7d_pct": _avg(returns_7d),
        "avg_return_14d_pct": _avg(returns_14d),
        "avg_return_30d_pct": _avg(returns_30d),
        "win_rate_7d_pct": _rate(winners_7d, measured_7d),
        "measured_7d": measured_7d,
        "win_rate_7d_buy_pct": _rate(winners_7d_buy, measured_7d_buy),
        "measured_7d_buy": measured_7d_buy,
        "win_rate_7d_sell_pct": _rate(winners_7d_sell, measured_7d_sell),
        "measured_7d_sell": measured_7d_sell,
        # None when the benchmark is unavailable — format_digest_message says so
        # explicitly rather than quietly printing an unbenchmarked win rate.
        "excess": _safe_benchmark_excess(),
        # None until llm_universe_curator has produced at least one week of
        # curated data — omitted from the message entirely until then.
        "llm_curation_comparison": _safe_llm_curation_comparison(days),
    }


def _safe_benchmark_excess(lookback_days: int = 90) -> dict | None:
    """benchmark_excess() must never be able to break the weekly digest send."""
    try:
        return benchmark_excess(lookback_days)
    except Exception as e:
        logger.warning(f"[forward_signals] benchmark_excess failed: {e}")
        return None


def _llm_curation_comparison(days: int = 7) -> dict | None:
    """Split recent forward_signals by whether the ticker was in the LLM's
    most recent weekly curated set, to measure whether curation adds
    anything over the quant-only baseline (see src/llm_universe_curator.py).

    Returns None when there is no curation data yet — the feature is off, or
    hasn't produced a week of curated data — so the digest omits this section
    entirely rather than showing an empty or meaningless comparison. Never
    raises; call through _safe_llm_curation_comparison().
    """
    with get_connection() as conn:
        week_row = conn.execute(
            "SELECT MAX(week_of) AS w FROM llm_curated_universe"
        ).fetchone()
        if not week_row or not week_row["w"]:
            return None
        curated = {
            r["ticker"] for r in conn.execute(
                "SELECT ticker FROM llm_curated_universe "
                "WHERE week_of = ? AND action IN ('keep','add')",
                (week_row["w"],),
            ).fetchall()
        }
        if not curated:
            return None

        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        # signal_type filter for the same reason as weekly_digest() above —
        # WATCH rows are not trade signals and must not enter this comparison.
        rows = conn.execute(
            "SELECT ticker, signal_type, return_7d_pct FROM forward_signals "
            "WHERE signal_ts >= ? AND return_7d_pct IS NOT NULL "
            "AND signal_type IN ('BUY', 'SELL')",
            (cutoff,),
        ).fetchall()

    if not rows:
        return None

    def _group_stats(group_rows):
        rets, wins, measured = [], 0, 0
        for r in group_rows:
            ret = r["return_7d_pct"]
            rets.append(ret)
            measured += 1
            is_win = (ret < 0) if r["signal_type"] == "SELL" else (ret > 0)
            wins += 1 if is_win else 0
        return {
            "measured": measured,
            "avg_return_pct": (sum(rets) / len(rets)) if rets else None,
            "win_rate_pct": (wins / measured * 100.0) if measured else None,
        }

    curated_rows = [r for r in rows if r["ticker"] in curated]
    other_rows = [r for r in rows if r["ticker"] not in curated]
    if not curated_rows and not other_rows:
        return None

    return {"llm_curated": _group_stats(curated_rows), "quant_only": _group_stats(other_rows)}


def _safe_llm_curation_comparison(days: int = 7) -> dict | None:
    """_llm_curation_comparison() must never be able to break the weekly digest send."""
    try:
        return _llm_curation_comparison(days)
    except Exception as e:
        logger.warning(f"[forward_signals] LLM curation comparison failed: {e}")
        return None


def format_digest_message(d: dict) -> str:
    """Telegram-ready human-readable summary."""
    lines = [
        f"📊 Weekly Forward Signals Digest ({d['window_days']}d)",
        f"Total signals: {d['total_signals']}",
    ]
    if d["by_type"]:
        breakdown = ", ".join(f"{k}={v}" for k, v in d["by_type"].items())
        lines.append(f"Breakdown: {breakdown}")
    if d["avg_return_7d_pct"] is not None:
        lines.append(f"Avg 7D return:  {d['avg_return_7d_pct']:+.2f}%  ({d['measured_7d']} measured)")
    if d["avg_return_14d_pct"] is not None:
        lines.append(f"Avg 14D return: {d['avg_return_14d_pct']:+.2f}%")
    if d["avg_return_30d_pct"] is not None:
        lines.append(f"Avg 30D return: {d['avg_return_30d_pct']:+.2f}%")
    if d["win_rate_7d_pct"] is not None:
        lines.append(f"Win rate 7D:    {d['win_rate_7d_pct']:.1f}%")
    if d.get("win_rate_7d_buy_pct") is not None:
        lines.append(f"  BUY win rate:  {d['win_rate_7d_buy_pct']:.1f}% ({d['measured_7d_buy']} measured)")
    if d.get("win_rate_7d_sell_pct") is not None:
        lines.append(f"  SELL win rate: {d['win_rate_7d_sell_pct']:.1f}% ({d['measured_7d_sell']} measured)")

    # The headline. A win rate without a benchmark measures the market, not the
    # signal — that is precisely how three months of zero edge read as 55.9%.
    ex = d.get("excess")
    if ex:
        verdict = ("✅ statistically meaningful" if ex["significant"]
                   else "⚠️ NOT distinguishable from zero")
        lines += [
            "",
            f"📐 vs {ex['benchmark']} (7D, same windows):",
            f"  Excess:    {ex['mean_excess_pct']:+.2f}%  (n={ex['n']}, t={ex['t_stat']:+.2f})",
            f"  Beat rate: {ex['beat_rate_pct']:.1f}%",
            f"  {verdict}",
        ]
        if not ex["significant"]:
            lines.append("  🎯 Action: win rate above is market beta, not edge. Do not tune on it.")
    else:
        lines += ["", "📐 vs benchmark: unavailable — win rate above is UNBENCHMARKED"]

    cmp = d.get("llm_curation_comparison")
    if cmp:
        lc, qo = cmp["llm_curated"], cmp["quant_only"]
        lines += ["", "🧠 LLM-curated vs quant-only (7D):"]
        if lc["avg_return_pct"] is not None:
            lines.append(
                f"  LLM-curated: {lc['avg_return_pct']:+.2f}% avg, "
                f"{lc['win_rate_pct']:.1f}% win ({lc['measured']} measured)"
            )
        else:
            lines.append("  LLM-curated: no measured signals this window")
        if qo["avg_return_pct"] is not None:
            lines.append(
                f"  Quant-only:  {qo['avg_return_pct']:+.2f}% avg, "
                f"{qo['win_rate_pct']:.1f}% win ({qo['measured']} measured)"
            )
        else:
            lines.append("  Quant-only:  no measured signals this window")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# News-Catalyst Event-Study Measurement — WATCH signal capture
# ─────────────────────────────────────────────────────────────────────────
# Designed in CLAUDE.md ("PLANNED: News-Catalyst Event-Study Measurement").
# Goal: measure whether a momentum/supertrend technical hit *plus* fresh news
# of a given sentiment direction predicts forward returns differently than a
# hit with no news at all — a prerequisite research step before ever
# considering a news-driven trading signal (that decision is explicitly
# parked, see CLAUDE.md Incident Archive, 2026-08-25). This module only
# records data; nothing here touches order_manager, execution_engine, or
# ibkr_worker.
#
# CAVEAT (documented, not "fixed" — there is no fix): Polygon's per-article
# sentiment label is a third-party black box this project does not
# independently validate. Every direction-based result this data produces
# should be read as "does Polygon's sentiment model predict returns", not
# "does news sentiment predict returns" in some model-independent sense.
# Separately, an LLM-computed sentiment label on already-published news
# carries a theoretical look-ahead-bias risk if the underlying model's
# training data included the article's aftermath (arXiv:2309.17322) — this is
# unverifiable from our side and is not something this project can correct
# for; it is only something every consumer of this data must keep in mind.

def _sentiment_direction(news: Optional[dict]) -> str:
    """Normalize a gap_scanner.fetch_recent_news_catalyst() result to a
    dedup/study bucket: 'positive' / 'negative' / 'neutral' / 'no_news'.

    'no_news' covers both "nothing published in the freshness window" (news
    is None) and "an article was found but has no ticker-specific sentiment"
    (fetch_recent_news_catalyst() can return sentiment=None when the ticker
    isn't in the article's own `insights` array) — both carry zero directional
    information for this study, so they belong in the same control bucket
    rather than a third bucket nothing downstream is designed to interpret.
    """
    if not news:
        return "no_news"
    sentiment = (news.get("sentiment") or "").strip().lower()
    return sentiment or "no_news"


def _last_watch_signature(ticker: str) -> Optional[tuple]:
    """Most recent WATCH signal's (sentiment_direction, news_publisher) for
    `ticker`, or None if this ticker has never had a WATCH row recorded.

    Direction is stored in the existing `ai_verdict` column (otherwise
    unpopulated for WATCH rows — signal_combiner.py never sets it for BUY/SELL
    either). Publisher is compared too, not just direction, per an external
    design review (IdeaDistill panel, 2026-08-26): a same-direction article
    from a genuinely different outlet is a materially new observation (a
    second, independent source corroborating/repeating a story), not a repeat
    of the same one — pure direction-based dedup was too coarse. Two no-news
    hits in a row both have publisher=None, which compares equal, so the
    control group is unaffected.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT ai_verdict, news_publisher FROM forward_signals "
            "WHERE ticker = ? AND signal_type = 'WATCH' "
            "ORDER BY signal_ts DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    if not row:
        return None
    return (row["ai_verdict"], row["news_publisher"])


def _news_age_minutes(news: Optional[dict]) -> Optional[float]:
    """Minutes between the article's published_utc and now, or None when
    there's no news. Lets a later analysis distinguish "this hit was caught
    within one scan cycle of the article publishing" (genuine catalyst
    reaction) from "sentiment has just been sitting there a while" (pure
    persistence) — the 30-min momentum/supertrend cadence means a WATCH row
    on its own can't tell those apart otherwise. Per the IdeaDistill design
    review, 2026-08-26 — rename the hypothesis or add this distinction; this
    is the distinction."""
    if not news or not news.get("published_utc"):
        return None
    try:
        published = news["published_utc"]
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 60.0)
    except Exception:
        return None


def _build_watch_catalyst_summary(source: str, news: Optional[dict]) -> str:
    """Human-readable catalyst_summary for a WATCH row — source tag plus
    either the news title or an explicit 'control' marker, so a later read of
    the raw table can tell a no-news control observation apart from a lookup
    that simply hadn't been wired up."""
    if news and news.get("title"):
        return f"[{source}] {news['title'][:200]}"
    return f"[{source}] no fresh news (control)"


def record_watch_signals(hits: list, source: str, max_workers: int = 10) -> dict:
    """
    Log a signal_type='WATCH' forward_signals row for every momentum/
    supertrend hit — the data-capture step of the News-Catalyst Event-Study
    Measurement. `hits` is the raw result list from
    src.momentum_scanner.scan_momentum() or
    src.supertrend.scan_supertrend_universe() (every hit, not just what
    auto_watchlist_agent ends up adding to the watchlist) — tickers with fresh
    news attached AND tickers with none are both logged; the no-news group is
    the control, not something to skip.

    Dedup key is (ticker, sentiment_direction, news_publisher) — NOT
    (ticker, time_window) and NOT (ticker, article_id). A hit repeating with
    the same direction AND the same publisher (or the same no-news state) is
    the same observation and is skipped; a direction change, a genuinely new
    publisher on the same direction, or news appearing/disappearing is logged
    as a new one. Logging every same-direction repeat would inflate the
    sample with correlated, non-independent points — the same clustering
    failure mode exit_simulation.py already hit once (see CLAUDE.md Incident
    Archive, 2026-08-05, "Exit Simulation"). The publisher condition was added
    2026-08-26 after an external design review found pure direction-based
    dedup too coarse: a second, independent outlet corroborating the same
    direction is materially new information, not a repeat of one story.

    Each recorded row also carries news_publisher and news_age_minutes (time
    between the article's published_utc and the moment this hit was caught) —
    added for the same review: the 30-min scan cadence means a WATCH row
    can't otherwise distinguish "this is a fresh reaction to news that just
    broke" from "sentiment has been sitting here a while" (pure persistence).
    Downstream analysis in catalyst_event_study.py should treat these as
    different populations, not pool them.

    News lookups are fanned out via ThreadPoolExecutor (same pattern as
    gap_scanner.scan_premarket_gaps / catalyst_scanner.fetch_sec_8k_events).
    Confirmed live 2026-08-25 that Massive/Polygon's Starter plan has no daily
    quota (soft 100 req/sec guidance only, a 20-call burst test hit no rate
    limit), so scanning the full ~270-290 hits/cycle this way is not a cost or
    rate-limit concern (see CLAUDE.md Incident Archive). A lookup failure for
    one ticker never blocks the others and never blocks recording that
    ticker's row — it just falls back to the no_news bucket, matching
    fetch_recent_news_catalyst()'s own never-raises contract.

    DB writes happen sequentially after the concurrent fetch phase completes
    (same two-phase split as watchlist_manager.py's scan_watchlist() —
    concurrent I/O, then sequential writes — see CLAUDE.md Incident Archive,
    2026-08-18), so there is no concurrent-write risk here despite the
    parallel news fetch.

    Returns {"checked", "recorded", "deduped", "news_found"}.
    """
    from src.gap_scanner import fetch_recent_news_catalyst

    stats = {"checked": 0, "recorded": 0, "deduped": 0, "news_found": 0}
    if not hits:
        return stats

    tickers = [h["ticker"] for h in hits if h.get("ticker")]
    if not tickers:
        return stats

    def _lookup(ticker: str):
        try:
            return ticker, fetch_recent_news_catalyst(ticker)
        except Exception as e:
            logger.debug(f"[forward_signals] WATCH news lookup failed for {ticker}: {e}")
            return ticker, None

    news_by_ticker: dict = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for ticker, news in pool.map(_lookup, tickers):
            news_by_ticker[ticker] = news

    for hit in hits:
        ticker = (hit.get("ticker") or "").upper()
        price = hit.get("price")
        if not ticker or not price:
            continue
        stats["checked"] += 1

        news = news_by_ticker.get(ticker)
        if news:
            stats["news_found"] += 1
        direction = _sentiment_direction(news)
        publisher = (news.get("publisher_name") or None) if news else None

        try:
            last_signature = _last_watch_signature(ticker)
        except Exception as e:
            # Fail-open: worst case this produces one extra correlated row
            # (the same non-independence record_watch_signals otherwise
            # guards against), not a silently lost observation.
            logger.warning(f"[forward_signals] WATCH dedup check failed for {ticker}: {e}")
            last_signature = None

        if last_signature == (direction, publisher):
            stats["deduped"] += 1
            continue

        record_signal(SignalRecord(
            ticker=ticker,
            signal_type="WATCH",
            entry_price=float(price),
            composite_score=hit.get("score"),
            catalyst_summary=_build_watch_catalyst_summary(source, news),
            supertrend_level=hit.get("level"),
            ai_verdict=direction,
            news_publisher=publisher,
            news_age_minutes=_news_age_minutes(news),
        ))
        stats["recorded"] += 1

    logger.info(f"[forward_signals] WATCH signals [{source}]: {stats}")
    return stats
