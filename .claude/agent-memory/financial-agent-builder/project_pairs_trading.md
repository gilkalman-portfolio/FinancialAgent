---
name: DSSPT Pairs Trading Module
description: Architecture and key design decisions for the DSSPT pairs trading feature added 2026-04-17
type: project
---

DSSPT (Dynamic State-Space Pairs Trading) was added to FinancialAgent on 2026-04-17.

**Files created/modified:**
- `src/pairs_scanner.py` — core engine (new)
- `src/database.py` — two new tables + four CRUD functions (modified)
- `_pages_modules/page_pairs.py` — 3-tab Streamlit page (new)
- `dashboard.py` — added "Pairs Trading" to sidebar nav (modified)
- `scheduler.py` — weekly clustering + per-cycle spread monitor (modified)

**Key design decisions:**
- Kalman Filter implemented manually with numpy (no pykalman). State = [beta_t], random-walk transition F=1, H=X_t, Q=1e-4, R=var(spread).
- OU process fit via OLS on dS_t = a + b*S_{t-1}: theta = -b/dt, mu = a/(theta*dt). Returns None if b >= 0 (non-mean-reverting).
- DBSCAN eps=0.5, min_samples=2 on PCA-scaled (StandardScaler) scores.
- Pairs filter: half_life < 30 days from static OLS. Live spreads use Kalman beta.
- Regime check: VIX < 25 → "mean_reverting", else "trending". Alerts suppressed in trending regime.
- Signal thresholds: |z| > 2.0 triggers alert, max_half_life=20d for monitor.
- Weekly clustering every Monday 06:00 via `schedule.every().monday`. Time configurable via `pairs_clustering_time` key in `scheduler_config.json`.
- Spread monitor runs every 5 min inside existing `_price_monitor_thread()` — no new thread needed.
- Universe: SP100_SUBSET (top-30 large caps hardcoded in pairs_scanner.py).
- DB tables: `pairs_watchlist` (UNIQUE on ticker_y, ticker_x with upsert), `pairs_signals`.

**Why:** User requested a statistical arbitrage module as a standalone feature with scheduler integration and Telegram alerts.

**How to apply:** When extending pairs trading logic, refer to `_kalman_filter_beta()` for Kalman implementation pattern and `_fit_ou()` for OU estimation. The `monitor_pairs_spreads()` function is the scheduler entry point.
