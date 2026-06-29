"""
Price Alert Monitor
Checks watchlist price targets every N minutes and sends Telegram alerts.
Runs as a background thread inside scheduler.py.

Logic:
  - Alert when price crosses price_target (static threshold, not %)
  - Market hours only: Mon-Fri 09:30-16:00 ET
  - Cooldown: 4 hours per ticker (avoids spam)
  - Always logs — even when no targets set — so you can confirm it's alive
"""

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from loguru import logger
from dotenv import load_dotenv
load_dotenv()

from src.database import (
    watchlist_get_all, watchlist_save_alert, watchlist_get_alerts,
    alert_trade_open, alert_trade_close, alert_trade_get_open,
)
from src.telegram_notifier import TelegramNotifier
from src.yf_cache import get_price as _cached_price, get_history as _cached_hist
from src.execution_engine import calc_position_size, is_noise_window

COOLDOWN_HOURS = 4

# Hold period (min, max days) per BUY alert type
_ALERT_HOLD_DAYS: dict[str, tuple[int, int]] = {
    "rsi_oversold":           (3, 7),
    "macd_bullish":           (5, 14),
    "supertrend_1h_flip":     (3, 7),
    "supertrend_flip":        (7, 14),
    "supertrend_triple_bull": (7, 21),
    "breakout_alert":         (3, 10),
}

# Alert types that CLOSE an open trade
_SELL_ALERT_TYPES = {
    "rsi_overbought", "macd_bearish", "supertrend_triple_bear",
}
_ET = ZoneInfo("America/New_York")


def _is_market_hours() -> bool:
    now = datetime.now(_ET)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now <= market_close


def _cooldown_ok_type(ticker: str, alert_type: str, hours: int = COOLDOWN_HOURS) -> bool:
    """Generic cooldown check — True if no alert of this type sent within `hours`."""
    for a in watchlist_get_alerts(ticker=ticker, limit=20):
        if a["alert_type"] == alert_type:
            try:
                if datetime.now() - datetime.fromisoformat(a["sent_at"]) < timedelta(hours=hours):
                    return False
            except Exception:
                pass
            break
    return True


def _cooldown_ok(ticker: str) -> bool:
    """Backward-compat wrapper for price_target cooldown."""
    return _cooldown_ok_type(ticker, "price_target")


def _get_price(ticker: str) -> float | None:
    return _cached_price(ticker, ttl=180)   # 3-min cache — fresh enough for 5-min monitor


def _build_action_block(price: float, hist, hold_days: str, note: str = "") -> str:
    """Hebrew action points block with stop/target/size from ATR. Used by all BUY alerts."""
    try:
        high = hist["High"]
        low = hist["Low"]
        prev_close = hist["Close"].shift(1)
        tr = (high - low).combine((high - prev_close).abs(), max).combine(
            (low - prev_close).abs(), max)
        atr = float(tr.tail(14).mean())
        sz = calc_position_size(
            price=price, atr=atr, portfolio_value=100_000,
            regime_multiplier=1.0, track="A",
        )
        stop_pct = (sz["stop_price"] - price) / price * 100
        tgt_pct  = (sz["target_price"] - price) / price * 100
        lines = [
            "",
            "🎯 *תוכנית פעולה:*",
            f"• כניסה: ${price:.2f} עכשיו",
            f"• סטופ לוס: ${sz['stop_price']:.2f} ({stop_pct:.1f}%) | 2×ATR",
            f"• יעד: ${sz['target_price']:.2f} (+{tgt_pct:.1f}%) | יחס {sz['rr_ratio']:.1f}:1",
            f"• החזק: {hold_days}",
            f"• גודל: {sz['shares']} מניות (${sz['dollar_invested']:,.0f})",
        ]
        if note:
            lines.append(f"\n⚠️ {note}")
        return "\n".join(lines)
    except Exception:
        return f"\n🎯 כניסה: ${price:.2f} | החזק: {hold_days}"


