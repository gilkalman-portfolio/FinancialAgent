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
from loguru import logger

from src.database import (
    watchlist_get_all, watchlist_add,
    watchlist_save_alert, watchlist_get_alerts,
)
from src.telegram_notifier import TelegramNotifier
from src.hysteresis import (
    passes_hysteresis,
    SQUEEZE_SI_ENTRY, SQUEEZE_SI_EXIT,
    CATALYST_SI_ENTRY, CATALYST_SI_EXIT,
    LIQUIDITY_ADV_ENTRY, LIQUIDITY_ADV_EXIT,
    AUTO_EXIT_COOLDOWN_DAYS, AUTO_WL_REENTRY_SCORE, AUTO_WL_MIN_HOLD_DAYS,
    AUTO_WL_SCORE_ENTRY,
)


def _in_watchlist(ticker: str, existing: set) -> bool:
    return (ticker or "").upper() in existing


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
    """Returns True if dollar-volume passes the hysteresis-aware gate, or if data unavailable.

    Uses LIQUIDITY_ADV_ENTRY / _EXIT for tickers gating into vs. already-in the
    monitored pool. Falls back to the legacy `min_avg_dollar_volume` config value
    as a floor when it is set and lower than the entry threshold."""
    cfg_floor = src_cfg.get("min_avg_dollar_volume", 0) or 0
    price = r.get("price", 0) or 0
    dv = r.get("avg_dollar_volume") or (price * r.get("avg_volume", 0))
    if not dv:
        return True  # no data — don't filter out
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


_FILTERS = {
    "squeeze":  _filter_squeeze,
    "catalyst": _filter_catalyst,
    "momentum": _filter_momentum,
}

_EMOJI = {
    "squeeze":  "🔥",
    "catalyst": "⚡",
    "momentum": "🚀",
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
    # momentum
    return (
        f"{ticker} | Score {r.get('score', 0):.0f} | "
        f"ROC {r.get('roc_20d', 0):+.1f}% | Vol {r.get('vol_ratio', 0):.1f}x | "
        f"RSI {r.get('rsi', 0):.0f}"
    )


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
        # strong signal (score >= AUTO_WL_REENTRY_SCORE) bypasses the
        # AUTO_EXIT_COOLDOWN_DAYS window. Fail-safe: any error → skip.
        try:
            _candidate_score = r.get("explosion_score") or r.get("score", 0) or 0
            if _in_auto_exit_cooldown(ticker) and _candidate_score < AUTO_WL_REENTRY_SCORE:
                logger.info(
                    f"[auto-watchlist] {ticker} in {AUTO_EXIT_COOLDOWN_DAYS}d cooldown after auto-exit "
                    f"(score {_candidate_score:.0f} < re-entry {AUTO_WL_REENTRY_SCORE}) — skip"
                )
                continue
        except Exception as _cd_err:
            logger.warning(f"[auto-watchlist] {ticker} cooldown check failed: {_cd_err} — skipping (fail-safe)")
            continue

        if cur_total + len(added) >= max_total:
            logger.info(f"auto_watchlist: max_total ({max_total}) reached — stopping")
            break

        if len(added) >= max_per_source:
            logger.info(
                f"auto_watchlist [{source}]: max_items_per_source ({max_per_source}) reached"
            )
            break

        notes = _build_notes(r, source)
        score = r.get("explosion_score") or r.get("score", 0)
        price = r.get("price", 0)

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
        try:
            TelegramNotifier().send_message(msg)
            logger.info(
                f"auto_watchlist [{source}]: Telegram sent for {[r['ticker'] for r in added]}"
            )
        except Exception as e:
            logger.warning(f"auto_watchlist [{source}]: Telegram failed: {e}")

    return added
