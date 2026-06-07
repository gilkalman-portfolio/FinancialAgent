# Financial Agent — Features
*Last updated: 2026-04-24*

Complete reference for all capabilities in the Streamlit dashboard.

---

## 10 Pages

| Page | Key Capabilities |
|---|---|
| **Scan** | Multi-factor composite scoring, DCF column, background scan worker, sector/index mode, watchlist-only mode |
| **Research** | Deep Dive (DCF card, Bull/Bear AI debate, news, analyst targets) + Side-by-Side Compare (up to 4 tickers) |
| **Watchlist & Portfolio** | Grid card UI, 10 alert types, real-time P&L, price targets, stop-loss, live price refresh |
| **Market** | Live indices (CBOE), VIX level card, Futures bar, sector heatmap, market mood, earnings calendar, macro events |
| **News Impact** | 3-layer LLM article analysis + Stock News tab + Upcoming Events tab |
| **Short Squeeze** | Squeeze Score 0–100, ranked table + cards, sparklines, AI Verdict (Hebrew/RTL), sector scan, insider overlay |
| **Catalyst Scanner** | Finds small/mid-cap stocks with upcoming catalyst + explosion score + PDUFA calendar + unusual options signal + news catalyst flag + insider buy/sell detail |
| **Backtest** | Signal accuracy validation against historical scan data |
| **History** | Score trend over time per ticker |
| **Scheduler** | Job config, manual send buttons, price monitor + volume spike monitor status |

---

## Scoring Engine (0–100)

Composite score built from 9 weighted components + bonuses.

| Component | Weight | Notes |
|---|---|---|
| RSI | 15 | RSI >75 = 0 pts |
| MACD | 15 | Bullish crossover |
| MA Trend | 20 | Price vs SMA20/50/200 |
| Volume | 10 | Volume spike detection |
| Momentum | 10 | 5-day price momentum |
| Short Interest | 10 | SI% of float |
| Institutional | 5 | Inst. holdings % |
| Insider | 5 | SEC Form 4 net buying |
| Fundamentals | 10 | P/E, Revenue Growth, Margin, D/E |
| DCF | 15 | Margin of Safety vs intrinsic value |
| Squeeze Bonus | +15 | SI≥20% + vol spike + price rising |
| Google Trends | +5 | Trending interest bonus |

**Signals:** 75+ = STRONG BUY · 60–74 = BUY · 45–59 = WATCH · 35–44 = NEUTRAL · <35 = SKIP

---

## DCF Valuation (`src/dcf_valuation.py`)

5-year discounted free cash flow model, built into the score.

```
Intrinsic Value = Σ(FCF_t / (1+WACC)^t) + Terminal Value / (1+WACC)^n
Margin of Safety = (Intrinsic - Price) / Intrinsic * 100
```

| Parameter | Value |
|---|---|
| Horizon | 5 years |
| Growth rate | avg(revenue + earnings growth), clamped 3%–25% |
| WACC | 10% base + up to 3% for high D/E ratio |
| Terminal growth | 2.5% |
| Data source | `freeCashflow` from yfinance (fallback: operatingCF − capex) |

**MoS → Score mapping:** ≥40% → 15pts · 20–40% → 11pts · 5–20% → 7pts · 0–5% → 3pts · <0% → 0pts

Returns `None` for loss-making companies (no positive FCF) — does not penalise score.

---

## Short Squeeze Scanner (`src/squeeze_scanner.py`)

| Component | Weight | Penalty / Bonus |
|---|---|---|
| SI% of Float | 50% | — |
| Days to Cover | 20% | — |
| Est. Borrow Fee | 20% | N/A → −20pts · ≥20% → +15pts |
| Volume Ratio | 10% | — |

- **Borrow Fee:** estimated from SI% via Finviz (industry-calibrated scale), cached 2h
- **Breakout price:** 52-week high
- **Critical Alert 🚨:** dist to breakout <5% AND SI/DTC/Fee all in Top 10% of scan
- **Sparkline:** 7-day price + volume mini-chart
- **AI Verdict:** on-demand Gemini/Groq analysis, displayed RTL for Hebrew text
- **Insider Overlay 🥇:** SEC Form 4 reverse lookup, overlap banner + table marker
- **Sector mode:** scan by iShares index + sector

