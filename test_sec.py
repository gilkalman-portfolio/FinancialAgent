import os
import sys
from dotenv import load_dotenv
from loguru import logger

# Add src to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from src.insider_tracker import InsiderTracker

def test_sec_connection():
    load_dotenv()
    
    email = os.getenv("SEC_USER_AGENT_EMAIL")
    if not email:
        print("❌ ERROR: SEC_USER_AGENT_EMAIL not found in .env")
        return
    
    print(f"🔍 Testing SEC connection with User-Agent: FinancialAgent ({email})")
    
    tracker = InsiderTracker()
    
    # Test ticker map loading
    if not tracker.ticker_map:
        print("❌ FAILED: Could not load SEC ticker map (company_tickers.json)")
        return
    print(f"✅ SEC Ticker Map loaded ({len(tracker.ticker_map)} tickers)")
    
    # Test specific ticker (NVDA is usually active)
    ticker = "NVDA"
    print(f"⏳ Fetching insider data for {ticker}...")
    
    try:
        score = tracker.calculate_conviction_score(ticker)
        
        if score.ticker == ticker:
            print(f"✅ SUCCESS: Data retrieved for {ticker}")
            print(f"   - Conviction Score: {score.conviction_score}")
            print(f"   - Purchases (90d): {score.total_purchases_90d}")
            print(f"   - Sales (90d): {score.total_sales_90d}")
            print(f"   - Net Buying: {score.net_buying_90d}")
            print(f"   - Transactions found: {len(score.recent_transactions)}")
            
            if len(score.recent_transactions) > 0:
                print(f"   - Sample Transaction: {score.recent_transactions[0]['insider']} | {score.recent_transactions[0]['type']} | {score.recent_transactions[0]['shares']} shares")
        else:
            print(f"❌ FAILED: Unexpected result for {ticker}")
            
    except Exception as e:
        print(f"❌ ERROR during execution: {str(e)}")

if __name__ == "__main__":
    test_sec_connection()
