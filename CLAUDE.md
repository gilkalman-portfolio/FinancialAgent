# FinancialAgent — Claude Code Context

## Project Overview
AI-powered stock scanner & financial analysis dashboard.
- **Location:** `C:/Projects/FinancialAgent`
- **Stack:** Python 3.14, Streamlit 1.52.2, SQLite, yfinance, Finnhub, Alpha Vantage, SEC EDGAR
- **LLMs:** Gemini 2.0 Flash (primary) → Groq Llama 3.3 70B (fallback) via `src/llm_client.py`
- **Run:** `streamlit run dashboard.py` → http://localhost:8501
- **Tests:** `python -m pytest tests/ --ignore=tests/test_new_apis.py --ignore=tests/test_ibkr_connection.py --ignore=tests/test_ibkr_worker_once.py` → 269 passed, 4 pre-existing failures (bars_ago × 3, fill_callback × 1)

---

## Architecture

### Entry Points
- `dashboard.py` — Streamlit router, 11 pages (Scan, Research, Watchlist, Market, News Impact, Squeeze, Catalyst, Options Flow, Backtest, History, Scheduler)
- `scheduler.py` — Background jobs + price monitor daemon thread

### Pages (`_pages_modules/`)
| File | Purpose |
|---|---|
| `page_scan.py` | Multi-factor scan + DCF column |
| `page_research.py` | Deep Dive + Side-by-Side Compare |
| `page_watchlist.py` | Watchlist + Portfolio + Price Target |
| `page_market.py` | Indices + Sector Heatmap + Earnings |
| `page_news_impact.py` | Article Analysis + Stock News + Upcoming Events |
| `page_squeeze.py` | Short Squeeze Scanner |
| `page_backtest.py` | Signal accuracy validation |
| `page_history.py` | Score trend per ticker |
| `page_scheduler.py` | Scheduler config + manual send buttons |

### Core Modules (`src/`)
| File | Purpose |
|---|---|
| `stock_scorer.py` | Scoring engine 0–100, includes DCF |
| `dcf_valuation.py` | DCF engine (5-year FCF model) |
| `squeeze_scanner.py` | Squeeze Score + AI Verdict |
| `borrow_fee.py` | Finviz scraper for borrow fee estimate |
| `price_alert_monitor.py` | Supertrend 15m/1h/daily + price target + volume spike — daemon thread |
| `telegram_news_digest.py` | Market digest + Portfolio news |
| `database.py` | SQLite CRUD + auto migration |
| `watchlist_manager.py` | Alert logic — score threshold, price levels, portfolio stop/target, score delta. `price_change` gated to ET 04:00–20:00 (`zoneinfo`) |
| `score_alert.py` | Score jump/drop alerts for ALL scanned tickers (not just watchlist) — 24h cooldown, shared alert types with watchlist_manager |
| `llm_client.py` | Gemini → Groq fallback. `_try_groq()` wrapped in try/except — Groq errors raise `RuntimeError` instead of propagating raw |
| `market_feed.py` | Live indices + macro events. `get_upcoming_macro()` returns approximate weekly schedule — events marked `*` as disclaimer |
| `news_impact_analyzer.py` | 3-layer LLM news analysis |
| `macro_signals.py` | Macro signals |
| `telegram_notifier.py` | Telegram send logic — 4000-char truncation guard |
| `scan_worker.py` | Background scan thread |
| `index_loader.py` | iShares index/sector loader — falls back to Wikipedia for S&P 500 when iShares returns HTML; CACHE_TTL=30d |
| `catalyst_scanner.py` | Catalyst Scanner engine — explosion score, PDUFA, unusual options |
| `options_flow.py` | Options chain data, PCR, unusual call/put activity (yfinance). **OI=0 false positive fixed** — contracts with OI=0 and volume<500 are skipped; volume≥500 uses `volume/100` ratio instead of sentinel `9999`. |
| `auto_watchlist_agent.py` | Auto-adds squeeze/catalyst/momentum candidates to watchlist with Telegram summary. **`alert_score` uses `AUTO_WL_SCORE_ENTRY` (70) from hysteresis.py** — consistent with all other auto-watchlist entry thresholds (was 60 from config). |
| `ibkr_realtime.py` | IB Gateway connector via `ib_async` — historical bars + live snapshot + bracket order placement + position/account queries for US stocks |
| `ibkr_worker.py` | Standalone daemon (Python 3.13, `.venv313`) — runs Supertrend(1H) every 5 min on the monitoring queue, fires combined alerts + submits orders via `order_manager` + syncs positions/daily P&L via `position_tracker`. **`sync_positions()` runs at the START of each cycle** (before ticker loop). **`bars_ago != 1` check** in `_check_ticker()` — only fires on the exact bar that flipped, preventing stale-flip duplicates. **`_is_signal_hours()` gate** — signals blocked outside 04:00–20:00 ET (`zoneinfo`), preventing pre-market/overnight order submission. Subscribes to `ib.orderStatusEvent` for fill/cancel callbacks. Startup reconciliation + periodic fill sweep (every 30 min). Hosts `TelegramCommandHandler` thread. **Windows named mutex singleton** (`Global\FinancialAgent_IBKRWorker_Singleton`) — `_acquire_singleton_lock()` in `main()` prevents two worker instances; second instance exits with code 1 immediately. `multiprocessing.freeze_support()` called in `__main__` to prevent Windows spawn-mode double-execution. **`_update_order_log()` race fix** — FILLED status uses `NOT IN ('FILLED','ERROR')` guard; CANCELLED uses `= 'SUBMITTED'` guard; prevents bracket-order child-leg cancel from overwriting a FILLED status. |
| `monitoring_queue.py` | Source of truth for "which tickers get real-time IBKR monitoring" — scanner score ≥ 65 + manual watchlist + recent BUY alerts (72h) + liquidity gate (hysteresis: enter $5M / exit $3M ADV). Queue state persisted to `monitoring_queue_snapshot` DB table. **`_persist_queue()` only called when `apply_liquidity_gate=True`** — prevents `signal_combiner.evaluate()` calls (gate=False) from corrupting the snapshot with unfiltered tickers. |
| `order_manager.py` | Wraps IBKR order calls; runs execution_engine veto checks before submission; logs every attempt to `order_log` DB table. Injects `position_tracker` into execution engine for daily loss limit. **Fetches `portfolio_tickers` from `ibkr_positions` DB before `evaluate_trade()`** — enables sector concentration veto (Layer 6). paper_mode=True default; live requires `IBKR_LIVE=true` env var. Module-level `_trading_paused` flag — when True, `submit()` returns PAUSED without evaluating. Passes `signal_type` ("BUY"/"SELL") to `evaluate_trade()`. |
| `position_tracker.py` | Syncs IBKR positions to `ibkr_positions` DB table every 5 min; records `daily_pnl` once per day; exposes `get_current_exposure()`, `get_portfolio_value()`, `get_daily_pnl()` for execution engine. `get_portfolio_value()` DB fallback uses `ORDER BY date DESC LIMIT 1` (most recent row, not just today) — prevents returning 0.0 early morning before `record_daily_pnl()` runs. **`record_daily_pnl()` 09:30 ET gate** — skips before 09:30 ET (market open) to avoid writing a $0 row from pre-market account summary; uses `INSERT OR REPLACE` (was `INSERT OR IGNORE`) so the row is updated if re-run after the first write. |
| `signal_combiner.py` | Supertrend 1H flip → BUY/SELL alert; enforces daily cap (10), 24h dedup. **No composite score gate** — Supertrend flip is the sole trigger for both BUY and SELL (symmetric behavior, user preference 2026-06-03). Score pulled for message enrichment only. **`_try_claim_dedup()` performs SELECT+INSERT atomically in a single DB connection** — eliminates the race window of the old split check+write. `_record_dedup()` removed (was dead code). **SELL gate** — before firing a SELL alert, checks `ibkr_positions WHERE ticker = ? AND shares > 0`; suppresses SELL (no Telegram, no order) when no open position exists. |
| `forward_signals.py` | Records every fired alert with entry price + data quality check; `record_fill()` updates `fill_price`/`fill_source` from IBKR callback — **guards against CANCELLED orders** (cross-checks `order_log.status` before writing, skips if CANCELLED to prevent bracket-order race from corrupting win-rate); daily 18:00 job fills `price_after_{7,14,30}d`; weekly Friday 20:00 Telegram digest with win-rate metrics |
| `earnings_sentiment.py` | Tier 1 = Finnhub EPS surprise history (free), Tier 2 = LLM transcript analysis (paid). Score 0–5 added to `stock_scorer.py` bonus band. **EDGAR fallback**: when Finnhub returns empty, uses `edgar_fcf.get_eps_yoy_growth()` (YoY EPS% proxy, `source='edgar_eps_yoy'`) instead of returning score=0. |
| `hysteresis.py` | Central helper `passes_hysteresis(current, in_set, entry, exit)` + threshold constants (composite, SI, liquidity, watchlist score) |
| `stock_forecaster.py` | Ensemble forecaster (ARIMA/MA/ES/MLP). Constructor accepts `point_in_time: datetime` — strictly truncates input to ≤ point-in-time to prevent backtest look-ahead bias |
| `news_catalyst_monitor.py` | Background thread — checks news every N min; freshness gate skips articles older than `max_article_age_minutes` (default 45, config key `news_catalyst_max_article_age_minutes`) |
| `run_dashboard_tunnel.py` | Cloudflare Quick Tunnel launcher; sends URL on startup + daily heartbeat at 08:05 IL with health status. `_tunnel_healthy()` checks both local cloudflared metrics AND public DNS resolution — catches expired quick-tunnel URLs where cloudflared stays running but DNS is deregistered |
| `run_tunnel_watchdog.py` | Watchdog for `run_dashboard_tunnel.py` — auto-restarts on crash or clean exit, sends Telegram on startup/restart/crash. Registered as `FinancialAgentTunnelWatchdog` Windows Task. Stop with `stop_tunnel.flag` sentinel |
| `supertrend.py` | Supertrend calculation (ATR-based, Wilder EMA) — used by `ibkr_worker.py` and `price_alert_monitor.py` |
| `market_regime.py` | BULL / CAUTION / BEAR regime based on VIX thresholds (20/28) + SPY vs SMA200; used by `execution_engine.py` for position sizing and stop adjustments. **`_SPY_HISTORY = "1y"`** (~252 trading days) — computes actual SMA200, not SMA126 (was `"6mo"`, now fixed). |
| `execution_engine.py` | Trade decision engine (Layers 0–6): daily loss limit (Layer 0), hard veto, confluence check, position sizing scaled by market regime, time-of-day flag, sector exposure guard. `evaluate_trade()` accepts optional `signal_type` param — SELL with no open position is vetoed (Layer -1). **`check_hard_vetos()` accepts `signal_type`** — BEAR regime veto applies to BUY only (`signal_type != "SELL"`), allowing exits in BEAR market. |
| `momentum_scanner.py` | 5-factor momentum score: Price ROC, Relative Strength vs SPY, MA Stack, RSI zone, Volume Surge; batch yfinance download; runs every 30 min as daemon thread |
| `long_setup_scanner.py` | 5-factor long setup scanner (RSI zone, MACD crossover, Volume surge, MA alignment, Momentum); daily 09:30; auto-adds top candidates to watchlist |
| `opportunity_tracker.py` | Records every BUY signal as opportunity with T1/stop targets; daily 18:00 fills outcomes; weekly Friday 20:00 Telegram digest with win-rate |
| `alert_monitor.py` | Daily health-check agent at 09:30 — detects noisy alerts, dead threads, portfolio drawdowns >8%; sends Telegram health report. Uses `get_connection()` from `src.database` (WAL-safe). **`THREAD_TYPES` no longer includes `supertrend_intraday_flip`** (hard-removed dead code — was causing daily false-positive "thread dead" warnings). |
| `telegram_command_handler.py` | Two-way Telegram — polls `getUpdates` every 30s in background thread; commands: `/status`, `/positions`, `/pause`, `/resume`, `/cancel <TICKER>`; security: only responds to `TELEGRAM_CHAT_ID`; offset persisted to `telegram_command_state` DB table. `/status` reads queue size from `monitoring_queue_snapshot` DB and P&L from `daily_pnl` DB (no live IBKR call). `_load_offset()` returns `int(row["value"])` — was returning raw TEXT causing TypeError on `last_update_id + 1`. |
| `finnhub_client.py` | Finnhub API wrapper — earnings surprises, transcript list/content |
| `edgar_fcf.py` | SEC EDGAR XBRL provider — free, no API key. Functions: `get_edgar_fcf_median` (median of 4 annual 10-K FCF values for DCF), `get_revenue_cagr` (5yr CAGR), `get_interest_coverage` (EBIT/InterestExpense), `get_current_ratio` (AssetsCurrent/LiabilitiesCurrent), `get_eps_yoy_growth` (quarterly YoY proxy). 24h in-memory cache per ticker. Rate: 0.12s delay between requests (≤10 req/sec SEC policy). |

