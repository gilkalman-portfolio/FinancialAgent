"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    MEME-SQUEEZE SENTINEL 2025                                 ║
║                                                                               ║
║  A production-ready Python tool to identify high-probability short-squeeze   ║
║  candidates and meme-stock breakouts in real-time.                           ║
║                                                                               ║
║  Author: Gil (QA Engineer & Quant Developer)                                 ║
║  Date: December 2025                                                         ║
║  Market Context: High-velocity social sentiment + institutional liquidity    ║
╚══════════════════════════════════════════════════════════════════════════════╝

SETUP INSTRUCTIONS:
==================

1. INSTALL DEPENDENCIES:
   ```bash
   pip install praw pytrends schedule rich pandas_ta sqlalchemy
   ```

2. REDDIT API CREDENTIALS (REQUIRED):
   - Go to: https://www.reddit.com/prefs/apps
   - Click "Create App" or "Create Another App"
   - Select "script"
   - Name: "Meme-Squeeze-Sentinel"
   - Redirect URI: http://localhost:8080
   - Copy your credentials to .env file:
   
   REDDIT_CLIENT_ID=your_client_id_here
   REDDIT_CLIENT_SECRET=your_client_secret_here
   REDDIT_USER_AGENT=Meme-Squeeze-Sentinel/1.0 by YourRedditUsername

3. RUN THE SENTINEL:
   ```bash
   python src/meme_squeeze_sentinel.py
   ```

FEATURES:
=========
✅ Real-time short squeeze detection (Short Interest > 18%, Days to Cover)
✅ Social media velocity tracking (Reddit WallStreetBets + ShortSqueeze)
✅ Volume confirmation (RVOL > 2.5x)
✅ Technical triggers (Price > 20 EMA, breaking 5-day high)
✅ Anti-trap filters (Institutional ownership, fake-out detection)
✅ Google Trends integration
✅ SQLite database for historical tracking
✅ Rich CLI dashboard
✅ Scheduled scanning every 60 minutes during market hours
✅ Hybrid watchlist (custom + Russell 2000 scanning)

CORE ALGORITHM:
===============
Explosion Score (0-100):
  - Squeeze Factor (35%): (SI% × Days to Cover) [Threshold: SI > 18%]
  - Social Velocity (25%): Reddit mentions delta (4h vs 24h baseline)
  - Volume Confirmation (25%): RVOL > 2.5x (10-day average)
  - Technical Trigger (15%): Price > 20 EMA + breaking 5-day high

ANTI-TRAP LOGIC (2025):
========================
🚨 Institutional Ownership > 85% → Flag "High Risk of Dump"
🚨 Price ↑ but Volume ↓ → Flag "Divergence/Bull Trap"
🚨 Google Trends spike confirmation required