def check_price_targets():
    """
    Main check — called every N minutes.
    Only runs during US market hours (Mon-Fri 09:30-16:00 ET).
    Fires alert when price reaches or crosses price_target exactly (not % based).
    """
    if not _is_market_hours():
        now_et = datetime.now(_ET)
        logger.info(f"Price alert monitor: outside market hours ({now_et.strftime('%a %H:%M ET')}) — skipping")
        return

    items   = watchlist_get_all()
    targets = [w for w in items if w.get("price_target") and float(w["price_target"]) > 0]

    if not targets:
        logger.info("Price alert monitor: no price targets set — nothing to check")
        return

    logger.info(f"Price alert monitor: checking {len(targets)} target(s): {[w['ticker'] for w in targets]}")
    telegram = TelegramNotifier()

    for item in targets:
        ticker = item["ticker"]
        target = float(item["price_target"])
        price  = _get_price(ticker)

        if price is None:
            logger.warning(f"Price alert monitor: could not get price for {ticker}")
            continue

        logger.info(f"Price alert monitor: {ticker} price=${price:.2f} target=${target:.2f}")

        # Fire when price crosses the target (direction matters)
        # Use a tiny $0.05 buffer only to handle tick gaps — not a % threshold
        reached = abs(price - target) <= 0.05
        if reached and _cooldown_ok(ticker):
            hit_above = price >= target
            direction_emoji = "⬆️" if hit_above else "⬇️"
            action_line = "• שקול לממש רווח חלקי או להגדיר סטופ טריילינג" if hit_above \
                     else "• הגדר סטופ לוס — המחיר חצה מתחת ליעד"
            msg = (
                f"🎯 *יעד מחיר הושג — {ticker}*\n"
                f"{direction_emoji} מחיר: ${price:.2f} | יעד: ${target:.2f}\n\n"
                f"🎯 *תוכנית פעולה:*\n"
                f"{action_line}\n"
                f"• עדכן את היעד אם הטרנד ממשיך\n\n"
                f"⏰ {datetime.now().strftime('%H:%M')}"
            )
            if item.get("notes"):
                msg += f"\nהערה: {item['notes']}"
            try:
                telegram.send_message(msg)
                watchlist_save_alert(ticker, "price_target", msg, score=None, price=price)
                logger.info(f"Price target alert SENT: {ticker} @ ${price:.2f} (target ${target:.2f})")
            except Exception as e:
                logger.warning(f"Failed to send price alert {ticker}: {e}")


def check_volume_spikes():
    """
    Fires a Telegram alert when a watchlist ticker's current day volume
    exceeds `volume_spike_x` × its 10-day average volume.
    Only runs during market hours. Cooldown: 4 hours per ticker.
    """
    if not _is_market_hours():
        return

    items    = watchlist_get_all()
    monitors = [w for w in items if (w.get("volume_spike_x") or 0) > 0]

    if not monitors:
        return

    logger.info(f"Volume spike monitor: checking {len(monitors)} ticker(s)")
    # Telegram suppressed 2026-05-20 — lagging/high-latency indicator. DB kept for audit.

    for item in monitors:
        ticker    = item["ticker"]
        threshold = float(item["volume_spike_x"])
        try:
            from src.yf_cache import get_info as _cached_info
            info     = _cached_info(ticker, ttl=180)
            vol_curr = info.get("volume") or 0
            vol_avg  = (info.get("averageVolume")
                        or info.get("averageDailyVolume10Day")
                        or 0)
            price    = info.get("currentPrice") or info.get("regularMarketPrice") or 0

            if not vol_avg or not vol_curr:
                logger.debug(f"Volume spike {ticker}: no volume data")
                continue

            ratio = vol_curr / vol_avg
            logger.info(f"Volume spike {ticker}: {ratio:.2f}x avg (threshold {threshold}x)")

            if ratio >= threshold and _cooldown_ok_type(ticker, "volume_spike"):
                msg = (
                    f"📊 *ספייק נפח — {ticker}*\n"
                    f"נפח: {vol_curr:,.0f} ({ratio:.1f}× ממוצע)\n"
                    f"💰 מחיר: ${float(price):.2f}\n\n"
                    f"🎯 *תוכנית פעולה:*\n"
                    f"• נפח גבוה = אישור לתנועה — בדוק כיוון\n"
                    f"• שורי + נפח → כניסה עם סטופ קרוב\n"
                    f"• דובי + נפח → מכור / צא\n\n"
                    f"⏰ {datetime.now().strftime('%H:%M')}"
                )
                if item.get("notes"):
                    msg += f"\nהערה: {item['notes']}"
                try:
                    # Telegram suppressed 2026-05-20 — lagging/high-latency indicator. DB kept for audit.
                    watchlist_save_alert(ticker, "volume_spike", msg,
                                         score=None, price=float(price))
                    logger.info(f"Volume spike alert recorded (DB only): {ticker} {ratio:.1f}x")
                except Exception as e:
                    logger.warning(f"Failed to record volume spike alert {ticker}: {e}")

        except Exception as e:
            logger.warning(f"Volume spike check {ticker}: {e}")