---

## Scoring Engine (0–100)

Base total = 145. Normalized 0–100, plus bonus band up to +20.

| Component | Weight | Notes |
|---|---|---|
| RSI | 15 | RSI >75 = 0 pts |
| MACD | 15 | |
| MA Trend | 20 | |
| Volume | 10 | |
| Momentum | 10 | |
| Forecast | 15 | ARIMA/MLP ensemble via `stock_forecaster.py` — **weight defined but `forecast_score` currently set to 0 in code** (marked `# indicative only — excluded from score`); score uses 11 components summing to 115 as `core_max` |
| Short Interest | 10 | SI% of Float |
| Institutional | 5 | |
| Insider | 5 | SEC Form 4 |
| Fundamentals | 10 | P/E, Revenue CAGR 5yr (EDGAR → yfinance fallback), Margin, Interest Coverage (EDGAR → D/E fallback) |
| DCF | 15 | Margin of Safety vs intrinsic value |
| News Sentiment | 5 | Earnings EPS surprise + LLM transcript analysis via `earnings_sentiment.py` |
| Squeeze Bonus | +15 | SI≥20% + vol spike + price up |
| Google Trends | +5 | bonus |

**Signals:** 75+ = STRONG BUY · 60–74 = BUY · 45–59 = WATCH · 35–44 = NEUTRAL · <35 = SKIP

---

## Hysteresis Bands (`src/hysteresis.py`)

All binary thresholds in the project use **entry/exit deadbands** instead of single cutoffs, to prevent thrashing on values that oscillate near the boundary. The helper:

```python
passes_hysteresis(current_value, previously_in_set, entry_thr, exit_thr) -> bool
```

Returns True if the value should be considered "in the set" given prior membership and the entry/exit thresholds.

| Threshold | Entry | Exit | Source |
|---|---|---|---|
| Auto-watchlist score | 70 | 40 | + min-hold 3 days; + 7-day re-entry cooldown unless ≥ 75 |
| Composite-for-BUY | 60 | — | **gate intentionally removed 2026-06-03** — Supertrend flip is sole trigger (symmetric BUY/SELL) |
| Squeeze SI% | 15 | 10 | filter for squeeze pool |
| Catalyst SI% | 10 | 5 | filter for catalyst pool |
| Liquidity ADV ($) | $5M | $3M | monitoring_queue gate |

**Auto-exit cooldown:** when an auto-added ticker is removed, a `watchlist_alerts` row of type `auto_exit_cooldown` is written. Re-add is blocked for 7 days unless score ≥ 75 (re-entry threshold higher than normal 70 entry).

---

## Backtest Integrity — Point-in-Time Forecasting

`src/stock_forecaster.py` is the only model that could leak future data into past-decision contexts. Constructor accepts:

```python
StockForecaster(data, point_in_time=datetime(...))   # truncates data to <= pit
```

When `point_in_time` is set, all rows after it are dropped before any model fits. Critical for any historical-replay or audit code path. **Live scanning** uses the default (`None`) — equivalent to using all available data up to now.

Caveat: `MLPRegressor.early_stopping=True` uses a shuffled validation split — not strict label leakage but suboptimal for time series. Flagged but unchanged.

---

## DB Concurrency — WAL Hardening (`src/database.py`)

The DB has two concurrent writers: `scheduler.py` (main `.venv`) and `ibkr_worker.py` (`.venv313`). Hardened with:

| PRAGMA | Value | Why |
|---|---|---|
| `journal_mode` | `WAL` | Readers don't block writers |
| `synchronous` | `FULL` | Corruption-safe on Windows Docker / network FS |
| `busy_timeout` | `30000` ms | Wait when another writer holds the lock |
| `wal_autocheckpoint` | `4000` pages | Bound WAL file to ~16 MB |
| `auto_vacuum` | `INCREMENTAL` | Reclaim space without exclusive `VACUUM` lock |

**Do NOT add `isolation_level=None`** to `get_connection()` — it silently enables autocommit and breaks every `with conn:` transactional block. Default isolation is intentionally preserved.

High-frequency writes (`save_result`, `watchlist_save_alert`, `record_signal`, `update_outcomes`) are wrapped with `@retry_on_busy` (5 attempts, exponential backoff) as defense-in-depth.

`prune_old_data` uses `PRAGMA incremental_vacuum(1000)` instead of full `VACUUM` (no exclusive lock).

Verified by `tests/test_db_wal_concurrency.py` — 4 writers + 1 reader, 1000 writes, 0 errors at 139 writes/sec aggregate.

---

## IBKR Real-Time Architecture

**Stack split (because `ib_async` is incompatible with Python 3.14):**
- Main project — Python 3.14, `.venv`
- IBKR worker only — Python 3.13, `.venv313`

**Process layout:**
```
┌─────────────────────┐      ┌──────────────────────┐
│ run_scheduler_      │      │ run_ibkr_worker_     │
│ watchdog (pythonw)  │      │ watchdog (pythonw)   │
└─────────┬───────────┘      └──────────┬───────────┘
          │ spawns                       │ spawns
          ▼                              ▼
┌─────────────────────┐      ┌──────────────────────┐
│ scheduler.py        │      │ src.ibkr_worker      │
│ .venv (Py 3.14)     │◄────►│ .venv313 (Py 3.13)   │
│ All scoring/alerts  │ DB   │ Supertrend(1H) loop  │
└─────────────────────┘      │ + orderStatus cb     │
                              │ + TelegramCmdHandler │
                              └──────────┬───────────┘
                                        │
                                        ▼
                              ┌──────────────────────┐
                              │ Docker IB Gateway    │
                              │ paper port 4002      │
                              └──────────────────────┘
```

Both watchdogs use `CREATE_NO_WINDOW` flag — no CMD windows appear. Registered as Windows Scheduled Tasks: `FinancialAgentWatchdog`, `FinancialAgentIBKRWorker`.

**⚠️ Python Launcher trap (fixed 2026-06-25):** On Windows, `.venv313\Scripts\python.exe` is NOT the real Python 3.13 interpreter — it is `py.exe` (the Windows Python Launcher, ~249 KB). The launcher always spawns the real interpreter as a child process, causing **two processes** to appear for every worker invocation. The fix: `run_ibkr_worker_watchdog.py` reads `pyvenv.cfg` to find the base interpreter (`executable = C:\...\Python313\python.exe`) and invokes it directly, activating the venv via env vars instead of relying on the launcher:
```python
env["VIRTUAL_ENV"] = str(VENV313_DIR)
env["PATH"] = venv_scripts + os.pathsep + env["PATH"]
env["__PYVENV_LAUNCHER__"] = str(VENV313_DIR / "Scripts" / "python.exe")  # tells base Python which venv's pyvenv.cfg to load
env.pop("PYTHONHOME", None)
```
**Do NOT change `PYTHON` back to `VENV313_DIR / "Scripts" / "python.exe"` — that reverts the two-process bug.**

**Orphan worker prevention:** watchdog writes `ibkr_worker.pid` after `Popen()` and deletes it after `proc.wait()`. On the next watchdog start, `_kill_orphaned_worker()` reads the PID file and calls `TerminateProcess()` on any leftover worker from a previous watchdog crash (Windows does not kill children when parent exits).

**Gateway settings persistence:** `/home/trader/Jts` is mounted via a **named Docker volume** `ibkr_jts` (declared in `docker-compose.yaml`). API settings (Trusted IPs `172.18.0.1`, "Allow connections from localhost only" unchecked, "Read-Only API" unchecked) survive `docker-compose down`/`up` and host restarts. Settings auto-persist when you click OK on the Configure dialog — no explicit Save needed.

```yaml
# docker-compose.yaml — bottom of file
volumes:
  ibkr_jts:    # preserves /home/trader/Jts across container restarts
```

First-time setup (after adding the volume): start container → VNC → Configure → Settings → API → Settings → toggle the 3 options → OK. Done — volume persists thereafter.

---

## IBKR Order Execution (added 2026-05-29)

**Flow:** `ibkr_worker` detects Supertrend flip → `signal_combiner.evaluate()` fires alert → Telegram sent → `order_manager.submit()` called. Fill/cancel callbacks fire asynchronously via `orderStatusEvent`.

```
run_once():
  position_tracker.sync_positions()              ← FIRST: fresh ibkr_positions for veto checks
  for each ticker in queue:
    signal_combiner.evaluate()
      → order_manager.submit(alert)
          → if _trading_paused: return PAUSED          ← Telegram /pause blocks all orders
          → engine.set_position_tracker(tracker)       ← injects tracker for daily loss check
          → engine.evaluate_trade(signal_type=action)
              → Layer -1: SELL veto if exposure==0     ← no open position to sell (reads ibkr_positions)
              → Layer 0: check_daily_loss_limit()      ← daily P&L vs portfolio value
              → Layer 2–6: hard veto → confluence → sizing → noise → sector
          → ibkr_realtime.place_bracket_order()        ← LMT entry + STP stop + LMT target
          → _format_submitted_message()                ← rich BUY/SELL message with P&L
  position_tracker.record_daily_pnl()            ← write daily_pnl row (once/day)

  (async) ib.orderStatusEvent → _on_order_status()
      → Filled:    _update_order_log(FILLED) + forward_signals.record_fill() + Telegram
      → Cancelled:  _update_order_log(CANCELLED)
      → Inactive:   _update_order_log(ERROR)
```

