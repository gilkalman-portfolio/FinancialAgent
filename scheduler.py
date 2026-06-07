"""
Scheduler - reads scheduler_config.json and runs aggregate scans automatically
Run: python scheduler.py
"""

import sys, json, time, threading, os
from pathlib import Path
from datetime import datetime
from typing import Optional

import schedule
from loguru import logger

from src.stock_scorer import score_stock, signal_label
from src.database import (init_db, save_scan_run, save_result,
                          watchlist_add, watchlist_get_all,
                          watchlist_save_alert, watchlist_get_alerts,
                          get_connection)
from src.telegram_notifier import TelegramNotifier
from src.score_alert import check_alerts
from src.execution_engine import evaluate_trade, format_trade_alert, normalize_score_data
from src.hysteresis import (
    passes_hysteresis,
    AUTO_WL_SCORE_ENTRY, AUTO_WL_SCORE_EXIT, AUTO_WL_MIN_HOLD_DAYS,
)

CONFIG_PATH = Path("scheduler_config.json")
LOG_FILE    = Path(__file__).parent / "logs" / "scheduler.log"
_PID_FILE   = Path(__file__).parent / "logs" / "scheduler.pid"


# ── Singleton guard — prevents two scheduler processes running simultaneously ──
def _acquire_singleton() -> bool:
    """Return True if this is the only running scheduler instance.
    Writes a PID file so future starts can detect us. Returns False (and logs)
    if another live instance is already running — caller should sys.exit(0)."""
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            if old_pid != os.getpid():
                try:
                    os.kill(old_pid, 0)   # signal 0 = existence check (cross-platform)
                    logger.error(
                        f"[singleton] Duplicate start blocked — scheduler PID {old_pid} "
                        "is already running. Exiting cleanly."
                    )
                    return False
                except OSError:
                    logger.info(f"[singleton] Stale PID {old_pid} — overwriting")
        except (ValueError, Exception) as _e:
            logger.debug(f"[singleton] PID file unreadable ({_e}) — overwriting")
    _PID_FILE.write_text(str(os.getpid()))
    logger.info(f"[singleton] Lock acquired (PID {os.getpid()})")
    return True


def _release_singleton():
    """Remove the PID file on clean exit."""
    try:
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _log(msg: str):
    """Thread-safe log — writes directly to file AND loguru."""
    logger.info(msg)
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | INFO | {msg}\n")
    except Exception:
        pass


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {"enabled": False}


def _is_trading_day() -> bool:
    """Returns True only on US market trading days (Mon-Fri, excluding federal holidays)."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:          # Saturday / Sunday
        return False
    # US market holidays (NYSE schedule) — fixed + floating
    year, month, day = now.year, now.month, now.day
    # Fixed holidays
    if (month, day) in [(1, 1), (7, 4), (12, 25)]:
        return False
    # New Year's / Christmas / Independence Day observed (if on weekend → adjacent weekday)
    if (month, day) in [(1, 2), (7, 3), (12, 24), (12, 26)]:
        if now.weekday() == 4:      # observed on Friday when holiday falls on Saturday
            return False
        if now.weekday() == 0:      # observed on Monday when holiday falls on Sunday
            return False
    # MLK Day — 3rd Monday of January
    if month == 1 and now.weekday() == 0 and 15 <= day <= 21:
        return False
    # Presidents Day — 3rd Monday of February
    if month == 2 and now.weekday() == 0 and 15 <= day <= 21:
        return False
    # Memorial Day — last Monday of May
    if month == 5 and now.weekday() == 0 and day >= 25:
        return False
    # Juneteenth — June 19 (observed)
    if (month, day) == (6, 19):
        return False
    if (month, day) == (6, 18) and now.weekday() == 4:   # observed Friday
        return False
    if (month, day) == (6, 20) and now.weekday() == 0:   # observed Monday
        return False
    # Labor Day — 1st Monday of September
    if month == 9 and now.weekday() == 0 and day <= 7:
        return False
    # Thanksgiving — 4th Thursday of November
    if month == 11 and now.weekday() == 3 and 22 <= day <= 28:
        return False
    # Good Friday — not easily computable, skip (rare edge case)
    return True


def load_tickers(sector: str, max_stocks: int, index_names: list = None) -> list:
    """מחזיר עד max_stocks מניות לסקטור מכל אחד מה-indices, מבלי כפילויות."""
    if index_names is None:
        index_names = ["Russell 2000"]
    try:
        from src.index_loader import get_tickers_by_sector
        seen: set = set()
        result: list = []
        for idx in index_names:
            for t in get_tickers_by_sector(idx, sector, max_stocks):
                if t not in seen:
                    seen.add(t)
                    result.append(t)
        return result
    except Exception:
        csv = Path("russell_holdings.csv")
        if not csv.exists():
            return []
        import pandas as pd
        for skip in [10, 9, 11, 0, 1, 2]:
            try:
                df = pd.read_csv(csv, skiprows=skip, encoding="utf-8", on_bad_lines="skip")
                tc = next((c for c in df.columns if "ticker" in c.lower() or "symbol" in c.lower()), None)
                sc = next((c for c in df.columns if "sector" in c.lower()), None)
                if tc and sc:
                    tickers = df[df[sc] == sector][tc].dropna().tolist()
                    return [t for t in tickers if len(str(t)) <= 5][:max_stocks]
            except Exception:
                continue
        return []


def _min_hold_satisfied(added_at: str, min_days: int = AUTO_WL_MIN_HOLD_DAYS) -> bool:
    """Returns True if a watchlist entry has been held at least `min_days` since added_at.

    `added_at` is expected to be an ISO timestamp string. On any parse failure we
    fail OPEN (return True) so that legacy/missing data does not lock entries in
    permanently."""
    from datetime import timedelta
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


def _alert_sent_recently(ticker: str, alert_type: str, hours: int = 24) -> bool:
    """Returns True if this alert_type was already sent for ticker within the last N hours."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    alerts = watchlist_get_alerts(ticker=ticker, limit=50)
    return any(a["alert_type"] == alert_type and a["sent_at"] > cutoff for a in alerts)


