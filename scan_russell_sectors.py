"""
Russell Index Sector Scanner
Scan all stocks in Russell 2000/1000 by sector and find explosive opportunities
"""

import sys
sys.path.insert(0, 'src')

import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from stock_forecaster import StockForecaster
from loguru import logger
import yaml
from tools import StockDataFetcher

class RussellSectorScanner:
    """
    Scan Russell index stocks by sector
    """
    
    # Russell 2000 sectors with representative stocks
    RUSSELL_SECTORS = {
        "Technology": [
            "OCS", "CYTK", "APLD", "LUNR", "SLS"]
    }
    
    def __init__(self):
        with open('config/config.yaml', 'r') as f:
            self.config = yaml.safe_load(f)
        self.fetcher = StockDataFetcher(self.config)
    
    def calculate_simple_score(self, ticker: str) -> dict:
        """
        Calculate simplified explosion score for faster scanning
        """
        try:
            # Get data
            stock = yf.Ticker(ticker)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=180)
            historical_data = stock.history(start=start_date, end=end_date)
            
            if historical_data.empty or len(historical_data) < 50:
                return None
            
            current_price = historical_data['Close'].iloc[-1]
            
            # Quick forecast
            forecaster = StockForecaster(ticker, historical_data)
            forecast_result = forecaster.run_all_forecasts(days_ahead=7)
            
            if not forecast_result:
                return None
            
            expected_change = forecast_result['change_percent']
            
            # Simple scoring
            forecast_score = min(50, max(0, expected_change * 2.5))
            
            # Volume check
            avg_volume = historical_data['Volume'].rolling(20).mean().iloc[-1]
            recent_volume = historical_data['Volume'].iloc[-5:].mean()
            volume_ratio = recent_volume / avg_volume
            volume_score = min(25, volume_ratio * 15)
            
            # Momentum
            price_5d_ago = historical_data['Close'].iloc[-5]
            momentum = ((current_price - price_5d_ago) / price_5d_ago) * 100
            momentum_score = min(25, max(0, momentum * 5))
            
            total_score = forecast_score + volume_score + momentum_score
            
            return {
                'ticker': ticker,
                'score': round(total_score, 1),
                'current_price': current_price,
                'forecast': forecast_result['predicted_price'],
                'change_pct': expected_change,
                'volume_ratio': round(volume_ratio, 2),
                'momentum': round(momentum, 2)
            }
            
        except Exception as e:
            logger.debug(f"Error analyzing {ticker}: {e}")
            return None
    
    def scan_sector(self, sector: str, min_score: float = 50.0):
        """
        Scan all stocks in a specific sector
        
        Args:
            sector: Sector name (e.g., 'Technology', 'Healthcare')
            min_score: Minimum score threshold
        """
        if sector not in self.RUSSELL_SECTORS:
            print(f"Unknown sector: {sector}")
            print(f"Available sectors: {', '.join(self.RUSSELL_SECTORS.keys())}")
            return []
        
        tickers = self.RUSSELL_SECTORS[sector]
        
        print("\n" + "="*80)
        print(f"SCANNING RUSSELL SECTOR: {sector.upper()}")
        print("="*80)
        print(f"Analyzing {len(tickers)} stocks...")
        print(f"Minimum score threshold: {min_score}")
        print("="*80 + "\n")
        
        results = []
        
        for i, ticker in enumerate(tickers, 1):
            print(f"[{i}/{len(tickers)}] {ticker}...", end=" ", flush=True)
            
            score_data = self.calculate_simple_score(ticker)
            
            if score_data and score_data['score'] >= min_score:
                results.append(score_data)
                print(f"✓ Score: {score_data['score']:.1f}")
            else:
                print("✗")
        
        # Sort by score
        results.sort(key=lambda x: x['score'], reverse=True)
        
        # Display results
        self._display_results(sector, results)
        
        return results
    
    def scan_all_sectors(self, min_score: float = 50.0, top_n_per_sector: int = 3):
        """
        Scan all Russell sectors and find top opportunities in each
        
        Args:
            min_score: Minimum score threshold
            top_n_per_sector: Number of top stocks to show per sector
        """
        print("\n" + "="*80)
        print("RUSSELL INDEX FULL SECTOR SCAN")
        print("="*80)
        print(f"Scanning {len(self.RUSSELL_SECTORS)} sectors")
        print(f"Minimum score: {min_score} | Top stocks per sector: {top_n_per_sector}")
        print("="*80 + "\n")
        
        all_results = {}
        
        for sector in self.RUSSELL_SECTORS.keys():
            print(f"\n🔍 {sector}...")
            results = self.scan_sector(sector, min_score)
            all_results[sector] = results[:top_n_per_sector]
        
        # Summary
        print("\n" + "="*80)
        print("CROSS-SECTOR SUMMARY")
        print("="*80)
        
        # Flatten and sort all results
        all_stocks = []
        for sector, stocks in all_results.items():
            for stock in stocks:
                stock['sector'] = sector
                all_stocks.append(stock)
        
        all_stocks.sort(key=lambda x: x['score'], reverse=True)
        
        print(f"\nTOP 10 OPPORTUNITIES ACROSS ALL SECTORS:\n")
        print(f"{'Rank':<5} {'Ticker':<8} {'Sector':<20} {'Score':<7} {'Price':<10} {'Forecast':<10} {'Signal'}")
        print("-" * 80)
        
        for i, stock in enumerate(all_stocks[:10], 1):
            signal = self._get_signal(stock['score'])
            print(f"{i:<5} {stock['ticker']:<8} {stock['sector']:<20} {stock['score']:<7.1f} "
                  f"${stock['current_price']:<9.2f} {stock['change_pct']:>+6.1f}%  {signal}")
        
        print("="*80 + "\n")
        
        return all_results
    
    def _display_results(self, sector: str, results: list):
        """Display sector scan results"""
        print(f"\n{'='*80}")
        print(f"RESULTS FOR {sector.upper()}")
        print('='*80)
        
        if not results:
            print("\nNo stocks found above minimum score threshold.")
            return
        
        print(f"\nFOUND {len(results)} HIGH-POTENTIAL STOCKS:\n")
        print(f"{'Rank':<5} {'Ticker':<8} {'Score':<7} {'Price':<10} {'Forecast':<10} {'Vol':<8} {'Mom':<8} {'Signal'}")
        print("-" * 80)
        
        for i, stock in enumerate(results, 1):
            signal = self._get_signal(stock['score'])
            print(f"{i:<5} {stock['ticker']:<8} {stock['score']:<7.1f} "
                  f"${stock['current_price']:<9.2f} {stock['change_pct']:>+6.1f}%  "
                  f"{stock['volume_ratio']:<7.2f}x {stock['momentum']:>+6.1f}%  {signal}")
        
        # Top 3 details
        if len(results) >= 3:
            print(f"\n{'='*80}")
            print(f"TOP 3 IN {sector.upper()}")
            print('='*80)
            
            for i, stock in enumerate(results[:3], 1):
                print(f"\n#{i}. {stock['ticker']} - Score: {stock['score']:.1f}/100")
                print("-" * 50)
                print(f"Current:  ${stock['current_price']:.2f}")
                print(f"Forecast: ${stock['forecast']:.2f} ({stock['change_pct']:+.1f}%)")
                print(f"Volume:   {stock['volume_ratio']:.2f}x average")
                print(f"Momentum: {stock['momentum']:+.1f}% (5-day)")
                print(f"Signal:   {self._get_signal(stock['score'])}")
    
    def _get_signal(self, score: float) -> str:
        """Get trading signal based on score"""
        if score >= 80:
            return "STRONG BUY"
        elif score >= 70:
            return "BUY"
        elif score >= 60:
            return "Consider"
        elif score >= 50:
            return "Watch"
        else:
            return "Skip"

