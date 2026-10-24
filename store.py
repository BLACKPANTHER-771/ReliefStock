"""
store.py - ReliefStock AI: all persistent state, the ledger and the event log.

One JSON-serialisable state dict, saved through a tiny backend interface:
    backend.get(key) -> value or None        backend.set(key, value)
Backends:
    DictBackend      in-memory (tests)
    JsonFileBackend  a local .json file (running in VS Code)
    CtxBackend       wraps Agentverse `ctx.storage` (hosted agent; VERIFY on first
                     run: it assumes ctx.storage.get(key) / ctx.storage.set(key, value))

Hosted agents reset Python globals on every invocation, so the Store must be
rebuilt from the backend at the start of each message: Store(CtxBackend(ctx.storage)).

ATOMICITY: `with store.transaction():` snapshots the state; if anything inside
raises, the state is restored and nothing is saved. Outside a transaction every
mutating call saves immediately.

The event log is append-only: events are only ever added, never edited.
No `from __future__ import annotations` (breaks dataclasses on Agentverse).
"""

import copy
import json
import os
from contextlib import contextmanager

from models import (
    Allocation, Delivery, Event, EventType, Resource, ResourceRequest, Site,
)

STATE_KEY = "reliefstock_state_v1"


class StoreError(Exception):
    """Raised for invalid stock changes, corrupted state, and similar."""


# ------------------------------------------------------------------ backends
class DictBackend(object):
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value


class JsonFileBackend(object):
    def __init__(self, path):
        self.path = path

    def _read(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r") as f:
            return json.load(f)

    def get(self, key):
        return self._read().get(key)

    def set(self, key, value):
        data = self._read()
        data[key] = value
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)          # atomic swap: never a half-written file


class CtxBackend(object):
    """Wraps Agentverse ctx.storage. Check the official docs / smoke test first."""

    def __init__(self, storage):
        self.storage = storage

    def get(self, key):
        return self.storage.get(key)

    def set(self, key, value):
        self.storage.set(key, value)


# --------------------------------------------------------------------- state
def fresh_state():
    return {
        "version": 1,
        "round_no": 1,
        "inventory": {r.value: 0 for r in Resource},
        "requests": {},
        "allocations": {},
        "deliveries": {},
        "events": [],
        "extra_sites": {},
        "confirmations": {},
    }