# ── Auto-watchlist re-entry cooldown ──────────────────────────────────────────
# When an auto-added ticker is auto-exited, we write an `auto_exit_cooldown`
# alert row. For AUTO_EXIT_COOLDOWN_DAYS after that, the ticker may NOT be
# re-added at the normal entry threshold (70). Only an exceptionally strong
# signal — score >= AUTO_WL_REENTRY_SCORE (75) — bypasses the cooldown.
# This breaks the daily thrash loop: add @ 70 → exit @ 49 → re-add @ 71 → ...
# Trade-off: a legitimate catalyst that lifts the score on day 3 will be ignored
# unless it pushes score above the re-entry threshold (75). Acceptable for noise
# reduction.
# Constants live in src/hysteresis.py — single source of truth
from src.hysteresis import AUTO_EXIT_COOLDOWN_DAYS, AUTO_WL_REENTRY_SCORE  # noqa: E402


def _in_auto_exit_cooldown(ticker: str, days: int = AUTO_EXIT_COOLDOWN_DAYS) -> bool:
    """Returns True if ticker was auto-exited within the last `days` days."""
    return _alert_sent_recently(ticker, "auto_exit_cooldown", hours=days * 24)


def _check_breakout(ticker: str, score: float, price: float) -> Optional[str]:
    if score < 65:
        return None
    try:
        import yfinance as yf
        import numpy as np
        hist = yf.Ticker(ticker).history(period="1y")
        if hist is None or len(hist) < 22:
            return None
        close = hist["Close"]
        volume = hist["Volume"]
        high_52w = close.iloc[:-1].max()
        sma20    = close.iloc[-20:].mean()
        std20    = close.iloc[-20:].std()
        upper_bb = sma20 + 2 * std20
        broke_52w = price > high_52w
        broke_bb  = price > upper_bb
        if not (broke_52w or broke_bb):
            return None
        # Quality filters for Bollinger-only breakouts — require confirming volume + momentum.
        # A 52w high break is strong enough on its own; Bollinger-only needs more evidence.
        if broke_bb and not broke_52w:
            vol_5  = volume.iloc[-5:].mean()
            vol_30 = volume.iloc[-30:].mean() if len(volume) >= 30 else volume.mean()
            rvol   = vol_5 / vol_30 if vol_30 > 0 else 1.0
            mom_5d = (close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100 if len(close) >= 6 else 0
            if rvol < 1.2 or mom_5d < 5.0:
                logger.debug(
                    f"Breakout {ticker} skipped: Bollinger-only with RVOL={rvol:.2f}x mom={mom_5d:.1f}%"
                )
                return None
        w52  = "✅" if broke_52w else "❌"
        bb   = "✅" if broke_bb  else "❌"
        msg = (
            f"🚀 BREAKOUT — {ticker}\n"
            f"52w High: {w52} | Bollinger: {bb}\n"
            f"Price: ${price:.2f} | Score: {score:.0f}\n"
            f"🎯 Momentum entry — buy breakout with stop below ${price * 0.92:.2f} (-8%)."
        )
        # NOTE: format_trade_plan_block intentionally removed — run_scan() appends
        # the execution engine block (evaluate_trade → format_trade_alert) which uses
        # a single consistent R:R framework. Two parallel trade plans in one message
        # produced contradictory stop/target values and confused the actionable output.
        return msg
    except Exception as e:
        logger.debug(f"Breakout check {ticker}: {e}")
        return None


def run_scan():
    if not _is_trading_day():
        logger.info("run_scan: skipping — weekend")
        return
    cfg = load_config()
    if not cfg.get("enabled", False):
        logger.info("Scheduler disabled - skipping")
        return

    sectors      = cfg.get("sectors", [])
    max_stocks   = cfg.get("max_stocks", 50)
    min_score    = cfg.get("min_score", 45)
    fc_days      = cfg.get("forecast_days", 30)
    telegram_on  = cfg.get("telegram", True)
    scan_indices = cfg.get("scan_indices", ["Russell 2000"])

    logger.info(f"Scheduled scan starting | sectors={sectors} | min={min_score} | indices={scan_indices}")
    init_db()

    all_results = []
    tickers_map = {s: load_tickers(s, max_stocks, scan_indices) for s in sectors}

    # Always include watchlist tickers in the main scan — prevents missing big moves
    # on tickers that are in watchlist but not in any sector index.
    wl_tickers_all = [w["ticker"] for w in watchlist_get_all()]
    already_covered = {t for lst in tickers_map.values() for t in lst}
    wl_extra = [t for t in wl_tickers_all if t not in already_covered]
    if wl_extra:
        tickers_map["watchlist"] = wl_extra
        logger.info(f"run_scan: added {len(wl_extra)} watchlist tickers not in sectors: {wl_extra}")

    total  = sum(len(v) for v in tickers_map.values())
    run_id = save_scan_run("scheduled", total)

    breakout_telegram = TelegramNotifier() if telegram_on else None

    watchlist_map = {w["ticker"]: w for w in watchlist_get_all()}
    auto_exited = []

    # In-scan dedup sets — second line of defence against duplicate alerts.
    # Primary guard is the singleton PID lock; these catch any edge-case
    # where the same ticker would be processed twice within a single run
    # (e.g. ticker appears in two sectors, or a future parallel refactor).
    _breakout_fired_this_run: set = set()
    _auto_exited_this_run:    set = set()

    # Hysteresis: entry at >=70 (see auto-add below), exit only when < 40 AND held >= 3 days.
    # See src/hysteresis.py for rationale. Old behaviour was symmetric 50/50, causing thrash.
    EXIT_SCORE_THRESHOLD = AUTO_WL_SCORE_EXIT  # 40

    for source, tickers in tickers_map.items():
        for ticker in tickers:
            r = score_stock(ticker, forecast_days=fc_days)
            if r and r["score"] >= min_score:
                r["source"] = source
                all_results.append(r)
                save_result(run_id, {**r, "explosion_score": r["score"]})

            if (r and r["score"] < EXIT_SCORE_THRESHOLD
                    and ticker in watchlist_map
                    and ticker not in _auto_exited_this_run):
                item = watchlist_map[ticker]
                if _is_auto_ticker(item.get("notes", "")) and _min_hold_satisfied(item.get("added_at", "")):
                    try:
                        from src.database import watchlist_remove
                        watchlist_remove(ticker)
                        watchlist_save_alert(
                            ticker, "auto_exit_score",
                            f"Auto-exit: score {r['score']:.0f} < {EXIT_SCORE_THRESHOLD}",
                            score=r["score"], price=r.get("price")
                        )
                        # Also write a re-entry cooldown marker (7d) so this
                        # ticker isn't immediately re-added by a later scan.
                        watchlist_save_alert(
                            ticker, "auto_exit_cooldown",
                            f"Cooldown {AUTO_EXIT_COOLDOWN_DAYS}d after auto-exit (score {r['score']:.0f})",
                            score=r["score"], price=r.get("price")
                        )
                        auto_exited.append(f"{ticker} ({r['score']:.0f})")
                        _auto_exited_this_run.add(ticker)
                        logger.info(f"Auto-exit (scan): removed {ticker} (score {r['score']:.0f})")
                        del watchlist_map[ticker]
                    except Exception as e:
                        logger.warning(f"Auto-exit remove {ticker}: {e}")

            if r and r["score"] >= 65 and telegram_on and ticker not in _breakout_fired_this_run:
                breakout_msg = _check_breakout(ticker, r["score"], r["price"])
                if breakout_msg and not _alert_sent_recently(ticker, "breakout_alert", hours=24):
                    try:
                        # Enrich breakout alert with execution engine
                        try:
                            decision = evaluate_trade(ticker, normalize_score_data(r))
                            if decision:
                                breakout_msg += "\n\n" + format_trade_alert(decision)
                        except Exception as _ee:
                            logger.debug(f"Execution engine skipped for breakout {ticker}: {_ee}")
                        # Telegram suppressed — superseded by combined_buy which fires at
                        # the actual breakout candle (IBKR real-time). Scan-time breakouts
                        # are structurally late (08:30/16:30 on prior-close data).
                        # DB write retained for opportunity tracking and audit.
                        watchlist_save_alert(ticker, "breakout_alert", breakout_msg,
                                             score=r["score"], price=r["price"])
                        _breakout_fired_this_run.add(ticker)
                        logger.info(f"Breakout alert logged (telegram suppressed): {ticker}")
                        # Record in opportunity tracker
                        try:
                            from src.opportunity_tracker import record_opportunity
                            from src.execution_engine import build_trade_plan
                            plan = build_trade_plan(ticker, r["price"])
                            if plan:
                                record_opportunity(
                                    ticker=ticker,
                                    signal_type="breakout_alert",
                                    entry_price=r["price"],
                                    stop_loss=plan["stop_loss"],
                                    target1=plan["target1"],
                                    target2=plan["target2"],
                                    rr_ratio=plan["rr_ratio"],
                                )
                        except Exception as _oe:
                            logger.debug(f"Opportunity record skipped for breakout {ticker}: {_oe}")
                    except Exception as e:
                        logger.warning(f"Breakout alert failed {ticker}: {e}")

    logger.info(f"Scan complete | {len(all_results)} above threshold")

    # ── Batch auto-exit notification ──────────────────────────────────────────
    if auto_exited and telegram_on and breakout_telegram:
        breakout_telegram.send_message(
            f"🗑 Auto-exit: removed {len(auto_exited)} ticker(s) (score < {EXIT_SCORE_THRESHOLD})\n"
            + ", ".join(auto_exited)
            + "\n🎯 These stocks no longer meet minimum criteria."
        )

    # ── Auto-add high-score stocks to watchlist ───────────────────────────────
    if cfg.get("auto_watchlist", True):
        existing = {w["ticker"] for w in watchlist_get_all()}
        # Ensure we don't exceed max total items (usually 30)
        max_total = cfg.get("auto_watchlist", {}).get("watchlist_policy", {}).get("max_items_total", 30)
        
        auto_added = []
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        # Sort results by score to add best ones first
        all_results.sort(key=lambda x: x["score"], reverse=True)
        
        for r in all_results:
            if len(existing) >= max_total:
                break
                
            if r["score"] >= AUTO_WL_SCORE_ENTRY and r["ticker"] not in existing:
                # Re-entry cooldown: if recently auto-exited, require stronger score
                if (_in_auto_exit_cooldown(r["ticker"])
                        and r["score"] < AUTO_WL_REENTRY_SCORE):
                    logger.info(
                        f"Auto-watchlist: {r['ticker']} in cooldown (score "
                        f"{r['score']:.0f} < {AUTO_WL_REENTRY_SCORE} re-entry) — skip"
                    )
                    continue
                watchlist_add(
                    r["ticker"],
                    notes=f"Auto: score {r['score']:.0f} on {today_str}",
                    alert_score=AUTO_WL_SCORE_ENTRY,
                    alert_pct=5.0,
                )
                existing.add(r["ticker"])
                # Suppress immediate re-alert — the auto-add itself is the notification
                for atype in ("score_threshold", "price_change"):
                    watchlist_save_alert(
                        r["ticker"], atype,
                        f"suppressed — auto-added at score {r['score']:.0f}",
                        score=r["score"], price=r.get("price"),
                    )
                auto_added.append(r["ticker"])
                logger.info(f"Auto-watchlist: added {r['ticker']} (score {r['score']:.0f})")
                
        if auto_added and telegram_on:
            telegram = TelegramNotifier()
            telegram.send_message(
                f"➕ Auto-added {len(auto_added)} ticker(s) to Watchlist\n"
                + ", ".join(auto_added)
            )

    jumped = check_alerts(all_results)
    if jumped:
        logger.info(f"Score alerts: {len(jumped)}")

    try:
        from src.backtester import run_backtest
        for days in [7, 14, 30]:
            summary = run_backtest(days)
            if summary["total"] > 0:
                logger.info(f"Backtest {days}d: {summary['accuracy_pct']}% accuracy | avg {summary['avg_return']:+.2f}%")
    except Exception as e:
        logger.warning(f"Backtest failed: {e}")

    if telegram_on and all_results:
        telegram = TelegramNotifier()
        all_results.sort(key=lambda x: x["score"], reverse=True)
        top   = all_results[:10]
        lines = "\n".join(
            f"{i+1}. {r['ticker']} {r['score']:.0f} - {signal_label(r['score'])} | {r['macd']} | {r['ma_trend']}"
            for i, r in enumerate(top)
        )
        telegram.send_message(
            f"Scheduled Scan {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"Top {len(top)} of {len(all_results)} above {min_score}:\n\n{lines}"
        )


_AUTO_PREFIXES = ("Auto:", "Auto [", "Momentum:", "Squeeze:", "Catalyst:")


def _is_auto_ticker(notes: str) -> bool:
    return (notes or "").startswith(_AUTO_PREFIXES)


def run_weekly_rotation():
    cfg = load_config()
    if not cfg.get("enabled", False):
        return
    try:
        init_db()
        from src.database import get_recent_scan_scores, watchlist_remove
        from src.momentum_scanner import scan_momentum
        from src.index_loader import get_index

        items = watchlist_get_all()
        auto_items = [w for w in items if _is_auto_ticker(w.get("notes", ""))]
        if not auto_items:
            logger.info("Weekly rotation: no auto-added tickers in watchlist")
            return

        existing_tickers = {w["ticker"] for w in items}

        def avg_score(ticker: str) -> float:
            scores = get_recent_scan_scores(ticker, limit=3)
            return sum(scores) / len(scores) if scores else 0.0

        scored = [(w["ticker"], avg_score(w["ticker"])) for w in auto_items]
        weakest_ticker, weakest_score = min(scored, key=lambda x: x[1])
        logger.info(f"Weekly rotation: weakest={weakest_ticker} score_3d_avg={weakest_score:.1f}")

        tickers_set: set = set()
        for index_name in ["Russell 2000", "S&P 500"]:
            df = get_index(index_name)
            if df is not None:
                tickers_set.update(df["ticker"].tolist())
        candidates_tickers = [t for t in tickers_set if t not in existing_tickers]

        if not candidates_tickers:
            logger.info("Weekly rotation: no candidate tickers available")
            return

        results = scan_momentum(candidates_tickers, min_score=75)
        if not results:
            logger.info("Weekly rotation: no momentum candidates with score >= 75")
            return

        best = results[0]
        best_ticker = best["ticker"]
        best_score = best["score"]

        if best_score <= weakest_score:
            logger.info(
                f"Weekly rotation: best candidate {best_ticker} ({best_score:.1f}) "
                f"not better than weakest {weakest_ticker} ({weakest_score:.1f}) — skipping"
            )
            return

        watchlist_remove(weakest_ticker)
        today_str = datetime.now().strftime("%Y-%m-%d")
        watchlist_add(
            best_ticker,
            notes=f"Auto [rotation]: score {best_score:.0f} on {today_str}",
            alert_score=70,
            alert_pct=5.0,
        )
        logger.info(f"Weekly rotation: removed {weakest_ticker} → added {best_ticker}")

        if cfg.get("telegram", True):
            TelegramNotifier().send_message(
                f"🔄 Weekly rotation: removed {weakest_ticker} ({weakest_score:.0f}) "
                f"→ added {best_ticker} ({best_score:.0f})"
            )
    except Exception as e:
        logger.error(f"Weekly rotation failed: {e}")


def run_watchlist_scan():
    if not _is_trading_day():
        logger.info("run_watchlist_scan: skipping — weekend")
        return
    try:
        init_db()
        from src.watchlist_manager import scan_watchlist
        from src.database import watchlist_remove
        results = scan_watchlist()
        logger.info(f"Watchlist scan complete: {len(results)} tickers")

        cfg = load_config()
        telegram_on = cfg.get("telegram", True)
        auto_exited = []
        
        # Hysteresis exit — see src/hysteresis.py. Was 50 (symmetric with old entry).
        EXIT_SCORE_THRESHOLD = AUTO_WL_SCORE_EXIT  # 40

        for r in results:
            if r.get("score", 100) < EXIT_SCORE_THRESHOLD:
                item = next((w for w in watchlist_get_all() if w["ticker"] == r["ticker"]), None)
                if item and _is_auto_ticker(item.get("notes", "")) and _min_hold_satisfied(item.get("added_at", "")):
                    if _alert_sent_recently(r["ticker"], "auto_exit_score", hours=12):
                        continue  # already exited in this morning's run_scan
                    try:
                        # Write cooldowns FIRST so they exist even if remove fails
                        watchlist_save_alert(
                            r["ticker"], "auto_exit_score",
                            f"Auto-exit: score {r['score']:.0f} < {EXIT_SCORE_THRESHOLD}",
                            score=r["score"], price=r.get("price")
                        )
                        # Re-entry cooldown marker (7d) — see _in_auto_exit_cooldown
                        watchlist_save_alert(
                            r["ticker"], "auto_exit_cooldown",
                            f"Cooldown {AUTO_EXIT_COOLDOWN_DAYS}d after auto-exit (score {r['score']:.0f})",
                            score=r["score"], price=r.get("price")
                        )
                        watchlist_remove(r["ticker"])
                        auto_exited.append(f"{r['ticker']} ({r['score']:.0f})")
                        logger.info(f"Auto-exit: removed {r['ticker']} (score {r['score']:.0f})")
                    except Exception as e:
                        logger.warning(f"Auto-exit remove {r['ticker']}: {e}")
        if auto_exited and telegram_on:
            TelegramNotifier().send_message(
                f"🗑 Auto-exit: removed {len(auto_exited)} ticker(s) (score < {EXIT_SCORE_THRESHOLD})\n"
                + ", ".join(auto_exited)
                + "\n🎯 These stocks no longer meet minimum criteria."
            )
    except Exception as e:
        logger.error(f"Watchlist scan failed: {e}")


def run_portfolio_scan():
    if not _is_trading_day():
        logger.info("run_portfolio_scan: skipping — weekend")
        return
    try:
        init_db()
        from src.watchlist_manager import scan_portfolio
        results = scan_portfolio()
        logger.info(f"Portfolio scan complete: {len(results)} positions")
    except Exception as e:
        logger.error(f"Portfolio scan failed: {e}")


def run_squeeze_scan():
    if not _is_trading_day():
        logger.info("run_squeeze_scan: skipping — weekend")
        return
    cfg = load_config()
    if not cfg.get("enabled", False):
        return
    try:
        init_db()
        from src.squeeze_scanner import scan_tickers
        from src.telegram_notifier import TelegramNotifier

        sectors    = cfg.get("sectors", [])
        max_stocks = cfg.get("max_stocks", 50)
        tickers    = []
        for s in sectors:
            tickers.extend(load_tickers(s, max_stocks))
        tickers = list(dict.fromkeys(tickers))  # dedupe

        logger.info(f"Squeeze scan starting | {len(tickers)} tickers")
        results = scan_tickers(tickers, min_score=40.0)
        logger.info(f"Squeeze scan complete | {len(results)} candidates")

        # ── Auto-Watchlist Agent ──────────────────────────────────────────────
        from src.auto_watchlist_agent import run as aw_run
        aw_run(results, "squeeze", cfg)

        if not results:
            return

        telegram_on = cfg.get("telegram", True)
        if not telegram_on:
            return

        # ── High SI+DTC combos (cooldown-filtered) ────────────────────────────
        # Thresholds raised 2026-05-20: 15%/10 → 20%/15 to reduce alert volume.
        # Prior rate ~106/week was noise; this targets ~30/week of high-conviction setups.
        high_si = [
            r for r in results
            if r.get("si_pct", 0) > 20 and r.get("dtc", 0) > 15
            and not _alert_sent_recently(r["ticker"], "squeeze_si_alert", hours=24)
        ]
        for r in high_si:
            watchlist_save_alert(
                r["ticker"], "squeeze_si_alert",
                f"SI {r['si_pct']:.1f}% DTC {r['dtc']:.1f} Score {r['score']:.0f}",
                score=r["score"], price=r.get("price")
            )
        if high_si:
            logger.info(f"Squeeze SI alert flagged for: {[r['ticker'] for r in high_si]}")

        # ── Single combined message: SI alerts + top scan results ─────────────
        entry_candidates = [r for r in results if r.get("entry_signal")]
        top = results[:10]

        sections = []

        if high_si:
            alert_lines = []
            for r in high_si:
                entry_flag = " 🟢 ENTRY" if r.get("entry_signal") else ""
                alert_lines.append(
                    f"  {r['ticker']} | SI {r['si_pct']:.1f}% | DTC {r['dtc']:.1f} | "
                    f"RVOL {r.get('rvol', 0):.1f}x | Score {r['score']:.0f}{entry_flag}"
                )
            sections.append("🚨 High SI+DTC Alert\n" + "\n".join(alert_lines))

        scan_lines = []
        for r in top:
            signal_flag = " 🟢 ENTRY" if r.get("entry_signal") else ""
            scan_lines.append(
                f"  {r['ticker']} | Score {r['score']:.0f} | SI {r['si_pct']:.0f}% | "
                f"DTC {r['dtc']:.1f} | RVOL {r.get('rvol', 0):.1f}x | "
                f"2d {r['price_change_2d']:+.1f}%{signal_flag}"
            )
        sections.append(
            f"🔥 Top Squeeze Candidates ({len(results)} found, {len(entry_candidates)} entry signals)\n"
            + "\n".join(scan_lines)
        )

        # Telegram suppressed 2026-05-20 — user wants only IBKR real-time + catalyst alerts. DB kept.
        _ = (
            f"📊 Squeeze Scan — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            + "\n\n".join(sections)
        )

    except Exception as e:
        logger.error(f"Squeeze scan failed: {e}")


def run_catalyst_alert():
    """Daily scan: biotech/pharma catalysts (earnings, PDUFA, 8-K) with SI >= 10%.
    Sends a Telegram alert for the top 5 candidates sorted by explosion_score."""
    if not _is_trading_day():
        logger.info("run_catalyst_alert: skipping — weekend")
        return
    cfg = load_config()
    if not cfg.get("enabled", False) or not cfg.get("telegram", True):
        return
    try:
        init_db()
        from src.catalyst_scanner import scan_catalysts

        logger.info("Catalyst+SI alert scan starting")
        results = scan_catalysts(
            days_ahead=7,
            catalyst_types=["earnings", "pdufa", "sec_8k"],
            min_explosion_score=40,
        )

        # Filter: SI >= 10% + event within 7 days
        candidates = [
            r for r in results
            if r.get("si_pct", 0) >= 10
            and r.get("days_to_event", 99) <= 7
            and r.get("price", 0) >= 5.0
            and not _alert_sent_recently(r["ticker"], "catalyst_si_alert", hours=24)
        ]
        candidates.sort(key=lambda x: x.get("explosion_score", 0), reverse=True)
        top = candidates[:5]

        logger.info(f"Catalyst+SI alert: {len(candidates)} candidates, sending top {len(top)}")

        # ── Auto-Watchlist Agent ──────────────────────────────────────────────
        from src.auto_watchlist_agent import run as aw_run
        aw_run(candidates, "catalyst", cfg)

        if not top:
            return

        blocks = []
        for r in top:
            ticker        = r["ticker"]
            event_label   = r.get("catalyst_detail") or r.get("catalyst", "Event")
            event_date    = r.get("catalyst_date", "?")
            days_to_event = r.get("days_to_event", "?")
            si_pct        = r.get("si_pct", 0)
            dtc           = r.get("dtc") or r.get("days_to_cover", 0)
            float_m       = r.get("float_m", 0)
            score         = r.get("explosion_score", 0)
            price         = r.get("price", 0)
            vol_ratio     = r.get("vol_ratio", 0)
            rsi           = r.get("rsi")
            extras = ""
            if r.get("has_unusual_calls"):
                extras += " | Unusual calls"
            if r.get("has_insider"):
                extras += " | Insider buying"

            rsi_warning = " ⚠️ RSI overbought — sell-the-news risk." if (rsi and rsi > 75) else ""
            if score >= 70:
                action = f"⚡ HIGH — Consider small position before event. Set stop -8%.{rsi_warning}"
            elif score >= 50:
                action = f"👀 MEDIUM — Watch closely. Enter only on breakout above resistance.{rsi_warning}"
            else:
                action = f"📋 LOW — Monitor only. Wait for volume confirmation before entry.{rsi_warning}"

            block = (
                f"*{ticker}* | Score {score:.0f} | ${price:.2f}\n"
                f"  Event: {event_label} on {event_date} ({days_to_event}d)\n"
                f"  SI: {si_pct:.1f}% | DTC: {dtc:.1f} | Float: {float_m:.0f}M"
                + (f" | RSI: {rsi:.0f}" if rsi else "")
                + (f" | RVOL: {vol_ratio:.1f}x" if vol_ratio else "")
                + extras
                + f"\n  🎯 {action}"
            )
            blocks.append(block)
            watchlist_save_alert(ticker, "catalyst_si_alert", block,
                                 score=score, price=price)
            logger.info(f"Catalyst+SI: {ticker} | score={score:.0f} | {event_label}")

        msg = (
            f"🔥 Catalyst + High SI — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"{len(top)} tickers | SI ≥10% + event ≤7d\n\n"
            + "\n\n".join(blocks)
        )
        TelegramNotifier().send_message(msg)

    except Exception as e:
        logger.error(f"Catalyst alert failed: {e}")


def run_long_setups():
    """Daily Long Setup scan — top setups from SP100_SUBSET sent to Telegram."""
    if not _is_trading_day():
        logger.info("run_long_setups: skipping — weekend")
        return
    try:
        cfg = load_config()
        if not cfg.get("long_setups_enabled", True):
            return

        from src.long_setup_scanner import scan_long_setups
        min_score = float(cfg.get("long_setups_min_score", 55))
        top_n     = int(cfg.get("long_setups_top_n", 5))

        _log(f"Long setups scan: min_score={min_score} top_n={top_n}")
        results = scan_long_setups(min_score=min_score, top_n=top_n)
        _log(f"Long setups: {len(results)} setup(s) found")

        if not results or not cfg.get("telegram", True):
            return

        lines = []
        for r in results:
            macd_str = f"MACD {r['macd_cross_days']}d" if r["macd_cross_days"] else "no cross"
            lines.append(
                f"{r['ticker']} | Score {r['score']:.0f} | ${r['price']:.2f} | "
                f"RSI {r['rsi']:.0f} | Vol {r['vol_ratio']:.1f}x | "
                f"5d {r['pct_5d']:+.1f}% | {macd_str}"
            )

        msg = (
            f"Long Setups — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"Top {len(results)} bullish setup(s):\n\n" + "\n".join(lines)
        )
        # Telegram suppressed — long_setups covers SP100 large-caps with daily bars;
        # MACD crossovers up to 5 days old are scored as "recent", making this a
        # structurally stale signal. No dedup against combined_buy. DB log retained.
        _log(f"Long setups logged (telegram suppressed): {[r['ticker'] for r in results]}")
    except Exception as e:
        logger.error(f"Long setups scan failed: {e}")


def run_watchlist_cleanup():
    if not _is_trading_day():
        logger.info("run_watchlist_cleanup: skipping — weekend")
        return
    cfg = load_config()
    if not cfg.get("telegram", True):
        return
    try:
        init_db()
        from src.database import get_recent_scan_scores, watchlist_get_all

        items = watchlist_get_all()
        auto_items = [w for w in items if _is_auto_ticker(w.get("notes", ""))]

        removed = []
        for item in auto_items:
            ticker = item["ticker"]
            scores = get_recent_scan_scores(ticker, limit=3)
            if len(scores) >= 3 and all(s < 50 for s in scores):
                try:
                    last_score = float(scores[0]) if scores else 0.0
                    with get_connection() as conn:
                        conn.execute(
                            "INSERT INTO watchlist_alerts (ticker, alert_type, message, sent_at, score, price) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                ticker.upper(),
                                "auto_exit_cooldown",
                                f"Cleanup auto-exit after 3x score<50 (scores={scores})",
                                datetime.now().isoformat(),
                                last_score,
                                None,
                            ),
                        )
                        conn.execute(
                            "DELETE FROM watchlist WHERE ticker = ?",
                            (ticker.upper(),),
                        )
                    removed.append(ticker)
                    logger.info(f"Watchlist cleanup: removed {ticker} (scores {scores})")
                except Exception as e:
                    logger.warning(f"Watchlist cleanup remove {ticker}: {e}")

        if removed:
            telegram = TelegramNotifier()
            telegram.send_message(
                f"🧹 Watchlist cleanup: removed {len(removed)} ticker(s): "
                + ", ".join(removed)
            )
            logger.info(f"Watchlist cleanup: {len(removed)} removed")
        else:
            logger.info("Watchlist cleanup: nothing to remove")
    except Exception as e:
        logger.error(f"Watchlist cleanup failed: {e}")


