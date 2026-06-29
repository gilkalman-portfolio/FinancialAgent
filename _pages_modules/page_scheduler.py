"""Page: Scheduler"""
import html as _html_mod
import streamlit as st
import json
import subprocess
import sys
import os
from pathlib import Path
from src.index_loader import get_sectors

_LOG_DIR  = Path("logs")
_LOG_FILE = _LOG_DIR / "scheduler.log"
_PID_FILE = Path("scheduler.pid")
_CFG_FILE = Path("scheduler_config.json")
_TIMES    = ["07:00", "07:30", "08:00", "08:15", "08:30", "09:00", "09:15",
             "09:30", "12:00", "15:30", "16:00", "16:30", "17:00"]

_DEFAULT_CFG = {
    "enabled":                         False,
    "times":                           ["08:30", "16:30"],
    "watchlist_time":                  "09:00",
    "portfolio_time":                  "09:15",
    "market_digest_time":              "08:00",
    "portfolio_news_time":             "08:15",
    "price_alert_interval_minutes":    5,
    "news_catalyst_enabled":           True,
    "news_catalyst_interval_minutes":  15,
    "news_catalyst_threshold":         3,
    "news_catalyst_max_llm_per_cycle": 3,
    "news_catalyst_scope":             "portfolio+watchlist",
    "sectors":                         [],
    "max_stocks":                      50,
    "min_score":                       45,
    "forecast_days":                   30,
    "telegram":                        True,
}


def _is_running(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                    capture_output=True, text=True)
            return str(pid) in result.stdout
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def _stop_process(pid: int) -> tuple[bool, str]:
    try:
        if sys.platform == "win32":
            result = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                return True, f"Stopped PID {pid}"
            return False, result.stderr.strip() or "taskkill failed"
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
            return True, f"Stopped PID {pid}"
    except Exception as e:
        return False, str(e)


def _get_status() -> tuple[bool, int | None]:
    if not _PID_FILE.exists():
        return False, None
    try:
        pid = int(_PID_FILE.read_text().strip())
        if _is_running(pid):
            return True, pid
        _PID_FILE.unlink(missing_ok=True)
        return False, None
    except Exception:
        return False, None


def _safe_index(options: list, value: str, default: int = 0) -> int:
    try:
        return options.index(value)
    except ValueError:
        return default


