# Financial Agent
**AI-Powered Stock Scanner & Financial Analysis Dashboard**

Streamlit dashboard combining technical analysis, DCF valuation, short squeeze scanning, catalyst detection, news impact analysis, and real-time IBKR order execution with Telegram alerts.

---

## Stack

| Layer | Technology |
|---|---|
| UI | Streamlit 1.52.2 |
| Language | Python 3.14 (main) · Python 3.13 (IBKR worker) |
| DB | SQLite (WAL mode, WAL hardened) |
| LLMs | Gemini 2.0 Flash → Groq Llama 3.3 70B fallback |
| Market Data | yfinance · Finnhub · Alpha Vantage · SEC EDGAR (free) |
| Broker | Interactive Brokers via `ib_async` (Python 3.13 venv, Docker IB Gateway) |
| Alerts | Telegram Bot (outbound + two-way commands) |

---

## Features (11 pages)

| Page | What it does |
|---|---|
| **Scan** | Multi-factor scoring (0–100) with DCF column. Background scan worker. |
| **Research** | Deep Dive (DCF card, Bull/Bear debate, AI analysis) + Compare Side-by-Side |
| **Watchlist & Portfolio** | Price targets, real-time alerts, P&L dashboard, sector allocation, 21 alert types |
| **Market** | Live indices, Futures bar, VIX level, sector heatmap, mood, earnings, macro events |
| **News Impact** | 3-layer LLM analysis + Stock News + Upcoming Events tab |
| **Short Squeeze** | Squeeze Score 0–100, sparklines, AI Verdict, sector scan mode, insider overlay |
| **Catalyst Scanner** | Upcoming catalyst + explosion score, PDUFA/AdCom, unusual options, insider |
| **Options Flow** | Options chain data, PCR, unusual call/put activity |
| **Backtest** | Signal accuracy + Supertrend P&L simulator + Forward Signal win-rate (live) |
| **History** | Score trend over time per ticker |
| **Scheduler** | Automated scans + Telegram digest + price monitor + IBKR queue status |

---

## Setup

```bash
pip install -r requirements.txt
```

`.env`:
```
GROQ_API_KEY=...
GEMINI_API_KEY=...
FINNHUB_API_KEY=...
ALPHA_VANTAGE_API_KEY=...
SEC_USER_AGENT_EMAIL=your@email.com
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
IBKR_LIVE=              # leave unset for paper mode (port 4002); set to "true" for live (port 4001)
```

```bash
streamlit run dashboard.py                     # → http://localhost:8501
python -m pytest tests/ --ignore=tests/test_new_apis.py   # 273 tests, 0 failures
```

---

## Process Architecture

Two watchdogs start at Windows login (Task Scheduler), each supervising a long-running child:

```
FinancialAgentWatchdog          FinancialAgentIBKRWorker        FinancialAgentTunnelWatchdog
(run_scheduler_watchdog.py)     (run_ibkr_worker_watchdog.py)   (run_tunnel_watchdog.py)
        │                               │                               │
        ▼                               ▼                               ▼
  scheduler.py                  src/ibkr_worker.py              run_dashboard_tunnel.py
  .venv (Py 3.14)               .venv313 (Py 3.13)              (Cloudflare Quick Tunnel)
  All scoring/alerts             Supertrend(1H) loop
  DB writes via WAL              + bracket orders
                    ◄──── SQLite DB (WAL) ────►
```

**Stop cleanly:** create `stop_scheduler.flag`, `stop_ibkr_worker.flag`, or `stop_tunnel.flag` in the project root.

**Python Launcher fix:** `.venv313\Scripts\python.exe` is the Windows Python Launcher, not the real interpreter. The IBKR watchdog reads `pyvenv.cfg` to find the real `Python313\python.exe` and invokes it directly, preventing a phantom parent+child process pair.

---

## Scheduler Jobs

> Times from `scheduler_config.json` — overrides code defaults.

