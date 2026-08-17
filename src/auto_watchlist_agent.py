"""
Auto-Watchlist Agent
Receives scan results from any source (squeeze / catalyst / momentum),
filters by per-source rules from scheduler_config.json,
deduplicates via cooldown, then adds qualifying stocks to the watchlist.

Usage:
    from src.auto_watchlist_agent import run as aw_run
    added = aw_run(results, source="squeeze", cfg=load_config())
"""

from datetime import datetime, timedelta
from typing import Optional
from loguru import logger

from src.database import (
    watchlist_get_all, watchlist_add, watchlist_remove,
    watchlist_save_alert, watchlist_get_alerts, get_recent_scan_scores,
    has_open_ibkr_position,
)
from src.telegram_notifier import TelegramNotifier
from src.hysteresis import (
    passes_hysteresis,
    SQUEEZE_SI_ENTRY, SQUEEZE_SI_EXIT,
    CATALYST_SI_ENTRY, CATALYST_SI_EXIT,
    LIQUIDITY_ADV_ENTRY, LIQUIDITY_ADV_EXIT,
    AUTO_EXIT_COOLDOWN_DAYS, AUTO_WL_REENTRY_SCORE, AUTO_WL_MIN_HOLD_DAYS,
    AUTO_WL_SCORE_ENTRY, AUTO_WL_SCORE_EXIT,
)


def _in_watchlist(ticker: str, existing: set) -> bool:
    return (ticker or "").upper() in existing


# Mirrors scheduler.py's _AUTO_PREFIXES / _is_auto_ticker exactly — duplicated
# locally (not imported) because scheduler.py imports FROM this module, so
# importing back would be circular.
_AUTO_PREFIXES = ("Auto:", "Auto [", "Momentum:", "Squeeze:", "Catalyst:")


def _is_auto_ticker(notes: str) -> bool:
    return (notes or "").startswith(_AUTO_PREFIXES)


# ── Cooldown ──────────────────────────────────────────────────────────────────

_LEGACY_ALERT_TYPES = {
    "momentum": {"auto_wl_momentum", "momentum_alert"},
    "squeeze":  {"auto_wl_squeeze",  "squeeze_alert"},
    "catalyst": {"auto_wl_catalyst", "catalyst_alert"},
}

def _cooldown_ok(ticker: str, source: str, cooldown_minutes: int) -> bool:
    valid_types = _LEGACY_ALERT_TYPES.get(source, {f"auto_wl_{source}"})
    cutoff = (datetime.now() - timedelta(minutes=cooldown_minutes)).isoformat()
    alerts = watchlist_get_alerts(ticker=ticker, limit=50)
    return not any(a["alert_type"] in valid_types and a["sent_at"] > cutoff for a in alerts)


# Re-entry cooldown after auto-exit. Constants live in src.hysteresis (single
# source of truth). If a ticker was auto-exited within AUTO_EXIT_COOLDOWN_DAYS,
# refuse to re-add from any auto-watchlist source unless the score is
# exceptionally strong (>= AUTO_WL_REENTRY_SCORE). Breaks the add → exit →
# re-add thrash loop.


def _in_auto_exit_cooldown(ticker: str, days: int = AUTO_EXIT_COOLDOWN_DAYS) -> bool:
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    alerts = watchlist_get_alerts(ticker=ticker, limit=50)
    return any(a["alert_type"] == "auto_exit_cooldown" and a["sent_at"] > cutoff for a in alerts)


# ── Per-source filters ────────────────────────────────────────────────────────

def _check_liquidity(r: dict, src_cfg: dict, already_in: bool = False) -> bool:
    """Returns True if dollar-volume passes the hysteresis-aware gate. Fails
    CLOSED when data is unavailable (2026-08-17, was fail-open) — a thinly-
    traded ticker with missing volume data should not sail past the gate by
    default; this matches monitoring_queue.py::_liquid(), the equivalent gate
    for real IBKR monitoring, which already fails closed correctly.

    Uses LIQUIDITY_ADV_ENTRY / _EXIT for tickers gating into vs. already-in the
    monitored pool. Falls back to the legacy `min_avg_dollar_volume` config value
    as a floor when it is set and lower than the entry threshold."""
    cfg_floor = src_cfg.get("min_avg_dollar_volume", 0) or 0
    price = r.get("price", 0) or 0
    dv = r.get("avg_dollar_volume") or (price * r.get("avg_volume", 0))
    if not dv:
        return False  # no data — cannot verify liquidity, don't let it through
    # Hysteresis band (only applied if config floor doesn't override stricter)
    if not passes_hysteresis(dv, already_in, LIQUIDITY_ADV_ENTRY, LIQUIDITY_ADV_EXIT):
        return False
    if cfg_floor and dv < cfg_floor:
        return False
    return True


