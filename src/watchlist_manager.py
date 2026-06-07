"""
Watchlist Manager
- Scans all watchlist tickers
- Detects: score threshold, price % change, price_above, price_below alerts
- Sends Telegram alerts with 24-hour cooldown per alert_type per ticker
"""
import os, requests
import yfinance as yf
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from loguru import logger
from dotenv import load_dotenv
load_dotenv()

from src.database import (watchlist_get_all, watchlist_save_alert,
                           watchlist_get_alerts, portfolio_get_all,
                           get_last_saved_score)
from src.stock_scorer import score_stock, signal_label
from src.telegram_notifier import TelegramNotifier
from src.execution_engine import evaluate_trade, format_trade_alert, normalize_score_data, format_trade_plan_block, build_trade_plan

ALERT_COOLDOWN_HOURS = 24
_ET = None  # lazy-loaded ZoneInfo


def _is_trading_hours() -> bool:
    """True only during US regular market hours: Mon–Fri 09:30–16:00 ET.

    Used to gate price_change / price_above / price_below alerts so they
    don't fire on stale pre-market or after-hours yfinance prices.
    """
    try:
        from zoneinfo import ZoneInfo
        global _ET
        if _ET is None:
            _ET = ZoneInfo("America/New_York")
        now = datetime.now(_ET)
        if now.weekday() >= 5:          # Saturday = 5, Sunday = 6
            return False
        market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
        return market_open <= now <= market_close
    except Exception:
        return True  # fail-open so alerts aren't silently lost on tz errors
SCORE_DELTA_THRESHOLD = 15  # pts — triggers score_delta_drop / score_delta_rise alert

# Minimum score to auto-set price alerts after a BUY signal
AUTO_PRICE_ALERT_MIN_SCORE = 60


def _auto_set_price_alerts(ticker: str, price: float, score: float, item: dict):
    """
    After a BUY-type alert fires, compute a trade plan and write price_above /
    price_below / price_target to the watchlist row — IF the fields are currently
    empty (never overwrite user's manual settings).

    Only runs when score >= AUTO_PRICE_ALERT_MIN_SCORE.
    """
    if score < AUTO_PRICE_ALERT_MIN_SCORE:
        return
    try:
        plan = build_trade_plan(ticker, price)
        if not plan:
            return
        from src.database import watchlist_update
        # Only set each field when it is currently NULL / 0
        updates = {}
        if not item.get("price_above"):
            updates["price_above"] = plan["entry_high"]
        if not item.get("price_below"):
            updates["price_below"] = plan["stop_loss"]
        if not item.get("price_target"):
            updates["price_target"] = plan["target1"]
        if updates:
            watchlist_update(ticker, **updates)
            logger.info(
                f"Auto price alerts set for {ticker}: "
                + ", ".join(f"{k}={v:.2f}" for k, v in updates.items())
            )
    except Exception as e:
        logger.warning(f"_auto_set_price_alerts({ticker}): {e}")


def _last_alert_price(ticker: str, alert_type: str) -> Optional[float]:
    """Price from the most recent alert of this type, or None."""
    for a in watchlist_get_alerts(ticker=ticker, limit=50):
        if a["alert_type"] == alert_type and a.get("price"):
            return float(a["price"])
    return None


def _cooldown_passed(ticker: str, alert_type: str) -> bool:
    """True if no alert of this type was sent in the last ALERT_COOLDOWN_HOURS."""
    for a in watchlist_get_alerts(ticker=ticker, limit=50):
        if a["alert_type"] == alert_type:
            try:
                sent = datetime.fromisoformat(a["sent_at"])
                if datetime.now() - sent < timedelta(hours=ALERT_COOLDOWN_HOURS):
                    return False
            except Exception:
                pass
            break
    return True


