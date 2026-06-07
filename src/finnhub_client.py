"""
Finnhub API Client - News + Sentiment + Social Data

Free Tier: 60 requests/minute
Get API key: https://finnhub.io/register

Features:
- Company news
- Social sentiment (Twitter, Reddit, StockTwits)
- Market buzz
- Insider transactions (backup)
"""

import requests
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass
import time
from loguru import logger
import os
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

FINNHUB_API_KEY = os.getenv('FINNHUB_API_KEY', '')
FINNHUB_BASE_URL = 'https://finnhub.io/api/v1'

# Rate limiting
RATE_LIMIT_PER_MINUTE = 60
RATE_LIMIT_DELAY = 60.0 / RATE_LIMIT_PER_MINUTE  # ~1 second between calls


# ══════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class NewsArticle:
    """News article from Finnhub"""
    headline: str
    summary: str
    source: str
    url: str
    datetime: datetime
    category: str
    sentiment: Optional[float] = None  # -1 to 1
    
    def to_dict(self) -> dict:
        return {
            'headline': self.headline,
            'summary': self.summary,
            'source': self.source,
            'url': self.url,
            'datetime': self.datetime.isoformat(),
            'category': self.category,
            'sentiment': self.sentiment
        }


@dataclass
class SocialSentiment:
    """Social sentiment data"""
    ticker: str
    timestamp: datetime
    
    # Reddit
    reddit_mentions: int
    reddit_positive: float
    reddit_negative: float
    reddit_score: float
    
    # Twitter
    twitter_mentions: int
    twitter_positive: float
    twitter_negative: float
    twitter_score: float
    
    # Overall
    total_mentions: int
    overall_sentiment: float
    sentiment_label: str
    
    def to_dict(self) -> dict:
        return {
            'ticker': self.ticker,
            'timestamp': self.timestamp.isoformat(),
            'reddit_mentions': self.reddit_mentions,
            'reddit_score': self.reddit_score,
            'twitter_mentions': self.twitter_mentions,
            'twitter_score': self.twitter_score,
            'total_mentions': self.total_mentions,
            'overall_sentiment': self.overall_sentiment,
            'sentiment_label': self.sentiment_label
        }


@dataclass
class MarketBuzz:
    """Market buzz data"""
    ticker: str
    attention: float  # 0-100
    sentiment: float  # -1 to 1
    buzz_score: float  # Combined metric
    articles_count: int
    
    def to_dict(self) -> dict:
        return {
            'ticker': self.ticker,
            'attention': self.attention,
            'sentiment': self.sentiment,
            'buzz_score': self.buzz_score,
            'articles_count': self.articles_count
        }


# ══════════════════════════════════════════════════════════════════════════════
# FINNHUB CLIENT
# ══════════════════════════════════════════════════════════════════════════════

