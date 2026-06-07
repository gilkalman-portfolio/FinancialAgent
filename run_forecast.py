"""
Quick Start - Stock Price Forecasting
Run this file to get 30-day price forecasts for stocks
"""

import sys
sys.path.insert(0, 'src')

from tools import StockDataFetcher
from stock_forecaster import StockForecaster
from loguru import logger
import yaml

def forecast_stock(ticker: str, days_ahead: int = 30):
    """
    Generate price forecast for a stock
    
    Args:
        ticker: Stock symbol (e.g., 'NVDA', 'TSLA', 'AAPL')
        days_ahead: Number of days to forecast (default: 30)
    """
    print(f"\n{'='*70}")
    print(f"FORECASTING: {ticker}")
    print('='*70)
    
    try:
        # Load config
        with open('config/config.yaml', 'r') as f:
            config = yaml.safe_load(f)
        
        # Fetch data
        print(f"Fetching historical data for {ticker}...")
        fetcher = StockDataFetcher(config)
        stock_data = fetcher.get_stock_data(ticker)
        
        if not stock_data:
            print(f"ERROR: Could not fetch data for {ticker}")
            return
        
        # Get historical data directly from yfinance
        import yfinance as yf
        from datetime import datetime, timedelta
        stock = yf.Ticker(ticker)
        end_date = datetime.now()
        start_date = end_date - timedelta(days=180)
        historical_data = stock.history(start=start_date, end=end_date)
        
        if historical_data.empty:
            print(f"ERROR: No historical data for {ticker}")
            return
        
        current_price = stock_data.get('current_price', historical_data['Close'].iloc[-1])
        
        print(f"Current Price: ${current_price:.2f}")
        print(f"Generating {days_ahead}-day forecast...\n")
        
        # Run forecast
        forecaster = StockForecaster(ticker, historical_data)
        result = forecaster.run_all_forecasts(days_ahead=days_ahead)
        
        if not result:
            print(f"ERROR: Forecast failed for {ticker}")
            return
        
        # Display results
        print(f"\n{'-'*70}")
        print(f"30-DAY FORECAST RESULTS")
        print(f"{'-'*70}")
        print(f"Predicted Price: ${result['predicted_price']:.2f}")
        print(f"Expected Change: {result['change_percent']:+.2f}%")
        print(f"\nConfidence Range ({result['confidence_interval']}):")
        print(f"  Bullish Case:  ${result['upper_bound']:.2f} ({((result['upper_bound']/current_price - 1) * 100):+.2f}%)")
        print(f"  Base Case:     ${result['predicted_price']:.2f} ({result['change_percent']:+.2f}%)")
        print(f"  Bearish Case:  ${result['lower_bound']:.2f} ({((result['lower_bound']/current_price - 1) * 100):+.2f}%)")
        print(f"\nModels Used: {', '.join(result['models_used'])}")
        print('='*70)
        
    except Exception as e:
        print(f"ERROR: {e}")
        logger.error(f"Forecast failed for {ticker}: {e}")

def main():
    """Main execution"""
    
    print("\n" + "="*70)
    print("STOCK PRICE FORECASTING TOOL")
    print("="*70)
    print("\nGenerating 30-day price forecasts using AI models:")
    print("  - ARIMA (Statistical)")
    print("  - Moving Average + Trend")
    print("  - Exponential Smoothing")
    print("  - Ensemble (Combined)")
    
    # List of stocks to forecast
    # EDIT THIS LIST with your stocks!
    stocks = [
        "NVDA",   # NVIDIA
        "TSLA",   # Tesla
        "AAPL",   # Apple
        "OCS",
        "LUNR",
        "APLD",
        "CYTK",
        # Add more stocks here:
        # "PLTR",   # Palantir
        # "MSFT",   # Microsoft
        # "GOOGL",  # Google
    ]
    
    # Run forecasts
    for ticker in stocks:
        forecast_stock(ticker, days_ahead=30)
    
    print(f"\n{'='*70}")
    print("FORECASTING COMPLETE!")
    print('='*70)
    print("\nNext Steps:")
    print("1. Edit the 'stocks' list in this file to add your stocks")
    print("2. Change days_ahead parameter (7, 14, 30, 60)")
    print("3. View detailed logs in logs/agent.log")
    print('='*70 + "\n")

if __name__ == "__main__":
    main()
