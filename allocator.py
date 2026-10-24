"""
allocator.py - ReliefStock AI, Phase 3: allocation with a 30% minimum-share floor.

For each resource, requests are served in PRIORITY order (priority.py):
  Phase A  every request gets its minimum share (30% of what it asked for)
           in priority order, while stock lasts;
  Phase B  the remaining stock tops requests up, again in priority order.
So a low-ranked but real need (the hamlet asking for 60 medical kits) is not
reduced to zero by one big high-ranked request, yet high-ranked requests still
get the biggest slice. If stock is too small for every floor, floors are filled
strictly in priority order and the first request that cannot get its full floor
receives whatever is left (no stock is left lying unused).

Every request ends FULFILLED, PARTIAL or UNMET with a reason ("no stock" vs
"stock went to higher-ranked requests"). Distance plays no part here.

Two steps, so a round can be previewed before it is committed:
    plan_round(store)    pure: reads the store, changes nothing
    commit_round(store, plan)   atomic: ledger + inventory + events, or nothing
    run_round(store)     plan + commit
No `from __future__ import annotations` (breaks dataclasses on Agentverse).
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List

import config
from models import (
    Allocation, Delivery, EventType, PriorityScore, RequestStatus,
    Resource, ResourceRequest, UnmetReason, new_id,
)
from priority import rank_requests


class StaleRoundError(Exception):
    """The store changed between planning and committing a round."""


@dataclass
class AllocationRow:
    request: ResourceRequest
    score: PriorityScore
    requested: int
    floor: int
    granted: int
    status: RequestStatus
    reason: str


@dataclass
class RoundPlan:
    round_no: int
    rows: List[AllocationRow] = field(default_factory=list)      # global rank order
    stock_before: Dict[str, int] = field(default_factory=dict)
    stock_after: Dict[str, int] = field(default_factory=dict)


# ------------------------------------------------------------------ pure core
def min_share(quantity):
    """At least MIN_SHARE_FRACTION of the request, rounded UP, never above the request."""
    floor = int(math.ceil(round(quantity * config.MIN_SHARE_FRACTION, 9)))
    return max(1, min(quantity, floor))


def allocate_resource(stock, ranked_requests):
    """
    stock: int available. ranked_requests: requests of ONE resource, best first.
    Returns (grants, floors), lists aligned with ranked_requests.
    """
    remaining = int(stock)
    floors = [min_share(r.quantity) for r in ranked_requests]
    grants = [0] * len(ranked_requests)
    for i in range(len(ranked_requests)):                    # Phase A: minimum shares
        g = min(floors[i], remaining)
        grants[i] = g
        remaining -= g
    for i, r in enumerate(ranked_requests):                  # Phase B: top-ups
        extra = min(r.quantity - grants[i], remaining)
        grants[i] += extra
        remaining -= extra
    return grants, floors


def explain_outcome(requested, granted, floor, stock_before):
    """Returns (RequestStatus, plain-language reason)."""
    pct = int(round(config.MIN_SHARE_FRACTION * 100))
    if granted >= requested:
        return RequestStatus.FULFILLED, ""
    if granted == 0:
        if stock_before <= 0:
            return RequestStatus.UNMET, UnmetReason.NO_STOCK.value
        return RequestStatus.UNMET, UnmetReason.HIGHER_RANKED.value
    if granted < floor:
        return RequestStatus.PARTIAL, ("Only %d of %d: stock was too low to reach the %d%% minimum "
                                       "share" % (granted, requested, pct))
    if granted == floor:
        return RequestStatus.PARTIAL, ("Only %d of %d: received the %d%% minimum share; the rest of "
                                       "the stock went to higher-ranked requests" % (granted, requested, pct))
    return RequestStatus.PARTIAL, ("Only %d of %d: stock ran out after higher-ranked requests were "
                                   "served" % (granted, requested))


# ---------------------------------------------------------------- round steps
def plan_round(store, now=None):
    """Rank all open requests and decide every grant. Changes nothing."""
    now = now or datetime.now(timezone.utc)
    plan = RoundPlan(round_no=store.current_round())
    plan.stock_before = store.get_inventory()
    plan.stock_after = dict(plan.stock_before)

    ranked = rank_requests(store.open_requests(), store.prior_allocation(), now)
    by_resource = {}
    for req, score in ranked:
        by_resource.setdefault(req.resource, []).append((req, score))

    grants_by_id = {}
    for resource, items in by_resource.items():
        stock = plan.stock_before.get(resource.value, 0)
        grants, floors = allocate_resource(stock, [r for r, _ in items])
        for (req, score), g, f in zip(items, grants, floors):
            grants_by_id[req.request_id] = (g, f, stock)
        plan.stock_after[resource.value] = stock - sum(grants)

    for req, score in ranked:                                 # keep global rank order
        g, f, stock = grants_by_id[req.request_id]
        status, reason = explain_outcome(req.quantity, g, f, stock)
        plan.rows.append(AllocationRow(req, score, req.quantity, f, g, status, reason))
    return plan


def commit_round(store, plan):
    """Write the plan: ledger, deliveries, inventory, events. All or nothing."""
    if plan.round_no != store.current_round() or plan.stock_before != store.get_inventory():
        raise StaleRoundError("Stock or round changed since this plan was made; plan again.")

    with store.transaction():
        granted_by_resource = {}
        for row in plan.rows:
            req = row.request
            req.status = row.status
            req.allocated_qty = row.granted
            req.reason = row.reason
            store.update_request(req)

            store.log(EventType.PRIORITY_COMPUTED,
                      "Rank %d for %s %s: %s" % (row.score.rank, req.site_id, req.resource.value,
                                                  row.score.explain()),
                      request_id=req.request_id, site_id=req.site_id, data=row.score.to_dict())

            if row.granted > 0:
                alloc = Allocation(allocation_id=new_id("ALC"), request_id=req.request_id,
                                   site_id=req.site_id, resource=req.resource,
                                   requested_qty=req.quantity, allocated_qty=row.granted,
                                   round_no=plan.round_no)
                store.add_allocation(alloc)
                store.add_delivery(Delivery(delivery_id=alloc.delivery_id,
                                            allocation_id=alloc.allocation_id,
                                            site_id=req.site_id, resource=req.resource,
                                            sent_qty=row.granted))
                store.log(EventType.ALLOCATED,
                          "%s: %d of %d %s allocated (%s)%s"
                          % (req.site_id, row.granted, req.quantity, req.resource.value,
                             row.status.value, (" - " + row.reason) if row.reason else ""),
                          request_id=req.request_id, allocation_id=alloc.allocation_id,
                          delivery_id=alloc.delivery_id, site_id=req.site_id,
                          data={"status": row.status.value, "granted": row.granted})
                granted_by_resource[req.resource] = granted_by_resource.get(req.resource, 0) + row.granted
            else:
                store.log(EventType.ALLOCATED,
                          "%s: UNMET for %d %s - %s" % (req.site_id, req.quantity,
                                                        req.resource.value, row.reason),
                          request_id=req.request_id, site_id=req.site_id,
                          data={"status": "UNMET", "granted": 0})

        for resource, total in granted_by_resource.items():
            # raises StoreError (and rolls everything back) if stock would go negative
            store.adjust_stock(resource, -total, "allocation round %d" % plan.round_no)
        store.advance_round()
    return plan


def run_round(store, now=None):
    return commit_round(store, plan_round(store, now))


# ---------------------------------------------------------------------- tests
if __name__ == "__main__":
    from store import DictBackend, Store, StoreError

    NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    T0 = NOW.isoformat(timespec="seconds")

    def req(rid, site, res, qty, sev, people):
        r = ResourceRequest(site_id=site, resource=res, quantity=qty, people_affected=people,
                            severity=sev, created_at=T0)
        r.request_id = rid
        return r

    def fresh(stock):
        s = Store(DictBackend())
        for res, q in stock.items():
            s.set_stock(res, q)
        return s

    # ---- DEMO: medical kits 150, hospital asks 150, hamlet asks 60 -> hamlet gets 18 ----
    s = fresh({Resource.MEDICAL_KITS: 150})
    s.add_request(req("REQ-HOSP", "S2", Resource.MEDICAL_KITS, 150, 5, 400))
    s.add_request(req("REQ-HAM", "S3", Resource.MEDICAL_KITS, 60, 3, 60))
    plan = run_round(s, NOW)
    got = {r.request.request_id: r for r in plan.rows}
    assert got["REQ-HAM"].granted == 18 and got["REQ-HAM"].status == RequestStatus.PARTIAL
    assert got["REQ-HOSP"].granted == 132 and got["REQ-HOSP"].status == RequestStatus.PARTIAL
    assert s.get_stock("medical_kits") == 0 and s.current_round() == 2
    assert "minimum share" in got["REQ-HAM"].reason
    # priority-first WITHOUT the floor would have given the hamlet 0:
    assert allocate_resource(150, [req("a", "S2", Resource.MEDICAL_KITS, 150, 5, 1),
                                   req("b", "S3", Resource.MEDICAL_KITS, 60, 3, 1)])[0][1] >= 18

    # ---- DEMO: water 2000 vs demand 2100 (request #1 = 1200) ----
    s = fresh({Resource.WATER: 2000})
    for rid, site, qty, sev, ppl in [("W1", "S1", 1200, 5, 900), ("W2", "S4", 400, 4, 300),
                                     ("W3", "S6", 300, 3, 200), ("W4", "S8", 200, 2, 100)]:
        s.add_request(req(rid, site, Resource.WATER, qty, sev, ppl))
    plan = run_round(s, NOW)
    got = {r.request.request_id: r for r in plan.rows}
    assert [got[k].granted for k in ("W1", "W2", "W3", "W4")] == [1200, 400, 300, 100]
    assert got["W4"].status == RequestStatus.PARTIAL and got["W1"].status == RequestStatus.FULFILLED
    assert sum(r.granted for r in plan.rows) == 2000 and s.get_stock("water") == 0
    assert all(r.granted >= r.floor for r in plan.rows)

    # ---- no stock: UNMET "no stock" ----
    s = fresh({})
    s.add_request(req("N1", "S1", Resource.BLANKETS, 100, 3, 50))
    plan = run_round(s, NOW)
    assert plan.rows[0].status == RequestStatus.UNMET and plan.rows[0].reason == "no stock"

    # ---- stock too small for every floor: strict priority, nothing wasted ----
    s = fresh({Resource.FOOD_PACKS: 10})
    for rid, site, sev in [("F1", "S1", 5), ("F2", "S2", 4), ("F3", "S3", 3)]:
        s.add_request(req(rid, site, Resource.FOOD_PACKS, 100, sev, 100))
    plan = run_round(s, NOW)
    got = {r.request.request_id: r for r in plan.rows}
    assert (got["F1"].granted, got["F2"].granted, got["F3"].granted) == (10, 0, 0)
    assert got["F2"].reason == "stock went to higher-ranked requests"
    assert "too low" in got["F1"].reason and s.get_stock("food_packs") == 0

    # ---- everything fits: all FULFILLED, leftover stock stays ----
    s = fresh({Resource.WATER: 1000})
    s.add_request(req("E1", "S1", Resource.WATER, 300, 3, 100))
    plan = run_round(s, NOW)
    assert plan.rows[0].status == RequestStatus.FULFILLED and s.get_stock("water") == 700

    # ---- ledger + deliveries + trace ----
    allocs = s.all_allocations()
    assert len(allocs) == 1 and allocs[0].allocated_qty == 300
    d = s.get_delivery(allocs[0].delivery_id)
    assert d.status.value == "ALLOCATED" and d.sent_qty == 300
    assert any(e.event_type == EventType.ALLOCATED for e in s.trace(d.delivery_id))

    # ---- prior allocation feeds the next round's penalty ----
    s.add_request(req("E2", "S1", Resource.WATER, 100, 3, 100))
    s.add_request(req("E3", "S5", Resource.WATER, 100, 3, 100))
    plan2 = plan_round(s, NOW)
    rank = {r.request.request_id: r for r in plan2.rows}
    assert rank["E2"].score.prior_allocation_penalty == config.PRIOR_PENALTY_MAX
    assert rank["E3"].score.prior_allocation_penalty == 0.0
    assert rank["E3"].score.rank < rank["E2"].score.rank        # S5 (never served) outranks S1

    # ---- atomicity: failure halfway leaves stock, requests and round untouched ----
    s = fresh({Resource.WATER: 500})
    s.add_request(req("A1", "S1", Resource.WATER, 200, 5, 100))
    s.add_request(req("A2", "S2", Resource.WATER, 200, 4, 100))
    plan = plan_round(s, NOW)
    calls = {"n": 0}
    real_add = s.add_delivery

    def flaky(delivery):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        real_add(delivery)

    setattr(s, "add_delivery", flaky)       # setattr: editors flag direct method assignment
    try:
        commit_round(s, plan)
        raise AssertionError("should have failed")
    except RuntimeError:
        pass
    setattr(s, "add_delivery", real_add)
    assert s.get_stock("water") == 500 and s.current_round() == 1
    assert len(s.open_requests()) == 2 and len(s.all_allocations()) == 0
    commit_round(s, plan_round(s, NOW))                          # and a clean retry works
    assert s.get_stock("water") == 100

    # ---- stale plan is refused ----
    s = fresh({Resource.WATER: 500})
    s.add_request(req("S1X", "S1", Resource.WATER, 200, 5, 100))
    stale = plan_round(s, NOW)
    s.set_stock(Resource.WATER, 50)
    try:
        commit_round(s, stale)
        raise AssertionError("stale plan accepted")
    except StaleRoundError:
        pass

    print("allocator.py OK")