**OrderManager** (`src/order_manager.py`):
- Accepts optional `position_tracker` param; injects into execution engine via `set_position_tracker()`
- **First check**: if `_trading_paused` is True → returns `{status: "PAUSED"}`, logs to `order_log`, skips all evaluation
- Calls `execution_engine.evaluate_trade()` with latest `scan_results` data
- If vetoed → logs to `order_log` table with status=VETOED, sends Telegram veto message
- If approved → calls `ibkr_realtime.place_bracket_order()` with sizing from execution engine
- Logs every attempt to `order_log` DB table (SUBMITTED/VETOED/FILLED/CANCELLED/ERROR/PAUSED)
- `set_paused(bool)` / `is_paused()` — module-level functions, toggled by Telegram `/pause` and `/resume`

**PositionTracker** (`src/position_tracker.py`):
- `sync_positions()` — calls `ibkr_realtime.get_positions()`, upserts to `ibkr_positions` table, removes closed positions
- `record_daily_pnl()` — writes one `daily_pnl` row per calendar day (skips if already recorded)
- `get_current_exposure(ticker)` — returns market_value from DB (0.0 if no position)
- `get_portfolio_value()` — tries IBKR first; DB fallback queries `ORDER BY date DESC LIMIT 1` (most recent day, avoids 0.0 early morning)
- `get_daily_pnl()` — tries IBKR first; falls back to `daily_pnl` DB table

**Safety:**
- `paper_mode=True` always unless `IBKR_LIVE=true` env var is explicitly set
- Paper port 4002 is the default; live port 4001 requires both `paper_mode=False` AND env flag
- All execution engine vetos (daily loss limit, liquidity, R:R, gap-down, BEAR regime, sector concentration) are enforced before any order touches IBKR
- **BEAR regime veto is BUY-only** — `check_hard_vetos(signal_type="SELL")` passes through in BEAR market to allow exits
- Daily loss limit: `max_daily_loss_pct` from `scheduler_config.json` (default 2%); if `position_tracker` is not injected, veto is skipped with WARNING log

**Bracket order structure:**
- Parent: LMT order at entry price (transmit=False)
- Child 1: STP order at stop price (transmit=False)
- Child 2: LMT order at target price (transmit=True — triggers full bracket transmission)

**Fill callback (added 2026-05-29):** `ibkr_worker.py` subscribes to `ib.orderStatusEvent` each cycle:
- `Filled` → `_update_order_log(order_id, "FILLED", fill_price)` + `forward_signals.record_fill(ticker, fill_price, order_id)` + Telegram "💰 ORDER FILLED" message
- `Cancelled` → `_update_order_log(order_id, "CANCELLED")`
- `Inactive` / `ApiCancelled` → `_update_order_log(order_id, "ERROR", notes=status_str)`

`record_fill()` in `forward_signals.py` finds the most recent row for the ticker where `fill_price IS NULL` and `data_quality_flag != 'SUSPECT'`, then sets `fill_price` and `fill_source='IBKR_CALLBACK'`.

**Startup order reconciliation (added 2026-05-29):** `_reconcile_orders_on_startup()` in `ibkr_worker.py` — called once in `main()` after `init_db()`, before the main polling loop. Opens a dedicated IBKR connection, fetches open orders, and queries `order_log` for all SUBMITTED rows. Any SUBMITTED row whose `ibkr_order_id` is not found in IBKR's open orders is marked `status=ERROR, notes="Not found on reconnect"`. Rows still live on IBKR are left as SUBMITTED. Logs reconciliation summary. Wrapped in try/except so failures don't prevent the main loop from starting.

**Periodic fill sweep (added 2026-05-29):** `_periodic_fill_sweep()` in `ibkr_worker.py` — runs at most every 30 min (`FILL_SWEEP_INTERVAL_SECS`), called at the end of `run_once()` after position sync. Catches fills missed by `orderStatusEvent` callbacks (e.g., due to disconnect or TWS restart). Steps:
1. Query `order_log` for SUBMITTED rows older than 5 min (`FILL_SWEEP_MIN_AGE_SECS`)
2. Fetch IBKR open orders via `get_open_orders()`
3. Fetch session fills via `ib.fills()` (ib_async in-memory fill list)
4. For each stale SUBMITTED row: if `ibkr_order_id` still in open orders → skip; if in fills → mark FILLED + call `record_fill()` to update `forward_signals`; if gone from both → mark ERROR
5. Logs sweep results (N filled, N errored, N unchanged)

Module-level `_last_fill_sweep_ts` tracks when the last sweep ran.

**Two-Way Telegram (added 2026-05-29):** `src/telegram_command_handler.py` — background thread inside `ibkr_worker`, polls Telegram `getUpdates` every 30s.

| Command | Action |
|---|---|
| `/status` | Regime, monitoring queue size, open positions, daily P&L, paused state, last signal |
| `/positions` | Table of open IBKR positions (ticker, shares, avg_cost, unrealized P&L) |
| `/pause` | Sets `order_manager._trading_paused=True` — all `submit()` calls return PAUSED |
| `/resume` | Sets `order_manager._trading_paused=False` — normal order flow resumes |
| `/cancel <TICKER>` | Cancels all open IBKR orders for the specified ticker |

Security: only responds to messages from `TELEGRAM_CHAT_ID` (same chat used for outbound alerts). Unknown senders are silently ignored. `last_update_id` persisted to `telegram_command_state` DB table to avoid reprocessing old messages on restart.

---

## Forward Signal Validation

Every fired BUY/SELL alert is recorded in `forward_signals` with `entry_price` at signal time. A daily 18:00 job (`run_forward_outcomes_update` in scheduler) backfills `price_after_{7,14,30}d` and `return_{7,14,30}d_pct` once horizons mature. Weekly Friday 20:00 job (`run_forward_digest`) sends a Telegram summary:

```
📊 Weekly Forward Signals Digest (7d)
Total signals: N
Breakdown: BUY=X, SELL=Y
Avg 7D return: ±X.XX%
Win rate 7D:   XX.X%
```

This is the project's primary **forward-paper-trading** validation channel — replaces traditional historical backtest as the source of truth for tuning thresholds.

**Data quality guard (added 2026-05-29):** `record_signal()` now runs `_check_entry_price_plausibility()` before inserting. It flags `data_quality_flag='SUSPECT'` when:
- `entry_price == 105.0` (known IBKR paper-account placeholder), or
- entry price diverges >20% from the most recent `scan_results.price` for the same ticker within 24h.

Early signals (2026-05-18/19) for PLUG and ZETA had `entry_price=105.0` — both rows were manually corrected. The guard prevents future occurrences from silently corrupting win-rate calculations.

---

## Catalyst Scanner (`src/catalyst_scanner.py`)

### Explosion Score (0–100)

| Component | Max pts | Notes |
|---|---|---|
| Urgency | 30 | Today=30 · 1d=27 · 3d=17 · 7d=8 · 14d+=4 |
| SI% Fuel | 25 | ≥20%=25 · ≥15%=18 · ≥10%=11 · ≥5%=5 |
| Float Amplifier | 20 | ≤5M=20 · ≤15M=16 · ≤40M=11 · ≤100M=6 |
| Volume Building | 10 | ≥3x=10 · ≥2x=7 · ≥1.5x=4 |
| Insider Buying | 10 | SEC Form 4 net buying 90d |
| Momentum | 5 | 5-day price change |
| Unusual Options | +8 | Unusual CALL vol/OI≥3x; +4 if PCR<0.7 |

**Labels:** ≥70=HIGH · 50–69=MEDIUM · 30–49=LOW · <30=WATCH

### Catalyst Types
- `earnings` — Nasdaq API earnings calendar
- `analyst` — Finnhub upgrades (requires FINNHUB_API_KEY)
- `sec_8k` — EDGAR 8-K filings, 8 parallel workers
- `pdufa` — BioPharma Catalyst FDA calendar (no key; cache 6h → `data/pdufa_cache.json`)

### Source Modes
- **Nasdaq Calendar** — all upcoming earnings reporters
- **Watchlist + Portfolio** — your tickers (shows placeholders when no catalyst found)
- **Manual Tickers** — comma-separated list
- **Index / Sector** — iShares indices. For biotech: Russell 2000 → Health Care (~150 tickers)

### Unusual Options Signal (`_unusual_options_pts`)
Reuses `src/options_flow.py → get_options_summary()` (yfinance).
- +8 pts if unusual CALL contracts (vol/OI ≥ 3x or vol ≥ 5000)
- +4 pts if PCR < 0.7 (bullish sentiment) and no unusual calls
- Returns 0 on any failure — many small caps have no options data

---

## DCF Engine (`src/dcf_valuation.py`)

```
Enterprise Value = Σ FCF_t/(1+WACC)^t  +  TV/(1+WACC)^n
Terminal Value   = FCF_n*(1+g) / (WACC-g)
Equity Value     = Enterprise Value − Net Debt   ← net debt subtraction (critical)
Intrinsic/share  = Equity Value / sharesOutstanding
Margin of Safety = (Intrinsic − Price) / Intrinsic * 100
```

**FCF source priority (tiered):**
1. SEC EDGAR XBRL — median of last 4 annual 10-K values (`edgar_fcf.get_edgar_fcf_median`) — audited, free
2. yfinance cashflow DataFrame "Free Cash Flow" row — multi-year median of positive years
3. yfinance `info.freeCashflow` — TTM single value
4. `operatingCashflow − |capitalExpenditures|`

**WACC:**
- Cost of equity: CAPM `Ke = Rf (10Y Treasury ^TNX) + Beta × 5.5% ERP` (Damodaran); clamped 7%–20%; fallback 10%
- Cost of debt: `interestExpense / totalDebt` (actual); falls back to tier estimate (5%/6%/8% by D/E)
- `WACC = E/(D+E)×Ke + D/(D+E)×Kd×(1−tax)`; clamped 7%–15%
- Note: higher leverage **lowers** WACC (debt cheaper than equity after tax shield); equity impact captured by net debt subtraction

**Other:**
- Growth: blend of historical FCF CAGR (60%) + revenue/earnings proxy (40%); clamped −10%–25%
- Financial sector exclusion: `sector in ("Financial Services", "Banks", "Insurance")` → returns None (fall through to P/S)
- Over-leveraged exclusion: `equity_value ≤ 0` → returns None
- Terminal growth: 2.5%, Horizon: 5 years
- Return dict includes: `fcf_source`, `fcf_used_m`, `cost_of_equity_pct`, `cost_of_debt_pct`, `net_debt_m`

---

## DB Schema (`data/financial_agent.db`)

