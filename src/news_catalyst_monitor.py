"""
News Catalyst Monitor
=====================
Thread שרץ ברקע ובודק חדשות כל N דקות עבור portfolio + watchlist.
מתריע ב-Telegram רק על catalysts משמעותיים (שקט ומדויק).

זרימה:
  1. שלוף חדשות מכל המקורות (news_fetcher)
  2. סנן headlines חדשים בלבד (dedup ב-DB)
  3. catalyst_score מהיר על ה-headline — ≥ threshold בלבד ממשיכים
  4. LLM analysis (run_full_analysis) — מוגבל ל-MAX_LLM_PER_CYCLE
  5. חשב direct impact על הטיקר הספציפי מה-portfolio/watchlist
  6. שלח Telegram עם context מלא
"""

import time
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from loguru import logger

from src.database import (
    portfolio_get_all, watchlist_get_all,
    news_seen_contains, news_seen_add, news_seen_cleanup,
    watchlist_save_alert,
)
from src.telegram_notifier import TelegramNotifier
from src.news_fetcher import get_ticker_news, get_market_news, catalyst_score

# ── Config defaults (overridden by scheduler_config.json) ─────────────────────
DEFAULT_INTERVAL_MINUTES   = 15
DEFAULT_CATALYST_THRESHOLD = 3    # catalyst_score ≥ this → send to LLM
DEFAULT_MAX_LLM_PER_CYCLE  = 3    # max LLM calls per interval (cost control)
DEFAULT_SCOPE              = "portfolio+watchlist"  # or "portfolio", "watchlist"
DEFAULT_MAX_ARTICLE_AGE_MIN = 45  # skip articles published >45 min ago — avoids
                                  # reactive "X soared after..." headlines that are
                                  # already priced in by the time we detect them


def _headline_key(headline: str, ticker: str) -> str:
    """Stable dedup key — first 80 chars of headline + ticker."""
    raw = f"{ticker}:{headline[:80].lower().strip()}"
    return hashlib.md5(raw.encode()).hexdigest()


def _get_tracked_tickers(scope: str) -> Dict[str, Dict]:
    """
    Returns {ticker: {source, entry_price, shares, ...}} for all tracked tickers.
    source = "portfolio" | "watchlist"
    """
    tickers: Dict[str, Dict] = {}

    if "portfolio" in scope:
        for item in portfolio_get_all():
            tickers[item["ticker"]] = {
                "source":      "portfolio",
                "entry_price": item.get("entry_price"),
                "shares":      item.get("shares", 0),
                "stop_loss":   item.get("stop_loss"),
                "target_price": item.get("target_price"),
                "notes":       item.get("notes", ""),
            }

    if "watchlist" in scope:
        for item in watchlist_get_all():
            if item["ticker"] not in tickers:  # portfolio takes precedence
                tickers[item["ticker"]] = {
                    "source":      "watchlist",
                    "entry_price": None,
                    "shares":      0,
                    "notes":       item.get("notes", ""),
                }

    return tickers


def _get_current_price(ticker: str) -> Optional[float]:
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).fast_info
        return float(getattr(info, "last_price", None) or 0) or None
    except Exception:
        return None


def _portfolio_context(ticker: str, info: Dict, current_price: Optional[float]) -> str:
    """Build context string for portfolio positions."""
    if info["source"] != "portfolio" or not info.get("entry_price"):
        return ""
    entry = info["entry_price"]
    shares = info.get("shares", 0)
    if current_price and entry:
        pnl_pct = (current_price - entry) / entry * 100
        pnl_val = (current_price - entry) * shares if shares else 0
        pnl_str = f"{pnl_pct:+.1f}% (${pnl_val:+.0f})" if shares else f"{pnl_pct:+.1f}%"
    else:
        pnl_str = "N/A"
    parts = [f"Entry: ${entry:.2f}", f"P&L: {pnl_str}"]
    if info.get("stop_loss"):
        parts.append(f"Stop: ${info['stop_loss']:.2f}")
    if info.get("target_price"):
        parts.append(f"Target: ${info['target_price']:.2f}")
    return " | ".join(parts)


def _impact_on_ticker(ticker: str, analysis: Dict, tracker_info: Dict) -> Optional[Dict]:
    """
    מחפש את הטיקר הספציפי ב-affected list מה-LLM.
    אם לא נמצא — בודק אם ה-sentiment הכללי מספיק חזק.
    מחזיר dict עם impact/reason/layer, או None אם לא רלוונטי.
    """
    affected = analysis.get("affected", [])

    # חיפוש ישיר
    for item in affected:
        if item.get("ticker", "").upper() == ticker.upper():
            return {
                "ticker":    ticker,
                "impact":    item.get("impact", "neutral"),
                "layer":     item.get("layer", "direct"),
                "reason":    item.get("reason", ""),
                "magnitude": item.get("magnitude", 1),
                "direct":    True,
            }

    # לא נמצא ישירות — אם ה-LLM sentiment חזק + זה portfolio, עדיין שווה לדעת
    sentiment = analysis.get("sentiment", "neutral")
    if sentiment == "neutral":
        return None
    # רק אם magnitude ממוצע של affected מעל 3
    avg_mag = sum(a.get("magnitude", 1) for a in affected) / len(affected) if affected else 0
    if avg_mag < 3:
        return None

    return {
        "ticker":    ticker,
        "impact":    "positive" if sentiment == "positive" else "negative",
        "layer":     "macro",
        "reason":    f"General market {sentiment} sentiment | Avg magnitude {avg_mag:.1f}",
        "magnitude": int(avg_mag),
        "direct":    False,
    }