---

## Insider Tracking

| Mode | Condition | Speed |
|---|---|---|
| sec-api.io | `SEC_API_KEY` in `.env` | ~1s per ticker |
| EDGAR XML | no key (fallback) | 4–7s per ticker |

- Deep Dive page: per-ticker Form 4 history table
- Short Squeeze page: reverse lookup (who bought in last 1/3/7 days), min value filter, 15-min cache
- Catalyst Scanner: insider column shows 🟢 NB (N buys) / 🔴 NS (N sells) / ★ cluster (3+ insiders)

---

## Market Page

| Widget | Source |
|---|---|
| VIX Card | CBOE real-time API — level: Calm / Normal / Caution / Fear / Panic |
| Indices | CBOE (^GSPC, ^NDX, ^DJI, ^RUT) + yfinance (Oil, Gold, Bonds, FX, BTC) |
| Futures Bar | yfinance (ES=F, NQ=F, YM=F, GC=F, CL=F) |
| VIX Futures | investing.com via Playwright subprocess |
| Sector Heatmap | yfinance XL* ETFs — 1D / 5D |
| Market Mood | Alpha Vantage + Finnhub — sentiment from ~40 articles |
| Earnings | Nasdaq API — this week by date |
| Macro Events | Hardcoded schedule (CPI, Fed, NFP, etc.) |

VIX colors are **inverted**: rising VIX = red (fear), falling = green (calm).

---

## News & Analysis

### News Impact (`page_news_impact.py`)
- **Article Analysis:** paste any URL → 3-layer LLM analysis (summary, affected tickers, sentiment, trading implication)
- **Stock News:** yfinance + Alpha Vantage headlines per ticker, sentiment badges
- **Upcoming Events:** earnings + macro events calendar

### Research Deep Dive (`page_research.py`)
- Full DCF card (intrinsic value, MoS%, WACC, growth rate, valuation label)
- Bull vs Bear AI debate
- AI summary with entry/exit strategy
- Analyst price targets + recommendation breakdown
- Recent news headlines

---

## Alerts & Telegram

### 10 Alert Types

| Type | Trigger | Frequency |
|---|---|---|
| `score_threshold` | score ≥ configured threshold | On scan |
| `price_change` | price moved ≥ configured % | On scan |
| `price_target` | price within $0.05 of target | Every 5 min |
| `price_above` | price crossed above level | On scan |
| `price_below` | price dropped below level | On scan |
| `stop_loss` | portfolio: price ≤ stop loss | On scan |
| `target_hit` | portfolio: price ≥ target | On scan |
| `score_drop` | portfolio: score < 35 | On scan |
| `volume_spike` | volume > X × 10d avg | Every 5 min |
| `supertrend_flip` | Supertrend BUY/SELL crossover | On watchlist scan |

Cooldown: 24h per ticker+type. Exceptions: `price_target` + `volume_spike` → 4h.

### Scheduled Telegram Digests

| Time | Message |
|---|---|
| 08:00 | Market mood + indices + top headlines |
| 08:15 | Recent news for all portfolio holdings |
| 08:30, 16:30 | Post-scan alerts |
| Every 5 min | Price target monitor + volume spike monitor (daemon thread) |

---

## Catalyst Scanner (`src/catalyst_scanner.py`)

Finds small/mid-cap stocks with an upcoming catalyst that have high explosion potential — especially when combined with high short interest, low float, volume building, insider buying, or unusual call activity.

### Explosion Score (0–100)

| Component | Max pts | Logic |
|---|---|---|
| Urgency | 30 | Today=30 · 1d=27 · 3d=17 · 7d=8 · 14d+=4 |
| SI% Fuel | 25 | ≥20%=25 · ≥15%=18 · ≥10%=11 · ≥5%=5 |
| Float Amplifier | 20 | ≤5M=20 · ≤15M=16 · ≤40M=11 · ≤100M=6 · >100M=2 |
| Volume Building | 10 | ≥3x=10 · ≥2x=7 · ≥1.5x=4 · ≥1.2x=2 |
| Insider Buying | 10 | +10 if net buying in 90d (SEC Form 4) |
| Momentum | 5 | ≥5%=5 · ≥2%=3 · ≥0%=1 · <0%=0 |
| Unusual Options | +8 | +8 unusual CALL contracts (vol/OI≥3x or vol≥5000) · +4 if PCR<0.7 |

