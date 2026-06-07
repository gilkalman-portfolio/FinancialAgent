# Financial Agent
**AI-Powered Stock Scanner & Financial Analysis Dashboard**

Streamlit dashboard combining technical analysis, DCF valuation, short squeeze scanning, catalyst detection, news impact analysis, and real-time Telegram alerts.

---

## Features (11 pages)

| Page | What it does |
|---|---|
| **Scan** | Multi-factor scoring with DCF column. Background scan worker. |
| **Research** | Deep Dive (DCF card, Bull/Bear debate, AI analysis) + Compare Side-by-Side |
| **Watchlist & Portfolio** | Price targets, real-time alerts, P&L dashboard, sector allocation, 12 alert types |
| **Market** | Live indices, Futures bar, VIX level, sector heatmap, mood, earnings, macro events |
| **News Impact** | 3-layer LLM analysis + Stock News + Upcoming Events tab |
| **Short Squeeze** | Squeeze Score 0–100, sparklines, AI Verdict, sector scan mode, insider overlay |
| **Catalyst Scanner** | Small/mid-cap stocks with upcoming catalyst + explosion score, news catalyst flag, insider buy/sell detail |
| **Options Flow** | Options chain data, PCR, unusual call/put activity |
| **Backtest** | Signal accuracy validation |
| **History** | Score trend over time per ticker |
| **Scheduler** | Automated scans + Telegram news digest + price target + volume spike monitor |

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
SEC_API_KEY=...                  # optional — sec-api.io, enables fast insider data
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

```bash
streamlit run dashboard.py                          # → http://localhost:8501
python -m pytest tests/test_scorer.py              # 56 tests
python -m pytest tests/test_insider_sec_api.py     # insider + sec-api tests
```

---

## Background Scheduler (Watchdog + Task Scheduler)

הסריקות וה-price alerts רצים ברקע גם כשהדשבורד סגור, ומתחילים אוטומטית עם Windows — ללא תלות ב-NSSM.

```
run_scheduler_watchdog.py    # מאתחל את scheduler.py אוטומטית אחרי כל קריסה
logs/scheduler.log           # פעילות ה-scheduler
logs/watchdog.log            # crash + restart events
```

**רישום חד-פעמי (בוצע):**
```powershell
# Task Scheduler: FinancialAgentWatchdog — At Startup, Run as Admin
Start-ScheduledTask -TaskName "FinancialAgentWatchdog"   # הפעלה ידנית
Stop-ScheduledTask  -TaskName "FinancialAgentWatchdog"   # עצירה מלאה
# עצירה רכה (jobs בלבד): כפתור Disable בדף Scheduler ב-Streamlit
```

**ארכיטקטורה:**
```
Watchdog (run_scheduler_watchdog.py)
└── scheduler.py
    ├── Market Digest:        08:00  → Telegram
    ├── Catalyst+SI Alert:    08:05  → Telegram (SI≥10% + catalyst ≤7d + explosion≥40)
    ├── Portfolio News:       08:15  → Telegram
    ├── Squeeze+SI Alert:     07:45  → Telegram (SI>15% + DTC>10)
    ├── Scan:                 08:30, 16:30  (auto-watchlist if score≥70)
    ├── Watchlist:            09:00  (score/price/supertrend/delta alerts)
    ├── Portfolio:            09:15  (stop loss / target / score drop / delta alerts)
    ├── Price Monitor:        כל 5 דקות (thread) → price_target + volume_spike + supertrend (15m+daily)
    ├── Momentum Monitor:     כל 30 דקות (thread)
    └── News Catalyst Monitor: כל 15 דקות (thread)
```

---

## Auto-Refresh (Streamlit)

דפים נבחרים מתרעננים אוטומטית באמצעות `streamlit-autorefresh` — רק כשאתה נמצא בדף.

| דף | מרווח | סיבה |
|---|---|---|
| Market | 10 דקות | indices + futures |
| Watchlist | 5 דקות | מחירים live |
| News Impact | 20 דקות | LLM calls יקרים |
| Squeeze | 15 דקות | שילוב API calls |
| Scan / Research / Backtest | ידני בלבד | סריקות ארוכות |

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
| Fundamentals | 10 | P/E, Revenue Growth, Margin, D/E |
| **DCF** | **15** | **Margin of Safety vs intrinsic value** |
| Squeeze Bonus | +15 | SI≥20% + volume spike + price rising |
| Google Trends | +5 | bonus |

