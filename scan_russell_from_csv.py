"""
Russell Scanner from CSV - 100% Dynamic!
Download Russell 2000 holdings once, scan any sector anytime!

HOW TO USE:
1. Download from: https://www.ishares.com/us/products/239710/ishares-russell-2000-etf
2. Click "Holdings" → "Download to Excel"
3. Save as 'russell_holdings.csv' in project folder
4. Run this script!
"""

import sys
sys.path.insert(0, 'src')

import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from stock_forecaster import StockForecaster
from loguru import logger
import yaml

class CSVRussellScanner:
    """
    Scan Russell stocks dynamically from CSV file
    """
    
    def __init__(self, csv_path='russell_holdings.csv'):
        self.csv_path = csv_path
        self.russell_df = None
        with open('config/config.yaml', 'r') as f:
            self.config = yaml.safe_load(f)
    
    def load_russell_csv(self):
        """
        Load Russell stocks from downloaded CSV
        """
        try:
            print(f"Loading Russell 2000 stocks from {self.csv_path}...")
            
            # Try different skip row configurations
            for skip in [10, 9, 11, 0]:
                try:
                    df = pd.read_csv(self.csv_path, skiprows=skip)
                    
                    # Find columns that look like ticker, name, sector
                    ticker_col = None
                    name_col = None
                    sector_col = None
                    
                    for col in df.columns:
                        col_lower = str(col).lower()
                        if 'ticker' in col_lower or 'symbol' in col_lower:
                            ticker_col = col
                        elif 'name' in col_lower and not 'file' in col_lower:
                            name_col = col
                        elif 'sector' in col_lower:
                            sector_col = col
                    
                    if ticker_col and sector_col:
                        df = df[[ticker_col, name_col, sector_col]].copy()
                        df.columns = ['ticker', 'name', 'sector']
                        df = df.dropna(subset=['ticker', 'sector'])
                        df = df[df['ticker'] != '-']
                        
                        self.russell_df = df
                        
                        print(f"\n✓ Successfully loaded {len(df)} Russell 2000 stocks!")
                        print(f"✓ Found {df['sector'].nunique()} unique sectors")
                        
                        return df
                        
                except Exception as e:
                    continue
            
            raise Exception("Could not parse CSV file")
            
        except FileNotFoundError:
            print("\n" + "="*80)
            print("ERROR: russell_holdings.csv not found!")
            print("="*80)
            print("\nPlease download the file:")
            print("1. Go to: https://www.ishares.com/us/products/239710/ishares-russell-2000-etf")
            print("2. Click 'Holdings' tab")
            print("3. Click 'Download to Excel'")
            print("4. Save as 'russell_holdings.csv' in project folder")
            print("5. Run this script again")
            print("="*80)
            return None
        
        except Exception as e:
            logger.error(f"Error loading CSV: {e}")
            print(f"\n❌ Could not load CSV file: {e}")
            print("\nMake sure the file is formatted correctly.")
            return None
    
    def show_available_sectors(self):
        """Show all available sectors and stock counts"""
        if self.russell_df is None:
            self.load_russell_csv()
        
        if self.russell_df is None:
            return
        
        print("\n" + "="*80)
        print("AVAILABLE SECTORS IN RUSSELL 2000")
        print("="*80)
        
        sector_counts = self.russell_df['sector'].value_counts()
        
        for i, (sector, count) in enumerate(sector_counts.items(), 1):
            print(f"{i:2}. {sector:<40} ({count:4} stocks)")
        
        print("="*80)
    
    def get_stocks_by_sector(self, sector_name):
        """
        Get all stocks in a specific sector
        
        Args:
            sector_name: Sector name or partial match
        
        Returns:
            List of tickers
        """
        if self.russell_df is None:
            self.load_russell_csv()
        
        if self.russell_df is None:
            return []
        
        # Flexible matching
        sector_name_lower = sector_name.lower()
        
        # Try exact match
        mask = self.russell_df['sector'].str.lower() == sector_name_lower
        
        # If no exact match, try contains
        if mask.sum() == 0:
            mask = self.russell_df['sector'].str.lower().str.contains(sector_name_lower, na=False)
        
        sector_stocks = self.russell_df[mask]
        
        if len(sector_stocks) == 0:
            print(f"\n❌ No stocks found for sector: {sector_name}")
            print("Available sectors:")
            for sector in self.russell_df['sector'].unique()[:10]:
                print(f"  - {sector}")
            return []
        
        tickers = sector_stocks['ticker'].tolist()
        
        print(f"\n✓ Found {len(tickers)} stocks in '{sector_stocks['sector'].iloc[0]}' sector")
        
        return tickers
    
    def calculate_score(self, ticker: str, forecast_days: int = 60):
        """Calculate explosion score"""
        try:
            stock = yf.Ticker(ticker)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=180)
            historical_data = stock.history(start=start_date, end=end_date)
            
            if historical_data.empty or len(historical_data) < 50:
                return None
            
            current_price = historical_data['Close'].iloc[-1]
            
            # Forecast
            forecaster = StockForecaster(ticker, historical_data)
            forecast_result = forecaster.run_all_forecasts(days_ahead=forecast_days)
            
            if not forecast_result:
                return None
            
            expected_change = forecast_result['change_percent']
            forecast_score = min(50, max(0, expected_change * 2.5))
            
            # Volume
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
                'current_price': round(current_price, 2),
                'forecast': round(forecast_result['predicted_price'], 2),
                'change_pct': round(expected_change, 2),
                'volume_ratio': round(volume_ratio, 2),
                'momentum': round(momentum, 2)
            }
            
        except Exception as e:
            logger.debug(f"Error analyzing {ticker}: {e}")
            return None
    
    def scan_sector(self, sector_name: str, min_score: float = 45.0, max_stocks: int = 30, forecast_days: int = 30):
        """
        Scan all stocks in a sector
        
        Args:
            sector_name: Sector name
            min_score: Minimum score threshold
            max_stocks: Maximum number of stocks to scan
            forecast_days: Number of days ahead to forecast (default: 30)
        """
        tickers = self.get_stocks_by_sector(sector_name)
        
        if not tickers:
            return []
        
        if max_stocks and len(tickers) > max_stocks:
            print(f"Limiting scan to first {max_stocks} stocks (out of {len(tickers)} total)")
            tickers = tickers[:max_stocks]
        
        print("\n" + "="*80)
        print(f"RUSSELL SECTOR SCAN: {sector_name.upper()}")
        print("="*80)
        print(f"Scanning {len(tickers)} stocks...")
        print(f"Forecast period: {forecast_days} days")
        print(f"Minimum score threshold: {min_score}")
        print("="*80 + "\n")
        
        results = []
        
        for i, ticker in enumerate(tickers, 1):
            print(f"[{i}/{len(tickers)}] {ticker}...", end=" ", flush=True)
            
            score_data = self.calculate_score(ticker, forecast_days=forecast_days)
            
            if score_data and score_data['score'] >= min_score:
                results.append(score_data)
                print(f"✓ Score: {score_data['score']:.1f}")
            else:
                print("✗")
        
        # Sort by score
        results.sort(key=lambda x: x['score'], reverse=True)
        
        # Display results
        self._display_results(sector_name, results)
        
        return results
    
    def _display_results(self, sector: str, results: list):
        """Display results"""
        print(f"\n{'='*80}")
        print(f"RESULTS FOR {sector.upper()}")
        print('='*80)
        
        if not results:
            print("\n❌ No stocks found above minimum score threshold.")
            print("\nTry:")
            print("  - Lower min_score (e.g., 40.0 or 35.0)")
            print("  - Increase max_stocks to scan more")
            print("  - Try a different sector")
            return
        
        print(f"\n✓ FOUND {len(results)} HIGH-POTENTIAL STOCKS:\n")
        print(f"{'Rank':<5} {'Ticker':<8} {'Score':<7} {'Price':<10} {'Forecast':<12} {'Vol':<8} {'Mom':<8} {'Signal'}")
        print("-" * 80)
        
        for i, stock in enumerate(results, 1):
            signal = self._get_signal(stock['score'])
            print(f"{i:<5} {stock['ticker']:<8} {stock['score']:<7.1f} "
                  f"${stock['current_price']:<9.2f} {stock['change_pct']:>+6.1f}% ({stock['forecast']:.2f})  "
                  f"{stock['volume_ratio']:<7.2f}x {stock['momentum']:>+6.1f}%  {signal}")
        
        # Top 5 details
        top_n = min(5, len(results))
        if top_n > 0:
            print(f"\n{'='*80}")
            print(f"TOP {top_n} DETAILED ANALYSIS")
            print('='*80)
            
            for i, stock in enumerate(results[:top_n], 1):
                print(f"\n#{i}. {stock['ticker']} - Explosion Score: {stock['score']:.1f}/100")
                print("-" * 50)
                print(f"Current Price:    ${stock['current_price']:.2f}")
                print(f"30-Day Forecast:  ${stock['forecast']:.2f} ({stock['change_pct']:+.1f}%)")
                print(f"Volume Ratio:     {stock['volume_ratio']:.2f}x average")
                print(f"5-Day Momentum:   {stock['momentum']:+.1f}%")
                print(f"Signal:           {self._get_signal(stock['score'])}")
    
    def _get_signal(self, score: float) -> str:
        """Get trading signal"""
        if score >= 80:
            return "🚀 STRONG BUY"
        elif score >= 70:
            return "📈 BUY"
        elif score >= 60:
            return "🤔 Consider"
        elif score >= 50:
            return "👀 Watch"
        else:
            return "⏭️ Skip"

