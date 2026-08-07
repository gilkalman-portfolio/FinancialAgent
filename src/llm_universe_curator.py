"""
LLM Universe Curator — weekly qualitative curation of the scanner's top-score
pool into the tickers actually worth active real-time monitoring.

Mandate (defined 2026-08-07):
  - Curates a PRE-FILTERED shortlist (top ~150 by composite score). Never
    introduces a ticker the quant scanner did not already surface — enforced
    in code, not just by prompt (see curate_universe()'s allowlist check).
  - Balanced "beat the S&P 500" objective — no forced tilt toward momentum,
    value, or catalyst plays.
  - Hard exclusion: price < PRICE_FLOOR (penny/manipulation-prone), enforced
    before the LLM ever sees the ticker — same $5 floor already used for
    catalyst alerts.
  - Low turnover: last week's list is given as an anchor; the LLM is asked to
    justify CHANGES, not rebuild from scratch.

Disabled by default. Gated by scheduler_config.json's
"llm_universe_curation_enabled" in monitoring_queue.py's integration point —
this module has no opinion on whether it should run; run_weekly_curation()
does nothing unless a caller (scheduler.py) invokes it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from src.database import get_connection

logger = logging.getLogger(__name__)

DIGEST_TOP_N = 150
PRICE_FLOOR = 5.0
SCAN_LOOKBACK_HOURS = 48       # covers both daily scan runs (08:30 + 15:00)
CURATION_STALE_DAYS = 10       # older than this, treat as absent (weekly cadence)
DEFAULT_TARGET_SIZE = 80       # roughly today's monitoring_queue size

_SYSTEM_PROMPT = """You curate a weekly stock-monitoring watchlist for a long-only \
systematic trading bot. The bot's mandate is to beat the S&P 500 over time — no \
forced tilt toward momentum, value, or catalyst plays; use balanced judgment.

You do NOT pick stocks from scratch. You are given a pre-filtered shortlist that \
already passed a quantitative score gate. Your job is to select the subset (roughly \
{target_size} tickers) that most deserves active real-time monitoring this week.

Hard rule: NEVER include a ticker that is not in the shortlist below. Any ticker \
you return that is not on the shortlist will be discarded.

Stability rule: last week's selection is given as an anchor. Only change it where \
you have a real, articulable reason — this is a low-turnover mandate, not a full \
weekly rebuild. Prefer keeping a ticker unless something concrete changed.

Respond with ONLY a JSON object, no markdown fences, no commentary, in exactly \
this shape:
{{
  "selections": [
    {{"ticker": "XXXX", "action": "keep", "rationale": "one short sentence"}},
    {{"ticker": "YYYY", "action": "add", "rationale": "one short sentence"}}
  ],
  "removed": [
    {{"ticker": "ZZZZ", "rationale": "one short sentence"}}
  ]
}}
"removed" lists only tickers from last week's list that you are dropping."""


def _monday_of(dt: datetime) -> str:
    """ISO date of the Monday on/before dt — the week_of key."""
    monday = dt.date() - timedelta(days=dt.weekday())
    return monday.isoformat()