**Signals:** 75+ = STRONG BUY · 60–74 = BUY · 45–59 = WATCH · 35–44 = NEUTRAL · <35 = SKIP

---

## Catalyst Scanner

Finds small/mid-cap stocks with an upcoming catalyst and high explosion potential.

| Component | Max pts | Logic |
|---|---|---|
| Urgency | 30 | Today=30 · 1d=27 · 3d=17 · 7d=8 |
| SI% Fuel | 25 | ≥20%=25 · ≥15%=18 · ≥10%=11 |
| Float Amplifier | 20 | ≤5M=20 · ≤15M=16 · ≤40M=11 · ≤100M=6 |
| Volume Building | 10 | ≥3x=10 · ≥2x=7 · ≥1.5x=4 |
| Insider Buying | 10 | net buying 90d (Form 4) |
| Momentum | 5 | 5-day price change |
| Unusual Options | +8 | Unusual CALL activity via yfinance; +4 if PCR<0.7 |

**Labels:** ≥70 = HIGH · 50–69 = MEDIUM · 30–49 = LOW · <30 = WATCH

**Catalyst types:** Earnings (Nasdaq API) · Analyst Upgrade (Finnhub) · SEC 8-K (EDGAR, parallel fetch) · **💊 FDA/PDUFA** (BioPharma Catalyst, no key, 6h cache)

**Extra columns:** News Catalyst 🔵 (regex on headlines: FDA/merger/deal/approval/…) · Insider 🟢/🔴 (buy/sell count from Form 4) · 📊 Options (unusual call activity badge)

**Biotech scan:** Index / Sector → Russell 2000 → Health Care (~150 tickers)

---

## Watchlist Alert Types (12)

| Type | Trigger | Frequency |
|---|---|---|
| `score_threshold` | Score ≥ configured threshold | On scan |
| `price_change` | Price moved ≥ configured % | On scan |
| `price_target` | Price within $0.05 of target | Every 5 min |
| `price_above` | Price crossed above level | On scan |
| `price_below` | Price dropped below level | On scan |
| `stop_loss` | Portfolio: price ≤ stop loss | On scan |
| `target_hit` | Portfolio: price ≥ target | On scan |
| `score_drop` | Portfolio: score < 35 | On scan |
| `volume_spike` | Volume > X × 10d avg | Every 5 min |
| `supertrend_flip` | Supertrend BUY/SELL flip — Daily bars | Every 5 min + on scan |
| `supertrend_intraday_flip` | Supertrend BUY/SELL flip — 15-min bars | Every 5 min |
| `score_delta_drop` | Score fell ≥15 pts since last scan | On scan |
| `score_delta_rise` | Score rose ≥15 pts since last scan | On scan |

Cooldown: 24h per ticker+type · price_target + volume_spike: 4h · supertrend_intraday_flip: 1h

---

## Supertrend Alert (`src/supertrend.py`)

Pine Script v4 algorithm implemented in Python (1:1 translation).

- **Parameters:** ATR period=10, multiplier=3.0 (Pine Script defaults)
- **BUY signal:** trend flips from -1 → 1 (price crosses above support band)
- **SELL signal:** trend flips from 1 → -1 (price crosses below support band)
- Enable per ticker in Watchlist → `📈 Supertrend Alert` checkbox
- **Real-time check every 5 min** via `price_alert_monitor.py`:
  - **15-min bars** (`interval="15m", period="5d"`) — intraday flip detection, cooldown 1h
  - **Daily bars** (`period="60d"`) — trend confirmation, cooldown 4h
- Also checked during scheduled `scan_watchlist()` at 09:00

---

## Portfolio P&L Dashboard

The Portfolio tab shows a full P&L breakdown — no scan required, uses live prices.

**KPI bar (6 metrics):** Invested Capital · Portfolio Value · Total P&L · Return % · Winners · Losers

**3 sub-tabs:**
| Tab | Content |
|---|---|
| **P&L Table** | Sortable DataFrame: Price, Entry, Shares, Invested, Value, P&L $, P&L %, Score, Sector, Stop, Target |
| **Sector Allocation** | Bar chart of portfolio weight % per sector + summary table (sector fetched from yfinance, cached 1h) |
| **Position Cards** | Detailed per-position cards with stop/target progress bar |

---

## Market Page

