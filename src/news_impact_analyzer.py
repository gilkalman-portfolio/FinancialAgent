"""
News Impact Analyzer - ניתוח השפעת חדשות על מניות
ארכיטקטורה (3 שכבות):
  1. LLM  — מזהה חברות מפורשות + macro signals מהטקסט
  2. Macro — מטריצת קורלציות hardcoded ממפה signals → סקטורים/מניות
  3. Grounding — נתוני מחיר אמיתיים (RSI/short%/volume) מאמתים כל המלצה

אפס המצאות — כל טיקר עובר אימות yfinance לפני שמוצג.
"""

import os
import json
import html as _html_mod
import requests
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

# Sentiment keywords moved to src/news_fetcher.py (centralized)

# ── Sector ETF mapping — לחישוב RS ─────────────────────────────────────────────

SECTOR_ETFS = {
    'Information Technology': 'XLK',
    'Health Care': 'XLV',
    'Financials': 'XLF',
    'Consumer Discretionary': 'XLY',
    'Communication Services': 'XLC',
    'Industrials': 'XLI',
    'Consumer Staples': 'XLP',
    'Energy': 'XLE',
    'Utilities': 'XLU',
    'Real Estate': 'XLRE',
    'Materials': 'XLB',
}


def analyze_sentiment(text: str) -> Dict[str, Any]:
    """Delegate to centralized news_fetcher sentiment."""
    from src.news_fetcher import keyword_sentiment
    return keyword_sentiment(text)


def get_sector_peers(ticker: str, limit: int = 5) -> List[Dict[str, str]]:
    """
    מחזיר מניות מאותו sector כ'מניות קשורות עקיפות'.
    משתמש ב-yfinance info.
    """
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        sector = info.get('sector', '')
        if not sector:
            return []

        # טוען מניות מהindex cache
        from src.index_loader import get_index
        df = get_index('S&P 500')
        if df is None:
            return []

        peers = df[df['sector'].str.lower() == sector.lower()]['ticker'].tolist()
        peers = [p for p in peers if p != ticker][:limit]

        return [{'ticker': p, 'relation': 'same_sector', 'sector': sector} for p in peers]
    except Exception as e:
        logger.debug(f"Sector peers failed for {ticker}: {e}")
        return []


def _resolve_ticker(company_name: str) -> Optional[str]:
    """
    מאמת שם חברה → טיקר אמיתי דרך yfinance.
    מחזיר None אם לא נמצא או לא סחיר.
    """
    try:
        import yfinance as yf
        ticker = company_name.strip().upper()
        info = yf.Ticker(ticker).fast_info
        # אם יש מחיר אמיתי — הטיקר קיים
        price = getattr(info, 'last_price', None)
        if price and price > 0:
            return ticker
        return None
    except Exception:
        return None


def _real_magnitude(ticker: str) -> int:
    """
    מחשב magnitude 1-5 מנתוני מחיר אמיתיים:
    - volatility (ATR/price) אחרון 20 ימים
    - short interest אם זמין
    """
    try:
        import yfinance as yf
        import numpy as np
        hist = yf.Ticker(ticker).history(period="1mo")
        if hist.empty or len(hist) < 5:
            return 2
        pct_changes = hist['Close'].pct_change().dropna().abs()
        avg_vol = pct_changes.mean()
        # ממפה volatility → magnitude
        if avg_vol >= 0.04:   return 5
        elif avg_vol >= 0.03: return 4
        elif avg_vol >= 0.02: return 3
        elif avg_vol >= 0.01: return 2
        else:                 return 1
    except Exception:
        return 2


