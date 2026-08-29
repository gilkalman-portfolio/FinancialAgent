"""
Watchdog for scheduler.py — auto-restarts on crash.
Register in Windows Task Scheduler to run at startup.
"""

import subprocess
import sys
import time
import logging
from logging.handlers import RotatingFileHandler
import urllib.request
import urllib.parse
import os
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000  # subprocess flag: hide console window on Windows

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOG_DIR / "watchdog.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
SCRIPT = ROOT / "scheduler.py"
RESTART_DELAY = 15  # seconds between restarts
STOP_SENTINEL = ROOT / "stop_scheduler.flag"

# The scheduler child's stderr used to go to DEVNULL — an uncaught exception
# (5 same-day returncode=1 crashes on 2026-08-26) left no traceback anywhere:
# scheduler.py's own loguru sink can't record an exception that kills the
# process before any handler runs. See CLAUDE.md Incident Archive 2026-08-26.
# Deliberately a raw file redirect, not piping the child's stderr back
# through loguru — that's a separate, bigger change. Rotation thresholds
# mirror scheduler.py's loguru sink (rotation="10 MB", retention=5) for
# consistency between the two log families.
STDERR_LOG          = LOG_DIR / "scheduler_stderr.log"
STDERR_MAX_BYTES    = 10 * 1024 * 1024  # 10 MB
STDERR_BACKUP_COUNT = 5


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


def _open_stderr_log(attempt: int):
    """Opens (rotating first if the previous file is already oversized) the
    file the scheduler child process's stderr is redirected into for this
    launch attempt, with a header line so multiple attempts landing in the
    same file stay distinguishable.

    Deliberately a raw file object, not a logging.Handler — subprocess needs
    a real OS file descriptor for stderr=, which a Handler doesn't expose.
    RotatingFileHandler is reused only for its rollover/rename logic
    (doRollover() works standalone, with no dependency on going through
    emit()).

    Never raises — falls back to subprocess.DEVNULL (the old behavior) if
    the log file can't be opened, e.g. a permissions or disk-space issue,
    but always logs that fallback first so the degradation is visible in
    watchdog.log instead of silently reproducing the original gap.
    """
    try:
        roller = RotatingFileHandler(STDERR_LOG, maxBytes=STDERR_MAX_BYTES,
                                      backupCount=STDERR_BACKUP_COUNT, encoding="utf-8")
        try:
            if STDERR_LOG.exists() and STDERR_LOG.stat().st_size >= STDERR_MAX_BYTES:
                roller.doRollover()
        finally:
            roller.close()

        f = open(STDERR_LOG, "a", encoding="utf-8")
        f.write(f"\n{'=' * 80}\n{time.strftime('%Y-%m-%d %H:%M:%S')} | "
                f"Launching scheduler (attempt #{attempt})\n{'=' * 80}\n")
        f.flush()
        return f
    except Exception as e:
        logging.error(f"Failed to open stderr log ({STDERR_LOG}) — stderr will be discarded: {e}")
        return subprocess.DEVNULL


def main():
    logging.info("=== Watchdog started ===")
    _send_telegram("🟢 <b>Scheduler</b> — watchdog started, launching scheduler...")
    attempt = 0

    while True:
        attempt += 1
        logging.info(f"Launching scheduler (attempt #{attempt})")
        if attempt > 1:
            _send_telegram(f"🔄 <b>Scheduler</b> — restarting (attempt #{attempt})")

        stderr_log = _open_stderr_log(attempt)
        try:
            proc = subprocess.run(
                [PYTHON, str(SCRIPT)],
                cwd=str(ROOT),
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=stderr_log,
            )
        except Exception as e:
            logging.error(f"Failed to launch scheduler: {e}")
            _send_telegram(f"🔴 <b>Scheduler</b> — failed to launch: {e}")
            time.sleep(RESTART_DELAY)
            continue
        finally:
            if stderr_log is not subprocess.DEVNULL:
                stderr_log.close()

        # Check stop sentinel first.
        if STOP_SENTINEL.exists():
            logging.info("Stop sentinel detected — watchdog stopping cleanly.")
            STOP_SENTINEL.unlink()
            _send_telegram("⏹️ <b>Scheduler</b> — stopped (sentinel file detected)")
            break

        if proc.returncode == 0:
            logging.info("Scheduler exited cleanly (returncode=0). Watchdog stopping.")
            _send_telegram("⏹️ <b>Scheduler</b> — exited cleanly, watchdog stopping.")
            break

        logging.warning(
            f"Scheduler crashed (returncode={proc.returncode}). "
            f"Restarting in {RESTART_DELAY}s..."
        )
        _send_telegram(
            f"🔴 <b>Scheduler</b> — crashed (returncode={proc.returncode})\n"
            f"Restarting in {RESTART_DELAY}s..."
        )
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