```
watchlist:                    ticker, added_at, notes, alert_score, alert_pct,
                              price_above, price_below, price_target, volume_spike_x, supertrend_alert
portfolio:                    ticker, added_at, entry_price, shares, notes, stop_loss, target_price
watchlist_alerts:             ticker, alert_type, message, sent_at, score, price
scan_results:                 ...raw_data (JSON including dcf dict)
alert_trades:                 ticker, entry_alert_type, entry_price, entry_time, hold_days_min,
                              hold_days_max, exit_price, exit_time, exit_reason, exit_alert_type,
                              pnl_pct, status (open/closed)
forward_signals:              ticker, signal_ts, signal_type, entry_price, composite_score,
                              catalyst_summary, supertrend_level, supertrend_atr, ai_verdict,
                              telegram_sent_at, price_after_{7,14,30}d, return_{7,14,30}d_pct,
                              status (open/matured), data_quality_flag, fill_price, fill_source
monitoring_queue_snapshot:    ticker, saved_at — persists accepted monitoring queue across restarts
ibkr_positions:               ticker (PK), shares, avg_cost, unrealized_pnl, market_value, last_synced
                              — synced every 5 min from IBKR via position_tracker
daily_pnl:                    date (PK), day_pnl, net_liquidation, recorded_at
                              — one row per calendar day, written by position_tracker
order_log:                    ticker, action, shares, entry_price, stop_price, target_price,
                              status (SUBMITTED/VETOED/FILLED/CANCELLED/ERROR/PAUSED), fill_price,
                              ibkr_order_id, created_at, updated_at, notes
telegram_command_state:       key (PK), value — persists Telegram getUpdates offset across restarts
```

Migration via `_migrate()` in `database.py` — adds columns without breaking data.

`watchlist_alerts` doubles as a **cooldown registry** — all alert systems check this table via `_alert_sent_recently(ticker, alert_type, hours)` before sending.

---

## Scheduler Jobs (`scheduler.py`)

> **Times below are from `scheduler_config.json` — NOT from code defaults.** The code in `scheduler.py` has its own fallback values (e.g. `["08:30", "16:30"]` for scans) but the config file overrides them. Always check `scheduler_config.json` for the actual runtime schedule.

| Job | Default Time | Function |
|---|---|---|
| Watchlist Cleanup | 08:00 | `run_watchlist_cleanup()` |
| Catalyst+SI Alert | 08:05 | `run_catalyst_alert()` |
| Portfolio News | 08:30 | `run_portfolio_news()` |
| Scan + Auto-Watchlist + Breakout | 08:30, 15:00 (**from `scheduler_config.json` `"times"`**, code fallback is `["08:30", "16:30"]`) | `run_scan()` |
| Portfolio | 09:15 | `run_portfolio_scan()` |
| Market Digest | 09:30 | `run_market_digest()` |
| Long Setups | 09:30 | `run_long_setups()` — configurable `long_setups_time`; guarded by `long_setups_enabled` |
| Alert Monitor Health Check | 09:30 | `run_alert_monitor()` — noisy alerts, dead threads, portfolio drawdown >8% |
| Watchlist | 12:00 | `run_watchlist_scan()` |
| Squeeze + SI Alert | 12:00 | `run_squeeze_scan()` |
| Weekly Rotation | Monday 08:15 | `run_weekly_rotation()` — replaces low-scoring auto-added tickers |
| Forward Outcomes Update | 18:00 daily | `run_forward_outcomes_update()` — fills `price_after_{7,14,30}d` |
| Opportunity Outcomes Update | 18:00 daily | `run_opportunity_outcomes()` — fills T1/stop hit status |
| Forward Signals Digest | Friday 20:00 | `run_forward_digest()` — Telegram win-rate summary |
| Opportunity Digest | Friday 20:00 | `run_opportunity_digest()` — Telegram T1/stop hit summary |
| Price Monitor + Supertrend | every 5 min (thread) | `_price_monitor_thread()` |
| Momentum Monitor | every 30 min (thread) | `_momentum_monitor_thread()` — scans `momentum_indices`, auto-adds via `auto_watchlist_agent` |
| News Catalyst Monitor | every 15 min (thread) | `catalyst_monitor_thread()` — configurable `news_catalyst_max_article_age_minutes` (default 45) |

Config: `scheduler_config.json`

### Auto-Watchlist (`run_scan`)
After every scan, any ticker with score ≥ 70 not already in the watchlist is auto-added:
- `alert_score=70`, `alert_pct=5.0`
- Notes: `"Auto: score {N} on {date}"`
- One Telegram summary sent with all added tickers
- **Immediate cooldown suppression**: after auto-add, `score_threshold` + `price_change` cooldown records are written to `watchlist_alerts` so the next watchlist scan (12:00) does not re-fire BUY alerts for the same stocks
- Controlled by `"auto_watchlist": true` in `scheduler_config.json`

### Auto-Exit (`run_scan` + `run_watchlist_scan`)
Auto-added tickers (notes prefix: `"Auto:"`) with **score ≤ 40** are removed from the watchlist, but only after a **minimum hold of 3 days** (`AUTO_WL_MIN_HOLD_DAYS`) since `added_at`. This prevents same-day add→exit thrash on noisy boundary tickers.
- `run_scan` (08:30/15:00) removes during scan, writes `auto_exit_score` cooldown + `auto_exit_cooldown` row
- `run_watchlist_scan` (12:00) checks `_alert_sent_recently("auto_exit_score", hours=12)` to avoid double-removal
- **Batched notification**: one message lists all removed tickers, not one per ticker
- **Re-entry block**: after auto-exit, an `auto_exit_cooldown` row is written to `watchlist_alerts`. For **7 days** (`AUTO_EXIT_COOLDOWN_DAYS`) the ticker may not be re-added from any auto-watchlist source unless its score is ≥ **75** (`AUTO_WL_REENTRY_SCORE`). All three constants live in `src/hysteresis.py`.
- **Transaction order hardened** (both `run_scan` and `run_watchlist_scan`): cooldown rows written **BEFORE** `watchlist_remove()` — if remove fails, cooldown still blocks re-add.

### Squeeze Scan (`run_squeeze_scan`) — **1 combined message**
Sends a **single** Telegram per run with two sections:
1. `🚨 High SI+DTC Alert` — tickers with SI > 15% AND DTC > 10 (cooldown 24h, saved as `squeeze_si_alert`)
2. `🔥 Top Squeeze Candidates` — top 10 by score from full scan
- If no High SI alerts, only the Top Candidates section is shown

### Catalyst + High-SI Alert (`run_catalyst_alert`) — **1 combined message**
Daily scan via `scan_catalysts(days_ahead=7, types=["earnings","pdufa","sec_8k"])`.
Filters: `SI ≥ 10% AND event ≤ 7 days AND price ≥ $5.00 AND explosion_score ≥ 40`.
All top 5 combined into **one** Telegram message.
- Each ticker block includes event details + `🎯 Action:` guidance by explosion_score tier
- Cooldown: 24h per ticker (`alert_type = "catalyst_si_alert"`)
- **Bug fixed**: `si_pct` was incorrectly multiplied ×100 (showing 1500% instead of 15%); filter was `>= 0.10` instead of `>= 10` — both corrected
- Controlled by `"catalyst_alert_time"` in config (default `"08:05"`)

### Breakout Alert (`_check_breakout` in `run_scan`)
Runs inside `run_scan()` for every ticker with score ≥ 65.
- **52w High break**: `price > max(1y_history['Close'][:-1])`
- **Bollinger Upper break**: `price > 20d_SMA + 2×std`
- Triggers if either condition is true → saved to `watchlist_alerts` (**Telegram suppressed** — superseded by `combined_buy` which fires at the actual breakout candle via IBKR real-time; scan-time breakouts are structurally late on prior-close data)
- Cooldown: 24h per ticker (`alert_type = "breakout_alert"`)
- **Trade plan**: `_check_breakout()` does NOT call `format_trade_plan_block` — `run_scan()` appends the execution engine block (`evaluate_trade` → `format_trade_alert`). Only one trade plan per message; calling both produced contradictory stop/target values.

```
🚀 BREAKOUT — {ticker}
52w High: ✅/❌ | Bollinger: ✅/❌
Price: $XX.XX | Score: YY
🎯 Momentum entry — buy breakout with stop below $XX.XX (-8%).
```

### Watchlist TTL Cleanup (`run_watchlist_cleanup`)
Runs daily at 08:00. Only targets auto-added tickers (`notes` starts with `"Auto:"`).
- Fetches last 3 scan scores via `get_recent_scan_scores(ticker, limit=3)` in `database.py:289`
- If all 3 scores < 50 → removed from watchlist
- One Telegram summary: `"🧹 Watchlist cleanup: removed N tickers: ..."`

### Supertrend — 3 Timeframes (`price_alert_monitor.py` only)
`check_supertrend_flips()` runs on **all watchlist tickers** every 5 min (daemon thread). The `supertrend_alert` column on the watchlist row is no longer used — all tickers are always checked.

| Timeframe | History | Cooldown | Alert type |
|---|---|---|---|
| 15m (intraday) | 5d | 1h | `supertrend_intraday_flip` — **silenced (DB-only)** |
| 1h | 10d | 2h | `supertrend_1h_flip` — **silenced (DB-only)** |
| 1d (daily) | 60d | 4h | `supertrend_flip` — **silenced (DB-only)** |

ATR uses Wilder's EMA (`ewm(alpha=1/period)`) — identical to TradingView Pine Script.

> **Note:** All Supertrend timeframes in `price_alert_monitor.py` are DB-log-only (silenced from Telegram). Real-time Telegram alerts go via `ibkr_worker` → `signal_combiner` → `combined_buy/sell`. `ibkr_worker._check_ticker()` enforces **`bars_ago == 1`** — only the flip on the immediately preceding bar fires an event; stale flips from older bars are discarded.

### Score Jump/Drop Alerts (`src/score_alert.py`)
Fires for **all scanned tickers** (not just watchlist) when score delta ≥ 15 pts.
- Uses **same alert types** as `watchlist_manager.py` (`score_delta_rise` / `score_delta_drop`) → shared 24h cooldown, no duplicates
- Includes `🎯 Action:` guidance based on resulting score level

### Cooldown Helper
```python
_alert_sent_recently(ticker, alert_type, hours=24) -> bool  # scheduler.py
_cooldown_passed(ticker, alert_type) -> bool                 # watchlist_manager.py
```
Both check `watchlist_alerts` table. All alert systems write to this table after sending — used by every component to suppress duplicates.

### Telegram Truncation Guard
`TelegramNotifier.send_message()` enforces a 4000-char limit (Telegram max is 4096). Messages exceeding this are trimmed with `…[truncated]` appended and a warning logged.

---

## Alert Types (21)

All alerts include a `🎯 Action:` line with actionable guidance.