| Job | Time | Channel |
|---|---|---|
| Watchlist Cleanup | 08:00 | Telegram (summary) |
| Catalyst + High-SI Alert | 08:05 | Telegram (combined) |
| Cloudflare heartbeat | 08:05 | Telegram |
| Portfolio News | 08:30 | Telegram |
| Scan + Auto-Watchlist | 08:30, 15:00 | Telegram (auto-added summary) |
| Portfolio Scan | 09:15 | Telegram (stop/target/score) |
| Market Digest | 09:30 | Telegram |
| Alert Monitor health | 09:30 | Telegram |
| Watchlist Scan | 12:00 | Telegram (score/price alerts) |
| Squeeze Scan | 12:00 | Telegram |
| Long Setups | 09:30 | DB only |
| Weekly Rotation | Mon 08:15 | Telegram |
| Forward Outcomes | 18:00 daily | DB only |
| Forward Digest | Fri 20:00 | Telegram (win-rate) |
| Price Monitor + Supertrend | every 5 min (thread) | DB only |
| Momentum Monitor | every 30 min (thread) | Telegram (auto-add) |
| News Catalyst Monitor | every 15 min (thread) | Telegram (LLM analysis) |
| IBKR combined_buy / combined_sell | real-time, every 5 min | Telegram + bracket order |

---

## Scoring Engine (0–100)

| Component | Weight | Notes |
|---|---|---|
| RSI | 15 | RSI >75 = 0 pts |
| MACD | 15 | |
| MA Trend | 20 | |
| Volume | 10 | |
| Momentum | 10 | |
| SI% of Float | 10 | Short interest |
| Institutional | 5 | |
| Insider | 5 | SEC Form 4 |
| Fundamentals | 10 | P/E, Revenue CAGR (EDGAR), Interest Coverage (EDGAR), Margin |
| DCF | 15 | CAPM WACC · EDGAR FCF (Tier 1) · net debt subtracted |
| Squeeze Bonus | +15 | SI≥20% + vol spike + price up |
| Google Trends | +5 | bonus |
| Earnings Sentiment | +5 | EPS surprise (Finnhub) + LLM transcript + EDGAR EPS YoY fallback |

**Signals:** 75+ = STRONG BUY · 60–74 = BUY · 45–59 = WATCH · 35–44 = NEUTRAL · <35 = SKIP

---

## DCF Engine (`src/dcf_valuation.py`)

```
Enterprise Value = Σ FCF_t/(1+WACC)^t + TV/(1+WACC)^n
Equity Value     = Enterprise Value − Net Debt
Intrinsic/share  = Equity Value / sharesOutstanding
```

**FCF source priority (tiered):**
1. SEC EDGAR XBRL — median of 4 annual 10-K values (audited, free)
2. yfinance cashflow DataFrame — multi-year median
3. yfinance TTM `info.freeCashflow`
4. OCF − CapEx

**WACC:** CAPM cost of equity (Rf = ^TNX + Beta × 5.5% ERP) + actual cost of debt (InterestExpense/totalDebt). Clamped 7%–15%.

**Exclusions:** Financial sector (banks/insurance) → returns None → P/S fallback. Over-leveraged (equity_value ≤ 0) → None.

---

## IBKR Real-Time Order Execution

**Flow:** Supertrend 1H flip → `signal_combiner.evaluate()` → Telegram alert → `order_manager.submit()` → execution engine (7 veto layers) → `ibkr_realtime.place_bracket_order()`.

**Execution engine layers:**
| Layer | Check |
|---|---|
| -1 | SELL veto: no open position |
| 0 | Daily loss limit (max_daily_loss_pct from config) |
| 1 | Hard vetos: ADV < $5M, R:R < 1.5:1, gap-down |
| 2 | Confluence score |
| 3 | Position sizing (regime-adjusted) |
| 4 | Time-of-day noise filter |
| 5 | Sector concentration |

**Safety:** `paper_mode=True` by default. BEAR regime veto is BUY-only — SELL exits always pass.