def scan_watchlist(force: bool = False) -> List[Dict]:
    """
    Scans all watchlist tickers. Checks four alert types:
      - score_threshold : score >= alert_score
      - price_change    : price moved >= alert_pct% vs last recorded price
      - price_above     : price crossed above price_above target
      - price_below     : price crossed below price_below target
    All alerts respect ALERT_COOLDOWN_HOURS to prevent spam.
    """
    tickers = watchlist_get_all()
    if not tickers:
        return []

    results  = []
    telegram = TelegramNotifier()

    for item in tickers:
        ticker      = item["ticker"]
        alert_score = item.get("alert_score", 60)
        alert_pct   = item.get("alert_pct", 5.0)
        price_above = item.get("price_above")
        price_below = item.get("price_below")

        try:
            r = score_stock(ticker, forecast_days=30)
            if not r:
                continue

            score = r["score"]
            price = r["price"]
            result = {**r, "ticker": ticker, "alert_score": alert_score,
                      "alert_pct": alert_pct, "price_above": price_above, "price_below": price_below}

            # ── Score threshold — only alert when crossing the threshold, not while above it ──
            prev_score_alert = _last_alert_price(ticker, "score_threshold")  # reuse price field as prev score
            already_above = prev_score_alert is not None and prev_score_alert >= alert_score
            if score >= alert_score and not already_above and _cooldown_passed(ticker, "score_threshold"):
                # Telegram suppressed 2026-05-20 — superseded by combined_buy (composite ≥60).
                # evaluate_trade() removed from this branch: the enriched message was never
                # sent to Telegram, so calling the execution engine here was pure CPU waste
                # on every 12:00 scan. Minimal DB record kept for history + cooldown tracking.
                try:
                    msg = (f"score_threshold: {ticker} score={score:.0f} price=${price:.2f}")
                    watchlist_save_alert(ticker, "score_threshold", msg, score, score)
                    logger.info(f"Alert logged (telegram suppressed): {ticker} (score_threshold)")
                except Exception as e:
                    logger.warning(f"Failed to log score_threshold for {ticker}: {e}")
                # Auto price alerts — set price_above/below/target if not already set
                _auto_set_price_alerts(ticker, price, score, item)

            # ── Price % change vs last recorded ───────────────────────────
            # Gate: only fire during US regular market hours (09:30–16:00 ET).
            # Pre-market and after-hours yfinance prices are unreliable / stale.
            prev_price = _last_alert_price(ticker, "price_change")
            if prev_price is None:
                # No baseline yet — record current price as baseline, don't alert
                watchlist_save_alert(ticker, "price_change", f"Baseline price recorded: ${price:.2f}", score=None, price=price)
                logger.debug(f"Price baseline set for {ticker}: ${price:.2f}")
            elif prev_price > 0 and _is_trading_hours():
                pct_change = (price - prev_price) / prev_price * 100
                if abs(pct_change) >= alert_pct and _cooldown_passed(ticker, "price_change"):
                    direction = "📈 UP" if pct_change > 0 else "📉 DOWN"
                    action = ("Consider adding to position." if pct_change > 0 and score >= 60
                              else "Review position — check if stop needs adjustment." if pct_change < 0
                              else "Monitor for continuation.")
                    msg = (f"💹 Price Alert: {ticker}\n"
                           f"Moved {pct_change:+.1f}% {direction}\n"
                           f"Current: ${price:.2f} | Previous: ${prev_price:.2f}\n"
                           f"Score: {score:.0f} ({signal_label(score)})\n"
                           f"🎯 {action}")
                    _send_alert(telegram, ticker, "price_change", msg, score, price)

            # ── Price level alerts (above / below unified) ────────────────
            # Gate: only fire during regular market hours (09:30–16:00 ET).
            for level_val, alert_type, emoji, action in [
                (price_above, "price_above", "📈", "Level broken to upside — consider entry or add."),
                (price_below, "price_below", "📉", "Level broken to downside — tighten stop or exit."),
            ]:
                if not level_val or not _is_trading_hours():
                    continue
                triggered = (alert_type == "price_above" and price >= level_val) or \
                            (alert_type == "price_below" and price <= level_val)
                if triggered and _cooldown_passed(ticker, alert_type):
                    direction = "ABOVE" if alert_type == "price_above" else "BELOW"
                    msg = (
                        f"{emoji} {ticker} crossed {direction} ${level_val:.2f}\n"
                        f"Price: ${price:.2f} | Score: {score:.0f} ({signal_label(score)})\n"
                        f"🎯 {action}"
                    )
                    _send_alert(telegram, ticker, alert_type, msg, score, price)

            # ── Score delta alert ──────────────────────────────────────────
            prev_score = get_last_saved_score(ticker)
            if prev_score is not None:
                _send_score_delta_alert(telegram, ticker, score, prev_score, price,
                                        context=f"Signal: {signal_label(score)}",
                                        score_data=result,
                                        watchlist_item=item)

            results.append(result)

        except Exception as e:
            logger.warning(f"Watchlist scan {ticker}: {e}")

    return results


