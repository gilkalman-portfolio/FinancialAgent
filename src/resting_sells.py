"""
Cumulative resting-SELL accounting — the long-only invariant at the broker.

The invariant is NOT "each resting SELL fits inside the position". It is:

    sum of every resting SELL claim on a ticker  <=  shares held

where a bracket's STP stop and LMT target count ONCE (they are an OCA pair —
when one fills the other is cancelled, so together they can only ever sell
their quantity once). Any other SELL (a software exit LMT, an orphaned order,
a second bracket) is its own claim.

YOU, 2026-11-04: 278 held, bracket target LMT 278 + STP 278 resting, then the
T1 tiered exit sent a LMT SELL 111. Checked one at a time, 111 <= 278 and
278 <= 278 both pass. At the 09:30 open both filled: 389 sold, short 111.

Two places enforce it, both through cap_ticker():
  - order_manager.submit_exit() — before sending any exit, the existing claims
    are shrunk to `held - exit_qty`; if that can't be confirmed, the exit is
    not sent (fail closed — never stack).
  - ibkr_worker._reconcile_sell_sources() — every cycle, any ticker whose
    claims exceed held is shrunk back under it (self-healing for anything that
    got past the first, e.g. orphaned clientId-0 legs).

When claims must shrink, bracket/protective claims absorb the cut first and
software exit orders keep their size: the exit is the most recent deliberate
decision, and once it fills the position it was protecting is smaller anyway.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)


@dataclass
class Claim:
    """One independent way shares can leave the account."""
    src: Any                      # connection that can see/modify these orders
    orders: list[dict] = field(default_factory=list)
    protective: bool = False      # bracket leg(s) — absorbs a cut before software exits

    @property
    def qty(self) -> int:
        # OCA: only one member can fill, so the claim is the largest member.
        return max((int(o.get("qty") or 0) for o in self.orders), default=0)

    @property
    def ticker(self) -> str:
        return self.orders[0].get("ticker", "") if self.orders else ""

    def describe(self) -> str:
        legs = "+".join(f"{o.get('order_type')}#{o.get('order_id')}" for o in self.orders)
        return f"{legs} {self.qty}sh"


def _is_day(o: dict) -> bool:
    return str(o.get("tif") or "").upper() == "DAY"


def build_claims(entries: Iterable[tuple[Any, dict]],
                 qty_cap: float | None = None) -> list[Claim]:
    """Group (source, SELL order) entries for ONE ticker into claims.

    Grouping, per source (a clientId-1 and a clientId-0 session number their
    orders independently, so groups never span sources):
      1. same non-empty oca_group           -> one claim
      2. else same non-zero parent_id       -> one claim (bracket children)
      3. else a STP and a LMT of equal qty, neither DAY -> paired as a bracket
         (orphaned legs can lose their parent link; pairing them is the
         conservative read only when the sizes match)
      4. anything else                      -> its own claim

    qty_cap, if given, caps each order's quantity for accounting (the per-order
    pass in the reconcile has already sized every order down to held).
    """
    by_src: dict[int, tuple[Any, list[dict]]] = {}
    for src, o in entries:
        if o.get("action") != "SELL":
            continue
        if qty_cap is not None:
            o = {**o, "qty": min(int(o.get("qty") or 0), int(qty_cap))}
        by_src.setdefault(id(src), (src, []))[1].append(o)

    claims: list[Claim] = []
    for src, orders in by_src.values():
        groups: dict[tuple, Claim] = {}
        loose: list[dict] = []
        for o in orders:
            if o.get("oca_group"):
                key = ("oca", o["oca_group"])
            elif o.get("parent_id"):
                key = ("parent", o["parent_id"])
            else:
                loose.append(o)
                continue
            groups.setdefault(key, Claim(src=src, protective=True)).orders.append(o)
        claims.extend(groups.values())

        stps = [o for o in loose if o.get("order_type") == "STP" and not _is_day(o)]
        lmts = [o for o in loose if o.get("order_type") == "LMT" and not _is_day(o)]
        paired: set[int] = set()
        for s in stps:
            for l in lmts:
                if id(l) in paired:
                    continue
                if int(s.get("qty") or 0) == int(l.get("qty") or 0):
                    paired.update((id(s), id(l)))
                    claims.append(Claim(src=src, orders=[s, l], protective=True))
                    break
        for o in loose:
            if id(o) in paired:
                continue
            protective = o.get("order_type") == "STP" or not _is_day(o) and o.get("tif")
            claims.append(Claim(src=src, orders=[o], protective=bool(protective)))
    return claims


def plan_cap(claims: list[Claim], max_total: int) -> list[tuple[Claim, int]]:
    """Return [(claim, new_qty)] needed so sum(claim.qty) <= max_total.

    Software (non-protective) claims are budgeted first, then protective ones;
    within each class, lower order ids first (deterministic). new_qty == 0
    means cancel the claim's orders.
    """
    max_total = max(0, int(max_total))
    if sum(c.qty for c in claims) <= max_total:
        return []

    def _key(c: Claim):
        return (c.protective, min(int(o.get("order_id") or 0) for o in c.orders))

    budget = max_total
    plan: list[tuple[Claim, int]] = []
    for c in sorted(claims, key=_key):
        keep = min(c.qty, budget)
        budget -= keep
        if keep < c.qty:
            plan.append((c, keep))
    return plan


def apply_plan(plan: list[tuple[Claim, int]]) -> tuple[bool, list[str]]:
    """Resize/cancel every order in the plan through its own source.

    Returns (all_confirmed, descriptions). Stops at the first unconfirmed
    step — whatever already shrank stays shrunk, which only ever reduces
    exposure to a short.
    """
    done: list[str] = []
    for claim, new_qty in plan:
        for o in claim.orders:
            if int(o.get("qty") or 0) <= new_qty:
                continue
            oid = o.get("order_id")
            try:
                ok = claim.src.resize_order(oid, new_qty)
            except Exception as e:
                logger.error(f"[resting_sells] resize {claim.ticker} #{oid} -> {new_qty} raised: {e}")
                ok = False
            if not ok:
                return False, done + [f"{claim.ticker} #{oid} {o.get('qty')} -> {new_qty} NOT confirmed"]
            done.append(
                f"{claim.ticker} {o.get('order_type')} #{oid} {o.get('qty')}sh -> "
                + (f"{new_qty}sh" if new_qty else "cancelled")
            )
    return True, done


def cap_ticker(sources: Iterable[tuple[Any, list[dict]]], ticker: str,
               max_total: int) -> tuple[bool, list[str]]:
    """Shrink every resting SELL claim on ticker, across all sources, so their
    total is <= max_total. (True, changes) when confirmed or nothing to do."""
    entries = [(src, o) for src, orders in sources for o in (orders or [])
               if isinstance(o, dict) and o.get("ticker") == ticker]
    claims = build_claims(entries)
    plan = plan_cap(claims, max_total)
    if not plan:
        return True, []
    logger.warning(
        f"[resting_sells] {ticker}: resting SELL claims "
        f"{' + '.join(c.describe() for c in claims)} = {sum(c.qty for c in claims)} "
        f"> allowed {max_total} — shrinking"
    )
    return apply_plan(plan)