| Type | Trigger | Cooldown | Source |
|---|---|---|---|
| `combined_buy` | Supertrend 1H bullish flip + composite ≥ 60 (or hold ≥ 50 with recent BUY) + monitoring queue membership | 24h | `ibkr_worker` → `signal_combiner` |
| `combined_sell` | Supertrend 1H bearish flip + monitoring queue membership | 24h | `ibkr_worker` → `signal_combiner` |
| `score_threshold` | score ≥ alert_score (crossing, not while above) | 24h | `watchlist_manager.py` |
| `price_change` | price moved ≥ alert_pct% from baseline; **gated to ET 04:00–20:00** (pre-market open → AH close) — outside window baseline is still updated, alert is suppressed | 24h | `watchlist_manager.py` |
| `price_target` | price within $0.05 of target | 4h | `price_alert_monitor.py` |
| `price_surge_rescore` | watchlist ticker moves >10% since last recorded baseline; rescores via `score_stock()`; Telegram if score ≥ 55; gated to 09:30–16:00 ET | 2h | `price_alert_monitor.py` |
| `price_above` | price crossed above level | 24h | `watchlist_manager.py` |
| `price_below` | price dropped below level | 24h | `watchlist_manager.py` |
| `score_delta_rise` | score jumped ≥ 15 pts | 24h | `watchlist_manager.py` + `score_alert.py` (shared type) |
| `score_delta_drop` | score dropped ≥ 15 pts | 24h | `watchlist_manager.py` + `score_alert.py` (shared type) |
| `stop_loss` | portfolio: price ≤ stop_loss | 24h | `watchlist_manager.py` |
| `target_hit` | portfolio: price ≥ target_price | 24h | `watchlist_manager.py` |
| `score_drop` | portfolio: score < 35 | 24h | `watchlist_manager.py` |
| `squeeze_si_alert` | SI > 15% AND DTC > 10 | 24h | `scheduler.py` |
| `catalyst_si_alert` | SI ≥ 10% + catalyst ≤ 7 days + explosion_score ≥ 40 | 24h | `scheduler.py` |
| `breakout_alert` | score ≥ 65 + 52w high OR Bollinger upper break | 24h | `scheduler.py` |
| `supertrend_intraday_flip` | Supertrend flip on 15m bars | 1h | `price_alert_monitor.py` |
| `supertrend_1h_flip` | Supertrend flip on 1h bars | 2h | `price_alert_monitor.py` |
| `supertrend_flip` | Supertrend flip on daily bars | 4h | `price_alert_monitor.py` |
| `auto_exit_score` | auto-added ticker score ≤ 40 AND held ≥ 3 days | 12h | `scheduler.py` (dedup between run_scan + run_watchlist_scan) |
| `auto_exit_cooldown` | written on auto-exit | blocks re-add for 7 days unless score ≥ 75 | `scheduler.py` + `auto_watchlist_agent.py` |

### Alert Code Notes
- `price_above` / `price_below` — unified loop in `watchlist_manager.py`
- `score_delta_rise` / `score_delta_drop` — written by both `watchlist_manager.py` (`_send_score_delta_alert()`) and `score_alert.py`; shared cooldown type prevents double-fire
- `score_threshold` + `price_change` — also written as **suppression records** immediately after auto-watchlist add, to prevent the 12:00 watchlist scan from re-alerting the same stocks that were just added at 09:20

---

## HTML Rendering Rule

All multiline HTML must go through `_html()` before `st.markdown(unsafe_allow_html=True)`:
```python
def _html(raw: str) -> str:
    return " ".join(raw.split())
```

---

## Environment Variables (`.env`)

```
GROQ_API_KEY
GEMINI_API_KEY
FINNHUB_API_KEY
ALPHA_VANTAGE_API_KEY
SEC_USER_AGENT_EMAIL
TELEGRAM_ENABLED
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
IBKR_LIVE              # "true" to enable live order placement (port 4001); absent or any other value = paper mode (port 4002)
```

---

## Known Limitations

- Alpha Vantage: 25 req/day (free tier) — daily quota counter warns at 23 requests
- Google Trends: occasional 429 errors; 1-hour cache and threading.Lock() mitigate burst issues
- Borrow fee: Finviz approximation from SI% — directionally correct, not exact; circular dependency with SI% already used in squeeze score
- Price monitor: only runs when Scheduler is active
- DCF: returns None for loss-making companies (no positive FCF), financial sector (banks/insurance — FCFF invalid), or over-leveraged (equity_value ≤ 0). Falls through to P/S valuation.
- Backtest: requires at least 1 week of scan data in DB. `price_at_signal` is now fetched from yfinance on the same auto-adjust basis as `price_after` — corporate actions (splits, spinoffs) no longer produce spurious returns.
- Insider tracker: slow (~4–7s); EDGAR calls now have `timeout=15` to avoid indefinite hangs
- PDUFA scraper: depends on BioPharma Catalyst HTML structure — returns `[]` gracefully on failure
- Unusual Options: yfinance options data unavailable for many small caps → returns 0 pts silently
- Price sanity check in scorer (3× median threshold) catches API glitches but won't catch modest bad data
- `get_upcoming_macro()` in `market_feed.py` shows approximate weekly schedule (events marked `*`) — not a live economic calendar
- `ibkr_realtime.get_positions()` `market_value` is initially cost-basis estimate; corrected by `portfolio()` enrichment — enrichment failure logged as WARNING but doesn't block SELL veto (cost-basis > 0 for any open position)
- `stock_scorer.py` `Forecast` weight (15) is defined but `forecast_score=0` in code — the weight is inactive

---

## Telegram Message Map (all sends, by morning order)

| Time | Message | Source | Msgs |
|---|---|---|---|
| 08:00 | Watchlist Cleanup summary (removed auto tickers) | `run_watchlist_cleanup` | 0–1 |
| 08:05 | Catalyst + High-SI (up to 5 tickers, combined) | `run_catalyst_alert` | 0–1 |
| 08:05 | Cloudflare URL heartbeat (daily reminder + health check) | `run_dashboard_tunnel.py` | 1 |
| 08:30 | Portfolio News | `run_portfolio_news` | 1 |
| 08:30 | Auto-exit batch (if any auto-tickers scored <35) | `run_scan` | 0–1 |
| 08:30 | Breakout alerts (DB-only, silenced from Telegram — superseded by `combined_buy`) | `run_scan` | — |
| 08:30 | Score jump/drop (DB-only, silenced from Telegram — superseded by `combined_buy`) | `score_alert.py` | — |
| 08:30 | Auto-added tickers summary | `run_scan` | 0–1 |
| 09:15 | Portfolio scan alerts (stop_loss, target_hit, score_drop) | `run_portfolio_scan` | 0–N |
| 09:30 | Market Digest (indices + headlines) | `run_market_digest` | 1 |
| 09:30 | Alert Monitor health report (noisy alerts / drawdown) | `run_alert_monitor` | 0–1 |
| every 5m | `combined_buy` / `combined_sell` (real-time Supertrend 1H via IBKR) + order submission result (SUBMITTED/VETOED/PAUSED) | `ibkr_worker` → `signal_combiner` → `order_manager` | 0–N |
| on fill | 💰 ORDER FILLED — ticker + fill price + order ID | `ibkr_worker` → `_on_order_status` callback | 0–N |
| on command | Reply to `/status`, `/positions`, `/pause`, `/resume`, `/cancel` | `telegram_command_handler` | 0–N |
| every 5m | Supertrend flip 15m / 1h / daily (DB-only, silenced from Telegram) | `price_alert_monitor.py` | 0–N |
| every 15m | News catalyst (LLM analysis) | `catalyst_monitor_thread` | 0–N |
| every 30m | Momentum scanner alerts (auto-add candidates) | `_momentum_monitor_thread` | 0–N |
| 12:00 | Squeeze Scan (High SI+DTC + Top 10 candidates) | `run_squeeze_scan` | 1 |
| 12:00 | Watchlist scan alerts (score_threshold, price_change, price levels, score_delta) | `run_watchlist_scan` | 0–N |
| 15:00 | Same as 08:30 (second daily scan — 09:00 ET, before US market open 09:30 ET) | `run_scan` | 0–N |
| 18:00 | (no Telegram) Forward + Opportunity outcomes backfill | scheduler | — |
| Friday 20:00 | Forward Signals Digest (win-rate 7/14/30d) | `run_forward_digest` | 1 |
| Friday 20:00 | Opportunity Digest (T1/stop hit rates) | `run_opportunity_digest` | 1 |

**All messages include `🎯 Action:` guidance. Max length: 4000 chars (truncation guard in `TelegramNotifier`).**

---

## Pending Features

- [x] LSTM/MLP for FCF% improvement — MLP neural network (sklearn 64→32) added to `stock_forecaster.py` ensemble (weight 0.20); DCF in `dcf_valuation.py` now uses 4yr historical FCF trend (60%) blended with revenue/earnings proxy (40%) via `stock.cashflow`
- [x] Earnings sentiment from Finnhub transcripts — `src/earnings_sentiment.py` (new): Tier 1 = EPS surprise history (free, last 4Q), Tier 2 = LLM transcript analysis (paid). Score 0-5 added to `stock_scorer.py` bonus. `finnhub_client.py` has `get_earnings_surprises()`, `get_earnings_transcript_list()`, `get_earnings_transcript()`
- [ ] Sector-level sub-scanning in main Scan page — currently only in Squeeze; `page_scan.py` scans all sectors uniformly
- [x] Supertrend real-time check (every 5 min) — `check_supertrend_flips()` runs on all watchlist tickers; 3 timeframes (15m/1h/daily); ATR fixed to Wilder's EMA matching TradingView
- [x] Breakout Alert in scan — `_check_breakout()` in `scheduler.py`; 52w high + Bollinger Upper; score ≥ 65; cooldown 24h
- [x] Watchlist TTL cleanup — `run_watchlist_cleanup()` in `scheduler.py`; removes auto-added tickers with 3× score < 50; runs 08:00 daily
- [x] Unified price_above/below alerts — single loop in `watchlist_manager.py:110`; unified score_delta via `_send_score_delta_alert()` helper
- [ ] Fear & Greed Index widget — `page_market.py` shows VIX text description only; no F&G data source wired in
- [ ] Weight tuning based on backtest data — `WEIGHTS` dict in `stock_scorer.py` is static; no adaptive logic
- [x] Dual-index scan universe — `run_scan()` now loads from both Russell 2000 (small-cap) AND S&P 500 (large-cap) via `scan_indices` in `scheduler_config.json`; ~946 unique tickers per scan (was ~531). `load_tickers()` in `scheduler.py` accepts `index_names: list` and deduplicates. `index_loader.py` falls back to Wikipedia for S&P 500 when iShares returns HTML.
- [ ] Russell 2000 support in main Scan page — works in Catalyst Scanner + scheduler, not wired into `page_scan.py`
- [ ] SEC 8-K item classification (1.01 bullish / 1.03 bearish) — `catalyst_scanner.py` fetches 8-K but doesn't classify by item number

