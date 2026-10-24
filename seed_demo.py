"""
seed_demo.py - ReliefStock AI: the demo scenario (ALL DATA IS FICTIONAL).

9 sites (registry.py), 10 current requests, one earlier round of history.

  WATER    stock 2000, demand 2100 (request #1 asks 1200)
             -> three requests fully met, the lowest-ranked gets 100 of 200
  MEDICAL  stock 150: hospital (#5) asks 150, hamlet (#6) asks 60
             -> strict priority-first would give the hamlet 0; the 30% minimum
                share gives it 18 (hospital gets 132)
  BLANKETS stock 500, demand 580 (#7 200, #8 180, #9 120, #10 80)
             -> request #10 is from the Primary School Shelter, which already
                received all 80 blankets it asked for in an EARLIER round, so
                it carries the prior-allocation penalty and drops below #9
  FOOD     stock 300, no requests (shows untouched stock)

Every current request goes through validate_request(), so the demo exercises
the real Phase 1 path. Loading the demo WIPES the store.
No `from __future__ import annotations` (breaks dataclasses on Agentverse).
"""

from datetime import datetime, timedelta, timezone

from models import (
    Allocation, Delivery, DeliveryStatus, EventType, RequestStatus,
    Resource, ResourceRequest,
)
from validation import validate_request

DEMO_STOCK = {
    Resource.WATER: 2000,
    Resource.MEDICAL_KITS: 150,
    Resource.BLANKETS: 500,
    Resource.FOOD_PACKS: 300,
}

# (number, hours already waiting, raw request as a coordinator would report it)
DEMO_REQUESTS = [
    (1, 4, {"site": "S1", "resource": "water", "quantity": 1200, "people_affected": 900,
            "severity": 5, "vulnerable_groups": ["children", "elderly"]}),
    (2, 3, {"site": "S4", "resource": "water", "quantity": 400, "people_affected": 250,
            "severity": 4, "vulnerable_groups": ["children"]}),
    (3, 3, {"site": "S6", "resource": "water", "quantity": 300, "people_affected": 180,
            "severity": 3, "vulnerable_groups": []}),
    (4, 6, {"site": "S8", "resource": "water", "quantity": 200, "people_affected": 120,
            "severity": 2, "vulnerable_groups": []}),
    (5, 2, {"site": "S2", "resource": "medical_kits", "quantity": 150, "people_affected": 400,
            "severity": 5, "vulnerable_groups": ["injured", "elderly"]}),
    (6, 5, {"site": "S3", "resource": "medical_kits", "quantity": 60, "people_affected": 60,
            "severity": 3, "vulnerable_groups": ["children", "pregnant"]}),
    (7, 2, {"site": "S5", "resource": "blankets", "quantity": 200, "people_affected": 80,
            "severity": 4, "vulnerable_groups": ["elderly", "disabled"]}),
    (8, 1, {"site": "S7", "resource": "blankets", "quantity": 180, "people_affected": 140,
            "severity": 3, "vulnerable_groups": ["children"]}),
    (9, 1, {"site": "S9", "resource": "blankets", "quantity": 120, "people_affected": 200,
            "severity": 3, "vulnerable_groups": []}),
    (10, 1, {"site": "S4", "resource": "blankets", "quantity": 80, "people_affected": 300,
             "severity": 3, "vulnerable_groups": []}),
]


def _iso(moment):
    return moment.isoformat(timespec="seconds")


def _seed_history(store, now):
    """Round 1 (finished): the Primary School Shelter got all 80 blankets it asked for."""
    past = now - timedelta(days=1)
    old = ResourceRequest(site_id="S4", resource=Resource.BLANKETS, quantity=80,
                          people_affected=250, severity=3, created_at=_iso(past),
                          status=RequestStatus.FULFILLED, allocated_qty=80)
    store.add_request(old)                                    # lands in round 1
    alloc = Allocation(allocation_id="ALC-HISTORY1", request_id=old.request_id, site_id="S4",
                       resource=Resource.BLANKETS, requested_qty=80, allocated_qty=80, round_no=1)
    store.add_allocation(alloc)
    store.add_delivery(Delivery(
        delivery_id=alloc.delivery_id, allocation_id=alloc.allocation_id, site_id="S4",
        resource=Resource.BLANKETS, sent_qty=80, depot_id="D1", status=DeliveryStatus.DELIVERED,
        received_qty=80, confirmed_by="Pooja Kalita", confirmed_at=_iso(past + timedelta(hours=5))))
    store.log(EventType.ALLOCATED, "S4: 80 of 80 blankets allocated (FULFILLED) [demo history]",
              request_id=old.request_id, allocation_id=alloc.allocation_id,
              delivery_id=alloc.delivery_id, site_id="S4")
    store.log(EventType.DELIVERED, "S4 confirmed receipt of 80 blankets [demo history]",
              request_id=old.request_id, allocation_id=alloc.allocation_id,
              delivery_id=alloc.delivery_id, site_id="S4")
    store.advance_round()                                     # history is now "earlier"


