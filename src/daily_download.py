"""Batch daily-OHLCV download: Massive (paid, no per-call quota) first,
yfinance for whatever Massive could not serve.

Drop-in for the `yf.download(tickers, period=..., auto_adjust=True)` calls the
full-universe scanners (momentum / supertrend / long-setup) make: same
(field, ticker) MultiIndex-column shape, always MultiIndex.
"""
import pandas as pd
import yfinance as yf
from loguru import logger

from src import massive_client


def download_daily(tickers: list, period: str = "1y") -> pd.DataFrame:
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        return pd.DataFrame()

    primary = massive_client.download_daily(tickers, period)
    served = set(primary.columns.get_level_values(1)) if not primary.empty else set()
    missing = [t for t in tickers if t not in served]
    if served:
        logger.info(f"[daily_download] Massive served {len(served)}/{len(tickers)} tickers")
    if not missing:
        return primary

    logger.info(f"[daily_download] yfinance fallback for {len(missing)} ticker(s)")
    try:
        raw = yf.download(missing, period=period, auto_adjust=True, progress=False, threads=True)
    except Exception as e:
        logger.warning(f"[daily_download] yfinance fallback failed: {e}")
        return primary
    if raw is None or raw.empty:
        return primary
    if not isinstance(raw.columns, pd.MultiIndex):
        raw.columns = pd.MultiIndex.from_product([raw.columns, [missing[0]]])
    if primary.empty:
        return raw
    return pd.concat([primary, raw], axis=1).sort_index()
