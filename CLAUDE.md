# FinancialAgent — Claude Code Context

## Project Overview
AI-powered stock scanner & financial analysis dashboard.
- **Location:** `C:/Projects/FinancialAgent`
- **Stack:** Python 3.14, Streamlit 1.52.2, SQLite, yfinance, Finnhub, Alpha Vantage, SEC EDGAR
- **LLMs:** Gemini 2.0 Flash (primary) → Groq Llama 3.3 70B (fallback) via `src/llm_client.py`
- **Run:** `streamlit run dashboard.py` → http://localhost:8501
- **Tests:** `python -m pytest tests/ --ignore=tests/test_new_apis.py --ignore=tests/test_ibkr_connection.py --ignore=tests/test_ibkr_worker_once.py` → **348 passed, 5 pre-existing failures** (test_pnl_digest_fixes.py — unrelated to core logic)

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
| `ibkr_worker.py` | Standalone daemon (Python 3.13, `.venv313`) — runs Supertrend(1H) every 5 min on the monitoring queue, fires combined alerts + submits orders via `order_manager` + syncs positions/daily P&L via `position_tracker`. **`sync_positions()` runs at the START of each cycle** (before ticker loop). **`bars_ago != 1` check** in `_check_ticker()` — only fires on the exact bar that flipped, preventing stale-flip duplicates. **`_is_signal_hours()` gate** — signals blocked outside 04:00–20:00 ET (`zoneinfo`), preventing pre-market/overnight order submission. Subscribes to `ib.orderStatusEvent` for fill/cancel callbacks. Startup reconciliation + periodic fill sweep (every 30 min). Hosts `TelegramCommandHandler` thread. **Windows named mutex singleton** (`Global\FinancialAgent_IBKRWorker_Singleton`) — `_acquire_singleton_lock()` in `main()` prevents two worker instances; second instance exits with code 1 immediately. `multiprocessing.freeze_support()` called in `__main__` to prevent Windows spawn-mode double-execution. **`_update_order_log()` race fix** — FILLED status uses `NOT IN ('FILLED','ERROR')` guard; CANCELLED uses `= 'SUBMITTED'` guard; prevents bracket-order child-leg cancel from overwriting a FILLED status. **`PER_CYCLE_BUY_CAP = 3`** — max 3 `combined_buy` per `run_once()` cycle; excess signals release their dedup claim (`release_dedup()`) for retry next cycle — prevents alert floods when many tickers flip simultaneously. **SUBMITTED Telegram condensed** — 1-liner `✅ {ticker} {shares}sh @ ${price:.2f} | #{order_id}` (full detail already in signal message; was duplicating it). |
| `monitoring_queue.py` | Source of truth for "which tickers get real-time IBKR monitoring" — scanner score ≥ 65 + manual watchlist + recent BUY alerts (72h) + liquidity gate (hysteresis: enter $5M / exit $3M ADV). Queue state persisted to `monitoring_queue_snapshot` DB table. **`_persist_queue()` only called when `apply_liquidity_gate=True`** — prevents `signal_combiner.evaluate()` calls (gate=False) from corrupting the snapshot with unfiltered tickers. |
| `order_manager.py` | Wraps IBKR order calls; runs execution_engine veto checks before submission; logs every attempt to `order_log` DB table. Injects `position_tracker` into execution engine for daily loss limit. **Fetches `portfolio_tickers` from `ibkr_positions` DB before `evaluate_trade()`** — enables sector concentration veto (Layer 6). paper_mode=True default; live requires `IBKR_LIVE=true` env var. Module-level `_trading_paused` flag — when True, `submit()` returns PAUSED without evaluating. Passes `signal_type` ("BUY"/"SELL") to `evaluate_trade()`. |
| `position_tracker.py` | Syncs IBKR positions to `ibkr_positions` DB table every 5 min; records `daily_pnl` once per day; exposes `get_current_exposure()`, `get_portfolio_value()`, `get_daily_pnl()` for execution engine. `get_portfolio_value()` DB fallback uses `ORDER BY date DESC LIMIT 1` (most recent row, not just today) — prevents returning 0.0 early morning before `record_daily_pnl()` runs. **`record_daily_pnl()` 09:30 ET gate** — skips before 09:30 ET (market open) to avoid writing a $0 row from pre-market account summary; uses `INSERT OR REPLACE` (was `INSERT OR IGNORE`) so the row is updated if re-run after the first write. |
| `signal_combiner.py` | Supertrend 1H flip → BUY/SELL alert; enforces daily cap (10), 24h dedup. **BUY: no score gate** — any monitoring-queue ticker gets alerted on bullish flip. **SELL: no score gate** — gated only on open position (`ibkr_positions WHERE shares > 0`); `SELL_MAX_SCORE=55` was added 2026-07-15 and removed 2026-08-02 — blocked 100% of exits since all positions score ≥ 70. Score pulled for message enrichment (BUY) only. **`_try_claim_dedup()` performs SELECT+INSERT atomically in a single DB connection** — eliminates the race window of the old split check+write. `_record_dedup()` removed (was dead code). **SELL position gate** — before firing a SELL alert, checks `ibkr_positions WHERE ticker = ? AND shares > 0`; suppresses SELL (no Telegram, no order) when no open position exists. |
| `forward_signals.py` | Records every fired alert with entry price + data quality check; `record_fill()` updates `fill_price`/`fill_source` from IBKR callback — **guards against CANCELLED orders** (cross-checks `order_log.status` before writing, skips if CANCELLED to prevent bracket-order race from corrupting win-rate); daily 18:00 job fills `price_after_{7,14,30}d`; weekly Friday 20:00 Telegram digest with win-rate metrics |
| `earnings_sentiment.py` | Tier 1 = Finnhub EPS surprise history (free), Tier 2 = LLM transcript analysis (paid). Score 0–5 added to `stock_scorer.py` bonus band. **EDGAR fallback**: when Finnhub returns empty, uses `edgar_fcf.get_eps_yoy_growth()` (YoY EPS% proxy, `source='edgar_eps_yoy'`) instead of returning score=0. |
| `hysteresis.py` | Central helper `passes_hysteresis(current, in_set, entry, exit)` + threshold constants (composite, SI, liquidity, watchlist score) |
| `stock_forecaster.py` | Ensemble forecaster (ARIMA/MA/ES/MLP). Constructor accepts `point_in_time: datetime` — strictly truncates input to ≤ point-in-time to prevent backtest look-ahead bias |
| `news_catalyst_monitor.py` | Background thread — checks news every N min; freshness gate skips articles older than `max_article_age_minutes` (default 45, config key `news_catalyst_max_article_age_minutes`) |
| `run_dashboard_tunnel.py` | Cloudflare Quick Tunnel launcher; sends URL on startup + daily heartbeat at 08:05 IL with health status. `_tunnel_healthy()` checks both local cloudflared metrics AND public DNS resolution — catches expired quick-tunnel URLs where cloudflared stays running but DNS is deregistered |
| `run_tunnel_watchdog.py` | Watchdog for `run_dashboard_tunnel.py` — auto-restarts on crash or clean exit, sends Telegram on startup/restart/crash. Registered as `FinancialAgentTunnelWatchdog` Windows Task. Stop with `stop_tunnel.flag` sentinel |
| `supertrend.py` | Supertrend calculation (ATR-based, Wilder EMA) — used by `ibkr_worker.py` and `price_alert_monitor.py` |
| `market_regime.py` | BULL / CAUTION / BEAR regime based on VIX thresholds (20/28) + SPY vs SMA200; used by `execution_engine.py` for position sizing and stop adjustments. **`_SPY_HISTORY = "1y"`** (~252 trading days) — computes actual SMA200, not SMA126 (was `"6mo"`, now fixed). |
| `execution_engine.py` | Trade decision engine (Layers -1.5 through 6): daily loss limit (Layer 0), hard veto, confluence check, position sizing scaled by market regime, time-of-day flag, sector exposure guard. `evaluate_trade()` accepts optional `signal_type` param — SELL with no open position is vetoed (Layer -1). **Layer -1.5: already-long BUY veto** (added 2026-08-03) — checks `ibkr_positions WHERE shares > 0` before Layer 0; BUY vetoed if ticker already held long (no pyramiding). Short positions (shares < 0) NOT vetoed — BUY to cover a short is legitimate. Fail-open on DB error. **`check_hard_vetos()` accepts `signal_type`** — BEAR regime veto applies to BUY only (`signal_type != "SELL"`), allowing exits in BEAR market. |
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
| Composite-for-BUY | 60 | — | **gate intentionally removed 2026-06-03** — Supertrend flip is sole trigger for BUY |
| Composite-for-SELL | — | — | **gate removed 2026-08-02** — SELL gated only on open position (ibkr_positions WHERE shares > 0); SELL_MAX_SCORE=55 was reverted because it blocked 100% of exits (all positions score ≥ 70) |
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
1. `🚨 High SI+DTC Alert` — tickers with SI > 20% AND DTC > 15 (cooldown 24h, saved as `squeeze_si_alert`)
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
| `squeeze_si_alert` | SI > 20% AND DTC > 15 | 24h | `scheduler.py` |
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
| 08:30 | Scheduled Scan Top 10 (top 10 of all scanned above `min_score`) | `run_scan` | 0–1 |
| 09:15 | Portfolio scan alerts (stop_loss, target_hit, score_drop) | `run_portfolio_scan` | 0–N |
| 09:30 | Market Digest (indices + headlines) | `run_market_digest` | 1 |
| 09:30 | Alert Monitor health report (noisy alerts / drawdown) | `run_alert_monitor` | 0–1 |
| every 5m | `combined_buy` / `combined_sell` (real-time Supertrend 1H via IBKR) + order submission result (SUBMITTED/VETOED/PAUSED) | `ibkr_worker` → `signal_combiner` → `order_manager` | 0–N |
| on fill | 💰 ORDER FILLED — ticker + fill price + order ID | `ibkr_worker` → `_on_order_status` callback | 0–N |
| on command | Reply to `/status`, `/positions`, `/pause`, `/resume`, `/cancel` | `telegram_command_handler` | 0–N |
| every 5m | Supertrend flip 15m / 1h / daily (DB-only, silenced from Telegram) | `price_alert_monitor.py` | 0–N |
| every 15m | News catalyst (LLM analysis) | `catalyst_monitor_thread` | 0–N |
| every 30m | Momentum scanner alerts (auto-add candidates) | `_momentum_monitor_thread` | 0–N |
| 12:00 | Squeeze Scan (High SI+DTC + Top 10 candidates) — DB-only, silenced from Telegram (`scheduler.py` builds the message into `_`, never sends) | `run_squeeze_scan` | — |
| 12:00 | Watchlist scan alerts (score_threshold, price_change, price levels, score_delta) | `run_watchlist_scan` | 0–N |
| 15:00 | Same as 08:30 incl. Scheduled Scan Top 10 (second daily scan — 09:00 ET, before US market open 09:30 ET) | `run_scan` | 0–N |
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