**Labels:** ≥70 = HIGH · 50–69 = MEDIUM · 30–49 = LOW · <30 = WATCH

### Catalyst Types
- **📅 Earnings** — Nasdaq earnings calendar
- **📈 Analyst Upgrade** — Finnhub upgrade/downgrade events (requires FINNHUB_API_KEY)
- **📋 SEC 8-K** — EDGAR material filings, fetched in parallel (8 workers, ~8× faster)
- **💊 FDA/PDUFA** — BioPharma Catalyst public calendar; no API key required; 6h cache at `data/pdufa_cache.json`

### Extra Columns in Results Table

| Column | What it shows |
|---|---|
| **News** 🔵 | Regex-detected catalyst keyword in recent headlines (FDA / merger / deal / approval / partnership / funding / …). Multiple badges if multiple matches. |
| **Insider** 🟢🔴 | Buy/Sell transaction count from Form 4. 🟢 3B = 3 purchases. 🔴 2S = 2 sales. ★ = cluster (3+ different insiders). |
| **📊 Options** | Badge on ticker cell. Blue = unusual CALL activity detected (vol/OI≥3x). Light blue = bullish PCR (<0.7) without unusual contracts. Powered by `src/options_flow.py`. |

### Filters
- `days_ahead` — catalyst window (3–30 days)
- `max_market_cap_b` — exclude large caps (default $5B)
- `min_si_pct` — minimum short interest %
- `min_explosion_score` — drop below threshold
- `check_insider` — optional, slow without SEC_API_KEY

### Source Modes
- **Nasdaq Calendar** — all upcoming earnings
- **Watchlist + Portfolio** — your holdings only (shows all tickers, even without a catalyst)
- **Manual Tickers** — user-supplied list (only shows tickers with a found catalyst)
- **Index / Sector** — iShares index constituents. Biotech: Russell 2000 → Health Care (~150 tickers)

### Behaviour
- Scan only runs on button click — no auto-run on page load or filter change
- Results cached in session state by parameter hash
- `phase_cb` updates progress bar per phase (earnings → analyst → 8-K → PDUFA → per-ticker)

### PDUFA / FDA Calendar (`fetch_pdufa_events`)
- Source: `https://www.biopharmacatalyst.com/calendars/fda-calendar` (public, no key)
- Parses HTML table with `requests` + `beautifulsoup4`
- Returns events in same dict format as `fetch_earnings_events()`
- Supports all source modes: calendar (all events), manual/watchlist/index (filtered by ticker list)
- Falls back to `[]` gracefully if the page is unreachable or HTML structure changes

### Unusual Options Signal (`_unusual_options_pts`)
- Reuses `src/options_flow.py → get_options_summary()` (yfinance)
- +8 pts if any CALL contract has vol/OI ≥ 3x or volume ≥ 5000
- +4 pts if PCR (by volume) < 0.7 and no unusual calls
- Returns 0 silently on any failure — options data absent for many small caps

---

## Supertrend Indicator (`src/supertrend.py`)

Pine Script v4 algorithm implemented in Python (1:1 mathematical translation).

```
hl2   = (High + Low) / 2
ATR   = rolling mean of True Range (period bars)
upper = hl2 + multiplier × ATR   (resistance)
lower = hl2 − multiplier × ATR   (support)
Bands adjusted with carry-forward logic (max/min, same as up1/dn1 in Pine Script)
Trend flips to Bearish when close < lower
Trend flips to Bullish when close > upper
```

- **Default params:** period=10, multiplier=3.0 (same as Pine Script `defval`)
- **BUY signal:** trend[-2]==-1 and trend[-1]==1
- **SELL signal:** trend[-2]==1 and trend[-1]==-1
- Enable per ticker: Watchlist → `📈 Supertrend Alert` checkbox
- Checked during `scan_watchlist()` (runs at 09:00 + 16:30 via scheduler)
- Telegram alert format: `🟢 Supertrend BUY: AAPL\nTrend → Bullish\nרמה: $180.50`

