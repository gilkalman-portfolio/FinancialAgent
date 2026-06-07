import pandas as pd
from datetime import datetime
from typing import List, Optional
from dataclasses import dataclass, asdict
from loguru import logger
from tqdm import tqdm
import yfinance as yf

# ייבוא מהמודולים שלך
from src.insider_tracker import InsiderTracker

# משקולות V2.0
WEIGHTS_V2 = {
    'squeeze_factor': 0.30,
    'insider_activity': 0.15,
    'social_velocity': 0.20,
    'volume_confirmation': 0.20,
    'technical_trigger': 0.15
}


@dataclass
class SqueezeScoreV2:
    ticker: str
    explosion_score: float
    insider_activity: float
    insider_purchases_90d: int
    insider_sales_90d: int
    insider_shares_total: float
    perf_since_trade: float = 0.0
    catalyst: str = ""


class MemeSqueezeSentinelV2:
    def __init__(self, watchlist: List[str]):
        self.watchlist = watchlist
        self.insider_tracker = InsiderTracker()

    def scan_all(self):
        results = []
        print(f"Starting Enhanced Scan for {len(self.watchlist)} stocks...")

        for ticker in tqdm(self.watchlist, desc="Analyzing"):
            # כאן היינו קוראים ל-super().calculate_explosion_score(ticker)
            # לצורך הדוגמה, נתמקד באינטגרציה של האינסיידרים:
            ins_data = self.insider_tracker.calculate_conviction_score(ticker)

            # חישוב אחוז שינוי (Performance)
            perf = 0.0
            try:
                current_price = yf.Ticker(ticker).fast_info['lastPrice']
                if ins_data.recent_transactions:
                    last_price = ins_data.recent_transactions[0]['price']
                    perf = ((current_price - last_price) / last_price) * 100
            except:
                pass

            score = SqueezeScoreV2(
                ticker=ticker,
                explosion_score=ins_data.conviction_score,  # כאן תבוא הנוסחה המלאה
                insider_activity=ins_data.conviction_score,
                insider_purchases_90d=ins_data.total_purchases_90d,
                insider_sales_90d=ins_data.total_sales_90d,
                insider_shares_total=sum(t['shares'] for t in ins_data.recent_transactions),
                perf_since_trade=round(perf, 2),
                catalyst="Insider Cluster" if ins_data.clustered_buying else "Normal"
            )
            results.append(score)

        return results


def main():
    watchlist = ['GME', 'NVDA', 'PLTR', 'IONQ', 'AVGO']
    sentinel = MemeSqueezeSentinelV2(watchlist)
    results = sentinel.scan_all()

    # שמירה לאקסל מפורט
    df = pd.DataFrame([asdict(r) for r in results])
    df.to_excel("final_market_report.xlsx", index=False)
    print(f"\n✅ Scan Complete. Results saved to final_market_report.xlsx")


if __name__ == "__main__":
    main()