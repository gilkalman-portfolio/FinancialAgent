"""
Telegram News Digest
Sends two types of Telegram messages:
  1. Daily market digest — top headlines + market mood + indices
  2. Portfolio news alert — recent news for holdings in portfolio
"""

import os
from datetime import datetime
from loguru import logger
from dotenv import load_dotenv
load_dotenv()

from src.telegram_notifier import TelegramNotifier
from src.database import portfolio_get_all


def send_market_digest():
    """
    Sends a morning market digest to Telegram:
    - Market mood (bullish/bearish score)
    - Top 5 headlines
    - Key indices snapshot
    """
    try:
        from src.market_feed import get_market_news, get_market_mood, get_market_indices
        telegram = TelegramNotifier()

        articles = get_market_news(20)
        mood     = get_market_mood(articles)
        indices  = get_market_indices()

        # ── Mood line ──────────────────────────────────────────────────────────
        mood_label_he = {"Bullish": "שורי", "Bearish": "דובי"}.get(mood["label"], "ניטרלי")
        mood_emoji = "🟢" if mood["label"] == "Bullish" else "🔴" if mood["label"] == "Bearish" else "🟡"
        lines = [
            f"📰 *סיכום שוק — {datetime.now().strftime('%a %d %b %H:%M')}*",
            "",
            f"{mood_emoji} *מצב השוק: {mood_label_he}* ({mood['score']}% שורי)",
            f"📊 {mood['bullish']} שורי · {mood['neutral']} ניטרלי · {mood['bearish']} דובי",
            "",
        ]

        # ── Indices ────────────────────────────────────────────────────────────
        lines.append("*מדדים עיקריים:*")
        for idx in indices[:6]:  # S&P, Nasdaq, Dow, Russell, VIX, Bitcoin
            arrow = "▲" if idx["up"] else "▼"
            price_fmt = f"{idx['price']:,.0f}" if idx["price"] > 1000 else f"{idx['price']:.2f}"
            lines.append(f"  {arrow} {idx['name']}: {price_fmt} ({idx['change']:+.2f}%)")

        lines.append("")
        lines.append("*כותרות מובילות:*")

        # ── Top 5 headlines by sentiment strength ─────────────────────────────
        priority = {"Bullish": 0, "Bearish": 1, "Somewhat-Bullish": 2, "Somewhat-Bearish": 3, "Neutral": 4}
        sorted_articles = sorted(articles, key=lambda a: priority.get(a.get("sentiment", "Neutral"), 4))
        for a in sorted_articles[:5]:
            sent = a.get("sentiment", "Neutral")
            emoji = "🟢" if "Bullish" in sent and "Somewhat" not in sent else \
                    "🟡" if "Somewhat" in sent else \
                    "🔴" if "Bearish" in sent else "⚪"
            headline = a.get("headline", "")[:150]
            lines.append(f"  {emoji} {headline}")

        msg = "\n".join(lines)
        telegram.send_message(msg, parse_mode="Markdown")
        logger.info("Market digest sent to Telegram")

    except Exception as e:
        logger.error(f"Market digest failed: {e}")


def send_portfolio_news():
    """
    Sends recent news for all portfolio holdings to Telegram.
    One message per holding with relevant headlines.
    """
    try:
        telegram  = TelegramNotifier()
        holdings  = portfolio_get_all()
        if not holdings:
            logger.info("Portfolio news: no holdings")
            return

        lines = [f"💼 *חדשות תיק — {datetime.now().strftime('%a %d %b')}*", ""]
        found_any = False

        for item in holdings:
            ticker = item["ticker"]
            try:
                from src.news_fetcher import get_ticker_news
                articles_raw = get_ticker_news(ticker, days=2)
                if not articles_raw:
                    continue

                found_any = True
                lines.append(f"*{ticker}* ({len(articles_raw)} כתבות):")
                for a in articles_raw[:3]:
                    headline = a.get("headline", "")[:150]
                    pub = a.get("published", "")
                    prefix = f"[{pub}] " if pub else ""
                    lines.append(f"  • {prefix}{headline}")
                lines.append("")

            except Exception as e:
                logger.debug(f"Portfolio news {ticker}: {e}")

        if not found_any:
            logger.info("Portfolio news: no news found for any holding")
            return

        telegram.send_message("\n".join(lines), parse_mode="Markdown")
        logger.info(f"Portfolio news digest sent for {len(holdings)} holdings")

    except Exception as e:
        logger.error(f"Portfolio news failed: {e}")