| Widget | Source | Notes |
|---|---|---|
| **Futures Bar** | yfinance (ES=F, NQ=F, YM=F, GC=F, CL=F) | Pre/post market direction |
| **VIX Card** | yfinance (^VIX) | Level: Calm / Normal / Caution / Fear / Panic |
| **Indices** | yfinance | S&P, Nasdaq, Dow, Russell, Oil, Gold, Bond, FX, BTC |
| **Sector Heatmap** | yfinance (XL* ETFs) | 1D / 5D |
| **Market Mood** | Alpha Vantage + Finnhub | Sentiment from 40 articles |
| **Earnings** | Nasdaq API | This week, by date |
| **Macro Events** | Hardcoded schedule | CPI, Fed, NFP, etc. |

**VIX Levels:** <15 Calm · 15-20 Normal · 20-30 Caution · 30-40 Fear · 40+ Panic

---

## DCF Valuation

5-year discounted cash flow model built into the scoring engine.

**Formula:** `Intrinsic Value = Σ(FCF_t / (1+WACC)^t) + Terminal Value`

- Growth rate: avg(revenue growth, earnings growth), clamped 3%–25%
- WACC: 10% base + up to 3% for high leverage · Terminal growth: 2.5%
- Data: `freeCashflow` from yfinance

**Margin of Safety → Score:** ≥40% → 15pts · 20-40% → 11pts · 5-20% → 7pts · 0-5% → 3pts · <0% → 0pts

---

## LLM Architecture

- **Primary:** Gemini 2.5 Flash
- **Fallback:** Groq / Llama 3.3 70B — triggered automatically on Gemini 429 rate-limit
- All LLM calls routed through `src/llm_client.py → llm_complete()`

---

## Insider Tracking (SEC Form 4)

| Mode | Speed | Source |
|---|---|---|
| **sec-api.io** (with `SEC_API_KEY`) | ~1s per ticker | Single API call |
| **EDGAR XML** (fallback) | 4–7s per ticker | Manual XML scraping |

**Catalyst Scanner insider column:** shows 🟢 3B (3 buys) / 🔴 2S (2 sells) / ★ cluster (3+ insiders) instead of a plain boolean.

**Reverse Lookup** (requires `SEC_API_KEY`): Short Squeeze page — shows who bought in 1/3/7 days. Cache 15 min.

---

## Short Squeeze Scanner

| Component | Weight | Adjustment |
|---|---|---|
| SI% of Float | 50% | — |
| Days to Cover | 20% | — |
| Est. Borrow Fee | 20% | None → -20pts · ≥20% → +15pts |
| Volume Ratio | 10% | — |

- **Borrow Fee:** estimated from SI% via Finviz
- **Sparkline:** 7-day price + volume chart
- **Critical Alert 🚨:** dist <5% AND all metrics Top 10%
- **Insider Overlay 🥇:** SEC Form 4 reverse lookup
- **AI Verdict:** on-demand Hebrew analysis (RTL rendered)
- **Sector mode:** iShares index + sector

---

## Project Structure

```
FinancialAgent/
├── dashboard.py
├── scheduler.py
├── install_service.bat
├── service_control.bat
├── _pages_modules/
│   ├── page_scan.py
│   ├── page_research.py
│   ├── page_watchlist.py        # 12 alert types, P&L dashboard, sector allocation
│   ├── page_market.py
│   ├── page_news_impact.py
│   ├── page_squeeze.py          # insider overlay 🥇
│   ├── page_catalyst.py         # catalyst scanner + news flag + insider + PDUFA + options
│   ├── page_options_flow.py     # options chain, PCR, unusual activity
│   ├── page_backtest.py
│   ├── page_history.py
│   └── page_scheduler.py
├── src/
│   ├── stock_scorer.py
│   ├── dcf_valuation.py
│   ├── squeeze_scanner.py
│   ├── borrow_fee.py
│   ├── supertrend.py            # Supertrend algorithm (Pine Script 1:1)
│   ├── catalyst_scanner.py      # explosion_score(), scan_catalysts(), PDUFA, unusual options
│   ├── price_alert_monitor.py   # price_target + volume_spike + supertrend (15m+daily) every 5 min
│   ├── watchlist_manager.py     # scan_watchlist() — all alert types including score delta
│   ├── options_flow.py          # get_options_summary(), scan_unusual_activity()
│   ├── news_fetcher.py          # detect_news_catalyst(), fetch_yfinance_news()
│   ├── news_catalyst_monitor.py # background thread — news-triggered alerts every 15 min
│   ├── momentum_scanner.py      # background thread — momentum alerts every 30 min
│   ├── telegram_news_digest.py
│   ├── insider_tracker.py
│   ├── sec_api_client.py
│   ├── market_feed.py
│   ├── database.py              # SQLite CRUD + auto migration
│   ├── llm_client.py            # Gemini → Groq fallback
│   ├── earnings_sentiment.py    # EPS surprise history (Tier 1) + LLM transcript (Tier 2)
│   └── pattern_detectors/
│       └── technical_indicators.py
├── tests/
│   ├── test_scorer.py           # 56 unit tests
│   └── test_insider_sec_api.py
├── logs/
└── data/
    └── financial_agent.db
```