**Two-way Telegram commands:** `/status`, `/positions`, `/pause`, `/resume`, `/cancel <TICKER>`

---

## Hysteresis Bands

All binary thresholds use entry/exit deadbands to prevent thrashing:

| Threshold | Entry | Exit |
|---|---|---|
| Auto-watchlist score | 70 | 40 |
| Squeeze SI% | 15% | 10% |
| Catalyst SI% | 10% | 5% |
| Liquidity ADV | $5M | $3M |

Auto-exit cooldown: 7 days after auto-exit before re-add (bypass only if score ≥ 75).

---

## Alert Types (21)

Real-time alerts go via `ibkr_worker` → Supertrend 1H flip. All yfinance-based alerts are DB-log-only (silenced from Telegram to reduce noise).

| Type | Source | Telegram |
|---|---|---|
| `combined_buy` / `combined_sell` | IBKR real-time | ✅ |
| `catalyst_si_alert` | daily scan | ✅ |
| `news_catalyst` | LLM analysis every 15 min | ✅ |
| `price_above` / `price_below` / `price_target` / `price_change` | user-defined | ✅ |
| `stop_loss` / `target_hit` / `score_drop` | portfolio monitor | ✅ |
| `auto_wl_momentum` / `auto_wl_squeeze` | scan auto-add | ✅ |
| `breakout_alert` / `score_delta_rise` / `score_delta_drop` | daily scan | DB only |
| `supertrend_flip` / `supertrend_1h_flip` | price monitor | DB only |

---

## DB Schema (key tables)

| Table | Purpose |
|---|---|
| `scan_results` | Raw scan output per ticker (JSON) |
| `watchlist` / `portfolio` | User-managed tickers |
| `watchlist_alerts` | Alert log + cooldown registry |
| `forward_signals` | Every BUY/SELL alert + 7/14/30d outcomes |
| `ibkr_positions` | Live IBKR positions (synced every 5 min) |
| `daily_pnl` | One row per calendar day from IBKR |
| `order_log` | Every order attempt (SUBMITTED/VETOED/FILLED/CANCELLED/ERROR/PAUSED) |
| `monitoring_queue_snapshot` | Persisted IBKR monitoring queue |
| `telegram_command_state` | Telegram polling offset (crash-safe) |

WAL hardening: `journal_mode=WAL`, `synchronous=FULL`, `busy_timeout=30s`, `retry_on_busy` decorator (5 attempts, exponential backoff).

---

## Project Structure