def scan_portfolio() -> List[Dict]:
    """
    Scans portfolio holdings.
    Alerts on: stop loss breach, target price hit, score drop below 35.
    All with ALERT_COOLDOWN_HOURS cooldown.
    """
    holdings = portfolio_get_all()
    if not holdings:
        return []

    telegram = TelegramNotifier()
    results  = []

    for item in holdings:
        ticker       = item["ticker"]
        entry_price  = item["entry_price"]
        stop_loss    = item.get("stop_loss")
        target_price = item.get("target_price")
        shares       = item.get("shares", 0)

        try:
            r = score_stock(ticker, forecast_days=30)
            if not r:
                continue

            score   = r["score"]
            price   = r["price"]
            pnl_pct = ((price - entry_price) / entry_price * 100) if entry_price else 0
            pnl_val = (price - entry_price) * shares if shares else None

            result = {**r, "ticker": ticker, "entry_price": entry_price,
                      "shares": shares, "pnl_pct": pnl_pct, "pnl_val": pnl_val,
                      "stop_loss": stop_loss, "target_price": target_price}

            if stop_loss and price <= stop_loss and _cooldown_passed(ticker, "stop_loss"):
                pnl_str = f"{pnl_val:+.0f}$" if pnl_val else ""
                msg = (f"🛑 STOP LOSS HIT: {ticker}\n"
                       f"Price: ${price:.2f} ≤ Stop: ${stop_loss:.2f}\n"
                       f"P&L: {pnl_pct:+.1f}% {pnl_str}\n"
                       f"🎯 EXIT — close position to limit losses.")
                _send_alert(telegram, ticker, "stop_loss", msg, score, price)

            if target_price and price >= target_price and _cooldown_passed(ticker, "target_hit"):
                pnl_str = f"{pnl_val:+.0f}$" if pnl_val else ""
                msg = (f"🎯 TARGET HIT: {ticker}\n"
                       f"Price: ${price:.2f} ≥ Target: ${target_price:.2f}\n"
                       f"P&L: {pnl_pct:+.1f}% {pnl_str}\n"
                       f"🎯 Take profit or raise stop to lock in gains.")
                _send_alert(telegram, ticker, "target_hit", msg, score, price)

            if score < 35 and _cooldown_passed(ticker, "score_drop"):
                msg = (f"⚠️ Portfolio Warning: {ticker}\n"
                       f"Score: {score:.0f}/100 (SKIP) | P&L: {pnl_pct:+.1f}% | Price: ${price:.2f}\n"
                       f"🎯 Review position — consider exit or tight stop.")
                _send_alert(telegram, ticker, "score_drop", msg, score, price)

            # ── Score delta alert ──────────────────────────────────────────
            prev_score = get_last_saved_score(ticker)
            if prev_score is not None:
                _send_score_delta_alert(telegram, ticker, score, prev_score, price,
                                        context=f"P&L: {pnl_pct:+.1f}%",
                                        label_prefix="Portfolio ")

            results.append(result)

        except Exception as e:
            logger.warning(f"Portfolio scan {ticker}: {e}")

    return results


