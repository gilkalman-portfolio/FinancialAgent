"""
Earnings Sentiment Analyzer
Two-tier signal:
  Tier 1 (free)  — EPS surprise history from Finnhub (last 4 quarters)
  Tier 2 (paid)  — LLM analysis of latest earnings call transcript
Returns score 0-5 and sentiment label for use in stock_scorer.py bonus.
"""

import json
import threading
import time
from typing import Dict
from loguru import logger

# ── In-memory cache: ticker → (result_dict, timestamp) ──────────────────────
_CACHE: Dict[str, tuple] = {}
_CACHE_TTL = 6 * 3600   # 6 hours
_CACHE_LOCK = threading.Lock()


def _cached(ticker: str):
    with _CACHE_LOCK:
        entry = _CACHE.get(ticker)
    if entry and (time.time() - entry[1]) < _CACHE_TTL:
        return entry[0]
    return None


def _store(ticker: str, result: dict):
    with _CACHE_LOCK:
        _CACHE[ticker] = (result, time.time())


# ── Tier 1: EPS surprise scoring ────────────────────────────────────────────

def _score_eps_surprises(surprises: list) -> tuple:
    """Returns (score 0-5, sentiment str, detail str)."""
    if not surprises:
        return 0, 'neutral', 'no earnings data'

    beats  = sum(1 for s in surprises if s['surprise_pct'] >  2.0)
    misses = sum(1 for s in surprises if s['surprise_pct'] < -2.0)
    n      = len(surprises)

    # Most recent quarter weighted slightly higher
    latest_pct = surprises[0]['surprise_pct'] if surprises else 0

    if beats >= 3:
        score, sentiment = 5, 'bullish'
        detail = f"{beats}/{n} beats (latest {latest_pct:+.1f}%)"
    elif beats == 2 and misses == 0:
        score, sentiment = 4, 'bullish'
        detail = f"2/{n} beats, no misses (latest {latest_pct:+.1f}%)"
    elif beats == 1 and misses == 0:
        score, sentiment = 3, 'neutral'
        detail = f"1/{n} beat, no misses (latest {latest_pct:+.1f}%)"
    elif misses == 0:
        score, sentiment = 2, 'neutral'
        detail = f"all inline ±2% over {n}Q"
    elif misses <= 2 and beats >= misses:
        score, sentiment = 1, 'bearish'
        detail = f"{misses}/{n} misses (latest {latest_pct:+.1f}%)"
    else:
        score, sentiment = 0, 'bearish'
        detail = f"{misses}/{n} misses — consistent disappointment"

    return score, sentiment, detail


# ── EDGAR EPS YoY fallback ──────────────────────────────────────────────────

def _edgar_eps_fallback(ticker: str) -> dict:
    """
    EDGAR EPS YoY growth proxy — used when Finnhub returns no data.
    Computes average YoY EPS% change over last 4 quarters.
    Maps to score 0-5 as rough earnings quality proxy.
    NOT equivalent to EPS surprise — no analyst estimate available.
    """
    try:
        from src.edgar_fcf import get_eps_yoy_growth
        yoy = get_eps_yoy_growth(ticker)
        if yoy is None:
            return {"score": 0, "sentiment": "neutral", "source": "none", "detail": "no data"}

        if   yoy >= 0.30: score, sentiment = 5, "bullish"
        elif yoy >= 0.15: score, sentiment = 4, "bullish"
        elif yoy >= 0.05: score, sentiment = 3, "neutral"
        elif yoy >= -0.05: score, sentiment = 2, "neutral"
        elif yoy >= -0.15: score, sentiment = 1, "bearish"
        else:              score, sentiment = 0, "bearish"

        return {
            "score":     score,
            "sentiment": sentiment,
            "source":    "edgar_eps_yoy",
            "detail":    f"EPS YoY avg: {yoy*100:+.1f}% (4Q)",
        }
    except Exception:
        return {"score": 0, "sentiment": "neutral", "source": "none", "detail": "edgar fallback failed"}


# ── Tier 2: Transcript LLM analysis ─────────────────────────────────────────

