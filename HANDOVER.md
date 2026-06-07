# Financial Agent — HANDOVER
*עודכן: 2026-04-24*

---

## פרטי פרויקט
- **מיקום:** `C:/Projects/FinancialAgent`
- **Stack:** Python 3.14, Streamlit 1.52.2, yfinance, Gemini 2.5 Flash / Groq Llama 3.3 70B, SQLite, Finnhub, Alpha Vantage, SEC EDGAR, sec-api.io
- **הרצה:** `streamlit run dashboard.py` → http://localhost:8501
- **Tests:** `python -m pytest tests/test_scorer.py` · `python -m pytest tests/test_insider_sec_api.py`
- **Service:** `run_scheduler_watchdog.py` → מריץ את `scheduler.py` ברקע, מאתחל אוטומטית אחרי קריסה. רשום ב-Windows Task Scheduler כ-`FinancialAgentWatchdog` (At Startup, Run as Admin)

---

## מבנה הפרויקט

```
C:/Projects/FinancialAgent/
├── dashboard.py
├── scheduler.py
├── run_scheduler_watchdog.py   # *** חדש *** מאתחל את scheduler.py אחרי קריסה
├── _pages_modules/
│   ├── page_scan.py
│   ├── page_research.py
│   ├── page_watchlist.py        # 10 alert types, volume_spike + supertrend fields — auto-refresh 5 min
│   ├── page_market.py           # Futures bar + VIX level card — auto-refresh 10 min
│   ├── page_news_impact.py      # auto-refresh 20 min
│   ├── page_squeeze.py          # Insider Overlay 🥇 — auto-refresh 15 min
│   ├── page_catalyst.py         # Catalyst Scanner + news flag + insider + PDUFA + options
│   ├── page_backtest.py
│   ├── page_history.py
│   └── page_scheduler.py
├── src/
│   ├── stock_scorer.py
│   ├── dcf_valuation.py
│   ├── squeeze_scanner.py
│   ├── borrow_fee.py
│   ├── supertrend.py            # *** חדש *** Pine Script Supertrend → Python
│   ├── price_alert_monitor.py   # check_price_targets() + check_volume_spikes()
│   ├── telegram_news_digest.py
│   ├── insider_tracker.py
│   ├── sec_api_client.py
│   ├── market_feed.py
│   ├── database.py              # watchlist: volume_spike_x + supertrend_alert columns
│   ├── watchlist_manager.py     # scan_watchlist() + supertrend_flip alert
│   ├── catalyst_scanner.py      # scan_catalysts(), explosion_score(), PDUFA, unusual options
│   ├── news_fetcher.py          # detect_news_catalyst(), fetch_yfinance_news()
│   ├── news_impact_analyzer.py
│   ├── news_catalyst_monitor.py
│   ├── macro_signals.py
│   ├── telegram_notifier.py
│   ├── scan_worker.py
│   ├── ui_theme.py
│   ├── llm_client.py
│   └── index_loader.py
├── tests/
│   ├── test_scorer.py           # 56 unit tests
│   └── test_insider_sec_api.py
├── logs/
│   ├── scheduler.log            # כל פעילות ה-scheduler
│   └── watchdog.log             # crash + restart events מה-watchdog
└── data/
    └── financial_agent.db
```

---

## Background Scheduler (Watchdog + Task Scheduler)

```
run_scheduler_watchdog.py    # מאתחל את scheduler.py אוטומטית אחרי כל קריסה
logs/watchdog.log            # תיעוד כל restart + exit code
logs/scheduler.log           # פעילות ה-scheduler עצמו
```

**רישום ב-Task Scheduler (בוצע):**
```powershell
# Task: FinancialAgentWatchdog
# Trigger: At Startup
# Action: python run_scheduler_watchdog.py
# RunLevel: Highest (Admin)
# RestartCount: 3 / RestartInterval: 1 min (שכבת הגנה כפולה)
```