def main():
    """Main execution"""
    
    scanner = CSVRussellScanner()
    
    print("\n" + "="*80)
    print("DYNAMIC RUSSELL 2000 SCANNER FROM CSV")
    print("="*80)
    print("Scans REAL Russell 2000 stocks by sector!")
    print("="*80)
    
    # Load the CSV
    df = scanner.load_russell_csv()
    
    if df is None:
        return
    
    # Show available sectors
    scanner.show_available_sectors()
    
    print("\n" + "="*80)
    print("EXAMPLE SCAN")
    print("="*80)
    
    # Example: Scan Technology sector
    # CHANGE THESE SETTINGS:
    sector_to_scan = "Health Care"  # ← Change this to any sector!
    min_score = 45.0               # ← Lower = more results
    max_stocks = None                # ← How many stocks to scan
    
    results = scanner.scan_sector(
        sector_name=sector_to_scan,
        min_score=min_score,
        max_stocks=max_stocks
    )
    
    print(f"\n{'='*80}")
    print("SCAN COMPLETE!")
    print('='*80)
    print(f"\nScanned {max_stocks} stocks, found {len(results)} opportunities")
    print("\nNext steps:")
    print("1. Change 'sector_to_scan' to scan different sectors")
    print("2. Lower 'min_score' to see more results")
    print("3. Increase 'max_stocks' to scan more (slower)")
    print("4. Research the top-ranked stocks!")
    print("="*80 + "\n")

if __name__ == "__main__":
    main()