def check_supertrend_flips():
    """
    Records Supertrend flips on 1h and Daily timeframes to the DB for audit history.

    Telegram suppressed 2026-05-20 — superseded by combined_buy/sell via ibkr_worker
    (signal_combiner with hysteresis + cross-TF dedup + daily cap). DB writes are
    preserved so the audit trail remains intact.

    Only runs during market hours.
    """
    if not _is_market_hours():
        return

    items = watchlist_get_all()

    if not items:
        return

    from src.supertrend import supertrend as calc_supertrend

    logger.info(f"Supertrend monitor: checking {len(items)} ticker(s) — 1h + daily (DB-only, Telegram suppressed)")
    # Telegram suppressed 2026-05-20 — superseded by combined_buy/sell via ibkr_worker
    # (TelegramNotifier no longer instantiated here — DB writes only)

    for item in items:
        ticker = item["ticker"]
        price  = _get_price(ticker) or 0.0

        # ── Pre-load daily history (used by both 1h guard and daily section) ─────
        try:
            hist_1d_pre = _cached_hist(ticker, period="60d", ttl=1800)
            st_1d_pre   = calc_supertrend(hist_1d_pre.iloc[:-1], lookback=2)
            daily_bearish = st_1d_pre.get("direction") == "Bearish"
        except Exception:
            hist_1d_pre   = None
            daily_bearish = False

        # ── 1-hour ────────────────────────────────────────────────────────────
        try:
            hist_1h = _cached_hist(ticker, period="10d", interval="1h", ttl=600)
            # Drop the last (incomplete, in-progress) bar before computing
            st_1h   = calc_supertrend(hist_1h.iloc[:-1], lookback=3)

            if st_1h["signal"] and _cooldown_ok_type(ticker, "supertrend_1h_flip", hours=2):
                is_buy      = st_1h["signal"] == "BUY"
                # Guard: don't send 1h BUY when daily trend is bearish (falling knife)
                if is_buy and daily_bearish:
                    logger.info(f"Supertrend 1h BUY {ticker}: BLOCKED — daily ST is bearish")
                else:
                    emoji       = "📈" if is_buy else "📉"
                    action_hdr  = "קנה עכשיו" if is_buy else "מכור עכשיו"
                    flip_label  = "שורי" if is_buy else "דובי"
                    bars_ago_1h = st_1h.get("bars_ago", 1)
                    age_str     = f"לפני {bars_ago_1h} שעות" if bars_ago_1h > 1 else "בשעה האחרונה"
                    daily_note  = " | Daily ST: שורי ✅" if not daily_bearish else ""
                    msg = (
                        f"{emoji} *{action_hdr} — {ticker}*\n"
                        f"📊 Supertrend התהפך → {flip_label} [1h]{daily_note}\n"
                        f"⏱ הפיכה: {age_str} | רמה: ${st_1h['level']:.2f}\n"
                        f"💰 מחיר: ${price:.2f}"
                    )
                    if is_buy:
                        hist_for_sizing = hist_1d_pre if hist_1d_pre is not None else hist_1h
                        msg += _build_action_block(price, hist_for_sizing, "3–7 ימים | מגמת 1h")
                        if is_noise_window():
                            msg += "\n⚠️ חלון רעש — המתן לאישור"
                    else:
                        msg += (
                            "\n\n🎯 *תוכנית פעולה:*\n"
                            "• שקול לצאת או להקטין פוזיציה\n"
                            "• הנמך סטופ לשמירת רווחים"
                        )
                    msg += f"\n⏰ {datetime.now().strftime('%H:%M')}"
                    if item.get("notes"):
                        msg += f"\nהערה: {item['notes']}"
                    # Telegram suppressed 2026-05-20 — superseded by combined_buy/sell via ibkr_worker
                    watchlist_save_alert(ticker, "supertrend_1h_flip", msg, score=None, price=price)
                    if is_buy:
                        hold = _ALERT_HOLD_DAYS["supertrend_1h_flip"]
                        alert_trade_open(ticker, "supertrend_1h_flip", price, hold[0], hold[1])
                    else:
                        alert_trade_close(ticker, "supertrend_1h_flip", price, "sell_alert")
                    logger.info(f"Supertrend 1h flip SENT: {ticker} {st_1h['signal']} @ ${price:.2f}")
        except Exception as e:
            logger.warning(f"Supertrend 1h {ticker}: {e}")

        # ── Daily ─────────────────────────────────────────────────────────────
        try:
            # Reuse pre-loaded daily history if available
            hist_1d = hist_1d_pre if hist_1d_pre is not None else _cached_hist(ticker, period="60d", ttl=1800)
            st_1d   = st_1d_pre if hist_1d_pre is not None else calc_supertrend(hist_1d.iloc[:-1], lookback=2)

            if st_1d["signal"] and _cooldown_ok_type(ticker, "supertrend_flip"):
                is_buy      = st_1d["signal"] == "BUY"
                emoji       = "🟢" if is_buy else "🔴"
                action_hdr  = "קנה עכשיו" if is_buy else "מכור עכשיו"
                flip_label  = "שורי" if is_buy else "דובי"
                bars_ago_1d = st_1d.get("bars_ago", 1)
                age_str_1d  = f"לפני {bars_ago_1d} ימים" if bars_ago_1d > 1 else "אתמול"
                msg = (
                    f"{emoji} *{action_hdr} — {ticker}*\n"
                    f"📊 Supertrend התהפך → {flip_label} [Daily]\n"
                    f"⏱ הפיכה: {age_str_1d} | רמה: ${st_1d['level']:.2f}\n"
                    f"💰 מחיר: ${price:.2f}"
                )
                if is_buy:
                    msg += _build_action_block(price, hist_1d, "7–14 ימים | מגמה יומית")
                    if is_noise_window():
                        msg += "\n⚠️ חלון רעש — המתן לאישור"
                else:
                    msg += (
                        "\n\n🎯 *תוכנית פעולה:*\n"
                        "• צא מהפוזיציה\n"
                        "• המתן לאישור שורי לפני כניסה חדשה"
                    )
                msg += f"\n⏰ {datetime.now().strftime('%H:%M')}"
                if item.get("notes"):
                    msg += f"\nהערה: {item['notes']}"
                # Telegram suppressed 2026-05-20 — superseded by combined_buy/sell via ibkr_worker
                watchlist_save_alert(ticker, "supertrend_flip", msg, score=None, price=price)
                if is_buy:
                    hold = _ALERT_HOLD_DAYS["supertrend_flip"]
                    alert_trade_open(ticker, "supertrend_flip", price, hold[0], hold[1])
                else:
                    alert_trade_close(ticker, "supertrend_flip", price, "sell_alert")
                logger.info(f"Supertrend daily flip SENT: {ticker} {st_1d['signal']} @ ${price:.2f}")
        except Exception as e:
            logger.warning(f"Supertrend daily {ticker}: {e}")