def get_watchlist_news(tickers: List[str], days: int = 3) -> Dict[str, List]:
    """Fetch recent news for watchlist tickers from Finnhub."""
    api_key  = os.getenv("FINNHUB_API_KEY", "")
    today    = datetime.now().strftime("%Y-%m-%d")
    from_dt  = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    news_map = {}

    for ticker in tickers:
        try:
            resp = requests.get(
                f"https://finnhub.io/api/v1/company-news?symbol={ticker}"
                f"&from={from_dt}&to={today}",
                headers={"X-Finnhub-Token": api_key},
                timeout=5
            )
            if resp.status_code == 200:
                news_map[ticker] = resp.json()[:5]
        except Exception:
            news_map[ticker] = []

    return news_map


def _send_score_delta_alert(
    telegram: TelegramNotifier,
    ticker: str,
    score: float,
    prev_score: float,
    price: float,
    context: str = "",
    label_prefix: str = "",
    score_data: dict = None,
    watchlist_item: dict = None,
):
    delta = score - prev_score
    if delta <= -SCORE_DELTA_THRESHOLD and _cooldown_passed(ticker, "score_delta_drop"):
        # Telegram suppressed 2026-05-20 — user wants only IBKR real-time + catalyst alerts. DB kept.
        action = ("EXIT — signal broken." if score < 35
                  else "Tighten stop. No new entries until stabilized.")
        msg = (
            f"⚠️ {label_prefix}Score Drop: {ticker}\n"
            f"Score: {prev_score:.0f} → {score:.0f} ({delta:+.0f} pts) | {signal_label(score)}\n"
            f"{context} | Price: ${price:.2f}\n"
            f"🎯 {action}"
        )
        try:
            watchlist_save_alert(ticker, "score_delta_drop", msg, score, price)
            logger.info(f"Alert logged (telegram suppressed): {ticker} (score_delta_drop)")
        except Exception as e:
            logger.warning(f"Failed to log score_delta_drop for {ticker}: {e}")
    elif delta >= SCORE_DELTA_THRESHOLD and _cooldown_passed(ticker, "score_delta_rise"):
        # Telegram suppressed 2026-05-20 — superseded by combined_buy (composite ≥60)
        msg = (
            f"🚀 {label_prefix}Score Surge: {ticker}\n"
            f"Score: {prev_score:.0f} → {score:.0f} ({delta:+.0f} pts) | {signal_label(score)}\n"
            f"{context} | Price: ${price:.2f}"
        )
        if score >= 60 and score_data:
            try:
                decision = evaluate_trade(ticker, normalize_score_data(score_data))
                if decision:
                    msg += "\n\n" + format_trade_alert(decision)
                else:
                    msg += "\n🎯 Consider entry with stop -8% (execution threshold not met)"
            except Exception as _ee:
                logger.warning(f"Execution engine skipped for {ticker}: {_ee}")
                msg += "\n🎯 Consider entry with stop -8%."
        else:
            action = ("Consider entry with stop -8%." if score >= 60
                      else "Watch — improving but not yet actionable.")
            msg += f"\n🎯 {action}"
        # Append trade plan block
        try:
            plan_block = format_trade_plan_block(ticker, price)
            if plan_block:
                msg += plan_block
        except Exception as _pp:
            logger.warning(f"Trade plan block skipped for {ticker} (delta_rise): {_pp}")
        # Keep DB log only; Telegram suppressed (see comment above).
        try:
            watchlist_save_alert(ticker, "score_delta_rise", msg, score, price)
            logger.info(f"Alert logged (telegram suppressed): {ticker} (score_delta_rise)")
        except Exception as e:
            logger.warning(f"Failed to log score_delta_rise for {ticker}: {e}")
        # Auto price alerts — set if empty and score >= threshold
        if score >= AUTO_PRICE_ALERT_MIN_SCORE:
            _auto_set_price_alerts(ticker, price, score, watchlist_item or {})


def _send_alert(telegram: TelegramNotifier, ticker: str, alert_type: str,
                message: str, score: float, price: float):
    try:
        telegram.send_message(message)
        watchlist_save_alert(ticker, alert_type, message, score, price)
        logger.info(f"Alert sent: {ticker} ({alert_type})")
    except Exception as e:
        logger.warning(f"Failed to send alert for {ticker}: {e}")
