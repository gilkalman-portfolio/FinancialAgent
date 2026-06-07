"""Page: Scan"""
import streamlit as st
import pandas as pd
import json, sys, time as _time
from pathlib import Path
from datetime import datetime, timedelta
import subprocess
from src.ui_theme import badge, tooltip, score_cell
from src.stock_scorer import score_stock, signal_label
from src.database import get_latest_scan, create_scan_job, get_scan_job, get_connection
from src.index_loader import get_index, get_sectors, get_tickers_by_sector, list_indices


@st.cache_data(ttl=3600)
def _load_indices():
    return list_indices()

@st.cache_data(ttl=3600)
def _load_sectors_for_index(index_name):
    return get_sectors(index_name)

@st.cache_data(ttl=3600)
def _load_tickers(index_name, sector, max_stocks):
    return get_tickers_by_sector(index_name, sector, max_stocks if max_stocks else None)


def _results_to_df(results):
    rows = []
    for r in results:
        dcf = r.get("dcf") or {}
        rows.append({
            "Ticker":    r["ticker"],
            "Score":     r["score"],
            "Signal":    signal_label(r["score"]),
            "Price":     r.get("price"),
            "FC%":       r.get("forecast_change"),
            "RSI":       r.get("rsi"),
            "MACD":      r.get("macd"),
            "MA Trend":  r.get("ma_trend"),
            "SI% Float": r.get("short_pct"),
            "Squeeze":   r.get("squeeze_active", False),
            "DCF Value": dcf.get("intrinsic_value"),
            "MoS%":      dcf.get("margin_of_safety"),
            "Valuation": dcf.get("valuation"),
        })
    return pd.DataFrame(rows).sort_values("Score", ascending=False)


def render():
    st.markdown("### Scan")
    col1, col2 = st.columns([2, 1])
    with col1:
        available_indices = _load_indices()
        sel_index   = st.selectbox("Index", available_indices, index=0)
        sectors     = _load_sectors_for_index(sel_index)
        sel_sectors = st.multiselect("Sectors", sectors, default=sectors[:1] if sectors else [])
        watchlist_raw = st.text_input("Watchlist (optional, comma separated)", "")
        if st.button("Refresh index data"):
            st.cache_data.clear()
            get_index(sel_index, force_refresh=True)
            st.success(f"{sel_index} refreshed")
    with col2:
        min_score     = st.slider("Min score", 0, 100, 40)
        max_stocks    = st.number_input("Max stocks per sector", 10, 500, 50, step=10)
        forecast_days = st.number_input("Forecast days", 7, 90, 30)

    st.markdown("""<div class="scan-info-box">
    Estimated scan time: ~6-10 sec per stock. Scan runs in background — you can navigate freely.
    </div>""", unsafe_allow_html=True)

    st.markdown('<div class="btn-primary">', unsafe_allow_html=True)
    run = st.button("Run scan")
    st.markdown('</div>', unsafe_allow_html=True)
    run_watchlist_only = st.button("Scan Watchlist Only")

    if run or run_watchlist_only:
        tickers_map = {}
        if run and not run_watchlist_only:
            for sec in sel_sectors:
                tickers = _load_tickers(sel_index, sec, int(max_stocks))
                if tickers: tickers_map[sec] = tickers
        if watchlist_raw.strip():
            wl = [t.strip().upper() for t in watchlist_raw.split(",") if t.strip()]
            if wl: tickers_map["Watchlist"] = wl

        total = sum(len(v) for v in tickers_map.values())
        if total == 0:
            st.warning("No tickers to scan.")
        else:
            job_params = {
                "index":        sel_index,
                "sectors":      sel_sectors if not run_watchlist_only else [],
                "watchlist":    [t.strip().upper() for t in watchlist_raw.split(",") if t.strip()] if watchlist_raw.strip() else [],
                "min_score":    int(min_score),
                "max_stocks":   int(max_stocks),
                "forecast_days": int(forecast_days),
            }
            job_id = create_scan_job(job_params)
            st.session_state["active_job_id"] = job_id
            st.session_state.pop("scan_results", None)
            from src import score_cache as _sc; _sc.clear()

            worker_pid_file = Path("scan_worker.pid")
            worker_running  = False
            if worker_pid_file.exists():
                try:
                    import os
                    pid = int(worker_pid_file.read_text().strip())
                    os.kill(pid, 0)
                    worker_running = True
                except Exception:
                    worker_pid_file.unlink(missing_ok=True)

            if not worker_running:
                import os
                os.makedirs("logs", exist_ok=True)
                _log_fh = open("logs/scan_worker.log", "a")
                proc = subprocess.Popen(
                    [sys.executable, "-m", "src.scan_worker"],
                    stdout=_log_fh,
                    stderr=subprocess.STDOUT,
                    creationflags=0x00000008 if sys.platform == "win32" else 0,
                    cwd=str(Path(__file__).parent.parent)
                )
                _log_fh.close()
                worker_pid_file.write_text(str(proc.pid))
            st.rerun()

    # Polling
    job_id = st.session_state.get("active_job_id")
    if job_id:
        job = get_scan_job(job_id)
        if job:
            status = job["status"]
            done   = job["done"] or 0
            total  = job["total"] or 0
            if status == "running":
                pct = done / total if total > 0 else 0
                st.progress(pct, text=f"Scanning: {done}/{total} stocks ({pct*100:.0f}%)")
                st.caption("You can navigate to other pages — scan continues in background")
                _time.sleep(2); st.rerun()
            elif status == "pending":
                st.info("Scan pending..."); _time.sleep(1); st.rerun()
            elif status == "done":
                run_id = job.get("run_id")
                if run_id:
                    with get_connection() as conn:
                        rows = conn.execute(
                            "SELECT * FROM scan_results WHERE run_id = ? ORDER BY explosion_score DESC",
                            (run_id,)
                        ).fetchall()
                    results = []
                    for row in rows:
                        raw = json.loads(row["raw_data"]) if row["raw_data"] else {}
                        raw["score"]    = row["explosion_score"]
                        raw["ticker"]   = row["ticker"]
                        raw["price"]    = row["price"]
                        raw["rsi"]      = row["rsi"] if row["rsi"] else raw.get("rsi")
                        raw["macd"]     = raw.get("macd") or row["macd_signal"]
                        raw["ma_trend"] = row["ma_trend"] if row["ma_trend"] else raw.get("ma_trend")
                        results.append(raw)
                    st.session_state["scan_results"] = results
                st.session_state.pop("active_job_id", None)
                st.success(f"Scan complete — {done} stocks scanned"); st.rerun()
            elif status == "error":
                st.error(f"Scan error: {job.get('error', '')}");
                st.session_state.pop("active_job_id", None)

    results = st.session_state.get("scan_results", [])
    if results:
        _render_results_table(results)
    elif not st.session_state.get("active_job_id"):
        latest = get_latest_scan(50)
        if latest:
            st.caption("Showing last scan from DB")
            for r in latest:
                raw = json.loads(r["raw_data"]) if r.get("raw_data") else {}
                r["macd"]            = raw.get("macd") or r.get("macd_signal")
                r["ma_trend"]        = r.get("ma_trend") or raw.get("ma_trend")
                r["score"]           = r["explosion_score"]
                r["short_pct"]       = raw.get("short_pct")
                r["forecast_change"] = raw.get("forecast_change")
                r["dcf"]             = raw.get("dcf")
            _render_results_table([dict(r) for r in latest])