def run_market_digest():
    if not _is_trading_day():
        logger.info("run_market_digest: skipping — weekend")
        return
    try:
        from src.telegram_news_digest import send_market_digest
        send_market_digest()
    except Exception as e:
        logger.error(f"Market digest failed: {e}")


def run_portfolio_news():
    if not _is_trading_day():
        logger.info("run_portfolio_news: skipping — weekend")
        return
    try:
        from src.telegram_news_digest import send_portfolio_news
        send_portfolio_news()
    except Exception as e:
        logger.error(f"Portfolio news failed: {e}")


def run_alert_monitor():
    try:
        from src.alert_monitor import run_alert_monitor as _run
        _run()
    except Exception as e:
        logger.error(f"Alert monitor failed: {e}")


def run_forward_outcomes_update():
    """Daily — fill price_after_7d/14d/30d for matured forward_signals."""
    try:
        from src.forward_signals import update_outcomes
        stats = update_outcomes()
        _log(f"Forward outcomes update: {stats}")
    except Exception as e:
        logger.error(f"Forward outcomes update failed: {e}")


def run_forward_digest():
    """Weekly — send Telegram digest summarising last 7d of signals."""
    try:
        from src.forward_signals import weekly_digest, format_digest_message
        d = weekly_digest(days=7)
        if d["total_signals"] == 0:
            _log("Forward digest: no signals in window — skipping send")
            return
        msg = format_digest_message(d)
        TelegramNotifier().send_message(msg)
        _log(f"Forward digest sent ({d['total_signals']} signals)")
    except Exception as e:
        logger.error(f"Forward digest failed: {e}")