"""

import os
import sys
import time
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from pathlib import Path

# Data & Analysis
import yfinance as yf
import pandas as pd
import numpy as np
# import pandas_ta as ta  # Commented out due to numba compatibility issues

# Social Media & Trends
import praw
from pytrends.request import TrendReq

# Scheduling & CLI
import schedule
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import box

# Logging
from loguru import logger
from dotenv import load_dotenv

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING SETUP
# ══════════════════════════════════════════════════════════════════════════════

# Configure loguru
logger.remove()  # Remove default handler
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    level="INFO"
)

# Also log to file
log_path = Path(__file__).parent.parent / 'logs' / 'meme_squeeze.log'
log_path.parent.mkdir(parents=True, exist_ok=True)
logger.add(
    log_path,
    rotation="10 MB",
    retention="7 days",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
    level="DEBUG"
)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION & CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

# Load environment variables
load_dotenv()

# Load config (optional - falls back to hardcoded defaults)
def load_config() -> Dict[str, Any]:
    """Load configuration from YAML file if exists"""
    config_path = Path(__file__).parent.parent / 'config' / 'meme_squeeze_config.yaml'
    
    if config_path.exists():
        try:
            import yaml
            with open(config_path, 'r') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.warning(f"Failed to load config: {e}")
    
    return {}

CONFIG = load_config()

# Market hours (EST)
MARKET_OPEN = CONFIG.get('schedule', {}).get('market_open', "09:30")
MARKET_CLOSE = CONFIG.get('schedule', {}).get('market_close', "16:00")

# Scoring weights
WEIGHTS = CONFIG.get('weights', {
    'squeeze_factor': 0.35,
    'social_velocity': 0.25,
    'volume_confirmation': 0.25,
    'technical_trigger': 0.15
})

# Thresholds
THRESHOLDS = CONFIG.get('thresholds', {
    'min_short_interest': 18.0,
    'min_rvol': 2.5,
    'institutional_dump_risk': 85.0,
    'min_explosion_score': 60.0,
})

# Default watchlist (meme stocks + quantum computing) - merged from config
_config_watchlist = []
for category in ['custom', 'quantum', 'space', 'growth']:
    _config_watchlist.extend(CONFIG.get('watchlist', {}).get(category, []))

DEFAULT_WATCHLIST = _config_watchlist if _config_watchlist else [
    'GME', 'AMC', 'BBBY', 'DJT',
    'IONQ', 'RGTI', 'QBTS', 'ARQQ',
    'LUNR', 'RKLB', 'SPCE',
    'PLTR', 'SOFI', 'RIVN', 'LCID'
]

# Reddit subreddits to monitor
SUBREDDITS = CONFIG.get('reddit', {}).get('subreddits', ['wallstreetbets', 'shortsqueeze', 'stocks'])

# Database path
DB_PATH = Path(__file__).parent.parent / 'data' / 'meme_squeeze.db'

# Rich console
console = Console()

# ══════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SqueezeScore:
    """Container for squeeze analysis results"""
    ticker: str
    timestamp: datetime
    explosion_score: float
    
    # Components
    squeeze_factor: float
    social_velocity: float
    volume_confirmation: float
    technical_trigger: float
    
    # Metrics
    short_interest_pct: Optional[float]
    days_to_cover: Optional[float]
    reddit_mentions_4h: int
    reddit_mentions_24h: int
    rvol: float
    price: float
    ema_20: float
    high_5d: float
    
    # Risk flags
    institutional_ownership: Optional[float]
    high_inst_risk: bool
    bull_trap_detected: bool
    google_trend_spike: bool
    
    # Actionable
    catalyst: str
    stop_loss_suggestion: float
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database storage"""
        return asdict(self)


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