**ניהול:**
```powershell
Start-ScheduledTask  -TaskName "FinancialAgentWatchdog"   # הפעלה ידנית
Stop-ScheduledTask   -TaskName "FinancialAgentWatchdog"   # עצירה מלאה
# עצירה רכה (jobs בלבד): כפתור Disable בדף Scheduler ב-Streamlit
```

```
Watchdog (run_scheduler_watchdog.py)
└── scheduler.py
    ├── Market Digest    08:00 → Telegram
    ├── Portfolio News   08:15 → Telegram
    ├── Scan             08:30, 16:30 → Telegram
    ├── Watchlist        09:00 → Telegram  (כולל Supertrend flip check)
    ├── Portfolio        09:15 → Telegram
    └── Price Monitor    כל 5 דקות (thread) → price_target + volume_spike → Telegram
```

---

## מנוע ציונים (`src/stock_scorer.py`)

| רכיב | משקל | הערות |
|---|---|---|
| RSI | 15 | RSI>75 = 0 |
| MACD | 15 | |
| MA Trend | 20 | |
| Volume | 10 | |
| Momentum | 10 | |
| Short Interest | 10 | SI% of Float |
| Institutional | 5 | |
| Insider | 5 | SEC Form 4 |
| Fundamentals | 10 | P/E, Revenue Growth, Margin, D/E |
| DCF | 15 | Margin of Safety vs intrinsic value |
| Squeeze Bonus | +15 | SI≥20% + vol spike + price up |
| Google Trends | +5 | bonus |

---

## DCF Valuation

```
Intrinsic Value = Σ(FCF_t / (1+WACC)^t) + Terminal Value / (1+WACC)^n
Margin of Safety = (Intrinsic - Price) / Intrinsic * 100
```
- Growth: 3%-25% · WACC: 10%+D/E · Terminal: 2.5% · Horizon: 5y

---

## Insider Tracking

### `src/sec_api_client.py` — verified field names (live API)
```python
filing.issuer.tradingSymbol
filing.reportingOwner.name
filing.reportingOwner.relationship   # {isOfficer, officerTitle, isDirector, ...}
filing.nonDerivativeTable.transactions[].coding.code        # P/S/M/A
filing.nonDerivativeTable.transactions[].amounts.shares
filing.nonDerivativeTable.transactions[].amounts.pricePerShare
```

### מצבי עבודה
| מצב | תנאי | מהירות |
|---|---|---|
| sec-api.io | `SEC_API_KEY` ב-.env | ~1s |
| EDGAR XML | אין מפתח | 4-7s |

### Catalyst Scanner — insider column
`_get_insider_signal(ticker)` מחזיר dict: `{buys, sells, net, clustered, value}`.
מוצג כ-🟢 3B / 🔴 2S / ★ cluster (3+ insiders שונים).

---

## Catalyst Scanner (`src/catalyst_scanner.py`)

מוצא מניות small/mid-cap עם קטליזטור קרוב ופוטנציאל פיצוץ גבוה.

### Explosion Score (0–100)

| רכיב | מקס | לוגיקה |
|---|---|---|
| Urgency | 30 | Today=30 · 1d=27 · 3d=17 · 7d=8 |
| SI% Fuel | 25 | ≥20%=25 · ≥15%=18 · ≥10%=11 |
| Float Amplifier | 20 | ≤5M=20 · ≤15M=16 · ≤40M=11 · ≤100M=6 |
| Volume Building | 10 | ≥3x=10 · ≥2x=7 · ≥1.5x=4 |
| Insider Buying | 10 | net buying 90d (Form 4) |
| Momentum | 5 | 5-day price change |
| Unusual Options | +8 | unusual CALL vol/OI≥3x · +4 if PCR<0.7 |

Labels: ≥70=HIGH · 50-69=MEDIUM · 30-49=LOW · <30=WATCH

### Catalyst Types
- `earnings` — Nasdaq API
- `analyst` — Finnhub (requires FINNHUB_API_KEY)
- `sec_8k` — EDGAR parallel (8 workers)
- `pdufa` — BioPharma Catalyst HTML scrape, cache 6h → `data/pdufa_cache.json`