def load_demo(store, now=None):
    """
    Wipe the store and load the scenario. Returns
    {"round_no", "stock", "requests": {number: request_id}}.
    All-or-nothing: a failure leaves the previous state untouched.
    """
    now = now or datetime.now(timezone.utc)
    numbers = {}
    with store.transaction():
        store.reset()
        for resource, qty in DEMO_STOCK.items():
            store.set_stock(resource, qty, source="demo seed")
        _seed_history(store, now)
        for number, hours_ago, raw in DEMO_REQUESTS:
            result, _ = validate_request(raw, open_requests=store.open_requests(),
                                         created_at=_iso(now - timedelta(hours=hours_ago)))
            if not result.ok:
                raise RuntimeError("Demo request #%d is invalid: %s" % (number, " ".join(result.errors)))
            store.add_request(result.request)
            numbers[number] = result.request.request_id
    return {"round_no": store.current_round(), "stock": store.get_inventory(), "requests": numbers}


def seed_demo_safe(store, now=None):
    """
    Safely seed the 10 demo requests and missing stock without wiping the store.
    Preserves all existing inventory and operational records.
    Prevents duplicates if demo requests already exist.
    """
    now = now or datetime.now(timezone.utc)
    numbers = {}
    with store.transaction():
        for resource, min_qty in DEMO_STOCK.items():
            current_qty = store.get_stock(resource.value)
            if current_qty < min_qty:
                store.set_stock(resource, min_qty, source="demo seed")

        if not store.prior_allocation():
            _seed_history(store, now)

        open_keys = {(r.site_id, r.resource.value) for r in store.open_requests()}
        for number, hours_ago, raw in DEMO_REQUESTS:
            res_val = raw["resource"]
            site_id = raw["site"]
            if (site_id, res_val) in open_keys:
                continue
            result, _ = validate_request(
                raw, open_requests=store.open_requests(),
                created_at=_iso(now - timedelta(hours=hours_ago)),
            )
            if result.ok:
                store.add_request(result.request)
                numbers[number] = result.request.request_id

    return {
        "round_no": store.current_round(),
        "stock": store.get_inventory(),
        "open_count": len(store.open_requests()),
        "added_count": len(numbers),
    }


if __name__ == "__main__":
    from allocator import plan_round, run_round
    from priority import rank_requests
    from store import DictBackend, Store

    NOW = datetime.now(timezone.utc)
    s = Store(DictBackend())
    info = load_demo(s, NOW)
    num = info["requests"]

    # --- the scenario is what the brief says it is ---
    assert info["round_no"] == 2 and len(s.open_requests()) == 10
    demand = {}
    for r in s.open_requests():
        demand[r.resource.value] = demand.get(r.resource.value, 0) + r.quantity
    assert demand == {"water": 2100, "medical_kits": 210, "blankets": 580}, demand
    assert info["stock"] == {"water": 2000, "medical_kits": 150, "blankets": 500, "food_packs": 300}

    # --- the prior-allocation penalty flips the blankets ranking ---
    r9, r10 = s.get_request(num[9]), s.get_request(num[10])
    without = {r.request_id: sc.rank for r, sc in rank_requests([r9, r10], None, NOW)}
    with_hist = {r.request_id: sc.rank for r, sc in rank_requests([r9, r10], s.prior_allocation(), NOW)}
    assert without[num[10]] < without[num[9]], "without history #10 should outrank #9"
    assert with_hist[num[9]] < with_hist[num[10]], "with history #9 should outrank #10"

    plan = plan_round(s, NOW)
    grants = {n: row.granted for row in plan.rows for n, rid in num.items() if rid == row.request.request_id}
    status = {n: row.status.value for row in plan.rows for n, rid in num.items() if rid == row.request.request_id}

    assert [grants[n] for n in (1, 2, 3, 4)] == [1200, 400, 300, 100], grants      # water
    assert status[4] == "PARTIAL" and status[1] == "FULFILLED"
    assert (grants[5], grants[6]) == (132, 18), grants                              # medical kits
    assert [grants[n] for n in (7, 8, 9, 10)] == [200, 180, 96, 24], grants        # blankets
    assert status[9] == "PARTIAL" and status[10] == "PARTIAL"

    run_round(s, NOW)
    assert s.get_inventory() == {"water": 0, "medical_kits": 0, "blankets": 0, "food_packs": 300}
    assert s.current_round() == 3 and len(s.open_requests()) == 0

    print("seed_demo.py OK")
    print("water   :", [grants[n] for n in (1, 2, 3, 4)], "of 2000 (demand 2100)")
    print("medical :", [grants[n] for n in (5, 6)], "of 150 -> hamlet gets", grants[6], "not 0")
    print("blankets:", [grants[n] for n in (7, 8, 9, 10)], "of 500 (demand 580)")
    print("#10 rank without history:", without[num[10]], "| with history:", with_hist[num[10]])