def _analyze_transcript(text: str) -> int:
    """LLM analysis of transcript text. Returns score 0-5, or -1 on failure."""
    if not text or len(text) < 200:
        return -1
    try:
        from src.llm_client import llm_complete
        # Truncate to ~6000 chars to stay within token limits
        snippet = text[:6000]
        prompt = (
            "Analyze this earnings call transcript excerpt and return ONLY a JSON object.\n\n"
            f"TRANSCRIPT:\n{snippet}\n\n"
            "Return JSON with these exact keys:\n"
            '{"tone": "confident|cautious|mixed", '
            '"guidance": "raised|lowered|maintained|none", '
            '"beat_miss": "beat|miss|inline", '
            '"score": <integer 0-5>}\n\n'
            "Score guide: 5=very bullish tone+raised guidance, 4=bullish, "
            "3=neutral, 2=mixed/cautious, 1=bearish, 0=very bearish/miss+lowered."
        )
        raw = llm_complete(prompt, max_tokens=150)
        # Extract JSON from response
        start = raw.find('{')
        end   = raw.rfind('}') + 1
        if start == -1 or end == 0:
            return -1
        parsed = json.loads(raw[start:end])
        return int(parsed.get('score', -1))
    except Exception as e:
        logger.debug(f"Transcript LLM failed: {e}")
        return -1


# ── Public API ───────────────────────────────────────────────────────────────

def get_earnings_sentiment(ticker: str, finnhub_key: str) -> dict:
    """
    Returns dict:
      score     : int  0-5
      sentiment : str  'bullish' | 'bearish' | 'neutral'
      source    : str  'transcript_llm' | 'eps_surprise' | 'none'
      detail    : str  human-readable reason
    """
    fallback = {'score': 0, 'sentiment': 'neutral', 'source': 'none', 'detail': 'no data'}

    if not finnhub_key:
        return fallback

    cached = _cached(ticker)
    if cached:
        logger.debug(f"earnings_sentiment cache hit: {ticker}")
        return cached

    try:
        from src.finnhub_client import FinnhubClient
        client = FinnhubClient(finnhub_key)

        # ── Tier 1: EPS surprises ─────────────────────────────────────────────
        surprises = client.get_earnings_surprises(ticker, limit=4)
        eps_score, eps_sentiment, eps_detail = _score_eps_surprises(surprises)

        # ── Tier 2: Transcript (paid tier — skip gracefully if empty) ─────────
        transcript_text = ''
        transcripts = client.get_earnings_transcript_list(ticker)
        if transcripts:
            transcript_text = client.get_earnings_transcript(transcripts[0]['id'])

        llm_score = _analyze_transcript(transcript_text)

        # ── EDGAR fallback when Finnhub has no EPS data ───────────────────────
        if not surprises:
            edgar_result = _edgar_eps_fallback(ticker)
            # If transcript LLM also produced something, blend it in
            if llm_score >= 0:
                blended = round((edgar_result['score'] * 0.5) + (llm_score * 0.5))
                edgar_result = {
                    'score':     blended,
                    'sentiment': ('bullish' if blended >= 4 else 'bearish' if blended <= 1 else 'neutral'),
                    'source':    'transcript_llm',
                    'detail':    f"{edgar_result['detail']} | LLM score: {llm_score}/5",
                }
            _store(ticker, edgar_result)
            logger.debug(f"earnings_sentiment {ticker} (EDGAR fallback): {edgar_result}")
            return edgar_result

        # ── Merge (Finnhub data present) ──────────────────────────────────────
        if llm_score >= 0:
            final_score = round((eps_score * 0.5) + (llm_score * 0.5))
            source      = 'transcript_llm'
            detail      = f"EPS: {eps_detail} | LLM score: {llm_score}/5"
        else:
            final_score = eps_score
            source      = 'eps_surprise'
            detail      = eps_detail

        # Map score to sentiment
        if final_score >= 4:
            sentiment = 'bullish'
        elif final_score <= 1:
            sentiment = 'bearish'
        else:
            sentiment = 'neutral'

        result = {
            'score':     final_score,
            'sentiment': sentiment,
            'source':    source,
            'detail':    detail,
        }
        _store(ticker, result)
        logger.debug(f"earnings_sentiment {ticker}: {result}")
        return result

    except Exception as e:
        logger.debug(f"earnings_sentiment Finnhub failed for {ticker}: {e} — trying EDGAR fallback")
        edgar_result = _edgar_eps_fallback(ticker)
        _store(ticker, edgar_result)
        return edgar_result