### PDUFA Feed (`fetch_pdufa_events`)
```python
fetch_pdufa_events(days_ahead=30, tickers_filter=None) -> List[Dict]
```
- מקור: `https://www.biopharmacatalyst.com/calendars/fda-calendar` (ציבורי, ללא API key)
- parse עם `beautifulsoup4` (html.parser, built-in)
- מחזיר אותו dict format כמו `fetch_earnings_events()`
- `tickers_filter=None` → כל האירועים · `set` → רק הטיקרים שנבחרו
- Cache TTL: 6 שעות · fallback `[]` על כל שגיאה

### Unusual Options (`_unusual_options_pts`)
```python
_unusual_options_pts(ticker) -> tuple[float, bool]  # (pts, has_unusual_calls)
```
- קורא ל-`get_options_summary(ticker, max_expirations=3)` מ-`src/options_flow.py`
- +8 pts: unusual CALL (vol/OI≥3x או vol≥5000)
- +4 pts: PCR_vol < 0.7 (bullish, ללא unusual)
- Returns `(0, False)` על כל שגיאה — many small caps לא נסחרים options

### שינויים עיקריים (2026-04-14)
- **FDA/PDUFA**: סוג קטליזטור 4 — BioPharma Catalyst calendar, checkbox בלוח
- **Unusual Options**: `_unusual_options_pts()` מחובר ל-`explosion_score()` כ-`unusual_options_pts=0`
- **Biotech hint**: הוספת `st.info()` ב-Index/Sector mode — "Russell 2000 → Health Care"
- **Badge 📊 Options**: badge על ticker cell בטבלה כשיש unusual calls
- **`explosion_score()`**: הוסף `unusual_options_pts: float = 0` — backward compatible

### שינויים עיקריים (2026-04-10)
- **SEC 8-K**: קריאות מקביליות עם `ThreadPoolExecutor(max_workers=8)` — מהיר פי ~8
- **`watchlist_mode`**: placeholder "Watchlist" מוצג רק במצב Watchlist+Portfolio, לא ב-Index/Sector
- **`phase_cb`**: progress bar מתעדכן בכל שלב (earnings → analyst → 8-K → per-ticker)
- **News Catalyst**: `detect_news_catalyst()` — regex על headlines, מחזיר list של keywords
- **Insider detail**: `_get_insider_signal()` מחזיר buys/sells/net/clustered — לא bool
- **Logging**: logger בכל נקודת כשלון (news fetch, insider, TA calc)

### Session State
| Key | תוכן |
|---|---|
| `catalyst_results` | רשימת תוצאות |
| `catalyst_cache_key` | מזהה cache לפי פרמטרים |
| `catalyst_ai_{ticker}` | AI verdict per ticker |

---

## Supertrend (`src/supertrend.py`) — ***חדש***

תרגום 1:1 של Pine Script v4 ל-Python/pandas.

```python
def supertrend(hist: pd.DataFrame, period=10, multiplier=3.0) -> dict:
    # Returns: {"direction": "Bullish"/"Bearish", "signal": "BUY"/"SELL"/None, "level": float}
```

**אלגוריתם:**
1. `hl2 = (High + Low) / 2`
2. `ATR = rolling mean of True Range (period bars)`
3. `upper = hl2 + mult × ATR`, `lower = hl2 − mult × ATR`
4. Carry-forward bands (max/min — same as `up1/dn1` in Pine Script)
5. Trend flips: close < lower → Bearish, close > upper → Bullish
6. Signal: flip on last two bars

**איפה נבדק:** `scan_watchlist()` (09:00 + 16:30) — לא real-time.
**Alert type:** `supertrend_flip` ב-`watchlist_alerts`.

---

## Volume Spike Alert — ***חדש***

`src/price_alert_monitor.check_volume_spikes()` — רץ כל 5 דקות (דרך `_price_monitor_thread`).

