"""
Predict Price Movers - Find stocks about to explode!
Combines AI forecasting with technical indicators
"""

import sys
sys.path.insert(0, 'src')

import pandas as pd
from tools import StockDataFetcher, TechnicalAnalyzer
from stock_forecaster import StockForecaster
from pattern_detectors.technical_indicators import TechnicalIndicators
import yfinance as yf
from datetime import datetime, timedelta
import yaml
from loguru import logger

class PriceMoverPredictor:
    """
    Predicts which stocks are about to move significantly
    """
    
    def __init__(self):
        with open('config/config.yaml', 'r') as f:
            self.config = yaml.safe_load(f)
        self.fetcher = StockDataFetcher(self.config)
        self.tech_analyzer = TechnicalAnalyzer()
        self.tech_indicators = TechnicalIndicators()
    
    def calculate_explosion_score(self, ticker: str) -> dict:
        """
        Calculate explosion score (0-100) for a stock
        
        Components:
        1. Forecast upside (0-40 points)
        2. Technical indicators (0-30 points)
        3. Volume & momentum (0-30 points)
        """
        try:
            print(f"\nAnalyzing {ticker}...")
            
            # Get data
            stock = yf.Ticker(ticker)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=180)
            historical_data = stock.history(start=start_date, end=end_date)
            
            if historical_data.empty:
                return None
            
            current_price = historical_data['Close'].iloc[-1]
            
            # 1. FORECAST SCORE (0-40 points)
            forecaster = StockForecaster(ticker, historical_data)
            forecast_result = forecaster.run_all_forecasts(days_ahead=30)
            
            if not forecast_result:
                return None
            
            # Score based on expected change
            expected_change = forecast_result['change_percent']
            forecast_score = min(40, max(0, expected_change * 2))  # 20% = 40 points
            
            # Bonus for narrow confidence range (high certainty)
            range_width = (forecast_result['upper_bound'] - forecast_result['lower_bound']) / current_price * 100
            certainty_bonus = max(0, 20 - range_width / 2)  # Narrow range = bonus
            forecast_score += min(10, certainty_bonus)
            
            # 2. TECHNICAL INDICATORS SCORE (0-30 points)
            tech_score = 0
            
            # Calculate RSI manually
            delta = historical_data['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            current_rsi = rsi.iloc[-1] if not rsi.empty else 50
            
            if 30 < current_rsi < 50:  # Coming out of oversold
                tech_score += 10
            elif current_rsi > 50:  # Strong momentum
                tech_score += 5
            
            # MACD - simple calculation
            exp1 = historical_data['Close'].ewm(span=12, adjust=False).mean()
            exp2 = historical_data['Close'].ewm(span=26, adjust=False).mean()
            macd = exp1 - exp2
            signal = macd.ewm(span=9, adjust=False).mean()
            histogram = macd - signal
            
            if histogram.iloc[-1] > 0 and macd.iloc[-1] > signal.iloc[-1]:  # Bullish
                tech_score += 10
            elif histogram.iloc[-1] > 0:
                tech_score += 5
            
            # Moving averages - price above MAs
            ma20 = historical_data['Close'].rolling(20).mean().iloc[-1]
            ma50 = historical_data['Close'].rolling(50).mean().iloc[-1]
            
            if current_price > ma20:
                tech_score += 5
            if current_price > ma50:
                tech_score += 5
            
            # 3. VOLUME & MOMENTUM SCORE (0-30 points)
            volume_score = 0
            
            # Volume surge
            avg_volume = historical_data['Volume'].rolling(20).mean().iloc[-1]
            recent_volume = historical_data['Volume'].iloc[-5:].mean()
            volume_ratio = recent_volume / avg_volume
            
            if volume_ratio > 1.5:  # 50% above average
                volume_score += 15
            elif volume_ratio > 1.2:
                volume_score += 10
            elif volume_ratio > 1.0:
                volume_score += 5
            
            # Price momentum (last 5 days)
            price_5d_ago = historical_data['Close'].iloc[-5]
            momentum = ((current_price - price_5d_ago) / price_5d_ago) * 100
            
            if momentum > 3:  # Strong momentum
                volume_score += 15
            elif momentum > 1:
                volume_score += 10
            elif momentum > 0:
                volume_score += 5
            
            # TOTAL SCORE
            total_score = min(100, forecast_score + tech_score + volume_score)
            
            # Calculate risk/reward ratio
            upside = (forecast_result['upper_bound'] - current_price) / current_price * 100
            downside = (current_price - forecast_result['lower_bound']) / current_price * 100
            risk_reward = upside / downside if downside > 0 else 0
            
            return {
                'ticker': ticker,
                'current_price': current_price,
                'predicted_price': forecast_result['predicted_price'],
                'expected_change': expected_change,
                'bullish_target': forecast_result['upper_bound'],
                'bearish_target': forecast_result['lower_bound'],
                'explosion_score': round(total_score, 1),
                'forecast_score': round(forecast_score, 1),
                'technical_score': round(tech_score, 1),
                'volume_score': round(volume_score, 1),
                'risk_reward_ratio': round(risk_reward, 2),
                'rsi': round(current_rsi, 1) if not pd.isna(current_rsi) else None,
                'volume_ratio': round(volume_ratio, 2),
                'momentum_5d': round(momentum, 2)
            }
            
        except Exception as e:
            logger.error(f"Error analyzing {ticker}: {e}")
            return None
    
    def scan_watchlist(self, tickers: list, min_score: float = 50.0):
        """
        Scan multiple stocks and find the best opportunities
        
        Args:
            tickers: List of stock symbols
            min_score: Minimum explosion score to consider
        """
        print("\n" + "="*80)
        print("PRICE MOVER PREDICTION SCANNER")
        print("="*80)
        print(f"Scanning {len(tickers)} stocks for explosive opportunities...")
        print(f"Minimum score threshold: {min_score}")
        print("="*80)
        
        results = []
        
        for ticker in tickers:
            score_data = self.calculate_explosion_score(ticker)
            if score_data and score_data['explosion_score'] >= min_score:
                results.append(score_data)
        
        # Sort by explosion score
        results.sort(key=lambda x: x['explosion_score'], reverse=True)
        
        # Display results
        print(f"\n{'='*80}")
        print(f"FOUND {len(results)} HIGH-POTENTIAL STOCKS")
        print('='*80)
        
        if not results:
            print("\nNo stocks found above minimum score threshold.")
            print("Try lowering min_score or adding more stocks to watchlist.")
            return []
        
        print(f"\n{'Rank':<5} {'Ticker':<8} {'Score':<7} {'Price':<10} {'Forecast':<12} {'R/R':<8} {'Signal'}")
        print("-" * 80)
        
        for i, result in enumerate(results, 1):
            signal = self._get_signal(result)
            print(f"{i:<5} {result['ticker']:<8} {result['explosion_score']:<7.1f} "
                  f"${result['current_price']:<9.2f} {result['expected_change']:>+6.1f}% "
                  f"({result['predicted_price']:.2f})  {result['risk_reward_ratio']:<8.2f} {signal}")
        
        # Detailed top 3
        print(f"\n{'='*80}")
        print("TOP 3 DETAILED ANALYSIS")
        print('='*80)
        
        for i, result in enumerate(results[:3], 1):
            self._print_detailed_analysis(i, result)
        
        return results
    
    def _get_signal(self, result: dict) -> str:
        """Get trading signal based on score"""
        score = result['explosion_score']
        if score >= 80:
            return "STRONG BUY"
        elif score >= 70:
            return "BUY"
        elif score >= 60:
            return "Consider"
        else:
            return "Watch"
    
    def _print_detailed_analysis(self, rank: int, result: dict):
        """Print detailed analysis for a stock"""
        print(f"\n#{rank}. {result['ticker']} - Explosion Score: {result['explosion_score']:.1f}/100")
        print("-" * 50)
        print(f"Current Price:    ${result['current_price']:.2f}")
        print(f"30-Day Forecast:  ${result['predicted_price']:.2f} ({result['expected_change']:+.1f}%)")
        print(f"Bullish Target:   ${result['bullish_target']:.2f}")
        print(f"Bearish Target:   ${result['bearish_target']:.2f}")
        print(f"\nScore Breakdown:")
        print(f"  Forecast:       {result['forecast_score']:.1f}/50 points")
        print(f"  Technical:      {result['technical_score']:.1f}/30 points")
        print(f"  Volume/Momentum: {result['volume_score']:.1f}/30 points")
        print(f"\nKey Metrics:")
        print(f"  RSI:            {result['rsi']:.1f}")
        print(f"  Volume Ratio:   {result['volume_ratio']:.2f}x")
        print(f"  5D Momentum:    {result['momentum_5d']:+.2f}%")
        print(f"  Risk/Reward:    {result['risk_reward_ratio']:.2f}")
        print(f"\nSignal: {self._get_signal(result)}")

def main():
    """Main execution"""
    
    predictor = PriceMoverPredictor()
    
    # YOUR WATCHLIST - Edit this!
    watchlist = [
        
        # Add your stocks here:
        "OCS", "CYTK", "APLD", "ACHR", "PL", "RDW", "RCAT", "AIRO", "ACHR",
        "AIRI", "BETA", "BYRN", "AAPG", "TAK", "HLN", "PHG", "ROIV", "EQNR",
        "KMI", "ET", "PBR", "RTO", "QXO", "JOBY", "ZTO", "CNH"
    ]
    
    # Scan for opportunities
    # Lower min_score to see more results (e.g., 40.0)
    results = predictor.scan_watchlist(watchlist, min_score=50.0)
    
    print(f"\n{'='*80}")
    print("SCAN COMPLETE!")
    print('='*80)
    print(f"\nNext steps:")
    print("1. Research the top-ranked stocks in detail")
    print("2. Check news and fundamentals")
    print("3. Set price alerts for entry points")
    print("4. Consider position sizing based on risk/reward")
    print("="*80 + "\n")

if __name__ == "__main__":
    main()
