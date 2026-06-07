"""
Insider Tracker — Form 4 conviction scoring
Primary: sec-api.io (fast, single call)
Fallback: SEC EDGAR XML scraper (original, slow)
"""

import os
import requests
from lxml import etree
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

_email = os.getenv("SEC_USER_AGENT_EMAIL", "")
if not _email:
    logger.warning("SEC_USER_AGENT_EMAIL not set in .env — SEC API may return 403.")
HEADERS = {'User-Agent': f'FinancialAgent ({_email})'}

_SEC_API_AVAILABLE = bool(os.getenv("SEC_API_KEY", ""))


@dataclass
class InsiderScore:
    ticker: str
    conviction_score: float
    total_purchases_90d: int
    total_sales_90d: int
    net_buying_90d: int
    clustered_buying: bool
    large_purchases: int
    total_value_bought: float
    recent_transactions: list


class InsiderTracker:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._ticker_map = None  # lazy load — only needed for fallback
        if _SEC_API_AVAILABLE:
            logger.info("InsiderTracker: using sec-api.io (fast mode)")
        else:
            logger.info("InsiderTracker: sec-api.io unavailable, using EDGAR XML (slow mode)")

    # ── Public ────────────────────────────────────────────────────────────────

    def calculate_conviction_score(self, ticker: str) -> InsiderScore:
        """Score 0-100 based on Form 4 transactions in the last 90 days."""
        if _SEC_API_AVAILABLE:
            return self._score_via_sec_api(ticker)
        return self._score_via_edgar_xml(ticker)

    # ── sec-api.io path (fast) ────────────────────────────────────────────────

    def _score_via_sec_api(self, ticker: str) -> InsiderScore:
        from src.sec_api_client import get_insider_transactions
        try:
            txns = get_insider_transactions(ticker, days=90)
            return self._build_score(ticker, txns)
        except Exception as e:
            logger.debug(f"InsiderTracker sec-api failed for {ticker}: {e}, falling back to EDGAR")
            return self._score_via_edgar_xml(ticker)

    # ── EDGAR XML path (original fallback) ───────────────────────────────────

    def _score_via_edgar_xml(self, ticker: str) -> InsiderScore:
        if self._ticker_map is None:
            self._ticker_map = self._load_ticker_map()

        cik = self._ticker_map.get(ticker.upper())
        if not cik:
            return InsiderScore(ticker, 0, 0, 0, 0, False, 0, 0, [])

        cutoff = datetime.now() - timedelta(days=90)
        try:
            url      = f"https://data.sec.gov/submissions/CIK{cik}.json"
            response = self.session.get(url, timeout=15)
            filings  = response.json()['filings']['recent']

            transactions = []
            for i in range(len(filings['form'])):
                if filings['form'][i] != '4':
                    continue
                try:
                    filing_date = datetime.strptime(filings['filingDate'][i], '%Y-%m-%d')
                except Exception:
                    continue
                if filing_date < cutoff:
                    break

                acc_num  = filings['accessionNumber'][i].replace('-', '')
                doc_name = filings['primaryDocument'][i]
                xml_url  = self._fix_sec_url(cik, acc_num, doc_name)

                try:
                    resp = self.session.get(xml_url, timeout=15)
                    root = etree.fromstring(resp.content)
                except Exception as e:
                    logger.debug(f"XML parse error for {ticker} filing {i}: {e}")
                    time.sleep(0.1)
                    continue

                owner_nodes = root.xpath("//rptOwnerName/text()")
                owner = owner_nodes[0] if owner_nodes else "Unknown"

                for tx in root.xpath("//nonDerivativeTransaction"):
                    try:
                        code_nodes   = tx.xpath(".//transactionCode/text()")
                        shares_nodes = tx.xpath(".//transactionShares/value/text()")
                        price_nodes  = tx.xpath(".//transactionPricePerShare/value/text()")
                        if not code_nodes or not shares_nodes or not price_nodes:
                            continue
                        code   = code_nodes[0]
                        shares = float(shares_nodes[0])
                        price  = float(price_nodes[0])
                        if code not in ("P", "S"):
                            continue
                        transactions.append({
                            "date":    filings['filingDate'][i],
                            "insider": owner,
                            "role":    "",
                            "type":    "BUY" if code == "P" else "SELL",
                            "shares":  shares,
                            "price":   price,
                            "value":   shares * price,
                        })
                    except Exception:
                        continue

                time.sleep(0.12)

            return self._build_score(ticker, transactions)

        except Exception as e:
            logger.debug(f"InsiderTracker EDGAR failed for {ticker}: {e}")
            return InsiderScore(ticker, 0, 0, 0, 0, False, 0, 0, [])

    # ── Shared scoring logic ──────────────────────────────────────────────────

    @staticmethod
    def _build_score(ticker: str, transactions: list) -> InsiderScore:
        purchases, sales, large_buys, total_val = 0, 0, 0, 0.0
        unique_insiders = set()

        for tx in transactions:
            if tx["type"] == "BUY":
                purchases += 1
                total_val += tx["value"]
                unique_insiders.add(tx["insider"])
                if tx["value"] > 100_000:
                    large_buys += 1
            else:
                sales += 1

        buy_ratio = purchases / (purchases + sales + 1)
        return InsiderScore(
            ticker=ticker,
            conviction_score=round(buy_ratio * 100, 2),
            total_purchases_90d=purchases,
            total_sales_90d=sales,
            net_buying_90d=purchases - sales,
            clustered_buying=len(unique_insiders) >= 3,
            large_purchases=large_buys,
            total_value_bought=total_val,
            recent_transactions=transactions,
        )

    # ── EDGAR helpers ─────────────────────────────────────────────────────────

    def _load_ticker_map(self) -> dict:
        try:
            url  = "https://www.sec.gov/files/company_tickers.json"
            data = self.session.get(url).json()
            return {v['ticker']: str(v['cik_str']).zfill(10) for k, v in data.items()}
        except Exception as e:
            logger.error(f"Failed to load SEC ticker map: {e}")
            return {}

    @staticmethod
    def _fix_sec_url(cik, acc_num, doc_name) -> str:
        clean_doc = doc_name.split('/')[-1]
        return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_num}/{clean_doc}"