### Hardening Sprint 2026-05
- [x] Look-ahead bias fix in `stock_forecaster.py` (`point_in_time` parameter strictly truncates input to ≤ PIT before any model fits)
- [x] WAL hardening in `database.py` (`journal_mode=WAL`, `synchronous=FULL`, `busy_timeout=30s`, `incremental_vacuum`, `retry_on_busy` decorator)
- [x] Hysteresis bands across all binary thresholds (entry/exit deadbands centralized in `src/hysteresis.py`)
- [x] Auto-exit 7-day cooldown (`auto_exit_cooldown` row in `watchlist_alerts` — prevents thrash-loop where a boundary ticker is added/exited/re-added daily; bypass only on score ≥ 75)
- [x] IBKR Real-Time worker in separate `.venv313` (Python 3.13 — `ib_async` incompatible with 3.14)
- [x] `forward_signals` table — measures every alert's 7/14/30-day return; weekly Telegram digest
- [x] Scan time shifted: second daily scan is **15:00** local (UTC+3) = 09:00 ET via `scheduler_config.json`. Code fallback in `scheduler.py:1051` is `["08:30", "16:30"]` but config overrides it.
- [x] `price_change` market-hours gate — `watchlist_manager.py` gates `price_change` Telegram alerts to ET 04:00–20:00 via `zoneinfo`; outside window baseline is still recorded but no Telegram fires (prevents stale pre-market/overnight alerts)
- [x] `price_surge_rescore` baseline fix — `_BASELINE_TYPES` in `price_alert_monitor.py` excludes `supertrend_triple_bull` + `supertrend_1h_flip`; those fire at momentum peaks so using them as baseline made normal retracements appear as large-% drops
- [x] News catalyst freshness filter — `news_catalyst_monitor.py` skips articles older than `max_article_age_minutes` (default 45, UI-configurable); prevents reactive "X soared after…" stale-news alerts already priced in
- [x] Cloudflare URL heartbeat — `run_dashboard_tunnel.py` sends daily 08:05 IL reminder with URL + `🟢/🔴` health status
- [x] Breakout trade plan dedup — `_check_breakout()` no longer calls `format_trade_plan_block`; only execution engine block appended by `run_scan()` → single consistent trade plan per message
- [x] Queue cliff fix — `monitoring_queue.build_queue()` third feeder: tickers with `combined_buy` in last 72h bypass `SCANNER_MIN_SCORE=65`
- [x] Queue persistence — `monitoring_queue_snapshot` DB table; loaded on startup, persisted at end of every `build_queue()` call
- [x] Cleanup cooldown race — `run_watchlist_cleanup()` inserts cooldown BEFORE delete in single transaction
- [x] Forward signal data quality guard — `_check_entry_price_plausibility()` flags IBKR placeholder (105.0) and >20% divergence from scan price; `data_quality_flag` column on `forward_signals`
- [x] IBKR bracket order placement — `ibkr_realtime.place_bracket_order()` (LMT + STP + LMT bracket), `cancel_order()`, `get_open_orders()`
- [x] Order Manager — `src/order_manager.py`: execution engine veto → bracket order → `order_log` DB table; paper_mode default
- [x] Worker order wiring — `ibkr_worker.py` calls `order_manager.submit()` after Telegram send; sends VETOED/SUBMITTED follow-up messages
- [x] Position & account tracking — `ibkr_realtime.get_positions()` / `get_account_summary()` / `get_daily_pnl()`; `src/position_tracker.py` syncs to `ibkr_positions` + `daily_pnl` DB tables every 5 min via `ibkr_worker`
- [x] Daily loss limit veto — `execution_engine.check_daily_loss_limit()` (Layer 0, before all other vetos); reads `max_daily_loss_pct` from `scheduler_config.json` (default 2%); `position_tracker` injected via `order_manager` → `set_position_tracker()`
- [x] Fill callback → `forward_signals` — `ibkr_worker` subscribes to `ib.orderStatusEvent`; on Filled: updates `order_log` status + writes real `fill_price`/`fill_source` to `forward_signals` via `record_fill()`; on Cancelled/Inactive: updates `order_log` accordingly
- [x] `order_log` status updates — Filled→FILLED (with fill_price), Cancelled→CANCELLED, Inactive/ApiCancelled→ERROR; guard clause prevents overwriting terminal statuses
- [x] Two-way Telegram commands — `src/telegram_command_handler.py`: `/status`, `/positions`, `/pause`, `/resume`, `/cancel <TICKER>`; polls `getUpdates` every 30s; offset persisted to `telegram_command_state` DB table; security: TELEGRAM_CHAT_ID only
- [x] Trading pause flag — `order_manager._trading_paused` module-level bool; `set_paused()`/`is_paused()`; when True, `submit()` returns `{status: "PAUSED"}` and logs to `order_log` with status=PAUSED; toggled via Telegram `/pause` and `/resume`
- [x] `max_daily_loss_pct` in config — added `"max_daily_loss_pct": 0.02` to `scheduler_config.json`; `execution_engine._get_max_daily_loss_pct()` already reads this key (was falling back to hardcoded 0.02)
- [x] Startup order reconciliation — `_reconcile_orders_on_startup()` in `ibkr_worker.py`; on startup, marks stale SUBMITTED `order_log` rows as ERROR if their `ibkr_order_id` is not found in IBKR open orders; prevents orphaned SUBMITTED rows from accumulating across restarts
- [x] Periodic fill sweep — `_periodic_fill_sweep()` in `ibkr_worker.py`; every 30 min, sweeps SUBMITTED rows older than 5 min against `ib.fills()` + `get_open_orders()` to catch fills missed by `orderStatusEvent` callbacks (disconnect, TWS restart); updates `order_log` and `forward_signals` accordingly
- [x] Telegram command handler `.env` fix — `telegram_command_handler.py` calls `load_dotenv(Path(__file__).parent.parent / ".env")` at module level; previously relied on the caller having already loaded env vars, silently failing in `.venv313` context
- [x] `/status` DB-only path — replaced `build_queue()` (yfinance call) and `tracker.get_daily_pnl()` (blocking IBKR call) with direct DB reads from `monitoring_queue_snapshot` and `daily_pnl`; eliminates indefinite hang when IBKR is between cycles
- [x] `position_tracker` DB fallback — `get_daily_pnl()` and `get_portfolio_value()` now try IBKR first, then silently fall back to `daily_pnl` table; `"Not connected"` ERROR log eliminated
- [x] `_reply()` response logging — `sendMessage` response is now checked; `ok=false` logged as WARNING with Telegram's description (was silently discarded)
- [x] SELL without position veto — `execution_engine.evaluate_trade()` accepts `signal_type` param; SELL is vetoed ("No open position to sell") if `_position_tracker.get_current_exposure(ticker) == 0`; runs as Layer -1 before daily loss check
- [x] Position pre-sync fix (2026-05-30) — `ibkr_worker.run_once()` now calls `tracker.sync_positions()` BEFORE the ticker loop, not after; previously the Layer -1 veto read stale `ibkr_positions` data (up to 5 min old) causing SELL orders to bypass the no-open-position veto when a position was closed between cycles (CPRI/HNST/IFF incident)
- [x] Improved SUBMITTED Telegram message — `order_manager._format_submitted_message()` builds rich BUY (entry/stop/target/cost basis/order ID) and SELL (exit/P&L vs avg cost/remaining shares/order ID) messages; `ibkr_worker` uses `result["message"]` directly
- [x] Order funnel tests — `tests/test_order_funnel.py`: 8 tests covering happy path, engine veto, daily loss limit, trading pause, paper mode gate, SELL message format, SELL-without-position veto, fill callback; uses real SQLite (tmp_path), mocked IBKR + yfinance

### Hardening Sprint 2026-06

- [x] **SMA200 fix** — `market_regime.py` `_SPY_HISTORY` changed `"6mo"` → `"1y"`; was computing SMA126 causing incorrect BULL/BEAR classification
- [x] **BEAR veto allows SELL** — `execution_engine.check_hard_vetos()` now gates only BUY (`signal_type != "SELL"`); SELL exits are always allowed in BEAR market
- [x] **`bars_ago` stale-flip guard** — `ibkr_worker._check_ticker()` rejects events where `bars_ago != 1`; prevents duplicate alerts from replaying old flips each 5-min cycle
- [x] **Atomic dedup** — `signal_combiner._try_claim_dedup()` does SELECT+INSERT in single DB connection; race window eliminated. Dead `_record_dedup()` function removed.
- [x] **portfolio_tickers in sector veto** — `order_manager.submit()` fetches live positions from `ibkr_positions` DB and passes to `evaluate_trade(portfolio_tickers=...)`; Layer 6 sector concentration now active
- [x] **Portfolio value morning fallback** — `position_tracker.get_portfolio_value()` DB fallback uses `ORDER BY date DESC LIMIT 1`; no longer returns 0.0 before first `record_daily_pnl()` of the day
- [x] **Telegram offset int cast** — `telegram_command_handler._load_offset()` returns `int(row["value"])`; was returning TEXT causing `TypeError` that silently killed command handler
- [x] **Groq exception handling** — `llm_client._try_groq()` wrapped in try/except; Groq failures now raise `RuntimeError` instead of propagating raw, preventing `UnboundLocalError` in callers
- [x] **Reddit cache fix** — `reddit_sentiment.social_score()` uses `timedelta.total_seconds()` (was `.seconds` = 0-59 only; cache never worked beyond 60 seconds)
- [x] **score_cache thread-safe** — `threading.Lock()` added; `stats()` TTL display fixed (`total_seconds()`)
- [x] **Backtester correct score** — `backtester.run_backtest()` now filters `scan_type='scheduled'` to use stock_scorer composite score; was using `explosion_score` (catalyst metric) for all rows
- [x] **XSS fixes — page_research.py** — 7 injection points patched with `html.escape()`: LLM output (bull/bear/verdict/AI analysis), yfinance holder names, Finnhub headlines (+ `javascript:` URL guard)
- [x] **_html() rule — page_news_impact.py** — `_html()` function added and applied to all 6 multiline HTML blocks; previously violated project HTML rendering rule
- [x] **monitoring_queue snapshot integrity** — `_persist_queue()` only called when `apply_liquidity_gate=True`; `signal_combiner.evaluate()` calls `build_queue(gate=False)` which no longer overwrites snapshot with unfiltered tickers
- [x] **auto-exit transaction order** — `run_watchlist_scan()` now writes both cooldown rows BEFORE `watchlist_remove()`; matches the hardened pattern in `run_watchlist_cleanup()`
- [x] **news_impact_analyzer UnboundLocalError** — `raw = ""` initialized before `try` block; `json.JSONDecodeError` handler no longer crashes with `UnboundLocalError` when `llm_complete()` throws
- [x] **news_impact_analyzer XSS** — LLM-supplied `reason` strings in `run_full_analysis()` escaped with `html.escape()`
- [x] **macro events disclaimer** — `market_feed.get_upcoming_macro()` marks all events `*` and appends `"* approximate — verify dates"` entry; prevents misleading "CPI every Tuesday" display
- [x] **options_flow OI=0 false positive** — contracts with `openInterest=0` and `volume<500` skipped; `volume≥500` uses `volume/100` ratio instead of sentinel `9999`
- [x] **page_catalyst.py XSS** — company name, sector, catalyst detail from yfinance/EDGAR escaped before HTML injection
- [x] **page_scan.py file handle leak** — `subprocess.Popen(stdout=open(...))` replaced with explicit `_log_fh` variable that is closed after spawn
- [x] **page_backtest.py forward signals** — `📊 Forward Signal Win-Rate (Live)` section added showing 7d/30d win-rate from `forward_signals` table (the authoritative source)
- [x] **alert_monitor WAL** — `check_portfolio_health()` replaced raw `sqlite3.connect()` with `get_connection()` from `src.database` (WAL pragmas, retry_on_busy)
- [x] **alert_monitor dead type** — `supertrend_intraday_flip` removed from `THREAD_TYPES`; was causing guaranteed false-positive "dead thread" report every morning
- [x] **auto_watchlist_agent score threshold** — `alert_score` now uses `AUTO_WL_SCORE_ENTRY` (70) from hysteresis.py; was 60 from config, inconsistent with rest of system
- [x] **google_trends thread-safe** — `threading.Lock()` added to singleton; 1-hour in-memory cache prevents 100+ concurrent API calls per scan
- [x] **insider_tracker timeouts** — EDGAR HTTP calls now have `timeout=15`; no longer risk hanging indefinitely
- [x] **alpha_vantage quota warning** — daily call counter logs WARNING when approaching 23/25 req/day limit
- [x] **ibkr_realtime market_value** — `abs()` on both `position` and `avgCost` for short-position safety; comment clarifies this is cost-basis estimate updated by `portfolio()` enrichment
- [x] **scheduler_config.json** — 7 missing keys added: `news_catalyst_max_article_age_minutes`, `long_setups_enabled`, `long_setups_time`, `long_setups_min_score`, `long_setups_top_n`, `alert_monitor_time`, `weekly_rotation_time`
- [x] **news_fetcher bearish keywords** — `recession` (3) and `wipe` (2) added to `NEGATIVE_KW`; "Market crash wipes out gains as recession fears grow" now correctly classified Bearish
- [x] **squeeze_scanner is_critical_alert** — SI < 10% absolute floor added; prevents all-low-SI lists from marking each other critical via 90th percentile
- [x] **test suite hardened** — 9 new tests in `tests/test_new_fixes.py` (bars_ago, atomic dedup, BEAR veto, forward_signals quality); `_in_memory_db` fixture in `test_order_funnel.py` patched for `src.ibkr_worker.get_connection` + `ibkr_positions` schema; `_recent_buy_tickers` mocked in liquidity hysteresis test; numpy bool comparison fixed in `test_fixes.py`

