"""
Automated Scanner - Runs scheduled scans and sends reports

Features:
- Scheduled scanning (morning/afternoon)
- Email reports (HTML)
- Alert system (high scores)
- Database logging
- Error handling
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.meme_squeeze_sentinel_v2 import MemeSqueezeSentinelV2
from src.database import init_db, save_scan_run, save_result, save_alert
from src.telegram_notifier import TelegramNotifier
from datetime import datetime, time
import schedule
import time as time_module
from loguru import logger
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
import os

init_db()

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

SCHEDULE_CONFIG = {
    'pre_market': '08:30',      # Pre-market analysis
    'market_open': '09:30',     # Market open scan
    'midday': '12:00',          # Midday update
    'pre_close': '15:30',       # Pre-close analysis
    'post_close': '16:30'       # Daily summary
}

EMAIL_CONFIG = {
    'smtp_server': os.getenv('SMTP_SERVER', 'smtp.gmail.com'),
    'smtp_port': int(os.getenv('SMTP_PORT', '587')),
    'sender_email': os.getenv('SENDER_EMAIL', ''),
    'sender_password': os.getenv('SENDER_PASSWORD', ''),
    'recipient_email': os.getenv('RECIPIENT_EMAIL', ''),
    'enabled': os.getenv('EMAIL_ENABLED', 'false').lower() == 'true'
}

ALERT_THRESHOLDS = {
    'critical': 85,  # Send immediate alert
    'high': 70,      # Include in daily report
    'moderate': 60   # Track only
}

# ══════════════════════════════════════════════════════════════════════════════
# EMAIL TEMPLATES
# ══════════════════════════════════════════════════════════════════════════════

def generate_html_report(results, scan_time, scan_type='Daily'):
    """Generate HTML email report"""
    
    # Filter by threshold
    critical = [r for r in results if r.explosion_score >= ALERT_THRESHOLDS['critical']]
    high = [r for r in results if ALERT_THRESHOLDS['high'] <= r.explosion_score < ALERT_THRESHOLDS['critical']]
    moderate = [r for r in results if ALERT_THRESHOLDS['moderate'] <= r.explosion_score < ALERT_THRESHOLDS['high']]
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px; }}
            .container {{ max-width: 800px; margin: 0 auto; background-color: white; padding: 30px; border-radius: 10px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
            .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 5px; text-align: center; }}
            .section {{ margin: 20px 0; }}
            .section-title {{ font-size: 18px; font-weight: bold; color: #333; border-bottom: 2px solid #667eea; padding-bottom: 5px; margin-bottom: 15px; }}
            .ticker-card {{ background-color: #f9f9f9; padding: 15px; margin: 10px 0; border-radius: 5px; border-left: 4px solid #667eea; }}
            .score {{ font-size: 24px; font-weight: bold; color: #667eea; }}
            .critical {{ border-left-color: #e74c3c; }}
            .high {{ border-left-color: #f39c12; }}
            .moderate {{ border-left-color: #3498db; }}
            .metric {{ display: inline-block; margin-right: 20px; }}
            .metric-label {{ font-size: 12px; color: #777; }}
            .metric-value {{ font-size: 16px; font-weight: bold; color: #333; }}
            .footer {{ text-align: center; color: #777; font-size: 12px; margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd; }}
            .alert {{ background-color: #fff3cd; border: 1px solid #ffc107; padding: 10px; border-radius: 5px; margin: 10px 0; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🚀 Meme-Squeeze Alert</h1>
                <p>{scan_type} Analysis - {scan_time}</p>
            </div>
    """
    
    # Critical alerts
    if critical:
        html += """
            <div class="section">
                <div class="section-title">🔥 CRITICAL ALERTS (85+)</div>
        """
        for r in critical:
            html += f"""
                <div class="ticker-card critical">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <div>
                            <h3 style="margin: 0;">{r.ticker}</h3>
                            <span class="score">{r.explosion_score:.1f}/100</span>
                        </div>
                        <div style="text-align: right;">
                            <div class="metric-value">${r.price:.2f}</div>
                            <div class="metric-label">Price</div>
                        </div>
                    </div>
                    <div style="margin-top: 15px;">
                        <div class="metric">
                            <div class="metric-label">Short Interest</div>
                            <div class="metric-value">{r.short_interest_pct:.1f}%</div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">Insider</div>
                            <div class="metric-value">{r.insider_activity:.0f}/100</div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">RVOL</div>
                            <div class="metric-value">{r.rvol:.1f}x</div>
                        </div>
                    </div>
                    <div style="margin-top: 10px;">
                        <strong>Catalyst:</strong> {r.catalyst}
                    </div>
                </div>
            """
        html += "</div>"
    
    # High conviction
    if high:
        html += """
            <div class="section">
                <div class="section-title">⚡ HIGH CONVICTION (70-84)</div>
        """
        for r in high:
            html += f"""
                <div class="ticker-card high">
                    <strong>{r.ticker}</strong> - {r.explosion_score:.1f}/100 
                    (${r.price:.2f}, SI: {r.short_interest_pct:.1f}%)
                    <br><small>{r.catalyst}</small>
                </div>
            """
        html += "</div>"
    
    # Summary
    html += f"""
            <div class="section">
                <div class="section-title">📊 SUMMARY</div>
                <div class="metric">
                    <div class="metric-label">Total Scanned</div>
                    <div class="metric-value">{len(results)}</div>
                </div>
                <div class="metric">
                    <div class="metric-label">Critical (85+)</div>
                    <div class="metric-value">{len(critical)}</div>
                </div>
                <div class="metric">
                    <div class="metric-label">High (70-84)</div>
                    <div class="metric-value">{len(high)}</div>
                </div>
                <div class="metric">
                    <div class="metric-label">Moderate (60-69)</div>
                    <div class="metric-value">{len(moderate)}</div>
                </div>
            </div>
            
            <div class="footer">
                <p>⚠️ This is not financial advice. Always do your own research.</p>
                <p>Meme-Squeeze Sentinel V2.0 with Insider Tracking</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    return html


def send_email_report(subject, html_content):
    """Send email report"""
    
    if not EMAIL_CONFIG['enabled']:
        logger.info("Email disabled - skipping send")
        return False
    
    if not EMAIL_CONFIG['recipient_email']:
        logger.warning("No recipient email configured")
        return False
    
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = EMAIL_CONFIG['sender_email']
        msg['To'] = EMAIL_CONFIG['recipient_email']
        
        html_part = MIMEText(html_content, 'html')
        msg.attach(html_part)
        
        server = smtplib.SMTP(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port'])
        server.starttls()
        server.login(EMAIL_CONFIG['sender_email'], EMAIL_CONFIG['sender_password'])
        server.send_message(msg)
        server.quit()
        
        logger.info(f"Email sent successfully to {EMAIL_CONFIG['recipient_email']}")
        return True
    
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SCANNER
# ══════════════════════════════════════════════════════════════════════════════

class AutomatedScanner:
    """Automated scanning system"""
    
    def __init__(self):
        self.sentinel = MemeSqueezeSentinelV2(watchlist=['GME', 'AMC', 'IONQ', 'RGTI', 'PLTR', 'SOFI', 'HOOD', 'QBTS', 'LUNR', 'RKLB'])
        self.telegram = TelegramNotifier()
        self.last_scan_results = []
        logger.info("Automated Scanner initialized")
    
    def run_scan(self, scan_type='Scheduled'):
        """Run a scan and process results"""
        
        logger.info(f"Starting {scan_type} scan...")
        scan_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        try:
            # Run scan
            results = self.sentinel.scan_all()
            self.last_scan_results = results
            
            if not results:
                logger.warning("No results from scan")
                return
            
            # Filter high scores
            high_scores = [r for r in results if r.explosion_score >= ALERT_THRESHOLDS['moderate']]
            critical_scores = [r for r in results if r.explosion_score >= ALERT_THRESHOLDS['critical']]
            
            logger.info(f"Scan complete: {len(results)} total, {len(high_scores)} above {ALERT_THRESHOLDS['moderate']}")
            
            # Save to DB
            run_id = save_scan_run(scan_type=scan_type, total_scanned=len(results))
            for r in results:
                save_result(run_id, {
                    "ticker": r.ticker,
                    "explosion_score": r.explosion_score,
                    "price": getattr(r, "price", None),
                    "catalyst": getattr(r, "catalyst", ""),
                })

            # Telegram alerts for critical
            for r in critical_scores:
                self.telegram.send_critical_alert(
                    r.ticker,
                    r.explosion_score,
                    getattr(r, "price", 0),
                    getattr(r, "catalyst", "")
                )
                save_alert(r.ticker, "critical", r.explosion_score, getattr(r, "catalyst", ""))

            # Telegram daily summary
            if scan_type in ("Daily Summary", "Post-Close"):
                self.telegram.send_daily_summary(results, scan_time)
            
            # Save to file
            report_path = Path(__file__).parent.parent / 'reports' / f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            report_path.parent.mkdir(exist_ok=True)
            
            with open(report_path, 'w') as f:
                json.dump([
                    {
                        'ticker': r.ticker,
                        'score': r.explosion_score,
                        "price": getattr(r, "price", None),
                        'timestamp': scan_time
                    }
                    for r in results
                ], f, indent=2)
            
            logger.info(f"Report saved to {report_path}")
        
        except Exception as e:
            logger.error(f"Scan failed: {e}")
            import traceback
            traceback.print_exc()
    
    def pre_market_scan(self):
        """Pre-market analysis (8:30 AM)"""
        logger.info("Running pre-market scan...")
        self.run_scan('Pre-Market')
    
    def market_open_scan(self):
        """Market open scan (9:30 AM)"""
        logger.info("Running market open scan...")
        self.run_scan('Market Open')
    
    def midday_scan(self):
        """Midday update (12:00 PM)"""
        logger.info("Running midday scan...")
        self.run_scan('Midday Update')
    
    def pre_close_scan(self):
        """Pre-close analysis (3:30 PM)"""
        logger.info("Running pre-close scan...")
        self.run_scan('Pre-Close')
    
    def post_close_scan(self):
        """Daily summary (4:30 PM)"""
        logger.info("Running daily summary scan...")
        self.run_scan('Daily Summary')


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

def main():
    """Main scheduler loop"""
    
    logger.info("="*60)
    logger.info("AUTOMATED SCANNER STARTING")
    logger.info("="*60)
    
    scanner = AutomatedScanner()
    
    # Schedule scans
    schedule.every().day.at(SCHEDULE_CONFIG['pre_market']).do(scanner.pre_market_scan)
    schedule.every().day.at(SCHEDULE_CONFIG['market_open']).do(scanner.market_open_scan)
    schedule.every().day.at(SCHEDULE_CONFIG['midday']).do(scanner.midday_scan)
    schedule.every().day.at(SCHEDULE_CONFIG['pre_close']).do(scanner.pre_close_scan)
    schedule.every().day.at(SCHEDULE_CONFIG['post_close']).do(scanner.post_close_scan)
    
    logger.info("Schedule configured:")
    for name, time in SCHEDULE_CONFIG.items():
        logger.info(f"  {name}: {time}")
    
    logger.info("\nScanner running... Press Ctrl+C to stop")
    
    # Run immediately on start (for testing)
    logger.info("\nRunning initial scan...")
    scanner.run_scan('Initial')
    
    # Main loop
    try:
        while True:
            schedule.run_pending()
            time_module.sleep(60)  # Check every minute
    
    except KeyboardInterrupt:
        logger.info("\nScanner stopped by user")
    except Exception as e:
        logger.error(f"Scanner error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