def llm_analyze_news(article_text: str) -> Optional[Dict[str, Any]]:
    """
    שולח את הטקסט ל-LLM — מחלץ:
    1. חברות מפורשות מהטקסט
    2. macro signals (oil_up, rates_up, inflation_high, ...)
    3. surprise_factor — האם הידיעה מפתיעה?

    LLM לא ממציא טיקרים. טיקרים ו-magnitude מאומתים מנתונים אמיתיים.
    """
    from src.llm_client import llm_complete
    from src.macro_signals import get_signals_for_llm_prompt

    available_signals = get_signals_for_llm_prompt()

    prompt = f"""You are a financial news analyst. Extract structured data from the article.

Article:
{article_text[:6000]}

Return JSON only, no other text, no markdown, no HTML tags in any field:
{{
  "summary": "2-sentence summary in Hebrew — plain text only, no HTML",
  "sentiment": "positive/negative/neutral",
  "surprise_factor": true/false,
  "companies": [
    {{
      "name": "Company Name",
      "ticker": "TICKER_IF_EXPLICITLY_STATED_IN_ARTICLE",
      "impact": "positive/negative/neutral",
      "layer": "direct/competitor/supply_chain",
      "reason": "קצר בעברית — plain text only, no HTML"
    }}
  ],
  "macro_signals": [
    {{
      "signal": "signal_key_from_list_below",
      "direction": "positive/negative",
      "surprise": true/false,
      "reason": "why this signal applies"
    }}
  ],
  "sectors": [
    {{
      "sector": "Sector Name",
      "impact": "positive/negative/neutral",
      "reason": "קצר בעברית — plain text only, no HTML"
    }}
  ]
}}

Rules for companies:
- Only include companies EXPLICITLY mentioned in the article
- ticker: fill ONLY if the article states it explicitly, else empty string ""
- Do NOT invent tickers

Available macro signal keys (pick all that apply):
{available_signals}

Rules for macro_signals:
- Pick ONLY signals clearly indicated by the article
- surprise=true if the news beat/missed expectations or was unexpected
- direction: positive = signal is occurring in its positive form (e.g., oil going UP)"""

    raw = ""
    try:
        raw = llm_complete(prompt, max_tokens=2500)
        logger.info(f"[NewsAnalyzer] LLM raw response length: {len(raw)} chars")
        logger.debug(f"[NewsAnalyzer] LLM raw response: {raw[:500]}")
        cleaned = raw.replace('```json', '').replace('```', '').strip()
        result = json.loads(cleaned)
        companies = result.get('companies', [])
        macro_sigs = result.get('macro_signals', [])
        sectors = result.get('sectors', [])
        logger.info(f"[NewsAnalyzer] LLM extracted: {len(companies)} companies, {len(macro_sigs)} macro signals, {len(sectors)} sectors, sentiment={result.get('sentiment')}")
        return result
    except json.JSONDecodeError as e:
        logger.error(f"[NewsAnalyzer] LLM JSON parse failed: {e} | raw: {raw[:300] if raw else '(no response)'}")
        return None
    except Exception as e:
        logger.error(f"[NewsAnalyzer] LLM news analysis failed: {e}")
        return None


def _get_sector_tickers(sector_name: str, max_stocks: int = 10) -> List[str]:
    """
    מחזיר רשימת טיקרים אמיתיים מהindex לפי שם סקטור.
    משתמש ב-index_loader שכבר מכיל את כל הנתונים.
    """
    try:
        from src.index_loader import get_index
        # מנסה S&P 500 קודם, אחר כך Russell 2000
        for index_name in ["S&P 500", "Russell 2000", "Nasdaq 100"]:
            df = get_index(index_name)
            if df is None or df.empty:
                continue
            mask = df["sector"].str.lower().str.contains(sector_name.lower(), na=False)
            tickers = df[mask]["ticker"].dropna().tolist()
            if tickers:
                return tickers[:max_stocks]
        return []
    except Exception as e:
        logger.debug(f"Sector tickers failed for {sector_name}: {e}")
        return []


def _score_ticker_for_news(ticker: str, news_sentiment: str) -> Optional[Dict]:
    """
    שולף נתוני מחיר אמיתיים לטיקר ומחשב האם החדשה + הנתונים = הזדמנות.
    מחזיר dict עם הנתונים או None אם הטיקר לא זמין.

    לוגיקה:
    - חדשה חיובית + RSI נמוך (oversold) = הזדמנות חזקה
    - חדשה חיובית + short interest גבוה = squeeze potential
    - חדשה שלילית + RSI גבוה (overbought) = סיכון גדול
    - volume spike = confirmation
    """
    try:
        import yfinance as yf
        import numpy as np

        ticker_obj = yf.Ticker(ticker)
        hist = ticker_obj.history(period="3mo")
        if hist.empty or len(hist) < 10:
            return None

        close = hist["Close"]
        volume = hist["Volume"]

        # RSI
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, float('nan'))
        rsi = round(float(100 - (100 / (1 + rs.iloc[-1]))), 1)

        # Volume spike vs 20-day avg
        vol_avg = float(volume.iloc[-21:-1].mean())
        vol_today = float(volume.iloc[-1])
        vol_ratio = round(vol_today / vol_avg, 2) if vol_avg > 0 else 1.0

        # Short interest
        info = ticker_obj.fast_info
        short_pct = getattr(info, 'short_percent_of_float', None)
        short_pct = round(float(short_pct) * 100, 1) if short_pct else None

        # חישוב opportunity_score — כמה החדשה רלוונטית לנתונים
        opportunity = "neutral"
        magnitude = 2

        if news_sentiment == "positive":
            if rsi < 40:                          # oversold + חדשה טובה
                opportunity = "strong_buy"
                magnitude = 5
            elif rsi < 55 and short_pct and short_pct > 15:  # squeeze potential
                opportunity = "squeeze_candidate"
                magnitude = 4
            elif rsi < 60:
                opportunity = "buy"
                magnitude = 3
            elif rsi > 75:                         # כבר overbought — מתומחר
                opportunity = "already_priced_in"
                magnitude = 1

        elif news_sentiment == "negative":
            if rsi > 65:                           # overbought + חדשה רעה
                opportunity = "strong_sell"
                magnitude = 5
            elif rsi > 50:
                opportunity = "sell"
                magnitude = 3
            elif rsi < 35:                         # כבר oversold — מתומחר
                opportunity = "already_priced_in"
                magnitude = 1

        return {
            "ticker":      ticker,
            "rsi":         rsi,
            "vol_ratio":   vol_ratio,
            "short_pct":   short_pct,
            "opportunity": opportunity,
            "magnitude":   magnitude,
        }

    except Exception as e:
        logger.debug(f"Score ticker failed for {ticker}: {e}")
        return None