### Exit Strategy Implementation — 2026-08-02

Root cause analysis: forward-signal data showed BUY win rate 55.8% but avg win +5.03% vs avg loss −6.38% → near-zero expectancy. All 42 open positions had 0 active bracket legs (TIF=DAY expired every night). SELL_MAX_SCORE=55 gate removed (added 2026-07-15) — it blocked 100% of exits since all positions score ≥ 70.

- [x] **TIF=DAY → GTC** — `src/ibkr_realtime.py`: bracket order legs (parent LMT, STP stop, LMT target) changed from `tif="DAY"` to `tif="GTC"`. Standalone SELL limit order (line 195) remains `DAY` — appropriate for intraday exit signals. Prevents stops/targets from expiring at each market close.
- [x] **`modify_stop_order(ticker, new_stop)`** — new method on `IBKRConnection` in `src/ibkr_realtime.py`. Finds the active STP SELL order for a ticker in `ib.openTrades()` and updates `auxPrice` via `placeOrder()` (IBKR's modify-in-place mechanism).
- [x] **`get_open_orders()` extended** — now returns `lmt_price` and `aux_price` fields; `aux_price` is the stop trigger price for STP orders.
- [x] **`_atr(df, period)` helper** — `src/ibkr_worker.py`: Wilder's ATR using `ewm(alpha=1/period)` — matches `supertrend.py` convention.
- [x] **`_update_trailing_stops(conn)`** — `src/ibkr_worker.py`: every 15 min, for each active STP SELL order, computes `price - 2.5 * ATR_1h` and raises the stop if result is higher than current stop. Never lowers. Sends Telegram on each move. Called at end of `run_once()`.
- [x] **`_trading_days_since(date_str)`** — counts Mon-Fri trading days between entry date and today; used by time stop.
- [x] **`_check_time_stops(conn)`** — `src/ibkr_worker.py`: every 4 hours, checks positions held > 15 trading days with abs(pnl_pct) < 3% (zombies). Submits SELL at market-price limit (0.1% below last), logs to `order_log` with note "TIME STOP: Nd zombie", sends Telegram.
- [x] **`exit_tier` column** — `src/database.py` migration adds `exit_tier INTEGER DEFAULT 0` to `ibkr_positions`. Values: 0=no partial exit, 1=T1 taken (40% sold), 2=T2 taken (30% more sold).
- [x] **`_check_tiered_exits(conn)`** — `src/ibkr_worker.py`: T1 at +7%: sell 40%, move stop to breakeven; T2 at +14%: sell 50% of remaining; remainder trails via Supertrend + ATR trailing stop. Sends Telegram on each tier.
- [x] **`_check_score_deterioration(conn)`** — `src/ibkr_worker.py`: if score drops ≥15pts from entry signal score AND current score <55 AND pnl >-3%, submits full SELL. 24h dedup via `watchlist_alerts` (`alert_type='score_deterioration_exit'`).
- [x] **`run_once()` wiring** — all 4 exit functions called at end of each 5-min cycle, each in separate `try/except` so a failure in one never blocks others.
- [x] **SELL gate removed from docstring** — `src/signal_combiner.py` docstrings updated to reflect current behavior: "SELL: no score gate — gated only on open position."
- [x] **Scheduled task updated** — `sell-gate-evaluation-2026-08-05` prompt rewritten to evaluate exit strategy activation instead of the removed SELL gate.

**Test baseline after sprint:** 348 passed, 5 pre-existing failures (test_pnl_digest_fixes.py — unrelated to this sprint).

### Anti-Pyramiding & Alert Flood Hardening — 2026-08-03

Root cause: live trading produced 9 `combined_buy` signals for different tickers within 4 minutes (multi-ticker simultaneous Supertrend flip), generating 27 Telegram messages (3 per trade: signal + SUBMITTED duplicate + fill). Additionally, existing long positions were receiving new BUY orders on subsequent Supertrend flips — pure pyramiding with no portfolio-level cap.

- [x] **Layer -1.5: already-long BUY veto** — `src/execution_engine.py`: `evaluate_trade()` now checks `ibkr_positions WHERE ticker = ? AND shares > 0` before Layer 0 (daily loss limit). BUY vetoed with "L-1.5: Already long {ticker} ({shares}sh) — no pyramiding" if position exists. Short positions (shares < 0) are NOT vetoed — BUY to cover a short is legitimate. Fail-open on DB error (warning logged, trade allowed) so DB unavailability never permanently blocks all BUYs. Root cause confirmed: CALM 4×, PNW 3×, FUBO 2×, NTST 2×, ONB 2× duplicate BUY orders in `order_log`.
- [x] **`PER_CYCLE_BUY_CAP = 3`** — `src/ibkr_worker.py`: max 3 `combined_buy` signals per `run_once()` cycle. Counter increments after `fired += 1`; excess signals call `release_dedup()` (rolls back the daily cap claim) and skip via `continue` — ticker retries on the next 5-min cycle. Prevents the "9 signals in 4 minutes" flood pattern while still allowing recovery next cycle.
- [x] **SUBMITTED Telegram condensed** — `src/ibkr_worker.py`: `result["message"]` from `order_manager._format_submitted_message()` replaced with a 1-liner `✅ {ticker} {shares}sh @ ${price:.2f} | #{order_id}`. The signal message already carries entry/stop/target/cost basis — the SUBMITTED echo was duplicating everything. VETOED orders still send their full veto reason.

**Test baseline:** 348 passed, 5 pre-existing failures — identical to Exit Strategy sprint baseline.

### Pre-Live Safety Hardening — 2026-08-04

3-agent comprehensive audit before switching to live IBKR trading. 10 fixes across order sizing, signal gates, crash safety, and DB integrity.

- [x] **Portfolio value passed to sizing** — `src/order_manager.py`: `submit()` now fetches `self.position_tracker.get_portfolio_value()` and passes it to `evaluate_trade()`. Previously hardcoded at `$100,000` — position sizes were wrong for any other account size. Falls back to `$100k` if tracker unavailable.
- [x] **SELL closes full position** — `src/order_manager.py`: `shares = held` (was `shares = min(engine_sizing, held)`). A Supertrend bearish flip means trend reversed — partial exit (risk-based sizing ≈10% of position) left 90% exposed with 24h dedup blocking re-entry. Now always closes the entire held position.
- [x] **`get_portfolio_value()` zero fallthrough** — `src/position_tracker.py`: IBKR returns `net_liquidation=0` pre-market and briefly after reconnect with no exception. Now treats `value ≤ 0` as a miss and falls through to DB fallback (yesterday's NLV) — prevents all trades being vetoed by Layer 0 during pre-market signal window.
- [x] **daily_pnl paper→live transition guard** — `src/position_tracker.py`: when fallback delta `> 50% of current NLV`, it is discarded with a WARNING (paper→live NLV jump, e.g. +$143k, disabled Layer 0 entirely on first live day). Live day_pnl now defaults to 0 until IBKR populates it.
- [x] **BUY signal gate tightened to 09:30 ET** — `src/ibkr_worker.py`: `_is_signal_hours(signal_type)` now takes the signal type. BUY entries require 09:30–20:00 ET (regular session open). SELL exits remain 04:00–20:00 ET so exits are never suppressed in pre-market. Pre-market BUY orders had stale LMT prices from the prior session close and thin liquidity.
- [x] **`_check_time_stops()` dedup** — `src/ibkr_worker.py`: before each zombie SELL, checks `order_log WHERE status='SUBMITTED' AND action='SELL'`. Prevents submitting a second full-position SELL every 4 hours (could create a short in live if IBKR accepts both).
- [x] **`_check_time_stops()` order-before-place** — `src/ibkr_worker.py`: `order_log` INSERT with `ibkr_order_id=NULL` written BEFORE `place_limit_order()`. On crash between place and write, the order is now trackable and reconcilable. After successful place, row is updated with real `order_id`.
- [x] **`_check_score_deterioration()` order-before-place** — `src/ibkr_worker.py`: same pattern — `order_log` INSERT before IBKR call, then UPDATE with real order_id.
- [x] **T1/T2 tiered exits dedup** — `src/ibkr_worker.py`: before firing T1 or T2, checks for any SUBMITTED partial SELL in `order_log`. If T1 SELL is pending, T2 is skipped for this cycle — prevents computing T2 on a stale (pre-T1-fill) share count that would result in T1+T2 = 90% of original (potential short).
- [x] **T1/T2 order-before-place + pre-update exit_tier** — `src/ibkr_worker.py`: `exit_tier` column and `order_log` row both written BEFORE `place_limit_order()`. Crash between place and DB write no longer leaves either an un-tracked order or a tier that re-fires.

**Remaining known issues (not blocking live, mitigated by bracket STP):**
- `modify_stop_order()` matches first STP SELL by ticker — ambiguous if multiple STPs exist. Requires `stop_order_id` column in `order_log` to fix fully (schema change).
- `record_fill()` not idempotent on duplicate fill events — rare but can corrupt an older forward_signals row.
- Sub-hour worker restart can replay `bars_ago==1` flip before dedup is established — narrow window, Layer -1.5 partially mitigates.
- Bracket stop-leg rejection from IBKR is async (ib_async limitation) — rollback code is dead in practice.

**Test baseline:** 348 passed, 5 pre-existing failures.

### Live-Readiness Audit — 2026-08-05

Pre-live forensic audit of paper trading. **Headline finding: the `combined_buy` signal has no
statistically detectable alpha.** 145 matured BUY signals benchmarked against SPY over identical
holding windows: 7d excess **−0.20%** (t=−0.26), 14d **+0.40%** (t=+0.41), 30d **+2.14%** (t=+1.15).
No horizon reaches |t| > 2; beat-SPY rate 49.7% / 55.6% / 54.4%. The reported 55.9% win rate was
never benchmarked — it was measuring market beta. `run_forward_digest` still reports raw win rate
only; that is the metric that hid this for three months.

Five execution defects found and fixed. All were silent — none produced an error anyone saw.

- [x] **Startup reconciliation destroyed fill state** — `_reconcile_orders_on_startup()` marked every
  SUBMITTED row missing from IBKR's open orders as ERROR, without checking whether it had filled.
  The watchdog restarts the worker on every clean exit, so this ran constantly and was the main
  producer of 30 bogus ERROR rows (14 for tickers still held). Now looks the order up in the broker's
  execution history and marks it FILLED; anything unexplained is LEFT SUBMITTED. Never destroys state.
- [x] **`ib.fills()` is session-scoped** — `IBKRConnection.get_executions()` added in
  `src/ibkr_realtime.py`, using `reqExecutions()` (broker-side, survives reconnects) merged with
  in-session `fills()`. `_periodic_fill_sweep()` now uses it. A missing order is no longer ERROR by
  default: `FILL_SWEEP_UNKNOWN_GRACE_SECS = 24h` grace, and if execution data is unavailable entirely
  the status is left untouched. A stuck SUBMITTED is cheaper than a false ERROR that erases a fill.
- [x] **Time stop measured from the LAST fill** — `_check_time_stops()` used
  `ORDER BY created_at DESC`, so every pyramid add restarted the clock and no scaled position could
  ever age into a time stop. CALM (first buy 2026-06-26, last add 2026-07-22) read as 9 trading days
  against the 15-day threshold. Now `ASC`, plus a fallback to the earliest BUY of any status for
  positions whose FILLED rows were corrupted (RRR/TRS/LCII had none and were invisible).
- [x] **Exit layer was gated behind the entry queue** — `run_once()` did `if not queue: return 0`
  before the exit block, making trailing stops, time stops, tiered exits and the fill sweep
  conditional on there being something new to buy. A quiet scan silently disarmed every stop.
- [x] **Long-only invariant enforced** — `OrderManager._pending_sell_shares()` subtracts in-flight
  SELLs: `sellable = held − pending`, veto at ≤ 0. `ibkr_worker._has_pending_sell()` added to tiered
  exits (previously matched only `notes LIKE 'T% partial exit%'`) and to score-deterioration exits
  (previously had no in-flight check at all while submitting a FULL-position SELL). Prevents two exit
  paths from each sizing against the same stale `ibkr_positions` share count.

**On the apparent oversell:** `order_log` showed KSS 287 bought / 309 sold, GTY 143/146, OMC 61/63,
which reads as a naked short. It is NOT conclusive — the corrupted ERROR rows understate purchases in
the same accounting (KSS has 559 shares of ERROR buys). The share ledger is unreliable in both
directions until fill tracking has been correct for a full cycle. The guards above are justified as
prevention, not as proof a short occurred.

**Order-log repair:** only 7 of the 30 bogus ERROR rows can be proven to have filled (share
reconciliation closes exactly with them and not without: CBT ×2, FUBO, LCII, RRR ×2, TRS). The other
23 involve positions since closed and are unreconstructable — left as ERROR rather than guessed. Real
fill prices are unrecoverable (IBKR serves ~24h of executions), so repair sets status only and leaves
`fill_price` NULL rather than substituting limit prices.

Tests: `tests/test_exit_and_longonly_fixes.py` — 13 tests. Baseline **361 passed, 5 pre-existing
failures** (`test_pnl_digest_fixes.py`, unrelated).

### 🔴 Short-Position Blindness — 2026-08-05 (most severe defect found)

**The account held 14 real short positions worth −$2.37M against an $853k NLV, and not one
of them existed in `ibkr_positions`.** Unrealized loss on them was ≈ −$121k (IT −$65k,
EXPE −$31k, ITRI −$27k). This is a long-only bot.

Root cause, one line — `position_tracker.sync_positions()`:

```python
# Skip short/phantom positions — we never intentionally short;
# negative shares are paper-account artifacts from pre-position-gate era.
positions = {t: d for t, d in positions.items() if d.get("shares", 0) > 0}
```

The comment's assumption was false. Because the rows were dropped before the upsert, and
because the follow-up `DELETE ... WHERE ticker NOT IN (...)` then treated them as closed,
the shorts were invisible to **everything**: every veto layer (all keyed on `shares > 0`),
the `/positions` Telegram command, `alert_monitor`, and any audit that read the table.
It also explains the daily P&L anomaly — NLV fell $1,035,074 → $853,301 in a day while the
visible long book was only $178k, which is impossible from marks alone.

It also settles an earlier question: `order_log` showed KSS 287 bought / 309 sold and that
was walked back as "not conclusive". It was conclusive — KSS was short 13,247 shares.

Fixes:
- [x] **Shorts are recorded, not filtered.** `sync_positions()` persists negative-share rows.
- [x] **`_raise_short_alarm()`** — a short in a long-only bot halts trading:
  `order_manager.set_paused(True)` re-applied every cycle (never throttled), plus a Telegram
  alarm listing each position, throttled to once per 6h per ticker-set so the 5-minute sync
  loop cannot flood. It deliberately does **not** auto-cover: unwinding is a human decision.
- [x] **`get_current_exposure()` is long-only by contract** — returns 0.0 for a short. It
  feeds the Layer −1 SELL veto, which treated "exposure != 0" as "there is something to
  sell"; reporting a short there would have let a SELL through and deepened it.
- [x] **Layer −1 uses `exposure <= 0`** rather than `== 0` as belt-and-braces.

`tests/test_short_detection.py` — 10 tests. Baseline **415 passed**, 5 pre-existing failures.

**Operator action this does NOT cover:** the 14 existing shorts must be closed manually in
TWS. The code prevents recurrence and forces a halt; it does not unwind an existing book.

### Trigger Backtest — 2026-08-05

`src/trigger_backtest.py` + `run_trigger_backtest.py` added. This is the validation
capability the project never had — CLAUDE.md previously named forward paper trading as the
source of truth, which at ~65 signals/month cannot resolve an edge below ~1%/trade in under
a year. The backtest replays the **exact production trigger** (`trend_series()` mirrors
`src/supertrend.py` line for line; `tests/test_trigger_backtest.py` walks every bar asserting
the two agree) over ~3 years of hourly bars.

```bash
python run_trigger_backtest.py --tickers 250        # ~90s, writes data/backtest_cache/
```

**Result — 17,810 flips, 242 tickers, 2023-09 .. 2026-07, excess vs IWM:**

| horizon | naive t | non-overlap + month-cluster | verdict |
|---|---|---|---|
| 7d | +1.65 | **+1.31** | no edge |
| 14d | +3.04 | **+1.58** | no edge |
| 30d | +4.89 | **+2.27** (mean +0.70%) | marginal edge |

Naive t-stats are inflated: 30-day windows fired days apart share most of their holding
period, so signals are not independent observations. Always report the clustered figure.

**The actionable finding: the edge exists only at ~30 days, and the system exits in days.**
It harvests at the horizon where there is nothing and exits before the horizon where there is
something. This is the same shape as the forward data (30d was its best horizon too) and
explains the near-zero realized expectancy.

**Entry filters do not help.** `close > SMA50` looked best naively (30d t=+4.42) but under the
strict treatment it *underperforms* the unfiltered trigger (t=+1.52 vs +2.27). Adding
SMA200/RSI/regime/price/ADV filters costs sample without adding excess. Do not add them.

Sensitivity: drop thinnest month t=+2.04 · drop best+worst t=+2.61 · 2024-onward t=+2.03 ·
2025-onward t=+1.41 (n.s.) · 63% of months positive · median monthly excess +0.55%.
Decay by year: 2023 +1.16% → 2024 +0.61% → 2025 +0.45% → 2026 +0.40%.
Regime split: SPY above SMA200 +0.62% (t=5.02) · below SMA200 +0.13% (t=0.34) — no edge in
a falling tape. Net of 0.2–0.3% round-trip friction the edge is +0.2% to +0.4% per 30-day hold.

**⚠️ Survivorship bias — the largest unquantified weakness.** The universe comes from
`scan_results`, i.e. tickers the bot scanned in 2026, replayed back to 2023. Delisted and
collapsed names are structurally absent. Correcting this needs point-in-time index membership,
which the project does not store. Treat +0.70% as an upper bound, not an estimate.

### Exit Simulation — 2026-08-05 (supersedes the horizon result above)

`run_exit_simulation.py` + `simulate_exits()` replay each signal **bar by bar** under
competing exit policies, with pessimistic fills (a bar that gaps through the stop fills at
the OPEN; when one bar touches both stop and target, the stop is assumed first) and the
benchmark measured over the **realised** holding window.

**The +0.70% horizon edge does not survive contact with a stop.** The horizon study measured
unmanaged close-to-close returns. Once a real exit policy is applied:

| exit regime | hold | net | bench | excess | t |
|---|---|---|---|---|---|
| **A — live: Supertrend flip exit** | 7.1d | +0.32% | +0.32% | **+0.00%** | +0.04 |
| B — stop 2ATR + 30d time | 21.1d | +1.22% | +1.16% | −0.11% | −0.35 |
| C — stop 3ATR + 30d time | 25.4d | +1.59% | +1.45% | −0.11% | −0.32 |
| D — 3ATR stop, trail 3ATR after +5% | 21.6d | +1.05% | +1.22% | −0.14% | −0.56 |
| E — stop 3ATR + 60d time | 43.0d | +3.13% | +2.55% | −0.12% | −0.22 |
| F — stop 2ATR, target 4ATR, 30d | 17.5d | +0.71% | +1.00% | −0.27% | −1.51 |
| G — **no stop**, 30d time (reference only) | 30.7d | +2.20% | +1.83% | +0.50% | +1.63 |

Every regime carrying a stop lands at zero or negative excess. The only positive variant
holds for 30 days with **no stop at all**, is not statistically significant (t=1.63), and is
not tradeable — it accepts unbounded single-name loss. The 30-day edge comes from a fat right
tail (win rate 47.6% with a positive mean), and a stop truncates future winners faster than it
saves losers.

**Regime A reproduces live behaviour and lands at exactly +0.00% excess (t=0.04)** — the same
near-zero expectancy three months of paper trading produced. The simulator independently
arrives at the observed live result, which is the strongest calibration evidence available.
A +0.32% net against a +0.32% benchmark is uncompensated single-name risk.

Two methodology bugs were found and fixed while producing this; both had inverted the answer:
- **Stop ATR timeframe.** Stops were sized from *hourly* ATR while production uses *daily*
  (`execution_engine._get_atr()` → `history(period="30d")`, yfinance daily default). The
  hourly stop is several times tighter, so ordinary intraday noise took it out within ~2 days
  and produced a false "stops destroy the edge" signal. `ExitConfig.stop_timeframe` now
  defaults to `"daily"` and **skips** a signal rather than silently falling back.
- **Outcome-selected clustering.** `clustered_stats()` spaced non-overlapping trades by the
  *current* trade's holding period, so a 3-day stop-out needed only 3 days of clearance while
  a 30-day winner needed 30 — preferentially admitting fast losers. Regime A's true +0.00%
  excess was being reported as −1.03%. Non-overlap is now defined by the previous kept
  trade's **exit**, which is outcome-blind. `tests/test_exit_simulation.py` pins both.

### Signal Panel — 2026-08-05

`src/signal_library.py` + `run_signal_panel.py`. `run_backtest(signal_fn=...)` is now
signal-agnostic, so a new idea is one function, not a new backtest. Six pre-committed
candidates were tested against the production trigger as control.

**Nothing survives.** Threshold for 7 tests is |t| > 3.07; the best candidate reaches +1.42.

| signal | signals | 30d excess (naive t) | tradeable excess | t |
|---|---|---|---|---|
| supertrend_flip (control) | 17,810 | +0.57% (+4.89) | −0.11% | −0.32 |
| golden_cross | 820 | +1.33% (+1.68) | +1.25% | +0.92 |
| breakout_52w + volume | 2,526 | +2.41% (+4.58) | +0.68% | +1.25 |
| donchian_20 | 11,479 | +0.70% (+4.70) | +0.01% | +0.05 |
| momentum_12_1 | 3,957 | +1.16% (+4.19) | +0.12% | +0.25 |
| rsi_dip_in_uptrend | 1,310 | +0.23% (+0.61) | +0.33% | +0.50 |
| gap_up_continuation | 986 | +2.92% (+2.63) | +1.12% | +1.42 |

The pattern is the finding: naive 30-day excess looks strong for several candidates
(t = 4.19 … 4.70) and **every one collapses to ~0 under a real stop**. This is not a
property of the Supertrend trigger — it is a property of this whole family of long-only
momentum/breakout entries on this universe. Changing the trigger does not help.

`tradeable` = stop 3ATR(daily) + 30d time, 0.20% round-trip friction, month-clustered on
outcome-blind non-overlapping trades.

**Three harness bugs were found by these tests, each of which had inverted a result:**
- **Daily signals were gated out entirely.** The intraday 09:30–20:00 ET gate compares
  `ts.hour`, and a daily bar is stamped at midnight — so all six candidates reported *zero*
  signals rather than being evaluated. The gate now applies only when `ts != ts.normalize()`.
- **Signals predating the price series produced fake year-long holds.** Daily signals reach
  back 5y while hourly bars cover ~730d, so a 2022 signal mapped to hourly bar 0 and "held"
  until the series began: 98-day average holds under a 30-day stop, and hugely inflated
  returns. `simulate_exits()` now walks daily signals on daily bars and skips any signal
  more than 5 days from the nearest bar. Before this fix `rsi_dip_in_uptrend` reported
  +5.16% excess at t=+3.33 and appeared to survive the multiple-comparison threshold.
- **Forward returns used the wrong timeframe for daily signals** — entry was a daily close
  but the return was measured from a mid-session hourly bar. Now matched to the signal's own
  timeframe.

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
