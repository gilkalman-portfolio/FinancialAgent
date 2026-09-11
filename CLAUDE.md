# FinancialAgent — Claude Code Context

## Project Overview
AI-powered stock scanner & financial analysis dashboard.
- **Location:** `C:/Projects/FinancialAgent`
- **Stack:** Python 3.14, Streamlit 1.52.2, SQLite, yfinance, Finnhub, Alpha Vantage, SEC EDGAR
- **LLMs:** Gemini 2.0 Flash (primary) → Groq Qwen3.6 27B (fallback, `reasoning_effort="none"`) via `src/llm_client.py`
- **Run:** `streamlit run dashboard.py` → http://localhost:8501
- **Tests:** `python -m pytest tests/ --ignore=tests/test_new_apis.py --ignore=tests/test_ibkr_connection.py --ignore=tests/test_ibkr_worker_once.py` → **891 passed, 1 failed** (2026-09-02, after merging `main`'s 2026-08-29 work with the `insider_cluster_scanner.py` addition; skips seen on some runs are environment-only — `ib_async` needs `.venv313`, one test needs live `.env` credentials). The 1 failure (`test_news_fetcher_massive.py::TestGetTickerNewsWiresMassive::test_massive_included_when_key_present`) is pre-existing and unrelated — confirmed via `git stash` that it fails identically without this session's changes; not investigated further. This number moves every sprint — re-run it rather than trusting this line for long. The old "5 pre-existing failures in test_pnl_digest_fixes.py" were test rot (helpers seeded with literal dates against a relative-date filter), fixed by using relative dates. **Never seed a time-filtered query with a literal date.**

---

## Architecture

### Entry Points
- `dashboard.py` — Streamlit router, 11 pages (Scan, Research, Watchlist, Market, News Impact, Squeeze, Catalyst, Options Flow, Backtest, History, Scheduler)
- `scheduler.py` — Background jobs + price monitor daemon thread. `run_scan()`'s ticker-scoring loop is parallelized (`_score_scan_universe()`, `ThreadPoolExecutor`, `_SCAN_MAX_WORKERS=5`, added 2026-08-29, same 2-phase pattern as `watchlist_manager.py`) — DB writes/auto-exit/Telegram (Phase 2) stay sequential in original order, only scoring (Phase 1) is concurrent.

### Pages (`_pages_modules/`)
| File | Purpose |
|---|---|
| `page_scan.py` | Multi-factor scan + DCF column |
| `page_research.py` | Deep Dive + Side-by-Side Compare |
| `page_watchlist.py` | Watchlist + Portfolio + Price Target |
| `page_market.py` | Indices + Sector Heatmap + Earnings + Fear & Greed Index |
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
| `watchlist_manager.py` | Alert logic — score threshold, price levels, portfolio stop/target, score delta. `price_change` gated to ET 04:00–20:00 (`zoneinfo`). `scan_watchlist()`/`scan_portfolio()` score tickers concurrently (`ThreadPoolExecutor`, `_DEFAULT_SCAN_MAX_WORKERS=5`) before running all alert-checking/DB-writes/Telegram sequentially in original order — added 2026-08-18, cut a 50-ticker scan from ~6-7min to ~2min. Required a prerequisite fix in `sec_api_client.py`/`insider_tracker.py` first — see Incident Archive. |
| `score_alert.py` | Score jump/drop alerts for ALL scanned tickers (not just watchlist) — 24h cooldown, shared alert types with watchlist_manager |
| `llm_client.py` | Gemini → Groq fallback (`qwen/qwen3.6-27b`, migrated 2026-08-16 off decommissioned `llama-3.3-70b-versatile`; `reasoning_effort="none"` to keep behavior non-reasoning for tight `max_tokens` budgets like `earnings_sentiment.py`'s 150). `_try_groq()` wrapped in try/except — Groq errors raise `RuntimeError` instead of propagating raw |
| `market_feed.py` | Live indices + macro events. `get_upcoming_macro()` returns approximate weekly schedule — events marked `*` as disclaimer |
| `news_fetcher.py` | Single source of truth for all news in the app (`get_ticker_news()`, `get_market_news()`) — every other module reads news through here, never a raw API call of its own. Sources merged: Google News RSS → yfinance → Finnhub → **Massive/Polygon** (added 2026-08-26 — same paid `MASSIVE_API_KEY` already used by `gap_scanner.py`'s momentum/supertrend Telegram enrichment, now also feeding the general News Impact/Research pages; carries real per-ticker sentiment from `insights`, not the keyword heuristic the other sources fall back to) → Alpha Vantage (25 req/day, gated behind `len(sources)<5`) → Marketaux (100 req/day). Dedup by first-60-chars-of-headline across all sources. |
| `news_impact_analyzer.py` | 3-layer LLM news analysis |
| `macro_signals.py` | Macro signals |
| `telegram_notifier.py` | Telegram send logic — 4000-char truncation guard |
| `scan_worker.py` | Background scan thread (manual dashboard scans, 6-thread pool) |
| `index_loader.py` | iShares index/sector loader — falls back to Wikipedia for S&P 500 when iShares returns HTML; CACHE_TTL=30d |
| `catalyst_scanner.py` | Catalyst Scanner engine — explosion score, PDUFA, unusual options |
| `options_flow.py` | Options chain data, PCR, unusual call/put activity (yfinance). Contracts with OI=0 and volume<500 are skipped; volume≥500 uses `volume/100` ratio instead of a sentinel |
| `auto_watchlist_agent.py` | Auto-adds squeeze/catalyst/momentum/supertrend candidates to watchlist with Telegram summary. `alert_score` uses `AUTO_WL_SCORE_ENTRY` (70) from hysteresis.py, consistent across sources. **Capacity rotation** (`_evict_for_capacity()`, added 2026-08-14): when `watchlist_policy.max_items_total` is reached, the weakest eligible AUTO-added incumbent (3-day avg score, must have cleared `AUTO_WL_MIN_HOLD_DAYS`) is evicted to make room instead of silently dropping the candidate — same "replace weakest with better" logic as `scheduler.run_weekly_rotation()`, triggered on demand instead of weekly. Scored candidates (squeeze/catalyst/momentum) must strictly beat the weakest incumbent's score; unscored candidates (supertrend — no composite gate by design) can only claim a slot whose incumbent has already fallen below `AUTO_WL_SCORE_EXIT` (40), so the least-vetted source can't bump a ticker still earning its place. See [Incident Archive](#2026-08-14--watchlist-pinned-at-capacity-zero-new-adds) for the root cause this fixed. **News catalyst enrichment** (added 2026-08-25, momentum/supertrend only): `run()` calls `gap_scanner.fetch_recent_news_catalyst()` for each ticker in the final `added` list (never the raw scan results, to keep it cheap) and appends a `📰 {title} [{sentiment}, unverified] ({age}m ago)` line to the Telegram summary via `_build_telegram_line()` (the ", unverified" caveat is mandatory on every sentiment tag — added 2026-08-26 after external design review, see `_news_catalyst_suffix()`) — closes the "why did this move" gap for the two full-universe technical scanners, which otherwise run every 30 min with no catalyst context at all. Soft enrichment: a lookup failure never blocks the DB add or the Telegram send. squeeze/catalyst are untouched (already carry their own context). |
| `ibkr_realtime.py` | IB Gateway connector via `ib_async` — historical bars + live snapshot + bracket order placement (GTC legs) + `modify_stop_order()` / `resize_sell_orders()` (in-place order modification) + position/account queries for US stocks. `place_bracket_order()` returns `{"order_id": parent_id, "stop_order_id": stop_leg_id}` (changed 2026-08-29, was a bare int) so the STP leg's id can be recorded on `order_log`; `modify_stop_order()` takes an optional `stop_order_id` to target that exact order (fails safe — no ticker-scan fallback if given but not found live), falling back to the original ticker/type/action scan when omitted. |
| `ibkr_worker.py` | Standalone daemon (Python 3.13, `.venv313`) — runs Supertrend(1H) every 5 min on the monitoring queue, fires combined alerts + submits orders via `order_manager` + syncs positions/daily P&L via `position_tracker`. `sync_positions()` runs at the START of each cycle. `bars_ago != 1` check in `_check_ticker()` — only fires on the exact bar that flipped. `_is_signal_hours(signal_type)` gate — BUY requires 09:30–20:00 ET, SELL 04:00–20:00 ET. Subscribes to `ib.orderStatusEvent` for fill/cancel callbacks; startup reconciliation + periodic fill sweep (every 30 min, `get_executions()` — broker-side, survives reconnects). `_lookup_stop_order_id(ticker)` (added 2026-08-29) fetches the recorded STP leg id from the most recent BUY row, passed into `modify_stop_order()` at both call sites (`_update_trailing_stops()`, `_check_tiered_exits()`'s T1 breakeven move) for exact-order targeting instead of the ambiguous ticker scan. Hosts `TelegramCommandHandler` thread. Windows named mutex singleton (`Global\FinancialAgent_IBKRWorker_Singleton`) + `multiprocessing.freeze_support()`. `PER_CYCLE_BUY_CAP = 3`. All 4 exit checks (`_update_trailing_stops`, `_check_time_stops`, `_check_tiered_exits`, `_check_score_deterioration`) route through `order_manager.submit_exit()`, never call `place_limit_order()` directly. `_reconcile_resting_sell_orders()` — cancels resting SELL orders with no backing position, resizes ones larger than the position they protect. |
| `monitoring_queue.py` | Source of truth for "which tickers get real-time IBKR monitoring" — scanner score ≥ 65 (`SCANNER_MIN_SCORE`) + manual watchlist + recent BUY alerts (72h) + liquidity gate (hysteresis: enter $5M / exit $3M ADV). Queue state persisted to `monitoring_queue_snapshot` DB table; `_persist_queue()` only called when `apply_liquidity_gate=True`. |
| `order_manager.py` | Wraps IBKR order calls; runs execution_engine veto checks before submission; logs every attempt to `order_log` DB table. `submit()` fetches live `portfolio_value` from `position_tracker` (falls back to $100k) and `portfolio_tickers` from `ibkr_positions` for the sector veto. SELL always closes the FULL held position (never a partial risk-sized exit). `submit_exit()` is the single funnel all software-driven exits (time stop, tiered exit, score deterioration) must go through — enforces the trading pause, `shares ≤ held − already-working`, and `order_log` written before the broker call. paper_mode=True default; live requires `IBKR_LIVE=true`. Module-level `_trading_paused` flag, toggled by Telegram `/pause`/`/resume`. |
| `position_tracker.py` | Syncs IBKR positions (including **short/negative-share rows — never filtered**) to `ibkr_positions` every 5 min; records `daily_pnl` once per day (gated to after 09:30 ET, `INSERT OR REPLACE`). `_account_is_ready()` requires `net_liquidation > 0` before an empty position list is trusted to mean "flat" (see IBKR operational trap below). `_raise_short_alarm()` — a short in this long-only bot pauses trading (re-applied every cycle) and sends a throttled Telegram alarm; never auto-covers. `get_current_exposure()` is long-only by contract (returns 0.0 for a short). `get_portfolio_value()` / `get_daily_pnl()` try IBKR first, fall back to DB (`ORDER BY date DESC LIMIT 1`); a paper→live NLV jump >50% is discarded as a fallback delta. |
| `signal_combiner.py` | Supertrend 1H flip → BUY/SELL alert; enforces daily cap (10), 24h dedup. BUY and SELL both have **no composite-score gate** — any monitoring-queue ticker alerts on a bullish flip; SELL is gated only on an open position (`ibkr_positions WHERE shares > 0`). `_try_claim_dedup()` does SELECT+INSERT atomically in one DB connection. |
| `forward_signals.py` | Records every fired alert with entry price + data quality check (`data_quality_flag='SUSPECT'` for the IBKR $105 placeholder or >20% divergence from scan price). `record_fill()` cross-checks `order_log.status`, skips CANCELLED orders, and (added 2026-08-26) only ever targets `signal_type IN ('BUY','SELL')` — see below. Idempotent per `ibkr_order_id` (added 2026-08-29, `fill_order_id` column) — a duplicate fill event (reconnect replay, or the periodic fill sweep re-finding an already-processed execution) no-ops and returns `True` instead of landing on a different, unrelated row once the true target's `fill_price` is no longer NULL. Daily 18:00 job fills `price_after_{1,2,3,7,14,30}d`; weekly Friday 20:00 Telegram digest with win-rate metrics — **raw win rate only, not benchmarked against SPY**, see [Live-Readiness Audit](#2026-08-05--live-readiness-audit-no-measurable-alpha). `weekly_digest()`/the LLM-curation comparison both filter to `signal_type IN ('BUY','SELL')`. **`record_watch_signals()`** (added 2026-08-26) is the data-capture layer for the News-Catalyst Event-Study Measurement — logs `signal_type='WATCH'` rows for every momentum/supertrend hit (wired into `scheduler.py`'s two monitor threads), with or without attached news (`gap_scanner.fetch_recent_news_catalyst()`); dedup key is `(ticker, sentiment_direction)`, stored in the otherwise-unused `ai_verdict` column. Read-side analysis (abnormal returns vs benchmark, cross-sectional date-clustering correction, placebo/control-date test, and — added 2026-08-29 — a ticker-blocked bootstrap for within-ticker serial correlation plus an ADV-matched-pairs liquidity control) lives in `src/catalyst_event_study.py`, run on demand like `trigger_backtest.py` — see Incident Archive, 2026-08-26. |
| `earnings_sentiment.py` | Tier 1 = Finnhub EPS surprise history (free), Tier 2 = LLM transcript analysis (paid). Score 0–5 added to `stock_scorer.py` bonus band. EDGAR fallback when Finnhub is empty (`get_eps_yoy_growth()`, `source='edgar_eps_yoy'`). |
| `hysteresis.py` | Central helper `passes_hysteresis(current, in_set, entry, exit)` + threshold constants (composite, SI, liquidity, watchlist score) — see [Hysteresis Bands](#hysteresis-bands-srchysteresispy) below |
| `stock_forecaster.py` | Ensemble forecaster (ARIMA/MA/ES/MLP). Constructor accepts `point_in_time: datetime` — strictly truncates input to ≤ point-in-time to prevent backtest look-ahead bias. `MLPRegressor.early_stopping=True` uses a shuffled validation split — non-ideal for time series, intentionally unchanged (flagged in code). |
| `news_catalyst_monitor.py` | Background thread — checks news every N min; freshness gate skips articles older than `max_article_age_minutes` (default 45, config key `news_catalyst_max_article_age_minutes`) |
| `insider_cluster_scanner.py` | Added 2026-08-30 — free, market-wide forward-validation data capture for the QuantConnect insider-cluster-buying research lead (see Incident Archive, 2026-08-28/29/30 — DSR~59-65% out-of-sample, not yet built into anything live). Fetches SEC's free daily Form-Type index (`form.YYYYMMDD.idx`), parses every Form 4 filing's own embedded XML for `issuerTradingSymbol` (ticker resolution is independent of which CIK — issuer's or the individual insider's — the row happened to be indexed under; most filings index under the insider's own CIK, not the issuer's, so a naive CIK-lookup approach would silently miss most rows). Every open-market PURCHASE (`transactionCode=='P'`) is logged to `insider_purchase_events`, unfiltered by market cap/liquidity — a complete, universe-agnostic raw record. The sub-$2B/no-coverage universe filter (matching the validated QC config) is applied late, only to tickers that already show 2+ distinct insiders in the trailing 72h window, keeping yfinance lookups cheap (dozens/day, not hundreds). Records a `forward_signals` WATCH row per detected cluster (dedup via a `[insider_cluster]`-prefixed `catalyst_summary`, distinct from the news-catalyst WATCH source's `ai_verdict`-based dedup) — DB-only, no Telegram, never places a trade, mirrors `forward_signals.record_watch_signals()`'s data-capture-only contract. All SEC calls (daily index + per-filing XML) go through a shared, thread-safe rate limiter capped at 8 req/sec total across the whole worker pool, not per-thread — SEC's fair-access policy caps automated access at 10/sec total, and a naive unthrottled `ThreadPoolExecutor` was confirmed live to burst past that and get HTTP 429 on nearly every request, with the block outlasting the burst itself (a single follow-up request minutes later still 429'd). |
| `challenge_portfolio.py` | Added 2026-09-11 — standalone $10,000 paper-trading simulator for a bounded, one-month, head-to-head comparison against an external agent (see [$10K Challenge Strategy](#10k-challenge-strategy-srcchallenge_portfoliopy-run_challenge_cyclepy) below for the full evidence basis and rules). Entry: daily-bar Supertrend bullish flip (`supertrend.scan_supertrend_universe`) filtered to tickers with an open-market insider Form-4 purchase in the trailing 45 days (`massive_insider_client.get_insider_transactions` — fails closed on any error, since that filter alone is this strategy's entire evidence basis). Exit: ATR(14)×2.5 stop that only ever trails upward, plus a 30-day time-stop. Deliberately independent of `order_manager.py`/`execution_engine.py`/`ibkr_worker.py` — never touches the live/paper IBKR path; its own `challenge_trades`/`challenge_positions`/`challenge_equity_log` tables hold all state. Driven by `run_challenge_cycle.py`, run once/day via the user's own always-on Task Scheduler (not from a Claude Code cloud session — see that script's docstring for why). |
| `run_dashboard_tunnel.py` | Cloudflare Quick Tunnel launcher; sends URL on startup + daily heartbeat at 08:05 IL. `_tunnel_healthy()` checks both local cloudflared metrics AND public DNS resolution (`socket.getaddrinfo`) — catches expired quick-tunnel URLs where cloudflared stays running but DNS is deregistered; 3 consecutive failures trigger a tunnel restart + new URL. `start_streamlit()` calls **`_clear_stale_port()`** (added 2026-08-29) before every launch — kills a leftover Streamlit already bound to `STREAMLIT_PORT` (a `CREATE_NO_WINDOW` child survives its own parent being interrupted, since it has no console to receive the signal) only after positively identifying it as our own `dashboard.py` process (PowerShell `Get-NetTCPConnection`/`Get-CimInstance Win32_Process`, no psutil dependency); an unrecognized occupant is logged and left alone. Streamlit's stderr is now captured to a rotating `logs/streamlit_stderr.log` (10MB×5, mirrors `run_scheduler_watchdog.py`'s `_open_stderr_log`) instead of being silently discarded. See Incident Archive 2026-08-29. |
| `run_tunnel_watchdog.py` | Watchdog for `run_dashboard_tunnel.py` — auto-restarts on crash or clean exit, Telegram on startup/restart/crash (rate-limited to 1/5min). Registered as `FinancialAgentTunnelWatchdog` Windows Task. Stop with `stop_tunnel.flag` sentinel |
| `supertrend.py` | Supertrend calculation (ATR-based, Wilder EMA, identical to TradingView Pine Script) — used by `ibkr_worker.py` and `price_alert_monitor.py`. **`scan_supertrend_universe()`** (added 2026-08-14) batch-downloads daily OHLCV (yfinance, ~0.2s/ticker) across the full scan universe and returns every ticker with a fresh bullish flip (`bars_ago==1`), with **no composite-score gate** — mirrors a bare TradingView `alertcondition(buySignal)`, closing the coverage gap left by `monitoring_queue.py`'s `SCANNER_MIN_SCORE=65` filter. |
| `market_regime.py` | BULL / CAUTION / BEAR regime based on VIX thresholds (20/28) + SPY vs SMA200 (`_SPY_HISTORY = "1y"`, ~252 trading days); used by `execution_engine.py` for position sizing and stop adjustments. **`get_fear_greed_index()`** (added 2026-08-29) — composite 0-100 Fear/Greed gauge built from `get_regime()`'s VIX + SPY-vs-SMA200 (market-wide PCR evaluated and skipped — `options_flow.get_options_summary()` pulls multiple full option-chain expirations, too slow/fragile for a page-load-time widget for a signal that's noisy at single-ETF granularity anyway); rendered on the Market page in the VIX card's visual style. Returns `score=None`/"Unavailable" instead of a fabricated score when `get_regime()`'s failure-fallback sentinel (`vix=0.0`) fires. Not CNN's real 7-factor methodology — an in-house approximation, documented as such in-code. |
| `execution_engine.py` | Trade decision engine (Layers -1.5 through 6): daily loss limit (Layer 0), hard veto, confluence check, position sizing scaled by market regime, time-of-day flag, sector exposure guard. **Layer -1: SELL veto** if `exposure <= 0` (no open position, belt-and-braces against a short reporting as "exposure≠0"). **Layer -1.5: already-long BUY veto** — no pyramiding; short positions (shares<0) NOT vetoed since BUY-to-cover is legitimate. BEAR regime veto is BUY-only (`check_hard_vetos(signal_type=...)`) — SELL exits always allowed. Fail-open on DB error. |
| `momentum_scanner.py` | 5-factor momentum score (Price ROC, Relative Strength vs SPY, MA Stack, RSI zone, Volume Surge); vectorized pandas + `yf.download` batch (~0.2s/ticker at scale); runs every 30 min as daemon thread |
| `gap_scanner.py` | Two independent scanners feeding the Premarket Gap Alert and Opening Print Alert one-shot Telegram jobs — see Scheduler Jobs below. `scan_premarket_gaps()` uses **Massive/Polygon per-ticker REST** (`MASSIVE_API_KEY`, `ThreadPoolExecutor` fan-out, `massive_max_workers` in `scheduler_config.json`) — rewritten 2026-08-16 after yfinance was found to always report premarket volume as exactly 0 in production (see Incident Archive). Every call routes through `_thread_session()` — one pooled, keep-alive `requests.Session()` per worker thread (`threading.local()`), not a bare `requests.get()` per call — after the fresh-connection-per-call version crashed the entire scheduler process with a native heap-corruption fault 2026-08-18 (see Incident Archive). `scan_opening_prints()` still `yf.download` 1m regular-session bars; both the `Open` and `Close` series are now filtered to bars whose ET calendar date matches today before any percentage/volume math runs — added 2026-08-19 after yfinance was found to occasionally hand back more than one trading day of 1m bars under rate-limiting/degraded service, letting a stale multi-day-old bar be silently read as the 09:30 open print (see Incident Archive). The two scanners themselves are informational-only: not called from `auto_watchlist_agent.py`, `order_manager.py`, or `ibkr_worker.py`. Added 2026-08-15 after the NMAX gap-catch investigation (see Incident Archive). **`fetch_recent_news_catalyst()`** (added 2026-08-25) is a separate, reusable helper on the same Massive/Polygon infrastructure (`/v2/reference/news`, second-precision `published_utc`, per-ticker sentiment from the `insights` array — not the article's overall framing) — this one IS called from `auto_watchlist_agent.py` for momentum/supertrend Telegram enrichment; see that row above. |
| `long_setup_scanner.py` | 5-factor long setup scanner (RSI zone, MACD crossover, Volume surge, MA alignment, Momentum); daily 09:30; auto-adds top candidates to watchlist |
| `opportunity_tracker.py` | Records every BUY signal as opportunity with T1/stop targets; daily 18:00 fills outcomes; weekly Friday 20:00 Telegram digest with win-rate |
| `alert_monitor.py` | Daily health-check agent at 09:30 — detects noisy alerts, dead threads, portfolio drawdowns >8%; sends Telegram health report. Uses `get_connection()` (WAL-safe, `with` block — no leaked connections). |
| `telegram_command_handler.py` | Two-way Telegram — polls `getUpdates` every 30s; commands: `/status`, `/positions`, `/pause`, `/resume`, `/cancel <TICKER>` (ticker validated `re.fullmatch(r"[A-Z]{1,6}")`); security: only responds to `TELEGRAM_CHAT_ID`; offset persisted to `telegram_command_state` DB table. `/status` reads queue size from `monitoring_queue_snapshot` and P&L from `daily_pnl` (no live IBKR call — avoids hangs). |
| `finnhub_client.py` | Finnhub API wrapper — earnings surprises, transcript list/content |
| `edgar_fcf.py` | SEC EDGAR XBRL provider — free, no API key. `get_edgar_fcf_median`, `get_revenue_cagr`, `get_interest_coverage` (zero-debt → 100.0 cap, not None), `get_current_ratio`, `get_eps_yoy_growth`. Shared `_FACTS_CACHE` (24h TTL) avoids duplicate SEC fetches; `OrderedDict` with LRU eviction + hard cap (`_FACTS_CACHE_MAX_SIZE=100`, added 2026-08-25) since each entry holds the full raw companyfacts payload (1.7-4.9MB/ticker observed) and the scan universe touches thousands of distinct tickers/day. Rate: 0.12s delay between requests. |
| `weight_tuning_backtest.py` | Weight-tuning research tool for `stock_scorer.py`'s `WEIGHTS` dict (added 2026-08-26). Reads every historical `scan_results` row's `raw_data._scores` breakdown (no point-in-time reconstruction needed — it was already recorded live), attaches forward returns from yfinance on the SAME series used for entry (no DB-vs-yfinance price-basis mismatch to patch around, unlike `backtester.py`), and reports per-component Fama-MacBeth monthly IC plus a walk-forward comparison of candidate reweightings vs the current static WEIGHTS. See "Weight Tuning Backtest" section below. |

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
| Fundamentals | 10 | P/E, Revenue CAGR 5yr (EDGAR → yfinance fallback), Margin, Interest Coverage (EDGAR → D/E fallback) — **`_score_fundamentals()` caps at a hardcoded `min(score, 10)`, not `WEIGHTS['fundamentals']`** — see note below |
| DCF | 15 | Margin of Safety vs intrinsic value — **`dcf_score`/`calculate_ps_valuation()` use hardcoded literal buckets (15/11/7/3/0, or 13/9/6/3/1), not `WEIGHTS['dcf']`** — see note below |
| News Sentiment | 5 | Earnings EPS surprise + LLM transcript analysis via `earnings_sentiment.py`; capped by `min(WEIGHTS['news_sentiment'], _es["score"])` (fixed 2026-08-26 — previously uncapped, so the weight had zero effect) |
| Squeeze Bonus | +15 | SI≥20% + vol spike + price up |
| Google Trends | +5 | bonus |

**Signals:** 75+ = STRONG BUY · 60–74 = BUY · 45–59 = WATCH · 35–44 = NEUTRAL · <35 = SKIP

**⚠️ Not every `WEIGHTS` entry is actually a live lever (found 2026-08-26 while scoping the "weight tuning" backlog item):**
- **`forecast`** — already known-inactive (see above): `forecast_score` hardcoded to 0, excluded from `core`/`core_max` entirely. Changing `WEIGHTS['forecast']` does nothing.
- **`news_sentiment`** — was completely dead (see fix above); now a working cap.
- **`fundamentals`** and **`dcf`** — their own component score is computed from hardcoded literals in `_score_fundamentals()` / `calculate_dcf()` / `calculate_ps_valuation()`, NOT scaled by `WEIGHTS['fundamentals']`/`WEIGHTS['dcf']`. The weight value still feeds into `core_max` (the normalization denominator), so raising it doesn't give that component more influence — it dilutes every OTHER component's relative share instead, the opposite of the intuitive effect. **Deliberately left unfixed**: `execution_engine.py::_score_fundamental_pillar()` (lines ~564-569) independently normalizes `fundamentals_score` and `dcf_score` against the SAME hardcoded maxima (`/10.0*15` and `/15.0*15`) for its own Track A/B confluence scoring, which feeds real (paper/live) trade decisions. Rescaling either component by its `WEIGHTS` value without also updating those two lines would silently corrupt the execution engine's confluence math the next time someone tunes `WEIGHTS['fundamentals']`/`WEIGHTS['dcf']` — so this needs a coordinated fix across both files, not a one-line change in `stock_scorer.py` alone. Any future weight-tuning work on these two components must update both call sites together.
- **`momentum`** and **`insider`** — the weight only sets the cap ceiling (`min(weight, raw_points)`); the underlying point formula below the cap doesn't rescale with the weight. Not broken, just a different (still-legitimate) coupling than the fully-proportional components.

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

## Weight Tuning Backtest (`src/weight_tuning_backtest.py`, `run_weight_tuning_backtest.py`)

Built 2026-08-26 for the Open Backlog item "weight tuning based on backtest data." Written and unit-tested (`tests/test_weight_tuning_backtest.py`, pure math, no network/DB) in a network-sandboxed session that could reach neither yfinance nor a populated `data/financial_agent.db` — **run against real production data the same day**, see the 2026-08-26 Incident Archive entry below for the actual result. Re-run it yourself before trusting any *future* output — the numbers move as more `scan_results` history accumulates.

### What it measures

1. **Per-component IC** — does each of `stock_scorer.py`'s 12 scoring components, as currently computed, actually correlate with forward returns? Uses a Fama-MacBeth-style monthly cross-sectional Spearman rank IC (one scan per ticker per month, then mean/t-stat across independent months) — the panel-data analog of `trigger_backtest.py`'s month-clustering discipline, so a ticker scanned 20× in one month can't inflate that month's evidence.
2. **Reweighting comparison** — a walk-forward test (fit on the first `--train-frac` of the date range, evaluate on the untouched remainder) of the current static `WEIGHTS` against an equal-weight split and an IC-proportional data-driven split, both restricted to the components that are actual live levers today (see below). Reports against the same Bonferroni-style multiple-comparison threshold `run_signal_panel.py` uses.

### Why this doesn't need to reconstruct anything historically

Unlike `trigger_backtest.py` (which has to replay OHLCV from scratch because the monitoring queue and composite score can't be reconstructed point-in-time), every historical `scan_results` row written by `score_stock()` already carries the full per-component breakdown in `raw_data._scores`, exactly as computed live at scan time. Only forward returns need fetching — and they're measured on the SAME yfinance daily-close series used for entry, so there's no split/spinoff basis mismatch for `backtester.py`'s `price_sig_adj` workaround to exist for in the first place.

### Which components are actually reweightable

Confirmed by reading `stock_scorer.py`'s code, not by running the backtest — this part is already true today, independent of any data:

| Category | Components | Behavior |
|---|---|---|
| `EXACT_LINEAR` | rsi, macd, ma, volume, short, institutional | `score = weight × frac(inputs)` exactly — a reweighting candidate for these is exact |
| `CAP_ONLY` | momentum, insider | weight only sets the ceiling on a hardcoded-formula raw point value — reweighting is a conservative approximation (never overstates) |
| `HARDCODED` | fundamentals, dcf | computed from literals with **no** dependence on the weight at all today — see Scoring Engine section above. A candidate reweighting here is a "what if this were wired to scale with weight" hypothetical, not a preview of current behavior |
| excluded entirely | forecast (inactive), news/trends (bonus-band, not part of `core`) | reported on for informational IC only |

`run_weight_tuning_backtest.py`'s candidate reweightings only ever touch `EXACT_LINEAR ∪ CAP_ONLY` — never `fundamentals`/`dcf`, because wiring those up for real needs a coordinated fix in `execution_engine.py` too (it independently normalizes both against the same hardcoded maxima for Track A/B confluence scoring — see Scoring Engine section).

### Running it

```bash
python run_weight_tuning_backtest.py                  # 30d horizon, default
python run_weight_tuning_backtest.py --horizon 14
python run_weight_tuning_backtest.py --no-cache        # force fresh yfinance downloads
python run_weight_tuning_backtest.py --min-rows 200    # raise the "enough history" bar
```

Requires: the production `data/financial_agent.db` (months of accumulated `scan_results` — a fresh clone's empty DB raises "no `scan_results` table", not a false "no edge" result) and live yfinance access. Run it on the machine that runs `scheduler.py`.

**Before trusting any output**, check the printed reconstruction sanity check first — it recomputes every row's composite score from `raw_data._scores` under the CURRENT `WEIGHTS` and compares to the score actually stored in the DB. If `pct_within_1pt` isn't ~100%, either `WEIGHTS` has changed since some of that history was scanned (this tool assumes it hasn't) or the formula in `weight_tuning_backtest.py` has drifted from `stock_scorer.py`'s — the script hard-fails on this rather than printing a misleading report.

**Expected finding, not a bug**: the composite score was already found to carry no measurable edge (2026-08-05 Live-Readiness Audit) — a component-level null result (no component's IC survives, "control" wins the walk-forward comparison) would be the consistent, expected outcome, not evidence the tool is broken. **This is what the first real run found** — see the 2026-08-26 Incident Archive entry below.

---

## $10K Challenge Strategy (`src/challenge_portfolio.py`, `run_challenge_cycle.py`)

Built 2026-09-11 for a user-requested, bounded, one-month, $10,000 paper-trading
strategy to be run head-to-head against an external agent. **Read this whole
section before trusting any output it produces.**

### Evidence basis — and its limits

This project had already answered "is there a validated, live-ready, profitable
long-only entry signal here" *before* this module existed. The 2026-08-28
full-day signal-validation pass (Incident Archive) tested technical
trend-following on both large-cap and genuine small-cap universes, pairs
trading, PEAD, catalyst-conditioned entries, and an exit-policy sweep — each
measured with a real train/holdout split and a Bonferroni-corrected
significance bar, not naive overlapping-window stats. Its own written
conclusion: **"are we on track for live trading in 1-2 months: no ... not yet
met by anything tested."** That finding is not superseded by this module —
it's the reason this module looks the way it does.

Exactly one configuration in that entire pass did not collapse to zero or
negative: **Supertrend daily bullish flip, gated on any open-market insider
purchase (Form 4, code 'P') in the trailing 45 days.** It survived a
walk-forward holdout with the right sign but weakened sharply (7d/14d
tradeable t=+1.64 in-sample → t=+0.35 out-of-sample), and win rate improved
49.1%→57.3% out of sample. A *stricter* version of the identical idea
(requiring 2+ insider buys instead of any) looked stronger in-sample and
**flipped negative out of sample** — the session's own live example of "the
best-looking variant is the most overfit one." That is the single most
evidence-backed configuration this project has produced, which is exactly why
it's the one implemented here — and it is still a thin, weak, small-sample
signal that directionally survived a holdout, not a confirmed source of
alpha. This module exists to be a disciplined, capital-preservation-first bet
placed on that thin signal, not a demonstrated money-maker. **Do not read a
good or bad month from this as proof of anything** — one month is far short
of the "≥100 independent clusters" gate the 2026-08-28 entry itself sets
before trusting any signal with real capital.

### Why a new standalone module instead of reusing the live pipeline

`signal_combiner.py`/`order_manager.py`/`execution_engine.py`/`ibkr_worker.py`
are the live (paper or real) IBKR trading path, with a long, hard-won incident
history of subtle bugs there having real financial consequences (see the
"shorts" incident family in the Incident Archive: 2026-08-03, -05, -06, -12).
Bolting a new, unvalidated filter onto that path risked either compromising a
battle-tested system or being silently neutered by its existing gates (its
BUY path already has *no* score gate at all — see Hysteresis Bands — so an
insider filter would need genuinely new plumbing, not a config tweak).
`challenge_portfolio.py` is fully independent instead: its own tables, its
own yfinance-only price fetching, zero calls into the live order path. A bug
here cannot touch the live account, and a bug in the live account cannot
touch this experiment.

### Rules

| Parameter | Value | Why |
|---|---|---|
| Starting cash | $10,000 | as specified |
| Universe | Russell 2000 + S&P 500 | same as the production supertrend-universe monitor |
| Entry signal | Fresh daily Supertrend bullish flip (`bars_ago==1`) | `src/supertrend.py::scan_supertrend_universe()`, unfiltered by composite score (same as production) |
| Entry filter | Any open-market insider purchase in trailing 45 days | the one surviving configuration — see Evidence basis above; fails CLOSED on lookup error or missing `MASSIVE_API_KEY` |
| Price floor | $5.00 | matches `gap_scanner`/`auto_watchlist` supertrend-source convention |
| Max positions | 5 | diversification within a $10k account while keeping position sizes meaningful |
| Sizing | `min(total_equity/5, cash)` capped by `2% of equity / stop distance`, whichever binds tighter | equal-weight with an explicit risk ceiling — this project's own research found no entry-side edge, so disciplined risk management is the actual lever being pulled, not signal quality |
| Stop-loss | `entry_price − 2.5×ATR(14)`, trails up only, never down | matches `price_alert_monitor.py`'s existing 2.5×ATR trailing-stop scale |
| Time-stop | 30 days | matches the challenge's own horizon and `run_exit_simulation.py`'s validated methodology |

Deliberately **not** used: the "no stop-loss, 30-day time-exit only" variant
that scored best in the 2026-08-28 exit-policy sweep. That sweep ran on the
*plain* flip signal, not the insider-filtered one — adopting a result that
was never jointly tested with this entry signal would compound two
separately-cherry-picked choices, exactly the multiple-comparisons trap that
pass spent all day documenting. The realistic, already-validated ATR+time-stop
discipline is used instead.

### External verification (2026-09-11)

User-directed follow-up: check the strategy's design against outside sources
rather than trusting only this project's own internal backtest. Real findings,
with citations — not just "looks reasonable":

- **Insider open-market buying predicting forward returns is a real, decades-
  old, independently-replicated academic finding**, not something specific to
  this project's own research: Seyhun (1986/1998) found ~4.3% abnormal return
  over 300 days for net insider buying; Jeng, Metrick & Zeckhauser (2003) and
  the Wharton "Estimating the Returns to Insider Trading" study found insider
  buying beats the market by roughly 6–10%/year depending on period, with
  **about half the abnormal return accruing within the first month** —
  external support (found after the fact, not designed around) for this
  module's ~45-day insider-purchase window and 30-day time-stop horizon.
  ([InsideArbitrage academic summary](https://www.insidearbitrage.com/academic-research-related-to-insider-trading/), [QuantInsti Form 4 event study](https://blog.quantinsti.com/sec-form-4-insider-trading-python-event-study/))
- **The effect concentrates in smaller, thinly-covered names** (multiple
  sources, consistent) — this is exactly why `_passes_market_cap_filter()`
  (sub-$2B market cap, no analyst coverage via a `forwardPE` proxy) was
  **added to `find_entries()` as a direct result of this check**. The
  original version of this module scanned the full Russell 2000 + S&P 500
  universe with no market-cap filter at all — letting mega-cap flips (Apple,
  Microsoft, any S&P 500 name with a routine Form 4 buy) into the candidate
  pool, exactly where the academic literature says this effect is weakest.
  The new filter mirrors `insider_cluster_scanner.py`'s already-validated
  universe definition exactly, rather than inventing a new threshold.
- **SEC Form 4 must be filed within 2 business days of the transaction**
  ([Investor.gov bulletin](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-69)) — confirms the 45-day lookback window is reading genuinely
  fresh, regulatorily-timely disclosure, not stale filings.
- **Tension found, deliberately NOT acted on**: several sources describe
  cluster buying (2+ insiders) as roughly **2x** the excess return of a
  single insider's purchase ([summary discussion](https://insideract.com/learn/cluster-buying-explained), citing Lakonishok & Lee 2002 and Cohen, Malloy
  & Pomorski 2012) — the opposite direction from this project's own internal
  finding (see "Evidence basis" above) that requiring 2+ insiders on this
  exact joint (flip + insider) signal *flipped negative* out of sample. These
  aren't necessarily contradictory — the internal result rests on a thin
  ~51-unit clustered sample (already flagged as such in Open Backlog) and
  could be small-sample noise rather than a real reversal of a much
  better-powered academic finding — but changing this module's filter from
  "any insider buy" to "2+" on the strength of outside literature alone,
  without testing that specific change against this specific joint signal,
  would be exactly the kind of untested tweak this project's incident
  archive spent all of 2026-08-28 warning against. **Left as "any insider
  buy in 45 days."** Added to Open Backlog as a real thing to test once
  either real challenge-cycle history or a larger-sample internal backtest
  of the 2+ variant exists.
- **ATR stop multiple checked against the "canonical" Turtle Trading system**
  (2x ATR(20), fixed stop, no exception) ([Turtle Trading rules summary](https://trendspider.com/learning-center/richard-dennis-turtle-trading-strategy/)) — this module's 2.5x/ATR(14) is a
  deliberate deviation for consistency with this project's own existing
  `price_alert_monitor.py` trailing-stop convention, not an oversight; 2–3x
  is the broadly accepted range in trend-following practice generally, so
  this was left unchanged.
- **Signal decay is real and worth taking seriously, not just a caveat**:
  McLean & Pontiff (2016) found published anomalies lose much of their edge
  after becoming public knowledge, and insider-buying has been a retail-
  facing, publicly tracked signal (OpenInsider and similar sites) for over a
  decade. This is independently consistent with — not contradicted by —
  this project's own finding that the joint signal's holdout t-stat weakened
  sharply (1.64→0.35): a real-but-decaying effect is exactly what that
  pattern would look like, as opposed to either "fully fake" or "as strong
  as the original 1990s papers."

Net effect of this pass: **one real code fix** (the market-cap filter), one
finding explicitly flagged and left for future testing rather than acted on
without evidence (single vs. cluster), and several design choices (45-day
window, 30-day horizon, 2.5x ATR) that turned out to have independent outside
support that wasn't the reason they were originally chosen.

### Deployment — this cannot run from a Claude Code cloud session

Confirmed live 2026-09-11, not assumed: this sandbox's cron (`CronCreate`) is
session-only (dies when the session ends) and recurring jobs hard-expire
after 7 days regardless; separately, this sandbox's outbound network policy
rejects Yahoo Finance outright (`query2.finance.yahoo.com` etc., organization
proxy policy, not a transient failure). Neither of those is a Claude Code
limitation specific to this repo — no cloud chat session can stay live and
network-connected for 30 days unattended. `run_challenge_cycle.py` has to run
on a machine that stays on, once per trading day (e.g. via the same Windows
Task Scheduler / watchdog pattern already documented under "IBKR Real-Time
Architecture" above). See that script's own docstring for the exact command.

### State

`challenge_trades` (immutable fill ledger, source of truth for cash
accounting), `challenge_positions` (mutable current state, ticker PK, mirrors
`ibkr_positions`'s shape), `challenge_equity_log` (one daily mark-to-market
snapshot). All three added to `database.py::init_db()`. `run_challenge_cycle.py`
also writes a human-readable `data/challenge_report.md` on every run.

`tests/test_challenge_portfolio.py` (29 tests, no network) covers cash
accounting, position sizing (budget cap vs. risk cap, whichever binds
tighter), the insider-filter fail-closed contract, exit logic (stop-loss,
time-stop, trail-never-lowers), entry filtering, and max-positions enforcement
across a multi-candidate batch.

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
- `sec_8k` — Massive/Polygon classified 8-K disclosures (`fetch_sec_8k_events()`, rewritten 2026-08-19), batched via `tickers.any_of` (~100 tickers/call, sequential — no concurrency, no ThreadPoolExecutor). Item-number classification (primary/secondary/tertiary taxonomy, e.g. `strategic_transactions > deal_agreements > acquisition_agreement`) comes from the endpoint directly — the old "not implemented" limitation is closed. In calendar mode (`tickers=None`, the scheduled Catalyst+SI job) this checks whatever `earnings`/`pdufa` already found that run, not a separate ticker list.
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
forward_signals:              ticker, signal_ts, signal_type (BUY/SELL/WATCH), entry_price, composite_score,
                              catalyst_summary, supertrend_level, supertrend_atr, ai_verdict,
                              telegram_sent_at, price_after_{1,2,3,7,14,30}d, return_{1,2,3,7,14,30}d_pct,
                              status (open/matured), data_quality_flag, fill_price, fill_source, fill_order_id
                              (added 2026-08-29 — lets record_fill() recognize a duplicate fill event for
                              an already-applied ibkr_order_id and no-op instead of corrupting a different,
                              unrelated row; see below)
                              — WATCH rows (added 2026-08-26, News-Catalyst Event-Study Measurement) reuse
                              ai_verdict to hold sentiment_direction ('positive'/'negative'/'neutral'/'no_news'),
                              not an AI verdict text; every trading-relevant reader (weekly_digest,
                              record_fill, /status "last signal") filters to signal_type IN ('BUY','SELL')
monitoring_queue_snapshot:    ticker, saved_at — persists accepted monitoring queue across restarts
ibkr_positions:               ticker (PK), shares, avg_cost, unrealized_pnl, market_value, last_synced,
                              exit_tier — synced every 5 min from IBKR; shares can be negative (shorts)
daily_pnl:                    date (PK), day_pnl, net_liquidation, recorded_at
order_log:                    ticker, action, shares, entry_price, stop_price, target_price,
                              status (SUBMITTED/VETOED/FILLED/CANCELLED/ERROR/PAUSED), fill_price,
                              ibkr_order_id, stop_order_id (added 2026-08-29 — BUY-bracket rows only;
                              the STP leg's IBKR order id, lets modify_stop_order() target it exactly),
                              created_at, updated_at, notes
telegram_command_state:       key (PK), value — persists Telegram getUpdates offset across restarts
llm_curated_universe:         week_of, ticker, action (keep/add/remove), rationale, created_at
insider_purchase_events:      ticker, insider_name, transaction_date, price, shares, recorded_at —
                              raw Form 4 open-market-purchase log from insider_cluster_scanner.py,
                              UNIQUE(ticker, insider_name, transaction_date) makes daily re-scans idempotent
```

Migration via `_migrate()` in `database.py` — adds columns without breaking data. `watchlist_alerts` doubles as a **cooldown registry** — every alert system checks it via `_alert_sent_recently(ticker, alert_type, hours)` before sending.

---

## Scheduler Jobs (`scheduler.py`)

> **Times below are from `scheduler_config.json` — NOT from code defaults.** Always check `scheduler_config.json` for the actual runtime schedule; code fallbacks exist but are routinely overridden by config.

| Job | Default Time | Function |
|---|---|---|
| Watchlist Cleanup | 09:30 (tied to `market_digest_time`, no separate config key) | `run_watchlist_cleanup()` |
| Catalyst+SI Alert | 08:05 | `run_catalyst_alert()` |
| Insider Cluster Scan | 08:10 | `run_insider_cluster_scan()` — DB-only, no Telegram; scans **yesterday's** SEC daily Form-4 index (not today's — avoids a race with SEC's own EOD index generation) for 2+ distinct insiders buying the same sub-$2B stock within 72h, forward-validation data capture only |
| Premarket Gap Alert | 15:50 (~08:50 ET) | `run_premarket_gap_alert()` — full-universe scan for a real premarket gap (price move backed by actual premarket volume) vs. prior regular-session close; informational Telegram only, catches genuine overnight-news gappers |
| Opening Print Alert | 16:37 (~09:37 ET) | `run_opening_print_alert()` — full-universe scan comparing each ticker's 09:30 open print to its price ~7 min later with a volume filter; the check that would have caught NMAX (already +12-13% within its first 5-7 minutes on heavy volume) |
| LLM Universe Curation | 07:45, weekly | `run_llm_universe_curation()` — see below |
| Portfolio News | 08:30 | `run_portfolio_news()` |
| Scan + Auto-Watchlist + Breakout | 08:30, 15:00 | `run_scan()` |
| Portfolio | 09:15 | `run_portfolio_scan()` |
| Market Digest | 09:30 | `run_market_digest()` |
| Long Setups | 09:35 | `run_long_setups()` — `long_setups_enabled` |
| Alert Monitor Health Check | 09:40 | `run_alert_monitor()` |
| Watchlist | 12:00 | `run_watchlist_scan()` |
| Squeeze + SI Alert | 12:05 | `run_squeeze_scan()` |
| Weekly Rotation | Monday 08:15 | `run_weekly_rotation()` — replaces the single weakest auto-added ticker if a momentum candidate scores ≥75 and beats it |
| Forward Outcomes Update | 18:00 daily | `run_forward_outcomes_update()` |
| Opportunity Outcomes Update | 18:00 daily | `run_opportunity_outcomes()` |
| Forward Signals Digest | Friday 20:00 | `run_forward_digest()` |
| Opportunity Digest | Friday 20:00 | `run_opportunity_digest()` |
| Price Monitor + Supertrend | every 5 min (thread) | `_price_monitor_thread()` |
| Momentum Monitor | every 30 min (thread), market hours | `_momentum_monitor_thread()` — scans `momentum_indices`, auto-adds via `auto_watchlist_agent`. Also calls `_record_watch_signals()` (added 2026-08-26, `"event_study"` config gate) on EVERY hit before `auto_watchlist_agent` filters run — see forward_signals.py's `record_watch_signals()` above and the 2026-08-26 Incident Archive entry. |
| Supertrend Universe Monitor | every 30 min (thread), market hours, **staggered 15 min after Momentum Monitor** | `_supertrend_universe_monitor_thread()` (added 2026-08-14) — scans `supertrend_universe_indices` via `scan_supertrend_universe()`, auto-adds every fresh bullish flip with no score gate (only price ≥ $5 + optional liquidity). The stagger exists because both threads do a full-universe `yf.download()` and hit `YFRateLimitError` when they land in the same instant. Also calls `_record_watch_signals()` (same as Momentum Monitor above). |
| News Catalyst Monitor | every 15 min (thread) | `catalyst_monitor_thread()` |

### Auto-Watchlist (`run_scan`)
Score ≥ 70 and not already in the watchlist → auto-add (`alert_score=70`, `alert_pct=5.0`, notes `"Auto: score {N} on {date}"`). One Telegram summary per run. Immediately writes `score_threshold`/`price_change`/`score_delta_rise` suppression cooldowns so the 12:00 watchlist scan doesn't re-fire for the same stocks. Controlled by `"auto_watchlist": true`.

### Auto-Exit (`run_scan` + `run_watchlist_scan`)
Auto-added tickers (notes prefix `"Auto:"` or `"Auto ["`) with score ≤ 40 are removed after a **minimum hold of 3 days**. Cooldown rows (`auto_exit_score`, `auto_exit_cooldown`) are written **BEFORE** `watchlist_remove()` in both call sites — a failed remove still leaves the cooldown in place. **Re-entry block**: 7 days, unless score ≥ 75.

### LLM Universe Curation (`src/llm_universe_curator.py`, currently **enabled**)
Weekly (07:45), gated by `llm_universe_curation_enabled` in config. Curates the top-150-by-score digest down to ~80 tickers worth active monitoring — an LLM-judged **narrowing**, not a discovery mechanism (never introduces a ticker the quant scanner didn't already surface, enforced by an allowlist check on the response). Low-turnover by design (last week's list given as an anchor). Persisted to `llm_curated_universe`; stale (>10 days) curation is ignored, not enforced.

### Squeeze Scan — DB-only, not Telegram
`🚨 High SI+DTC Alert` (SI>20% AND DTC>15, 24h cooldown) + `🔥 Top Squeeze Candidates` (top 10) are built into one combined message, but that message is never sent — `run_squeeze_scan()` assigns it to a throwaway variable with the comment "Telegram suppressed 2026-05-20 — user wants only IBKR real-time + catalyst alerts. DB kept." Matches the `squeeze_si_alert` row in the Alert Types table below (DB-only); this section previously said "one Telegram" and was stale.

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
SEC_API_KEY             # sec-api.io — src/sec_api_client.py, src/insider_tracker.py. Free tier: 100 req/day, exhausted daily by a full-universe scan — falls back to the EDGAR XML scraper on 429, see Known Limitations
SEC_USER_AGENT_EMAIL
MARKETAUX_API_KEY       # optional — src/news_fetcher.py::fetch_marketaux_news, one of several news sources merged into get_ticker_news(); currently unset in production, that source path is simply skipped
TELEGRAM_ENABLED
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
IBKR_LIVE              # "true" to enable live order placement (port 4001); absent or any other value = paper mode (port 4002)
MASSIVE_API_KEY         # Massive/Polygon.io REST API — paid "Starter" plan, $29/mo. Used by src/gap_scanner.py (scan_premarket_gaps, fetch_recent_news_catalyst) and src/news_fetcher.py::fetch_massive_news (added 2026-08-26)
```

---

## Known Limitations

- Alpha Vantage: 25 req/day (free tier) — daily quota counter warns at 23 requests
- sec-api.io: 100 req/day (free tier) — a full-universe scan (hundreds of tickers, each hitting `insider_tracker.py`) exhausts this daily; expected, fails gracefully to the EDGAR fallback, not a bug to chase
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
- **News-Catalyst Event-Study Measurement** (`record_watch_signals()` in `forward_signals.py`, analysis in `src/catalyst_event_study.py`, built 2026-08-26) has **essentially zero accumulated WATCH-row history as of ship date** — it was just wired into the two monitor threads, so `direction_report()`/`placebo_test()` will report `n` too small to trust for real weeks. This is expected, not a bug: the whole point of building the data-capture layer first is to let real history accumulate before drawing any conclusion, the same sequencing `weight_tuning_backtest.py` and `trigger_backtest.py` both followed. Do not treat an early low-n run as a null result.

---

## Open Backlog

- [ ] Test whether requiring 2+ distinct insiders (instead of any single insider buy) improves `challenge_portfolio.py`'s real results, once either real challenge-cycle history or a larger-sample internal backtest exists. Flagged 2026-09-11 during external verification: academic literature says cluster buying ≈2x a single insider's excess return, the opposite direction from this project's own thin-sample (~51 units) internal finding that a 2+ cut flipped negative out of sample — see "$10K Challenge Strategy" → "External verification" above. Not changed without real evidence on this exact joint signal.
- [ ] Run `run_challenge_cycle.py` daily for 30 days (built 2026-09-11, started once deployed to a machine that stays on) — record final equity/return/trade log and compare against the insider-filtered-Supertrend backtest's own holdout numbers (t=+0.35/+0.02, see "$10K Challenge Strategy" above) before drawing ANY conclusion from a single month of one strategy. Update that section (or the Incident Archive) with what actually happened, good or bad — same "needs real history first" discipline this project applies everywhere else (weight tuning, event study, insider cluster scanner).
- [x] `run_scan()` parallelization — implemented 2026-08-29: `_score_scan_universe()`/`_score_scan_ticker()` (scheduler.py) mirror `watchlist_manager.py`'s 2-phase pattern exactly — Phase 1 scores the flattened ~2,463-ticker universe concurrently (`ThreadPoolExecutor`, `_SCAN_MAX_WORKERS=5`, same rationale as `_DEFAULT_SCAN_MAX_WORKERS`), Phase 2 (DB writes, auto-exit, breakout, Telegram) is byte-for-byte unchanged and still strictly sequential in original ticker order. Checked `scheduler-worker-load-issue-399d18` first as the note suggested — that worktree has no `ThreadPoolExecutor`/parallelization of its own, unrelated. `tests/test_scheduler_parallel_scan.py`, 7/7. **Not yet deployed to the live scheduler** — needs a restart to take effect, same as every prior fix in the Incident Archive.
- [x] Scheduler crash-traceback capture — implemented 2026-08-29: `run_scheduler_watchdog.py` now redirects the child `scheduler.py` process's stderr to a rotating file (`logs/scheduler_stderr.log`, 10MB × 5 backups) instead of `subprocess.DEVNULL`, with a timestamped attempt header per launch and a logged (never silent) fallback to DEVNULL if the file can't be opened. `tests/test_scheduler_watchdog_stderr.py`, 6/6. This only helps the *next* crash — the original 5 `returncode=1` crashes from 2026-08-26 (11:34–14:29) have no recoverable traceback and stay formally undiagnosed. Also not yet deployed to the live watchdog.
- [x] Sector-level sub-scanning in main Scan page — **stale claim, found false 2026-08-29**: `page_scan.py` has had an Index dropdown + per-sector `multiselect` + per-sector scan loop (`_load_tickers(sel_index, sec, ...)`) since the 2026-06-29 QA hardening commit (`3098f37`). Not a gap.
- [x] Fear & Greed Index widget — implemented 2026-08-29: `src/market_regime.py::get_fear_greed_index()`, composite 0-100 gauge from VIX + SPY-vs-SMA200 (PCR evaluated, skipped as not a cheap addition — see module table above), rendered on the Market page above the existing VIX card in the same visual style. `tests/test_page_market_fear_greed.py`, 12/12 (this is also the repo's first persisted `AppTest`-based page-render test — prior AppTest usage was ad hoc/one-off per the Incident Archive).
- [x] Weight tuning based on backtest data — `WEIGHTS` dict in `stock_scorer.py` is static. 2026-08-26: found `forecast`/`news_sentiment` were dead weights (news_sentiment now fixed) and `fundamentals`/`dcf` are disconnected from their own weight value (see Scoring Engine section above) — tune those two only together with `execution_engine.py`'s hardcoded normalization. Built `run_weight_tuning_backtest.py` and ran it against real production data the same day — see the 2026-08-26 Incident Archive entry below. Result: inconclusive on reweighting (only 2 out-of-sample test months so far), no action taken on `WEIGHTS`. Re-open and re-run in a few months once more `scan_results` history accumulates — do not treat this as permanently closed.
- [x] Russell 2000 support in main Scan page — **stale claim, found false 2026-08-29**: `page_scan.py`'s Index dropdown is `list_indices()` from `index_loader.py`, the same `INDICES` dict Catalyst Scanner/scheduler use — Russell 2000 has been selectable there since the same 2026-06-29 commit above. Not a gap.
- [ ] `supertrend_triple_bull/bear` — consider routing through `signal_combiner.evaluate()` for the same cap+dedup discipline `combined_buy/sell` gets (currently DB-only, uncapped)
- [x] `modify_stop_order()` STP ambiguity — fixed 2026-08-29: `order_log.stop_order_id` (new column) records the STP leg's IBKR order id at bracket-submission time (`place_bracket_order()` now returns `{"order_id":..., "stop_order_id":...}`, was a bare int); `modify_stop_order(ticker, price, stop_order_id=None)` targets that exact order when given (fails safe, no fallback if not found live), falls back to the original ticker/type/action scan when omitted (pre-migration rows, or a not-yet-updated caller). Wired into both `ibkr_worker.py` call sites via `_lookup_stop_order_id()`. `tests/test_ibkr_realtime.py` (new, 5 tests — no prior coverage existed for this function at all).
- [x] `record_fill()` idempotency — fixed 2026-08-29: new `forward_signals.fill_order_id` column; a duplicate fill for an already-recorded `ibkr_order_id` no-ops (returns `True`) before reaching the "most recent NULL fill_price row" query, closing the exact corruption path (a second call would otherwise land on a different, unrelated row). Covered by a test that replays the corruption mechanism directly (older unrelated row must stay untouched across a duplicate call).
- [ ] Track whether Supertrend-universe / capacity-rotation additions (2026-08-14) perform differently from the existing sources in `forward_signals` before treating the wider net as a return improvement, not just a coverage one
- [ ] Manually review `gap_scanner.py` Premarket Gap Alert / Opening Print Alert hit-rate and usefulness after a few weeks of live output before considering wiring either into `auto_watchlist_agent` (added 2026-08-15, see Incident Archive)
- [x] Plausibility clamp added to `scan_opening_prints()` 2026-08-25 (rejects `move_pct`/`dollar_volume` outliers) — root cause of the underlying corruption is still unconfirmed; if it recurs a third time with the clamp in place, revisit the cross-ticker-contamination theory in the Incident Archive
- [x] Deployed the 2026-08-25 `edgar_fcf._FACTS_CACHE` LRU-cap + `gc.collect()` fixes to the live `scheduler.py` process same day (restart via watchdog crash-recovery) — 30-min post-restart sampling showed memory oscillating flat, not climbing. Full-day validation (across the 08:30/15:00 main scan's DCF/EDGAR-heavy path) still outstanding; see Incident Archive
- [ ] Full-universe catalyst coverage still has a ~23h blind spot (09:37 ET → next day 08:50 ET) — only two one-shot snapshots + 30-min technical-only sweeps exist; the 2026-08-25 news enrichment fix adds "why" to existing hits but doesn't add more frequent full-universe checks. Options discussed: more frequent premarket snapshots, or extending real-time News Catalyst Monitor beyond portfolio+watchlist (rejected as too costly at full-universe scale). See Incident Archive
- [ ] Run `src.catalyst_event_study.direction_report()` / `placebo_test()` once a few weeks of real WATCH-row history has accumulated (built 2026-08-26, see Incident Archive) — same "needs real history first" sequencing as `weight_tuning_backtest.py`. Also still open from the 2026-08-25 entry: whether a hit-with-fresh-news that doesn't clear `auto_watchlist_agent`'s filters should ever get its own Telegram alert type — explicitly not decided, do not build without re-confirming. **Early exploratory run 2026-08-31** (769 WATCH rows, but only 3 independent trading dates — still far short of "a few weeks"): no direction/horizon cleared the clustering bar, and the negative abnormal drift showed up in the `no_news` control bucket too (as strongly as, or more than, the sentiment buckets), which given that week's broad market selloff (Iran-strikes risk-off) is at least as plausible an explanation as any news-catalyst effect. Not a result — re-run once real history accumulates. Same run surfaced and fixed a real bug: `_forward_pct_return()` returned `NaN` instead of `None` for a NaN price bar (yfinance data gap), and `_t_stat()` only filtered `None`, not `NaN` — one bad bar silently poisoned an entire aggregate mean/t-stat, with `placebo_test(horizon=3)` reporting `distinguishable_from_placebo: False` from a `nan` mean, indistinguishable from a genuine null result. Fixed in both `_forward_pct_return` and `_t_stat` (catalyst_event_study.py), plus the identical latent gap in `trigger_backtest._forward_return()` (explicitly "the same algorithm ... reused as a pattern" — `trigger_backtest.stats()` already filtered NaN correctly, only the leaf function didn't). Regression tests added to both test files.
- [x] Within-ticker serial-correlation correction — implemented 2026-08-29 as `_block_bootstrap_stats()` (ticker-blocked bootstrap, `src/catalyst_event_study.py`), opt-in via `direction_report(block_bootstrap=True)`. Chosen over Newey-West: per-ticker WATCH-observation counts/spacing are too irregular for one global lag-truncation parameter; resampling by ticker needs no such assumption. Proven on synthetic data (60 rows, 6 tickers, zero date-overlap so `_date_clustered_stats()` is provably blind to this axis) to correctly collapse a naive/clustered t≈3.4 false positive to |t|&lt;2 once only-6-independent-clusters is accounted for. `tests/test_catalyst_event_study.py` 46/46. Unit-tested only — real WATCH history still barely exists, same sequencing as the rest of this module.
- [x] Liquidity-matched control group — implemented 2026-08-29 as `liquidity_matched_direction_report()` (`src/catalyst_event_study.py`): greedy nearest-neighbor matched pairs on ADV (`_fetch_ticker_adv()` reuses `monitoring_queue.py`'s exact `(Close*Volume).tail(20).mean()` formula), without replacement, in log10 space with a caliper. Proven on synthetic data to collapse a &gt;4x deliberate ADV skew between groups to near-parity (0.8–1.25x) after matching. Same "unit-tested, no real history yet" caveat as above.
- [ ] Deploy the 2026-08-26 dedup/`news_publisher`/`news_age_minutes` fixes to the live scheduler (committed but not yet restarted onto as of that entry) — restart required, same pattern as every prior deploy.
- [x] `tests/test_new_fixes.py`'s hand-rolled `forward_signals` CREATE TABLE — fixed 2026-08-29: replaced with a real `db.init_db()` call against a `monkeypatch`-redirected `DB_PATH` (mirroring the pattern `tests/test_catalyst_event_study_watch_signals.py::temp_db` already used), so this schema can no longer drift from production. All 9 tests in the file re-verified passing.
- [ ] Per-ticker baseline normalization for the opening-print plausibility clamp (each ticker's own rolling median volume/open-range instead of one universal $2B/80% ceiling) — recommended by the 2026-08-26 IdeaDistill design review, needs a nightly batch job, not yet built. See Incident Archive.
- [ ] Evaluate a second data vendor for the opening-print feed — two unexplained corrupted-reading incidents from one vendor in one week (2026-08-19, 2026-08-21/24) is itself a signal worth investigating, per the same review. Not started.
- [ ] Deploy the 2026-08-26 clamp-visibility/tripwire fix (`gap_scanner.py`, `scheduler.py`) to the live scheduler — restart required, same pattern as every prior deploy.
- [ ] Review `logs/memory_soak.csv` in ~3 days (soak started 2026-08-26 ~14:08 IL, `FinancialAgentMemorySoak` scheduled task, 4-day duration) — confirm no multi-day memory creep on top of the per-cycle `gc.collect()` fix. If creep resumes, the next moves are allocator tuning (`MALLOC_ARENA_MAX`, jemalloc) or smaller batch sizes, per the 2026-08-26 IdeaDistill review. Delete the scheduled task + script once the soak concludes either way.
- [ ] Run `direction_report(promoted_only=True)` vs `direction_report(promoted_only=False)` once enough WATCH data has accumulated and compare — the survivorship check built 2026-08-26 has no data to run against yet. If the two disagree, that's evidence a full-population finding wouldn't hold for the actual promoted/enriched subset.
- [ ] Instrument whether recipients act differently on enriched-with-sentiment vs. unenriched Telegram alerts (2026-08-26 IdeaDistill review, recommendation #2 — not built). No click-tracking exists for Telegram; the realistic proxy is correlating `portfolio` table adds against `auto_wl_momentum`/`auto_wl_supertrend` alerts with vs. without a sentiment line, on a similar time-window join to the survivorship check above. A real feature, not a quick addition.
- [ ] Decide whether to cut the sentiment enrichment entirely or feed it upstream into `auto_watchlist_agent`'s filter, once the above two items produce actual data — explicitly not decided now, blocked on measurement that doesn't exist yet (2026-08-26 IdeaDistill review).
- [ ] **Formalize a reusable train/holdout split in `src/trigger_backtest.py`.** A 2026-08-28 signal-validation pass (see Incident Archive) hand-rolled a time-based dev/holdout split (first 70% of months vs. last 30%, never touched during discovery) separately in each throwaway research script, inconsistently — one early version even clustered by calendar month only instead of reusing `clustered_stats()`'s real per-ticker non-overlap discipline, understating its own significance. `weight_tuning_backtest.py` already has a `--train-frac` walk-forward split as precedent; `trigger_backtest.py`/`run_signal_panel.py` should get the equivalent as a real function, not something reinvented ad hoc under time pressure next time.
- [ ] **Survivorship bias in every `get_index()`-driven backtest universe.** `src/index_loader.py` returns the *current* Russell 2000 / S&P 500 constituent list (30-day-TTL cache), not point-in-time historical membership. Every backtest this project runs over a multi-year window (the 2026-08-28 small-cap signal panel, 35 months) silently excludes any ticker that was delisted, went bankrupt, or fell out of the index during that window — a real investor at the time would have been exposed to those. Likely biases results *optimistic*, not pessimistic (failures are removed from the sample). No known cheap fix — point-in-time index membership isn't a data source this project currently has.
- [ ] **No purged/embargoed cross-validation or advanced multiple-testing correction.** `run_signal_panel.py`'s Bonferroni-style threshold (`2.0 + 0.55·ln(N)`) is the only data-snooping control that exists. There's no purged k-fold / embargo-period discipline (López de Prado), no White's Reality Check / SPA test / Deflated Sharpe Ratio, and no persistent cross-session log of how many hypotheses have been tested against this data in total — the running trial count currently only lives in conversation history, not in a file. A 2026-08-28 exit-policy sweep (7 `ExitConfig` variants) nearly demonstrated this live: the best-looking variant (no stop-loss, t≈1.4) would have been a textbook pick-the-best-of-N overfit if taken at face value instead of measured against the panel's own Bonferroni bar.
- [ ] No Monte Carlo / bootstrap resampling of any trade sequence, and no systematic transaction-cost sensitivity sweep — every 2026-08-28 backtest used one fixed `friction_pct=0.20`. Likely *understates* real cost for the small-cap universe specifically (thin-name bid-ask spread can easily exceed 0.2% round-trip; today's small-cap "no edge" finding may itself be optimistic on this axis, not just on survivorship).
- [ ] No check, anywhere in the current harness, for return concentration (is a positive mean driven by a broad distribution or a couple of outlier winners?) or regime-sliced performance (`spy_bull`, already captured per-event by `run_backtest()`, has never actually been used to slice a result) before today. First exercised in the 2026-08-28 insider-conviction backtest (see Incident Archive) — worth making a standard part of every future signal report, not a one-off.
- [ ] Run an outlier-concentration check (per-trade `net_pct` distribution, not just the mean) on the 2026-08-28 insider-conviction backtest's `2+ buys, non-bull regime` cell (mean +15%, t=1.89, but only 51 clustered units across 6 months) before treating it as anything more than noise — see Incident Archive. Separately, that cell's whole non-bull-regime population is thin (6-7 months across the full 35-month sample); more history there specifically, not just more time in general, is needed before the regime-interaction finding can be trusted.
- [ ] Add unusual-options-activity as a new field on `forward_signals.record_watch_signals()`'s WATCH rows (`src/options_flow.py`'s live `get_options_summary()` output at signal time), alongside the existing news-catalyst fields. Confirmed 2026-08-28 (see Incident Archive) that this is NOT retroactively backtestable — `yfinance` has no historical options-chain data — so it can only ever be evaluated by collecting new data going forward, same "needs real time to pass" category as the WATCH layer itself, not a quick backtest like the insider-conviction one turned out to be.


---

## Incident & Research Archive

Moved to [.claude/rules/incident-archive.md](.claude/rules/incident-archive.md) (2026-08-26, `/doctor` cleanup) — same content, loads automatically whenever a session touches `src/**`, `scheduler.py`, `_pages_modules/**`, or `tests/**`. Read it directly if you're working outside those paths and need incident history.