```
FinancialAgent/
├── dashboard.py                    # Streamlit router (11 pages)
├── scheduler.py                    # Background jobs + daemon threads
├── run_scheduler_watchdog.py       # Auto-restart on crash
├── run_ibkr_worker_watchdog.py     # Auto-restart + orphan kill + singleton
├── run_dashboard_tunnel.py         # Cloudflare Quick Tunnel + heartbeat
├── run_tunnel_watchdog.py          # Tunnel auto-restart
├── _pages_modules/
│   ├── page_scan.py
│   ├── page_research.py
│   ├── page_watchlist.py
│   ├── page_market.py
│   ├── page_news_impact.py
│   ├── page_squeeze.py
│   ├── page_catalyst.py
│   ├── page_options_flow.py
│   ├── page_backtest.py
│   ├── page_history.py
│   └── page_scheduler.py
├── src/
│   ├── stock_scorer.py             # Scoring engine 0–100
│   ├── dcf_valuation.py            # CAPM WACC + EDGAR FCF + net debt
│   ├── edgar_fcf.py                # SEC EDGAR XBRL (FCF, CAGR, ICR, current ratio, EPS)
│   ├── stock_forecaster.py         # ARIMA/MA/ES/MLP ensemble (point-in-time safe)
│   ├── earnings_sentiment.py       # EPS surprise + LLM transcript + EDGAR fallback
│   ├── squeeze_scanner.py          # Squeeze Score + AI Verdict
│   ├── catalyst_scanner.py         # Explosion score, PDUFA, 8-K, unusual options
│   ├── momentum_scanner.py         # 5-factor momentum (ROC/RS/MA/RSI/Volume)
│   ├── long_setup_scanner.py       # Daily long setup scanner
│   ├── options_flow.py             # Options chain, PCR, unusual activity
│   ├── supertrend.py               # ATR Wilder EMA (TradingView-identical)
│   ├── ibkr_realtime.py            # IB Gateway connector (ib_async)
│   ├── ibkr_worker.py              # Standalone daemon (Py 3.13) — Supertrend loop + orders
│   ├── signal_combiner.py          # Supertrend flip → BUY/SELL dedup + daily cap
│   ├── order_manager.py            # Execution engine veto → bracket order → DB log
│   ├── execution_engine.py         # 7-layer veto + position sizing (regime-aware)
│   ├── position_tracker.py         # IBKR position sync → ibkr_positions DB
│   ├── market_regime.py            # BULL/CAUTION/BEAR (VIX + SPY SMA200)
│   ├── forward_signals.py          # Alert → 7/14/30d outcome tracking
│   ├── monitoring_queue.py         # Which tickers get real-time IBKR monitoring
│   ├── auto_watchlist_agent.py     # Auto-adds squeeze/catalyst/momentum candidates
│   ├── hysteresis.py               # Central entry/exit deadband thresholds
│   ├── price_alert_monitor.py      # Price target + supertrend (15m/1h/daily) every 5 min
│   ├── watchlist_manager.py        # Score/price/delta alert logic
│   ├── score_alert.py              # Score jump/drop for all scanned tickers
│   ├── news_catalyst_monitor.py    # Background thread — LLM news analysis every 15 min
│   ├── news_impact_analyzer.py     # 3-layer LLM news analysis
│   ├── news_fetcher.py             # Multi-source news fetch + catalyst_score
│   ├── alert_monitor.py            # Daily health-check (noisy alerts, dead threads, drawdown)
│   ├── telegram_notifier.py        # Telegram send (4000-char truncation guard)
│   ├── telegram_command_handler.py # Two-way Telegram polling (/status /positions /pause…)
│   ├── telegram_news_digest.py     # Market digest + Portfolio news
│   ├── opportunity_tracker.py      # BUY signal → T1/stop outcome tracking
│   ├── market_feed.py              # Live indices + macro events
│   ├── macro_signals.py            # Macro signals
│   ├── index_loader.py             # iShares + Wikipedia S&P 500 fallback
│   ├── borrow_fee.py               # SI% → borrow rate estimate (Finviz, 5-min error TTL)
│   ├── finnhub_client.py           # Finnhub API wrapper
│   ├── llm_client.py               # Gemini → Groq fallback
│   ├── database.py                 # SQLite CRUD + WAL + retry_on_busy
│   └── scan_worker.py              # Background scan thread
├── tests/                          # 273 tests, 0 failures
├── logs/
└── data/
    ├── financial_agent.db
    └── pdufa_cache.json
```

---

## Known Limitations

- Alpha Vantage: 25 req/day free tier — warns at 23
- Google Trends: occasional 429; 1-hour cache + threading.Lock()
- DCF: returns None for loss-making (no +FCF), financial sector, over-leveraged companies
- Forecast weight (15) is defined but `forecast_score=0` in code — inactive ("indicative only")
- MLP `early_stopping` uses shuffled validation split — not ideal for time series, flagged but unchanged
- PDUFA scraper depends on BioPharma Catalyst HTML structure — returns `[]` gracefully on failure
- Unusual Options: yfinance options data absent for many small caps → 0 pts
- `get_upcoming_macro()` is approximate weekly schedule — events marked `*`
- Backtest requires ≥1 week of scan data in DB

---

## Disclaimer

This is NOT financial advice. For research and educational purposes only.

---

*Last updated: 2026-06-29*
