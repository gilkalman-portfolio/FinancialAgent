"""
Get Russell 2000 stocks by sector from Wikipedia or other sources
"""

import pandas as pd
import requests
from bs4 import BeautifulSoup
import yfinance as yf
from loguru import logger

def get_russell_2000_from_wiki():
    """
    Get Russell 2000 stocks from Wikipedia
    Returns DataFrame with ticker, name, sector
    """
    try:
        url = "https://en.wikipedia.org/wiki/Russell_2000_Index"
        
        # Read tables from Wikipedia
        tables = pd.read_html(url)
        
        # The Russell 2000 constituent table
        df = tables[2]  # Usually the 3rd table
        
        # Clean up
        df.columns = ['Ticker', 'Company', 'Sector', 'Industry']
        
        print(f"✓ Found {len(df)} Russell 2000 stocks")
        print(f"✓ Sectors: {df['Sector'].nunique()}")
        
        return df
        
    except Exception as e:
        logger.error(f"Failed to get Russell 2000 from Wikipedia: {e}")
        return None

def get_sp500_etf_holdings():
    """
    Alternative: Get holdings from Russell 2000 ETF (IWM)
    """
    try:
        # This is a backup method using ETF holdings
        print("Fetching Russell 2000 ETF (IWM) holdings...")
        
        # Note: You'd need to scrape from iShares or use their API
        # For now, we'll use a manual curated list
        
        return None
        
    except Exception as e:
        logger.error(f"Failed to get ETF holdings: {e}")
        return None

def filter_by_sector(df, sector_name):
    """
    Filter DataFrame by sector
    
    Args:
        df: DataFrame with Russell stocks
        sector_name: Sector to filter (e.g., 'Technology', 'Healthcare')
    
    Returns:
        List of tickers in that sector
    """
    if df is None:
        return []
    
    # Normalize sector name
    sector_map = {
        'tech': 'Technology',
        'healthcare': 'Health Care',
        'health': 'Health Care',
        'financial': 'Financials',
        'finance': 'Financials',
        'consumer': 'Consumer Discretionary',
        'energy': 'Energy',
        'industrial': 'Industrials',
        'materials': 'Materials',
        'utilities': 'Utilities',
        'realestate': 'Real Estate',
        'communication': 'Communication Services'
    }
    
    sector_name_lower = sector_name.lower().replace(' ', '')
    matched_sector = sector_map.get(sector_name_lower, sector_name)
    
    # Filter
    sector_stocks = df[df['Sector'].str.contains(matched_sector, case=False, na=False)]
    
    tickers = sector_stocks['Ticker'].tolist()
    
    print(f"\n✓ Found {len(tickers)} stocks in {matched_sector}")
    
    return tickers

def get_all_sectors(df):
    """Get list of all available sectors"""
    if df is None:
        return []
    
    sectors = df['Sector'].unique().tolist()
    return sorted(sectors)

def save_to_csv(df, filename='russell_2000_stocks.csv'):
    """Save stocks to CSV for future use"""
    if df is not None:
        df.to_csv(filename, index=False)
        print(f"✓ Saved to {filename}")

def load_from_csv(filename='russell_2000_stocks.csv'):
    """Load stocks from saved CSV"""
    try:
        df = pd.read_csv(filename)
        print(f"✓ Loaded {len(df)} stocks from {filename}")
        return df
    except:
        return None

def main():
    """Test the fetching"""
    
    print("="*80)
    print("RUSSELL 2000 STOCK FETCHER")
    print("="*80)
    
    # Try to load from saved file first
    df = load_from_csv()
    
    # If not available, fetch from Wikipedia
    if df is None:
        print("\nFetching from Wikipedia...")
        df = get_russell_2000_from_wiki()
        
        if df is not None:
            save_to_csv(df)
    
    if df is None:
        print("\n❌ Could not fetch Russell 2000 stocks")
        return
    
    # Show available sectors
    print("\n" + "="*80)
    print("AVAILABLE SECTORS")
    print("="*80)
    sectors = get_all_sectors(df)
    for i, sector in enumerate(sectors, 1):
        count = len(df[df['Sector'] == sector])
        print(f"{i}. {sector:<30} ({count} stocks)")
    
    # Example: Get Technology stocks
    print("\n" + "="*80)
    print("EXAMPLE: Technology Stocks")
    print("="*80)
    tech_stocks = filter_by_sector(df, 'Technology')
    print(f"\nFirst 20 tickers: {tech_stocks[:20]}")
    
    print("\n" + "="*80)
    print("DATA READY!")
    print("="*80)
    print("\nYou can now use this data with scan_russell_sectors.py")
    print("The data is saved in 'russell_2000_stocks.csv'")

if __name__ == "__main__":
    main()
