# FinancialAgent — Claude Code Context

## Project Overview
AI-powered stock scanner & financial analysis dashboard.
- **Location:** `C:/Projects/FinancialAgent`
- **Stack:** Python 3.14, Streamlit 1.52.2, SQLite, yfinance, Finnhub, Alpha Vantage, SEC EDGAR
- **LLMs:** Gemini 2.0 Flash (primary) → Groq Llama 3.3 70B (fallback) via `src/llm_client.py`
- **Run:** `streamlit run dashboard.py` → http://localhost:8501
- **Tests:** `python -m pytest tests/ --ignore=tests/test_new_apis.py --ignore=tests/test_ibkr_connection.py --ignore=tests/test_ibkr_worker_once.py` → **533 passed, 2 skipped, 0 failed** (2026-08-14). This number moves every sprint — re-run it rather than trusting this line for long. The old "5 pre-existing failures in test_pnl_digest_fixes.py" were test rot (helpers seeded with literal dates against a relative-date filter), fixed by using relative dates. **Never seed a time-filtered query with a literal date.**

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
| `dcf_valuation.py` | DCF engine (5-year FCF model). Enterprise Value → Equity Value via net-debt subtraction; CAPM cost of equity + actual cost of debt for WACC; excludes financial-sector tickers and over-leveraged companies (falls through to P/S); tiered FCF sourcing (EDGAR → yfinance multi-year → yfinance TTM → OCF−CapEx) |
| `squeeze_scanner.py` | Squeeze Score + AI Verdict |
| `borrow_fee.py` | Finviz scraper for borrow fee estimate. Failure results cached 5 min (not 2h) |
| `price_alert_monitor.py` | Supertrend 15m/1h/daily + price target + volume spike — daemon thread |
| `telegram_news_digest.py` | Market digest + Portfolio news |
| `database.py` | SQLite CRUD + auto migration. WAL-hardened (see DB Concurrency below) |
| `watchlist_manager.py` | Alert logic — score threshold, price levels, portfolio stop/target, score delta. `price_change` gated to ET 04:00–20:00 (`zoneinfo`) |
| `score_alert.py` | Score jump/drop alerts for ALL scanned tickers (not just watchlist) — 24h cooldown, shared alert types with watchlist_manager |
| `llm_client.py` | Gemini → Groq fallback. `_try_groq()` wrapped in try/except — Groq errors raise `RuntimeError` instead of propagating raw |
| `market_feed.py` | Live indices + macro events. `get_upcoming_macro()` returns approximate weekly schedule — events marked `*` as disclaimer |
| `news_impact_analyzer.py` | 3-layer LLM news analysis |
| `macro_signals.py` | Macro signals |
| `telegram_notifier.py` | Telegram send logic — 4000-char truncation guard |
| `scan_worker.py` | Background scan thread (manual dashboard scans, 6-thread pool) |
| `index_loader.py` | iShares index/sector loader — falls back to Wikipedia for S&P 500 when iShares returns HTML; CACHE_TTL=30d |
| `catalyst_scanner.py` | Catalyst Scanner engine — explosion score, PDUFA, unusual options |
| `options_flow.py` | Options chain data, PCR, unusual call/put activity (yfinance). Contracts with OI=0 and volume<500 are skipped; volume≥500 uses `volume/100` ratio instead of a sentinel |
| `auto_watchlist_agent.py` | Auto-adds squeeze/catalyst/momentum/supertrend candidates to watchlist with Telegram summary. `alert_score` uses `AUTO_WL_SCORE_ENTRY` (70) from hysteresis.py, consistent across sources. **Capacity rotation** (`_evict_for_capacity()`, added 2026-08-14): when `watchlist_policy.max_items_total` is reached, the weakest eligible AUTO-added incumbent (3-day avg score, must have cleared `AUTO_WL_MIN_HOLD_DAYS`) is evicted to make room instead of silently dropping the candidate — same "replace weakest with better" logic as `scheduler.run_weekly_rotation()`, triggered on demand instead of weekly. Scored candidates (squeeze/catalyst/momentum) must strictly beat the weakest incumbent's score; unscored candidates (supertrend — no composite gate by design) can only claim a slot whose incumbent has already fallen below `AUTO_WL_SCORE_EXIT` (40), so the least-vetted source can't bump a ticker still earning its place. See [Incident Archive](#2026-08-14--watchlist-pinned-at-capacity-zero-new-adds) for the root cause this fixed. |
| `ibkr_realtime.py` | IB Gateway connector via `ib_async` — historical bars + live snapshot + bracket order placement (GTC legs) + `modify_stop_order()` / `resize_sell_orders()` (in-place order modification) + position/account queries for US stocks |
| `ibkr_worker.py` | Standalone daemon (Python 3.13, `.venv313`) — runs Supertrend(1H) every 5 min on the monitoring queue, fires combined alerts + submits orders via `order_manager` + syncs positions/daily P&L via `position_tracker`. `sync_positions()` runs at the START of each cycle. `bars_ago != 1` check in `_check_ticker()` — only fires on the exact bar that flipped. `_is_signal_hours(signal_type)` gate — BUY requires 09:30–20:00 ET, SELL 04:00–20:00 ET. Subscribes to `ib.orderStatusEvent` for fill/cancel callbacks; startup reconciliation + periodic fill sweep (every 30 min, `get_executions()` — broker-side, survives reconnects). Hosts `TelegramCommandHandler` thread. Windows named mutex singleton (`Global\FinancialAgent_IBKRWorker_Singleton`) + `multiprocessing.freeze_support()`. `PER_CYCLE_BUY_CAP = 3`. All 4 exit checks (`_update_trailing_stops`, `_check_time_stops`, `_check_tiered_exits`, `_check_score_deterioration`) route through `order_manager.submit_exit()`, never call `place_limit_order()` directly. `_reconcile_resting_sell_orders()` — cancels resting SELL orders with no backing position, resizes ones larger than the position they protect. |
| `monitoring_queue.py` | Source of truth for "which tickers get real-time IBKR monitoring" — scanner score ≥ 65 (`SCANNER_MIN_SCORE`) + manual watchlist + recent BUY alerts (72h) + liquidity gate (hysteresis: enter $5M / exit $3M ADV). Queue state persisted to `monitoring_queue_snapshot` DB table; `_persist_queue()` only called when `apply_liquidity_gate=True`. |
| `order_manager.py` | Wraps IBKR order calls; runs execution_engine veto checks before submission; logs every attempt to `order_log` DB table. `submit()` fetches live `portfolio_value` from `position_tracker` (falls back to $100k) and `portfolio_tickers` from `ibkr_positions` for the sector veto. SELL always closes the FULL held position (never a partial risk-sized exit). `submit_exit()` is the single funnel all software-driven exits (time stop, tiered exit, score deterioration) must go through — enforces the trading pause, `shares ≤ held − already-working`, and `order_log` written before the broker call. paper_mode=True default; live requires `IBKR_LIVE=true`. Module-level `_trading_paused` flag, toggled by Telegram `/pause`/`/resume`. |
| `position_tracker.py` | Syncs IBKR positions (including **short/negative-share rows — never filtered**) to `ibkr_positions` every 5 min; records `daily_pnl` once per day (gated to after 09:30 ET, `INSERT OR REPLACE`). `_account_is_ready()` requires `net_liquidation > 0` before an empty position list is trusted to mean "flat" (see IBKR operational trap below). `_raise_short_alarm()` — a short in this long-only bot pauses trading (re-applied every cycle) and sends a throttled Telegram alarm; never auto-covers. `get_current_exposure()` is long-only by contract (returns 0.0 for a short). `get_portfolio_value()` / `get_daily_pnl()` try IBKR first, fall back to DB (`ORDER BY date DESC LIMIT 1`); a paper→live NLV jump >50% is discarded as a fallback delta. |
| `signal_combiner.py` | Supertrend 1H flip → BUY/SELL alert; enforces daily cap (10), 24h dedup. BUY and SELL both have **no composite-score gate** — any monitoring-queue ticker alerts on a bullish flip; SELL is gated only on an open position (`ibkr_positions WHERE shares > 0`). `_try_claim_dedup()` does SELECT+INSERT atomically in one DB connection. |
| `forward_signals.py` | Records every fired alert with entry price + data quality check (`data_quality_flag='SUSPECT'` for the IBKR $105 placeholder or >20% divergence from scan price). `record_fill()` cross-checks `order_log.status` and skips CANCELLED orders. Daily 18:00 job fills `price_after_{7,14,30}d`; weekly Friday 20:00 Telegram digest with win-rate metrics — **raw win rate only, not benchmarked against SPY**, see [Live-Readiness Audit](#2026-08-05--live-readiness-audit-no-measurable-alpha). |
| `earnings_sentiment.py` | Tier 1 = Finnhub EPS surprise history (free), Tier 2 = LLM transcript analysis (paid). Score 0–5 added to `stock_scorer.py` bonus band. EDGAR fallback when Finnhub is empty (`get_eps_yoy_growth()`, `source='edgar_eps_yoy'`). |
| `hysteresis.py` | Central helper `passes_hysteresis(current, in_set, entry, exit)` + threshold constants (composite, SI, liquidity, watchlist score) — see [Hysteresis Bands](#hysteresis-bands-srchysteresispy) below |
| `stock_forecaster.py` | Ensemble forecaster (ARIMA/MA/ES/MLP). Constructor accepts `point_in_time: datetime` — strictly truncates input to ≤ point-in-time to prevent backtest look-ahead bias. `MLPRegressor.early_stopping=True` uses a shuffled validation split — non-ideal for time series, intentionally unchanged (flagged in code). |
| `news_catalyst_monitor.py` | Background thread — checks news every N min; freshness gate skips articles older than `max_article_age_minutes` (default 45, config key `news_catalyst_max_article_age_minutes`) |
| `run_dashboard_tunnel.py` | Cloudflare Quick Tunnel launcher; sends URL on startup + daily heartbeat at 08:05 IL. `_tunnel_healthy()` checks both local cloudflared metrics AND public DNS resolution (`socket.getaddrinfo`) — catches expired quick-tunnel URLs where cloudflared stays running but DNS is deregistered; 3 consecutive failures trigger a tunnel restart + new URL. |
| `run_tunnel_watchdog.py` | Watchdog for `run_dashboard_tunnel.py` — auto-restarts on crash or clean exit, Telegram on startup/restart/crash (rate-limited to 1/5min). Registered as `FinancialAgentTunnelWatchdog` Windows Task. Stop with `stop_tunnel.flag` sentinel |
| `supertrend.py` | Supertrend calculation (ATR-based, Wilder EMA, identical to TradingView Pine Script) — used by `ibkr_worker.py` and `price_alert_monitor.py`. **`scan_supertrend_universe()`** (added 2026-08-14) batch-downloads daily OHLCV (yfinance, ~0.2s/ticker) across the full scan universe and returns every ticker with a fresh bullish flip (`bars_ago==1`), with **no composite-score gate** — mirrors a bare TradingView `alertcondition(buySignal)`, closing the coverage gap left by `monitoring_queue.py`'s `SCANNER_MIN_SCORE=65` filter. |
| `market_regime.py` | BULL / CAUTION / BEAR regime based on VIX thresholds (20/28) + SPY vs SMA200 (`_SPY_HISTORY = "1y"`, ~252 trading days); used by `execution_engine.py` for position sizing and stop adjustments |
| `execution_engine.py` | Trade decision engine (Layers -1.5 through 6): daily loss limit (Layer 0), hard veto, confluence check, position sizing scaled by market regime, time-of-day flag, sector exposure guard. **Layer -1: SELL veto** if `exposure <= 0` (no open position, belt-and-braces against a short reporting as "exposure≠0"). **Layer -1.5: already-long BUY veto** — no pyramiding; short positions (shares<0) NOT vetoed since BUY-to-cover is legitimate. BEAR regime veto is BUY-only (`check_hard_vetos(signal_type=...)`) — SELL exits always allowed. Fail-open on DB error. |
| `momentum_scanner.py` | 5-factor momentum score (Price ROC, Relative Strength vs SPY, MA Stack, RSI zone, Volume Surge); vectorized pandas + `yf.download` batch (~0.2s/ticker at scale); runs every 30 min as daemon thread |
| `gap_scanner.py` | Two independent scanners feeding the Premarket Gap Alert and Opening Print Alert one-shot Telegram jobs — see Scheduler Jobs below. `scan_premarket_gaps()` uses **Massive/Polygon per-ticker REST** (`MASSIVE_API_KEY`, `ThreadPoolExecutor` fan-out, `massive_max_workers` in `scheduler_config.json`) — rewritten 2026-08-16 after yfinance was found to always report premarket volume as exactly 0 in production (see Incident Archive). `scan_opening_prints()` is unchanged, still `yf.download` 1m regular-session bars — separately verified working, no reason to add cost there. Informational-only: not called from `auto_watchlist_agent.py`, `order_manager.py`, or `ibkr_worker.py`. Added 2026-08-15 after the NMAX gap-catch investigation (see Incident Archive). |
| `long_setup_scanner.py` | 5-factor long setup scanner (RSI zone, MACD crossover, Volume surge, MA alignment, Momentum); daily 09:30; auto-adds top candidates to watchlist |
| `opportunity_tracker.py` | Records every BUY signal as opportunity with T1/stop targets; daily 18:00 fills outcomes; weekly Friday 20:00 Telegram digest with win-rate |
| `alert_monitor.py` | Daily health-check agent at 09:30 — detects noisy alerts, dead threads, portfolio drawdowns >8%; sends Telegram health report. Uses `get_connection()` (WAL-safe, `with` block — no leaked connections). |
| `telegram_command_handler.py` | Two-way Telegram — polls `getUpdates` every 30s; commands: `/status`, `/positions`, `/pause`, `/resume`, `/cancel <TICKER>` (ticker validated `re.fullmatch(r"[A-Z]{1,6}")`); security: only responds to `TELEGRAM_CHAT_ID`; offset persisted to `telegram_command_state` DB table. `/status` reads queue size from `monitoring_queue_snapshot` and P&L from `daily_pnl` (no live IBKR call — avoids hangs). |
| `finnhub_client.py` | Finnhub API wrapper — earnings surprises, transcript list/content |
| `edgar_fcf.py` | SEC EDGAR XBRL provider — free, no API key. `get_edgar_fcf_median`, `get_revenue_cagr`, `get_interest_coverage` (zero-debt → 100.0 cap, not None), `get_current_ratio`, `get_eps_yoy_growth`. 24h in-memory cache, single shared cache (no duplicate SEC fetches). Rate: 0.12s delay between requests. |

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

All binary thresholds in the project use **entry/exit deadbands** instead of single cutoffs, to prevent thrashing on values that oscillate near the boundary:

```python
passes_hysteresis(current_value, previously_in_set, entry_thr, exit_thr) -> bool
```

| Threshold | Entry | Exit | Source |
|---|---|---|---|
| Auto-watchlist score | 70 | 40 | + min-hold 3 days; + 7-day re-entry cooldown unless ≥ 75 |
| Composite-for-BUY | — | — | **gate intentionally removed 2026-06-03** — Supertrend flip is sole trigger for BUY (user preference, symmetric with SELL) |
| Composite-for-SELL | — | — | **gate removed 2026-08-02** — SELL gated only on open position; a `SELL_MAX_SCORE=55` gate was tried 2026-07-15 and reverted because it blocked 100% of exits (all positions score ≥ 70) |
| Squeeze SI% | 15 | 10 | filter for squeeze pool |
| Catalyst SI% | 10 | 5 | filter for catalyst pool |
| Liquidity ADV ($) | $5M | $3M | monitoring_queue gate |

**Auto-exit cooldown:** when an auto-added ticker is removed, a `watchlist_alerts` row of type `auto_exit_cooldown` is written. Re-add is blocked for 7 days unless score ≥ 75.

**Capacity rotation** (2026-08-14) uses the same entry/exit asymmetry in spirit: a scored candidate must strictly beat the weakest incumbent to evict it; an unscored candidate can only claim a slot already below the 40 exit bar. See `auto_watchlist_agent.py` above.

---

## Backtest Integrity — Point-in-Time Forecasting

`src/stock_forecaster.py` is the only model that could leak future data into past-decision contexts.

```python
StockForecaster(data, point_in_time=datetime(...))   # truncates data to <= pit
```

When `point_in_time` is set, all rows after it are dropped before any model fits — critical for any historical-replay or audit code path. **Live scanning** uses the default (`None`).

---

## DB Concurrency — WAL Hardening (`src/database.py`)

Two concurrent writers: `scheduler.py` (main `.venv`) and `ibkr_worker.py` (`.venv313`). Hardened with:

| PRAGMA | Value | Why |
|---|---|---|
| `journal_mode` | `WAL` | Readers don't block writers |
| `synchronous` | `FULL` | Corruption-safe on Windows Docker / network FS |
| `busy_timeout` | `30000` ms | Wait when another writer holds the lock |
| `wal_autocheckpoint` | `4000` pages | Bound WAL file to ~16 MB |
| `auto_vacuum` | `INCREMENTAL` | Reclaim space without exclusive `VACUUM` lock |

**Do NOT add `isolation_level=None`** to `get_connection()` — it silently enables autocommit and breaks every `with conn:` transactional block.

High-frequency writes (`save_result`, `watchlist_save_alert`, `record_signal`, `update_outcomes`) are wrapped with `@retry_on_busy` (5 attempts, exponential backoff). `prune_old_data` uses `PRAGMA incremental_vacuum(1000)`, never a full `VACUUM`.

Verified by `tests/test_db_wal_concurrency.py` — 4 writers + 1 reader, 1000 writes, 0 errors at 139 writes/sec aggregate.

---

## ⚠️ Operational trap: logging into Client Portal kills the Gateway session

IBKR permits one active session per username. **Logging into Client Portal (or TWS) displaces the IB Gateway session.** The failure is silent and easy to misread:

- the TCP port stays open and `ib.connect()` still succeeds
- every data request then times out — `positions request timed out`, `account updates for <ACCT> request timed out`, `executions request timed out`
- `Warning 2151, reqId -1: Positions info is not available yet`
- **`ib.positions()` returns an EMPTY LIST rather than raising**

That last point is the dangerous one. An empty response is indistinguishable from a flat account unless something that cannot legitimately be zero is checked too. On 2026-08-05 this was misread as "the account reset succeeded and is clean" while 15 shorts worth −$2.37M were still open, and `sync_positions()` deleted all 37 rows from `ibkr_positions` on the strength of it. Guarded since by `PositionTracker._account_is_ready()` (requires `net_liquidation > 0` before an empty list is allowed to clear the table).

**Symptoms are identical to a post-reset Gateway that has not finished logging in**, which is how it was misdiagnosed twice on 2026-08-05/06 — including one unnecessary `docker restart` of the Gateway.

**Before concluding anything about positions, check the sync age.** A reading is only trustworthy when `MAX(last_synced)` is within the last few minutes:

```bash
python -c "import sqlite3,datetime;c=sqlite3.connect('data/financial_agent.db');c.row_factory=sqlite3.Row;r=c.execute('SELECT COUNT(*) n,COALESCE(SUM(CASE WHEN shares<0 THEN 1 ELSE 0 END),0) s,MAX(last_synced) t FROM ibkr_positions').fetchone();print('rows',r[0],'shorts',r[1],'synced',r[2])"
```

Fix: log out of Client Portal. The Gateway recovers on the next cycle without a restart.

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

Both watchdogs use `CREATE_NO_WINDOW` flag — no CMD windows appear. Registered as Windows Scheduled Tasks: `FinancialAgentWatchdog`, `FinancialAgentIBKRWorker`. Neither watchdog auto-restarts its child if it exits with code 0 UNLESS that behavior is explicitly coded (the IBKR worker watchdog does, on a 60s `CLEAN_EXIT_DELAY`; the **scheduler watchdog does not** — a clean exit stops it permanently until manually relaunched. Confirmed live 2026-08-14: after `wsl --shutdown` killed Docker mid-session, Docker Desktop itself also did not auto-recover and needed a manual relaunch of `Docker Desktop.exe`).

**⚠️ Python Launcher trap (fixed 2026-06-25):** On Windows, `.venv313\Scripts\python.exe` is NOT the real Python 3.13 interpreter — it is `py.exe` (the Windows Python Launcher, ~249 KB), which always spawns the real interpreter as a child, producing **two processes** per worker invocation. Fix: `run_ibkr_worker_watchdog.py` reads `pyvenv.cfg` to find the base interpreter and invokes it directly, activating the venv via env vars instead of relying on the launcher:
```python
env["VIRTUAL_ENV"] = str(VENV313_DIR)
env["PATH"] = venv_scripts + os.pathsep + env["PATH"]
env["__PYVENV_LAUNCHER__"] = str(VENV313_DIR / "Scripts" / "python.exe")
env.pop("PYTHONHOME", None)
```
**Do NOT change `PYTHON` back to `VENV313_DIR / "Scripts" / "python.exe"` — that reverts the two-process bug.**

**Orphan worker prevention:** watchdog writes `ibkr_worker.pid` after `Popen()`, deletes it after `proc.wait()`. On next start, `_kill_orphaned_worker()` reads the PID file and `TerminateProcess()`s any leftover worker from a previous watchdog crash. **Worker singleton mutex** (`Global\FinancialAgent_IBKRWorker_Singleton`) is defense-in-depth on top of this.

**Gateway settings persistence:** `/home/trader/Jts` is mounted via named Docker volume `ibkr_jts`. API settings (Trusted IPs `172.18.0.1`, localhost-only unchecked, Read-Only API unchecked) survive `docker-compose down`/`up` and host restarts — auto-persist on clicking OK, no explicit Save needed.

```yaml
# docker-compose.yaml — bottom of file
volumes:
  ibkr_jts:    # preserves /home/trader/Jts across container restarts
```

---

## IBKR Order Execution

**Flow:** `ibkr_worker` detects Supertrend flip → `signal_combiner.evaluate()` fires alert → Telegram sent → `order_manager.submit()` called. Fill/cancel callbacks fire asynchronously via `orderStatusEvent`.

```
run_once():
  position_tracker.sync_positions()              ← FIRST: fresh ibkr_positions for veto checks
  for each ticker in queue:
    signal_combiner.evaluate()
      → order_manager.submit(alert)
          → if _trading_paused: return PAUSED
          → engine.evaluate_trade(signal_type=action)
              → Layer -1.5: already-long BUY veto (no pyramiding)
              → Layer -1: SELL veto if exposure<=0
              → Layer 0: check_daily_loss_limit()
              → Layer 2–6: hard veto → confluence → sizing → noise → sector
          → ibkr_realtime.place_bracket_order()        ← LMT entry + STP stop (GTC) + LMT target (GTC)
          → _format_submitted_message()
  position_tracker.record_daily_pnl()
  _reconcile_resting_sell_orders()                ← cancel/resize resting SELLs that no longer match the position

  (async) ib.orderStatusEvent → _on_order_status()
      → Filled:    _update_order_log(FILLED) + forward_signals.record_fill() + Telegram
      → Cancelled:  _update_order_log(CANCELLED)
      → Inactive:   _update_order_log(ERROR)
```

**OrderManager** (`src/order_manager.py`): fetches live `portfolio_value` and `portfolio_tickers` before evaluating; SELL always closes the FULL position; every software-driven exit (time stop, tiered exit, score deterioration) is funneled through `submit_exit()` (see [Root Cause of the Shorts](#2026-08-03--root-cause-of-the-shorts-runaway-tiered-exit)) which enforces the pause + `shares ≤ held − already-working` + order-log-before-broker-call ordering. `set_paused(bool)`/`is_paused()` toggled by Telegram `/pause`/`/resume`.

**PositionTracker** (`src/position_tracker.py`): syncs positions (shorts included, never filtered — see [Short-Position Blindness](#2026-08-05--short-position-blindness-most-severe-defect-found)) every 5 min; `record_daily_pnl()` once/day, gated to after 09:30 ET; `get_portfolio_value()`/`get_daily_pnl()` try IBKR then fall back to DB.

**Safety:** `paper_mode=True` always unless `IBKR_LIVE=true`; port 4002 (paper) vs 4001 (live) requires both the flag and `paper_mode=False`. All execution-engine vetoes run before any order touches IBKR. BEAR regime veto is BUY-only. Daily loss limit reads `max_daily_loss_pct` from `scheduler_config.json` (default 2%).

**Bracket order structure:** Parent LMT (transmit=False) → STP stop (GTC, transmit=False) → LMT target (GTC, transmit=True, triggers full bracket). The 3 `placeOrder()` calls are wrapped in try/except — if any leg fails, already-submitted legs are cancelled (no dangling unprotected parent orders).

**Reconciliation & fill sweep:** `_reconcile_orders_on_startup()` looks up broker execution history before marking a stale SUBMITTED row ERROR — it never marks a row ERROR just because it's missing from open orders (see [Live-Readiness Audit](#2026-08-05--live-readiness-audit-no-measurable-alpha)). `_periodic_fill_sweep()` (every 30 min) uses `get_executions()` (broker-side `reqExecutions()`, survives reconnects, not just the session-scoped `ib.fills()`), with a 24h grace period before an unresolved SUBMITTED row is touched.

**Two-Way Telegram** (`src/telegram_command_handler.py`, background thread inside `ibkr_worker`, polls every 30s):

| Command | Action |
|---|---|
| `/status` | Regime, monitoring queue size, open positions, daily P&L, paused state, last signal (all DB-only reads — no live IBKR call, avoids hangs) |
| `/positions` | Table of open IBKR positions |
| `/pause` / `/resume` | Toggles `order_manager._trading_paused` |
| `/cancel <TICKER>` | Cancels all open IBKR orders for the ticker |

Only responds to `TELEGRAM_CHAT_ID`. Offset persisted to `telegram_command_state`.

---

## Forward Signal Validation

Every fired BUY/SELL alert is recorded in `forward_signals` with `entry_price` at signal time. A daily 18:00 job backfills `price_after_{7,14,30}d` once horizons mature. Weekly Friday 20:00 Telegram digest:

```
📊 Weekly Forward Signals Digest (7d)
Total signals: N
Breakdown: BUY=X, SELL=Y
Avg 7D return: ±X.XX%
Win rate 7D:   XX.X%
```

**This raw win rate is never benchmarked against SPY** — see [Live-Readiness Audit](#2026-08-05--live-readiness-audit-no-measurable-alpha) for why that hid a near-zero-alpha signal for three months.

**Data quality guard:** `_check_entry_price_plausibility()` flags `data_quality_flag='SUSPECT'` for the known IBKR $105 paper-account placeholder, or entry price diverging >20% from the most recent scan price.

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
- `sec_8k` — EDGAR 8-K filings, 8 parallel workers (item-number classification, e.g. 1.01 bullish / 1.03 bearish, is NOT implemented — still open, see backlog)
- `pdufa` — BioPharma Catalyst FDA calendar (no key; cache 6h → `data/pdufa_cache.json`; reads `<thead>` headers to validate column order, falls back to hardcoded indices)

### Source Modes
Nasdaq Calendar · Watchlist + Portfolio · Manual Tickers · Index/Sector (iShares; biotech = Russell 2000 → Health Care, ~150 tickers)

### Unusual Options Signal
Reuses `src/options_flow.py → get_options_summary()`. +8 pts unusual CALL vol/OI≥3x or vol≥5000; +4 pts PCR<0.7 with no unusual calls; 0 on any failure (many small caps have no options data).

---

## DCF Engine (`src/dcf_valuation.py`)

```
Enterprise Value = Σ FCF_t/(1+WACC)^t  +  TV/(1+WACC)^n
Terminal Value   = FCF_n*(1+g) / (WACC-g)
Equity Value     = Enterprise Value − Net Debt   ← net debt subtraction (critical)
Intrinsic/share  = Equity Value / sharesOutstanding
Margin of Safety = (Intrinsic − Price) / Intrinsic * 100
```

**FCF source priority (tiered):** SEC EDGAR XBRL median of 4 annual 10-Ks → yfinance cashflow DataFrame multi-year median → yfinance TTM → `operatingCashflow − |capitalExpenditures|`.

**WACC:** Cost of equity via CAPM (`Ke = Rf(^TNX) + Beta × 5.5% ERP`, clamped 7–20%, fallback 10%). Cost of debt from actual `interestExpense/totalDebt`, falls back to tier estimate. `WACC = E/(D+E)×Ke + D/(D+E)×Kd×(1−tax)`, clamped 7–15%. Higher leverage lowers WACC (tax shield); equity impact captured separately via net debt subtraction.

**Other:** Growth = 60% historical FCF CAGR + 40% revenue/earnings proxy, clamped −10% to 25% (no artificial positive floor for declining businesses). Financial-sector and over-leveraged (`equity_value ≤ 0`) companies return None → falls through to P/S. Terminal growth 2.5%, horizon 5 years.

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
ibkr_positions:               ticker (PK), shares, avg_cost, unrealized_pnl, market_value, last_synced,
                              exit_tier — synced every 5 min from IBKR; shares can be negative (shorts)
daily_pnl:                    date (PK), day_pnl, net_liquidation, recorded_at
order_log:                    ticker, action, shares, entry_price, stop_price, target_price,
                              status (SUBMITTED/VETOED/FILLED/CANCELLED/ERROR/PAUSED), fill_price,
                              ibkr_order_id, created_at, updated_at, notes
telegram_command_state:       key (PK), value — persists Telegram getUpdates offset across restarts
llm_curated_universe:         week_of, ticker, action (keep/add/remove), rationale, created_at
```

Migration via `_migrate()` in `database.py` — adds columns without breaking data. `watchlist_alerts` doubles as a **cooldown registry** — every alert system checks it via `_alert_sent_recently(ticker, alert_type, hours)` before sending.

---

## Scheduler Jobs (`scheduler.py`)

> **Times below are from `scheduler_config.json` — NOT from code defaults.** Always check `scheduler_config.json` for the actual runtime schedule; code fallbacks exist but are routinely overridden by config.

| Job | Default Time | Function |
|---|---|---|
| Watchlist Cleanup | 08:00 | `run_watchlist_cleanup()` |
| Catalyst+SI Alert | 08:05 | `run_catalyst_alert()` |
| Premarket Gap Alert | 15:50 (~08:50 ET) | `run_premarket_gap_alert()` — full-universe scan for a real premarket gap (price move backed by actual premarket volume) vs. prior regular-session close; informational Telegram only, catches genuine overnight-news gappers |
| Opening Print Alert | 16:37 (~09:37 ET) | `run_opening_print_alert()` — full-universe scan comparing each ticker's 09:30 open print to its price ~7 min later with a volume filter; the check that would have caught NMAX (already +12-13% within its first 5-7 minutes on heavy volume) |
| LLM Universe Curation | 07:45, weekly | `run_llm_universe_curation()` — see below |
| Portfolio News | 08:30 | `run_portfolio_news()` |
| Scan + Auto-Watchlist + Breakout | 08:30, 15:00 | `run_scan()` |
| Portfolio | 09:15 | `run_portfolio_scan()` |
| Market Digest | 09:30 | `run_market_digest()` |
| Long Setups | 09:30 | `run_long_setups()` — `long_setups_enabled` |
| Alert Monitor Health Check | 09:30 | `run_alert_monitor()` |
| Watchlist | 12:00 | `run_watchlist_scan()` |
| Squeeze + SI Alert | 12:00 | `run_squeeze_scan()` |
| Weekly Rotation | Monday 08:15 | `run_weekly_rotation()` — replaces the single weakest auto-added ticker if a momentum candidate scores ≥75 and beats it |
| Forward Outcomes Update | 18:00 daily | `run_forward_outcomes_update()` |
| Opportunity Outcomes Update | 18:00 daily | `run_opportunity_outcomes()` |
| Forward Signals Digest | Friday 20:00 | `run_forward_digest()` |
| Opportunity Digest | Friday 20:00 | `run_opportunity_digest()` |
| Price Monitor + Supertrend | every 5 min (thread) | `_price_monitor_thread()` |
| Momentum Monitor | every 30 min (thread), market hours | `_momentum_monitor_thread()` — scans `momentum_indices`, auto-adds via `auto_watchlist_agent` |
| Supertrend Universe Monitor | every 30 min (thread), market hours, **staggered 15 min after Momentum Monitor** | `_supertrend_universe_monitor_thread()` (added 2026-08-14) — scans `supertrend_universe_indices` via `scan_supertrend_universe()`, auto-adds every fresh bullish flip with no score gate (only price ≥ $5 + optional liquidity). The stagger exists because both threads do a full-universe `yf.download()` and hit `YFRateLimitError` when they land in the same instant. |
| News Catalyst Monitor | every 15 min (thread) | `catalyst_monitor_thread()` |

### Auto-Watchlist (`run_scan`)
Score ≥ 70 and not already in the watchlist → auto-add (`alert_score=70`, `alert_pct=5.0`, notes `"Auto: score {N} on {date}"`). One Telegram summary per run. Immediately writes `score_threshold`/`price_change`/`score_delta_rise` suppression cooldowns so the 12:00 watchlist scan doesn't re-fire for the same stocks. Controlled by `"auto_watchlist": true`.

### Auto-Exit (`run_scan` + `run_watchlist_scan`)
Auto-added tickers (notes prefix `"Auto:"` or `"Auto ["`) with score ≤ 40 are removed after a **minimum hold of 3 days**. Cooldown rows (`auto_exit_score`, `auto_exit_cooldown`) are written **BEFORE** `watchlist_remove()` in both call sites — a failed remove still leaves the cooldown in place. **Re-entry block**: 7 days, unless score ≥ 75.

### LLM Universe Curation (`src/llm_universe_curator.py`, currently **enabled**)
Weekly (07:45), gated by `llm_universe_curation_enabled` in config. Curates the top-150-by-score digest down to ~80 tickers worth active monitoring — an LLM-judged **narrowing**, not a discovery mechanism (never introduces a ticker the quant scanner didn't already surface, enforced by an allowlist check on the response). Low-turnover by design (last week's list given as an anchor). Persisted to `llm_curated_universe`; stale (>10 days) curation is ignored, not enforced.

### Squeeze Scan — 1 combined message
`🚨 High SI+DTC Alert` (SI>20% AND DTC>15, 24h cooldown) + `🔥 Top Squeeze Candidates` (top 10), one Telegram.

### Catalyst + High-SI Alert — 1 combined message
`scan_catalysts(days_ahead=7)`. Filters: SI≥10% AND event≤7d AND price≥$5.00 AND explosion_score≥40. Top 5 combined into one message, 24h cooldown.

### Breakout Alert (`_check_breakout` in `run_scan`)
For every ticker with score ≥ 65: 52w-high break or Bollinger-upper break → saved to `watchlist_alerts`. **Telegram suppressed** — superseded by `combined_buy`, which fires at the actual breakout candle in real time; scan-time breakouts are structurally late on prior-close data. 24h cooldown. Only one trade plan per message (the execution-engine block from `run_scan()`, not `_check_breakout()`'s own).

### Watchlist TTL Cleanup (`run_watchlist_cleanup`, 08:00)
Auto-added tickers only. Last 3 scan scores all < 50 → removed. One batched Telegram summary.

### Supertrend — 3 Timeframes (`price_alert_monitor.py`)
Runs on all watchlist tickers every 5 min. All three timeframes (15m/1h/daily) are **DB-log-only, silenced from Telegram** — real-time alerts go via `ibkr_worker` → `signal_combiner` → `combined_buy/sell` instead. ATR uses Wilder's EMA, identical to TradingView.

### Score Jump/Drop Alerts (`src/score_alert.py`)
Fires for ALL scanned tickers (not just watchlist) on ≥15pt score delta, shared cooldown/alert-type with `watchlist_manager.py`.

### Cooldown Helper
```python
_alert_sent_recently(ticker, alert_type, hours=24) -> bool  # scheduler.py
_cooldown_passed(ticker, alert_type) -> bool                 # watchlist_manager.py
```
Both check `watchlist_alerts`.

---

## Alert Types & Channels

All alerts include a `🎯 Action:` line. Telegram is reserved for **real-time, high-conviction** signals — everything driven by yfinance polling (15-min lag) or a lagging indicator is DB-log-only, checkable for audit but not pushed.

| Type | Trigger | Cooldown | Channel | Source |
|---|---|---|---|---|
| `combined_buy` / `combined_sell` | Supertrend 1H flip + monitoring queue membership (no score gate either direction) | 24h | **Telegram** — the only true real-time path | `ibkr_worker` → `signal_combiner` |
| `catalyst_si_alert` | SI ≥ 10% + catalyst ≤ 7 days + explosion_score ≥ 40 | 24h | **Telegram** — forward-looking, latency-tolerant | `scheduler.py` |
| `premarket_gap_alert` | real premarket gap: gap% ≥ threshold + real premarket $volume ≥ threshold | 24h | **Telegram** — informational, unvalidated for edge | `scheduler.py` |
| `opening_print_alert` | 09:30 open print vs. price ~7min later, move% ≥ threshold + $volume ≥ threshold | 24h | **Telegram** — informational, unvalidated for edge | `scheduler.py` |
| `auto_wl_squeeze` / `auto_wl_catalyst` / `auto_wl_momentum` / `auto_wl_supertrend` | per-source filter pass in `auto_watchlist_agent.py` | 24h (1440min) | **Telegram** — informational | `auto_watchlist_agent.py` |
| `price_above` / `price_below` / `price_target` / `price_change` | user-defined levels; `price_change` gated ET 04:00–20:00 | 24h / 4h | **Telegram** — manual targets | `watchlist_manager.py`, `price_alert_monitor.py` |
| `price_surge_rescore` | watchlist ticker moves >10% since baseline; Telegram if rescored ≥55; gated 09:30–16:00 ET | 2h | **Telegram** | `price_alert_monitor.py` |
| `stop_loss` / `target_hit` / `score_drop` | portfolio position management | 24h | **Telegram** | `watchlist_manager.py` |
| `news_catalyst` | LLM news analysis, 45-min freshness gate | — | **Telegram** — forward-looking | `catalyst_monitor_thread` |
| `breakout_alert` | score≥65 + 52w-high or Bollinger break | 24h | DB-only — superseded by `combined_buy` (real-time, not stale prior-close) | `scheduler.py` |
| `squeeze_si_alert` | SI>20% AND DTC>15 | 24h | DB-only — daily cadence, not real-time | `scheduler.py` |
| `score_threshold` / `score_delta_rise` / `score_delta_drop` | score crosses alert_score / ±15pt delta | 24h | DB-only — redundant with `combined_buy`; drops covered retrospectively by the weekly digest | `watchlist_manager.py` + `score_alert.py` (shared type) |
| `supertrend_intraday_flip` / `supertrend_1h_flip` / `supertrend_flip` (daily) | Supertrend flip, 3 timeframes | 1h/2h/4h | DB-only — superseded by `combined_buy` real-time path | `price_alert_monitor.py` |
| `supertrend_triple_bull` / `_bear`, `rsi_oversold/overbought`, `macd_bullish/bearish`, `volume_spike` | lagging indicators on yfinance-lag data | — | DB-only — lag or ambiguous direction | `price_alert_monitor.py` |
| `auto_exit_score` | auto ticker score ≤ 40, held ≥ 3 days | 12h | Telegram (batched) | `scheduler.py` |
| `auto_exit_cooldown` | written on any auto-exit or capacity eviction | blocks re-add 7 days unless ≥75 | DB-only (cooldown registry, not user-facing) | `scheduler.py` + `auto_watchlist_agent.py` |

`score_threshold` + `price_change` are also written as **suppression records** immediately after an auto-watchlist add, so the 12:00 watchlist scan doesn't re-alert stocks just added at 09:20.

**Expected Telegram volume:** ~80/week (down from ~350/week before the 2026-05-20 cleanup pass that established this DB-only-vs-Telegram split).

---

## HTML Rendering Rule

All multiline HTML must go through `_html()` before `st.markdown(unsafe_allow_html=True)`:
```python
def _html(raw: str) -> str:
    return " ".join(raw.split())
```
Any user-, LLM-, or scraped-supplied string reaching `unsafe_allow_html=True` must also be `html.escape()`d first (multiple XSS fixes across `page_research.py`, `page_news_impact.py`, `page_options_flow.py`, `page_catalyst.py`, `page_scheduler.py`, `news_impact_analyzer.py`).

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
MASSIVE_API_KEY         # Massive/Polygon.io REST API — src/gap_scanner.py::scan_premarket_gaps only (paid "Starter" plan, $29/mo)
```

---

## Known Limitations

- Alpha Vantage: 25 req/day (free tier) — daily quota counter warns at 23 requests
- Google Trends: occasional 429 errors; 1-hour cache and `threading.Lock()` mitigate burst issues
- Borrow fee: Finviz approximation from SI% — directionally correct, not exact
- Price monitor: only runs when Scheduler is active
- DCF: returns None for loss-making companies, financial sector, or over-leveraged companies — falls through to P/S
- Backtest: requires at least 1 week of scan data; `price_at_signal` fetched on the same auto-adjust basis as `price_after` to avoid corporate-action artifacts
- Insider tracker: slow (~4–7s); EDGAR calls have `timeout=15`
- PDUFA scraper: depends on BioPharma Catalyst HTML structure — returns `[]` gracefully on failure
- Unusual Options: no options data for many small caps → returns 0 pts silently
- `get_upcoming_macro()` shows an approximate weekly schedule (events marked `*`) — not a live economic calendar
- `stock_scorer.py` Forecast weight (15) is defined but `forecast_score=0` in code — inactive
- **The `combined_buy`/Supertrend trigger itself has no demonstrated statistical edge net of a realistic exit** — see [Live-Readiness Audit](#2026-08-05--live-readiness-audit-no-measurable-alpha) and [Exit Simulation](#2026-08-05--exit-simulation-supersedes-the-horizon-result-above). Coverage-expanding features (Supertrend Universe Monitor, capacity rotation) close information gaps, not this gap.
- Multi-agent audits found and fixed a long tail of mechanical issues (connection leaks, XSS, thread-safety, cache TTL bugs, NaN handling) across 2026-05/06 — see git history for `CLAUDE.md` at those dates if a specific one needs to be traced; current-state facts from those fixes are folded into the module descriptions above rather than re-listed here.
- `gap_scanner.py`'s Premarket Gap Alert / Opening Print Alert (added 2026-08-15) are informational-only and **unvalidated for edge** — unlike `combined_buy`/Supertrend signals they don't write to `forward_signals` (they're not a trade signal), so there's no automated win-rate tracking. Manually review actual hit quality over a few weeks of live Telegram output before ever considering wiring either into `auto_watchlist_agent` or the IBKR pipeline. (This caveat is about trading-signal usefulness, not data correctness — the premarket half's underlying data source, Massive/Polygon since 2026-08-16, is a paid, verified-accurate real-premarket-volume feed; see Incident Archive.)

---

## Open Backlog

- [ ] Sector-level sub-scanning in main Scan page — currently only in Squeeze; `page_scan.py` scans all sectors uniformly
- [ ] Fear & Greed Index widget — `page_market.py` shows VIX text description only
- [ ] Weight tuning based on backtest data — `WEIGHTS` dict in `stock_scorer.py` is static
- [ ] Russell 2000 support in main Scan page — works in Catalyst Scanner + scheduler, not wired into `page_scan.py`
- [ ] SEC 8-K item classification (1.01 bullish / 1.03 bearish) — `catalyst_scanner.py` fetches 8-K but doesn't classify by item number
- [ ] `supertrend_triple_bull/bear` — consider routing through `signal_combiner.evaluate()` for the same cap+dedup discipline `combined_buy/sell` gets (currently DB-only, uncapped)
- [ ] `news_catalyst` threshold tuning — consider lowering `catalyst_threshold` from 3 to 2 if forward-paper-trading shows missed catalysts
- [ ] `modify_stop_order()` matches the first STP SELL by ticker — ambiguous if multiple STPs exist for one ticker; needs a `stop_order_id` column in `order_log` to fully fix
- [ ] `record_fill()` is not idempotent on duplicate fill events — rare, but can corrupt an older `forward_signals` row
- [ ] Track whether Supertrend-universe / capacity-rotation additions (2026-08-14) perform differently from the existing sources in `forward_signals` before treating the wider net as a return improvement, not just a coverage one
- [ ] Manually review `gap_scanner.py` Premarket Gap Alert / Opening Print Alert hit-rate and usefulness after a few weeks of live output before considering wiring either into `auto_watchlist_agent` (added 2026-08-15, see Incident Archive)

---

## Incident & Research Archive

Ordered chronologically. This is the project's institutional memory for *why* something is built the way it is — root causes, exact numbers, and (for the short-position family) an explicit note to future incidents. Routine mechanical fixes from the 2026-05/06 hardening sprints are **not** re-listed here; their outcomes are current-state facts in the sections above, and the sprint-by-sprint detail is recoverable via `git log -- CLAUDE.md` if ever needed.

### 2026-08-02 — Exit Strategy Implementation
Forward-signal data showed BUY win rate 55.8% but avg win +5.03% vs avg loss −6.38% — near-zero expectancy. Root cause: all 42 open positions had 0 active bracket legs (TIF=DAY expired every night). Fixed: bracket legs → GTC; added `modify_stop_order()`, ATR trailing stops (2.5×ATR/1h, every 15 min, never lowers), time stops (>15 trading days + |pnl|<3%, every 4h), tiered exits (T1 +7%→sell 40%+move stop to breakeven, T2 +14%→sell 50% of remainder), score-deterioration exit (score drop ≥15pt + current <55 + pnl>-3%). This sprint is also where the exit-routing bug below originated.

### 2026-08-03 — Root Cause of the Shorts (runaway tiered exit)
The IBKR Activity Statement settled it: no Transfers section exists — every short came from **executed SELL orders**, 55,687 shares of them that `order_log` never recorded. Mechanism: `_check_tiered_exits` called `place_limit_order()` **before** advancing `exit_tier` and before writing `order_log` — nothing suppressed a re-fire, nothing recorded it. A tight resubmission loop at the open (BMY: 224 identical −34-share executions in the same second; IT: 242 identical −14-share executions) kept selling the same 40%-of-position slice over and over until the position crossed zero, at which point the (then-existing) short filter in `sync_positions()` erased it from view entirely.

**Fix:** all three exit paths (time stop, tiered exit, score deterioration) now route through `order_manager.submit_exit()`, which enforces the pause + `shares ≤ held − already-working` + order-log-before-broker-call, in that order. `tests/test_exit_routing.py` replays the incident: 224 identical T1 submissions on an 86-share position now produce exactly 3 broker calls totalling 86 shares, 221 vetoed, plus a source-level tripwire that fails if any exit function reintroduces a direct `place_limit_order()` call.

### 2026-08-03 — Anti-Pyramiding & Alert Flood
Live trading produced 9 `combined_buy` signals for different tickers within 4 minutes, generating 27 Telegram messages (3 per trade). Existing long positions were also receiving new BUY orders on repeat flips — pure pyramiding, no portfolio-level cap. Confirmed duplicates in `order_log`: CALM ×4, PNW ×3, FUBO/NTST/ONB ×2. Fixed: Layer -1.5 already-long BUY veto (fail-open on DB error); `PER_CYCLE_BUY_CAP=3` per `run_once()` (excess signals release their dedup claim for retry next cycle); SUBMITTED Telegram condensed to a 1-liner (the signal message already carries entry/stop/target).

### 2026-08-04 — Pre-Live Safety Hardening
3-agent audit before switching to live IBKR trading; 10 fixes across sizing (`portfolio_value` was hardcoded $100k), SELL sizing (now always closes the full position, not a risk-sized partial), zero-NLV fallthrough, paper→live daily_pnl transition guard, BUY signal hours tightened to 09:30 ET (SELL stays 04:00–20:00 so exits are never suppressed pre-market), order-before-place ordering for time stops / score deterioration / tiered exits, and dedup checks against in-flight SUBMITTED SELLs.

**Remaining known issues at the time** (kept as an explicit record — mitigated by the bracket STP, not fully closed): `modify_stop_order()` ambiguity with multiple STPs per ticker; `record_fill()` non-idempotency on duplicate fills; a narrow sub-hour-restart window where a `bars_ago==1` flip could replay before dedup is established; bracket stop-leg rejection rollback being dead code in practice (`ib_async` reports it asynchronously). Both non-idempotency and the STP ambiguity are still open — see backlog.

### 2026-08-05 — Live-Readiness Audit: no measurable alpha
Pre-live forensic audit of paper trading. **Headline finding: `combined_buy` has no statistically detectable alpha.** 145 matured BUY signals vs SPY over identical holding windows — 7d excess **−0.20%** (t=−0.26), 14d **+0.40%** (t=+0.41), 30d **+2.14%** (t=+1.15). No horizon reaches |t|>2; beat-SPY rate 49.7%/55.6%/54.4%. The reported 55.9% win rate was never benchmarked — it was measuring market beta, and `run_forward_digest` still only reports raw win rate.

Five silent execution defects fixed: (1) startup reconciliation was marking every SUBMITTED row missing from IBKR's open orders as ERROR without checking whether it had filled — now looks up broker execution history first, never destroys state; (2) `ib.fills()` is session-scoped, replaced with `get_executions()` (broker-side `reqExecutions()`) plus a 24h grace period before an unresolved order is touched; (3) `_check_time_stops()` measured from the LAST fill (`ORDER BY created_at DESC`), so every pyramid add reset the clock and no scaled position could ever age into a time stop — now `ASC` with a fallback to the earliest BUY of any status; (4) the whole exit layer was gated behind `if not queue: return 0` — a quiet scan silently disarmed every stop; (5) long-only invariant: `_pending_sell_shares()` now subtracts in-flight SELLs before sizing a new one, closing a gap where two exit paths could each size against the same stale share count.

On the apparent KSS/GTY/OMC oversell pattern in `order_log`: not conclusive at the time (corrupted ERROR rows understated purchases in the same accounting) — see [Short-Position Blindness](#2026-08-05--short-position-blindness-most-severe-defect-found), which settled it: KSS was short 13,247 shares. Of 30 bogus ERROR rows, only 7 could be proven filled by share reconciliation (CBT×2, FUBO, LCII, RRR×2, TRS) and were repaired (status only, `fill_price` left NULL — real fill prices are unrecoverable past ~24h); the other 23 involve since-closed positions and are unreconstructable.

### 2026-08-05 — Short-Position Blindness (most severe defect found)
**The account held 14 real short positions worth −$2.37M against an $853k NLV, and not one existed in `ibkr_positions`.** Unrealized loss on them ≈ −$121k. This is a long-only bot. Root cause, one line in `sync_positions()`:
```python
# Skip short/phantom positions — we never intentionally short;
# negative shares are paper-account artifacts from pre-position-gate era.
positions = {t: d for t, d in positions.items() if d.get("shares", 0) > 0}
```
The comment's assumption was false. Dropping the rows before the upsert meant the follow-up `DELETE ... WHERE ticker NOT IN (...)` treated them as closed — invisible to every veto layer (all keyed on `shares > 0`), `/positions`, `alert_monitor`, any audit. Explained the same-day NLV anomaly ($1,035,074 → $853,301 with a visible long book of only $178k) and settled the KSS question above.

Fixed: shorts are now recorded, not filtered. `_raise_short_alarm()` — a short in this long-only bot halts trading (`set_paused(True)` re-applied every cycle) and sends a Telegram alarm throttled to 1/6h, but **deliberately never auto-covers** — unwinding is a human decision. `get_current_exposure()` is long-only by contract (returns 0.0 for a short, so the SELL veto can't be tricked into "there's something to sell"). Layer -1 uses `exposure <= 0` rather than `== 0` as belt-and-braces. `tests/test_short_detection.py` — 10 tests.

**Operator action this did not cover:** the 14 existing shorts had to be closed manually in TWS — the code prevents recurrence and forces a halt, it does not unwind an existing book.

### 2026-08-05 — Trigger Backtest
`src/trigger_backtest.py` — the validation capability the project lacked: forward paper trading at ~65 signals/month can't resolve an edge below ~1%/trade in under a year. Replays the exact production trigger (`trend_series()` mirrors `supertrend.py` line for line, tested bar-by-bar) over ~3 years of hourly bars (`python run_trigger_backtest.py --tickers 250`, ~90s).

**Result — 17,810 flips, 242 tickers, 2023-09..2026-07, excess vs IWM:** 7d clustered t=+1.31 (no edge) · 14d t=+1.58 (no edge) · 30d t=+2.27, mean +0.70% (marginal). Naive t-stats are inflated by overlapping holding periods — always use the clustered figure. **The edge exists only at ~30 days and the system exits in days** — it harvests where there's nothing and exits before where there's something.

Entry filters do not help: `close>SMA50` looked best naively (t=+4.42) but *underperforms* the unfiltered trigger under strict treatment (t=+1.52 vs +2.27). Do not add SMA200/RSI/regime/price/ADV filters. Regime split: above SMA200 +0.62% (t=5.02), below SMA200 +0.13% (t=0.34) — no edge in a falling tape. **Survivorship bias is the largest unquantified weakness** — the universe comes from `scan_results` (tickers the bot scanned in 2026) replayed to 2023; delisted names are structurally absent. Treat +0.70% as an upper bound.

### 2026-08-05 — Exit Simulation (supersedes the horizon result above)
`run_exit_simulation.py` — bar-by-bar replay under competing exit policies (pessimistic fills: gap-through-stop fills at the open; stop-before-target on same-bar touches).

**The +0.70% horizon edge does not survive contact with a stop.** Regime A (live: Supertrend flip exit) lands at **+0.00% excess (t=0.04)**, independently reproducing three months of live near-zero expectancy — the strongest calibration evidence available. Every stop-carrying regime lands at zero or negative excess; the only positive variant (no stop, 30d time) isn't tradeable (unbounded single-name loss) and isn't significant (t=1.63). Two methodology bugs were found and fixed in producing this, both of which had *inverted* the answer before the fix: stops were sized from hourly ATR instead of production's daily ATR (several times tighter, false "stops destroy the edge" signal); and non-overlap clustering was spaced by the *current* trade's holding period, preferentially admitting fast losers (Regime A's true +0.00% was initially reported as −1.03%). Both are pinned by `tests/test_exit_simulation.py`.

### 2026-08-05 — Signal Panel
`src/signal_library.py` — `run_backtest(signal_fn=...)` made signal-agnostic. Six pre-committed candidates tested against the production trigger as control; **nothing survives** the |t|>3.07 multiple-comparison threshold (best reaches +1.42). Naive 30d excess looks strong for several (t=4.19–4.70) and **every one collapses to ~0 under a real stop** — this is a property of the whole long-only momentum/breakout family on this universe, not the Supertrend trigger specifically. Changing the trigger does not help. Three harness bugs were found and fixed, each of which had inverted a result before the fix (daily signals gated out entirely by an hour-based intraday check; signals predating the price series producing fake year-long holds; forward returns measured on the wrong timeframe).

### 2026-08-12 — Resting Stop/Target Left Oversized After a Partial Exit
Fourth distinct mechanism (after 2026-08-03, -05, -06) that put the long-only bot into a short — each prior incident was patched narrowly for its own trigger instead of the real invariant: **a resting SELL order must never exceed the position it protects.** `_cancel_unbacked_sell_orders()` (added 2026-08-06) already checked this but only acted when `held<=0`; when `0<held<qty` it logged a warning and left the order in place, reasoning that cancelling would strip real protection — half right, but leaving it oversized was just as wrong.

**SENEA, 2026-08-10/11:** bracket BUY 70sh filled; T1 (+7%) sold 40%=28sh via a separate order and moved the STP to breakeven via `modify_stop_order()` — which only ever touches `auxPrice`, never `totalQuantity`. The STP stayed resting at qty=70 against a 42sh position; every cycle logged the warning and did nothing. A routine disconnect/reconnect gap let the stop fill for the full 70sh, flipping the account to **-28sh** — exactly the T1-sold amount, same signature as 08-06. The 08-05 short-alarm guard caught it on the next sync and paused trading (stayed paused across a worker restart; no BUYs submitted while paused — the safety net held). Paper account only, no real money at risk.

**Fix:** `resize_sell_orders(ticker, max_qty)` caps every resting SELL (STP or LMT) at the actual held quantity, in-place (same orderId). `_reconcile_resting_sell_orders()` (renamed from `_cancel_unbacked_sell_orders`) now resizes when `0<held<qty` instead of only logging. `tests/test_unbacked_sells.py` — 12 tests, replays SENEA exactly plus the both-legs-oversized case.

**Structural note for future incidents in this family:** if a fifth mechanism surfaces, prefer strengthening `_reconcile_resting_sell_orders` (the one place this invariant is enforced) over adding another narrow guard wherever it triggered.

### 2026-08-14 — Watchlist pinned at capacity, zero new adds
Building the Supertrend Universe Monitor (below) surfaced a pre-existing, unrelated defect: `watchlist_policy.max_items_total` (30) was a hard silent block with no eviction. With momentum, squeeze, catalyst, and now supertrend all running every 30 min or less, the watchlist was pinned at 30/30 and **every** source added 0 regardless of candidate quality — momentum alone found 192–356 hits across several cycles the same day, none added. Fixed by `_evict_for_capacity()` (see `auto_watchlist_agent.py` above) + raising the cap to 50. Live-verified the same day: first post-fix momentum cycle found 299 hits and added 9.

### 2026-08-14 — Supertrend Universe Monitor (coverage gap)
The IBKR real-time queue only ever runs Supertrend on tickers already scoring ≥65 (`monitoring_queue.SCANNER_MIN_SCORE`), so a ticker having a strong technical breakout with a middling composite score was never monitored — the flip and any subsequent rally were silently missed. `scan_supertrend_universe()` (`src/supertrend.py`) closes this by batch-scanning the whole index universe with no score gate, mirroring a bare TradingView `alertcondition(buySignal)`. Deliberately does **not** demonstrate improved edge — the Supertrend trigger itself has none (see the 2026-08-05 research entries above); this closes an information/FOMO gap, not an alpha gap. Track its `forward_signals` performance before treating it as more than that (see backlog).

### 2026-08-14 — Docker/WSL2 BSOD investigation (host-level, not this codebase)
User-machine troubleshooting, not a FinancialAgent code change — kept here only because the machine hosts the live IB Gateway container. WinDbg analysis of 5 minidumps found 2 of 5 sharing an identical kernel fault signature (`0x1_SysCallNum_36_nt!KiSystemServiceExitPico`) tied to WSL2's Pico-process syscall path, with the most recent crash occurring inside `com.docker.backend.exe` itself — a genuine Docker/WSL2 kernel bug, not a third-party driver. `.wslconfig` memory cap (4GB) applied; WSL updated 2.6.2→2.7.11. Full detail (bugcheck codes, process names per dump, VBS/Core-Isolation test still pending) is in this session's memory files, not here — this repo isn't the right home for host OS diagnostics.

### 2026-08-15 — Premarket Gap Alert + Opening Print Alert (the NMAX gap-catch gap)
Investigation of a `auto_wl_momentum` Telegram alert for NMAX (fired ~6h after the open at $10.78, already +13.4% off the $9.51 prior close and already 6% off its high) found the entire move happened in the first 3-5 minutes of the REGULAR session (09:30-09:35 ET): premarket (04:00-09:30 ET) was completely dead — 44 one-minute bars, **zero** cumulative volume, price flat $9.55-9.79 all morning, open print only +4%. `_momentum_monitor_thread` (every 30 min, additionally gated by watchlist-capacity rotation — many 30-min cycles found 300+ tickers scoring above threshold but added 0-1) structurally cannot catch a move that completes in its first few minutes.

Built two new one-shot Telegram jobs (`src/gap_scanner.py`, `scheduler.py::run_premarket_gap_alert` / `run_opening_print_alert`) instead of one, because NMAX and a genuine overnight-news gapper are different categories needing different data: a real premarket gap needs premarket volume as confirmation (the reason NMAX itself wasn't a premarket gap — no volume backed the flat overnight price), while NMAX's actual pattern needs a post-open comparison of the 09:30 print vs. price ~7 minutes later, with its own volume filter. Both run informational-only to Telegram (`premarket_gap_alert` / `opening_print_alert` alert types, `watchlist_alerts` table, no new migration) — explicitly **not** wired into `auto_watchlist_agent.run()`, `order_manager.py`, or `ibkr_worker.py` for this first version; see Known Limitations for the plan to validate before ever changing that.

### 2026-08-16 — Premarket Gap Alert data source was silently broken (yfinance → Massive/Polygon)
Follow-up investigation found `scan_premarket_gaps()` (added 2026-08-15) was **non-functional in production from day one**: yfinance's `prepost=True` intraday download always reports premarket volume as exactly **0**, confirmed across 6 independent real historical ticker/day cases pulled from this project's own `watchlist_alerts` table (NMAX, TKNO, STIM, FRMM, GO, CALM — including CALM, a liquid $80+ mid-cap, ruling out "only illiquid microcaps" as the explanation). Since the function's whole design gates on `premarket_dollar_volume >= min_premarket_dollar_volume`, the filter could never pass and the alert simply never fired, silently.

Two free alternatives were evaluated and both also failed: Alpaca's free/IEX-feed tier returns **zero bars at all** before 09:30 ET (not even zero-volume bars — the data isn't requested/returned at that tier); EODHD's free tier returns HTTP 403 ("Only EOD data allowed for free users") for any intraday request. Massive/Polygon (Polygon.io's Oct-2025 rebrand) was the only source that worked, verified against a real paid "Starter" account (`MASSIVE_API_KEY`, $29/mo, 15-min delayed — acceptable since this check runs well before the open, not real-time-critical) across the same 7 cases, including a negative control: WEST on 2026-07-27 correctly came back with **0** premarket volume (proving the source reports true zeros, not just "always something").

`scan_premarket_gaps()` was rewritten to call Massive/Polygon per-ticker (`/v2/aggs/ticker/{ticker}/prev` for prior close, `/v2/aggs/ticker/{ticker}/range/5/minute/{date}/{date}` for premarket bars — Polygon has no batch endpoint, unlike `yf.download`), fanned out via `ThreadPoolExecutor` (mirrors `catalyst_scanner.py::fetch_sec_8k_events`'s pattern), with a one-call preflight key check (fails fast on 401/403 instead of attempting ~5,000 doomed requests) and per-ticker failure isolation (one ticker's fetch error no longer aborts the whole scan, unlike the old shared-batch-call model). `scan_opening_prints()` was deliberately left untouched — it uses yfinance regular-session (not premarket) data, separately verified accurate, so there was no reason to add paid-API cost there. **Timestamp handling note for future maintainers:** Polygon/Massive bar timestamps (`t`) are Unix milliseconds in UTC, not ET — every bar must be explicitly converted (`tz=timezone.utc` then `.astimezone(ET)`) before any time-of-day boundary check, or the premarket window filter silently admits/drops the wrong bars.