class SqueezeDatabase:
    """SQLite database for tracking squeeze scores over time"""
    
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """Initialize database schema"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS squeeze_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                timestamp DATETIME NOT NULL,
                explosion_score REAL NOT NULL,
                squeeze_factor REAL,
                social_velocity REAL,
                volume_confirmation REAL,
                technical_trigger REAL,
                short_interest_pct REAL,
                days_to_cover REAL,
                reddit_mentions_4h INTEGER,
                reddit_mentions_24h INTEGER,
                rvol REAL,
                price REAL,
                ema_20 REAL,
                high_5d REAL,
                institutional_ownership REAL,
                high_inst_risk BOOLEAN,
                bull_trap_detected BOOLEAN,
                google_trend_spike BOOLEAN,
                catalyst TEXT,
                stop_loss_suggestion REAL
            )
        ''')
        
        # Create indices for faster queries
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ticker ON squeeze_scores(ticker)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON squeeze_scores(timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_score ON squeeze_scores(explosion_score)')
        
        conn.commit()
        conn.close()
        
        logger.info(f"Database initialized at {self.db_path}")
    
    def insert_score(self, score: SqueezeScore):
        """Insert a new squeeze score"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        data = score.to_dict()
        data['timestamp'] = data['timestamp'].isoformat()
        
        placeholders = ', '.join(['?' for _ in data])
        columns = ', '.join(data.keys())
        
        cursor.execute(
            f'INSERT INTO squeeze_scores ({columns}) VALUES ({placeholders})',
            tuple(data.values())
        )
        
        conn.commit()
        conn.close()
    
    def get_top_scores(self, limit: int = 10, min_score: float = 60.0) -> List[Dict]:
        """Get top explosion scores from latest scan"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Get latest timestamp
        cursor.execute('SELECT MAX(timestamp) FROM squeeze_scores')
        latest = cursor.fetchone()[0]
        
        if not latest:
            conn.close()
            return []
        
        cursor.execute('''
            SELECT * FROM squeeze_scores 
            WHERE timestamp = ? AND explosion_score >= ?
            ORDER BY explosion_score DESC
            LIMIT ?
        ''', (latest, min_score, limit))
        
        columns = [desc[0] for desc in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        
        conn.close()
        return results
    
    def get_ticker_history(self, ticker: str, days: int = 7) -> pd.DataFrame:
        """Get historical scores for a ticker"""
        conn = sqlite3.connect(self.db_path)
        
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        
        df = pd.read_sql_query('''
            SELECT * FROM squeeze_scores 
            WHERE ticker = ? AND timestamp >= ?
            ORDER BY timestamp DESC
        ''', conn, params=(ticker, cutoff))
        
        conn.close()
        return df


# ══════════════════════════════════════════════════════════════════════════════
# SOCIAL MEDIA MONITOR
# ══════════════════════════════════════════════════════════════════════════════

class RedditMonitor:
    """Monitor Reddit for ticker mentions"""
    
    def __init__(self):
        try:
            self.reddit = praw.Reddit(
                client_id=os.getenv('REDDIT_CLIENT_ID'),
                client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
                user_agent=os.getenv('REDDIT_USER_AGENT', 'Meme-Squeeze-Sentinel/1.0')
            )
            
            # Test connection
            self.reddit.user.me()
            logger.info("✅ Reddit API connected successfully")
            
        except Exception as e:
            logger.error(f"❌ Reddit API connection failed: {e}")
            logger.warning("Social velocity scoring will be disabled")
            self.reddit = None
    
    def get_mentions(self, ticker: str, hours: int = 4) -> int:
        """Count ticker mentions in specified subreddits over last N hours"""
        if not self.reddit:
            return 0
        
        count = 0
        cutoff = datetime.now() - timedelta(hours=hours)
        
        try:
            for subreddit_name in SUBREDDITS:
                subreddit = self.reddit.subreddit(subreddit_name)
                
                # Search new posts
                for submission in subreddit.new(limit=100):
                    post_time = datetime.fromtimestamp(submission.created_utc)
                    
                    if post_time < cutoff:
                        break
                    
                    # Check title and body
                    text = f"{submission.title} {submission.selftext}".upper()
                    if f"${ticker}" in text or f" {ticker} " in text:
                        count += 1
                
                # Small delay to respect rate limits
                time.sleep(0.5)
        
        except Exception as e:
            logger.debug(f"Error fetching mentions for {ticker}: {e}")
        
        return count


class GoogleTrendsMonitor:
    """Monitor Google Trends for search spikes"""
    
    def __init__(self):
        try:
            self.pytrends = TrendReq(hl='en-US', tz=360)
            logger.info("✅ Google Trends initialized")
        except Exception as e:
            logger.error(f"❌ Google Trends initialization failed: {e}")
            self.pytrends = None
    
    def check_spike(self, ticker: str, threshold: int = 50) -> bool:
        """Check if ticker has a Google search spike (interest > threshold)"""
        if not self.pytrends:
            return False
        
        try:
            # Build payload for last 7 days
            self.pytrends.build_payload([ticker], timeframe='now 7-d')
            
            # Get interest over time
            df = self.pytrends.interest_over_time()
            
            if df.empty or ticker not in df.columns:
                return False
            
            # Check if latest interest is above threshold
            latest_interest = df[ticker].iloc[-1]
            return latest_interest >= threshold
        
        except Exception as e:
            logger.debug(f"Google Trends check failed for {ticker}: {e}")
            return False


# ══════════════════════════════════════════════════════════════════════════════
# CORE SCORING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class MemeSqueezeSentinel:
    """Main squeeze detection engine"""
    
    def __init__(self, watchlist: List[str] = None, scan_russell: bool = False):
        self.watchlist = watchlist or DEFAULT_WATCHLIST
        self.scan_russell = scan_russell
        
        # Initialize components
        self.db = SqueezeDatabase(DB_PATH)
        self.reddit = RedditMonitor()
        self.trends = GoogleTrendsMonitor()
        
        logger.info(f"Sentinel initialized with {len(self.watchlist)} tickers")
        logger.info(f"Russell 2000 scanning: {'ENABLED' if scan_russell else 'DISABLED'}")
    
    def _get_scan_list(self) -> List[str]:
        """Get combined watchlist + Russell 2000 (if enabled)"""
        tickers = self.watchlist.copy()
        
        if self.scan_russell:
            try:
                # Import screener to get Russell 2000
                import sys
                from pathlib import Path
                
                # Add project root to path
                project_root = Path(__file__).parent.parent
                if str(project_root) not in sys.path:
                    sys.path.insert(0, str(project_root))
                
                from src.screener import StockScreener
                import yaml
                
                config_path = project_root / 'config' / 'config.yaml'
                with open(config_path, 'r') as f:
                    config = yaml.safe_load(f)
                
                screener = StockScreener(config)
                russell_tickers = screener.get_stock_universe('RUSSELL2000')
                
                # Combine and remove duplicates
                tickers = list(set(tickers + russell_tickers))
                logger.info(f"Scanning {len(tickers)} tickers (watchlist + Russell 2000)")
            
            except Exception as e:
                logger.error(f"Failed to load Russell 2000: {e}")
        
        return tickers
    
    def calculate_explosion_score(self, ticker: str) -> Optional[SqueezeScore]:
        """
        Calculate the Explosion Score (0-100) for a ticker.
        
        Returns SqueezeScore object or None if data unavailable.
        """
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            hist = stock.history(period='3mo')
            
            if hist.empty:
                return None
            
            # ─────────────────────────────────────────────────────────────────
            # 1. SQUEEZE FACTOR (35%)
            # ─────────────────────────────────────────────────────────────────
            short_interest = info.get('shortPercentOfFloat')
            shares_short = info.get('sharesShort', 0)
            avg_volume = info.get('averageVolume10days', info.get('averageVolume', 1))
            
            if short_interest and short_interest > THRESHOLDS['min_short_interest']:
                days_to_cover = shares_short / avg_volume if avg_volume > 0 else 0
                squeeze_raw = (short_interest / 100) * days_to_cover
                squeeze_score = min(squeeze_raw * 10, 100)  # Scale to 0-100
            else:
                squeeze_score = 0
                short_interest = short_interest or 0
                days_to_cover = 0
            
            # ─────────────────────────────────────────────────────────────────
            # 2. SOCIAL VELOCITY (25%)
            # ─────────────────────────────────────────────────────────────────
            mentions_4h = self.reddit.get_mentions(ticker, hours=4)
            mentions_24h = self.reddit.get_mentions(ticker, hours=24)
            
            if mentions_24h > 0:
                velocity_ratio = mentions_4h / (mentions_24h / 6)  # 4h vs 4h baseline
                social_score = min(velocity_ratio * 20, 100)  # Scale to 0-100
            else:
                social_score = 0
            
            # ─────────────────────────────────────────────────────────────────
            # 3. VOLUME CONFIRMATION (25%)
            # ─────────────────────────────────────────────────────────────────
            current_volume = hist['Volume'].iloc[-1]
            avg_volume_10d = hist['Volume'].rolling(10).mean().iloc[-1]
            
            rvol = current_volume / avg_volume_10d if avg_volume_10d > 0 else 0
            
            if rvol > THRESHOLDS['min_rvol']:
                volume_score = min((rvol / THRESHOLDS['min_rvol']) * 50, 100)
            else:
                volume_score = 0
            
            # ─────────────────────────────────────────────────────────────────
            # 4. TECHNICAL TRIGGER (15%)
            # ─────────────────────────────────────────────────────────────────
            current_price = hist['Close'].iloc[-1]
            
            # Calculate 20-day EMA manually (avoiding pandas_ta due to numba issues)
            def calculate_ema(data: pd.Series, period: int) -> pd.Series:
                """Calculate Exponential Moving Average"""
                return data.ewm(span=period, adjust=False).mean()
            
            hist['EMA_20'] = calculate_ema(hist['Close'], 20)
            ema_20 = hist['EMA_20'].iloc[-1]
            
            # 5-day high
            high_5d = hist['High'].tail(5).max()
            
            above_ema = current_price > ema_20
            breaking_high = current_price >= high_5d * 0.99  # Within 1% of 5-day high
            
            technical_score = 0
            if above_ema:
                technical_score += 50
            if breaking_high:
                technical_score += 50
            
            # ─────────────────────────────────────────────────────────────────
            # TOTAL EXPLOSION SCORE
            # ─────────────────────────────────────────────────────────────────
            explosion_score = (
                squeeze_score * WEIGHTS['squeeze_factor'] +
                social_score * WEIGHTS['social_velocity'] +
                volume_score * WEIGHTS['volume_confirmation'] +
                technical_score * WEIGHTS['technical_trigger']
            )
            
            # ─────────────────────────────────────────────────────────────────
            # ANTI-TRAP DETECTION
            # ─────────────────────────────────────────────────────────────────
            inst_ownership = info.get('heldPercentInstitutions')
            high_inst_risk = (inst_ownership or 0) > THRESHOLDS['institutional_dump_risk']
            
            # Bull trap: price up but volume down
            price_change = (hist['Close'].iloc[-1] / hist['Close'].iloc[-2]) - 1
            volume_change = (hist['Volume'].iloc[-1] / hist['Volume'].iloc[-2]) - 1
            bull_trap = price_change > 0 and volume_change < 0
            
            # Google Trends spike
            trend_spike = self.trends.check_spike(ticker)
            
            # ─────────────────────────────────────────────────────────────────
            # CATALYST & STOP-LOSS
            # ─────────────────────────────────────────────────────────────────
            catalyst = self._generate_catalyst(
                short_interest, mentions_4h, rvol, above_ema, trend_spike
            )
            
            # Stop-loss: 8% below current price
            stop_loss = current_price * 0.92
            
            # ─────────────────────────────────────────────────────────────────
            # CREATE RESULT OBJECT
            # ─────────────────────────────────────────────────────────────────
            return SqueezeScore(
                ticker=ticker,
                timestamp=datetime.now(),
                explosion_score=round(explosion_score, 2),
                squeeze_factor=round(squeeze_score, 2),
                social_velocity=round(social_score, 2),
                volume_confirmation=round(volume_score, 2),
                technical_trigger=round(technical_score, 2),
                short_interest_pct=short_interest,
                days_to_cover=round(days_to_cover, 2) if days_to_cover else None,
                reddit_mentions_4h=mentions_4h,
                reddit_mentions_24h=mentions_24h,
                rvol=round(rvol, 2),
                price=round(current_price, 2),
                ema_20=round(ema_20, 2),
                high_5d=round(high_5d, 2),
                institutional_ownership=inst_ownership,
                high_inst_risk=high_inst_risk,
                bull_trap_detected=bull_trap,
                google_trend_spike=trend_spike,
                catalyst=catalyst,
                stop_loss_suggestion=round(stop_loss, 2)
            )
        
        except Exception as e:
            logger.error(f"Error analyzing {ticker}: {e}")
            return None
    
    def _generate_catalyst(
        self,
        short_interest: float,
        mentions: int,
        rvol: float,
        above_ema: bool,
        trend_spike: bool
    ) -> str:
        """Generate human-readable catalyst explanation"""
        catalysts = []
        
        if short_interest and short_interest > 25:
            catalysts.append(f"High SI ({short_interest:.1f}%)")
        
        if mentions > 50:
            catalysts.append(f"Reddit buzz ({mentions} mentions)")
        
        if rvol > 5:
            catalysts.append(f"Volume surge ({rvol:.1f}x)")
        
        if above_ema:
            catalysts.append("Above 20 EMA")
        
        if trend_spike:
            catalysts.append("Google trend spike")
        
        return " | ".join(catalysts) if catalysts else "Multiple factors"
    
    def scan_all(self) -> List[SqueezeScore]:
        """Scan all tickers and return results"""
        tickers = self._get_scan_list()
        results = []
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            task = progress.add_task(f"Scanning {len(tickers)} tickers...", total=len(tickers))
            
            for ticker in tickers:
                score = self.calculate_explosion_score(ticker)
                
                if score and score.explosion_score >= THRESHOLDS['min_explosion_score']:
                    results.append(score)
                    self.db.insert_score(score)
                
                progress.advance(task)
        
        # Sort by explosion score
        results.sort(key=lambda x: x.explosion_score, reverse=True)
        
        return results


# ══════════════════════════════════════════════════════════════════════════════
# CLI DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

def display_results(results: List[SqueezeScore]):
    """Display results in a rich CLI table"""
    if not results:
        console.print("[yellow]No opportunities found above threshold.[/yellow]")
        return
    
    table = Table(
        title="🚀 MEME-SQUEEZE SENTINEL 2025 - Top Opportunities",
        box=box.DOUBLE_EDGE,
        show_header=True,
        header_style="bold magenta"
    )
    
    table.add_column("Ticker", style="cyan", justify="center")
    table.add_column("Score", style="green", justify="center")
    table.add_column("Price", justify="right")
    table.add_column("SI%", justify="right")
    table.add_column("RVOL", justify="right")
    table.add_column("Reddit", justify="center")
    table.add_column("Catalyst", style="yellow")
    table.add_column("Stop Loss", justify="right")
    table.add_column("⚠️ Risks", style="red")
    
    for score in results[:15]:  # Top 15
        risks = []
        if score.high_inst_risk:
            risks.append("INST")
        if score.bull_trap_detected:
            risks.append("TRAP")
        if not score.google_trend_spike:
            risks.append("NO-TREND")
        
        risk_str = " ".join(risks) if risks else "✓"
        
        table.add_row(
            score.ticker,
            f"{score.explosion_score:.1f}",
            f"${score.price:.2f}",
            f"{score.short_interest_pct:.1f}%" if score.short_interest_pct else "N/A",
            f"{score.rvol:.1f}x",
            f"{score.reddit_mentions_4h}",
            score.catalyst[:40] + "..." if len(score.catalyst) > 40 else score.catalyst,
            f"${score.stop_loss_suggestion:.2f}",
            risk_str
        )
    
    console.print(table)
    
    # Summary panel
    avg_score = sum(s.explosion_score for s in results) / len(results)
    
    summary = f"""
