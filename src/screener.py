"""
Stock Screener Module
Filters stocks based on various criteria and strategies.
"""

import yfinance as yf
from typing import Dict, Any, List, Optional
from loguru import logger
import pandas as pd


class StockScreener:
    """
    Screens stocks based on predefined or custom criteria.
    """
    
    # Predefined stock universes
    STOCK_UNIVERSES = {
        'SP500': None,  # Will be loaded dynamically
        'NASDAQ100': None,
        'DOW30': None,
        'RUSSELL2000': None,
    }
    
    # Predefined strategies
    STRATEGIES = {
        'small_cap_growth': {
            'min_market_cap': 300_000_000,       # $300M
            'max_market_cap': 400_000_000_000,    # $50B (was $10B - now more options!)
            'min_revenue_growth': 0.10,          # 10%+ (more lenient)
            'min_earnings_growth': 0.10,         # 10%+ (more lenient)  
            'min_price': 3,                      # Above $3 (lower threshold)
            'max_price': 200,                    # Below $200 (was $150)
            'positive_earnings': False,          # Allow negative (growth companies)
            'min_volume': 200_000,               # Lower volume threshold
            'sectors': [
                'Technology', 
                'Healthcare', 
                'Consumer Cyclical',         # yfinance name for Consumer Discretionary!
                'Consumer Defensive',        # yfinance name for Consumer Staples
                'Industrials', 
                'Communication Services', 
                'Financial Services',        # yfinance name!
                'Energy',
                'Basic Materials',           # Added
                'Real Estate',               # Added
                'Utilities'                  # Added
            ]
        },
        'value': {
            'max_pe_ratio': 15,
            'max_pb_ratio': 1.5,
            'min_dividend_yield': 0.02,
            'max_debt_equity': 1.0,
            'min_market_cap': 5_000_000_000,     # $5B+
        },
        'growth': {
            'min_revenue_growth': 0.20,
            'min_earnings_growth': 0.25,
            'min_market_cap': 1_000_000_000,
            'max_pe_ratio': 50,
            'sectors': ['Technology', 'Healthcare', 'Consumer Discretionary']
        },
        'dividend': {
            'min_dividend_yield': 0.03,
            'max_payout_ratio': 0.70,
            'min_market_cap': 10_000_000_000,
            'positive_earnings': True
        },
        'momentum': {
            'min_price_change_52w': 0.20,
            'above_ma_200': True,
            'rsi_range': (40, 70),
            'min_volume': 1_000_000
        },
        'penny_stocks': {
            'min_price': 1,
            'max_price': 10,
            'min_volume': 1_000_000,
            'min_market_cap': 100_000_000,
            'max_market_cap': 10_000_000_000
        }
    }
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the screener.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.screening_config = config.get('screening', {})
    
    def _get_sp500_fallback(self) -> List[str]:
        """
        Fallback list of major S&P 500 stocks (top 100 by market cap).
        Used when Wikipedia scraping fails.
        """
        return [
            # Mega Cap Tech
            "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "ORCL", "ADBE",
            # Large Cap Tech
            "CRM", "CSCO", "INTC", "AMD", "QCOM", "TXN", "INTU", "AMAT", "MU", "ADI",
            # Financials
            "JPM", "V", "MA", "BAC", "WFC", "MS", "GS", "BLK", "C", "SPGI",
            # Healthcare
            "UNH", "JNJ", "LLY", "ABBV", "MRK", "TMO", "ABT", "DHR", "PFE", "BMY",
            # Consumer
            "WMT", "HD", "MCD", "NKE", "SBUX", "LOW", "TGT", "COST", "TJX", "DG",
            # Industrial
            "CAT", "BA", "HON", "UPS", "RTX", "LMT", "GE", "MMM", "DE", "EMR",
            # Energy
            "XOM", "CVX", "COP", "SLB", "EOG", "PXD", "MPC", "VLO", "PSX", "OXY",
            # Communication
            "T", "VZ", "TMUS", "CMCSA", "DIS", "NFLX", "CHTR",
            # Utilities
            "NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "PEG",
            # Materials
            "LIN", "APD", "SHW", "ECL", "DD", "DOW", "NEM", "FCX",
            # Real Estate
            "PLD", "AMT", "CCI", "EQIX", "PSA", "SPG", "DLR",
            # Small/Mid Cap Growth
            "SQ", "SHOP", "ROKU", "DDOG", "NET", "CRWD", "ZS", "OKTA", "SNOW", "DKNG"
        ]
    
    def _get_nasdaq100_fallback(self) -> List[str]:
        """
        Fallback list of NASDAQ 100 stocks.
        Used when Wikipedia scraping fails.
        """
        return [
            # Mega Cap
            "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO",
            # Large Cap Tech
            "ADBE", "CSCO", "INTC", "AMD", "QCOM", "TXN", "INTU", "AMAT", "MU", "ADI",
            "LRCX", "KLAC", "SNPS", "CDNS", "MRVL", "FTNT", "PANW", "WDAY", "TEAM",
            # Software/Cloud
            "ORCL", "CRM", "ADSK", "ANSS", "DDOG", "ZS", "OKTA", "CRWD", "NET", "SNOW",
            # E-commerce/Consumer
            "NFLX", "PYPL", "ABNB", "BKNG", "DASH", "UBER", "LYFT", "MELI",
            # Biotech
            "AMGN", "GILD", "REGN", "VRTX", "BIIB", "ILMN", "MRNA", "ALXN",
            # Retail/Consumer
            "COST", "SBUX", "MNST", "PEP", "KDP", "MDLZ",
            # Communication
            "CMCSA", "CHTR", "T", "TMUS",
            # Other
            "HON", "ADP", "PAYX", "CPRT", "CTAS", "FAST", "VRSK", "IDXX", "EXR", "VRSN"
        ]
        
    def get_stock_universe(self, universe: str = 'SP500') -> List[str]:
        """
        Get list of tickers from a stock universe.
        
        Args:
            universe: Universe name (SP500, NASDAQ100, etc.)
            
        Returns:
            List of ticker symbols
        """
        logger.info(f"Loading stock universe: {universe}")
        
        if universe == 'SP500':
            # Get S&P 500 tickers from Wikipedia
            try:
                import pandas as pd
                import requests
                from io import StringIO
                
                url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
                
                # Fetch with proper headers
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                }
                response = requests.get(url, headers=headers)
                response.raise_for_status()
                
                # Parse HTML tables
                tables = pd.read_html(StringIO(response.text))
                df = tables[0]
                tickers = df['Symbol'].tolist()
                # Clean tickers (some have dots)
                tickers = [t.replace('.', '-') for t in tickers if isinstance(t, str)]
                logger.info(f"Loaded {len(tickers)} tickers from S&P 500")
                return tickers
            except Exception as e:
                logger.error(f"Error loading S&P 500: {str(e)}")
                logger.info("Using fallback S&P 500 list...")
                # Fallback to a predefined list of major S&P 500 stocks
                return self._get_sp500_fallback()
        
        elif universe == 'NASDAQ100':
            try:
                import pandas as pd
                import requests
                
                url = 'https://en.wikipedia.org/wiki/Nasdaq-100'
                # Add headers to avoid 403 Forbidden
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
                tables = pd.read_html(url, storage_options={'headers': headers})
                df = tables[4]  # The main table
                tickers = df['Ticker'].tolist()
                logger.info(f"Loaded {len(tickers)} tickers from NASDAQ 100")
                return tickers
            except Exception as e:
                logger.error(f"Error loading NASDAQ 100: {str(e)}")
                logger.info("Using fallback NASDAQ 100 list...")
                return self._get_nasdaq100_fallback()
        
        elif universe == 'DOW30':
            # Dow Jones 30 tickers
            tickers = [
                'AAPL', 'MSFT', 'JPM', 'V', 'UNH', 'JNJ', 'WMT', 'PG', 
                'MA', 'HD', 'CVX', 'MRK', 'KO', 'PEP', 'CSCO', 'DIS',
                'INTC', 'VZ', 'IBM', 'BA', 'NKE', 'MCD', 'CAT', 'TRV',
                'AXP', 'GS', 'MMM', 'HON', 'AMGN', 'CRM'
            ]
            logger.info(f"Loaded {len(tickers)} tickers from Dow 30")
            return tickers
        
        elif universe == 'RUSSELL2000':
            # Russell 2000 - Small cap stocks
            logger.info("Loading Russell 2000...")
            
            try:
                # Method 1: Try FinViz screener (most reliable, free, no API key needed)
                import requests
                import pandas as pd
                from bs4 import BeautifulSoup
                
                logger.info("Fetching from FinViz screener...")
                
                all_tickers = []
                
                # FinViz filter: Market Cap = Small ($300M to $2B) + Mid ($2B to $10B)
                # This covers most Russell 2000 stocks
                base_url = "https://finviz.com/screener.ashx"
                
                # We'll paginate through results
                for page in range(1, 30):  # Up to 30 pages (20 stocks per page = 600 stocks)
                    params = {
                        'v': '111',  # Overview view
                        'f': 'cap_smallover,geo_usa',  # Small cap + USA
                        'r': str((page - 1) * 20 + 1)  # Start position
                    }
                    
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    }
                    
                    try:
                        response = requests.get(base_url, params=params, headers=headers, timeout=10)
                        response.raise_for_status()
                        
                        # Parse HTML
                        soup = BeautifulSoup(response.text, 'html.parser')
                        
                        # Find ticker symbols in the table
                        ticker_links = soup.find_all('a', {'class': 'tab-link'})
                        
                        if not ticker_links:
                            # No more results
                            break
                        
                        page_tickers = [link.text.strip() for link in ticker_links if link.text.strip()]
                        
                        if not page_tickers:
                            break
                        
                        all_tickers.extend(page_tickers)
                        
                        logger.info(f"  Page {page}: Found {len(page_tickers)} tickers (Total: {len(all_tickers)})")
                        
                        # Stop if we got fewer than 20 (last page)
                        if len(page_tickers) < 20:
                            break
                        
                        # Small delay to be nice to FinViz
                        import time
                        time.sleep(0.5)
                        
                    except Exception as page_error:
                        logger.debug(f"Error on page {page}: {str(page_error)}")
                        break
                
                # Remove duplicates
                all_tickers = list(dict.fromkeys(all_tickers))
                
                if len(all_tickers) > 100:
                    logger.info(f"✅ Loaded {len(all_tickers)} small cap stocks from FinViz")
                    return all_tickers
                else:
                    raise Exception(f"Only found {len(all_tickers)} tickers from FinViz")
                
            except Exception as e:
                logger.warning(f"FinViz method failed: {str(e)}")
                
                # Method 2: Try iShares IWM ETF
                try:
                    logger.info("Trying iShares IWM ETF...")
                    
                    url = 'https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund'
                    
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    }
                    
                    response = requests.get(url, headers=headers, timeout=10)
                    response.raise_for_status()
                    
                    # Parse CSV (skip first 10 rows which are metadata)
                    from io import StringIO
                    df = pd.read_csv(StringIO(response.text), skiprows=10)
                    
                    # Get tickers from 'Ticker' column
                    if 'Ticker' in df.columns:
                        tickers = df['Ticker'].dropna().tolist()
                        # Clean tickers
                        tickers = [str(t).strip().upper() for t in tickers if str(t).strip() and str(t).strip() != '-']
                        # Remove duplicates
                        tickers = list(dict.fromkeys(tickers))
                        
                        if len(tickers) > 100:
                            logger.info(f"✅ Loaded {len(tickers)} tickers from Russell 2000 (IWM ETF)")
                            return tickers
                    
                    raise Exception("Could not parse IWM holdings")
                    
                except Exception as e2:
                    logger.warning(f"iShares method failed: {str(e2)}")
                    logger.info("Using curated Russell 2000 small cap list...")
                    
                    # Method 3: Enhanced fallback list (200+ stocks)
                    tickers = [
                        # Small Cap Tech (50 stocks)
                        "APPN", "ZI", "BILL", "GTLB", "FROG", "ALTR", "BRZE", "NCNO", "S", "PINS",
                        "SNAP", "TWLO", "DDOG", "NET", "CRWD", "ZS", "OKTA", "SNOW", "MDB", "ESTC",
                        "CFLT", "GTLB", "PD", "COUP", "DOCN", "FSLY", "LIXT", "RPD", "MGNI", "TTD",
                        "PUBM", "APPS", "BIGC", "VTEX", "BLZE", "IONQ", "RGTI", "QBTS", "ARQQ", "QUBT",
                        "LAZR", "VLDR", "OUST", "INVZ", "LSCC", "MPWR", "WOLF", "POWI", "ONTO", "UCTT",
                        
                        # Small Cap Healthcare/Biotech (50 stocks)
                        "ARVN", "LEGN", "PRTA", "AGIO", "SDGR", "IMVT", "YMAB", "KRYS", "FATE", "EDIT",
                        "NTLA", "CRSP", "BEAM", "VERV", "BLUE", "IONS", "SRPT", "BMRN", "RARE", "UTHR",
                        "FOLD", "DVAX", "HALO", "TGTX", "CGEM", "KYMR", "CGON", "SWTX", "ALLO", "PRVB",
                        "AUTL", "ZNTL", "IKNA", "CDMO", "CLDX", "ATNF", "APLS", "IMMP", "BMEA", "RCKT",
                        "SANA", "TSVT", "GRND", "LYEL", "KPTI", "SPRY", "NRIX", "ALEC", "BDTX", "GTHX",
                        
                        # Small Cap Consumer (30 stocks)
                        "YETI", "PLAY", "TXRH", "WING", "SHAK", "CAKE", "BLMN", "RRGB", "BJRI", "DNUT",
                        "PZZA", "JACK", "WEN", "BROS", "CAVA", "PLNT", "EAT", "FWRG", "DIN", "RUTH",
                        "DENN", "HAYW", "RICK", "CNNE", "TAST", "PBPB", "KRUS", "FOGO", "DINE", "GOOD",
                        
                        # Small Cap Industrial (30 stocks)
                        "GTLS", "ATKR", "MLI", "USLM", "PRIM", "ROAD", "HEES", "TTEK", "UFPI", "AZEK",
                        "BECN", "TREX", "UFPI", "FELE", "AAON", "NXT", "BLD", "IBP", "HLIO", "ACHR",
                        "JOBY", "LILM", "EVTL", "BLDE", "EH", "AYRO", "WKHS", "RIDE", "GOEV", "ARVL",
                        
                        # Small Cap Financial (20 stocks)
                        "CACC", "OCFC", "TOWN", "FFWM", "NWBI", "CBNK", "TCBI", "HTLF", "PFBC", "WSFS",
                        "ESSA", "FULT", "FIBK", "CASH", "CADE", "CBU", "HOPE", "HOMB", "UCBI", "WAFD",
                        
                        # Small Cap Energy (20 stocks)
                        "REI", "PTEN", "LBRT", "HP", "NINE", "WHD", "TALO", "WTTR", "VTLE", "CIVI",
                        "SM", "MGY", "MTDR", "PR", "AROC", "CRC", "RRC", "CNX", "CTRA", "PBF"
                    ]
                    
                    # Remove duplicates while preserving order
                    tickers = list(dict.fromkeys(tickers))
                    
                    logger.info(f"Using {len(tickers)} curated Russell 2000 tickers")
                    logger.warning("⚠️  This is a sample of ~200 stocks, not the full Russell 2000 index")
                    logger.warning("⚠️  For complete Russell 2000 data, consider using a paid data provider")
                    
                    return tickers
        
        elif universe == 'CUSTOM':
            # Get from config
            tickers = self.screening_config.get('custom_tickers', [])
            logger.info(f"Loaded {len(tickers)} custom tickers")
            return tickers
        
        else:
            logger.warning(f"Unknown universe: {universe}")
            return []
    
    def get_multiple_universes(self, universes: List[str]) -> List[str]:
        """
        Get combined list of tickers from multiple stock universes.
        Removes duplicates and returns unique tickers.
        
        Args:
            universes: List of universe names (e.g., ['SP500', 'NASDAQ100'])
            
        Returns:
            Combined list of unique ticker symbols
        """
        all_tickers = []
        
        for universe in universes:
            logger.info(f"Loading universe: {universe}")
            tickers = self.get_stock_universe(universe)
            all_tickers.extend(tickers)
        
        # Remove duplicates while preserving order
        unique_tickers = list(dict.fromkeys(all_tickers))
        
        logger.info(f"Combined {len(universes)} universes: {len(all_tickers)} total tickers, {len(unique_tickers)} unique")
        
        return unique_tickers
    
    def screen(
        self, 
        strategy: Optional[str] = None,
        custom_filters: Optional[Dict[str, Any]] = None,
        tickers: Optional[List[str]] = None,
        max_price_per_share: Optional[float] = None
    ) -> List[str]:
        """
        Screen stocks based on strategy or custom filters.
        
        Args:
            strategy: Strategy name (small_cap_growth, value, etc.)
            custom_filters: Custom filter dictionary
            tickers: List of tickers to screen (if None, uses universe from config)
            max_price_per_share: Override max price per share from config
            
        Returns:
            List of filtered ticker symbols
        """
        # Determine which filters to use
        if custom_filters:
            filters = custom_filters
            logger.info("Using custom filters")
        elif strategy and strategy in self.STRATEGIES:
            filters = self.STRATEGIES[strategy].copy()
            logger.info(f"Using '{strategy}' strategy")
        else:
            # Use strategy from config
            strategy = self.screening_config.get('strategy', 'small_cap_growth')
            filters = self.STRATEGIES.get(strategy, self.STRATEGIES['small_cap_growth']).copy()
            logger.info(f"Using '{strategy}' strategy from config")
        
        # Apply max_price_per_share from config or parameter
        if max_price_per_share is not None:
            filters['max_price'] = max_price_per_share
            logger.info(f"Overriding max price per share: ${max_price_per_share}")
        elif 'max_price_per_share' in self.screening_config:
            filters['max_price'] = self.screening_config['max_price_per_share']
            logger.info(f"Using max price per share from config: ${filters['max_price']}")
        
        # Get tickers to screen
        if tickers is None:
            # Check if multiple universes are specified in config
            universes_config = self.screening_config.get('stock_universes', None)
            
            if universes_config and isinstance(universes_config, list):
                # Multiple universes specified
                logger.info(f"Using multiple universes: {universes_config}")
                tickers = self.get_multiple_universes(universes_config)
            else:
                # Single universe (backward compatibility)
                universe = self.screening_config.get('stock_universe', 'SP500')
                tickers = self.get_stock_universe(universe)
        
        if not tickers:
            logger.error("No tickers to screen")
            return []
        
        logger.info(f"Screening {len(tickers)} stocks with {len(filters)} filters")
        logger.info(f"Price range: ${filters.get('min_price', 0)} - ${filters.get('max_price', 'unlimited')}")
        logger.info(f"Market cap range: ${filters.get('min_market_cap', 0)/1e9:.1f}B - ${filters.get('max_market_cap', 999)/1e9:.1f}B")
        logger.info(f"Filters being used: {filters}")  # DEBUG: Print all filters
        
        # Screen stocks
        filtered_tickers = []
        
        for i, ticker in enumerate(tickers):
            if i % 50 == 0:
                logger.info(f"Progress: {i}/{len(tickers)}")
            
            try:
                if self._passes_filters(ticker, filters):
                    filtered_tickers.append(ticker)
            except Exception as e:
                logger.debug(f"Error screening {ticker}: {str(e)}")
                continue
        
        logger.info(f"Screening complete: {len(filtered_tickers)}/{len(tickers)} stocks passed filters")
        return filtered_tickers
    
    def _passes_filters(self, ticker: str, filters: Dict[str, Any]) -> bool:
        """
        Check if a stock passes all filters.
        
        Args:
            ticker: Stock ticker
            filters: Filter dictionary
            
        Returns:
            True if passes all filters
        """
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            
            # Price filters
            if 'min_price' in filters:
                price = info.get('currentPrice', info.get('regularMarketPrice', 0))
                if price < filters['min_price']:
                    logger.debug(f"{ticker}: Price ${price:.2f} < min ${filters['min_price']}")
                    return False
            
            if 'max_price' in filters:
                price = info.get('currentPrice', info.get('regularMarketPrice', 999999))
                if price > filters['max_price']:
                    logger.debug(f"{ticker}: Price ${price:.2f} > max ${filters['max_price']}")
                    return False
            
            # Market cap filters
            if 'min_market_cap' in filters:
                market_cap = info.get('marketCap', 0)
                if market_cap < filters['min_market_cap']:
                    logger.debug(f"{ticker}: Market cap ${market_cap/1e9:.2f}B < min ${filters['min_market_cap']/1e9:.2f}B")
                    return False
            
            if 'max_market_cap' in filters:
                market_cap = info.get('marketCap', float('inf'))
                if market_cap > filters['max_market_cap']:
                    logger.debug(f"{ticker}: Market cap ${market_cap/1e9:.2f}B > max ${filters['max_market_cap']/1e9:.2f}B")
                    return False
            
            # Growth filters (optional - allow missing data)
            if 'min_revenue_growth' in filters:
                growth = info.get('revenueGrowth')
                # Only filter if we have data AND it's below threshold
                if growth is not None and growth < filters['min_revenue_growth']:

                    return False
            
            if 'min_earnings_growth' in filters:
                growth = info.get('earningsGrowth')
                # Only filter if we have data AND it's below threshold
                if growth is not None and growth < filters['min_earnings_growth']:
                    return False
            
            # Value filters (optional - allow missing data)
            if 'max_pe_ratio' in filters:
                pe = info.get('trailingPE')
                # Only filter if we have PE AND it's too high
                if pe is not None and pe > filters['max_pe_ratio']:
                    return False
            
            if 'max_pb_ratio' in filters:
                pb = info.get('priceToBook')
                # Only filter if we have P/B AND it's too high
                if pb is not None and pb > filters['max_pb_ratio']:
                    return False
            
            if 'max_debt_equity' in filters:
                de = info.get('debtToEquity')
                if de is not None and de > filters['max_debt_equity']:
                    return False
            
            # Dividend filters
            if 'min_dividend_yield' in filters:
                div_yield = info.get('dividendYield', 0)
                if div_yield < filters['min_dividend_yield']:
                    return False
            
            # Volume filters
            if 'min_volume' in filters:
                volume = info.get('averageVolume', 0)
                if volume < filters['min_volume']:
                    return False
            
            # Sector filter
            if 'sectors' in filters:
                sector = info.get('sector', '')
                if sector not in filters['sectors']:
                    logger.debug(f"{ticker}: Sector '{sector}' not in allowed list")
                    return False
            
            # Positive earnings filter
            if filters.get('positive_earnings', False):
                eps = info.get('trailingEps', 0)
                if eps <= 0:
                    logger.debug(f"{ticker}: Negative earnings ${eps:.2f}")
                    return False
            
            # If we get here, stock passed all filters
            logger.debug(f"{ticker}: ✅ PASSED all filters")
            return True
            
        except Exception as e:
            logger.debug(f"Error checking filters for {ticker}: {str(e)}")
            return False
    
    def get_top_stocks(
        self, 
        tickers: List[str], 
        sort_by: str = 'market_cap',
        limit: int = 50,
        ascending: bool = False
    ) -> List[str]:
        """
        Get top N stocks from a list, sorted by a metric.
        
        Args:
            tickers: List of tickers
            sort_by: Metric to sort by (market_cap, volume, revenue_growth, etc.)
            limit: Number of stocks to return
            ascending: Sort order
            
        Returns:
            Sorted list of tickers
        """
        logger.info(f"Sorting {len(tickers)} stocks by {sort_by}")
        
        stocks_data = []
        for ticker in tickers:
            try:
                stock = yf.Ticker(ticker)
                info = stock.info
                
                value = None
                if sort_by == 'market_cap':
                    value = info.get('marketCap', 0)
                elif sort_by == 'volume':
                    value = info.get('averageVolume', 0)
                elif sort_by == 'revenue_growth':
                    value = info.get('revenueGrowth', 0)
                elif sort_by == 'earnings_growth':
                    value = info.get('earningsGrowth', 0)
                
                if value is not None:
                    stocks_data.append({'ticker': ticker, 'value': value})
            except:
                continue
        
        # Sort
        stocks_data.sort(key=lambda x: x['value'], reverse=not ascending)
        
        # Return top N tickers
        top_tickers = [s['ticker'] for s in stocks_data[:limit]]
        logger.info(f"Returning top {len(top_tickers)} stocks")
        
        return top_tickers


if __name__ == "__main__":
    # Test the screener
    import yaml
    
    # Load config
    with open('../config/config.yaml', 'r') as f:
        config = yaml.safe_load(f)
    
    screener = StockScreener(config)
    
    # Test small cap growth strategy
    print("\n" + "="*60)
    print("Testing Small Cap Growth Strategy")
    print("="*60)
    
    # Use a small sample for testing
    test_tickers = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'AMD', 'SQ', 'SHOP', 'ROKU', 'ZM']
    
    filtered = screener.screen(
        strategy='small_cap_growth',
        tickers=test_tickers
    )
    
    print(f"\nFiltered stocks: {filtered}")