def check_rsi_extremes():
    """
    Fires when RSI crosses into oversold (<30) → buy signal, or overbought (>75) → caution.
    Uses 1h bars for detection, daily bars for sizing. Cooldown 4h.
    """
    if not _is_market_hours():
        return
    items = watchlist_get_all()
    if not items:
        return
    # Telegram suppressed 2026-05-20 — lagging/high-latency indicator. DB kept for audit.

    for item in items:
        ticker = item["ticker"]
        price  = _get_price(ticker) or 0.0
        if not price:
            continue
        try:
            hist_1h = _cached_hist(ticker, period="5d", interval="1h", ttl=300)
            if len(hist_1h) < 16:
                continue
            close       = hist_1h["Close"]
            delta       = close.diff()
            gain        = delta.clip(lower=0).rolling(14).mean()
            loss        = (-delta.clip(upper=0)).rolling(14).mean()
            rs          = gain / loss.replace(0, float("nan"))
            rsi_series  = 100 - (100 / (1 + rs))
            rsi_now     = float(rsi_series.iloc[-1])
            rsi_prev    = float(rsi_series.iloc[-2])

            if rsi_now < 30 and rsi_prev >= 30 and _cooldown_ok_type(ticker, "rsi_oversold", hours=4):
                hist_1d = _cached_hist(ticker, period="60d", ttl=1800)
                # Guard 1: don't buy in a freefall — price must be within 15% of SMA50 daily
                close_1d = hist_1d["Close"]
                sma50_1d = float(close_1d.rolling(min(50, len(close_1d))).mean().iloc[-1])
                if price < sma50_1d * 0.85:
                    logger.info(f"RSI oversold {ticker}: BLOCKED — price ${price:.2f} > 15% below SMA50 ${sma50_1d:.2f} (downtrend)")
                    continue
                # Guard 2: don't buy if 1h Supertrend is bearish (falling knife on hourly)
                try:
                    from src.supertrend import supertrend as _calc_st
                    st_1h_check = _calc_st(hist_1h.iloc[:-1], lookback=3)
                    if st_1h_check.get("direction") == "Bearish":
                        logger.info(f"RSI oversold {ticker}: BLOCKED — 1h Supertrend is bearish (direction={st_1h_check['direction']})")
                        continue
                except Exception:
                    pass
                action  = _build_action_block(price, hist_1d, "3–7 ימים | ריבאונד מ-oversold",
                                              "המתן לקנדל ירוק לאישור כניסה")
                noise   = "\n⚠️ חלון רעש — המתן לאישור" if is_noise_window() else ""
                msg = (
                    f"🟢 *קנה עכשיו — {ticker}*\n"
                    f"📊 RSI חצה לאזור oversold | RSI={rsi_now:.1f} | SMA50: ${sma50_1d:.2f}\n"
                    f"💰 מחיר: ${price:.2f}"
                    f"{action}{noise}\n"
                    f"⏰ {datetime.now().strftime('%H:%M')}"
                )
                # Telegram suppressed 2026-05-20 — lagging/high-latency indicator. DB kept for audit.
                watchlist_save_alert(ticker, "rsi_oversold", msg, price=price)
                hold = _ALERT_HOLD_DAYS["rsi_oversold"]
                alert_trade_open(ticker, "rsi_oversold", price, hold[0], hold[1])
                logger.info(f"RSI oversold recorded (DB only): {ticker} RSI={rsi_now:.1f} @ ${price:.2f}")

            elif rsi_now > 80 and rsi_prev <= 80 and _cooldown_ok_type(ticker, "rsi_overbought", hours=4):
                # Guard: don't fire sell if a buy alert was sent in the last 2 hours
                _BUY_ALERT_TYPES = {
                    "rsi_oversold", "macd_bullish", "supertrend_1h_flip",
                    "supertrend_flip", "supertrend_triple_bull", "breakout_alert",
                }
                recent_buy = any(
                    a["alert_type"] in _BUY_ALERT_TYPES and
                    datetime.now() - datetime.fromisoformat(a["sent_at"]) < timedelta(hours=2)
                    for a in watchlist_get_alerts(ticker=ticker, limit=20)
                )
                if recent_buy:
                    logger.info(f"RSI overbought {ticker}: BLOCKED — buy alert sent in last 2h (RSI={rsi_now:.1f})")
                else:
                    msg = (
                        f"🔴 *מכור / הקטן פוזיציה — {ticker}*\n"
                        f"📊 RSI חצה לאזור overbought | RSI={rsi_now:.1f}\n"
                        f"💰 מחיר: ${price:.2f}\n\n"
                        f"🎯 *תוכנית פעולה:*\n"
                        f"• שקול לממש חלק מהפוזיציה עכשיו\n"
                        f"• הצמד סטופ טריילינג\n"
                        f"• אל תיכנס לפוזיציה חדשה\n\n"
                        f"⏰ {datetime.now().strftime('%H:%M')}"
                    )
                    # Telegram suppressed 2026-05-20 — lagging/high-latency indicator. DB kept for audit.
                    watchlist_save_alert(ticker, "rsi_overbought", msg, price=price)
                    alert_trade_close(ticker, "rsi_overbought", price, "sell_alert")
                    logger.info(f"RSI overbought recorded (DB only): {ticker} RSI={rsi_now:.1f} @ ${price:.2f}")

        except Exception as e:
            logger.warning(f"RSI check {ticker}: {e}")


