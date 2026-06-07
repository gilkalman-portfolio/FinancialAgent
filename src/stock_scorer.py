"""
Stock Scorer - Unified scoring engine
Combines: Technical, Short interest, Institutional, Insider, Reddit, Trends, Forecast, DCF
Each result includes '_timings' dict showing seconds per component.
"""

import os
import time as _time
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from loguru import logger

from src.pattern_detectors.technical_indicators import TechnicalIndicators
from src.stock_forecaster import StockForecaster
from src.insider_tracker import InsiderTracker
from src.google_trends import trends_score as google_trends_score
from src import score_cache
from src.alpha_vantage_client import get_price_fallback
from src.dcf_valuation import calculate_dcf, dcf_score_only, calculate_ps_valuation

WEIGHTS = {
    'rsi':            15,
    'macd':           15,
    'ma':             20,
    'volume':         10,
    'momentum':       10,
    'forecast':       15,
    'short_interest': 10,
    'institutional':   5,
    'insider':         5,
    'fundamentals':   10,   # P/E, growth, margins, debt
    'dcf':            15,   # DCF intrinsic value vs price (new)
    'news_sentiment':  5,
    'trends':          5,
}

_ti      = TechnicalIndicators()
_insider = InsiderTracker()


def score_stock(ticker: str, forecast_days: int = 30) -> Optional[Dict[str, Any]]:
    """Score a single stock 0-100. Returns None if data insufficient."""

    cached = score_cache.get(ticker, forecast_days)
    if cached is not None:
        return cached

    t_total = _time.time()
    timings = {}

    try:
        # ── Data fetch ───────────────────────────────────────────────────────
        t = _time.time()
        stock = yf.Ticker(ticker)
        end, start = datetime.now(), datetime.now() - timedelta(days=180)
        df   = stock.history(start=start, end=end)
        info = stock.info
        try:
            cashflow_df = stock.cashflow
        except Exception:
            cashflow_df = None
        timings['data_fetch'] = round(_time.time() - t, 2)

        if df.empty or len(df) < 50:
            logger.debug(f"{ticker}: insufficient data, trying Alpha Vantage fallback")
            av_data = get_price_fallback(ticker)
            if av_data is None:
                return None
            return {
                'ticker':         ticker,
                'score':          0,
                'price':          av_data['price'],
                'forecast_price': None,
                'forecast_change': None,
                'rsi':            None,
                'macd':           'unknown',
                'ma_trend':       'unknown',
                'golden_cross':   False,
                'volume_ratio':   1.0,
                'momentum':       av_data.get('change_pct', 0),
                'short_pct':      0,
                'days_to_cover':  0,
                'inst_pct':       0,
                'inst_change':    None,
                'insider_conv':   0,
                'news_sentiment': 'neutral',
                'news_score':     0,
                'trends_interest': 0,
                'trends_spike':   False,
                'dcf':            None,
                '_source':        'alpha_vantage_fallback',
                '_scores':        {},
                '_timings':       timings,
            }

        price = df['Close'].iloc[-1]

        # ── Price sanity check ───────────────────────────────────────────────
        # yfinance occasionally returns a corrupted last-row price (e.g. POWL
        # returned $560 when the real price was ~$185).  If the last close
        # deviates more than 3× from the 20-day median we treat it as bad data
        # and fall back to the previous close.
        if len(df) >= 5:
            median_20 = df['Close'].iloc[-21:-1].median()
            if median_20 > 0 and price > median_20 * 3:
                logger.warning(
                    f"{ticker}: last close ${price:.2f} is {price/median_20:.1f}x the "
                    f"20d median ${median_20:.2f} — likely bad data, using previous close"
                )
                price = df['Close'].iloc[-2]
            elif median_20 > 0 and price < median_20 / 3:
                logger.warning(
                    f"{ticker}: last close ${price:.2f} is only {price/median_20:.2f}x the "
                    f"20d median ${median_20:.2f} — likely bad data, using previous close"
                )
                price = df['Close'].iloc[-2]

        # ── Technical indicators ─────────────────────────────────────────────
        t = _time.time()
        df_ind  = _ti.calculate_all_indicators(df)
        signals = _ti.analyze_current_signals(df_ind)
        ind     = signals.get('indicators', {})
        timings['technical'] = round(_time.time() - t, 2)

        rsi_val    = ind.get('RSI', {}).get('value')
        macd_trend = ind.get('MACD', {}).get('trend', 'neutral')
        ma_info    = ind.get('MA', {})

        rsi_score  = _score_rsi(rsi_val)
        macd_score = WEIGHTS['macd'] if macd_trend == 'bullish' else (
                     WEIGHTS['macd'] // 2 if macd_trend == 'neutral' else 0)
        ma_score   = _score_ma(ma_info, df_ind, price)

        avg_vol      = df['Volume'].rolling(20).mean().iloc[-1]
        recent_vol   = df['Volume'].iloc[-5:].mean()
        vol_ratio    = recent_vol / avg_vol if avg_vol > 0 else 1.0
        vol_score    = min(WEIGHTS['volume'], int(vol_ratio * (WEIGHTS['volume'] / 2)))
        price_5d     = df['Close'].iloc[-5] if len(df) >= 5 else price
        momentum_pct = ((price - price_5d) / price_5d) * 100
        mom_score    = min(WEIGHTS['momentum'], max(0, int(momentum_pct * 3)))

        # ── Forecast ─────────────────────────────────────────────────────────
        t = _time.time()
        forecast_score, forecast_price, forecast_change = 0, None, None
        try:
            fc = StockForecaster(ticker, df).run_all_forecasts(days_ahead=forecast_days)
            if fc:
                forecast_change = fc.get('change_percent', 0)
                forecast_price  = fc.get('predicted_price')
                forecast_score  = 0   # indicative only — excluded from score
        except Exception:
            pass
        timings['forecast'] = round(_time.time() - t, 2)

        # ── Short interest ────────────────────────────────────────────────────
        short_pct   = info.get('shortPercentOfFloat') or 0
        short_ratio = info.get('shortRatio') or 0
        si_score    = _score_short_interest(short_pct, short_ratio)

        # ── Active Squeeze Detection ──────────────────────────────────────────
        squeeze_bonus = 0
        squeeze_active = False
        if short_pct >= 0.10:
            price_up   = momentum_pct > 3
            volume_up  = vol_ratio >= 1.5
            high_short = short_pct >= 0.20
            if price_up and volume_up and high_short:
                squeeze_bonus  = 15
                squeeze_active = True
            elif price_up and volume_up:
                squeeze_bonus  = 7
            elif volume_up and high_short:
                squeeze_bonus  = 4

        inst_pct    = info.get('heldPercentInstitutions') or 0
        inst_change = _get_institutional_change(stock)
        inst_score  = _score_institutional(inst_pct, inst_change)

        # ── Fundamentals (P/E, growth, margin, D/E) ──────────────────────────
        fund_score = _score_fundamentals(info)

        # ── DCF Valuation — P/S fallback for loss-making companies ───────────
        t = _time.time()
        dcf_data  = calculate_dcf(info, cashflow_df=cashflow_df)
        if dcf_data is None:
            dcf_data = calculate_ps_valuation(info)   # P/S proxy for negative-FCF names
        dcf_score = dcf_data["dcf_score"] if dcf_data else 0
        timings['dcf'] = round(_time.time() - t, 2)

        # ── Insider ───────────────────────────────────────────────────────────
        t = _time.time()
        insider_score = 0
        try:
            ins = _insider.calculate_conviction_score(ticker)
            insider_score = min(WEIGHTS['insider'], int(ins.conviction_score / 20))
        except Exception:
            pass
        timings['insider'] = round(_time.time() - t, 2)

        # ── Google Trends ─────────────────────────────────────────────────────
        t = _time.time()
        try:
            trends_data = google_trends_score(ticker)
            trends_scr  = min(WEIGHTS['trends'], trends_data['trends_score'])
        except Exception:
            trends_data = {'interest': 0, 'avg_interest': 0, 'spike': False, 'trends_score': 0}
            trends_scr  = 0
        timings['trends'] = round(_time.time() - t, 2)

        # ── Earnings sentiment ────────────────────────────────────────────────
        t = _time.time()
        news_score     = 0
        news_sentiment = 'neutral'
        try:
            _fh_key = os.getenv("FINNHUB_API_KEY", "")
            if _fh_key:
                from src.earnings_sentiment import get_earnings_sentiment
                _es        = get_earnings_sentiment(ticker, _fh_key)
                news_score     = _es["score"]
                news_sentiment = _es["sentiment"]
        except Exception:
            pass
        timings['earnings_sentiment'] = round(_time.time() - t, 2)

        timings['total'] = round(_time.time() - t_total, 2)

        # ── Score calculation ─────────────────────────────────────────────────
        # Core components (normalized to 90)
        core = (rsi_score + macd_score + ma_score + vol_score + mom_score +
                si_score + inst_score + insider_score + fund_score + dcf_score)
        core_max = (WEIGHTS['rsi'] + WEIGHTS['macd'] + WEIGHTS['ma'] + WEIGHTS['volume'] +
                    WEIGHTS['momentum'] + WEIGHTS['short_interest'] +
                    WEIGHTS['institutional'] + WEIGHTS['insider'] +
                    WEIGHTS['fundamentals'] + WEIGHTS['dcf'])
        bonus = trends_scr + squeeze_bonus + news_score
        total = round(min((core / core_max) * 90 + min(bonus, 20), 100.0), 1)

        result = {
            'ticker':           ticker,
            'score':            round(total, 1),
            'price':            round(price, 2),
            'forecast_price':   round(forecast_price, 2) if forecast_price else None,
            'forecast_change':  round(forecast_change, 2) if forecast_change else None,
            'forecast_note':    'indicative only — excluded from score',
            'rsi':              round(rsi_val, 1) if rsi_val is not None else None,
            'macd':             macd_trend,
            'ma_trend':         ma_info.get('trend', 'unknown'),
            'golden_cross':     ma_info.get('golden_cross', False),
            'volume_ratio':     round(vol_ratio, 2),
            'momentum':         round(momentum_pct, 2),
            'short_pct':        round(short_pct * 100, 1),
            'days_to_cover':    round(short_ratio, 1),
            'squeeze_active':   squeeze_active,
            'squeeze_bonus':    squeeze_bonus,
            'inst_pct':         round(inst_pct * 100, 1),
            'inst_change':      round(inst_change, 3) if inst_change else None,
            'insider_conv':     insider_score * 20,
            'fund_score':       fund_score,
            'fund_pe':          info.get('trailingPE') or info.get('forwardPE'),
            'fund_rev_growth':  info.get('revenueGrowth'),
            'fund_margin':      info.get('profitMargins'),
            'fund_de':          info.get('debtToEquity'),
            'dcf':              dcf_data,   # full DCF dict or None
            'dcf_score':        dcf_score,
            'news_sentiment':   news_sentiment,
            'news_score':       news_score,
            'trends_interest':  trends_data['interest'],
            'trends_spike':     trends_data['spike'],
            '_scores': {
                'rsi': rsi_score, 'macd': macd_score, 'ma': ma_score,
                'volume': vol_score, 'momentum': mom_score,
                'forecast': forecast_score, 'short': si_score,
                'institutional': inst_score, 'insider': insider_score,
                'fundamentals': fund_score, 'dcf': dcf_score,
                'news': news_score, 'trends': trends_scr,
            },
            '_timings': timings,
        }
        score_cache.put(ticker, result, forecast_days)
        return result

    except Exception as e:
        logger.debug(f"{ticker}: {e}")
        return None


# ── helpers ───────────────────────────────────────────────────────────────────

def _score_rsi(rsi: Optional[float]) -> int:
    if rsi is None:          return WEIGHTS['rsi'] // 2
    if 40 <= rsi <= 65:      return WEIGHTS['rsi']
    if 30 <= rsi < 40 or 65 < rsi <= 70: return int(WEIGHTS['rsi'] * 0.7)
    if rsi < 30:             return int(WEIGHTS['rsi'] * 0.5)
    if 70 < rsi <= 75:       return int(WEIGHTS['rsi'] * 0.3)
    return 0


def _score_ma(ma_info: dict, df: pd.DataFrame, price: float) -> int:
    score, w = 0, WEIGHTS['ma']
    trend = ma_info.get('trend', '')
    if trend == 'strong_uptrend':   score += int(w * 0.5)
    elif trend == 'uptrend':        score += int(w * 0.35)
    elif trend == 'sideways':       score += int(w * 0.15)
    if ma_info.get('golden_cross'): score += int(w * 0.25)
    for col, pct in [('SMA_20', 0.15), ('SMA_50', 0.1)]:
        if col in df.columns:
            val = df[col].iloc[-1]
            if pd.notna(val) and price > val:
                score += int(w * pct)
    return min(score, w)


def _score_short_interest(short_pct: float, dtc: float) -> int:
    w, score = WEIGHTS['short_interest'], 0
    if 0.15 <= short_pct <= 0.40:  score += int(w * 0.6)
    elif short_pct > 0.40:         score += int(w * 0.4)
    elif 0.08 <= short_pct < 0.15: score += int(w * 0.3)
    if dtc >= 5:   score += int(w * 0.4)
    elif dtc >= 3: score += int(w * 0.2)
    return min(score, w)


def _get_institutional_change(stock) -> Optional[float]:
    try:
        h = stock.institutional_holders
        if h is None or h.empty: return None
        if 'pctChange' in h.columns: return h['pctChange'].mean()
        if 'pctHeld'   in h.columns: return h['pctHeld'].mean()
    except Exception:
        pass
    return None


def _score_institutional(inst_pct: float, inst_change: Optional[float]) -> int:
    w, score = WEIGHTS['institutional'], 0
    if inst_pct >= 0.50:   score += int(w * 0.6)
    elif inst_pct >= 0.20: score += int(w * 0.5)
    elif inst_pct >= 0.05: score += int(w * 0.3)
    if inst_change is not None and inst_change > 0:
        score += int(w * 0.4)
    return min(score, w)


def _score_fundamentals(info: dict) -> int:
    w, score = WEIGHTS['fundamentals'], 0
    pe = info.get('trailingPE') or info.get('forwardPE')
    if pe:
        if 0 < pe <= 20:     score += 3
        elif 20 < pe <= 40:  score += 2
        elif 40 < pe <= 80:  score += 1
    rev_growth = info.get('revenueGrowth')
    if rev_growth is not None:
        if rev_growth >= 0.30:   score += 3
        elif rev_growth >= 0.15: score += 2
        elif rev_growth >= 0.05: score += 1
    margin = info.get('profitMargins')
    if margin is not None:
        if margin >= 0.20:   score += 2
        elif margin >= 0.10: score += 1
    de = info.get('debtToEquity')
    if de is not None:
        if de <= 0.5:    score += 2
        elif de <= 1.5:  score += 1
    return min(score, w)


def signal_label(score: float) -> str:
    if score >= 75: return "STRONG BUY"
    if score >= 60: return "BUY"
    if score >= 45: return "WATCH"
    if score >= 35: return "NEUTRAL"
    return "SKIP"