def _render_results_table(results):
    buys    = [r for r in results if signal_label(r["score"]) in ("BUY", "STRONG BUY")]
    watches = [r for r in results if signal_label(r["score"]) == "WATCH"]
    top     = max(results, key=lambda x: x["score"])

    # DCF summary
    undervalued = [r for r in results if (r.get("dcf") or {}).get("margin_of_safety", -999) >= 20]

    st.markdown(f"""<div class="metric-row">
      <div class="metric-card"><div class="metric-num">{len(results)}</div><div class="metric-lbl">Stocks above threshold</div></div>
      <div class="metric-card"><div class="metric-num">{len(buys)}</div><div class="metric-lbl">Buy signals</div></div>
      <div class="metric-card"><div class="metric-num">{len(watches)}</div><div class="metric-lbl">Watch signals</div></div>
      <div class="metric-card"><div class="metric-num">{top['score']:.0f}</div><div class="metric-lbl">Top score ({top['ticker']})</div></div>
      <div class="metric-card"><div class="metric-num">{len(undervalued)}</div><div class="metric-lbl">DCF Undervalued</div></div>
    </div>""", unsafe_allow_html=True)

    df = _results_to_df(results)
    html_rows = ""
    for _, row in df.iterrows():
        fc    = f"<td class='fc-cell'>{row['FC%']:+.1f}%</td>" if pd.notna(row["FC%"]) else "<td class='fc-cell'>N/A</td>"
        rsi   = f"{row['RSI']:.0f}" if pd.notna(row["RSI"]) else "N/A"
        pr    = f"${row['Price']:.2f}" if pd.notna(row["Price"]) else "N/A"
        si_val = row["SI% Float"]
        si    = f"{si_val:.1f}%{' 🔥' if row.get('Squeeze') else ''}" if pd.notna(si_val) else "N/A"

        # DCF cell
        mos = row.get("MoS%")
        dcf_val = row.get("DCF Value")
        if pd.notna(mos) and dcf_val is not None:
            mos_color = "#16a34a" if mos >= 20 else "#d97706" if mos >= 0 else "#dc2626"
            dcf_cell  = f"<td><span style='color:{mos_color};font-weight:600;'>${dcf_val:.2f}</span><br><span style='font-size:10px;color:{mos_color};'>{mos:+.0f}% MoS</span></td>"
        else:
            dcf_cell = "<td style='color:#94a3b8;font-size:11px;'>N/A</td>"

        html_rows += f"""<tr>
          <td><strong>{row['Ticker']}</strong></td>
          <td>{score_cell(row['Score'])}</td>
          <td>{badge(row['Signal'])}</td>
          <td>{pr}</td>
          {fc}<td>{rsi}</td>
          <td>{row['MACD'] or 'N/A'}</td>
          <td>{row['MA Trend'] or 'N/A'}</td>
          <td>{si}</td>
          {dcf_cell}
        </tr>"""

    st.markdown(f"""<table>
      <thead><tr>
        <th>Ticker</th><th>{tooltip('Score')}</th><th>Signal</th><th>Price</th>
        <th style="color:#94a3b8;">{tooltip('FC%')} *</th>
        <th>{tooltip('RSI')}</th><th>{tooltip('MACD')}</th>
        <th>{tooltip('MA Trend')}</th><th>{tooltip('SI% Float')}</th>
        <th>{tooltip('DCF / MoS')}</th>
      </tr></thead>
      <tbody>{html_rows}</tbody>
    </table>""", unsafe_allow_html=True)
    st.caption("* FC% = indicative forecast only · MoS = Margin of Safety vs intrinsic value")
