"""
Analyzers Module - Financial Analysis
Performs technical and fundamental analysis on stock data.
"""

import pandas as pd
import numpy as np
from typing import Dict, Any, Optional
from loguru import logger
import ta


class FinancialAnalyzer:
    """
    Analyzes stock data and computes financial metrics.
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the analyzer.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.value_criteria = config['analysis']['value']
        self.growth_criteria = config['analysis']['growth']
        self.technical_criteria = config['analysis']['technical']
    
    def analyze(self, stock_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Perform complete analysis on stock data.
        
        Args:
            stock_data: Stock data from fetcher
            
        Returns:
            Dictionary with analysis results
        """
        ticker = stock_data['ticker']
        logger.info(f"Analyzing {ticker}")
        
        analysis = {
            'value_metrics': self._analyze_value(stock_data),
            'growth_metrics': self._analyze_growth(stock_data),
            'technical_indicators': self._analyze_technical(stock_data),
            'overall_score': None
        }
        
        # Calculate overall score
        analysis['overall_score'] = self._calculate_score(analysis)
        
        return analysis
    
    def _analyze_value(self, stock_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze value investing metrics.
        
        Args:
            stock_data: Stock data
            
        Returns:
            Value metrics dictionary
        """
        pe_ratio = stock_data.get('pe_ratio')
        pb_ratio = stock_data.get('price_to_book')
        debt_equity = stock_data.get('debt_to_equity')
        dividend_yield = stock_data.get('dividend_yield')
        
        # Check against criteria
        pe_good = pe_ratio is not None and pe_ratio < self.value_criteria['max_pe_ratio']
        pb_good = pb_ratio is not None and pb_ratio < self.value_criteria['max_pb_ratio']
        debt_good = debt_equity is not None and debt_equity < self.value_criteria['max_debt_equity']
        div_good = dividend_yield is not None and dividend_yield >= self.value_criteria['min_dividend_yield']
        
        # Calculate value score (0-100)
        score_components = [pe_good, pb_good, debt_good, div_good]
        value_score = (sum(score_components) / len(score_components)) * 100
        
        return {
            'pe_ratio': pe_ratio,
            'pe_status': 'GOOD' if pe_good else 'HIGH' if pe_ratio else 'N/A',
            'price_to_book': pb_ratio,
            'pb_status': 'GOOD' if pb_good else 'HIGH' if pb_ratio else 'N/A',
            'debt_to_equity': debt_equity,
            'debt_status': 'GOOD' if debt_good else 'HIGH' if debt_equity else 'N/A',
            'dividend_yield': dividend_yield,
            'dividend_status': 'GOOD' if div_good else 'LOW' if dividend_yield else 'N/A',
            'value_score': value_score
        }
    
    def _analyze_growth(self, stock_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze growth metrics.
        
        Args:
            stock_data: Stock data
            
        Returns:
            Growth metrics dictionary
        """
        earnings_growth = stock_data.get('earnings_growth')
        revenue_growth = stock_data.get('revenue_growth')
        market_cap = stock_data.get('market_cap')
        
        # Check against criteria
        earnings_good = (earnings_growth is not None and 
                        earnings_growth >= self.growth_criteria['min_earnings_growth'])
        revenue_good = (revenue_growth is not None and 
                       revenue_growth >= self.growth_criteria['min_revenue_growth'])
        size_good = market_cap >= self.growth_criteria['min_market_cap']
        
        # Calculate growth score
        score_components = [earnings_good, revenue_good, size_good]
        growth_score = (sum(score_components) / len(score_components)) * 100
        
        return {
            'earnings_growth': earnings_growth,
            'earnings_status': 'GOOD' if earnings_good else 'LOW' if earnings_growth else 'N/A',
            'revenue_growth': revenue_growth,
            'revenue_status': 'GOOD' if revenue_good else 'LOW' if revenue_growth else 'N/A',
            'market_cap': market_cap,
            'size_status': 'GOOD' if size_good else 'SMALL',
            'growth_score': growth_score
        }
    
    def _analyze_technical(self, stock_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze technical indicators.
        
        Args:
            stock_data: Stock data
            
        Returns:
            Technical indicators dictionary
        """
        try:
            history = stock_data.get('history')
            if history is None or len(history) < 50:
                return {
                    'status': 'INSUFFICIENT_DATA',
                    'technical_score': 0
                }
            
            df = history.copy()
            current_price = stock_data['current_price']
            
            # Moving Averages
            ma_short = self.technical_criteria['ma_short_period']
            ma_long = self.technical_criteria['ma_long_period']
            
            df['MA_short'] = df['Close'].rolling(window=ma_short).mean()
            df['MA_long'] = df['Close'].rolling(window=ma_long).mean()
            
            ma_short_value = df['MA_short'].iloc[-1]
            ma_long_value = df['MA_long'].iloc[-1] if len(df) >= ma_long else None
            
            # RSI
            rsi = ta.momentum.RSIIndicator(df['Close'], window=14)
            current_rsi = rsi.rsi().iloc[-1]
            
            # MACD
            macd = ta.trend.MACD(df['Close'])
            macd_line = macd.macd().iloc[-1]
            signal_line = macd.macd_signal().iloc[-1]
            
            # Price vs Moving Averages
            above_ma_short = current_price > ma_short_value
            above_ma_long = current_price > ma_long_value if ma_long_value else None
            golden_cross = ma_short_value > ma_long_value if ma_long_value else None
            
            # RSI Analysis
            rsi_neutral = (self.technical_criteria['rsi_oversold'] < current_rsi < 
                          self.technical_criteria['rsi_overbought'])
            rsi_oversold = current_rsi < self.technical_criteria['rsi_oversold']
            rsi_overbought = current_rsi > self.technical_criteria['rsi_overbought']
            
            # MACD Signal
            macd_bullish = macd_line > signal_line
            
            # Volume trend
            avg_volume_recent = df['Volume'].tail(10).mean()
            avg_volume_older = df['Volume'].tail(50).mean()
            volume_increasing = avg_volume_recent > avg_volume_older
            
            # Calculate technical score
            signals = [
                above_ma_short,
                above_ma_long if above_ma_long is not None else True,
                golden_cross if golden_cross is not None else True,
                rsi_neutral or rsi_oversold,  # Oversold is opportunity
                macd_bullish,
                volume_increasing
            ]
            technical_score = (sum(signals) / len(signals)) * 100
            
            return {
                'current_price': current_price,
                'ma_50': ma_short_value,
                'ma_200': ma_long_value,
                'above_ma_50': above_ma_short,
                'above_ma_200': above_ma_long,
                'golden_cross': golden_cross,
                'rsi': current_rsi,
                'rsi_status': ('OVERSOLD' if rsi_oversold else 
                              'OVERBOUGHT' if rsi_overbought else 'NEUTRAL'),
                'macd': macd_line,
                'macd_signal': signal_line,
                'macd_bullish': macd_bullish,
                'volume_trend': 'INCREASING' if volume_increasing else 'DECREASING',
                'technical_score': technical_score
            }
            
        except Exception as e:
            logger.error(f"Error in technical analysis: {str(e)}")
            return {
                'status': 'ERROR',
                'error': str(e),
                'technical_score': 0
            }
    
    def _calculate_score(self, analysis: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calculate overall investment score.
        
        Args:
            analysis: Analysis results
            
        Returns:
            Overall score dictionary
        """
        value_score = analysis['value_metrics'].get('value_score', 0)
        growth_score = analysis['growth_metrics'].get('growth_score', 0)
        technical_score = analysis['technical_indicators'].get('technical_score', 0)
        
        # Weighted average (you can adjust weights)
        weights = {
            'value': 0.3,
            'growth': 0.4,
            'technical': 0.3
        }
        
        overall = (
            value_score * weights['value'] +
            growth_score * weights['growth'] +
            technical_score * weights['technical']
        )
        
        # Determine rating
        if overall >= 75:
            rating = 'STRONG_BUY'
        elif overall >= 60:
            rating = 'BUY'
        elif overall >= 40:
            rating = 'HOLD'
        elif overall >= 25:
            rating = 'SELL'
        else:
            rating = 'STRONG_SELL'
        
        return {
            'overall_score': overall,
            'rating': rating,
            'value_weight': value_score * weights['value'],
            'growth_weight': growth_score * weights['growth'],
            'technical_weight': technical_score * weights['technical']
        }


if __name__ == "__main__":
    # Simple test
    import yaml
    from src.tools import StockDataFetcher
    
    with open('../config/config.yaml', 'r') as f:
        config = yaml.safe_load(f)
    
    fetcher = StockDataFetcher(config)
    analyzer = FinancialAnalyzer(config)
    
    data = fetcher.get_stock_data("AAPL")
    if data:
        analysis = analyzer.analyze(data)
        print(f"\nAnalysis for {data['ticker']}:")
        print(f"Value Score: {analysis['value_metrics']['value_score']:.1f}")
        print(f"Growth Score: {analysis['growth_metrics']['growth_score']:.1f}")
        print(f"Technical Score: {analysis['technical_indicators']['technical_score']:.1f}")
        print(f"Overall: {analysis['overall_score']['overall_score']:.1f} - {analysis['overall_score']['rating']}")