class FinnhubClient:
    """Client for Finnhub API"""
    
    def __init__(self, api_key: str = FINNHUB_API_KEY):
        self.api_key = api_key
        self.base_url = FINNHUB_BASE_URL
        self.last_request_time = 0
        
        if not self.api_key:
            logger.warning("No Finnhub API key found. Get one at https://finnhub.io/register")
        else:
            logger.info("Finnhub client initialized")
    
    def _rate_limit(self):
        """Enforce rate limiting"""
        elapsed = time.time() - self.last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)
        self.last_request_time = time.time()
    
    def _request(self, endpoint: str, params: dict = None) -> dict:
        """Make API request with rate limiting"""
        if not self.api_key:
            logger.warning("API key not configured")
            return {}
        
        self._rate_limit()
        
        try:
            url = f"{self.base_url}/{endpoint}"
            params = params or {}
            params['token'] = self.api_key
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            return response.json()
        
        except requests.exceptions.RequestException as e:
            logger.error(f"Finnhub API error: {e}")
            return {}
    
    # ──────────────────────────────────────────────────────────────────────────
    # NEWS
    # ──────────────────────────────────────────────────────────────────────────
    
    def get_company_news(self, ticker: str, days: int = 7) -> List[NewsArticle]:
        """
        Get company news for last N days
        
        Args:
            ticker: Stock symbol
            days: Number of days (1-30)
        
        Returns:
            List of NewsArticle objects
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        params = {
            'symbol': ticker,
            'from': start_date.strftime('%Y-%m-%d'),
            'to': end_date.strftime('%Y-%m-%d')
        }
        
        data = self._request('company-news', params)
        
        if not data:
            return []
        
        articles = []
        for item in data[:20]:  # Limit to 20 most recent
            try:
                article = NewsArticle(
                    headline=item.get('headline', ''),
                    summary=item.get('summary', ''),
                    source=item.get('source', ''),
                    url=item.get('url', ''),
                    datetime=datetime.fromtimestamp(item.get('datetime', 0)),
                    category=item.get('category', 'general'),
                    sentiment=None  # Finnhub doesn't provide per-article sentiment
                )
                articles.append(article)
            except Exception as e:
                logger.debug(f"Error parsing article: {e}")
                continue
        
        logger.info(f"Fetched {len(articles)} news articles for {ticker}")
        return articles
    
    # ──────────────────────────────────────────────────────────────────────────
    # EARNINGS
    # ──────────────────────────────────────────────────────────────────────────

    def get_earnings_surprises(self, ticker: str, limit: int = 4) -> List[Dict]:
        """EPS actual vs estimate for last N quarters (free tier).
        Returns list of dicts: period, actual, estimate, surprise, surprise_pct."""
        data = self._request('stock/earnings', {'symbol': ticker, 'limit': limit})
        if not data or not isinstance(data, list):
            return []
        results = []
        for item in data[:limit]:
            try:
                actual   = item.get('actual')
                estimate = item.get('estimate')
                if actual is None or estimate is None:
                    continue
                surprise     = actual - estimate
                surprise_pct = (surprise / abs(estimate) * 100) if estimate != 0 else 0.0
                results.append({
                    'period':       item.get('period', ''),
                    'actual':       round(float(actual), 4),
                    'estimate':     round(float(estimate), 4),
                    'surprise':     round(float(surprise), 4),
                    'surprise_pct': round(float(surprise_pct), 2),
                })
            except Exception:
                continue
        return results

    def get_earnings_transcript_list(self, ticker: str) -> List[Dict]:
        """List available earnings call transcripts (requires paid tier).
        Returns list of dicts: id, title, date — empty on free tier (403/404)."""
        try:
            data = self._request('stock/transcripts/list', {'symbol': ticker})
            transcripts = data.get('transcripts', []) if isinstance(data, dict) else []
            return [
                {'id': t.get('id', ''), 'title': t.get('title', ''), 'date': t.get('time', '')}
                for t in transcripts if t.get('id')
            ]
        except Exception:
            return []

    def get_earnings_transcript(self, transcript_id: str) -> str:
        """Fetch full transcript text by ID (requires paid tier).
        Returns concatenated speaker turns, or empty string on failure."""
        try:
            data = self._request('stock/transcripts', {'id': transcript_id})
            if not data or not isinstance(data, dict):
                return ''
            parts = []
            for participant in data.get('transcript', []):
                name   = participant.get('name', '')
                speech = ' '.join(s.get('text', '') for s in participant.get('speech', []))
                if speech.strip():
                    parts.append(f"{name}: {speech.strip()}")
            return '\n\n'.join(parts)
        except Exception:
            return ''

    # ──────────────────────────────────────────────────────────────────────────
    # SOCIAL SENTIMENT
    # ──────────────────────────────────────────────────────────────────────────
    
    def get_social_sentiment(self, ticker: str) -> Optional[SocialSentiment]:
        """
        Get social sentiment from Reddit + Twitter
        
        Note: Free tier may have limited data
        """
        data = self._request('stock/social-sentiment', {'symbol': ticker})
        
        if not data or 'reddit' not in data:
            return None
        
        try:
            reddit = data.get('reddit', [{}])[0] if data.get('reddit') else {}
            twitter = data.get('twitter', [{}])[0] if data.get('twitter') else {}
            
            # Reddit metrics
            reddit_mentions = reddit.get('mention', 0)
            reddit_positive = reddit.get('positiveScore', 0)
            reddit_negative = reddit.get('negativeScore', 0)
            reddit_score = reddit.get('score', 0)
            
            # Twitter metrics
            twitter_mentions = twitter.get('mention', 0)
            twitter_positive = twitter.get('positiveScore', 0)
            twitter_negative = twitter.get('negativeScore', 0)
            twitter_score = twitter.get('score', 0)
            
            # Calculate overall
            total_mentions = reddit_mentions + twitter_mentions
            
            if total_mentions > 0:
                overall = (reddit_score * reddit_mentions + twitter_score * twitter_mentions) / total_mentions
            else:
                overall = 0
            
            # Sentiment label
            if overall >= 0.5:
                label = 'Very Bullish'
            elif overall >= 0.2:
                label = 'Bullish'
            elif overall >= -0.2:
                label = 'Neutral'
            elif overall >= -0.5:
                label = 'Bearish'
            else:
                label = 'Very Bearish'
            
            sentiment = SocialSentiment(
                ticker=ticker,
                timestamp=datetime.now(),
                reddit_mentions=reddit_mentions,
                reddit_positive=reddit_positive,
                reddit_negative=reddit_negative,
                reddit_score=reddit_score,
                twitter_mentions=twitter_mentions,
                twitter_positive=twitter_positive,
                twitter_negative=twitter_negative,
                twitter_score=twitter_score,
                total_mentions=total_mentions,
                overall_sentiment=overall,
                sentiment_label=label
            )
            
            logger.info(f"Social sentiment for {ticker}: {label} ({overall:.2f})")
            return sentiment
        
        except Exception as e:
            logger.error(f"Error parsing social sentiment: {e}")
            return None
    
    # ──────────────────────────────────────────────────────────────────────────
    # MARKET BUZZ
    # ──────────────────────────────────────────────────────────────────────────
    
    def get_market_buzz(self, ticker: str) -> Optional[MarketBuzz]:
        """
        Get market buzz metrics
        
        Combines attention + sentiment into buzz score
        """
        # Get news for buzz calculation
        news = self.get_company_news(ticker, days=3)
        
        if not news:
            return None
        
        # Simple buzz calculation
        articles_count = len(news)
        
        # Attention score (based on article count)
        # 10+ articles = high attention
        attention = min(articles_count * 10, 100)
        
        # Get social sentiment for overall sentiment
        social = self.get_social_sentiment(ticker)
        sentiment = social.overall_sentiment if social else 0
        
        # Buzz score: attention * (1 + sentiment)
        # High attention + positive sentiment = high buzz
        buzz_score = attention * (1 + sentiment) / 2
        
        buzz = MarketBuzz(
            ticker=ticker,
            attention=attention,
            sentiment=sentiment,
            buzz_score=buzz_score,
            articles_count=articles_count
        )
        
        logger.info(f"Market buzz for {ticker}: {buzz_score:.1f}/100")
        return buzz


# ══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def analyze_news_sentiment(articles: List[NewsArticle]) -> Dict[str, any]:
    """
    Analyze news sentiment (basic keyword analysis)
    
    Returns dict with sentiment metrics
    """
    if not articles:
        return {'sentiment': 0, 'label': 'Neutral', 'count': 0}
    
    # Keyword lists
    positive_keywords = [
        'surge', 'soar', 'jump', 'rally', 'gain', 'up', 'high', 'record',
        'breakout', 'bullish', 'strong', 'beat', 'exceed', 'growth', 'profit'
    ]
    
    negative_keywords = [
        'drop', 'fall', 'plunge', 'decline', 'down', 'low', 'crash',
        'bearish', 'weak', 'miss', 'loss', 'debt', 'risk', 'concern'
    ]
    
    positive_count = 0
    negative_count = 0
    
    for article in articles:
        text = (article.headline + ' ' + article.summary).lower()
        
        for word in positive_keywords:
            if word in text:
                positive_count += 1
        
        for word in negative_keywords:
            if word in text:
                negative_count += 1
    
    total = positive_count + negative_count
    
    if total == 0:
        sentiment = 0
        label = 'Neutral'
    else:
        sentiment = (positive_count - negative_count) / total
        
        if sentiment >= 0.3:
            label = 'Bullish'
        elif sentiment <= -0.3:
            label = 'Bearish'
        else:
            label = 'Neutral'
    
    return {
        'sentiment': sentiment,
        'label': label,
        'count': len(articles),
        'positive_keywords': positive_count,
        'negative_keywords': negative_count
    }


# ══════════════════════════════════════════════════════════════════════════════
# TESTING
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Test the client
    client = FinnhubClient()
    
    test_ticker = 'GME'
    
    print(f"\nTesting Finnhub API with {test_ticker}...")
    print("="*60)
    
    # Test news
    print("\n1. Company News:")
    news = client.get_company_news(test_ticker, days=7)
    for article in news[:3]:
        print(f"  - {article.headline[:60]}...")
        print(f"    {article.source} | {article.datetime.strftime('%Y-%m-%d')}")
    
    # Analyze sentiment
    sentiment_analysis = analyze_news_sentiment(news)
    print(f"\n  News Sentiment: {sentiment_analysis['label']} ({sentiment_analysis['sentiment']:.2f})")
    
    # Test social sentiment
    print("\n2. Social Sentiment:")
    social = client.get_social_sentiment(test_ticker)
    if social:
        print(f"  Reddit: {social.reddit_mentions} mentions, score: {social.reddit_score:.2f}")
        print(f"  Twitter: {social.twitter_mentions} mentions, score: {social.twitter_score:.2f}")
        print(f"  Overall: {social.sentiment_label} ({social.overall_sentiment:.2f})")
    
    # Test buzz
    print("\n3. Market Buzz:")
    buzz = client.get_market_buzz(test_ticker)
    if buzz:
        print(f"  Attention: {buzz.attention:.1f}/100")
        print(f"  Sentiment: {buzz.sentiment:.2f}")
        print(f"  Buzz Score: {buzz.buzz_score:.1f}/100")
