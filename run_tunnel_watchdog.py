"""
Watchdog for run_dashboard_tunnel.py — auto-restarts on crash or clean exit.
Register in Windows Task Scheduler to run at startup.

Stop cleanly: create a file named  stop_tunnel.flag  in the project root.
The watchdog detects it on the next exit, deletes it, and stops without restarting.
"""

import subprocess
import sys
import time
import logging
import urllib.request
import urllib.parse
import os
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOG_DIR / "tunnel_watchdog.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
SCRIPT = ROOT / "run_dashboard_tunnel.py"
RESTART_DELAY = 15       # seconds to wait before restarting after a crash
CLEAN_EXIT_DELAY = 60    # seconds to wait before restarting after a clean exit
STOP_SENTINEL = ROOT / "stop_tunnel.flag"


def _load_env() -> dict:
    env = {}
    env_path = ROOT / ".env"
    if not env_path.exists():
        return env
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _send_telegram(message: str) -> None:
    try:
        cfg = _load_env()
        token = cfg.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = cfg.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
        enabled = cfg.get("TELEGRAM_ENABLED", "true").lower()
        if not token or not chat_id or enabled == "false":
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logging.warning(f"Telegram notify failed: {e}")


def main():
    logging.info("=== Tunnel Watchdog started ===")
    _send_telegram("🟢 <b>Tunnel Watchdog</b> — started, launching dashboard tunnel...")
    attempt = 0

    while True:
        attempt += 1
        logging.info(f"Launching tunnel (attempt #{attempt})")
        if attempt > 1:
            _send_telegram(f"🔄 <b>Tunnel</b> — restarting (attempt #{attempt}), new URL will follow...")

        try:
            proc = subprocess.run(
                [PYTHON, str(SCRIPT)],
                cwd=str(ROOT),
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logging.error(f"Failed to launch tunnel: {e}")
            _send_telegram(f"🔴 <b>Tunnel</b> — failed to launch: {e}")
            time.sleep(RESTART_DELAY)
            continue

        # Check stop sentinel before deciding whether to restart
        if STOP_SENTINEL.exists():
            logging.info("Stop sentinel detected — watchdog stopping cleanly.")
            STOP_SENTINEL.unlink()
            _send_telegram("⏹️ <b>Tunnel</b> — stopped (sentinel file detected)")
            break

        if proc.returncode == 0:
            # Clean exit — restart after a short delay (mirrors ibkr_worker watchdog behaviour:
            # a returncode=0 usually means the tunnel detected it was dead and exited on purpose,
            # so we want to bring it back up, not stop the watchdog permanently).
            logging.info(
                f"Tunnel exited cleanly (returncode=0). "
                f"Restarting in {CLEAN_EXIT_DELAY}s..."
            )
            _send_telegram(
                f"🔄 <b>Tunnel</b> — exited cleanly, restarting in {CLEAN_EXIT_DELAY}s..."
            )
            time.sleep(CLEAN_EXIT_DELAY)
            continue

        logging.warning(
            f"Tunnel crashed (returncode={proc.returncode}). "
            f"Restarting in {RESTART_DELAY}s..."
        )
        _send_telegram(
            f"🔴 <b>Tunnel</b> — crashed (returncode={proc.returncode})\n"
            f"Restarting in {RESTART_DELAY}s..."
        )
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
