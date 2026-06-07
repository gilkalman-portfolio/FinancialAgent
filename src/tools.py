"""
Tools Module - Data Fetching and Analysis
Handles fetching stock data and technical analysis.
"""

import yfinance as yf
from typing import Dict, Any, Optional
from loguru import logger
from datetime import datetime, timedelta
from pattern_detectors.single_candle_patterns import SingleCandlePatternDetector
from pattern_detectors.technical_indicators import TechnicalIndicators
from src.stock_forecaster import StockForecaster


class StockDataFetcher:
    """
    Fetches stock data from Yahoo Finance and other sources.
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the data fetcher.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.cache = {}  # Simple in-memory cache
        self.cache_duration = config['data_sources'].get('cache_duration', 3600)
    
    def get_stock_data(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        Fetch comprehensive stock data.
        
        Args:
            ticker: Stock ticker symbol
            
        Returns:
            Dictionary with stock data or None if failed
        """
        # Check cache first
        cache_key = f"{ticker}_{datetime.now().strftime('%Y%m%d%H')}"
        if cache_key in self.cache:
            logger.debug(f"Using cached data for {ticker}")
            return self.cache[cache_key]
        
        try:
            logger.info(f"Fetching fresh data for {ticker}")
            stock = yf.Ticker(ticker)
            
            # Get basic info
            info = stock.info
            
            # Get historical data (last 6 months for technical analysis)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=180)
            history = stock.history(start=start_date, end=end_date)
            
            # Compile data
            data = {
                'ticker': ticker,
                'name': info.get('longName', ticker),
                'sector': info.get('sector', 'N/A'),
                'industry': info.get('industry', 'N/A'),
                
                # Price data
                'current_price': info.get('currentPrice', info.get('regularMarketPrice', 0)),
                'previous_close': info.get('previousClose', 0),
                'day_high': info.get('dayHigh', 0),
                'day_low': info.get('dayLow', 0),
                'year_high': info.get('fiftyTwoWeekHigh', 0),
                'year_low': info.get('fiftyTwoWeekLow', 0),
                
                # Volume
                'volume': info.get('volume', 0),
                'avg_volume': info.get('averageVolume', 0),
                
                # Market data
                'market_cap': info.get('marketCap', 0),
                'shares_outstanding': info.get('sharesOutstanding', 0),
                
                # Valuation metrics
                'pe_ratio': info.get('trailingPE', None),
                'forward_pe': info.get('forwardPE', None),
                'peg_ratio': info.get('pegRatio', None),
                'price_to_book': info.get('priceToBook', None),
                'price_to_sales': info.get('priceToSalesTrailing12Months', None),
                
                # Profitability
                'profit_margins': info.get('profitMargins', None),
                'operating_margins': info.get('operatingMargins', None),
                'roe': info.get('returnOnEquity', None),
                'roa': info.get('returnOnAssets', None),
                
                # Growth metrics
                'revenue_growth': info.get('revenueGrowth', None),
                'earnings_growth': info.get('earningsGrowth', None),
                'revenue': info.get('totalRevenue', None),
                'earnings_per_share': info.get('trailingEps', None),
                
                # Debt and cash
                'total_cash': info.get('totalCash', None),
                'total_debt': info.get('totalDebt', None),
                'debt_to_equity': info.get('debtToEquity', None),
                'current_ratio': info.get('currentRatio', None),
                'quick_ratio': info.get('quickRatio', None),
                
                # Dividend
                'dividend_rate': info.get('dividendRate', None),
                'dividend_yield': info.get('dividendYield', None),
                'payout_ratio': info.get('payoutRatio', None),
                
                # Historical prices for technical analysis
                'history': history,
                
                # Analyst recommendations
                'recommendation': info.get('recommendationKey', 'none'),
                'target_mean_price': info.get('targetMeanPrice', None),
                
                # Metadata
                'fetched_at': datetime.now().isoformat()
            }
            
            # Cache the data
            self.cache[cache_key] = data
            
            logger.info(f"Successfully fetched data for {ticker}")
            return data
            
        except Exception as e:
            logger.error(f"Error fetching data for {ticker}: {str(e)}")
            return None
    
    def get_recent_news(self, ticker: str, max_items: int = 5) -> list:
        """
        Fetch recent news for a stock.
        
        Args:
            ticker: Stock ticker symbol
            max_items: Maximum number of news items
            
        Returns:
            List of news items
        """
        try:
            stock = yf.Ticker(ticker)
            news = stock.news[:max_items] if hasattr(stock, 'news') else []
            return news
        except Exception as e:
            logger.error(f"Error fetching news for {ticker}: {str(e)}")
            return []
    
    def clear_cache(self):
        """Clear the data cache."""
        self.cache.clear()
        logger.info("Cache cleared")