def analyze_sector_impact(sectors: List[Dict], article_sentiment: str, max_per_sector: int = 8) -> List[Dict]:
    """
    לכל סקטור שזוהה בכתבה:
    1. שולף טיקרים אמיתיים מהindex
    2. בודק נתוני מחיר לכל טיקר
    3. מחזיר רק מניות שיש להן opportunity אמיתית לפי הנתונים

    זה grounded לחלוטין — אפס המצאות.
    """
    results = []
    for sector_info in sectors:
        sector_name = sector_info.get("sector", "")
        sector_sentiment = sector_info.get("impact", article_sentiment)
        if not sector_name:
            continue

        tickers = _get_sector_tickers(sector_name, max_stocks=max_per_sector)
        if not tickers:
            logger.debug(f"No tickers found for sector: {sector_name}")
            continue

        logger.info(f"Analyzing {len(tickers)} tickers for sector: {sector_name}")

        for ticker in tickers:
            scored = _score_ticker_for_news(ticker, sector_sentiment)
            if not scored:
                continue
            # מסנן מניות שכבר מתומחרות או neutral
            if scored["opportunity"] in ("already_priced_in", "neutral"):
                continue

            results.append({
                "ticker":    scored["ticker"],
                "company":   scored["ticker"],
                "impact":    sector_sentiment,
                "magnitude": scored["magnitude"],
                "layer":     "sector",
                "reason":    (
                    f"{sector_name} | {scored['opportunity']} | "
                    f"RSI={scored['rsi']}"
                    + (f" | Short={scored['short_pct']}%" if scored['short_pct'] else "")
                    + (f" | Vol×{scored['vol_ratio']}" if scored['vol_ratio'] > 1.5 else "")
                ),
                "rsi":       scored["rsi"],
                "vol_ratio": scored["vol_ratio"],
                "short_pct": scored["short_pct"],
                "opportunity": scored["opportunity"],
            })

    # מיון לפי magnitude ואחר כך opportunity
    results.sort(key=lambda x: x["magnitude"], reverse=True)
    return results


def get_ticker_news(ticker: str, days: int = 7) -> List[Dict]:
    """
    מחזיר חדשות לטיקר — delegate ל-news_fetcher המרכזי.
    """
    from src.news_fetcher import get_ticker_news as _fetch
    return _fetch(ticker, days=days)