def render():
    st.markdown("### Scheduler")
    _LOG_DIR.mkdir(exist_ok=True)

    cfg     = json.loads(_CFG_FILE.read_text()) if _CFG_FILE.exists() else _DEFAULT_CFG
    sectors = get_sectors("Russell 2000")

    # ── Status banner ──────────────────────────────────────────────────────────
    running, pid = _get_status()
    color  = "#16a34a" if running else "#6b7280"
    status = f"🟢 Scheduler is RUNNING (PID {pid})" if running else "⚪ Scheduler is STOPPED"
    st.markdown(f'<div style="background:{color};color:white;border-radius:10px;padding:10px 18px;'
                f'font-weight:700;font-size:15px;margin-bottom:12px;">{status}</div>',
                unsafe_allow_html=True)

    # ── Settings ───────────────────────────────────────────────────────────────
    st.markdown("#### Settings")
    col1, col2 = st.columns(2)

    with col1:
        enabled     = st.toggle("Enable scheduler", value=cfg.get("enabled", False))
        sel_sectors = st.multiselect("Sectors to scan", sectors, default=cfg.get("sectors", sectors[:2]))
        scan_times  = st.multiselect("Scan times", _TIMES, default=cfg.get("times", ["08:30", "16:30"]))
        watchlist_time = st.selectbox("Watchlist scan",  _TIMES, index=_safe_index(_TIMES, cfg.get("watchlist_time", "09:00")))
        portfolio_time = st.selectbox("Portfolio scan",  _TIMES, index=_safe_index(_TIMES, cfg.get("portfolio_time", "09:15")))

    with col2:
        sched_min   = st.slider("Min score", 0, 100, cfg.get("min_score", 45))
        sched_max   = st.number_input("Max stocks per sector", 10, 500, cfg.get("max_stocks", 50), step=10)
        sched_fc    = st.number_input("Forecast days", 7, 90, cfg.get("forecast_days", 30))
        telegram_on = st.toggle("Send Telegram scan summary", value=cfg.get("telegram", True))

        st.markdown("---")
        st.markdown("**📰 Telegram News Digest**")
        market_digest_time  = st.selectbox("Morning market digest", _TIMES,
                                            index=_safe_index(_TIMES, cfg.get("market_digest_time", "08:00")),
                                            help="Daily: indices + mood + top headlines")
        portfolio_news_time = st.selectbox("Portfolio news alert", _TIMES,
                                            index=_safe_index(_TIMES, cfg.get("portfolio_news_time", "08:15")),
                                            help="Daily: recent news for your portfolio holdings")
        squeeze_scan_time   = st.selectbox("Squeeze scan", _TIMES,
                                            index=_safe_index(_TIMES, cfg.get("squeeze_scan_time", "07:45")),
                                            help="Daily: squeeze candidates + entry signals via Telegram")

        st.markdown("---")
        st.markdown("**🎯 Price Target Monitor**")
        price_interval = st.select_slider(
            "Check price targets every",
            options=[1, 2, 3, 5, 10, 15, 30],
            value=cfg.get("price_alert_interval_minutes", 5),
            format_func=lambda x: f"{x} min",
            help="Sends Telegram when a watchlist ticker reaches its price target (within 1%)"
        )

        st.markdown("---")
        st.markdown("**📡 News Catalyst Monitor**")
        catalyst_enabled = st.toggle(
            "Enable news catalyst monitor",
            value=cfg.get("news_catalyst_enabled", True),
            help="Monitors portfolio+watchlist for breaking news catalysts"
        )
        catalyst_interval = st.select_slider(
            "Check news every",
            options=[5, 10, 15, 20, 30, 60],
            value=cfg.get("news_catalyst_interval_minutes", 15),
            format_func=lambda x: f"{x} min",
        )
        catalyst_threshold = st.slider(
            "Catalyst threshold",
            min_value=2, max_value=8,
            value=cfg.get("news_catalyst_threshold", 3),
            help="Higher = fewer but stronger alerts. 3=moderate, 6=major catalysts only"
        )
        catalyst_max_llm = st.slider(
            "Max LLM calls per cycle",
            min_value=1, max_value=10,
            value=cfg.get("news_catalyst_max_llm_per_cycle", 3),
            help="Cost control — limits Gemini calls per interval"
        )
        catalyst_scope = st.selectbox(
            "Monitor scope",
            options=["portfolio+watchlist", "portfolio", "watchlist"],
            index=["portfolio+watchlist", "portfolio", "watchlist"].index(
                cfg.get("news_catalyst_scope", "portfolio+watchlist")
            ),
        )
        catalyst_max_age = st.select_slider(
            "Max article age",
            options=[15, 20, 30, 45, 60, 90, 120],
            value=cfg.get("news_catalyst_max_article_age_minutes", 45),
            format_func=lambda x: f"{x} min",
            help=(
                "ידיעות ישנות יותר מערך זה מסוננות — מונע שליחת התראות על כותרות ריאקטיביות "
                "(\"X Stocks Soar After...\") שנכתבו אחרי שהמניה כבר זינקה. "
                "15 min = רק ידיעות טריות מאוד | 120 min = כמעט ללא סינון"
            ),
        )

    if st.button("💾 Save settings"):
        new_cfg = {
            "enabled":                                enabled,
            "times":                                  scan_times,
            "watchlist_time":                         watchlist_time,
            "portfolio_time":                         portfolio_time,
            "market_digest_time":                     market_digest_time,
            "portfolio_news_time":                    portfolio_news_time,
            "squeeze_scan_time":                      squeeze_scan_time,
            "price_alert_interval_minutes":           price_interval,
            "news_catalyst_enabled":                  catalyst_enabled,
            "news_catalyst_interval_minutes":         catalyst_interval,
            "news_catalyst_threshold":                catalyst_threshold,
            "news_catalyst_max_llm_per_cycle":        catalyst_max_llm,
            "news_catalyst_scope":                    catalyst_scope,
            "news_catalyst_max_article_age_minutes":  catalyst_max_age,
            "sectors":                                sel_sectors,
            "max_stocks":                             int(sched_max),
            "min_score":                              sched_min,
            "forecast_days":                          int(sched_fc),
            "telegram":                               telegram_on,
        }
        _CFG_FILE.write_text(json.dumps(new_cfg, indent=2))
        st.success("Saved. Restart the scheduler for changes to take effect.")

    # ── Jobs overview ──────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### Scheduled Jobs")

    def _jobs_from_cfg(c: dict) -> list[dict]:
        jobs = [
            {"key": "scan",           "name": "📊 Scan",              "time": ", ".join(c.get("times", [])),              "enabled": bool(c.get("times")),        "editable_key": "times",            "multi": True},
            {"key": "watchlist",      "name": "👁 Watchlist scan",    "time": c.get("watchlist_time", "—"),               "enabled": bool(c.get("watchlist_time")), "editable_key": "watchlist_time",   "multi": False},
            {"key": "portfolio",      "name": "💼 Portfolio scan",    "time": c.get("portfolio_time", "—"),               "enabled": bool(c.get("portfolio_time")), "editable_key": "portfolio_time",   "multi": False},
            {"key": "market_digest",  "name": "📰 Market digest",     "time": c.get("market_digest_time", "—"),           "enabled": bool(c.get("market_digest_time")), "editable_key": "market_digest_time", "multi": False},
            {"key": "portfolio_news", "name": "📬 Portfolio news",    "time": c.get("portfolio_news_time", "—"),          "enabled": bool(c.get("portfolio_news_time")), "editable_key": "portfolio_news_time", "multi": False},
            {"key": "squeeze_scan",   "name": "🔥 Squeeze scan",      "time": c.get("squeeze_scan_time", "—"),            "enabled": bool(c.get("squeeze_scan_time")), "editable_key": "squeeze_scan_time", "multi": False},
            {"key": "price_monitor",    "name": "🎯 Price monitor",      "time": f"every {c.get('price_alert_interval_minutes', 5)} min",      "enabled": True,                                      "editable_key": None, "multi": False},
            {"key": "catalyst_monitor", "name": "📡 Catalyst monitor",   "time": f"every {c.get('news_catalyst_interval_minutes', 15)} min",  "enabled": c.get('news_catalyst_enabled', True),     "editable_key": None, "multi": False},
        ]
        return jobs

    jobs = _jobs_from_cfg(cfg)
    edit_job = st.session_state.get("edit_job_key")

    for job in jobs:
        col_name, col_time, col_status, col_edit, col_del = st.columns([3, 2, 1.2, 1, 1])
        is_enabled = job["enabled"] and cfg.get("enabled", False)
        status_dot = "🟢" if (running and is_enabled) else "⚪"

        col_name.markdown(f"**{job['name']}**")
        col_time.markdown(f"`{job['time']}`")
        col_status.markdown(status_dot)

        # Edit
        if job["editable_key"] and col_edit.button("✏️", key=f"edit_{job['key']}"):
            st.session_state["edit_job_key"] = job["key"]
            st.rerun()

        # Delete (clear the time — disables the job)
        if job["editable_key"] and col_del.button("🗑", key=f"del_{job['key']}"):
            if job["multi"]:
                cfg[job["editable_key"]] = []
            else:
                cfg[job["editable_key"]] = ""
            _CFG_FILE.write_text(json.dumps(cfg, indent=2))
            st.success(f"{job['name']} disabled.")
            st.rerun()

    # Inline edit form
    if edit_job:
        job_obj = next((j for j in jobs if j["key"] == edit_job), None)
        if job_obj:
            st.markdown(f"**Edit: {job_obj['name']}**")
            if job_obj["multi"]:
                new_val = st.multiselect("Times", _TIMES, default=cfg.get(job_obj["editable_key"], []), key="edit_val_multi")
            else:
                cur = cfg.get(job_obj["editable_key"], _TIMES[0])
                new_val = st.selectbox("Time", _TIMES, index=_safe_index(_TIMES, cur), key="edit_val_single")
            c1, c2 = st.columns(2)
            if c1.button("💾 Save", key="edit_save"):
                cfg[job_obj["editable_key"]] = new_val
                _CFG_FILE.write_text(json.dumps(cfg, indent=2))
                st.session_state.pop("edit_job_key", None)
                st.success("Saved.")
                st.rerun()
            if c2.button("Cancel", key="edit_cancel"):
                st.session_state.pop("edit_job_key", None)
                st.rerun()

    # ── Manual send buttons ────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### Send now")
    mc1, mc2, mc3, mc4 = st.columns(4)
    with mc1:
        if st.button("📰 Send market digest now"):
            with st.spinner("Sending..."):
                try:
                    from src.telegram_news_digest import send_market_digest
                    send_market_digest()
                    st.success("Market digest sent!")
                except Exception as e:
                    st.error(f"Error: {_html_mod.escape(str(e))}")
    with mc2:
        if st.button("💼 Send portfolio news now"):
            with st.spinner("Sending..."):
                try:
                    from src.telegram_news_digest import send_portfolio_news
                    send_portfolio_news()
                    st.success("Portfolio news sent!")
                except Exception as e:
                    st.error(f"Error: {_html_mod.escape(str(e))}")
    with mc3:
        if st.button("🔥 Run squeeze scan now"):
            with st.spinner("Scanning... this may take a few minutes"):
                try:
                    from scheduler import run_squeeze_scan
                    run_squeeze_scan()
                    st.success("Squeeze scan complete — check Telegram!")
                except Exception as e:
                    st.error(f"Error: {_html_mod.escape(str(e))}")
    with mc4:
        if st.button("📡 Run catalyst check now"):
            with st.spinner("Checking news catalysts..."):
                try:
                    from src.news_catalyst_monitor import run_catalyst_check
                    _cfg = json.loads(_CFG_FILE.read_text()) if _CFG_FILE.exists() else {}
                    alerts = run_catalyst_check(
                        catalyst_threshold = _cfg.get("news_catalyst_threshold", 3),
                        max_llm_calls      = _cfg.get("news_catalyst_max_llm_per_cycle", 3),
                        scope              = _cfg.get("news_catalyst_scope", "portfolio+watchlist"),
                        force              = True,
                    )
                    st.success(f"Done — {alerts} alert(s) sent to Telegram")
                except Exception as e:
                    st.error(f"Error: {_html_mod.escape(str(e))}")

    # ── Process controls ───────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### Process control")
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("▶ Start scheduler", disabled=running):
            _LOG_DIR.mkdir(exist_ok=True)
            _SCHEDULER_PATH = Path(__file__).parent.parent / "scheduler.py"
            with open(_LOG_FILE, "a") as log_f:
                proc = subprocess.Popen(
                    [sys.executable, str(_SCHEDULER_PATH)],
                    stdout=log_f, stderr=subprocess.STDOUT,
                    cwd=str(_SCHEDULER_PATH.parent),
                    creationflags=0x00000008 if sys.platform == "win32" else 0,
                )
            _PID_FILE.write_text(str(proc.pid))
            st.success(f"Scheduler started (PID {proc.pid})")
            st.rerun()
    with col_b:
        if st.button("⏹ Stop scheduler", disabled=not running):
            ok, msg = _stop_process(pid)
            _PID_FILE.unlink(missing_ok=True)
            (st.success if ok else st.error)(msg)
            st.rerun()

    # ── IBKR Monitoring Queue ──────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 📡 IBKR Monitoring Queue")
    try:
        from src.monitoring_queue import build_queue
        from src.hysteresis import LIQUIDITY_ENTRY_ADV
        queue = build_queue()
        if queue:
            # Split into manual (watchlist) vs scanner-sourced
            manual_entries  = [e for e in queue if e.source == "manual"]
            scanner_entries = [e for e in queue if e.source == "scanner"]

            q_col1, q_col2, q_col3 = st.columns(3)
            q_col1.metric("סה\"כ במעקב", len(queue))
            q_col2.metric("מסריקה (score ≥65)", len(scanner_entries))
            q_col3.metric("מWatchlist ידני", len(manual_entries))

            with st.expander(f"📋 רשימת {len(queue)} מניות בqueue", expanded=True):
                rows = []
                for e in sorted(queue, key=lambda x: -(x.composite_score or 0)):
                    rows.append({
                        "Ticker": e.ticker,
                        "Score":  f"{e.composite_score:.0f}" if e.composite_score is not None else "—",
                        "Source": e.source,
                        "Last Seen": e.last_seen[:16] if e.last_seen else "—",
                    })
                import pandas as pd
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("Queue ריקה — הסריקה הבאה תאכלס אותה (08:30 / 16:30).")
    except Exception as _qe:
        st.warning(f"לא ניתן לטעון queue: {_qe}")

    # ── Log viewer ─────────────────────────────────────────────────────────────
    st.markdown("---")
    if _LOG_FILE.exists():
        with st.expander("📋 Scheduler log (last 50 lines)", expanded=running):
            lines = _LOG_FILE.read_text(errors="ignore").splitlines()[-50:]
            st.code("\n".join(lines) if lines else "(empty)", language="text")
        if st.button("🗑 Clear log"):
            _LOG_FILE.write_text("")
            st.rerun()
    else:
        st.caption("No log file yet.")