def _filter_squeeze(r: dict, src_cfg: dict, req_liquidity: bool, already_in: bool = False) -> bool:
    if r.get("score", 0) < src_cfg.get("min_score", 0):
        return False
    # Hysteresis SI band: enter at >=15, only drop below 10
    if not passes_hysteresis(r.get("si_pct", 0), already_in, SQUEEZE_SI_ENTRY, SQUEEZE_SI_EXIT):
        return False
    if r.get("dtc", 0) < src_cfg.get("min_days_to_cover", 0):
        return False
    if r.get("rvol", 0) < src_cfg.get("volume_spike_x", 0):
        return False
    max_cap = src_cfg.get("max_market_cap", 0)
    if max_cap:
        # squeeze returns market_cap_b (billions)
        mcap_raw = (r.get("market_cap_b") or 0) * 1e9
        if mcap_raw and mcap_raw > max_cap:
            return False
    if req_liquidity and not _check_liquidity(r, src_cfg, already_in=already_in):
        return False
    return True


def _filter_catalyst(r: dict, src_cfg: dict, req_liquidity: bool, already_in: bool = False) -> bool:
    if r.get("explosion_score", 0) < src_cfg.get("min_explosion_score", 0):
        return False
    # Hysteresis SI band: enter at >=10, only drop below 5
    if not passes_hysteresis(r.get("si_pct", 0), already_in, CATALYST_SI_ENTRY, CATALYST_SI_EXIT):
        return False
    if r.get("vol_ratio", 0) < src_cfg.get("volume_spike_x", 0):
        return False
    max_cap = src_cfg.get("max_market_cap", 0)
    if max_cap:
        mcap = r.get("market_cap") or 0
        if mcap and mcap > max_cap:
            return False
    if req_liquidity and not _check_liquidity(r, src_cfg, already_in=already_in):
        return False
    return True


def _filter_momentum(r: dict, src_cfg: dict, req_liquidity: bool, already_in: bool = False) -> bool:
    if r.get("score", 0) < src_cfg.get("min_score", 0):
        return False
    if r.get("vol_ratio", 0) < src_cfg.get("rvol_min", 0):
        return False
    if r.get("price_change_5d", 0) < src_cfg.get("price_change_5d_min", 0):
        return False
    # breakout filter: only apply if configured AND scanner computed it
    if src_cfg.get("breakout_lookback_days") and "is_breakout" in r and not r["is_breakout"]:
        return False
    if req_liquidity and not _check_liquidity(r, src_cfg, already_in=already_in):
        return False
    return True


def _filter_supertrend(r: dict, src_cfg: dict, req_liquidity: bool, already_in: bool = False) -> bool:
    """Intentionally NO composite-score gate — every fresh bullish Supertrend
    flip qualifies, mirroring a bare TradingView alertcondition(buySignal).
    Only sanity floors apply (penny-stock price floor, optional liquidity)."""
    if r.get("price", 0) < src_cfg.get("price_floor", 5.0):
        return False
    if req_liquidity and not _check_liquidity(r, src_cfg, already_in=already_in):
        return False
    return True


_FILTERS = {
    "squeeze":     _filter_squeeze,
    "catalyst":    _filter_catalyst,
    "momentum":    _filter_momentum,
    "supertrend":  _filter_supertrend,
}

_EMOJI = {
    "squeeze":    "🔥",
    "catalyst":   "⚡",
    "momentum":   "🚀",
    "supertrend": "📈",
}


# ── Notes builder ─────────────────────────────────────────────────────────────