def run_opportunity_outcomes():
    """Daily — update status of open opportunity_log rows."""
    try:
        from src.opportunity_tracker import update_outcomes
        stats = update_outcomes()
        _log(f"Opportunity outcomes update: {stats}")
    except Exception as e:
        logger.error(f"Opportunity outcomes update failed: {e}")


def run_opportunity_digest():
    """Weekly (Friday) — send Telegram opportunity digest."""
    try:
        from src.opportunity_tracker import weekly_digest, format_digest_message
        d = weekly_digest(days=7)
        total = len(d["hits"]) + len(d["stops"]) + len(d["open"]) + len(d["expired"])
        if total == 0:
            _log("Opportunity digest: no entries in window — skipping send")
            return
        msg = format_digest_message(d)
        TelegramNotifier().send_message(msg)
        _log(f"Opportunity digest sent ({total} entries)")
    except Exception as e:
        logger.error(f"Opportunity digest failed: {e}")


def _momentum_monitor_thread(interval_minutes: int, threshold: float, indices: list):
    """Background thread — scans indices for momentum breakouts every N min during market hours."""
    from src.momentum_scanner import scan_momentum
    from src.index_loader import get_index
    from src.price_alert_monitor import _is_market_hours

    _log(f"Momentum monitor started — every {interval_minutes} min | threshold={threshold:.0f} | indices={indices}")

    while True:
        try:
            if _is_market_hours():
                _log("Momentum monitor: running scan...")
                cfg = load_config()

                tickers_set: set = set()
                for index_name in indices:
                    df = get_index(index_name)
                    if df is not None:
                        tickers_set.update(df["ticker"].tolist())
                tickers = list(tickers_set)

                if not tickers:
                    _log("Momentum monitor: no tickers loaded — skipping")
                else:
                    src_mo_cfg = cfg.get("auto_watchlist", {}).get("sources", {}).get("momentum", {})
                    lookback   = int(src_mo_cfg.get("breakout_lookback_days", 20))
                    results    = scan_momentum(tickers, min_score=threshold, breakout_lookback_days=lookback)
                    _log(f"Momentum monitor: {len(results)} hits above {threshold:.0f}")

                    if results:
                        from src.auto_watchlist_agent import run as aw_run
                        added = aw_run(results, "momentum", cfg)
                        _log(f"Momentum monitor: auto_watchlist_agent added {len(added)} ticker(s)")
            else:
                now_str = datetime.now().strftime("%H:%M")
                _log(f"Momentum monitor: outside market hours ({now_str}) — skipping")
        except Exception as e:
            _log(f"Momentum monitor error: {e}")

        time.sleep(interval_minutes * 60)