---

## Volume Spike Alert

- Configure per ticker: Watchlist → `📊 Volume Spike ×` (e.g. 2.0 = 2× average)
- Checked every 5 minutes in `price_alert_monitor.check_volume_spikes()`
- Data: `info["volume"]` vs `info["averageVolume"]` from yfinance
- Cooldown: 4 hours per ticker
- Telegram alert: `📊 Volume Spike: AAPL\n2.4× avg volume\n(18,400,000 shares)\nמחיר: $185.20`

---

## DB Schema (`data/financial_agent.db`)

### `watchlist` table

```sql
id              INTEGER PK
ticker          TEXT UNIQUE
added_at        TEXT
notes           TEXT
alert_score     INTEGER DEFAULT 60
alert_pct       REAL DEFAULT 5.0
price_above     REAL
price_below     REAL
price_target    REAL
volume_spike_x  REAL DEFAULT 0     -- 0=off, >0 = X× avg vol threshold
supertrend_alert INTEGER DEFAULT 0  -- 0=off, 1=on
list_type       TEXT DEFAULT 'watch'
```

### `watchlist_alerts` table

```sql
id          INTEGER PK
ticker      TEXT
alert_type  TEXT   -- score_threshold | price_change | price_target | price_above |
                   -- price_below | stop_loss | target_hit | score_drop |
                   -- volume_spike | supertrend_flip
message     TEXT
sent_at     TEXT
score       REAL
price       REAL
```

### `portfolio` table

```sql
ticker, added_at, entry_price, shares, notes, stop_loss, target_price
```

Migration: `_migrate()` in `database.py` — adds columns without breaking existing data. Runs automatically on startup.

---

## UI Features

- **Tooltips** on all major financial terms across every page (Score, RSI, MACD, MA Trend, SI%, DCF, MoS, WACC, Days to Cover, Borrow Fee, Vol Ratio, VIX). Hover any underlined term or column header.
- **Background scan worker** — scan runs as a subprocess, navigate freely while it runs
- **Watchlist grid cards** — 3-column layout, each card shows score, signal badge, price, RSI, MACD, SI%, target % away, news headline, edit/remove buttons
- **Real-time P&L** — portfolio shows live price, unrealised gain/loss per position
- **Watchdog + Task Scheduler** — `run_scheduler_watchdog.py` auto-restarts the scheduler on crash; registered in Windows Task Scheduler (`FinancialAgentWatchdog`) to start at boot — no NSSM required
- **Auto-refresh** — `streamlit-autorefresh` added to Market (10 min), Watchlist (5 min), News Impact (20 min), Squeeze (15 min); Scan/Research/Backtest remain manual to avoid API abuse

---

## LLM Stack (`src/llm_client.py`)

| Model | Role |
|---|---|
| Gemini 2.5 Flash | Primary — used for all AI tasks |
| Groq Llama 3.3 70B | Fallback — automatic on Gemini failure |

---

## Data Sources

| Source | Used for | Limit |
|---|---|---|
| yfinance | Prices, fundamentals, news, short data, volume | None (unofficial) |
| Finnhub | News sentiment, analyst upgrades | Free tier |
| Alpha Vantage | News sentiment | 25 req/day (free) |
| CBOE real-time API | Live indices + VIX | Free, no key |
| SEC EDGAR | Insider Form 4 filings, 8-K filings | Free |
| sec-api.io | Fast insider lookup | 100 credits/endpoint (trial) |
| Finviz | Borrow fee approximation | Scraped |
| Nasdaq API | Earnings calendar | Free |
| Google Trends | Trending interest bonus | Occasional 429 |
| investing.com | VIX Futures | Playwright scraper |
| iShares | Index/sector constituents | Free JSON |
| BioPharma Catalyst | FDA/PDUFA/AdCom calendar | Free HTML scrape, 6h cache |

---

## Execution Engine (`src/market_regime.py` + `src/execution_engine.py`)

Bridges gap from screening signal → actionable trade. Built in 8 layers.

