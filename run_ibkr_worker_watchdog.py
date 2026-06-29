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

VENV313_DIR = ROOT / ".venv313"
if not VENV313_DIR.exists():
    raise RuntimeError(
        f".venv313 not found at {VENV313_DIR}. "
        f"Create it first: py -3.13 -m venv .venv313"
    )

# On Python 3.13 Windows, Scripts\python.exe is the Python Launcher (py.exe shim),
# NOT the real interpreter. The launcher always spawns the actual Python as a child,
# causing two processes to appear for every worker start. We bypass this by reading
# pyvenv.cfg to get the base interpreter and activating the venv via env vars instead.
_pyvenv_cfg = VENV313_DIR / "pyvenv.cfg"
_BASE_PYTHON = None
if _pyvenv_cfg.exists():
    for _line in _pyvenv_cfg.read_text().splitlines():
        if _line.startswith("executable"):
            _BASE_PYTHON = _line.split("=", 1)[1].strip()
            break
if not _BASE_PYTHON or not __import__("pathlib").Path(_BASE_PYTHON).exists():
    # Fallback: use Scripts\python.exe (launcher) if base interpreter not found
    _BASE_PYTHON = str(VENV313_DIR / "Scripts" / "python.exe")

PYTHON = _BASE_PYTHON
MODULE = "src.ibkr_worker"
RESTART_DELAY = 15       # seconds between crash restarts
CLEAN_EXIT_DELAY = 60    # seconds before restarting after a clean exit (Gateway disconnect etc.)
STOP_SENTINEL = ROOT / "stop_ibkr_worker.flag"
WORKER_PID_FILE = ROOT / "ibkr_worker.pid"


def _kill_orphaned_worker() -> None:
    """Kill any ibkr_worker left running from a previous watchdog crash.

    When the watchdog itself crashes, the Task Scheduler restarts it but the
    child worker process is NOT killed automatically on Windows (orphan process).
    Reading the PID file lets us terminate it before spawning a fresh worker,
    preventing the chronic 'two workers running' situation.
    """
    if not WORKER_PID_FILE.exists():
        return
    try:
        pid = int(WORKER_PID_FILE.read_text().strip())
        WORKER_PID_FILE.unlink(missing_ok=True)
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)  # PROCESS_TERMINATE=1
        if handle:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.CloseHandle(handle)
            logging.info(f"Killed orphaned ibkr_worker PID {pid}")
            _send_telegram(f"⚠️ <b>IBKR Worker</b> — killed orphaned worker PID {pid} from previous crash")
    except Exception as e:
        logging.warning(f"Could not kill orphaned worker from PID file: {e}")


def main():
    logging.info("=== IBKR worker watchdog started ===")
    _kill_orphaned_worker()
    _send_telegram("🟢 <b>IBKR Worker</b> — watchdog started, launching worker...")
    attempt = 0
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # Activate the venv for the base interpreter.
    # Python finds pyvenv.cfg relative to __PYVENV_LAUNCHER__ (the venv's python.exe shim),
    # not relative to the real executable. Setting this env var tells the base interpreter
    # which venv to activate — same mechanism the venv launcher uses internally.
    venv_scripts = str(VENV313_DIR / "Scripts")
    env["VIRTUAL_ENV"] = str(VENV313_DIR)
    env["PATH"] = venv_scripts + os.pathsep + env.get("PATH", "")
    env["__PYVENV_LAUNCHER__"] = str(VENV313_DIR / "Scripts" / "python.exe")
    env.pop("PYTHONHOME", None)  # must not be set when activating a venv manually

    worker_log_path = LOG_DIR / "ibkr_worker.log"

    while True:
        attempt += 1
        logging.info(f"Launching ibkr_worker (attempt #{attempt}) — output: {worker_log_path}")
        if attempt > 1:
            _send_telegram(f"🔄 <b>IBKR Worker</b> — restarting (attempt #{attempt})")

        try:
            with open(worker_log_path, "a", encoding="utf-8") as worker_log:
                proc = subprocess.Popen(
                    [PYTHON, "-m", MODULE],
                    cwd=str(ROOT),
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                    stdout=worker_log,
                    stderr=subprocess.STDOUT,
                )
                # Write PID so a future watchdog restart can kill this orphan if needed
                WORKER_PID_FILE.write_text(str(proc.pid))
                proc.wait()
                WORKER_PID_FILE.unlink(missing_ok=True)
        except Exception as e:
            logging.error(f"Failed to launch ibkr_worker: {e}")
            _send_telegram(f"🔴 <b>IBKR Worker</b> — failed to launch: {e}")
            WORKER_PID_FILE.unlink(missing_ok=True)
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
