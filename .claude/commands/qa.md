# QA Checklist — FinancialAgent

Perform a manual QA review of recent changes. Work through each section systematically. Report findings in a structured summary at the end.

---

## 1. Scoring Engine (`src/stock_scorer.py`)
- [ ] All 10 components return values in their expected range
- [ ] DCF score returns 0 when `dcf_valuation.py` returns None (loss-making companies)
- [ ] Squeeze bonus only applies when SI≥20% + volume spike + price rising
- [ ] Total score never exceeds 100 (core 90 + bonuses capped)
- [ ] Signals map correctly: 75+=STRONG BUY, 60-74=BUY, 45-59=WATCH, 35-44=NEUTRAL, <35=SKIP

## 2. DCF Engine (`src/dcf_valuation.py`)
- [ ] Returns None for companies with no positive FCF — does not crash
- [ ] Growth rate clamped between 3%–25%
- [ ] WACC between 10%–13% (base + D/E adjustment)
- [ ] Margin of Safety calculation is correct: `(Intrinsic - Price) / Intrinsic * 100`
- [ ] DCF score maps correctly to MoS thresholds (≥40%→15, 20-40%→11, 5-20%→7, 0-5%→3, <0%→0)

## 3. Database (`src/database.py`)
- [ ] `_migrate()` runs without errors on existing DB
- [ ] No data loss when adding new columns
- [ ] All 4 tables present: `watchlist`, `portfolio`, `watchlist_alerts`, `scan_results`
- [ ] `price_target` column exists in `watchlist`

## 4. Scheduler (`scheduler.py`)
- [ ] All 6 jobs defined and scheduled at correct default times
- [ ] Price monitor daemon thread starts correctly
- [ ] `scheduler_config.json` loads without errors; missing keys fall back to defaults
- [ ] No thread leaks — price monitor is daemon=True

## 5. Telegram
- [ ] `send_market_digest()` sends without error when TELEGRAM_ENABLED=true
- [ ] `send_portfolio_news()` handles empty portfolio gracefully
- [ ] Price target alert respects 4h cooldown
- [ ] Scan alerts fire only when score ≥ min_score in config

## 6. Pages
- [ ] All 9 pages import without errors
- [ ] Session state keys don't collide across pages (check HANDOVER for full list)
- [ ] All multiline HTML goes through `_html()` before `st.markdown`
- [ ] No hardcoded API keys in any page file

## 7. Alert Logic (`src/watchlist_manager.py`)
- [ ] All 8 alert types trigger under correct conditions
- [ ] 24h cooldown enforced per ticker+type (except price_target: 4h)
- [ ] `score_drop` fires only for portfolio holdings, not watchlist

## 8. Short Squeeze Scanner (`src/squeeze_scanner.py`)
- [ ] Squeeze score between 0–100
- [ ] Borrow fee penalty applied correctly: None→-20pts, ≥20%→+15pts
- [ ] Critical alert 🚨 fires only when dist<5% AND SI/DTC/Fee all in Top 10%
- [ ] Sector mode loads iShares data via `index_loader.py` without crash

## 9. Catalyst Scanner (`src/catalyst_scanner.py`)
- [ ] `explosion_score()` backward-compatible — calling without `unusual_options_pts` returns same result as before
- [ ] `fetch_pdufa_events()` returns `[]` gracefully when BioPharma Catalyst is unreachable (no exception raised)
- [ ] PDUFA cache written to `data/pdufa_cache.json`; second call within 6h uses cache (no HTTP request)
- [ ] `_unusual_options_pts()` returns `(0, False)` for tickers with no options data — no crash
- [ ] FDA/PDUFA checkbox visible in Catalyst Scanner UI (4th column next to SEC 8-K)
- [ ] 📊 Options badge appears on ticker cell when unusual calls detected
- [ ] Biotech hint visible in Index / Sector source mode
- [ ] Summary bar shows "💊 FDA/PDUFA: N" only when PDUFA events are present in results
- [ ] `scan_catalysts()` handles `catalyst_types=["pdufa"]` without earnings/analyst/8-K (no crash)

## 10. LLM Client (`src/llm_client.py`)
- [ ] Gemini called first; falls back to Groq on failure
- [ ] Fallback doesn't crash if both fail — returns graceful error string
- [ ] No API keys hardcoded; loaded from .env

## 11. General Code Quality
- [ ] No unused imports in recently modified files
- [ ] No `print()` statements left in production code (should use logging)
- [ ] No TODO/FIXME comments left unaddressed in modified files
- [ ] All new functions have docstrings or inline comments

---

## Output Format

Provide a summary in this format:

```
QA REPORT — [date]
==================
✅ PASSED: [list sections that passed]
⚠️  WARNINGS: [issues that work but need attention]
❌ FAILED: [broken or incorrect behavior]

Priority fixes:
1. ...
2. ...
```