class TechnicalAnalyzer:
    """
    Performs technical analysis including pattern detection and indicators.
    """
    
    def __init__(self):
        """Initialize technical analysis tools."""
        self.pattern_detector = SingleCandlePatternDetector(sensitivity=1.0)
        self.indicators_calculator = TechnicalIndicators()
        logger.info("TechnicalAnalyzer initialized")
    
    def analyze_stock_patterns(
        self, 
        ticker: str, 
        period: str = "1mo", 
        interval: str = "1h"
    ) -> Dict[str, Any]:
        """
        Analyze stock for patterns and technical indicators.
        
        Args:
            ticker: Stock ticker symbol
            period: Time period (1d, 5d, 1mo, 3mo, 6mo, 1y, etc.)
            interval: Data interval (1m, 5m, 15m, 30m, 1h, 1d, etc.)
            
        Returns:
            Dictionary with complete technical analysis
        """
        try:
            logger.info(f"Starting technical analysis for {ticker}")
            
            # Fetch data
            stock = yf.Ticker(ticker)
            df = stock.history(period=period, interval=interval)
            
            # Log what we got
            logger.info(f"Fetched {len(df)} candles for {ticker} (period={period}, interval={interval})")
            
            # Determine minimum required candles based on interval
            if interval in ['1m', '5m', '15m', '30m', '1h']:
                min_candles = 20  # Less strict for intraday
            else:
                min_candles = 50  # Daily or longer
            
            if df.empty or len(df) < min_candles:
                logger.warning(f"Insufficient data for {ticker}: {len(df)} candles (need {min_candles})")
                return {
                    'ticker': ticker,
                    'error': f'Insufficient data for analysis (got {len(df)} candles, need {min_candles})',
                    'data_points': len(df),
                    'suggestion': 'Try longer period (3mo, 6mo) or different interval (1d instead of 1h)'
                }
            
            logger.info(f"Fetched {len(df)} candles for {ticker}")
            
            # Detect patterns
            patterns = self.pattern_detector.detect_all_patterns(df)
            pattern_summary = self.pattern_detector.summarize_patterns(patterns)
            
            # Calculate indicators
            df_with_indicators = self.indicators_calculator.calculate_all_indicators(df)
            current_signals = self.indicators_calculator.analyze_current_signals(df_with_indicators)
            
            # Combine results
            result = {
                'ticker': ticker,
                'period': period,
                'interval': interval,
                'data_points': len(df),
                'date_range': {
                    'start': str(df.index[0]),
                    'end': str(df.index[-1])
                },
                'current_price': float(df['Close'].iloc[-1]),
                
                # Pattern analysis
                'patterns': {
                    'detected': patterns,
                    'summary': pattern_summary
                },
                
                # Technical indicators
                'indicators': current_signals['indicators'],
                'signals': current_signals['signals'],
                'score': current_signals['score'],
                
                # Combined recommendation
                'recommendation': self._generate_combined_recommendation(
                    pattern_summary, 
                    current_signals['score']
                )
            }
            
            logger.info(f"Technical analysis completed for {ticker}")
            return result
            
        except Exception as e:
            logger.error(f"Error in technical analysis for {ticker}: {str(e)}")
            return {
                'ticker': ticker,
                'error': str(e)
            }
    
    def get_quick_analysis(self, ticker: str) -> Dict[str, Any]:
        """
        Quick analysis with default parameters (1 month, hourly).
        
        Args:
            ticker: Stock ticker symbol
            
        Returns:
            Dictionary with quick analysis results
        """
        return self.analyze_stock_patterns(ticker, period="1mo", interval="1h")
    
    def get_daily_analysis(self, ticker: str) -> Dict[str, Any]:
        """
        Daily analysis (3 months, daily candles).
        
        Args:
            ticker: Stock ticker symbol
            
        Returns:
            Dictionary with daily analysis results
        """
        return self.analyze_stock_patterns(ticker, period="3mo", interval="1d")
    
    def _generate_combined_recommendation(
        self, 
        pattern_summary: Dict[str, Any],
        indicator_score: Dict[str, Any]
    ) -> Dict[str, str]:
        """
        Generate combined recommendation from patterns and indicators.
        
        Args:
            pattern_summary: Summary from pattern detection
            indicator_score: Score from technical indicators
            
        Returns:
            Combined recommendation with reasoning
        """
        pattern_sentiment = pattern_summary['overall_sentiment']
        indicator_recommendation = indicator_score['recommendation']
        indicator_confidence = indicator_score['confidence']
        
        # Strong alignment
        if pattern_sentiment == 'bullish' and indicator_recommendation == 'BUY':
            return {
                'action': 'STRONG BUY',
                'confidence': 'high',
                'reasoning': 'Both patterns and indicators align bullish. Strong buy signal detected.'
            }
        
        elif pattern_sentiment == 'bearish' and indicator_recommendation == 'SELL':
            return {
                'action': 'STRONG SELL',
                'confidence': 'high',
                'reasoning': 'Both patterns and indicators align bearish. Strong sell signal detected.'
            }
        
        # Indicator-driven
        elif indicator_recommendation == 'BUY':
            confidence = indicator_confidence if pattern_sentiment == 'neutral' else 'medium'
            return {
                'action': 'BUY',
                'confidence': confidence,
                'reasoning': f'Technical indicators suggest buy. Pattern sentiment is {pattern_sentiment}.'
            }
        
        elif indicator_recommendation == 'SELL':
            confidence = indicator_confidence if pattern_sentiment == 'neutral' else 'medium'
            return {
                'action': 'SELL',
                'confidence': confidence,
                'reasoning': f'Technical indicators suggest sell. Pattern sentiment is {pattern_sentiment}.'
            }
        
        # Mixed signals
        else:
            return {
                'action': 'HOLD',
                'confidence': 'low',
                'reasoning': 'Mixed signals from patterns and indicators. Recommend holding position.'
            }


if __name__ == "__main__":
    # Simple test
    import yaml
    
    with open('../config/config.yaml', 'r') as f:
        config = yaml.safe_load(f)
    
    fetcher = StockDataFetcher(config)
    data = fetcher.get_stock_data("AAPL")
    
    if data:
        print(f"\nFetched data for {data['name']} ({data['ticker']})")
        print(f"Price: ${data['current_price']:.2f}")
        print(f"P/E Ratio: {data['pe_ratio']}")
        print(f"Market Cap: ${data['market_cap']:,.0f}")
    else:
        print("Failed to fetch data")
