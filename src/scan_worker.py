"""
Scan Worker — רץ כ-background process נפרד.
מקבל scan jobs מה-DB, מריץ, ומעדכן progress.

הרצה:  python -m src.scan_worker
       (מופעל אוטומטית מה-dashboard)
"""

import sys
import json
import time
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from loguru import logger
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database import (
    init_db, save_scan_run, save_result,
    create_scan_job, get_scan_job, get_latest_scan_job, update_scan_job
)

POLL_INTERVAL = 1.5   # שניות בין בדיקות לjobs חדשים
MAX_WORKERS   = 6


def run_job(job: dict):
    """מריץ job בודד — כל הסריקה"""
    from src.stock_scorer import score_stock, signal_label
    from src.index_loader import get_tickers_by_sector

    job_id = job["id"]
    params = json.loads(job["params"])

    sel_index     = params.get("index", "Russell 2000")
    sel_sectors   = params.get("sectors", [])
    watchlist     = params.get("watchlist", [])
    min_score     = params.get("min_score", 40)
    max_stocks    = params.get("max_stocks", 50)
    forecast_days = params.get("forecast_days", 30)

    # בנה רשימת מניות
    tickers_map = {}
    for sec in sel_sectors:
        tickers = get_tickers_by_sector(sel_index, sec, max_stocks)
        if tickers:
            tickers_map[sec] = tickers
    if watchlist:
        tickers_map["Watchlist"] = watchlist

    all_tasks = [(t, src) for src, tickers in tickers_map.items() for t in tickers]
    total     = len(all_tasks)

    if total == 0:
        update_scan_job(job_id, status="done", done=0, total=0)
        return

    run_id = save_scan_run("background", total)
    update_scan_job(job_id, status="running", total=total, done=0, run_id=run_id)
    from src import score_cache; score_cache.clear()
    logger.info(f"Job {job_id}: scanning {total} tickers")

    done = 0

    def _score_one(args):
        ticker, source = args
        try:
            r = score_stock(ticker, forecast_days=forecast_days)
            return ticker, source, r
        except Exception as e:
            logger.debug(f"{ticker}: {e}")
            return ticker, source, None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_score_one, task): task for task in all_tasks}
        for future in as_completed(futures):
            ticker, source, r = future.result()
            done += 1
            update_scan_job(job_id, done=done)

            if r and (r["score"] >= min_score or "Watchlist" in source):
                save_result(run_id, {**r, "explosion_score": r["score"]})

    update_scan_job(job_id, status="done", done=done)
    logger.info(f"Job {job_id}: done ({done}/{total})")


def worker_loop():
    """Loop ראשי — מחכה לjobs חדשים"""
    init_db()
    logger.info("Scan worker started, polling for jobs...")

    while True:
        job = get_latest_scan_job()
        if job and job["status"] == "pending":
            try:
                run_job(job)
            except Exception as e:
                logger.error(f"Job {job['id']} failed: {e}")
                update_scan_job(job["id"], status="error", error=str(e))
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    worker_loop()