def build_universe_digest(top_n: int = DIGEST_TOP_N) -> list[dict]:
    """Top-ranked tickers from the SAME pool that already feeds the live
    monitoring queue, enriched with score components already computed by
    stock_scorer.py — no new scans or API calls. Returns [] if no recent
    scan data.

    Ranking is delegated to monitoring_queue._scanner_tickers() rather than
    querying scan_results directly. scan_results is shared by scheduled
    scans (explosion_score = the stock_scorer composite) AND catalyst scans
    (explosion_score = a differently-scaled explosion metric) with no
    scan_type filter of its own — mixing the two produced exactly this bug
    in backtester.py before it was fixed to filter scan_type='scheduled'.
    Reusing _scanner_tickers() guarantees the digest's population is
    provably identical to "the scanner pool" this feature is meant to
    curate, not a similar-but-diverging duplicate query.
    """
    from src.monitoring_queue import _scanner_tickers

    ranked = _scanner_tickers(min_score=0)[:top_n]  # already sorted DESC by score
    if not ranked:
        return []

    tickers = [t for t, _, _ in ranked]
    placeholders = ",".join("?" for _ in tickers)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT ticker, price, raw_data, catalyst, scanned_at
            FROM scan_results
            WHERE ticker IN ({placeholders})
            ORDER BY scanned_at ASC
            """,
            tickers,
        ).fetchall()
    meta = {}
    for r in rows:
        meta[r["ticker"]] = r  # ascending scan time — last write per ticker wins

    digest = []
    for ticker, score, _ in ranked:
        m = meta.get(ticker)
        price = m["price"] if m else None
        if price is None or price < PRICE_FLOOR:
            continue
        entry = {"ticker": ticker, "score": score, "price": price}
        try:
            raw = json.loads(m["raw_data"]) if m["raw_data"] else {}
        except (TypeError, ValueError):
            raw = {}
        if raw.get("momentum") is not None:
            entry["momentum"] = raw["momentum"]
        if raw.get("short_pct") is not None:
            entry["short_pct"] = raw["short_pct"]
        dcf = raw.get("dcf")
        if isinstance(dcf, dict) and dcf.get("margin_of_safety_pct") is not None:
            entry["dcf_margin_of_safety_pct"] = dcf["margin_of_safety_pct"]
        if m["catalyst"]:
            entry["catalyst"] = m["catalyst"]
        digest.append(entry)
    return digest


def curate_universe(
    digest: list[dict],
    previous_tickers: list[str],
    target_size: int = DEFAULT_TARGET_SIZE,
) -> dict | None:
    """Ask the LLM to curate `digest` down to roughly target_size tickers.

    Returns {"selections": [...], "removed": [...]} or None on any failure —
    callers must treat None as "no curation this cycle, no change." Every
    ticker in the LLM's output is verified against `digest` before being
    trusted; a ticker the LLM invents that isn't in the shortlist is dropped
    and logged, never persisted.
    """
    from src.llm_client import llm_complete

    if not digest:
        logger.warning("[llm_curator] empty digest — skipping curation")
        return None

    allowed = {d["ticker"] for d in digest}
    prompt = (
        f"Shortlist ({len(digest)} tickers, JSON):\n{json.dumps(digest)}\n\n"
        f"Last week's monitored tickers: {json.dumps(previous_tickers)}\n"
    )
    system = _SYSTEM_PROMPT.format(target_size=target_size)

    try:
        raw = llm_complete(prompt, system=system, max_tokens=4000)
    except Exception as e:
        logger.error(f"[llm_curator] LLM call failed: {e}")
        return None

    cleaned = raw.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"[llm_curator] failed to parse LLM response: {e}")
        return None

    selections = parsed.get("selections") if isinstance(parsed, dict) else None
    if not isinstance(selections, list):
        logger.error("[llm_curator] malformed response — 'selections' is not a list")
        return None
    removed = parsed.get("removed", [])
    if not isinstance(removed, list):
        removed = []

    verified = []
    for s in selections:
        t = s.get("ticker") if isinstance(s, dict) else None
        if isinstance(t, str):
            t = t.strip().upper()  # the LLM echoing "aapl" must still match "AAPL"
        if t not in allowed:
            logger.warning(f"[llm_curator] dropped off-list ticker from LLM output: {t!r}")
            continue
        action = s.get("action")
        verified.append({
            "ticker": t,
            "action": action if action in ("keep", "add") else "keep",
            "rationale": str(s.get("rationale", ""))[:300],
        })

    verified_removed = [
        {"ticker": r["ticker"], "rationale": str(r.get("rationale", ""))[:300]}
        for r in removed if isinstance(r, dict) and r.get("ticker")
    ]

    if not verified:
        logger.error("[llm_curator] LLM returned zero valid selections — treating as failure")
        return None

    return {"selections": verified, "removed": verified_removed}


def _previous_week_tickers(before_week_of: str) -> list[str]:
    """Most recent prior week's kept/added tickers — the stability anchor."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(week_of) AS w FROM llm_curated_universe WHERE week_of < ?",
            (before_week_of,),
        ).fetchone()
        if not row or not row["w"]:
            return []
        rows = conn.execute(
            "SELECT ticker FROM llm_curated_universe WHERE week_of = ? AND action IN ('keep','add')",
            (row["w"],),
        ).fetchall()
    return [r["ticker"] for r in rows]


def run_weekly_curation(target_size: int = DEFAULT_TARGET_SIZE) -> dict:
    """Orchestrates one weekly curation cycle: digest -> LLM -> persist.

    Always returns a summary dict, even on failure. On failure ("ok": False),
    nothing is written — the previous week's curated set, if any, remains in
    effect via current_curated_tickers()'s "most recent week_of" lookup.
    """
    week_of = _monday_of(datetime.now())
    digest = build_universe_digest()
    previous = _previous_week_tickers(week_of)

    result = curate_universe(digest, previous, target_size=target_size)
    if result is None:
        logger.warning("[llm_curator] curation failed or was empty — no change this week")
        return {"week_of": week_of, "curated": [], "removed": [], "ok": False}

    now = datetime.now().isoformat()
    rows = [
        (week_of, s["ticker"], s["action"], s["rationale"], now)
        for s in result["selections"]
    ] + [
        (week_of, r["ticker"], "remove", r["rationale"], now)
        for r in result["removed"]
    ]
    with get_connection() as conn:
        # A re-run for the same week_of (e.g. a scheduler crash/restart retry)
        # must REPLACE, not accumulate — otherwise a stale/superseded attempt's
        # rows stay UNION'd into current_curated_tickers() alongside the retry's.
        conn.execute("DELETE FROM llm_curated_universe WHERE week_of = ?", (week_of,))
        conn.executemany(
            "INSERT INTO llm_curated_universe (week_of, ticker, action, rationale, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )

    logger.info(
        f"[llm_curator] week {week_of}: {len(result['selections'])} kept/added, "
        f"{len(result['removed'])} removed"
    )
    return {
        "week_of": week_of,
        "curated": [s["ticker"] for s in result["selections"]],
        "removed": [r["ticker"] for r in result["removed"]],
        "ok": True,
    }


def current_curated_tickers() -> set | None:
    """Tickers from the most recent week_of with action in ('keep','add').

    Returns None — meaning "no curation in effect, do not filter" — if the
    table is empty, the latest week_of is older than CURATION_STALE_DAYS, or
    it resolves to an empty set. Stale data silently disables curation rather
    than filtering the live monitoring queue against outdated judgment.
    """
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(week_of) AS w FROM llm_curated_universe").fetchone()
        if not row or not row["w"]:
            return None
        try:
            week_dt = datetime.fromisoformat(row["w"])
        except ValueError:
            return None
        if (datetime.now() - week_dt).days > CURATION_STALE_DAYS:
            logger.warning(f"[llm_curator] curated universe stale (week_of={row['w']}) — ignoring")
            return None
        rows = conn.execute(
            "SELECT ticker FROM llm_curated_universe WHERE week_of = ? AND action IN ('keep','add')",
            (row["w"],),
        ).fetchall()
    tickers = {r["ticker"] for r in rows}
    return tickers or None