class Store(object):
    def __init__(self, backend):
        self.backend = backend
        self._depth = 0
        self.state = self._load()

    # ---- persistence ----
    def _load(self):
        raw = self.backend.get(STATE_KEY)
        if raw in (None, ""):
            return fresh_state()
        try:
            state = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            # Never silently wipe real data: stop and tell the operator.
            raise StoreError("Stored state is corrupted and could not be read.")
        if not isinstance(state, dict):
            raise StoreError("Stored state has an unexpected format.")
        merged = fresh_state()
        merged.update(state)
        return merged

    def _save(self):
        self.backend.set(STATE_KEY, json.dumps(self.state, separators=(",", ":")))

    def _commit(self):
        if self._depth == 0:
            self._save()

    @contextmanager
    def transaction(self):
        """All-or-nothing block. Nested use joins the outermost transaction."""
        snapshot = copy.deepcopy(self.state) if self._depth == 0 else None
        self._depth += 1
        try:
            yield self
        except BaseException:
            self._depth -= 1
            if snapshot is not None:
                self.state = snapshot
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                try:
                    self._save()
                except BaseException:
                    self.state = snapshot
                    raise

    def reset(self):
        self.state = fresh_state()
        self._commit()

    # ---- round counter ----
    def current_round(self):
        return int(self.state["round_no"])

    def advance_round(self):
        self.state["round_no"] = self.current_round() + 1
        self._commit()
        return self.current_round()

    # ---- inventory ----
    def get_stock(self, resource_value):
        return int(self.state["inventory"].get(resource_value, 0))

    def get_inventory(self):
        return dict(self.state["inventory"])

    def set_stock(self, resource, quantity, source="coordinator"):
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise StoreError("Stock must be a whole number, 0 or more.")
        before = self.get_stock(resource.value)
        self.state["inventory"][resource.value] = quantity
        self.log(EventType.STOCK_UPDATED,
                 "%s stock set from %d to %d (%s)" % (resource.value, before, quantity, source),
                 data={"resource": resource.value, "before": before, "after": quantity, "source": source})
        return quantity

    def adjust_stock(self, resource, delta, reason):
        before = self.get_stock(resource.value)
        after = before + int(delta)
        if after < 0:
            raise StoreError("Stock of %s cannot go below zero (have %d, change %d)."
                             % (resource.value, before, delta))
        self.state["inventory"][resource.value] = after
        self.log(EventType.STOCK_UPDATED,
                 "%s stock %d -> %d (%s)" % (resource.value, before, after, reason),
                 data={"resource": resource.value, "before": before, "after": after, "reason": reason})
        return after

    # ---- sites added at runtime ----
    def add_site(self, site):
        self.state["extra_sites"][site.site_id] = site.to_dict()
        self._commit()

    def extra_sites(self):
        return {sid: Site.from_dict(d) for sid, d in self.state["extra_sites"].items()}

    # ---- one-shot chat confirmations ----
    def get_confirmation(self, sender):
        return self.state["confirmations"].get(sender)

    def set_confirmation(self, sender, action):
        self.state["confirmations"][sender] = dict(action)
        self._commit()

    def clear_confirmation(self, sender):
        self.state["confirmations"].pop(sender, None)
        self._commit()

    # ---- requests ----
    def add_request(self, req):
        req.round_no = self.current_round()
        self.state["requests"][req.request_id] = req.to_dict()
        self.log(EventType.REQUEST_RECEIVED,
                 "Request %s: %s x %d from %s (severity %d, %d people)"
                 % (req.request_id, req.resource.value, req.quantity, req.site_id,
                    req.severity, req.people_affected),
                 request_id=req.request_id, site_id=req.site_id,
                 data={"quantity": req.quantity, "severity": req.severity})
        return req

    def get_request(self, request_id):
        d = self.state["requests"].get(request_id)
        return ResourceRequest.from_dict(d) if d else None

    def update_request(self, req):
        if req.request_id not in self.state["requests"]:
            raise StoreError("Unknown request %s." % req.request_id)
        self.state["requests"][req.request_id] = req.to_dict()
        self._commit()

    def all_requests(self):
        return [ResourceRequest.from_dict(d) for d in self.state["requests"].values()]

    def open_requests(self):
        return [r for r in self.all_requests() if r.status.value == "PENDING"]

    def log_rejection(self, summary, errors, site_id=""):
        return self.log(EventType.REQUEST_REJECTED,
                        "Rejected: %s | %s" % (summary, " ".join(errors)),
                        site_id=site_id, data={"errors": list(errors)})

    # ---- ledger: allocations and deliveries ----
    def add_allocation(self, alloc):
        self.state["allocations"][alloc.allocation_id] = alloc.to_dict()
        self._commit()

    def get_allocation(self, allocation_id):
        d = self.state["allocations"].get(allocation_id)
        return Allocation.from_dict(d) if d else None

    def all_allocations(self):
        return [Allocation.from_dict(d) for d in self.state["allocations"].values()]

    def add_delivery(self, delivery):
        self.state["deliveries"][delivery.delivery_id] = delivery.to_dict()
        self._commit()

    def get_delivery(self, delivery_id):
        d = self.state["deliveries"].get(delivery_id)
        return Delivery.from_dict(d) if d else None

    def update_delivery(self, delivery):
        if delivery.delivery_id not in self.state["deliveries"]:
            raise StoreError("Unknown delivery %s." % delivery.delivery_id)
        self.state["deliveries"][delivery.delivery_id] = delivery.to_dict()
        self._commit()

    def all_deliveries(self):
        return [Delivery.from_dict(d) for d in self.state["deliveries"].values()]

    def deliveries_for_site(self, site_id):
        return [d for d in self.all_deliveries() if d.site_id == site_id]

    # ---- event log (append-only) ----
    def log(self, event_type, message, request_id="", allocation_id="",
            delivery_id="", site_id="", data=None):
        ev = Event(event_type=event_type, message=message, request_id=request_id,
                   allocation_id=allocation_id, delivery_id=delivery_id,
                   site_id=site_id, data=dict(data or {}))
        self.state["events"].append(ev.to_dict())
        self._commit()
        return ev

    def events(self, limit=None, event_type=None):
        items = self.state["events"]
        if event_type is not None:
            items = [e for e in items if e["event_type"] == event_type.value]
        if limit:
            items = items[-limit:]
        return [Event.from_dict(e) for e in items]

    def trace(self, delivery_id):
        """Every event behind a delivery: its request, allocation and delivery."""
        d = self.get_delivery(delivery_id)
        if d is None:
            return []
        alloc = self.get_allocation(d.allocation_id)
        request_id = alloc.request_id if alloc else ""
        out = []
        for e in self.state["events"]:
            if (e["delivery_id"] == delivery_id
                    or (e["allocation_id"] and e["allocation_id"] == d.allocation_id)
                    or (request_id and e["request_id"] == request_id)):
                out.append(Event.from_dict(e))
        return out

    # ---- history used by the priority score ----
    def prior_allocation(self):
        """(site_id, resource) -> (allocated_total, requested_total) from EARLIER rounds."""
        cur = self.current_round()
        prior = {}
        for d in self.state["requests"].values():
            if d["round_no"] < cur and d["status"] != "PENDING":
                key = (d["site_id"], d["resource"])
                a, q = prior.get(key, (0, 0))
                prior[key] = (a + d["allocated_qty"], q + d["quantity"])
        return prior


