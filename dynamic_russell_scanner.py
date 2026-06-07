"""
Dynamic Russell Scanner - Fetches real Russell stocks by sector
Uses multiple data sources to get actual Russell 2000/1000 constituents
"""

import sys
sys.path.insert(0, 'src')

import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from stock_forecaster import StockForecaster
from loguru import logger
import yaml
import requests
from bs4 import BeautifulSoup
import time

class DynamicRussellScanner:
    """
    Dynamically fetch and scan Russell stocks by sector
    """
    
    def __init__(self):
        with open('config/config.yaml', 'r') as f:
            self.config = yaml.safe_load(f)
        self.russell_stocks = None
    
    def fetch_russell_from_slickcharts(self):
        """
        Fetch Russell 2000 stocks from Slickcharts
        More reliable than Wikipedia
        """
        try:
            print("Fetching Russell 2000 stocks from Slickcharts...")
            
            url = "https://www.slickcharts.com/russell2000"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(url, headers=headers)
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Find the table
            table = soup.find('table', {'class': 'table'})
            
            if not table:
                raise Exception("Could not find stock table")
            
            # Parse table
            rows = table.find_all('tr')[1:]  # Skip header
            
            stocks = []
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 3:
                    ticker = cols[2].text.strip()
                    company = cols[1].text.strip()
                    stocks.append({
                        'ticker': ticker,
                        'company': company,
                        'sector': None  # Will fill this later
                    })
            
            df = pd.DataFrame(stocks)
            print(f"✓ Fetched {len(df)} Russell 2000 stocks from Slickcharts")
            
            return df
            
        except Exception as e:
            logger.error(f"Failed to fetch from Slickcharts: {e}")
            return None
    
    def fetch_russell_from_ishares(self):
        """
        Fetch Russell 2000 stocks from iShares IWM ETF holdings
        Most accurate source!
        """
        try:
            print("Fetching Russell 2000 from iShares IWM ETF...")
            
            # iShares provides CSV downloads
            url = "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(url, headers=headers)
            
            # Parse CSV (skip first rows which are metadata)
            from io import StringIO
            csv_data = StringIO(response.text)
            
            # Skip metadata rows
            for _ in range(10):
                next(csv_data)
            
            df = pd.read_csv(csv_data)
            
            # Clean up
            df = df[['Ticker', 'Name', 'Sector']].copy()
            df.columns = ['ticker', 'company', 'sector']
            df = df.dropna(subset=['ticker'])
            df = df[df['ticker'] != '-']
            
            print(f"✓ Fetched {len(df)} Russell 2000 stocks from iShares")
            print(f"✓ Sectors available: {df['sector'].nunique()}")
            
            return df
            
        except Exception as e:
            logger.error(f"Failed to fetch from iShares: {e}")
            return None
    
    def enrich_with_yfinance_sectors(self, df):
        """
        Use yfinance to get sector info for stocks
        """
        print("\nEnriching with sector data from Yahoo Finance...")
        print("This may take a few minutes...")
        
        for idx, row in df.iterrows():
            if pd.isna(row['sector']) or row['sector'] == 'N/A':
                try:
                    ticker = row['ticker']
                    stock = yf.Ticker(ticker)
                    info = stock.info
                    
                    sector = info.get('sector', 'Unknown')
                    df.at[idx, 'sector'] = sector
                    
                    if idx % 50 == 0:
                        print(f"  Processed {idx}/{len(df)} stocks...")
                    
                    time.sleep(0.1)  # Rate limiting
                    
                except Exception as e:
                    logger.debug(f"Could not get sector for {ticker}: {e}")
                    continue
        
        print(f"✓ Enrichment complete!")
        return df
    
    def load_or_fetch_russell(self, force_refresh=False):
        """
        Load Russell stocks from cache or fetch fresh
        """
        cache_file = 'russell_2000_cache.csv'
        
        # Try to load from cache
        if not force_refresh:
            try:
                df = pd.read_csv(cache_file)
                print(f"✓ Loaded {len(df)} Russell stocks from cache")
                print(f"✓ Sectors: {df['sector'].nunique()}")
                self.russell_stocks = df
                return df
            except:
                print("No cache found, fetching fresh data...")
        
        # Fetch fresh data
        df = self.fetch_russell_from_ishares()
        
        if df is None:
            df = self.fetch_russell_from_slickcharts()
        
        if df is None:
            raise Exception("Could not fetch Russell stocks from any source")
        
        # Enrich with sectors if needed
        missing_sectors = df['sector'].isna().sum()
        if missing_sectors > 0:
            print(f"\n{missing_sectors} stocks missing sector data...")
            df = self.enrich_with_yfinance_sectors(df)
        
        # Save to cache
        df.to_csv(cache_file, index=False)
        print(f"\n✓ Saved to cache: {cache_file}")
        
        self.russell_stocks = df
        return df
    
    def get_sectors(self):
        """Get list of available sectors"""
        if self.russell_stocks is None:
            self.load_or_fetch_russell()
        
        sectors = self.russell_stocks['sector'].unique()
        sectors = [s for s in sectors if pd.notna(s) and s != 'Unknown']
        return sorted(sectors)
    
    def get_stocks_by_sector(self, sector_name):
        """
        Get all stocks in a specific sector
        
        Args:
            sector_name: Sector name (e.g., 'Technology', 'Healthcare')
        
        Returns:
            List of tickers in that sector
        """
        if self.russell_stocks is None:
            self.load_or_fetch_russell()
        
        # Flexible matching
        sector_name_lower = sector_name.lower()
        
        # Try exact match first
        sector_stocks = self.russell_stocks[
            self.russell_stocks['sector'].str.lower() == sector_name_lower
        ]
        
        # If no exact match, try contains
        if len(sector_stocks) == 0:
            sector_stocks = self.russell_stocks[
                self.russell_stocks['sector'].str.lower().str.contains(sector_name_lower, na=False)
            ]
        
        tickers = sector_stocks['ticker'].tolist()
        
        print(f"\n✓ Found {len(tickers)} stocks in '{sector_name}' sector")
        
        return tickers
    
    def calculate_simple_score(self, ticker: str):
        """Calculate explosion score for a stock"""
        try:
            stock = yf.Ticker(ticker)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=180)
            historical_data = stock.history(start=start_date, end=end_date)
            
            if historical_data.empty or len(historical_data) < 50:
                return None
            
            current_price = historical_data['Close'].iloc[-1]
            
            # Quick forecast
            forecaster = StockForecaster(ticker, historical_data)
            forecast_result = forecaster.run_all_forecasts(days_ahead=30)
            
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
                'current_price': current_price,
                'forecast': forecast_result['predicted_price'],
                'change_pct': expected_change,
                'volume_ratio': round(volume_ratio, 2),
                'momentum': round(momentum, 2)
            }
            
        except Exception as e:
            logger.debug(f"Error analyzing {ticker}: {e}")
            return None
    
    def scan_sector(self, sector_name: str, min_score: float = 50.0, max_stocks: int = None):
        """
        Scan all stocks in a sector
        
        Args:
            sector_name: Sector name
            min_score: Minimum explosion score
            max_stocks: Maximum number of stocks to scan (None = all)
        """
        tickers = self.get_stocks_by_sector(sector_name)
        
        if not tickers:
            print(f"No stocks found for sector: {sector_name}")
            return []
        
        if max_stocks:
            tickers = tickers[:max_stocks]
            print(f"Limiting scan to first {max_stocks} stocks")
        
        print("\n" + "="*80)
        print(f"DYNAMIC RUSSELL SECTOR SCAN: {sector_name.upper()}")
        print("="*80)
        print(f"Scanning {len(tickers)} stocks...")
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
        self._display_results(sector_name, results)
        
        return results
    
    def _display_results(self, sector: str, results: list):
        """Display scan results"""
        print(f"\n{'='*80}")
        print(f"RESULTS FOR {sector.upper()}")
        print('='*80)
        
        if not results:
            print("\nNo stocks found above minimum score threshold.")
            print("Try lowering min_score or scanning a different sector.")
            return
        
        print(f"\nFOUND {len(results)} HIGH-POTENTIAL STOCKS:\n")
        print(f"{'Rank':<5} {'Ticker':<8} {'Score':<7} {'Price':<10} {'Forecast':<10} {'Vol':<8} {'Mom':<8} {'Signal'}")
        print("-" * 80)
        
        for i, stock in enumerate(results, 1):
            signal = self._get_signal(stock['score'])
            print(f"{i:<5} {stock['ticker']:<8} {stock['score']:<7.1f} "
                  f"${stock['current_price']:<9.2f} {stock['change_pct']:>+6.1f}%  "
                  f"{stock['volume_ratio']:<7.2f}x {stock['momentum']:>+6.1f}%  {signal}")
        
        # Top 5 details
        top_n = min(5, len(results))
        if top_n > 0:
            print(f"\n{'='*80}")
            print(f"TOP {top_n} IN {sector.upper()}")
            print('='*80)
            
            for i, stock in enumerate(results[:top_n], 1):
                print(f"\n#{i}. {stock['ticker']} - Score: {stock['score']:.1f}/100")
                print("-" * 50)
                print(f"Current:  ${stock['current_price']:.2f}")
                print(f"Forecast: ${stock['forecast']:.2f} ({stock['change_pct']:+.1f}%)")
                print(f"Volume:   {stock['volume_ratio']:.2f}x average")
                print(f"Momentum: {stock['momentum']:+.1f}% (5-day)")
                print(f"Signal:   {self._get_signal(stock['score'])}")
    
    def _get_signal(self, score: float) -> str:
        """Get trading signal"""
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
    scanner = DynamicRussellScanner()
    
    print("\n" + "="*80)
    print("DYNAMIC RUSSELL 2000 SECTOR SCANNER")
    print("="*80)
    print("Fetches REAL Russell 2000 stocks by sector dynamically!")
    print("="*80 + "\n")
    
    # Step 1: Load or fetch Russell stocks
    try:
        df = scanner.load_or_fetch_russell(force_refresh=False)
    except Exception as e:
        print(f"Error: {e}")
        return
    
    # Step 2: Show available sectors
    print("\n" + "="*80)
    print("AVAILABLE SECTORS")
    print("="*80)
    sectors = scanner.get_sectors()
    for i, sector in enumerate(sectors, 1):
        count = len(scanner.get_stocks_by_sector(sector))
        print(f"{i}. {sector:<40} ({count} stocks)")
    
    # Step 3: Scan a sector
    print("\n" + "="*80)
    print("EXAMPLE SCAN")
    print("="*80)
    
    # Change this to scan different sectors:
    sector_to_scan = "Technology"  # ← Change this!
    max_stocks = 20  # ← Limit to first 20 stocks for speed
    
    scanner.scan_sector(
        sector_name=sector_to_scan,
        min_score=45.0,
        max_stocks=max_stocks
    )
    
    print(f"\n{'='*80}")
    print("SCAN COMPLETE!")
    print('='*80)
    print("\nNext steps:")
    print("1. Change 'sector_to_scan' variable to scan different sectors")
    print("2. Adjust 'max_stocks' to scan more/fewer stocks")
    print("3. Lower 'min_score' to see more results")
    print("4. Set force_refresh=True to get latest Russell constituents")
    print("="*80 + "\n")

if __name__ == "__main__":
    main()
