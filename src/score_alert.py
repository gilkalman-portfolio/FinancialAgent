"""
Score Alert — fires on sharp score changes for ALL scanned tickers (not just watchlist).
Uses the same alert types as watchlist_manager so cooldowns are shared and deduped.
"""

from src.database import get_score_trend, watchlist_save_alert
from src.telegram_notifier import TelegramNotifier
from src.watchlist_manager import _cooldown_passed
from src.execution_engine import evaluate_trade, format_trade_alert, normalize_score_data
from typing import List, Dict
from loguru import logger

JUMP_THRESHOLD = 15
DROP_THRESHOLD = 15
MIN_PREV_SCANS = 2
COOLDOWN_HOURS = 24


def check_alerts(results: List[Dict]) -> List[Dict]:
    telegram = TelegramNotifier()
    alerts_sent = []

    for r in results:
        ticker = r.get("ticker")
        score  = r.get("score") or r.get("explosion_score", 0)
        if not ticker or not score:
            continue

        history = get_score_trend(ticker, limit=5)
        if not history or len(history) < MIN_PREV_SCANS:
            continue

        prev_score = history[1].get("explosion_score") if len(history) > 1 else None
        if prev_score is None:
            continue

        delta = score - prev_score

        if delta >= JUMP_THRESHOLD:
            atype = "score_delta_rise"
            if not _cooldown_passed(ticker, atype):
                continue
            msg = (
                f"🚀 Score Jump: {ticker}\n"
                f"{prev_score:.0f} → {score:.0f} (+{delta:.0f} pts) | {_signal(score)}"
            )
            # Enrich with execution engine when score is actionable
            if score >= 60:
                try:
                    decision = evaluate_trade(ticker, normalize_score_data(r))
                    if decision:
                        msg += "\n\n" + format_trade_alert(decision)
                    else:
                        msg += f"\n🎯 {_action_on_rise(score)} (execution threshold not met)"
                except Exception as _ee:
                    logger.warning(f"Execution engine skipped for {ticker}: {_ee}")
                    msg += f"\n🎯 {_action_on_rise(score)}"
            else:
                msg += f"\n🎯 {_action_on_rise(score)}"
            # Telegram suppressed 2026-05-20 — superseded by combined_buy (composite ≥60)
            # Keep DB write for history.
            try:
                watchlist_save_alert(ticker, atype, msg, score=score, price=r.get("price"))
                alerts_sent.append({"ticker": ticker, "type": "jump", "delta": delta})
                logger.info(f"Score jump logged (telegram suppressed): {ticker} +{delta:.0f} pts")
            except Exception as e:
                logger.warning(f"Failed to log score_delta_rise for {ticker}: {e}")

        elif delta <= -DROP_THRESHOLD:
            atype = "score_delta_drop"
            if not _cooldown_passed(ticker, atype):
                continue
            # Telegram suppressed 2026-05-20 — user wants only IBKR real-time + catalyst alerts. DB kept.
            action = _action_on_drop(score)
            msg = (
                f"⚠️ Score Drop: {ticker}\n"
                f"{prev_score:.0f} → {score:.0f} ({delta:.0f} pts) | {_signal(score)}\n"
                f"🎯 {action}"
            )
            try:
                watchlist_save_alert(ticker, atype, msg, score=score, price=r.get("price"))
                alerts_sent.append({"ticker": ticker, "type": "drop", "delta": delta})
                logger.info(f"Score drop logged (telegram suppressed): {ticker} {delta:.0f} pts")
            except Exception as e:
                logger.warning(f"Failed to log score_delta_drop for {ticker}: {e}")

    return alerts_sent


def _action_on_rise(score: float) -> str:
    if score >= 75:
        return "STRONG BUY — consider entry. Set stop -8% below current price."
    if score >= 60:
        return "BUY signal — watch for volume confirmation before entry."
    return "WATCH — momentum improving, not yet actionable."


def _action_on_drop(score: float) -> str:
    if score < 35:
        return "SKIP — signal broken. Exit or avoid."
    if score < 45:
        return "NEUTRAL — reduce exposure, tighten stop."
    return "WATCH — weakening. No new entries until stabilized."


def _signal(score: float) -> str:
    if score >= 75: return "STRONG BUY"
    if score >= 60: return "BUY"
    if score >= 45: return "WATCH"
    if score >= 35: return "NEUTRAL"
    return "SKIP"
