"""Options flow data fetching and analysis via yfinance."""
from __future__ import annotations
import yfinance as yf
import pandas as pd


def get_options_summary(ticker: str, max_expirations: int = 6) -> dict | None:
    """
    Fetch options chain data for a ticker across the nearest N expirations.
    Returns a unified dict with calls/puts DataFrames, KPIs, and unusual activity.
    Returns None on error or if no options exist.
    """
    try:
        t = yf.Ticker(ticker)
        expirations = t.options
        if not expirations:
            return None

        try:
            current_price = t.fast_info.last_price
        except Exception:
            current_price = None

        all_calls, all_puts = [], []
        for exp in expirations[:max_expirations]:
            try:
                chain = t.option_chain(exp)
                calls = chain.calls.copy()
                puts  = chain.puts.copy()
                calls["expiration"] = exp
                puts["expiration"]  = exp
                all_calls.append(calls)
                all_puts.append(puts)
            except Exception:
                continue

        if not all_calls:
            return None

        df_calls = pd.concat(all_calls, ignore_index=True)
        df_puts  = pd.concat(all_puts,  ignore_index=True)

        for df in (df_calls, df_puts):
            df["volume"]            = pd.to_numeric(df["volume"],            errors="coerce").fillna(0)
            df["openInterest"]      = pd.to_numeric(df["openInterest"],      errors="coerce").fillna(0)
            df["impliedVolatility"] = pd.to_numeric(df["impliedVolatility"], errors="coerce").fillna(0)
            df["bid"]               = pd.to_numeric(df.get("bid",  0),       errors="coerce").fillna(0)
            df["ask"]               = pd.to_numeric(df.get("ask",  0),       errors="coerce").fillna(0)
            df["lastPrice"]         = pd.to_numeric(df.get("lastPrice", 0),  errors="coerce").fillna(0)

        total_call_vol = df_calls["volume"].sum()
        total_put_vol  = df_puts["volume"].sum()
        total_call_oi  = df_calls["openInterest"].sum()
        total_put_oi   = df_puts["openInterest"].sum()

        pcr_vol = (total_put_vol / total_call_vol) if total_call_vol > 0 else None
        pcr_oi  = (total_put_oi  / total_call_oi)  if total_call_oi  > 0 else None

        # Net premium flow (contracts × mid × 100 shares)
        df_calls["mid"] = (df_calls["bid"] + df_calls["ask"]) / 2
        df_puts["mid"]  = (df_puts["bid"]  + df_puts["ask"])  / 2
        call_premium = (df_calls["volume"] * df_calls["mid"] * 100).sum()
        put_premium  = (df_puts["volume"]  * df_puts["mid"]  * 100).sum()

        # Strike-level OI aggregation (for heatmap)
        sc = df_calls.groupby("strike")[["openInterest", "volume"]].sum().reset_index()
        sc.columns = ["strike", "call_oi", "call_vol"]
        sp = df_puts.groupby("strike")[["openInterest", "volume"]].sum().reset_index()
        sp.columns = ["strike", "put_oi", "put_vol"]
        strikes = sc.merge(sp, on="strike", how="outer").fillna(0).sort_values("strike")

        # Expiry-level volume breakdown
        ec = df_calls.groupby("expiration")["volume"].sum().reset_index()
        ec.columns = ["expiration", "call_volume"]
        ep = df_puts.groupby("expiration")["volume"].sum().reset_index()
        ep.columns = ["expiration", "put_volume"]
        exp_breakdown = ec.merge(ep, on="expiration", how="outer").fillna(0).sort_values("expiration")

        return {
            "ticker":         ticker.upper(),
            "price":          current_price,
            "expirations":    list(expirations[:max_expirations]),
            "total_call_vol": int(total_call_vol),
            "total_put_vol":  int(total_put_vol),
            "total_call_oi":  int(total_call_oi),
            "total_put_oi":   int(total_put_oi),
            "pcr_vol":        round(pcr_vol, 3) if pcr_vol is not None else None,
            "pcr_oi":         round(pcr_oi,  3) if pcr_oi  is not None else None,
            "call_premium":   call_premium,
            "put_premium":    put_premium,
            "unusual":        _find_unusual(df_calls, df_puts),
            "strikes":        strikes.to_dict("records"),
            "exp_breakdown":  exp_breakdown.to_dict("records"),
            "calls_df":       df_calls,
            "puts_df":        df_puts,
        }
    except Exception:
        return None


