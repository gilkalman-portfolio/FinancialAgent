"""
Watchdog for src/ibkr_worker.py — auto-restarts on crash OR clean exit.

Runs the IBKR worker under .venv313 (Python 3.13) because ib_async is not
compatible with Python 3.14. Register in Windows Task Scheduler to run at
startup, same pattern as run_scheduler_watchdog.py.

To stop the watchdog intentionally, create a sentinel file:
    stop_ibkr_worker.flag   (in the project root)
The watchdog will detect it on the next exit, delete it, and stop cleanly.
"""

import os
import subprocess
import sys
import time
import logging
import urllib.request
import urllib.parse
import json
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000  # subprocess flag: hide console window on Windows

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOG_DIR / "ibkr_worker_watchdog.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _load_env() -> dict:
    """Read .env file from project root — returns key/value dict."""
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
    """Send a Telegram message using credentials from .env. Fails silently."""
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

VENV313_PYTHON = ROOT / ".venv313" / "Scripts" / "python.exe"
if not VENV313_PYTHON.exists():
    raise RuntimeError(
        f".venv313 not found at {VENV313_PYTHON}. "
        f"Create it first: py -3.13 -m venv .venv313"
    )

PYTHON = str(VENV313_PYTHON)
MODULE = "src.ibkr_worker"
RESTART_DELAY = 15       # seconds between crash restarts
CLEAN_EXIT_DELAY = 60    # seconds before restarting after a clean exit (Gateway disconnect etc.)
STOP_SENTINEL = ROOT / "stop_ibkr_worker.flag"


def main():
    logging.info("=== IBKR worker watchdog started ===")
    _send_telegram("🟢 <b>IBKR Worker</b> — watchdog started, launching worker...")
    attempt = 0
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    worker_log_path = LOG_DIR / "ibkr_worker.log"

    while True:
        attempt += 1
        logging.info(f"Launching ibkr_worker (attempt #{attempt}) — output: {worker_log_path}")
        if attempt > 1:
            _send_telegram(f"🔄 <b>IBKR Worker</b> — restarting (attempt #{attempt})")

        try:
            with open(worker_log_path, "a", encoding="utf-8") as worker_log:
                proc = subprocess.run(
                    [PYTHON, "-m", MODULE],
                    cwd=str(ROOT),
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                    stdout=worker_log,
                    stderr=subprocess.STDOUT,
                )
        except Exception as e:
            logging.error(f"Failed to launch ibkr_worker: {e}")
            _send_telegram(f"🔴 <b>IBKR Worker</b> — failed to launch: {e}")
            time.sleep(RESTART_DELAY)
            continue

        # Check stop sentinel first — applies to both clean and crash exits.
        if STOP_SENTINEL.exists():
            logging.info("Stop sentinel detected — watchdog stopping cleanly.")
            STOP_SENTINEL.unlink()
            _send_telegram("⏹️ <b>IBKR Worker</b> — stopped (sentinel file detected)")
            break

        if proc.returncode == 0:
            # Clean exit: usually means IBKR Gateway went offline or worker
            # was restarted intentionally. Restart after a longer delay so we
            # don't spam reconnect attempts when the Gateway is truly down.
            logging.info(
                f"ibkr_worker exited cleanly (returncode=0). "
                f"Restarting in {CLEAN_EXIT_DELAY}s — create '{STOP_SENTINEL.name}' to stop."
            )
            _send_telegram(
                f"🟡 <b>IBKR Worker</b> — exited cleanly (Gateway disconnect?)\n"
                f"Restarting in {CLEAN_EXIT_DELAY}s..."
            )
            time.sleep(CLEAN_EXIT_DELAY)
        else:
            logging.warning(
                f"ibkr_worker crashed (returncode={proc.returncode}). "
                f"Restarting in {RESTART_DELAY}s..."
            )
            _send_telegram(
                f"🔴 <b>IBKR Worker</b> — crashed (returncode={proc.returncode})\n"
                f"Restarting in {RESTART_DELAY}s..."
            )
            time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