### Alert Analysis & Hardening — 2026-06-02

- [x] **`record_fill()` CANCELLED guard** — `src/forward_signals.py`: `record_fill()` now queries `order_log WHERE ibkr_order_id = ?` before writing; skips with WARNING if `status == 'CANCELLED'` — bracket-order race between child-leg cancel and parent fill no longer corrupts `forward_signals.fill_price` and win-rate metrics
- [x] **Signal market-hours gate** — `src/ibkr_worker.py`: `_is_signal_hours()` helper added (04:00–20:00 ET via `zoneinfo`); `_check_ticker()` returns `None` outside this window after the `bars_ago == 1` check — prevents pre-market signals (e.g. HNST fired at 02:00 ET on 2026-06-01)
- [x] **Catalyst alert price floor** — `scheduler.py` `run_catalyst_alert()`: filter now requires `r.get("price", 0) >= 5.0`; sub-$5 penny stocks (PLCE $4.18, AVXL $3.03) excluded — their bid-ask spread makes the -8% stop meaningless
- [x] **FAKE ticker purged from DB** — `DELETE FROM watchlist` + `watchlist_alerts WHERE ticker='FAKE'`; was generating spurious `price_change` baseline records every 5 min from `_price_monitor_thread`
- [x] **`price_surge_rescore` documented** — added to Alert Types table (count 18 → 19): trigger >10% move since last baseline, Telegram if score ≥ 55, 2h cooldown, 09:30–16:00 ET only
- [x] **IBKR watchdog clean-exit fix** — `run_ibkr_worker_watchdog.py`: previously stopped on `returncode=0` (clean exit from Gateway disconnect), causing worker to stay down until next login. Now restarts after `CLEAN_EXIT_DELAY=60s` on clean exit. To stop intentionally: create `stop_ibkr_worker.flag` in project root — watchdog detects it on next exit, deletes it, and stops cleanly.
- [x] **Watchdog Telegram notifications** — both `run_ibkr_worker_watchdog.py` and `run_scheduler_watchdog.py` now send Telegram alerts on: startup 🟢, restart 🔄, crash 🔴, clean exit / sentinel stop ⏹️. Standalone `_send_telegram()` + `_load_env()` functions — no dependency on `src.*`. Scheduler watchdog also gains `stop_scheduler.flag` sentinel.

### Tunnel Hardening — 2026-06-05

- [x] **`_tunnel_healthy()` DNS check** — `run_dashboard_tunnel.py`: health check now performs a public DNS lookup (`socket.getaddrinfo`) on the tunnel hostname in addition to the local cloudflared metrics check. Root cause: cloudflared process stays alive and reports `ha_connections=1` even after Cloudflare deregisters the quick-tunnel DNS record — the local metrics check always returned 🟢 while the URL was NXDOMAIN externally. With the DNS check, 3 consecutive failures trigger a tunnel restart + new URL.
- [x] **Tunnel watchdog** — `run_tunnel_watchdog.py` added (mirrors `run_scheduler_watchdog.py`): auto-restarts `run_dashboard_tunnel.py` on crash or clean exit; sends Telegram on startup 🟢, restart 🔄, crash 🔴. On restart, `run_dashboard_tunnel.py` naturally sends the new URL to Telegram. Registered as `FinancialAgentTunnelWatchdog` Windows Scheduled Task (trigger: at logon). Stop with `stop_tunnel.flag` sentinel.

### Worker Hardening — 2026-06-25

- [x] **Python Launcher two-process fix** — `run_ibkr_worker_watchdog.py`: root cause of chronic "two workers always running" — `.venv313\Scripts\python.exe` is the Windows Python Launcher (`py.exe`, ~249 KB), which always spawns the real interpreter as a child → perpetual parent+child pair per worker. Fix: reads `pyvenv.cfg` to extract `executable = C:\...\Python313\python.exe` and uses it directly. Venv activated via `__PYVENV_LAUNCHER__` + `VIRTUAL_ENV` + PATH env vars (same mechanism the launcher uses internally). Result: exactly ONE python313.exe process when worker is running.
- [x] **Orphan worker kill on watchdog restart** — `run_ibkr_worker_watchdog.py`: `_kill_orphaned_worker()` reads `ibkr_worker.pid` file (written by watchdog after `Popen()`), calls `TerminateProcess()` via ctypes on any leftover worker PID, then deletes the PID file. Prevents two workers when watchdog crashes and Task Scheduler restarts it (Windows does not kill orphan children on parent exit).
- [x] **Worker singleton mutex** — `src/ibkr_worker.py`: `_acquire_singleton_lock()` creates Windows named mutex `Global\FinancialAgent_IBKRWorker_Singleton`; second instance exits with code 1 immediately. Defense-in-depth — should never fire in normal operation after the Launcher fix, but guards against manual double-start. Mutex auto-released by OS on process exit even if `_release_singleton_lock()` is not called (crash-safe). Also writes `ibkr_worker_running.lock` file with PID for diagnostic purposes.
- [x] **`multiprocessing.freeze_support()`** — `src/ibkr_worker.py`: added in `if __name__ == "__main__"` block. Prevents Windows spawn-mode from double-executing `main()` when libraries that import `multiprocessing` (loguru, multitasking) trigger the multiprocessing infrastructure.
- [x] **SELL gate in signal_combiner** — `src/signal_combiner.py`: SELL signal suppressed when `ibkr_positions` has no row with `shares > 0` for the ticker. Prevents spurious SELL alerts + phantom orders when Supertrend flips bearish but position was already closed. Complementary to the existing Layer -1 veto in `execution_engine` (defense-in-depth at the signal layer before order submission).
- [x] **`record_daily_pnl()` 09:30 ET gate** — `src/position_tracker.py`: skips before 09:30 ET to avoid writing a $0 / $0 row from pre-market account summary (IBKR returns 0 net liquidation before market open). Changed `INSERT OR IGNORE` → `INSERT OR REPLACE` so a corrected re-run overwrites the early row rather than silently discarding it.
- [x] **`_update_order_log()` race condition** — `src/ibkr_worker.py`: FILLED update uses `WHERE status NOT IN ('FILLED','ERROR')` — prevents CANCELLED child leg from overwriting a parent FILLED status. CANCELLED update uses `WHERE status = 'SUBMITTED'` — only demotes rows that are still pending; FILLED rows are never touched. Previously used a single `WHERE ibkr_order_id = ?` with no status guard, allowing bracket-order async callbacks to corrupt terminal statuses.

### DCF & Data Quality Hardening — 2026-06-26

- [x] **Backtester corporate action fix** — `src/backtester.py`: `price_at_signal` now fetched from yfinance on same `auto_adjust=True` basis as `price_after`. Existing corrupted rows refreshed via `UPDATE` (was `INSERT OR IGNORE` which silently kept the bad value). **One-time DB cleanup**: deleted 3 `backtest_results` rows for DD (pct_change >100%, reverse split artifact, 7d/14d) and 1 row for POWL (pct_change <-50%, forward split artifact, 7d).
- [x] **DCF net debt subtraction** — `src/dcf_valuation.py`: Enterprise Value now correctly converted to Equity Value by subtracting `totalDebt − totalCash`. Over-leveraged companies (equity_value ≤ 0) return None → P/S fallback. Previously: EV used directly as equity value, inflating intrinsic by debt amount.
- [x] **DCF proper WACC** — CAPM cost of equity (`Ke = Rf(^TNX) + Beta × 5.5%`); actual cost of debt (`interestExpense/totalDebt`); proper `WACC = E/(D+E)×Ke + D/(D+E)×Kd×(1−tax)`. Higher leverage lowers WACC (debt cheaper after tax shield), equity impact captured by net debt subtraction.
- [x] **DCF financial sector exclusion** — Banks, Insurance, Financial Services return None from DCF (FCFF invalid for balance-sheet-driven businesses). TCBI, BAC etc. now correctly fall through to P/S.
- [x] **DCF growth floor** — changed +3% to −10%; declining businesses (revenueGrowth < 0) no longer get an artificial 3% floor.
- [x] **DCF FCF tiered sourcing** — `src/edgar_fcf.py` (new module): Tier 1 = SEC EDGAR XBRL median of 4 annual 10-K values (audited, free, no key); Tier 2 = yfinance cashflow DataFrame multi-year median; Tier 3 = yfinance TTM; Tier 4 = OCF−CapEx.
- [x] **EDGAR fundamentals** — `src/edgar_fcf.py` extended with: `get_revenue_cagr` (5yr CAGR from 10-K), `get_interest_coverage` (EBIT/InterestExpense), `get_current_ratio` (AssetsCurrent/LiabilitiesCurrent), `get_eps_yoy_growth` (quarterly YoY proxy). All 24h cached.
- [x] **Fundamentals scorer EDGAR integration** — `_score_fundamentals` in `stock_scorer.py`: Revenue CAGR 5yr (EDGAR) replaces yfinance 1yr; Interest Coverage (EDGAR) replaces D/E as debt quality signal (D/E kept as fallback). Thresholds: revenue 20%/8%/2%; ICR ≥5=2pts / ≥2=1pt.
- [x] **Earnings sentiment EDGAR fallback** — `src/earnings_sentiment.py`: when Finnhub returns empty, `_edgar_eps_fallback()` computes YoY EPS% from EDGAR 10-Q filings and maps to 0–5 score (`source='edgar_eps_yoy'`). Prevents `score=0, source='none'` for tickers Finnhub doesn't cover.