def _find_unusual(df_calls: pd.DataFrame, df_puts: pd.DataFrame,
                  min_volume: int = 100, vol_oi_ratio: float = 3.0) -> list:
    """Find unusual contracts: vol/OI ≥ threshold or high absolute volume."""
    def _calc_vol_oi_ratio(r) -> float | None:
        if r["openInterest"] > 0:
            return r["volume"] / r["openInterest"]
        # OI == 0: new contract with no established position
        if r["volume"] < 500:
            return None  # skip — no meaningful activity
        return r["volume"] / 100  # treat as proportional to volume alone

    results = []
    for side, df in [("CALL", df_calls), ("PUT", df_puts)]:
        df = df[df["volume"] >= min_volume].copy()
        df["vol_oi_ratio"] = df.apply(_calc_vol_oi_ratio, axis=1)
        df = df[df["vol_oi_ratio"].notna()]  # drop rows skipped due to OI=0 + low volume
        unusual = df[(df["vol_oi_ratio"] >= vol_oi_ratio) | (df["volume"] >= 5000)]
        for _, row in unusual.iterrows():
            results.append({
                "side":          side,
                "expiration":    row.get("expiration", ""),
                "strike":        row["strike"],
                "volume":        int(row["volume"]),
                "open_interest": int(row["openInterest"]),
                "vol_oi_ratio":  round(row["vol_oi_ratio"], 1),
                "iv":            round(row["impliedVolatility"] * 100, 1),
                "last_price":    round(row["lastPrice"], 2),
                "in_the_money":  bool(row.get("inTheMoney", False)),
            })
    results.sort(key=lambda x: x["volume"], reverse=True)
    return results[:50]


def get_ai_options_verdict(ticker: str, data: dict) -> str:
    """Generate an LLM-based verdict on options flow for a ticker."""
    from src.llm_client import llm_complete

    pcr       = data.get("pcr_vol", "N/A")
    call_vol  = data.get("total_call_vol", 0)
    put_vol   = data.get("total_put_vol",  0)
    call_prem = data.get("call_premium",   0)
    put_prem  = data.get("put_premium",    0)
    price     = data.get("price",          "N/A")

    top_unusual = data.get("unusual", [])[:5]
    unusual_str = "\n".join(
        f"  - {u['side']} ${u['strike']} exp {u['expiration']}: "
        f"vol={u['volume']:,}  OI={u['open_interest']:,}  ratio={u['vol_oi_ratio']}x  IV={u['iv']}%"
        for u in top_unusual
    ) or "  None detected"

    prompt = f"""Analyze the options flow for {ticker} (current price: ${price}):

Put/Call Volume Ratio (PCR): {pcr}
Call Volume: {call_vol:,}  |  Put Volume: {put_vol:,}
Estimated Call Premium Flow: ${call_prem:,.0f}
Estimated Put Premium Flow:  ${put_prem:,.0f}

Top Unusual Contracts:
{unusual_str}

כתוב ניתוח תמציתי של 3-4 משפטים בעברית:
1. סנטימנט (שורי/דובי/נייטרלי) לפי PCR וזרימת הפרמיה
2. מה הפעילות החריגה מרמזת על פוזיציונינג מוסדי
3. רמות מחיר מפתח לעקוב אחריהן
4. הערת זהירות אחת

היה ישיר ומעשי. ללא כתבי ויתור."""

    return llm_complete(
        prompt,
        system="אתה אנליסט זרימת אופציות מקצועי. השב בעברית. היה תמציתי וספציפי.",
        max_tokens=400,
    )


def get_ai_ticker_quick_verdict(r: dict) -> str:
    """
    Quick per-ticker AI verdict from scanner-level data only (no full chain needed).
    Explains what each metric means and what the positioning implies.
    """
    from src.llm_client import llm_complete

    ticker  = r.get("ticker", "?")
    price   = r.get("price",  "N/A")
    pcr     = r.get("pcr_vol")
    pcr_str = f"{pcr:.2f}" if pcr is not None else "N/A"
    call_vol = r.get("call_vol", 0)
    put_vol  = r.get("put_vol",  0)
    unusual  = r.get("unusual_count", 0)
    side     = r.get("top_side",  "?")
    strike   = r.get("top_strike",  0)
    expiry   = r.get("top_expiry",  "?")
    volume   = r.get("top_volume",  0)
    ratio    = r.get("top_vol_oi_ratio", 0)
    iv       = r.get("top_iv",  0)

    prompt = f"""You are explaining options flow data to a retail trader who is new to options.

Ticker: {ticker} | Price: ${price}
Put/Call Ratio: {pcr_str}
Call Volume: {call_vol:,} | Put Volume: {put_vol:,}
Unusual contracts detected: {unusual}
Biggest unusual contract: {side} ${strike:.0f} strike, expiry {expiry}, volume {volume:,}, Vol/OI ratio {ratio}x, IV {iv:.1f}%

כתוב הסבר ברור וידידותי בעברית (4-5 משפטים) הכולל:
1. מה ה-PCR של {pcr_str} אומר בשפה פשוטה (שורי/דובי/נייטרלי ולמה)
2. מה המשמעות של {unusual} חוזה/ות חריג/ים — במיוחד ה-{side} ב-${strike:.0f} עם יחס {ratio}x נפח/פוזיציה פתוחה
3. מה ה-IV של {iv:.1f}% מרמז לגבי תנודתיות צפויה
4. מסקנה אחת מעשית: מה ה"כסף החכם" עשוי לצפות?

השתמש בשפה פשוטה. הסבר מונחים טכניים. ללא כתבי ויתור."""

    return llm_complete(
        prompt,
        system="אתה מחנך ואנליסט אופציות ידידותי אך חד. השב בעברית בלבד.",
        max_tokens=350,
    )