def run_full_analysis(article_text: str) -> Dict[str, Any]:
    """
    Entry point — 3 שכבות:

    שכבה 1: LLM
      - מזהה חברות מפורשות + macro signals + surprise_factor
      - טיקרים מאומתים ב-yfinance, magnitude מ-volatility אמיתית

    שכבה 2: Macro Correlation Matrix
      - ממפה macro signals → רשימת טיקרים/סקטורים מושפעים (hardcoded, מבוסס היסטורי)
      - surprise_factor מכפיל את העוצמה ב-1.5x

    שכבה 3: Grounding
      - כל טיקר ממטריצת המאקרו עובר בדיקת RSI/short%/volume אמיתיים
      - רק מניות עם opportunity אמיתית עוברות (מסנן already_priced_in ו-neutral)
    """
    import re
    from src.macro_signals import get_affected_by_signals, MACRO_CORRELATION_MATRIX

    def _sanitize(text: str) -> str:
        """Strip HTML tags that LLM might have included."""
        return re.sub(r'<[^>]+>', '', str(text)).strip()

    llm_result = llm_analyze_news(article_text)
    if not llm_result:
        return {'error': 'LLM analysis failed', 'affected': [], 'sectors': [], 'summary': ''}

    article_sentiment = llm_result.get('sentiment', 'neutral')
    sectors           = llm_result.get('sectors', [])
    summary           = _sanitize(llm_result.get('summary', ''))  # Strip HTML tags from LLM
    macro_signals_raw = llm_result.get('macro_signals', [])
    surprise_factor   = llm_result.get('surprise_factor', False)
    logger.info(f"[NewsAnalyzer] run_full_analysis: sentiment={article_sentiment}, surprise={surprise_factor}, sectors={[s.get('sector') for s in sectors]}")

    # שכבה 1: חברות מפורשות מאומתות yfinance
    affected: List[Dict] = []
    existing_tickers: set = set()

    for company in llm_result.get('companies', []):
        ticker_hint = company.get('ticker', '').strip().upper()
        if not ticker_hint:
            logger.debug(f"[NewsAnalyzer] Company '{company.get('name')}' has no ticker hint — skipping")
            continue
        real_ticker = _resolve_ticker(ticker_hint)
        if not real_ticker:
            logger.warning(f"[NewsAnalyzer] Ticker '{ticker_hint}' ({company.get('name')}) not found in yfinance — skipping")
            continue
        logger.info(f"[NewsAnalyzer] Resolved ticker: {real_ticker} ({company.get('name')}, impact={company.get('impact')})")
        magnitude = _real_magnitude(real_ticker)
        affected.append({
            'ticker':    real_ticker,
            'company':   company.get('name', real_ticker),
            'impact':    company.get('impact', 'neutral'),
            'magnitude': magnitude,
            'layer':     company.get('layer', 'direct'),
            'reason':    _html_mod.escape(str(company.get('reason', ''))),
        })
        existing_tickers.add(real_ticker)

    # שכבה 2: Macro Correlation Matrix
    if macro_signals_raw:
        if surprise_factor:
            for sig in macro_signals_raw:
                sig['surprise'] = True

        macro_affected = get_affected_by_signals(macro_signals_raw)

        for ticker in macro_affected['positive_tickers']:
            if ticker in existing_tickers:
                continue
            scored = _score_ticker_for_news(ticker, 'positive')
            if not scored or scored['opportunity'] in ('already_priced_in', 'neutral'):
                continue
            signal_descs = [
                MACRO_CORRELATION_MATRIX.get(s.get('signal',''), {}).get('description', s.get('signal',''))
                for s in macro_signals_raw[:2]
            ]
            reason = " | ".join(filter(None, [
                " + ".join(signal_descs),
                scored.get('opportunity',''),
                f"RSI={scored['rsi']}",
                f"Short={scored['short_pct']}%" if scored.get('short_pct') and scored['short_pct'] > 10 else None,
                f"Vol×{scored['vol_ratio']:.1f}" if scored.get('vol_ratio', 1) > 1.5 else None,
            ]))
            affected.append({
                'ticker':      ticker,
                'company':     ticker,
                'impact':      'positive',
                'magnitude':   scored['magnitude'],
                'layer':       'macro',
                'reason':      reason,
                'opportunity': scored.get('opportunity'),
                'rsi':         scored.get('rsi'),
                'short_pct':   scored.get('short_pct'),
                'vol_ratio':   scored.get('vol_ratio'),
            })
            existing_tickers.add(ticker)

        for ticker in macro_affected['negative_tickers']:
            if ticker in existing_tickers:
                continue
            scored = _score_ticker_for_news(ticker, 'negative')
            if not scored or scored['opportunity'] in ('already_priced_in', 'neutral'):
                continue
            signal_descs = [
                MACRO_CORRELATION_MATRIX.get(s.get('signal',''), {}).get('description', s.get('signal',''))
                for s in macro_signals_raw[:2]
            ]
            reason = " | ".join(filter(None, [
                " + ".join(signal_descs),
                scored.get('opportunity',''),
                f"RSI={scored['rsi']}",
                f"Short={scored['short_pct']}%" if scored.get('short_pct') and scored['short_pct'] > 10 else None,
            ]))
            affected.append({
                'ticker':      ticker,
                'company':     ticker,
                'impact':      'negative',
                'magnitude':   scored['magnitude'],
                'layer':       'macro',
                'reason':      reason,
                'opportunity': scored.get('opportunity'),
                'rsi':         scored.get('rsi'),
                'short_pct':   scored.get('short_pct'),
                'vol_ratio':   scored.get('vol_ratio'),
            })
            existing_tickers.add(ticker)

    # שכבה 3: sector grounding
    sector_picks = analyze_sector_impact(sectors, article_sentiment)
    for pick in sector_picks:
        if pick['ticker'] not in existing_tickers:
            affected.append(pick)
            existing_tickers.add(pick['ticker'])

    layer_order = {'direct': 0, 'competitor': 1, 'supply_chain': 2, 'macro': 3, 'sector': 4}
    affected.sort(key=lambda x: (layer_order.get(x.get('layer', 'sector'), 4), -x.get('magnitude', 1)))

    logger.info(f"[NewsAnalyzer] Final affected tickers: {[a['ticker'] for a in affected]} (total={len(affected)})")

    return {
        'summary':       summary,
        'sentiment':     article_sentiment,
        'affected':      affected,
        'sectors':       sectors,
        'macro_signals': macro_signals_raw,
        'surprise':      surprise_factor,
    }