### QA Hardening Phase 2 — 2026-06-29

Second pass over the multi-agent audit list (16 fixes: HIGH + MEDIUM + LOW priorities).

- [x] **`score_delta_rise` suppression after auto-add** — `scheduler.py`: added `"score_delta_rise"` to the post-auto-add suppression loop (was only `score_threshold` + `price_change`). Prevents 12:00 watchlist scan from re-firing a delta alert for just-added tickers.
- [x] **`short_pct`/`short_ratio` NaN guard** — `stock_scorer.py`: `info.get('shortPercentOfFloat') or 0` returns `NaN` when yfinance returns NaN (NaN is truthy). Replaced with explicit `math.isnan()` check + float cast.
- [x] **DCF `growth_proxy` missing-data path** — `dcf_valuation.py`: when both `revenueGrowth` and `earningsGrowth` are None, `raw_growth_proxy` is now `None` (was `0`). Blend logic respects `None`; falls through to `historical_fcf_growth` alone, or logs 0% with a DEBUG message.
- [x] **`core_max` division-by-zero guard** — `stock_scorer.py`: `(core / core_max if core_max > 0 else 0)` — unreachable in normal operation but guards against future weight-config changes.
- [x] **Price monitor cycle timing** — `scheduler.py`: added `_t0 / _elapsed` around the `_price_monitor_thread` check loop; logs duration and emits WARNING if cycle exceeds 80% of the interval.
- [x] **Momentum scanner SPY missing warning** — `src/momentum_scanner.py`: logs `WARNING` when SPY data is absent or has < 21 bars, so RS scores using 0% benchmark are visible in logs.
- [x] **`borrow_fee.py` short error TTL** — Failure results (403/429/parse error) now cached for 5 min instead of 2 hours. Uses timestamp offset trick to preserve the existing TTL check logic.
- [x] **`/cancel` ticker validation** — `src/telegram_command_handler.py`: ticker validated with `re.fullmatch(r"[A-Z]{1,6}", ticker)` before calling IBKR. Rejects empty, too-long, or non-alpha inputs.
- [x] **`_reply()` truncation guard** — `src/telegram_command_handler.py`: replies truncated at 4000 chars with `…` to match TelegramNotifier behavior.
- [x] **`page_scheduler.py` XSS** — all `st.error(f"Error: {e}")` calls now use `html.escape(str(e))` to prevent raw exception text (potentially from external API responses) reaching the browser.
- [x] **`get_interest_coverage()` zero-debt fix** — `src/edgar_fcf.py`: when `InterestExpense == 0`, returns `100.0` (max cap) instead of `None`. Zero-debt companies now receive full ICR score instead of D/E fallback.
- [x] **`auto_watchlist_agent` DB write protection** — `src/auto_watchlist_agent.py`: `watchlist_add()` + `watchlist_save_alert()` wrapped in `try/except`. DB failure for one ticker no longer aborts the entire loop.
- [x] **Tunnel watchdog Telegram flood guard** — `run_tunnel_watchdog.py`: crash/restart notifications rate-limited to one per 5 minutes via `flood_guard=True` param. Startup and stop-sentinel messages always send.
- [x] **MLP `early_stopping` caveat documented** — `src/stock_forecaster.py`: added comment explaining the shuffled-validation-split limitation (non-ideal for time series, intentionally unchanged).
- [x] **PDUFA scraper column-order validation** — `src/catalyst_scanner.py`: reads `<thead>` headers to determine actual column indices for ticker/catalyst/date. Falls back to hardcoded defaults (0/2/3) if headers absent or unrecognized.
- [x] **`score_delta_rise` auto-add suppression** — already listed above.

### QA Hardening Sprint — 2026-06-28

Multi-agent QA audit (6 specialized agents) surfaced 9 HIGH findings, all fixed in this sprint.

- [x] **Auto-exit transaction order** — `scheduler.py` `run_scan()`: both `watchlist_save_alert()` calls (`auto_exit_score` + `auto_exit_cooldown`) now written **BEFORE** `watchlist_remove()`. Matches the hardened pattern already in `run_watchlist_scan()`. Prevents cooldown loss if DB remove succeeds but alert write fails.
- [x] **Daily loss limit unavailable-data veto** — `src/execution_engine.py` `check_daily_loss_limit()`: `portfolio_value ≤ 0` now returns `passed=False` ("portfolio_value unavailable") instead of `passed=True`. Previously a data-fetch failure or pre-market 0.0 silently bypassed the daily loss limit entirely.
- [x] **Supertrend pandas CoW** — `src/supertrend.py`: all `series.iloc[i] =` writes in the Supertrend band/trend loop replaced with `series.iat[i] =`. `iloc[i] =` triggers `SettingWithCopyWarning` in pandas ≥ 2.0 CoW mode and is scheduled to raise an error in future pandas; `iat[i]` is the correct scalar-position write.
- [x] **XSS in `page_options_flow.py`** — `_rtl()` helper: added `html.escape(text)` before `.replace('\n', '<br>')`. LLM-supplied text was injected raw into `st.markdown(unsafe_allow_html=True)`.
- [x] **`alert_monitor.py` connection leak** — replaced `conn = get_connection()` / `conn.close()` pattern with `with get_connection() as conn:` — connection is now guaranteed to close even on exception.
- [x] **EDGAR dual-cache eliminated** — `src/edgar_fcf.py`: `_FCF_CACHE` and its standalone HTTP fetch in `get_edgar_fcf_series()` removed. Function now delegates to `_fetch_facts()` (shared `_FACTS_CACHE`). Eliminates ~946 duplicate `companyfacts` SEC requests per scan (one per ticker was being made twice — once for FCF, once for fundamentals).
- [ ] ~~**BUY composite gate**~~ — **NOT applied**: user preference (2026-06-03) is Supertrend-flip-only with no score gate — symmetric BUY/SELL behavior. CLAUDE.md table entry "entry 60 / hold 50" is stale documentation; code is authoritative. `hysteresis.py` comment updated to reflect gate is intentionally absent.
- [x] **`meme_squeeze_sentinel.py` WAL** — `SqueezeDatabase`: added `_connect()` helper that sets `journal_mode=WAL`, `busy_timeout=10000`, `synchronous=NORMAL`. All methods now use `with self._connect() as conn:` context managers. Removes 6 bare `sqlite3.connect()` + `conn.close()` calls and associated connection leaks.
- [x] **Bracket order crash-atomicity** — `src/ibkr_realtime.py` `place_bracket_order()`: the 3 `placeOrder()` calls are now wrapped in `try/except`; if any leg fails, all already-submitted legs are cancelled to prevent dangling parent orders with no stop/target protection.

### Known Caveats (fixed 2026-05-29)

- [x] **Queue cliff vs combiner hold-band** — Fixed: added `_recent_buy_tickers()` as a third feeder in `build_queue()`. Tickers with a `combined_buy` alert in the last 72h bypass the `SCANNER_MIN_SCORE=65` gate, keeping them in the monitoring queue so `signal_combiner`'s hold-band (composite >= 50) is reachable.
- [x] **`_previous_queue` DB persistence** — Fixed: `monitoring_queue.py` now persists the accepted ticker set to `monitoring_queue_snapshot` DB table at the end of every `build_queue()` call. Loaded on process startup. Both scheduler and IBKR worker share the same snapshot via DB.
- [x] **Cleanup cooldown swallow on DB lock** — Fixed: `run_watchlist_cleanup()` in `scheduler.py` now inserts the `auto_exit_cooldown` row BEFORE deleting the ticker, both within a single `with get_connection() as conn:` transaction block. Either both succeed or neither does.

### Alert Cleanup 2026-05-20 — Final Pass

**Philosophy:** Telegram is reserved for **real-time, high-conviction** alerts. Everything driven by yfinance polling (15-min lag) or lagging indicators is DB-log-only.

**Telegram channel after cleanup (~80 messages/week):**

| Alert type | Source | Why kept |
|---|---|---|
| `combined_buy` / `combined_sell` | IBKR real-time (`ibkr_worker` → `signal_combiner`) | The only true real-time path |
| `catalyst_si_alert` | daily catalyst scanner | Forward-looking events — latency-tolerant |
| `breakout_alert` | daily scan (`run_scan`) | DB-only — **silenced from Telegram** (superseded by `combined_buy` IBKR real-time) |
| `auto_wl_momentum` / `auto_wl_squeeze` | scan auto-add | Informational |
| `price_above` / `price_below` / `price_target` / `price_change` | user-defined | Manual targets |
| `stop_loss` / `target_hit` / `score_drop` (portfolio) | portfolio monitor | Position management |
| `news_catalyst` | LLM news analysis | Forward-looking catalysts; freshness gate (45 min default) filters reactive stale articles |

**Silenced (DB log retained for audit):**

| Alert type | Reason | Source |
|---|---|---|
| `supertrend_1h_flip` | superseded by `combined_buy` | `price_alert_monitor.py` |
| `supertrend_flip` (daily) | superseded by `combined_buy` | `price_alert_monitor.py` |
| `supertrend_intraday_flip` | **hard-removed** dead code | — |
| `supertrend_triple_bull` / `_bear` | yfinance lag + duplicates `combined_buy` | `price_alert_monitor.py` |
| `rsi_oversold` / `rsi_overbought` | lagging indicator + yfinance lag | `price_alert_monitor.py` |
| `macd_bullish` / `macd_bearish` | lagging indicator + yfinance lag | `price_alert_monitor.py` |
| `volume_spike` | ambiguous direction (up vs down) | `price_alert_monitor.py` |
| `score_threshold` | redundant with `combined_buy` (composite ≥ 60) | `watchlist_manager.py` |
| `score_delta_rise` | redundant with `combined_buy` | `watchlist_manager.py` + `score_alert.py` |
| `score_delta_drop` | weekly digest covers retrospective drops | `watchlist_manager.py` + `score_alert.py` |
| `squeeze_si_alert` | daily cadence, not real-time | `scheduler.py:run_squeeze_scan` |

**Squeeze thresholds raised** (`scheduler.py:499`) — SI>15%/DTC>10 → SI>20%/DTC>15 (now silenced for Telegram but DB row written when ticker hits the new tighter bar).

**Expected volume:** ~350/week → **~80/week** (-77%).

**Open follow-ups:**
- [ ] `supertrend_triple_bull/bear` — consider routing through `signal_combiner.evaluate()` so the DB row earns the same cap+dedup discipline as `combined_buy/sell` (currently DB-only but no cap).
- [x] Queue cliff fix — added `_recent_buy_tickers()` third feeder in `build_queue()` (2026-05-29).
- [x] `_previous_queue` DB persistence — persisted to `monitoring_queue_snapshot` table (2026-05-29).
- [ ] `news_catalyst` threshold tuning — consider lowering catalyst_threshold from 3 to 2 if forward-paper-trading shows missed catalysts.
- [x] `news_catalyst` freshness gate — `max_article_age_minutes=45` implemented; configurable via `scheduler_config.json` + Scheduler UI.
