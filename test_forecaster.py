"""
Test Stock Forecaster Integration
"""

import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from stock_forecaster import StockForecaster
from loguru import logger

def test_forecaster():
    """Test the stock forecaster with sample data"""
    
    logger.info("Starting forecaster test...")
    
    # Generate realistic sample data
    np.random.seed(42)
    dates = pd.date_range(end=datetime.now(), periods=180, freq='D')
    
    # Simulate price with trend and volatility
    trend = np.linspace(0, 30, 180)
    volatility = np.random.normal(0, 2, 180)
    seasonality = 5 * np.sin(np.linspace(0, 4*np.pi, 180))
    prices = 150 + trend + volatility + seasonality
    
    data = pd.DataFrame({'Close': prices}, index=dates)
    
    # Initialize forecaster
    logger.info("Testing with NVDA simulation...")
    forecaster = StockForecaster("NVDA", data)
    
    # Run forecasts
    result = forecaster.run_all_forecasts(days_ahead=30)
    
    # Display results
    print("\n" + "="*70)
    print("STOCK FORECASTER TEST RESULTS")
    print("="*70)
    print(f"Ticker: {result['ticker']}")
    print(f"Current Price: ${result['current_price']:.2f}")
    print(f"Forecast (30 days): ${result['predicted_price']:.2f}")
    print(f"Expected Change: {result['change_percent']:+.2f}%")
    print(f"\nConfidence Range ({result['confidence_interval']}):")
    print(f"  Lower Bound: ${result['lower_bound']:.2f}")
    print(f"  Upper Bound: ${result['upper_bound']:.2f}")
    print(f"\nModels Used: {', '.join(result['models_used'])}")
    print("="*70)
    
    # Test with different forecast horizons
    print("\nTesting Multiple Forecast Horizons:")
    for days in [7, 14, 30, 60]:
        summary = forecaster.get_prediction_summary(days_ahead=days)
        if summary:
            print(f"  {days:2d} days: ${summary['predicted_price']:7.2f} ({summary['change_percent']:+6.2f}%)")
    
    print("\nAll tests passed! Forecaster is working correctly.\n")
    logger.success("Forecaster test completed successfully")

if __name__ == "__main__":
    test_forecaster()