def main():
    """Main execution"""
    scanner = RussellSectorScanner()
    
    print("\n" + "="*80)
    print("RUSSELL INDEX SECTOR SCANNER")
    print("="*80)
    print("\nChoose scan mode:")
    print("1. Scan specific sector")
    print("2. Scan all sectors (takes longer)")
    print("3. Quick scan - Technology only")
    print("4. Quick scan - Healthcare only")
    print("5. Quick scan - Financials only")
    print("="*80)
    
    # For now, let's do a quick tech scan
    # You can uncomment different options below:
    
    # Option 1: Scan single sector
    scanner.scan_sector("Technology", min_score=45.0)
    
    # Option 2: Scan all sectors (uncomment to use)
    # scanner.scan_all_sectors(min_score=50.0, top_n_per_sector=3)
    
    # Option 3: Scan multiple specific sectors
    # for sector in ["Technology", "Healthcare", "Financials"]:
    #     scanner.scan_sector(sector, min_score=45.0)
    
    print(f"\n{'='*80}")
    print("SCAN COMPLETE!")
    print('='*80)
    print("\nNext steps:")
    print("1. Research top-ranked stocks")
    print("2. Check sector trends")
    print("3. Compare opportunities across sectors")
    print("4. Set price alerts for best opportunities")
    print("="*80 + "\n")

if __name__ == "__main__":
    main()
