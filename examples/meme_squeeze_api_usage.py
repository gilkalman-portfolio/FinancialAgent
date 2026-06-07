"""
API Usage Examples for Meme-Squeeze Sentinel

This file shows how to integrate the Sentinel into your own projects
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.meme_squeeze_sentinel import (
    MemeSqueezeSentinel,
    SqueezeDatabase,
    RedditMonitor,
    GoogleTrendsMonitor
)
from rich.console import Console

console = Console()


# ══════════════════════════════════════════════════════════════════════════════
# EXAMPLE 1: Analyze Single Ticker
# ══════════════════════════════════════════════════════════════════════════════

def example_single_ticker():
    """Analyze a single ticker and get structured results"""
    
    console.print("\n[bold]Example 1: Single Ticker Analysis[/bold]\n")
    
    sentinel = MemeSqueezeSentinel(watchlist=['GME'], scan_russell=False)
    score = sentinel.calculate_explosion_score('GME')
    
    if score:
        console.print(f"Ticker: {score.ticker}")
        console.print(f"Explosion Score: {score.explosion_score:.1f}/100")
        console.print(f"Short Interest: {score.short_interest_pct:.1f}%")
        console.print(f"RVOL: {score.rvol:.1f}x")
        console.print(f"Catalyst: {score.catalyst}")
        console.print(f"Stop Loss: ${score.stop_loss_suggestion:.2f}\n")
    else:
        console.print("[red]Failed to analyze ticker[/red]\n")


# ══════════════════════════════════════════════════════════════════════════════
# EXAMPLE 2: Batch Analysis
# ══════════════════════════════════════════════════════════════════════════════

def example_batch_analysis():
    """Analyze multiple tickers at once"""
    
    console.print("\n[bold]Example 2: Batch Analysis[/bold]\n")
    
    tickers = ['GME', 'AMC', 'IONQ', 'RGTI', 'DJT']
    sentinel = MemeSqueezeSentinel(watchlist=tickers, scan_russell=False)
    
    results = []
    for ticker in tickers:
        score = sentinel.calculate_explosion_score(ticker)
        if score and score.explosion_score >= 60:
            results.append(score)
    
    # Sort by score
    results.sort(key=lambda x: x.explosion_score, reverse=True)
    
    console.print(f"Found {len(results)} opportunities above 60 score:\n")
    for score in results:
        console.print(f"  {score.ticker}: {score.explosion_score:.1f} - {score.catalyst}")
    
    console.print()


# ══════════════════════════════════════════════════════════════════════════════
# EXAMPLE 3: Database Queries
# ══════════════════════════════════════════════════════════════════════════════

def example_database_queries():
    """Query historical data from database"""
    
    console.print("\n[bold]Example 3: Database Queries[/bold]\n")
    
    db = SqueezeDatabase(project_root / 'data' / 'meme_squeeze.db')
    
    # Get top scores from latest scan
    top_scores = db.get_top_scores(limit=5, min_score=60.0)
    
    if top_scores:
        console.print("Top 5 from latest scan:")
        for row in top_scores:
            console.print(f"  {row['ticker']}: {row['explosion_score']:.1f}")
    else:
        console.print("No historical data yet - run a scan first!")
    
    console.print()


# ══════════════════════════════════════════════════════════════════════════════
# EXAMPLE 4: Custom Filters
# ══════════════════════════════════════════════════════════════════════════════

def example_custom_filters():
    """Filter results based on custom criteria"""
    
    console.print("\n[bold]Example 4: Custom Filters[/bold]\n")
    
    sentinel = MemeSqueezeSentinel(
        watchlist=['GME', 'AMC', 'IONQ', 'RGTI', 'BBBY', 'DJT'],
        scan_russell=False
    )
    
    results = sentinel.scan_all()
    
    # Filter: High squeeze factor + no institutional risk
    filtered = [
        s for s in results 
        if s.squeeze_factor > 50 and not s.high_inst_risk
    ]
    
    console.print(f"High squeeze + low inst. risk: {len(filtered)} tickers")
    for score in filtered[:3]:
        console.print(f"  {score.ticker}: SI={score.short_interest_pct:.1f}%, Score={score.explosion_score:.1f}")
    
    console.print()


# ══════════════════════════════════════════════════════════════════════════════
# EXAMPLE 5: Reddit Monitoring Only
# ══════════════════════════════════════════════════════════════════════════════

def example_reddit_monitor():
    """Use Reddit monitor independently"""
    
    console.print("\n[bold]Example 5: Reddit Monitoring[/bold]\n")
    
    reddit = RedditMonitor()
    
    if reddit.reddit:  # Check if connected
        tickers = ['GME', 'AMC', 'BBBY']
        
        for ticker in tickers:
            mentions_4h = reddit.get_mentions(ticker, hours=4)
            mentions_24h = reddit.get_mentions(ticker, hours=24)
            
            console.print(f"{ticker}:")
            console.print(f"  Last 4 hours: {mentions_4h} mentions")
            console.print(f"  Last 24 hours: {mentions_24h} mentions")
            
            if mentions_24h > 0:
                velocity = mentions_4h / (mentions_24h / 6)
                console.print(f"  Velocity ratio: {velocity:.2f}x\n")
    else:
        console.print("[yellow]Reddit API not configured[/yellow]\n")


# ══════════════════════════════════════════════════════════════════════════════
# EXAMPLE 6: Integration with Trading Bot (Conceptual)
# ══════════════════════════════════════════════════════════════════════════════

def example_trading_integration():
    """
    Conceptual example of integrating with a trading system
    (DO NOT use this in production without proper risk management!)
    """
    
    console.print("\n[bold]Example 6: Trading Integration (Conceptual)[/bold]\n")
    
    sentinel = MemeSqueezeSentinel(watchlist=['GME', 'AMC', 'IONQ'], scan_russell=False)
    
    # Scan for opportunities
    results = sentinel.scan_all()
    
    for score in results:
        # Example decision logic
        if score.explosion_score >= 85 and not score.high_inst_risk:
            console.print(f"🚨 ALERT: {score.ticker} - Explosion Score {score.explosion_score:.1f}")
            console.print(f"   Entry: ${score.price:.2f}")
            console.print(f"   Stop Loss: ${score.stop_loss_suggestion:.2f}")
            console.print(f"   Risk: {((score.price - score.stop_loss_suggestion) / score.price * 100):.1f}%")
            console.print(f"   Catalyst: {score.catalyst}\n")
            
            # Your trading logic here:
            # - Calculate position size based on risk
            # - Submit order via broker API
            # - Set stop-loss order
            # - Log trade for backtesting
    
    console.print()


# ══════════════════════════════════════════════════════════════════════════════
# EXAMPLE 7: Export to CSV
# ══════════════════════════════════════════════════════════════════════════════

def example_export_csv():
    """Export scan results to CSV"""
    
    console.print("\n[bold]Example 7: Export to CSV[/bold]\n")
    
    import pandas as pd
    
    sentinel = MemeSqueezeSentinel(watchlist=['GME', 'AMC', 'IONQ'], scan_russell=False)
    results = sentinel.scan_all()
    
    if results:
        # Convert to DataFrame
        data = [score.to_dict() for score in results]
        df = pd.DataFrame(data)
        
        # Save to CSV
        output_path = project_root / 'data' / 'meme_squeeze_export.csv'
        df.to_csv(output_path, index=False)
        
        console.print(f"Exported {len(results)} results to: {output_path}\n")
    else:
        console.print("[yellow]No results to export[/yellow]\n")


# ══════════════════════════════════════════════════════════════════════════════
# RUN ALL EXAMPLES
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    console.print("\n[bold cyan]═══════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan]    MEME-SQUEEZE SENTINEL - API USAGE EXAMPLES       [/bold cyan]")
    console.print("[bold cyan]═══════════════════════════════════════════════════════[/bold cyan]")
    
    # Run examples
    example_single_ticker()
    example_batch_analysis()
    example_database_queries()
    example_custom_filters()
    example_reddit_monitor()
    example_trading_integration()
    example_export_csv()
    
    console.print("\n[bold green]✅ All examples completed![/bold green]\n")