def check_macd_crossover():
    """
    Fires when MACD line crosses signal line on 1h bars. MACD(12,26,9). Cooldown 4h.
    """
    if not _is_market_hours():
        return
    items = watchlist_get_all()
    if not items:
        return
    # Telegram suppressed 2026-05-20 — lagging/high-latency indicator. DB kept for audit.

    for item in items:
        ticker = item["ticker"]
        price  = _get_price(ticker) or 0.0
        if not price:
            continue
        try:
            hist_1h     = _cached_hist(ticker, period="10d", interval="1h", ttl=300)
            if len(hist_1h) < 35:
                continue
            close       = hist_1h["Close"]
            macd_line   = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            macd_now    = float(macd_line.iloc[-1])
            macd_prev   = float(macd_line.iloc[-2])
            sig_now     = float(signal_line.iloc[-1])
            sig_prev    = float(signal_line.iloc[-2])

            if macd_now > sig_now and macd_prev <= sig_prev and _cooldown_ok_type(ticker, "macd_bullish", hours=4):
                # Guard 1: price must be above 20 SMA on 1h (not below the trend)
                sma20_1h = float(close.rolling(20).mean().iloc[-1])
                if price < sma20_1h:
                    logger.info(f"MACD bullish {ticker}: BLOCKED — price ${price:.2f} below SMA20 ${sma20_1h:.2f}")
                    continue
                # Guard 2: volume must be at least 80% of recent average (not a ghost candle)
                vol_now = float(hist_1h["Volume"].iloc[-1])
                vol_avg = float(hist_1h["Volume"].tail(20).mean())
                if vol_avg > 0 and vol_now < vol_avg * 0.8:
                    logger.info(f"MACD bullish {ticker}: BLOCKED — weak volume {vol_now:.0f} < 80% avg {vol_avg:.0f}")
                    continue
                hist_1d = _cached_hist(ticker, period="30d", ttl=1800)
                action  = _build_action_block(price, hist_1d, "5–14 ימים | פולו-טרנד")
                noise   = "\n⚠️ חלון רעש — המתן לאישור" if is_noise_window() else ""
                msg = (
                    f"📈 *קנה עכשיו — {ticker}*\n"
                    f"📊 MACD חצה מעל Signal Line | מחיר מעל MA20 | וולום ✅\n"
                    f"💰 מחיר: ${price:.2f} | SMA20: ${sma20_1h:.2f}"
                    f"{action}{noise}\n"
                    f"⏰ {datetime.now().strftime('%H:%M')}"
                )
                # Telegram suppressed 2026-05-20 — lagging/high-latency indicator. DB kept for audit.
                watchlist_save_alert(ticker, "macd_bullish", msg, price=price)
                hold = _ALERT_HOLD_DAYS["macd_bullish"]
                alert_trade_open(ticker, "macd_bullish", price, hold[0], hold[1])
                logger.info(f"MACD bullish cross recorded (DB only): {ticker} @ ${price:.2f}")

            elif macd_now < sig_now and macd_prev >= sig_prev and _cooldown_ok_type(ticker, "macd_bearish", hours=4):
                msg = (
                    f"📉 *צא מהפוזיציה — {ticker}*\n"
                    f"📊 MACD חצה מתחת ל-Signal Line | מומנטום יורד\n"
                    f"💰 מחיר: ${price:.2f}\n\n"
                    f"🎯 *תוכנית פעולה:*\n"
                    f"• שקול לצאת מהפוזיציה\n"
                    f"• הנמך סטופ לשמירת רווחים\n"
                    f"• אל תיכנס לפוזיציה חדשה לונג\n\n"
                    f"⏰ {datetime.now().strftime('%H:%M')}"
                )
                # Telegram suppressed 2026-05-20 — lagging/high-latency indicator. DB kept for audit.
                watchlist_save_alert(ticker, "macd_bearish", msg, price=price)
                alert_trade_close(ticker, "macd_bearish", price, "sell_alert")
                logger.info(f"MACD bearish cross recorded (DB only): {ticker} @ ${price:.2f}")

        except Exception as e:
            logger.warning(f"MACD check {ticker}: {e}")