```python
ratio = info["volume"] / info["averageVolume"]
if ratio >= item["volume_spike_x"]:
    send_telegram_alert(...)
```

- **Cooldown:** 4 שעות (generic `_cooldown_ok_type(ticker, "volume_spike")`)
- **DB column:** `watchlist.volume_spike_x REAL DEFAULT 0` (0 = disabled)
- **UI:** Watchlist → Add/Edit → `📊 Volume Spike ×`

---

## Watchlist & Portfolio

### Alert types (10)
| Type | מתי | תדירות |
|---|---|---|
| `score_threshold` | score ≥ alert_score | On scan |
| `price_change` | מחיר זז ≥ alert_pct% | On scan |
| `price_target` | בתוך $0.05 מהיעד | כל 5 דקות |
| `price_above` | עלה מעל price_above | On scan |
| `price_below` | ירד מתחת ל-price_below | On scan |
| `stop_loss` | Portfolio: ≤ stop_loss | On scan |
| `target_hit` | Portfolio: ≥ target_price | On scan |
| `score_drop` | Portfolio: score < 35 | On scan |
| `volume_spike` | volume > X × avg | כל 5 דקות |
| `supertrend_flip` | Supertrend BUY/SELL crossover | On watchlist scan |

### DB Schema — watchlist
```sql
volume_spike_x   REAL DEFAULT 0     -- 0=disabled, >0 = X× threshold
supertrend_alert INTEGER DEFAULT 0  -- 0=disabled, 1=enabled
```
Migration אוטומטית דרך `_migrate()` ב-`database.py`.

### _render_alerts() צבעים
```python
"volume_spike":    "#0ea5e9"   # כחול
"supertrend_flip": "#8b5cf6"   # סגול
```

---

## News Catalyst Detection (`src/news_fetcher.py`)

```python
NEWS_CATALYST_KEYWORDS = [
    "fda", "approval", "approved", "clearance",
    "merger", "acquisition", "buyout", "takeover",
    "deal", "agreement", "contract",
    "partnership", "collaboration",
    "raises", "funding", "investment", "round",
    "settlement", "verdict",
]

def detect_news_catalyst(headlines: list) -> list:
    """מחזיר list של keywords שנמצאו (unique, ordered by first appearance)."""
```

שימוש ב-`catalyst_scanner.py`:
```python
_news_items   = fetch_yfinance_news(ticker, limit=8)
_headlines    = [n.get("headline", "") for n in _news_items]  # key: "headline" לא "title"!
news_catalyst = detect_news_catalyst(_headlines)              # list
```

⚠️ **Bug שתוקן:** המפתח הנכון ב-`fetch_yfinance_news()` הוא `"headline"`, לא `"title"`.

---

## Session State

| דף | Keys |
|---|---|
| Scan | `scan_results`, `active_job_id` |
| Research Deep Dive | `deep_dive_tickers`, `deep_dive_data_{t}`, `debate_{t}`, `ai_summary_{t}` |
| Research Compare | `compare_data`, `compare_tickers` |
| Watchlist | `wl_results`, `wl_news`, `wl_ts` |
| Portfolio | `pt_results`, `pt_ts` |
| Market | `market_data`, `market_refresh_ts`, `sector_heatmap`, `sector_refresh_ts` |
| News Impact | `news_analysis`, `ni_score_{ticker}`, `stock_news_{ticker}_{days}`, `upcoming_events_data` |
| Squeeze | `sq_results`, `sq_ts`, `sq_ai_{ticker}`, `sq_insider_buyers` |
| Catalyst | `catalyst_results`, `catalyst_cache_key`, `catalyst_ai_{ticker}` |

---