def _escape_md(text: str) -> str:
    """Escape Telegram Markdown v1 special chars to prevent parse errors."""
    for ch in ("*", "_", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def _build_telegram_message(
    ticker: str,
    tracker_info: Dict,
    headline: str,
    article_url: str,
    source: str,
    analysis: Dict,
    impact: Dict,
    current_price: Optional[float],
) -> str:
    """Builds the Telegram alert message."""
    sentiment = analysis.get("sentiment", "neutral")
    summary   = analysis.get("summary", "")

    # Header
    impact_emoji = "🔴" if impact["impact"] == "negative" else "🟢" if impact["impact"] == "positive" else "🟡"
    source_badge = "📁 תיק השקעות" if tracker_info["source"] == "portfolio" else "👁 רשימת מעקב"
    layer_map = {"direct": "ישיר", "competitor": "מתחרה", "supply_chain": "שרשרת אספקה",
                 "macro": "מאקרו", "sector": "סקטור"}
    layer_he = layer_map.get(impact.get("layer", ""), impact.get("layer", ""))

    safe_headline = _escape_md(headline[:150])
    safe_summary  = _escape_md(summary)

    lines = [
        f"{impact_emoji} *התראת חדשות — {ticker}*",
        f"{source_badge} | קשר: {layer_he}",
        "",
        f"📰 _{safe_headline}_",
        f"🔗 {article_url}" if article_url else "",
        f"📡 {source}" if source else "",
        "",
    ]

    # Summary
    if summary:
        lines += [f"📋 *סיכום:*", safe_summary, ""]

    # Direct impact on this ticker
    if impact.get("reason"):
        lines += [f"💥 *השפעה על {ticker}:* {impact['reason']}", ""]

    # Portfolio context
    ctx = _portfolio_context(ticker, tracker_info, current_price)
    if ctx:
        price_str = f"${current_price:.2f}" if current_price else "N/A"
        lines += [f"💼 *פוזיציה:* מחיר={price_str} | {ctx}", ""]

    # Magnitude
    mag = impact.get("magnitude", 1)
    stars = "●" * mag + "○" * (5 - mag)
    lines.append(f"⚡ עוצמה: {stars} ({mag}/5)")

    # Other affected tickers (brief)
    other_affected = [
        a for a in analysis.get("affected", [])
        if a.get("ticker", "").upper() != ticker.upper()
    ][:4]
    if other_affected:
        lines.append("")
        lines.append("*גם מושפעים:* " + " | ".join(
            f"{a['ticker']} ({'↑' if a['impact'] == 'positive' else '↓'})"
            for a in other_affected
        ))

    lines.append(f"\n⏰ {datetime.now().strftime('%H:%M')}")

    return "\n".join(l for l in lines if l is not None)


def run_catalyst_check(
    catalyst_threshold: int = DEFAULT_CATALYST_THRESHOLD,
    max_llm_calls: int = DEFAULT_MAX_LLM_PER_CYCLE,
    scope: str = DEFAULT_SCOPE,
    force: bool = False,
    max_article_age_minutes: int = DEFAULT_MAX_ARTICLE_AGE_MIN,
) -> int:
    """
    Single check cycle. Returns number of alerts sent.
    force=True bypasses the dedup cache (used for manual runs).
    max_article_age_minutes: articles older than this are skipped — prevents
    sending alerts on reactive "X soared after..." headlines already priced in.
    """
    from src.news_impact_analyzer import run_full_analysis

    tracked = _get_tracked_tickers(scope)
    if not tracked:
        logger.info("[CatalystMonitor] No tracked tickers")
        return 0

    logger.info(f"[CatalystMonitor] Checking {len(tracked)} tickers (scope={scope}, max_age={max_article_age_minutes}m)")

    # Freshness cutoff — articles published before this timestamp are skipped
    freshness_cutoff_ts = (datetime.now() - timedelta(minutes=max_article_age_minutes)).timestamp()

    telegram    = TelegramNotifier()
    llm_calls   = 0
    alerts_sent = 0

    for ticker, tracker_info in tracked.items():
        try:
            articles = get_ticker_news(ticker, days=1, limit=20)
            cutoff_ts = (datetime.now() - timedelta(hours=24)).timestamp()
            articles = [a for a in articles if a.get("ts", 0) >= cutoff_ts or a.get("ts", 0) == 0]
            new_articles = []

            for article in articles:
                headline = article.get("headline", "")
                if not headline:
                    continue

                # Freshness gate — skip stale articles (already priced in)
                article_ts = article.get("ts", 0)
                if article_ts and article_ts < freshness_cutoff_ts:
                    logger.debug(
                        f"[CatalystMonitor] {ticker}: skipping stale article "
                        f"({int((datetime.now().timestamp() - article_ts) / 60)}m old) — '{headline[:60]}'"
                    )
                    continue

                key   = _headline_key(headline, ticker)
                cscore = catalyst_score(headline)

                if not force and news_seen_contains(key):
                    continue  # already processed

                # Mark as seen regardless of threshold (avoid re-checking low-score headlines)
                news_seen_add(key, ticker, cscore)

                if cscore >= catalyst_threshold:
                    new_articles.append((article, cscore))
                    logger.info(f"[CatalystMonitor] {ticker}: catalyst={cscore} — '{headline[:60]}'")

            if not new_articles:
                continue

            # Sort by catalyst score, take the strongest
            new_articles.sort(key=lambda x: x[1], reverse=True)
            top_article, top_score = new_articles[0]

            # LLM budget check
            if llm_calls >= max_llm_calls:
                logger.warning(f"[CatalystMonitor] LLM budget ({max_llm_calls}) reached — skipping {ticker}")
                continue

            headline = top_article.get("headline", "")
            url      = top_article.get("url", "")
            source   = top_article.get("source", "")

            logger.info(f"[CatalystMonitor] Running LLM for {ticker}: '{headline[:60]}'")
            llm_calls += 1

            try:
                from concurrent.futures import ThreadPoolExecutor, TimeoutError as _Timeout
                with ThreadPoolExecutor(max_workers=1) as _ex:
                    _fut = _ex.submit(run_full_analysis, headline)
                    analysis = _fut.result(timeout=45)
            except _Timeout:
                logger.warning(f"[CatalystMonitor] LLM timeout (45s) for {ticker} — skipping")
                continue
            except Exception as _e:
                logger.warning(f"[CatalystMonitor] LLM error for {ticker}: {_e}")
                continue
            if not analysis or analysis.get("error"):
                logger.warning(f"[CatalystMonitor] LLM failed for {ticker}: {analysis.get('error') if analysis else 'None'}")
                continue

            impact = _impact_on_ticker(ticker, analysis, tracker_info)
            if not impact:
                logger.info(f"[CatalystMonitor] {ticker}: no significant impact found — skipping alert")
                continue

            current_price = _get_current_price(ticker)

            msg = _build_telegram_message(
                ticker       = ticker,
                tracker_info = tracker_info,
                headline     = headline,
                article_url  = url,
                source       = source,
                analysis     = analysis,
                impact       = impact,
                current_price= current_price,
            )

            if telegram.send_message(msg, parse_mode="Markdown"):
                alerts_sent += 1
                # Save to watchlist_alerts for history
                watchlist_save_alert(
                    ticker     = ticker,
                    alert_type = "news_catalyst",
                    message    = f"[{impact['impact'].upper()}] {headline[:120]}",
                    price      = current_price,
                )
                logger.info(f"[CatalystMonitor] Alert sent for {ticker} | impact={impact['impact']} | layer={impact['layer']}")
            else:
                logger.warning(f"[CatalystMonitor] Telegram send failed for {ticker}")

        except Exception as e:
            logger.error(f"[CatalystMonitor] Error processing {ticker}: {e}")

    # Cleanup old seen headlines weekly
    try:
        news_seen_cleanup(days=7)
    except Exception:
        pass

    logger.info(f"[CatalystMonitor] Cycle complete | llm_calls={llm_calls} | alerts_sent={alerts_sent}")
    return alerts_sent


def catalyst_monitor_thread(
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    catalyst_threshold: int = DEFAULT_CATALYST_THRESHOLD,
    max_llm_calls: int = DEFAULT_MAX_LLM_PER_CYCLE,
    scope: str = DEFAULT_SCOPE,
    max_article_age_minutes: int = DEFAULT_MAX_ARTICLE_AGE_MIN,
):
    """
    Thread target — runs catalyst check every N minutes.
    Call from scheduler.py as daemon thread.
    """
    logger.info(
        f"[CatalystMonitor] Started — interval={interval_minutes}m "
        f"threshold={catalyst_threshold} max_llm={max_llm_calls} scope={scope} "
        f"max_age={max_article_age_minutes}m"
    )
    while True:
        try:
            from src.price_alert_monitor import _is_market_hours
            if _is_market_hours():
                run_catalyst_check(
                    catalyst_threshold      = catalyst_threshold,
                    max_llm_calls           = max_llm_calls,
                    scope                   = scope,
                    max_article_age_minutes = max_article_age_minutes,
                )
            else:
                logger.info("[CatalystMonitor] Outside market hours — skipping cycle")
        except Exception as e:
            logger.error(f"[CatalystMonitor] Unhandled error in cycle: {e}")
        time.sleep(interval_minutes * 60)