def check_supertrend_triple_alignment():
    """
    Fires when 1h + Daily Supertrend both point the same direction simultaneously.
    Much stronger signal than a single flip. Cooldown 4h.
    """
    if not _is_market_hours():
        return
    items = watchlist_get_all()
    if not items:
        return

    from src.supertrend import supertrend as calc_supertrend
    # Telegram suppressed 2026-05-20 — lagging/high-latency indicator. DB kept for audit.

    for item in items:
        ticker = item["ticker"]
        price  = _get_price(ticker) or 0.0
        if not price:
            continue
        try:
            hist_1h = _cached_hist(ticker, period="10d", interval="1h", ttl=600)
            hist_1d = _cached_hist(ticker, period="60d", ttl=1800)
            if len(hist_1h) < 20 or len(hist_1d) < 20:
                continue

            st_1h  = calc_supertrend(hist_1h.iloc[:-1])
            st_1d  = calc_supertrend(hist_1d.iloc[:-1])
            dir_1h = st_1h.get("direction", "")
            dir_1d = st_1d.get("direction", "")

            if dir_1h == "Bullish" and dir_1d == "Bullish":
                if not _cooldown_ok_type(ticker, "supertrend_triple_bull", hours=4):
                    continue
                action = _build_action_block(price, hist_1d, "7–21 ימים | טרנד חזק מאוד",
                                             "כניסה מומלצת — כל הטיימפריים מאושרים")
                msg = (
                    f"🚀 *קנה עכשיו! — {ticker}*\n"
                    f"📊 Supertrend שורי על שני טיימפריים: 1h ✅ + Daily ✅\n"
                    f"💰 מחיר: ${price:.2f} | רמת ST יומי: ${st_1d['level']:.2f}"
                    f"{action}\n"
                    f"⏰ {datetime.now().strftime('%H:%M')}"
                )
                # Telegram suppressed 2026-05-20 — lagging/high-latency indicator. DB kept for audit.
                watchlist_save_alert(ticker, "supertrend_triple_bull", msg, price=price)
                hold = _ALERT_HOLD_DAYS["supertrend_triple_bull"]
                alert_trade_open(ticker, "supertrend_triple_bull", price, hold[0], hold[1])
                logger.info(f"Supertrend triple BULL recorded (DB only): {ticker} @ ${price:.2f}")

            elif dir_1h == "Bearish" and dir_1d == "Bearish":
                if not _cooldown_ok_type(ticker, "supertrend_triple_bear", hours=4):
                    continue
                msg = (
                    f"🔴 *צא עכשיו! — {ticker}*\n"
                    f"📊 Supertrend דובי על שני טיימפריים: 1h ✅ + Daily ✅\n"
                    f"💰 מחיר: ${price:.2f} | רמת ST יומי: ${st_1d['level']:.2f}\n\n"
                    f"🎯 *תוכנית פעולה:*\n"
                    f"• צא מהפוזיציה מיידית\n"
                    f"• אל תיכנס לונג בשלב זה\n"
                    f"• המתן לאישור שורי לפני חזרה\n\n"
                    f"⏰ {datetime.now().strftime('%H:%M')}"
                )
                # Telegram suppressed 2026-05-20 — lagging/high-latency indicator. DB kept for audit.
                watchlist_save_alert(ticker, "supertrend_triple_bear", msg, price=price)
                alert_trade_close(ticker, "supertrend_triple_bear", price, "sell_alert")
                logger.info(f"Supertrend triple BEAR recorded (DB only): {ticker} @ ${price:.2f}")

        except Exception as e:
            logger.warning(f"Supertrend triple alignment {ticker}: {e}")