def get_ai_scan_analyst(results: list[dict]) -> str:
    """
    Holistic AI analysis of all tickers returned by the unusual activity scanner.
    Explains collective positioning, market themes, and standout tickers.
    """
    from src.llm_client import llm_complete

    if not results:
        return "No scan results to analyze."

    lines = []
    for r in results:
        pcr = f"{r['pcr_vol']:.2f}" if r.get("pcr_vol") is not None else "N/A"
        price = f"${r['price']:.2f}" if r.get("price") else "N/A"
        lines.append(
            f"{r['ticker']:6s} | {price:8s} | PCR={pcr:5s} | "
            f"CallVol={r['call_vol']:>8,} | PutVol={r['put_vol']:>8,} | "
            f"Unusual={r['unusual_count']:>2} | Top: {r['top_side']} ${r['top_strike']:.0f} "
            f"exp {r['top_expiry']} vol={r['top_volume']:,} ratio={r['top_vol_oi_ratio']}x IV={r['top_iv']:.1f}%"
        )

    table_str = "\n".join(lines)
    n = len(results)

    bullish_tickers = [r["ticker"] for r in results if r.get("pcr_vol") and r["pcr_vol"] < 0.7]
    bearish_tickers = [r["ticker"] for r in results if r.get("pcr_vol") and r["pcr_vol"] > 1.2]
    call_sweeps     = [r["ticker"] for r in results if r["top_side"] == "CALL"]
    put_sweeps      = [r["ticker"] for r in results if r["top_side"] == "PUT"]
    top3            = [r["ticker"] for r in results[:3]]

    prompt = f"""You are a senior options flow analyst reviewing a scan of {n} tickers for unusual activity.

SCAN DATA:
{table_str}

SUMMARY:
- Bullish PCR (<0.7): {', '.join(bullish_tickers) or 'none'}
- Bearish PCR (>1.2): {', '.join(bearish_tickers) or 'none'}
- Top call sweeps: {', '.join(call_sweeps) or 'none'}
- Top put sweeps:  {', '.join(put_sweeps) or 'none'}
- Most unusual activity: {', '.join(top3)}

כתוב ניתוח מובנה בעברית עם הסעיפים הבאים:

## סנטימנט שוק כולל
2-3 משפטים על מה שחלוקת ה-PCR וכיוון זרימת הפרמיה אומרים לנו על מצב הרוח בשוק כרגע.

## טיקרים בולטים
לכל אחד מ-3 הטיקרים הפעילים ביותר, משפט אחד המסביר ספציפית מה הפוזיציונינג באופציות שלהם מרמז.

## מה הנתונים אומרים (בשפה פשוטה)
3-4 נקודות המסבירות מדדי מפתח מהסריקה הזו במונחים פשוטים:
- מה PCR < 0.7 לעומת > 1.2 אומר כאן
- מה יחס נפח/פוזיציה פתוחה גבוה מסמן
- מה IV גבוה בחוזים חריגים מרמז
- מה call sweeps לעומת put sweeps בדרך כלל קודמים להם

## רמות מפתח לעקוב
הסטריקים החשובים ביותר בסריקה (תמיכה/התנגדות לפי OI גבוה או נפח חריג).

## סיכום
משפט אחד: מהו האות המוסדי הדומיננטי מהסריקה הזו?

היה ספציפי, השתמש בנתונים האמיתיים. ללא כתבי ויתור כלליים."""

    return llm_complete(
        prompt,
        system="אתה אנליסט זרימת אופציות בכיר. השב בעברית בלבד. היה ספציפי, מובנה ומעשי.",
        max_tokens=700,
    )


def scan_unusual_activity(tickers: list[str], min_volume: int = 300,
                           progress_callback=None) -> list[dict]:
    """
    Scan multiple tickers for unusual options activity.
    Returns a list of per-ticker summary dicts, sorted by unusual contract count.
    """
    results = []
    total = len(tickers)

    for i, ticker in enumerate(tickers):
        try:
            data = get_options_summary(ticker, max_expirations=3)
            if data and data.get("unusual"):
                top = data["unusual"][0]
                results.append({
                    "ticker":           ticker.upper(),
                    "price":            data["price"],
                    "pcr_vol":          data["pcr_vol"],
                    "call_vol":         data["total_call_vol"],
                    "put_vol":          data["total_put_vol"],
                    "unusual_count":    len(data["unusual"]),
                    "top_side":         top["side"],
                    "top_strike":       top["strike"],
                    "top_expiry":       top["expiration"],
                    "top_volume":       top["volume"],
                    "top_vol_oi_ratio": top["vol_oi_ratio"],
                    "top_iv":           top["iv"],
                })
        except Exception:
            pass

        if progress_callback:
            progress_callback(i + 1, total, ticker)

    results.sort(key=lambda x: x["unusual_count"], reverse=True)
    return results