---

## UI / UX

- **Tooltips:** hover over column headers and metric labels (RSI, MACD, MA Trend, SI%, DCF/MoS, WACC, DTC, Borrow Fee, Vol Ratio, VIX)
- **Catalyst Scanner:** scan only on button click — no auto-run on page load or filter change
- **AI Verdict (Short Squeeze):** Hebrew analysis displayed RTL

---

## Known Issues

- DCF returns None for loss-making companies (no positive FCF)
- DCF MoS extreme negative for high-growth stocks (e.g. TSLA) — correct by design
- Form 4 `pricePerShare` sometimes absent — shows "N/A"
- Price monitor / volume spike only run when Scheduler is active (or Windows Service)
- Borrow fee approximated from SI% — directionally correct, not exact
- Backtest requires at least one week of scan data in DB
- Google Trends occasional 429 errors
- Alpha Vantage free tier: 25 req/day
- sec-api.io trial: 100 credits per endpoint
- PDUFA scraper depends on BioPharma Catalyst HTML layout — falls back to empty list gracefully
- Unusual Options: yfinance options data absent for many small caps → 0 pts, no crash
- Score delta alerts require at least one prior scan result in DB per ticker

---

## Execution Engine (In Development)

Layers being built to bridge the gap from "interesting candidate" to "actionable trade":

| Layer | Module | Status |
|---|---|---|
| **1. Market Regime Throttle** | `src/market_regime.py` | 🔨 In progress |
| **2. Hard Veto Engine** | `src/execution_engine.py` | 🔨 In progress |
| **3. Two-Track Confluence** | `src/execution_engine.py` | 🔨 In progress |
| **4. Position Sizing** | `src/execution_engine.py` | 🔨 In progress |
| **5. Time-of-Day Flag** | `src/execution_engine.py` | Planned |
| **6. Sector Warn / Heatmap** | `src/execution_engine.py` | Planned |
| **7. Walk-Forward Backtest** | `_pages_modules/page_backtest.py` | Planned |
| **8. Paper Trading Audit Trail** | `src/paper_trading.py` | Planned |

### Regime Throttle
Three states — not a binary kill switch. Affects position size multiplier, signal frequency, and stop width:

| Regime | Trigger | Effect |
|---|---|---|
| **BULL** | SPY above 200d SMA + VIX < 20 | Full size, all signals active |
| **CAUTION** | SPY near 200d SMA or VIX 20–28 | 50% size, tighter stops |
| **BEAR** | SPY below 200d SMA + VIX > 28 | 30% size, exits only |

VIX thresholds are starting points — require walk-forward calibration per strategy.

### Two-Track Signal System

**Track A — High-Quality Confluence** (standard breakouts, momentum)
Weighted pillars (Technical 40 + Fundamental 30 + Catalyst 30), min total 60. Hard vetos apply.

**Track B — Special Situations** (squeeze, PDUFA, SEC 8-K surprise)
Dynamic catalyst weights: catalyst pillar increases to 50% when SI > 20% OR days-to-event < 5.
Max position size capped at 1.5% portfolio regardless of signal strength.

### Hard Vetos (both tracks)
- Avg daily dollar volume < $5M → reject
- R:R < 1.5:1 → reject
- Stock gapped down > 5% on earnings and signal is on the bounce → reject
- Regime = BEAR → exits only (no new longs)

### Enriched Alert Format
```
✅ AAPL — Track A Confluence | Regime: BULL
Technical ✅ 38/40 | Fundamental ✅ 26/30 | Catalyst ✅ Earnings in 4d
Entry: $182.50 | Stop: $178.20 (-2.4%) | Target: $194.00 (+6.3%)
Size: 27 shares ($4,928) — 1% portfolio risk | R:R: 2.7:1
⚠️ Time: first 30 min — wait for confirmation
```

---

## Disclaimer

This is NOT financial advice. For research and educational purposes only.

---

*Last updated: 2026-04-24*
