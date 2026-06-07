"""
Technical Indicators Module
Calculates popular technical indicators: RSI, MACD, Bollinger Bands, Moving Averages, etc.
"""

import pandas as pd
import numpy as np
from typing import Dict, Any, Optional, Tuple
from loguru import logger
import ta


class TechnicalIndicators:
    """
    Calculate and analyze technical indicators for trading decisions.
    Uses the 'ta' library for efficient calculations.
    """
    
    def __init__(self):
        """Initialize Technical Indicators calculator."""
        logger.info("TechnicalIndicators initialized")
    
    def calculate_all_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calculate all technical indicators and add them to the dataframe.
        
        Args:
            df: DataFrame with OHLC data (Open, High, Low, Close, Volume)
            
        Returns:
            DataFrame with added indicator columns
        """
        if df.empty or len(df) < 50:
            logger.warning(f"DataFrame too small for indicators: {len(df)} rows")
            return df
        
        df = df.copy()
        
        # Moving Averages
        df = self._add_moving_averages(df)
        
        # RSI
        df = self._add_rsi(df)
        
        # MACD
        df = self._add_macd(df)
        
        # Bollinger Bands
        df = self._add_bollinger_bands(df)
        
        # Volume indicators
        df = self._add_volume_indicators(df)
        
        # Stochastic
        df = self._add_stochastic(df)
        
        # ATR (Average True Range)
        df = self._add_atr(df)
        
        logger.info(f"Calculated all indicators for {len(df)} data points")
        return df
    
    def _add_moving_averages(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add Simple and Exponential Moving Averages.
        
        SMA: Simple Moving Average (arithmetic mean)
        EMA: Exponential Moving Average (weighted, more responsive)
        """
        # Simple Moving Averages
        df['SMA_10'] = ta.trend.sma_indicator(df['Close'], window=10)
        df['SMA_20'] = ta.trend.sma_indicator(df['Close'], window=20)
        df['SMA_50'] = ta.trend.sma_indicator(df['Close'], window=50)
        df['SMA_200'] = ta.trend.sma_indicator(df['Close'], window=200)
        
        # Exponential Moving Averages
        df['EMA_9'] = ta.trend.ema_indicator(df['Close'], window=9)
        df['EMA_21'] = ta.trend.ema_indicator(df['Close'], window=21)
        df['EMA_50'] = ta.trend.ema_indicator(df['Close'], window=50)
        
        return df
    
    def _add_rsi(self, df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
        """
        Add RSI (Relative Strength Index).
        
        RSI measures momentum:
            - Above 70: Overbought (potential sell)
            - Below 30: Oversold (potential buy)
            - 50: Neutral
        """
        df['RSI'] = ta.momentum.rsi(df['Close'], window=window)
        
        # RSI signals
        df['RSI_Oversold'] = df['RSI'] < 30
        df['RSI_Overbought'] = df['RSI'] > 70
        
        return df
    
    def _add_macd(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add MACD (Moving Average Convergence Divergence).
        
        MACD shows trend changes and momentum:
            - MACD line crosses above Signal: Bullish
            - MACD line crosses below Signal: Bearish
            - Histogram: Strength of trend
        """
        # MACD calculation
        macd = ta.trend.MACD(df['Close'])
        df['MACD'] = macd.macd()
        df['MACD_Signal'] = macd.macd_signal()
        df['MACD_Histogram'] = macd.macd_diff()
        
        # MACD crossover signals
        df['MACD_Bullish_Cross'] = (
            (df['MACD'] > df['MACD_Signal']) & 
            (df['MACD'].shift(1) <= df['MACD_Signal'].shift(1))
        )
        df['MACD_Bearish_Cross'] = (
            (df['MACD'] < df['MACD_Signal']) & 
            (df['MACD'].shift(1) >= df['MACD_Signal'].shift(1))
        )
        
        return df
    
    def _add_bollinger_bands(self, df: pd.DataFrame, window: int = 20, std: int = 2) -> pd.DataFrame:
        """
        Add Bollinger Bands.
        
        Bollinger Bands show volatility and potential reversal points:
            - Price touches upper band: Overbought
            - Price touches lower band: Oversold
            - Squeeze (bands narrow): Low volatility, potential breakout
        """
        bollinger = ta.volatility.BollingerBands(df['Close'], window=window, window_dev=std)
        
        df['BB_Upper'] = bollinger.bollinger_hband()
        df['BB_Middle'] = bollinger.bollinger_mavg()
        df['BB_Lower'] = bollinger.bollinger_lband()
        df['BB_Width'] = bollinger.bollinger_wband()
        
        # Bollinger Band signals
        df['BB_Upper_Touch'] = df['Close'] >= df['BB_Upper']
        df['BB_Lower_Touch'] = df['Close'] <= df['BB_Lower']
        df['BB_Squeeze'] = df['BB_Width'] < df['BB_Width'].rolling(20).mean() * 0.8
        
        return df
    
    def _add_volume_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add volume-based indicators.
        
        Volume confirms price movements:
            - High volume + price up = Strong bullish
            - High volume + price down = Strong bearish
            - Low volume = Weak signal
        """
        # Volume Moving Average
        df['Volume_MA'] = df['Volume'].rolling(window=20).mean()
        df['Volume_Ratio'] = df['Volume'] / df['Volume_MA']
        
        # On-Balance Volume (OBV)
        df['OBV'] = ta.volume.on_balance_volume(df['Close'], df['Volume'])
        
        # Volume Price Trend (VPT)
        df['VPT'] = ta.volume.volume_price_trend(df['Close'], df['Volume'])
        
        # High volume signals
        df['High_Volume'] = df['Volume_Ratio'] > 1.5
        
        return df
    
    def _add_stochastic(self, df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
        """
        Add Stochastic Oscillator.
        
        Stochastic measures momentum:
            - Above 80: Overbought
            - Below 20: Oversold
            - %K crosses %D: Signal
        """
        stoch = ta.momentum.StochasticOscillator(
            df['High'], df['Low'], df['Close'], 
            window=window, smooth_window=3
        )
        
        df['Stoch_K'] = stoch.stoch()
        df['Stoch_D'] = stoch.stoch_signal()
        
        # Stochastic signals
        df['Stoch_Oversold'] = df['Stoch_K'] < 20
        df['Stoch_Overbought'] = df['Stoch_K'] > 80
        
        return df
    
    def _add_atr(self, df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
        """
        Add ATR (Average True Range).
        
        ATR measures volatility:
            - High ATR: High volatility (risky)
            - Low ATR: Low volatility (consolidation)
        """
        df['ATR'] = ta.volatility.average_true_range(df['High'], df['Low'], df['Close'], window=window)
        df['ATR_Percent'] = (df['ATR'] / df['Close']) * 100
        
        return df
    
    def analyze_current_signals(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Analyze the most recent data and provide trading signals.
        
        Args:
            df: DataFrame with calculated indicators
            
        Returns:
            Dictionary with current signals and scores
        """
        if df.empty or len(df) < 50:
            return {'error': 'Insufficient data for analysis'}
        
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest
        
        signals = {
            'timestamp': latest.name,
            'price': float(latest['Close']),
            'indicators': {},
            'signals': {},
            'score': {}
        }
        
        # RSI Analysis
        signals['indicators']['RSI'] = {
            'value': float(latest['RSI']),
            'status': self._get_rsi_status(latest['RSI']),
            'signal': self._get_rsi_signal(latest['RSI'])
        }
        
        # MACD Analysis
        signals['indicators']['MACD'] = {
            'macd': float(latest['MACD']),
            'signal': float(latest['MACD_Signal']),
            'histogram': float(latest['MACD_Histogram']),
            'bullish_cross': bool(latest['MACD_Bullish_Cross']),
            'bearish_cross': bool(latest['MACD_Bearish_Cross']),
            'trend': 'bullish' if latest['MACD'] > latest['MACD_Signal'] else 'bearish'
        }
        
        # Moving Average Analysis
        signals['indicators']['MA'] = {
            'price_above_SMA50': bool(latest['Close'] > latest['SMA_50']),
            'price_above_SMA200': bool(latest['Close'] > latest['SMA_200']),
            'golden_cross': bool(latest['SMA_50'] > latest['SMA_200']),
            'trend': self._get_ma_trend(latest)
        }
        
        # Bollinger Bands Analysis
        signals['indicators']['BB'] = {
            'position': self._get_bb_position(latest),
            'width': float(latest['BB_Width']),
            'squeeze': bool(latest['BB_Squeeze']),
            'upper_touch': bool(latest['BB_Upper_Touch']),
            'lower_touch': bool(latest['BB_Lower_Touch'])
        }
        
        # Volume Analysis
        signals['indicators']['Volume'] = {
            'ratio': float(latest['Volume_Ratio']),
            'high_volume': bool(latest['High_Volume']),
            'trend': 'increasing' if latest['OBV'] > prev['OBV'] else 'decreasing'
        }
        
        # Stochastic Analysis
        signals['indicators']['Stochastic'] = {
            'k': float(latest['Stoch_K']),
            'd': float(latest['Stoch_D']),
            'oversold': bool(latest['Stoch_Oversold']),
            'overbought': bool(latest['Stoch_Overbought'])
        }
        
        # ATR (Volatility) Analysis
        signals['indicators']['ATR'] = {
            'value': float(latest['ATR']),
            'percent': float(latest['ATR_Percent']),
            'volatility': 'high' if latest['ATR_Percent'] > 3 else 'low'
        }
        
        # Generate overall trading signals
        signals['signals'] = self._generate_trading_signals(latest)
        
        # Calculate overall score
        signals['score'] = self._calculate_overall_score(signals)
        
        return signals
    
    def _get_rsi_status(self, rsi: float) -> str:
        """Determine RSI status."""
        if rsi > 70:
            return 'overbought'
        elif rsi < 30:
            return 'oversold'
        elif rsi > 50:
            return 'bullish'
        elif rsi < 50:
            return 'bearish'
        else:
            return 'neutral'
    
    def _get_rsi_signal(self, rsi: float) -> str:
        """Get trading signal from RSI."""
        if rsi < 30:
            return 'buy'
        elif rsi > 70:
            return 'sell'
        else:
            return 'hold'
    
    def _get_ma_trend(self, row: pd.Series) -> str:
        """Determine trend from moving averages."""
        if row['Close'] > row['SMA_20'] > row['SMA_50']:
            return 'strong_uptrend'
        elif row['Close'] > row['SMA_50']:
            return 'uptrend'
        elif row['Close'] < row['SMA_20'] < row['SMA_50']:
            return 'strong_downtrend'
        elif row['Close'] < row['SMA_50']:
            return 'downtrend'
        else:
            return 'sideways'
    
    def _get_bb_position(self, row: pd.Series) -> str:
        """Determine price position relative to Bollinger Bands."""
        price = row['Close']
        upper = row['BB_Upper']
        lower = row['BB_Lower']
        middle = row['BB_Middle']
        
        if price >= upper:
            return 'above_upper'
        elif price >= middle:
            return 'upper_half'
        elif price >= lower:
            return 'lower_half'
        else:
            return 'below_lower'
    
    def _generate_trading_signals(self, row: pd.Series) -> Dict[str, str]:
        """Generate overall trading signals based on all indicators."""
        signals = {}
        
        # Momentum Signal
        if row['RSI'] < 30 and row['Stoch_Oversold']:
            signals['momentum'] = 'strong_buy'
        elif row['RSI'] < 40:
            signals['momentum'] = 'buy'
        elif row['RSI'] > 70 and row['Stoch_Overbought']:
            signals['momentum'] = 'strong_sell'
        elif row['RSI'] > 60:
            signals['momentum'] = 'sell'
        else:
            signals['momentum'] = 'neutral'
        
        # Trend Signal
        if row['MACD_Bullish_Cross']:
            signals['trend'] = 'buy'
        elif row['MACD_Bearish_Cross']:
            signals['trend'] = 'sell'
        elif row['MACD'] > row['MACD_Signal'] and row['Close'] > row['SMA_50']:
            signals['trend'] = 'bullish'
        elif row['MACD'] < row['MACD_Signal'] and row['Close'] < row['SMA_50']:
            signals['trend'] = 'bearish'
        else:
            signals['trend'] = 'neutral'
        
        # Volatility Signal
        if row['BB_Lower_Touch']:
            signals['volatility'] = 'buy'
        elif row['BB_Upper_Touch']:
            signals['volatility'] = 'sell'
        elif row['BB_Squeeze']:
            signals['volatility'] = 'breakout_pending'
        else:
            signals['volatility'] = 'normal'
        
        return signals
    
    def _calculate_overall_score(self, signals: Dict) -> Dict[str, Any]:
        """
        Calculate overall bullish/bearish score.
        
        Returns:
            Dictionary with scores and recommendation
        """
        bullish_score = 0
        bearish_score = 0
        
        # RSI scoring
        rsi_val = signals['indicators']['RSI']['value']
        if rsi_val < 30:
            bullish_score += 2
        elif rsi_val < 40:
            bullish_score += 1
        elif rsi_val > 70:
            bearish_score += 2
        elif rsi_val > 60:
            bearish_score += 1
        
        # MACD scoring
        if signals['indicators']['MACD']['bullish_cross']:
            bullish_score += 2
        elif signals['indicators']['MACD']['bearish_cross']:
            bearish_score += 2
        elif signals['indicators']['MACD']['trend'] == 'bullish':
            bullish_score += 1
        elif signals['indicators']['MACD']['trend'] == 'bearish':
            bearish_score += 1
        
        # Moving Average scoring
        if signals['indicators']['MA']['golden_cross']:
            bullish_score += 2
        if signals['indicators']['MA']['price_above_SMA200']:
            bullish_score += 1
        elif not signals['indicators']['MA']['price_above_SMA200']:
            bearish_score += 1
        
        # Bollinger Bands scoring
        if signals['indicators']['BB']['lower_touch']:
            bullish_score += 1
        elif signals['indicators']['BB']['upper_touch']:
            bearish_score += 1
        
        # Volume confirmation
        if signals['indicators']['Volume']['high_volume']:
            # High volume amplifies the dominant signal
            if bullish_score > bearish_score:
                bullish_score += 1
            elif bearish_score > bullish_score:
                bearish_score += 1
        
        # Determine recommendation
        total_score = bullish_score + bearish_score
        if total_score == 0:
            recommendation = 'HOLD'
            confidence = 'low'
        elif bullish_score > bearish_score * 1.5:
            recommendation = 'BUY'
            confidence = 'high' if bullish_score >= 6 else 'medium'
        elif bearish_score > bullish_score * 1.5:
            recommendation = 'SELL'
            confidence = 'high' if bearish_score >= 6 else 'medium'
        else:
            recommendation = 'HOLD'
            confidence = 'medium'
        
        return {
            'bullish_score': bullish_score,
            'bearish_score': bearish_score,
            'recommendation': recommendation,
            'confidence': confidence,
            'strength': abs(bullish_score - bearish_score)
        }