## .env Variables
```
GROQ_API_KEY=...
GEMINI_API_KEY=...
FINNHUB_API_KEY=...
ALPHA_VANTAGE_API_KEY=...
SEC_USER_AGENT_EMAIL=...
SEC_API_KEY=...
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

---

## Tooltips — מערכת הסברים

| דף | שיטה | מונחים |
|---|---|---|
| **Scan** — thead כהה | CSS `.tooltip-term` (hover) | Score, FC%, RSI, MACD, MA Trend, SI% Float, DCF / MoS |
| **Squeeze** — `_render_table()` | `title` attribute | SI% Float, DTC, Borrow Fee, Vol Ratio |
| **Squeeze** — `_render_card()` | `title` + dotted underline | SI% of Float, DTC, Borrow Fee, Vol Ratio |
| **Research** — DCF card | CSS `.tooltip-term` + `title` | MoS, Intrinsic Value, WACC |
| **Research** — Compare table | `title` + dotted underline | Score, Signal, RSI, MACD, MA Trend, SI%, DCF/MoS |
| **Market** — VIX card | `title` attribute | VIX levels |

כיצד נוסף tooltip: הוסף ל-`TOOLTIPS` ב-`src/ui_theme.py`, השתמש ב-`tooltip('Key')`.

---

## בעיות ידועות
- Price monitor + volume spike רצים רק דרך scheduler
- Borrow fee: Finviz approximation בלבד
- Backtest: צריך שבוע נתונים ב-DB
- Google Trends 429 מדי פעם
- Alpha Vantage: 25 req/day
- DCF: מניות ללא FCF חיובי → None
- DCF: high-growth stocks (TSLA) → MoS שלילי קיצוני — נכון מתמטית
- Form 4 `pricePerShare` לא תמיד קיים → "N/A"
- sec-api.io trial: 100 קרדיטים לכל endpoint
- Supertrend flip: נבדק ב-scan בלבד, לא real-time intraday
- SEC 8-K EDGAR: לא מחזיר item numbers → לא ניתן לסווג bullish/bearish ללא קריאה נוספת

---

## הצעדים הבאים (PENDING)

### הושלמו ✅
- [x] **Watchdog + Task Scheduler** — `run_scheduler_watchdog.py` מאתחל את scheduler.py אחרי קריסה; רשום ב-Task Scheduler לעלייה אוטומטית עם Windows (ללא NSSM)
- [x] **Auto-refresh ב-Streamlit** — `streamlit-autorefresh` הוסף ל-4 דפים: Market (10m) · Watchlist (5m) · News Impact (20m) · Squeeze (15m)
- [x] Short Squeeze Scanner
- [x] Borrow Fee scraper
- [x] Price Target Monitor
- [x] Telegram News Digest
- [x] News Impact: Upcoming Events tab
- [x] Session state persistence
- [x] Sector scan ב-Short Squeeze
- [x] DCF Valuation
- [x] SEC API — Insider Tracker + Reverse Lookup
- [x] Windows Service
- [x] Live price + P&L ב-Watchlist/Portfolio
- [x] Market Page — CBOE + Playwright VIX Futures + צבעי VIX הפוכים
- [x] Watchlist Grid Cards + % away + Price Target buffer
- [x] Tooltips — כל המונחים הפיננסיים
- [x] AI Verdict RTL — Short Squeeze
- [x] **Catalyst Scanner** — earnings catalyst + explosion score
- [x] **Catalyst Scanner** — SEC 8-K parallel (8 workers)
- [x] **Catalyst Scanner** — News Catalyst flag (regex, multi-keyword)
- [x] **Catalyst Scanner** — Insider buy/sell detail (counts + cluster)
- [x] **Catalyst Scanner** — watchlist_mode (no fake placeholders in Index/Sector)
- [x] **Catalyst Scanner** — phase_cb progress feedback
- [x] **Catalyst Scanner** — FDA/PDUFA calendar (BioPharma Catalyst, no key, 6h cache)
- [x] **Catalyst Scanner** — Unusual Options signal (reuses options_flow.py, +8/+4 pts)
- [x] **Catalyst Scanner** — Biotech hint (Russell 2000 → Health Care)
- [x] **Supertrend alert** — Pine Script 1:1 implementation, Watchlist per-ticker
- [x] **Volume Spike alert** — X× avg volume threshold, every 5 min

### ממתינים — Execution Engine (סדר בנייה מוסכם)

**Layer 1 — Market Regime Throttle** (`src/market_regime.py`) 🔨
- `get_regime()` → BULL / CAUTION / BEAR
- SPY vs SMA200 + VIX threshold (20 / 28 — נדרש walk-forward calibration)
- מחזיר `{"regime": str, "spy_vs_sma200": float, "vix": float, "multiplier": float}`
- **לא** binary kill switch — multiplier על position size + stop width

**Layer 2 — Hard Veto Engine** (`src/execution_engine.py`) 🔨
- `check_hard_vetos(ticker, price, score_data, regime)` → `{"pass": bool, "reason": str}`
- Vetoes: liquidity < $5M daily · R:R < 1.5:1 · gap-down > 5% on earnings bounce · BEAR regime
- R:R מחושב מ-ATR(14): stop = price − 2×ATR, target מ-DCF intrinsic או 52w high

**Layer 3 — Two-Track Confluence** (`src/execution_engine.py`) 🔨
- `evaluate_trade(ticker, score_data, regime)` → `{"track": "A"|"B"|None, "confluence": dict, "signal": dict}`
- **Track A**: pillars Technical(40) + Fundamental(30) + Catalyst(30), total ≥ 60, min 10 each
- **Track B**: Special situations — catalyst weight dynamic: 50% if SI>20% OR days_to_event<5
- Track B: max position cap 1.5% portfolio hard

**Layer 4 — Position Sizing** (`src/execution_engine.py`) 🔨
- `calc_position_size(price, atr, portfolio_value, regime_multiplier, track)` → `{"shares": int, "dollar_risk": float, "stop": float, "target": float}`
- Risk 1% portfolio · stop = 2×ATR · regime_multiplier from Layer 1
- Hard caps: Track A max 5% portfolio · Track B max 1.5% · sector max 30%

**Layer 5 — Time-of-Day Flag** (`src/execution_engine.py`)
- `is_noise_window()` → bool — 09:30–10:00 ET or 15:45–16:00 ET
- אם True: מוסיף `⚠️ Time: wait for confirmation` לאזהרה — לא חוסם

**Layer 6 — Sector Exposure Guard** (`src/execution_engine.py`)
- `check_sector_exposure(ticker, portfolio)` → `{"concentration_pct": float, "warn": bool, "size_adj": float}`
- > 25% בסקטור: warn + מכפיל 0.5× נוסף על גודל פוזיציה

**Layer 7 — Walk-Forward Backtest** (`_pages_modules/page_backtest.py`)
- Train 6m / Test 3m / Roll quarterly
- Split by regime × track
- Metrics: max drawdown, profit factor, Sharpe, avg hold days, win rate by regime

**Layer 8 — Paper Trading Audit Trail** (`src/paper_trading.py`)
- DB table: `paper_trades` — ticker, signal_date, entry, stop, target, exit, exit_reason, regime_at_entry, track, realized_rr
- 30 ימי paper לפני live capital
- Statistics: actual win rate vs backtest, actual R:R, actual drawdown

### ממתינים — שאר הפיצ'רים
- [ ] SEC 8-K item classification (1.01 bullish / 1.03 bearish) — צריך EDGAR submissions API
- [ ] Fear & Greed Index
- [ ] Dollar Index (DXY) במדדים
- [ ] כוונון משקלים אחרי backtest data
- [ ] Russell 2000 תמיכה ב-Scan הראשי (IWM — כבר עובד ב-Catalyst Scanner)

### הודעת Telegram — פורמט מועשר (אחרי Execution Engine)
```
✅ AAPL — Track A | Regime: BULL
Technical ✅ 38/40 | Fundamental ✅ 26/30 | Catalyst ✅ Earnings in 4d
Entry: $182.50 | Stop: $178.20 (-2.4%) | Target: $194.00 (+6.3%)
Size: 27 shares ($4,928) — 1% risk | R:R: 2.7:1
⚠️ Sector Tech: 28% portfolio — size reduced
```