def check_expired_alert_trades():
    """
    Auto-closes open trades that have passed their hold_days_max.
    Fetches current price and records P&L. Runs in the 5-min monitor loop.
    """
    try:
        open_trades = alert_trade_get_open()
        if not open_trades:
            return
        for trade in open_trades:
            hold_max = trade.get("hold_days_max") or 7
            entry_time = datetime.fromisoformat(trade["entry_time"])
            if datetime.now() < entry_time + timedelta(days=hold_max):
                continue
            price = _get_price(trade["ticker"])
            if price:
                alert_trade_close(trade["ticker"], "expired", price, "expired", trade_id=trade["id"])
                logger.info(
                    f"Alert trade EXPIRED: {trade['ticker']} | "
                    f"entry={trade['entry_price']:.2f} exit={price:.2f} | "
                    f"hold_max={hold_max}d"
                )
    except Exception as e:
        logger.warning(f"check_expired_alert_trades: {e}")


def check_price_surge():
    """
    Rescore watchlist tickers that moved >10% since their last recorded price.
    Fires a Telegram alert with the new score + direction if score >= 55.
    Cooldown: 2h per ticker (alert_type='price_surge_rescore').
    Runs inside the 5-min monitor loop — market hours only.
    """
    if not _is_market_hours():
        return

    from src.stock_scorer import score_stock, signal_label

    notifier = TelegramNotifier()
    items = watchlist_get_all()

    for item in items:
        ticker = item["ticker"]
        try:
            # Find last known price from price_change or any recent alert
            last_price = None
            last_price_age: datetime | None = None
            # Baseline price sources — intentionally excludes supertrend_triple_bull,
            # supertrend_1h_flip (fire at momentum peaks → normal retracements look huge),
            # and score_threshold (written once on watchlist-add, months-stale by design).
            # combined_buy is a real entry signal and a valid baseline reference.
            _BASELINE_TYPES = {"price_change", "combined_buy", "price_surge_rescore"}
            for a in watchlist_get_alerts(ticker=ticker, limit=30):
                if a.get("price") and a["alert_type"] in _BASELINE_TYPES:
                    last_price = float(a["price"])
                    try:
                        last_price_age = datetime.fromisoformat(a["sent_at"])
                    except Exception:
                        pass
                    break

            if not last_price:
                continue

            # Stale baseline guard: if the last price reference is older than 14 days,
            # the comparison is meaningless (long-term drift, not a sudden move).
            # Refresh the baseline silently so future cycles measure from today.
            _MAX_BASELINE_AGE_DAYS = 14
            if last_price_age and (datetime.now() - last_price_age).days > _MAX_BASELINE_AGE_DAYS:
                price = _get_price(ticker)
                if price and price > 0:
                    watchlist_save_alert(
                        ticker, "price_surge_rescore",
                        f"[baseline refresh] {ticker} @ ${price:.2f}",
                        score=None, price=price,
                    )
                    logger.info(
                        f"price_surge_rescore: {ticker} baseline too stale "
                        f"({(datetime.now() - last_price_age).days}d old, was ${last_price:.2f}) "
                        f"— refreshed to ${price:.2f}, skipping alert this cycle"
                    )
                continue

            price = _get_price(ticker)
            if not price or price <= 0:
                continue

            pct_move = (price - last_price) / last_price * 100
            if abs(pct_move) < 10.0:
                continue

            # Big move detected — check cooldown before rescoring
            if not _cooldown_ok_type(ticker, "price_surge_rescore", hours=2):
                continue

            logger.info(f"Price surge detected: {ticker} {pct_move:+.1f}% (${last_price:.2f} → ${price:.2f}) — rescoring")

            r = score_stock(ticker)
            if not r:
                continue

            score   = r["score"]
            signal  = signal_label(score)
            arrow   = "📈" if pct_move > 0 else "📉"
            emoji   = "🟢" if score >= 60 else ("🟡" if score >= 45 else "🔴")

            msg = (
                f"{arrow} *תנועה חדה — {ticker}*\n"
                f"מחיר: ${price:.2f} | שינוי: {pct_move:+.1f}% מ-${last_price:.2f}\n"
                f"ציון מחודש: {emoji} {score:.0f}/100 ({signal})\n"
            )

            if score >= 60:
                hist = _cached_hist(ticker, period="30d", ttl=1800)
                msg += _build_action_block(price, hist, "תנועה חדה — בדוק כניסה")
            elif score < 40 and pct_move < 0:
                msg += "🎯 שקול יציאה — ציון וגם מחיר ירדו."
            else:
                msg += "🎯 עקוב אחר המשך — ציון לא מצדיק כניסה עדיין."

            watchlist_save_alert(ticker, "price_surge_rescore", msg,
                                 score=score, price=price)

            # Only send Telegram if score is meaningful
            if score >= 55:
                notifier.send_message(msg)
                logger.info(f"price_surge_rescore Telegram sent: {ticker} score={score:.0f}")
            else:
                logger.info(f"price_surge_rescore DB-only (score {score:.0f} < 55): {ticker}")

        except Exception as e:
            logger.warning(f"check_price_surge {ticker}: {e}")


def run_monitor(interval_minutes: int = 5):
    """Standalone loop — for testing or running independently."""
    logger.info(f"Price alert monitor standalone — every {interval_minutes} min")
    while True:
        try:
            check_price_targets()
            check_volume_spikes()
            check_supertrend_flips()
            check_rsi_extremes()
            check_macd_crossover()
            check_supertrend_triple_alignment()
            check_expired_alert_trades()
            check_price_surge()
        except Exception as e:
            logger.error(f"Price monitor error: {e}")
        time.sleep(interval_minutes * 60)


if __name__ == "__main__":
    import sys
    mins = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    run_monitor(mins)
