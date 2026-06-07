"""
Example: Using Stock Forecaster with Financial Agent
"""

import sys
sys.path.insert(0, 'src')

from agent_groq import FinancialAgentGroq
from forecaster_tool import ForecasterTool
from loguru import logger

def main():
    """Demonstrate stock forecasting integration"""
    
    print("="*70)
    print("STOCK ANALYSIS WITH AI PRICE FORECASTING")
    print("="*70)
    
    # Initialize agent
    agent = FinancialAgentGroq()
    forecaster = ForecasterTool()
    
    # Stocks to analyze
    tickers = ["NVDA", "TSLA", "AAPL"]
    
    for ticker in tickers:
        print(f"\n{'='*70}")
        print(f"Analyzing: {ticker}")
        print('='*70)
        
        # Get stock analysis
        result = agent.analyze_stock(ticker)
        
        if result['status'] != 'success':
            print(f"Error analyzing {ticker}: {result.get('message')}")
            continue
        
        # Add price forecast
        stock_data = result['data']
        stock_data = forecaster.add_forecast_to_analysis(stock_data, days_ahead=30)
        
        # Display results
        print(f"\nCurrent Price: ${stock_data['current_price']:.2f}")
        
        if 'forecast' in stock_data:
            forecast = stock_data['forecast']
            print(f"\n30-Day Forecast:")
            print(f"  Predicted: ${forecast['predicted_price']:.2f}")
            print(f"  Change: {forecast['change_percent']:+.2f}%")
            print(f"  Range: ${forecast['lower_bound']:.2f} - ${forecast['upper_bound']:.2f}")
            print(f"  Confidence: {forecast['confidence_interval']}")
            print(f"  Models: {', '.join(forecast['models_used'])}")
        
        # Agent recommendation
        rec = result['recommendation']
        print(f"\nAgent Recommendation: {rec['action']}")
        print(f"Confidence: {rec['confidence']}")
        print(f"Reasoning: {rec['reasoning'][:200]}...")
    
    print(f"\n{'='*70}")
    print("Analysis Complete!")
    print('='*70)

if __name__ == "__main__":
    main()