def _price_monitor_thread(interval_minutes: int):
    """Background thread — checks price targets + volume spikes + pairs spreads every N minutes."""
    from src.price_alert_monitor import (
        check_price_targets, check_volume_spikes, check_supertrend_flips,
        check_rsi_extremes, check_macd_crossover, check_supertrend_triple_alignment,
        check_expired_alert_trades, check_price_surge,
    )
    _log(f"Price alert monitor started — checking every {interval_minutes} min")
    while True:
        try:
            _log("Price alert monitor: running check...")
            check_price_targets()
            check_volume_spikes()
            check_supertrend_flips()
            check_rsi_extremes()
            check_macd_crossover()
            check_supertrend_triple_alignment()
            check_expired_alert_trades()
            check_price_surge()
            _log("Price alert monitor: check complete")
        except Exception as e:
            _log(f"Price monitor error: {e}")

        time.sleep(interval_minutes * 60)


def main():
    # ── Single-instance guard ─────────────────────────────────────────────────
    if not _acquire_singleton():
        # Another scheduler is live — exit cleanly so the watchdog also stops.
        # (Watchdog breaks its restart loop on returncode == 0.)
        sys.exit(0)

    try:
        _main_body()
    finally:
        _release_singleton()


def _main_body():
    cfg = load_config()
    if not cfg.get("enabled", False):
        logger.warning("Scheduler is disabled in scheduler_config.json")
        return

    times               = cfg.get("times", ["08:30", "16:30"])
    watchlist_time      = cfg.get("watchlist_time", "09:00")
    portfolio_time      = cfg.get("portfolio_time", "09:15")
    price_interval      = cfg.get("price_alert_interval_minutes", 5)
    market_digest_time  = cfg.get("market_digest_time", "08:00")
    portfolio_news_time = cfg.get("portfolio_news_time", "08:15")
    squeeze_scan_time   = cfg.get("squeeze_scan_time", "07:45")

    logger.info(f"Python: {sys.executable}")
    logger.info(
        f"Scheduler starting | scan={times} | watchlist={watchlist_time} | "
        f"portfolio={portfolio_time} | price_alerts=every {price_interval}min | "
        f"digest={market_digest_time} | portfolio_news={portfolio_news_time}"
    )

    for scan_time in times:
        schedule.every().day.at(scan_time).do(run_scan)
        logger.info(f"Scheduled scan at {scan_time}")

    schedule.every().day.at(watchlist_time).do(run_watchlist_scan)
    logger.info(f"Watchlist scan at {watchlist_time}")

    schedule.every().day.at(portfolio_time).do(run_portfolio_scan)
    logger.info(f"Portfolio scan at {portfolio_time}")

    schedule.every().day.at(market_digest_time).do(run_watchlist_cleanup)
    schedule.every().day.at(market_digest_time).do(run_market_digest)
    logger.info(f"Watchlist cleanup + market digest at {market_digest_time}")

    schedule.every().day.at(squeeze_scan_time).do(run_squeeze_scan)
    logger.info(f"Squeeze scan at {squeeze_scan_time}")

    catalyst_alert_time = cfg.get("catalyst_alert_time", "08:05")
    schedule.every().day.at(catalyst_alert_time).do(run_catalyst_alert)
    logger.info(f"Catalyst+SI alert at {catalyst_alert_time}")

    # Long Setups — daily after open
    long_setups_time = cfg.get("long_setups_time", "09:30")
    if cfg.get("long_setups_enabled", True):
        schedule.every().day.at(long_setups_time).do(run_long_setups)
        logger.info(f"Long setups scan at {long_setups_time}")

    # Weekly Rotation — every Monday at 08:15
    weekly_rotation_time = cfg.get("weekly_rotation_time", "08:15")
    schedule.every().monday.at(weekly_rotation_time).do(run_weekly_rotation)
    logger.info(f"Weekly rotation scheduled every Monday at {weekly_rotation_time}")

    schedule.every().day.at(portfolio_news_time).do(run_portfolio_news)
    logger.info(f"Portfolio news at {portfolio_news_time}")

    alert_monitor_time = cfg.get("alert_monitor_time", "09:30")
    schedule.every().day.at(alert_monitor_time).do(run_alert_monitor)
    logger.info(f"Alert monitor at {alert_monitor_time}")

    # Forward Signals — daily outcome update + weekly Telegram digest
    forward_outcomes_time = cfg.get("forward_outcomes_time", "18:00")
    schedule.every().day.at(forward_outcomes_time).do(run_forward_outcomes_update)
    logger.info(f"Forward outcomes update at {forward_outcomes_time}")

    forward_digest_time = cfg.get("forward_digest_time", "20:00")
    schedule.every().friday.at(forward_digest_time).do(run_forward_digest)
    logger.info(f"Forward weekly digest every Friday at {forward_digest_time}")

    # Opportunity Tracker — daily outcome update + weekly Telegram digest
    opp_outcomes_time = cfg.get("opportunity_outcomes_time", "18:00")
    schedule.every().day.at(opp_outcomes_time).do(run_opportunity_outcomes)
    logger.info(f"Opportunity outcomes update at {opp_outcomes_time}")

    opp_digest_time = cfg.get("opportunity_digest_time", "20:00")
    schedule.every().friday.at(opp_digest_time).do(run_opportunity_digest)
    logger.info(f"Opportunity weekly digest every Friday at {opp_digest_time}")

    # Price monitor — tight interval needs its own thread
    if price_interval > 0:
        monitor_thread = threading.Thread(
            target=_price_monitor_thread,
            args=(price_interval,),
            daemon=True,
            name="PriceAlertMonitor"
        )
        monitor_thread.start()
        logger.info(f"Price alert monitor thread started (every {price_interval} min)")

    # Momentum Monitor
    momentum_enabled  = cfg.get("momentum_enabled", True)
    momentum_interval = cfg.get("momentum_interval_minutes", 30)
    momentum_threshold = cfg.get("momentum_threshold", 70)
    momentum_indices  = cfg.get("momentum_indices", ["Russell 2000", "S&P 500"])

    if momentum_enabled:
        momentum_thread = threading.Thread(
            target=_momentum_monitor_thread,
            args=(momentum_interval, float(momentum_threshold), momentum_indices),
            daemon=True,
            name="MomentumMonitor"
        )
        momentum_thread.start()
        logger.info(
            f"Momentum monitor started "
            f"(every {momentum_interval}m | threshold={momentum_threshold} | indices={momentum_indices})"
        )

    # News Catalyst Monitor
    catalyst_enabled   = cfg.get("news_catalyst_enabled", True)
    catalyst_interval  = cfg.get("news_catalyst_interval_minutes", 15)
    catalyst_threshold = cfg.get("news_catalyst_threshold", 3)
    catalyst_max_llm   = cfg.get("news_catalyst_max_llm_per_cycle", 3)
    catalyst_scope     = cfg.get("news_catalyst_scope", "portfolio+watchlist")
    catalyst_max_age   = cfg.get("news_catalyst_max_article_age_minutes", 45)

    if catalyst_enabled:
        from src.news_catalyst_monitor import catalyst_monitor_thread
        catalyst_thread = threading.Thread(
            target=catalyst_monitor_thread,
            kwargs=dict(
                interval_minutes        = catalyst_interval,
                catalyst_threshold      = catalyst_threshold,
                max_llm_calls           = catalyst_max_llm,
                scope                   = catalyst_scope,
                max_article_age_minutes = catalyst_max_age,
            ),
            daemon=True,
            name="NewsCatalystMonitor"
        )
        catalyst_thread.start()
        logger.info(
            f"News catalyst monitor started "
            f"(every {catalyst_interval}m | threshold={catalyst_threshold} | scope={catalyst_scope})"
        )

    logger.info("Scheduler ready — waiting for scheduled times...")
    logger.info(f"Next jobs: scan={times}, watchlist={watchlist_time}, portfolio={portfolio_time}")

    while True:
        schedule.run_pending()
        time.sleep(30)
        cfg = load_config()
        if not cfg.get("enabled", False):
            logger.info("Scheduler disabled via config — exiting")
            break


if __name__ == "__main__":
    main()
