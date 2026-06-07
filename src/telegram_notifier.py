"""
Telegram Alert System

Get bot token: @BotFather on Telegram
Get chat ID: @userinfobot on Telegram

Features:
- Critical alerts (85+ scores)
- Daily summaries
- Custom formatting
- Image support
"""

import requests
from typing import Optional, List
from datetime import datetime
from loguru import logger
import os
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
TELEGRAM_ENABLED = os.getenv('TELEGRAM_ENABLED', 'false').lower() == 'true'

TELEGRAM_API_URL = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}'


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM NOTIFIER
# ══════════════════════════════════════════════════════════════════════════════

class TelegramNotifier:
    """Send alerts to Telegram"""
    
    def __init__(self):
        self.bot_token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.enabled = TELEGRAM_ENABLED
        
        if not self.enabled:
            logger.info("Telegram notifications disabled")
        elif not self.bot_token or not self.chat_id:
            logger.warning("Telegram credentials not configured")
            self.enabled = False
        else:
            logger.info("Telegram notifier initialized")
    
    MAX_MSG_LEN = 4000  # Telegram hard limit is 4096; leave margin for safety

    def send_message(self, text: str, parse_mode: str = 'Markdown') -> bool:
        if not self.enabled:
            logger.debug("Telegram disabled - skipping message")
            return False

        if len(text) > self.MAX_MSG_LEN:
            text = text[:self.MAX_MSG_LEN - 20] + "\n…[truncated]"
            logger.warning("Telegram message truncated to 4000 chars")

        try:
            url = f"{TELEGRAM_API_URL}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': parse_mode,
                'disable_web_page_preview': True
            }
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Telegram message sent successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False
    
    def send_photo(self, photo_url: str, caption: str = '') -> bool:
        """Send photo with caption"""
        if not self.enabled:
            return False
        
        try:
            url = f"{TELEGRAM_API_URL}/sendPhoto"
            
            payload = {
                'chat_id': self.chat_id,
                'photo': photo_url,
                'caption': caption,
                'parse_mode': 'Markdown'
            }
            
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            
            logger.info("Telegram photo sent successfully")
            return True
        
        except Exception as e:
            logger.error(f"Failed to send Telegram photo: {e}")
            return False
    
    # ──────────────────────────────────────────────────────────────────────────
    # FORMATTED ALERTS
    # ──────────────────────────────────────────────────────────────────────────
    
    def send_critical_alert(self, ticker: str, score: float, price: float, catalyst: str):
        """Send critical alert (85+)"""
        
        emoji = '🔥' if score >= 90 else '🚨'
        
        message = f"""
{emoji} *CRITICAL ALERT* {emoji}

*{ticker}* - Score: *{score:.1f}/100*
Price: ${price:.2f}

📊 *Catalyst:*
{catalyst}

⚠️ _High conviction opportunity detected!_
        """.strip()
        
        return self.send_message(message)
    
    def send_daily_summary(self, results: List, scan_time: str):
        """Send daily summary"""
        
        critical = [r for r in results if r.get("explosion_score", 0) >= 85]
        high = [r for r in results if 70 <= r.get("explosion_score", 0) < 85]
        moderate = [r for r in results if 60 <= r.get("explosion_score", 0) < 70]
        
        message = f"""
📊 *Daily Summary* - {scan_time}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
        
        if critical:
            message += f"🔥 *Critical (85+):* {len(critical)}\n"
            for r in critical[:3]:
                message += f"  • {r.get('ticker','?')}: {r.get('explosion_score',0):.1f} (${r.get('price',0):.2f})\n"
            if len(critical) > 3:
                message += f"  • ... +{len(critical)-3} more\n"
            message += "\n"
        
        if high:
            message += f"⚡ *High (70-84):* {len(high)}\n"
            for r in high[:3]:
                message += f"  • {r.get('ticker','?')}: {r.get('explosion_score',0):.1f}\n"
            if len(high) > 3:
                message += f"  • ... +{len(high)-3} more\n"
            message += "\n"
        
        if moderate:
            message += f"📍 *Moderate (60-69):* {len(moderate)}\n\n"
        
        message += f"*Total Scanned:* {len(results)}\n"
        message += f"_View full dashboard for details_"
        
        return self.send_message(message)
    
    def send_news_alert(self, ticker: str, headline: str, sentiment: str):
        """Send news alert with sentiment"""
        
        emoji_map = {
            'Bullish': '🟢',
            'Very Bullish': '💚',
            'Bearish': '🔴',
            'Very Bearish': '❤️',
            'Neutral': '⚪'
        }
        
        emoji = emoji_map.get(sentiment, '📰')
        
        message = f"""
{emoji} *News Alert*

*{ticker}* - _{sentiment}_

_{headline}_
        """.strip()
        
        return self.send_message(message)
    
    def send_insider_alert(self, ticker: str, insider_score: float, purchases: int, total_value: float):
        """Send insider activity alert"""
        
        if insider_score >= 80:
            emoji = '🔥'
        elif insider_score >= 60:
            emoji = '⚡'
        else:
            emoji = '📊'
        
        message = f"""
{emoji} *Insider Activity*

*{ticker}* - Conviction: *{insider_score:.1f}/100*

📈 Purchases (90d): {purchases}
💰 Total Value: ${total_value/1_000_000:.1f}M

_Strong insider buying detected_
        """.strip()
        
        return self.send_message(message)
    
    def send_test_message(self):
        """Send test message"""
        message = """
✅ *Telegram Notifications Active*

Meme-Squeeze Sentinel V2.0
Phase 3.5 - Advanced Features

You'll receive:
• 🔥 Critical alerts (85+ scores)
• 📊 Daily summaries
• 📰 Major news events
• 💰 Insider activity

_System operational_
        """.strip()
        
        return self.send_message(message)


# ══════════════════════════════════════════════════════════════════════════════
# TESTING
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    notifier = TelegramNotifier()
    
    print("\nTesting Telegram Notifications...")
    print("="*60)
    
    if not notifier.enabled:
        print("\n[X] Telegram is disabled")
        print("\nTo enable:")
        print("1. Get bot token from @BotFather on Telegram")
        print("2. Get chat ID from @userinfobot")
        print("3. Add to .env:")
        print("   TELEGRAM_ENABLED=true")
        print("   TELEGRAM_BOT_TOKEN=your_token")
        print("   TELEGRAM_CHAT_ID=your_chat_id")
    else:
        print("\n[OK] Telegram is enabled")
        print("\nSending test message...")

        if notifier.send_test_message():
            print("[OK] Test message sent!")
        else:
            print("[X] Failed to send message")

        print("\nSending example alerts...")

        # Critical alert
        notifier.send_critical_alert('GME', 87.3, 45.20, 'High SI | Insider cluster | Volume surge')

        # News alert
        notifier.send_news_alert('IONQ', 'IONQ announces quantum breakthrough', 'Very Bullish')

        # Insider alert
        notifier.send_insider_alert('GME', 78.0, 36, 19_000_000)

        print("\n[OK] Example alerts sent!")
