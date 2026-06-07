"""
Starts Streamlit dashboard + Cloudflare Quick Tunnel.
Sends tunnel URL to Telegram on startup.
"""
import subprocess
import sys
import re
import time
import threading
import logging
import socket
from pathlib import Path
import os
from dotenv import load_dotenv
import requests

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOG_DIR / "dashboard_tunnel.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

PYTHON = sys.executable
CLOUDFLARED = str(BASE_DIR / "cloudflared.exe")
STREAMLIT_PORT = 8501
CREATE_NO_WINDOW = 0x08000000


def send_telegram(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
    if not enabled or not token or not chat_id:
        logging.warning("Telegram not configured — skipping URL notification")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            },
            timeout=10,
        )
        logging.info("Telegram notification sent")
    except Exception as e:
        logging.error(f"Telegram send failed: {e}")


def start_streamlit():
    cmd = [
        PYTHON, "-m", "streamlit", "run",
        str(BASE_DIR / "dashboard.py"),
        "--server.port", str(STREAMLIT_PORT),
        "--server.headless", "true",
    ]
    proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), creationflags=CREATE_NO_WINDOW)
    logging.info(f"Streamlit started (PID {proc.pid})")
    return proc


def start_tunnel():
    cmd = [CLOUDFLARED, "tunnel", "--url", f"http://localhost:{STREAMLIT_PORT}", "--no-autoupdate"]
    proc = subprocess.Popen(
        cmd, cwd=str(BASE_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, creationflags=CREATE_NO_WINDOW,
    )
    logging.info(f"Cloudflare tunnel started (PID {proc.pid})")
    return proc


def extract_url(proc, timeout=40):
    url_pattern = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")
    deadline = time.time() + timeout
    for line in proc.stdout:
        logging.debug(f"cloudflared: {line.rstrip()}")
        match = url_pattern.search(line)
        if match:
            return match.group(0)
        if time.time() > deadline:
            break
    return None


def drain(proc):
    for line in proc.stdout:
        logging.debug(f"cloudflared: {line.rstrip()}")


METRICS_URL = "http://127.0.0.1:20241/metrics"
HEALTH_CHECK_INTERVAL = 60   # seconds between health checks
HEALTH_MAX_FAILS      = 3    # consecutive failures before declaring tunnel dead
HEARTBEAT_HOUR        = 8    # daily heartbeat at 08:05
HEARTBEAT_MINUTE      = 5


def _tunnel_healthy(public_url: str | None = None) -> bool:
    """Return True if cloudflared is running AND the public URL resolves in DNS."""
    # 1. Local metrics check (fast)
    try:
        r = requests.get(METRICS_URL, timeout=5)
        if r.status_code != 200:
            return False
    except Exception:
        return False

    # 2. Public DNS check — catches the case where cloudflared is running locally
    #    but Cloudflare has already deregistered the quick-tunnel DNS record.
    if public_url:
        hostname = public_url.replace("https://", "").replace("http://", "").rstrip("/")
        try:
            socket.getaddrinfo(hostname, 443, socket.AF_INET)
        except socket.gaierror:
            logging.warning(f"DNS lookup failed for {hostname} — tunnel URL expired")
            return False

    return True


def _heartbeat_thread(url_holder: list):
    """
    Background thread — sends a daily Telegram reminder at HEARTBEAT_HOUR:HEARTBEAT_MINUTE
    with the current tunnel URL.  url_holder is a 1-element list so the main thread
    can update it in-place if the tunnel ever restarts.
    """
    import datetime
    last_sent_date = None

    while True:
        now = datetime.datetime.now()
        if (now.hour == HEARTBEAT_HOUR and now.minute == HEARTBEAT_MINUTE
                and last_sent_date != now.date()):
            url = url_holder[0]
            if url:
                healthy = _tunnel_healthy(url)
                status = "🟢 פעיל" if healthy else "🔴 לא מגיב"
                send_telegram(
                    f"🌐 *FinancialAgent — Daily URL Reminder*\n\n"
                    f"[{url}]({url})\n\n"
                    f"Status: {status}\n"
                    f"_עודכן: {now.strftime('%H:%M')}_"
                )
                logging.info(f"Daily heartbeat sent ({status})")
            last_sent_date = now.date()
        time.sleep(30)  # check every 30s — precise enough for minute-level trigger


def main():
    logging.info("=== Dashboard Tunnel started ===")

    streamlit_proc = start_streamlit()
    time.sleep(4)

    tunnel_proc = start_tunnel()

    logging.info("Waiting for tunnel URL...")
    url = extract_url(tunnel_proc, timeout=40)

    if url:
        logging.info(f"Tunnel URL: {url}")
        send_telegram(
            f"🌐 *FinancialAgent Dashboard is LIVE*\n\n"
            f"[{url}]({url})\n\n"
            f"_לחץ לפתיחת הדשבורד מכל מקום_"
        )
    else:
        logging.warning("Could not extract tunnel URL within timeout")
        send_telegram("⚠️ *FinancialAgent* — tunnel started but URL not captured. Check `logs/dashboard_tunnel.log`.")

    threading.Thread(target=drain, args=(tunnel_proc,), daemon=True).start()

    # Daily heartbeat — url_holder[0] lets the thread always use the latest URL
    url_holder = [url]
    threading.Thread(target=_heartbeat_thread, args=(url_holder,), daemon=True).start()

    fail_count = 0
    last_health_check = time.time()

    while True:
        if streamlit_proc.poll() is not None:
            logging.warning(f"Streamlit exited (code {streamlit_proc.returncode}), shutting down tunnel")
            tunnel_proc.terminate()
            break
        if tunnel_proc.poll() is not None:
            logging.warning(f"Tunnel exited (code {tunnel_proc.returncode}), shutting down Streamlit")
            streamlit_proc.terminate()
            break

        # Periodic health check — detect zombie cloudflared or expired DNS
        if time.time() - last_health_check >= HEALTH_CHECK_INTERVAL:
            last_health_check = time.time()
            if _tunnel_healthy(url):
                fail_count = 0
            else:
                fail_count += 1
                logging.warning(f"Tunnel health check failed ({fail_count}/{HEALTH_MAX_FAILS})")
                if fail_count >= HEALTH_MAX_FAILS:
                    logging.error("Tunnel declared dead — restarting everything")
                    send_telegram("🔄 *FinancialAgent* — tunnel disconnected, restarting...")
                    tunnel_proc.terminate()
                    streamlit_proc.terminate()
                    break

        time.sleep(5)


if __name__ == "__main__":
    main()