if __name__ == "__main__":
    from models import RequestStatus

    be = DictBackend()
    s = Store(be)
    assert s.current_round() == 1 and s.get_stock("water") == 0

    # stock + request persist across a "new invocation" (hosted agents reset globals)
    s.set_stock(Resource.WATER, 2000)
    req = ResourceRequest(site_id="S1", resource=Resource.WATER, quantity=1200,
                          people_affected=900, severity=5)
    s.add_request(req)
    s2 = Store(be)
    assert s2.get_stock("water") == 2000 and s2.get_request(req.request_id).quantity == 1200
    assert len(s2.open_requests()) == 1

    # bad stock values are refused
    for bad in (-1, 1.5, True, "10"):
        try:
            s.set_stock(Resource.WATER, bad)
            raise AssertionError("accepted %r" % (bad,))
        except StoreError:
            pass

    # a failed transaction leaves NOTHING behind
    try:
        with s.transaction():
            s.adjust_stock(Resource.WATER, -500, "test")
            r = s.get_request(req.request_id)
            r.status = RequestStatus.FULFILLED
            s.update_request(r)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert s.get_stock("water") == 2000
    assert s.get_request(req.request_id).status == RequestStatus.PENDING
    assert Store(be).get_stock("water") == 2000          # and nothing was saved

    # stock can never go negative
    try:
        s.adjust_stock(Resource.WATER, -5000, "too much")
        raise AssertionError("went negative")
    except StoreError:
        pass

    # trace links delivery -> allocation -> request
    alloc = Allocation(allocation_id="ALC-1", request_id=req.request_id, site_id="S1",
                       resource=Resource.WATER, requested_qty=1200, allocated_qty=800)
    s.add_allocation(alloc)
    s.add_delivery(Delivery(delivery_id=alloc.delivery_id, allocation_id="ALC-1", site_id="S1",
                            resource=Resource.WATER, sent_qty=800))
    s.log(EventType.ALLOCATED, "allocated 800", request_id=req.request_id,
          allocation_id="ALC-1", delivery_id=alloc.delivery_id, site_id="S1")
    kinds = [e.event_type.value for e in s.trace(alloc.delivery_id)]
    assert "REQUEST_RECEIVED" in kinds and "ALLOCATED" in kinds, kinds

    # prior allocation only counts EARLIER rounds
    r = s.get_request(req.request_id)
    r.status, r.allocated_qty = RequestStatus.PARTIAL, 800
    s.update_request(r)
    assert s.prior_allocation() == {}                    # same round: not "prior" yet
    s.advance_round()
    assert s.prior_allocation() == {("S1", "water"): (800, 1200)}

    # corrupted data is reported, never silently wiped
    bad_be = DictBackend()
    bad_be.set(STATE_KEY, "{not json")
    try:
        Store(bad_be)
        raise AssertionError("corruption ignored")
    except StoreError:
        pass

    print("store.py OK ->", len(s.state["events"]), "events logged")