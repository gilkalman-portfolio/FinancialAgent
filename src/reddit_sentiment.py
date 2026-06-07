"""
Reddit Sentiment - Social velocity scoring (no API key required)
Uses Reddit's public JSON endpoints to count ticker mentions.
Cached per ticker to avoid rate limits.
"""

import re
import time
import requests
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger

SUBREDDITS  = ['wallstreetbets', 'stocks', 'investing', 'shortsqueeze']
HEADERS     = {'User-Agent': 'FinancialAgent/1.0 (stock sentiment scanner)'}
POSTS_LIMIT = 100
CACHE_TTL   = 1800

_cache: dict = {}
_rate_limited_until: float = 0   # epoch seconds - skip Reddit until this time


def _fetch_posts(subreddit: str, sort: str = 'new') -> list:
    global _rate_limited_until
    if time.time() < _rate_limited_until:
        return []   # still rate limited, skip entirely
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={POSTS_LIMIT}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=6)
        if r.status_code == 200:
            return r.json()['data']['children']
        if r.status_code == 429:
            _rate_limited_until = time.time() + 300  # back off 5 minutes
            logger.debug("Reddit rate limited - pausing 5 min")
        else:
            logger.debug(f"Reddit {subreddit} returned {r.status_code}")
    except Exception as e:
        logger.debug(f"Reddit fetch failed for {subreddit}: {e}")
    return []


def count_mentions(ticker: str, hours: int = 4) -> int:
    cutoff  = datetime.utcnow() - timedelta(hours=hours)
    pattern = re.compile(rf'\b{re.escape(ticker)}\b', re.IGNORECASE)
    count   = 0
    for sub in SUBREDDITS:
        posts = _fetch_posts(sub, sort='new')
        if not posts:
            continue
        for post in posts:
            data    = post.get('data', {})
            created = datetime.utcfromtimestamp(data.get('created_utc', 0))
            if created < cutoff:
                continue
            text = f"{data.get('title', '')} {data.get('selftext', '')}"
            if pattern.search(text):
                count += 1
        time.sleep(0.3)
    return count


def social_score(ticker: str) -> dict:
    # check cache
    cached = _cache.get(ticker)
    if cached:
        result, ts = cached
        if (datetime.utcnow() - ts).total_seconds() < CACHE_TTL:
            return result

    mentions_4h  = count_mentions(ticker, hours=4)
    mentions_24h = count_mentions(ticker, hours=24)

    baseline = mentions_24h / 6
    velocity = mentions_4h / baseline if baseline > 0 else (1.0 if mentions_4h == 0 else 3.0)

    if velocity >= 3.0 or mentions_4h >= 20:   score = 5
    elif velocity >= 2.0 or mentions_4h >= 10: score = 4
    elif velocity >= 1.5 or mentions_4h >= 5:  score = 3
    elif mentions_4h >= 2:                      score = 2
    elif mentions_4h >= 1:                      score = 1
    else:                                       score = 0

    result = {
        'mentions_4h':  mentions_4h,
        'mentions_24h': mentions_24h,
        'velocity':     round(velocity, 2),
        'social_score': score,
    }
    _cache[ticker] = (result, datetime.utcnow())
    return result