[bold]Scan Summary:[/bold]
  • Total Opportunities: {len(results)}
  • Average Score: {avg_score:.1f}
  • Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
  • Database: {DB_PATH}
"""
    
    console.print(Panel(summary, title="Summary", border_style="blue"))


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

def run_scan():
    """Execute a scan and display results"""
    console.print(f"\n[bold cyan]Starting scan at {datetime.now().strftime('%H:%M:%S')}[/bold cyan]\n")
    
    sentinel = MemeSqueezeSentinel(
        watchlist=DEFAULT_WATCHLIST,
        scan_russell=True  # Enable Russell 2000 scanning
    )
    
    results = sentinel.scan_all()
    display_results(results)
    
    console.print(f"\n[bold green]Next scan in 60 minutes[/bold green]\n")


def is_market_hours() -> bool:
    """Check if current time is during market hours (EST)"""
    now = datetime.now()
    
    # Skip weekends
    if now.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False
    
    # Check time (rough check, doesn't account for holidays)
    current_time = now.strftime('%H:%M')
    return MARKET_OPEN <= current_time <= MARKET_CLOSE


def scheduled_runner():
    """Run scan only during market hours"""
    if is_market_hours():
        run_scan()
    else:
        logger.info("Outside market hours - skipping scan")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    """Main entry point"""
    console.print(Panel.fit(
        "[bold cyan]MEME-SQUEEZE SENTINEL 2025[/bold cyan]\n"
        "Continuous monitoring mode - Scans every 60 minutes during market hours",
        border_style="cyan"
    ))
    
    # Validate Reddit credentials
    if not all([
        os.getenv('REDDIT_CLIENT_ID'),
        os.getenv('REDDIT_CLIENT_SECRET'),
        os.getenv('REDDIT_USER_AGENT')
    ]):
        console.print("[red]⚠️  WARNING: Reddit API credentials not found in .env file[/red]")
        console.print("[yellow]Social velocity scoring will be disabled[/yellow]")
        console.print("[yellow]See setup instructions at top of this file[/yellow]\n")
    
    # Run initial scan
    run_scan()
    
    # Schedule subsequent scans
    schedule.every(60).minutes.do(scheduled_runner)
    
    console.print("\n[bold green]Scheduler started - Press Ctrl+C to stop[/bold green]\n")
    
    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        console.print("\n[yellow]Sentinel stopped by user[/yellow]")
        sys.exit(0)


if __name__ == "__main__":
    main()