### Layer 1 — Market Regime Throttle

Not a binary kill switch — a multiplier on all downstream decisions.

| Regime | Condition | Position Multiplier | Signals |
|---|---|---|---|
| **BULL** | SPY > SMA200 + VIX < 20 | 1.0× (full) | All |
| **CAUTION** | SPY near SMA200 OR VIX 20–28 | 0.5× | All, tighter stops |
| **BEAR** | SPY < SMA200 + VIX > 28 | 0.3× | Exits only, no new longs |

Thresholds calibrated via walk-forward — not hardcoded permanently.

### Layer 2 — Hard Vetos (both tracks, no exceptions)

| Veto | Threshold |
|---|---|
| Liquidity | Avg daily dollar volume < $5M → reject |
| R:R | Reward-to-risk < 1.5:1 → reject |
| Gap-down | Stock gapped down > 5% on earnings, signal on bounce → reject |
| Regime BEAR | No new long entries |

### Layer 3 — Two-Track Confluence System

**Track A — High-Quality Confluence** (breakouts, momentum, fundamental setups)

Weighted pillars — not an AND gate. Soft vetos allow compensation across pillars.

| Pillar | Max | Minimum to avoid veto |
|---|---|---|
| Technical | 40 | 10 |
| Fundamental | 30 | 10 |
| Catalyst | 30 | 10 |
| **Total required** | — | **≥ 60** |

**Track B — Special Situations** (squeeze, PDUFA, SEC 8-K)

Dynamic catalyst weights — catalyst pillar weight increases when edge is sharpest:
- SI > 20% OR days-to-event < 5 → catalyst weight 50%, technical 30%, fundamental 20%
- Otherwise → catalyst 40%, technical 35%, fundamental 25%

Max position size hard-capped at 1.5% portfolio (binary event risk).

### Layer 4 — Position Sizing

Kelly-inspired, volatility-adjusted, regime-multiplied.

```
atr_pct       = ATR(14) / price
risk_per_trade = portfolio_value × 0.01   (1% risk)
stop_distance  = atr_pct × 2
position_size  = (risk_per_trade / stop_distance) × regime_multiplier
```

Hard caps: max 5% portfolio (Track A), max 1.5% portfolio (Track B), max 30% per sector.

### Layer 5 — Time-of-Day Flag

Alerts fired in first 30 min (09:30–10:00 ET) or last 15 min (15:45–16:00 ET) are flagged
`⚠️ Time: wait for confirmation` — not blocked, but marked.

### Layer 6 — Sector Exposure Guard

Warn + size down, never block. If sector already > 25% of portfolio:
- Add `⚠️ Sector concentration: X%` to alert
- Apply additional 0.5× size multiplier
- Portfolio heatmap shown in alert

### Enriched Alert Format (Track A example)

```
✅ AAPL — Track A Confluence | Regime: BULL
Technical ✅ 38/40 | Fundamental ✅ 26/30 | Catalyst ✅ Earnings in 4d
Entry: $182.50 | Stop: $178.20 (-2.4%) | Target: $194.00 (+6.3%)
Size: 27 shares ($4,928) — 1% portfolio risk | R:R: 2.7:1
```

---

## Known Limitations

- DCF returns `None` for loss-making companies (no positive FCF)
- DCF can show extreme negative MoS for high-growth stocks (e.g. TSLA) — correct by design
- Borrow fee is approximated from SI%, not a live quote
- Price monitor + volume spike only run when Scheduler is active (or as Windows Service)
- Backtest requires at least one week of scan history in DB
- Alpha Vantage: 25 req/day on free tier
- Google Trends: occasional 429 rate limit errors
- sec-api.io trial: 100 credits per endpoint
- Form 4 `pricePerShare` sometimes absent — shows N/A in insider table
- Supertrend flip detected at scan time only — not intraday real-time
- SEC 8-K EDGAR search does not return item numbers (1.01/2.01/etc.) — cannot auto-classify bullish vs bearish without additional API call
- PDUFA scraper depends on BioPharma Catalyst HTML structure — falls back to `[]` gracefully on failure
- Unusual Options: yfinance options data absent for many small caps → 0 pts, no crash