def _build_notes(r: dict, source: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    if source == "squeeze":
        return (
            f"Auto [squeeze]: SI {r.get('si_pct', 0):.1f}% "
            f"DTC {r.get('dtc', 0):.1f} Score {r.get('score', 0):.0f} on {today}"
        )
    if source == "catalyst":
        event = r.get("catalyst_detail") or r.get("catalyst", "Event")
        return f"Auto [catalyst]: {event} Score {r.get('explosion_score', 0):.0f} on {today}"
    if source == "momentum":
        return (
            f"Auto [momentum]: ROC {r.get('roc_20d', 0):+.1f}% "
            f"Vol {r.get('vol_ratio', 0):.1f}x Score {r.get('score', 0):.0f} on {today}"
        )
    if source == "supertrend":
        return f"Auto [supertrend]: Bullish flip @ ${r.get('price', 0):.2f}, stop ${r.get('level', 0):.2f} on {today}"
    return f"Auto [{source}] on {today}"


def _build_telegram_line(r: dict, source: str) -> str:
    ticker = r["ticker"]
    if source == "squeeze":
        return (
            f"{ticker} | Score {r.get('score', 0):.0f} | "
            f"SI {r.get('si_pct', 0):.1f}% | DTC {r.get('dtc', 0):.1f} | "
            f"RVOL {r.get('rvol', 0):.1f}x"
        )
    if source == "catalyst":
        event = r.get("catalyst_detail") or r.get("catalyst", "Event")
        days  = r.get("days_to_event", "?")
        return f"{ticker} | Score {r.get('explosion_score', 0):.0f} | {event} ({days}d)"
    if source == "supertrend":
        return f"{ticker} | Bullish flip @ ${r.get('price', 0):.2f} | Stop ${r.get('level', 0):.2f}"
    # momentum
    return (
        f"{ticker} | Score {r.get('score', 0):.0f} | "
        f"ROC {r.get('roc_20d', 0):+.1f}% | Vol {r.get('vol_ratio', 0):.1f}x | "
        f"RSI {r.get('rsi', 0):.0f}"
    )


# ── Capacity rotation ────────────────────────────────────────────────────────
# When the watchlist is at max_items_total, a new candidate used to be silently
# dropped (see the old "max_total reached — stopping" break below). With four
# discovery sources now running every 30 min or less (squeeze, catalyst,
# momentum, supertrend), that silently blocked most candidates most of the
# time. Instead, evict the weakest eligible AUTO-added incumbent to make room
# — the same "replace the weakest with something better" logic
# scheduler.run_weekly_rotation() already uses, just triggered on demand
# instead of once a week.

def _min_hold_satisfied(added_at: str, min_days: int = AUTO_WL_MIN_HOLD_DAYS) -> bool:
    """Duplicated from scheduler.py (not imported — scheduler.py imports FROM
    this module, so importing back would be circular). Fails open (True) on
    any parse failure so legacy/missing added_at never locks a ticker in."""
    if not added_at:
        return True
    try:
        ts = datetime.fromisoformat(added_at)
    except Exception:
        try:
            ts = datetime.strptime(added_at[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return True
    return (datetime.now() - ts) >= timedelta(days=min_days)


def _avg_recent_score(ticker: str) -> Optional[float]:
    """The ticker's real composite-scan average (last 3 scan_results rows), or
    None if it has never been through the main composite scan (run_scan()).
    Discovery-source scores (momentum/squeeze/catalyst — each its own 0-100
    formula) are NOT comparable to this and must never be substituted for it;
    see CLAUDE.md Incident Archive 2026-08-17 for why that substitution was
    the root cause of wrongly evicting strong picks."""
    scores = get_recent_scan_scores(ticker, limit=3)
    return sum(scores) / len(scores) if scores else None


def _candidate_beats_weakest(ticker: str, weakest_score: float) -> bool:
    """Whether `ticker` is allowed to evict an incumbent scoring `weakest_score`
    (a real composite average — see _weakest_evictable_auto_ticker). Shared by
    _evict_for_capacity and scheduler.run_weekly_rotation so the rule can't
    drift between the two call sites again.

    `ticker`'s own composite history decides which rule applies — not which
    scanner discovered it, so a supertrend/momentum/squeeze/catalyst pick that
    also happens to have real scan_results can compete fairly:

      - Real composite average exists for `ticker`: must STRICTLY beat
        weakest_score. Apples-to-apples, same scale on both sides.
      - No composite average for `ticker`: never numerically compared against
        a real score (there is nothing valid to compare). Can only claim a
        slot whose incumbent has already fallen below AUTO_WL_SCORE_EXIT (40)
        — i.e. dead weight that hasn't cycled out through the normal auto-exit
        path yet. Can never bump a ticker still earning its place.
    """
    candidate_avg = _avg_recent_score(ticker)
    if candidate_avg is not None:
        return candidate_avg > weakest_score
    return weakest_score < AUTO_WL_SCORE_EXIT


def _weakest_evictable_auto_ticker(items: list) -> Optional[tuple]:
    """Among auto-added tickers that have cleared the minimum hold period,
    have no open IBKR position (never sacrifice a real held position to free
    a watchlist slot), and have a real composite-scan average, return
    (ticker, avg_3d_score, added_at) for the lowest-scoring one — or None if
    there is nothing eligible.

    A ticker with zero scan_results rows (discovered only via momentum/
    squeeze/catalyst/supertrend, never independently covered by run_scan())
    is excluded entirely rather than defaulted to a score of 0.0 — that
    default was the root cause of wrongly evicting strong picks (e.g. a
    97/100 momentum score ticker) within hours of being added. See CLAUDE.md
    Incident Archive 2026-08-17."""
    eligible = [
        w for w in items
        if _is_auto_ticker(w.get("notes", ""))
        and _min_hold_satisfied(w.get("added_at", ""))
        and not has_open_ibkr_position(w["ticker"])
    ]
    if not eligible:
        return None

    scored = []
    for w in eligible:
        avg = _avg_recent_score(w["ticker"])
        if avg is None:
            continue
        scored.append((w["ticker"], avg, w.get("added_at", "")))
    if not scored:
        return None

    return min(scored, key=lambda x: x[1])


def _evict_for_capacity(ticker: str, cfg: dict) -> Optional[str]:
    """Try to free one watchlist slot for `ticker`, a candidate that arrived
    while the list is full. Whether it may evict the weakest incumbent is
    decided by _candidate_beats_weakest() — see that function for the rule.

    Returns the evicted ticker, or None if no eviction happened — the caller
    must then fall back to the old "list is full" block.
    """
    weakest = _weakest_evictable_auto_ticker(watchlist_get_all())
    if weakest is None:
        return None
    weakest_ticker, weakest_score, _ = weakest

    if weakest_ticker == ticker:
        return None  # candidate is already the incumbent — nothing to evict

    if not _candidate_beats_weakest(ticker, weakest_score):
        return None

    # Same hardened ordering as every other exit path in this codebase:
    # cooldown rows written BEFORE the remove, so a crash between the two
    # still blocks re-add.
    watchlist_save_alert(
        weakest_ticker, "auto_exit_score",
        f"Capacity rotation: replaced (3d avg score {weakest_score:.0f})",
        score=weakest_score,
    )
    watchlist_save_alert(
        weakest_ticker, "auto_exit_cooldown",
        f"Cooldown {AUTO_EXIT_COOLDOWN_DAYS}d after capacity rotation exit",
        score=weakest_score,
    )
    watchlist_remove(weakest_ticker)
    logger.info(
        f"auto_watchlist: capacity rotation evicted {weakest_ticker} "
        f"(3d avg score {weakest_score:.0f})"
    )

    if cfg.get("telegram", True):
        try:
            sent = TelegramNotifier().send_message(
                f"🔄 Capacity rotation: removed {weakest_ticker} "
                f"(score {weakest_score:.0f}) to make room for a new candidate"
            )
        except Exception as e:
            logger.warning(f"auto_watchlist: capacity rotation Telegram failed: {e}")
            sent = False
        if not sent:
            logger.warning(f"auto_watchlist: capacity rotation Telegram not sent — evicted {weakest_ticker} anyway")

    return weakest_ticker


# ── Main entry point ──────────────────────────────────────────────────────────

def run(results: list, source: str, cfg: dict) -> list:
    """
    Filter → deduplicate → add to watchlist → Telegram summary.

    Args:
        results: list of dicts from squeeze_scanner / catalyst_scanner / momentum_scanner
        source:  "squeeze" | "catalyst" | "momentum"
        cfg:     full scheduler config dict (reads cfg["auto_watchlist"])

    Returns:
        list of result dicts that were added to the watchlist this run
    """
    aw_cfg = cfg.get("auto_watchlist", {})
    if not aw_cfg.get("enabled", True):
        return []

    src_cfg = aw_cfg.get("sources", {}).get(source, {})
    if not src_cfg.get("enabled", True):
        return []

    filter_fn = _FILTERS.get(source)
    if not filter_fn:
        logger.warning(f"auto_watchlist_agent: unknown source '{source}'")
        return []

    policy         = aw_cfg.get("watchlist_policy", {})
    dedup_cfg      = aw_cfg.get("deduplication", {})
    cooldown_min   = dedup_cfg.get("cooldown_minutes", 1440)
    max_per_source = policy.get("max_items_per_source", 10)
    max_total      = policy.get("max_items_total", 30)
    req_liquidity  = policy.get("require_liquidity_check", True)
    alert_score    = AUTO_WL_SCORE_ENTRY  # 70 — consistent with hysteresis entry threshold
    alert_pct      = float(src_cfg.get("alert_pct", 5.0))
    vol_x          = float(src_cfg.get("volume_spike_x", 0.0))

    # Current watchlist state
    existing  = {w["ticker"] for w in watchlist_get_all()}
    cur_total = len(existing)

    # Filter — pass `already_in=True` for tickers already on the watchlist so the
    # hysteresis EXIT thresholds (not ENTRY) apply to them. Prevents thrash.
    candidates = [
        r for r in results
        if filter_fn(r, src_cfg, req_liquidity,
                     already_in=_in_watchlist(r.get("ticker", ""), existing))
    ]
    logger.info(
        f"auto_watchlist [{source}]: {len(candidates)}/{len(results)} pass filters"
    )

    added = []
    for r in candidates:
        ticker = r.get("ticker", "").upper()
        if not ticker:
            continue

        if not _cooldown_ok(ticker, source, cooldown_min):
            logger.debug(f"auto_watchlist [{source}]: {ticker} in cooldown — skip")
            continue

        # Re-entry cooldown after a prior auto-exit. Only an exceptionally
        # strong REAL COMPOSITE score (>= AUTO_WL_REENTRY_SCORE) bypasses the
        # AUTO_EXIT_COOLDOWN_DAYS window — a source-specific score (momentum/
        # squeeze/catalyst) is on a different scale and was previously able to
        # silently defeat this cooldown (see CLAUDE.md Incident Archive
        # 2026-08-17). No composite history → cooldown always stays in effect.
        # Fail-safe: any error → skip.
        try:
            if _in_auto_exit_cooldown(ticker):
                _candidate_avg = _avg_recent_score(ticker)
                if _candidate_avg is None or _candidate_avg < AUTO_WL_REENTRY_SCORE:
                    logger.info(
                        f"[auto-watchlist] {ticker} in {AUTO_EXIT_COOLDOWN_DAYS}d cooldown after auto-exit "
                        f"(composite avg {_candidate_avg} < re-entry {AUTO_WL_REENTRY_SCORE}) — skip"
                    )
                    continue
        except Exception as _cd_err:
            logger.warning(f"[auto-watchlist] {ticker} cooldown check failed: {_cd_err} — skipping (fail-safe)")
            continue

        # Check the per-source cap FIRST — it's a cheap break with no side
        # effects. Checking it before the capacity/eviction block below
        # avoids evicting a ticker to free a slot this candidate can't
        # actually use because the source is already exhausted this cycle.
        if len(added) >= max_per_source:
            logger.info(
                f"auto_watchlist [{source}]: max_items_per_source ({max_per_source}) reached"
            )
            break

        # A ticker already on the watchlist doesn't need a new slot — only
        # gate/evict when this candidate would actually grow the list.
        if ticker not in existing and cur_total + len(added) >= max_total:
            evicted = _evict_for_capacity(ticker, cfg)
            if evicted:
                existing.discard(evicted)
                cur_total -= 1
            else:
                logger.info(f"auto_watchlist: max_total ({max_total}) reached — stopping")
                break

        notes = _build_notes(r, source)
        score = r.get("explosion_score") or r.get("score", 0)
        price = r.get("price", 0)

        try:
            if ticker not in existing:
                watchlist_add(
                    ticker,
                    notes=notes,
                    alert_score=alert_score,
                    alert_pct=alert_pct,
                    volume_spike_x=vol_x,
                )
                existing.add(ticker)

            # Always save alert — this is the dedup key even if ticker was already in watchlist
            watchlist_save_alert(
                ticker, f"auto_wl_{source}", notes,
                score=score, price=price
            )
        except Exception as _add_err:
            logger.error(f"auto_watchlist [{source}]: DB write failed for {ticker}: {_add_err} — skipping")
            continue

        added.append(r)
        logger.info(
            f"auto_watchlist [{source}]: added {ticker} "
            f"(score={score:.0f} price=${price:.2f})"
        )

    if added and cfg.get("telegram", True):
        emoji = _EMOJI.get(source, "➕")
        lines = [_build_telegram_line(r, source) for r in added]
        msg = (
            f"{emoji} Auto-Watchlist [{source.upper()}] "
            f"— {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"Added {len(added)} ticker(s):\n\n" + "\n".join(lines)
        )
        # The adds already happened (DB writes above) — an independent, valid
        # decision regardless of Telegram. Only the courtesy notification can
        # fail here; it must be logged clearly rather than silently, now that
        # scheduler.py's logging actually reaches disk. See CLAUDE.md
        # Incident Archive 2026-08-17.
        try:
            sent = TelegramNotifier().send_message(msg)
        except Exception as e:
            logger.warning(f"auto_watchlist [{source}]: Telegram failed: {e}")
            sent = False
        if sent:
            logger.info(
                f"auto_watchlist [{source}]: Telegram sent for {[r['ticker'] for r in added]}"
            )
        else:
            logger.warning(
                f"auto_watchlist [{source}]: Telegram summary failed to send — "
                f"added anyway: {[r['ticker'] for r in added]}"
            )

    return added
