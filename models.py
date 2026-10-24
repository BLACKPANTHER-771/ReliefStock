
"""
models.py - ReliefStock AI shared data models.

Keep this module free of I/O and external dependencies.
Do not use `from __future__ import annotations`.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4


# ------------------------------------------------------------------
# Helpers


def new_id(prefix):
    return f"{prefix}-{uuid4().hex[:12].upper()}"


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------
# Enums


class Resource(str, Enum):
    WATER = "water"
    MEDICAL_KITS = "medical_kits"
    BLANKETS = "blankets"
    FOOD_PACKS = "food_packs"

    @property
    def unit(self):
        return {
            Resource.WATER: "litres",
            Resource.MEDICAL_KITS: "kits",
            Resource.BLANKETS: "blankets",
            Resource.FOOD_PACKS: "packs",
        }[self]


class VulnerableGroup(str, Enum):
    CHILDREN = "children"
    ELDERLY = "elderly"
    PREGNANT = "pregnant"
    DISABLED = "disabled"
    INJURED = "injured"


class RequestStatus(str, Enum):
    PENDING = "PENDING"
    FULFILLED = "FULFILLED"
    PARTIAL = "PARTIAL"
    UNMET = "UNMET"


class UnmetReason(str, Enum):
    NO_STOCK = "no stock"
    HIGHER_RANKED = "stock went to higher-ranked requests"
    NONE = ""


class DeliveryStatus(str, Enum):
    ALLOCATED = "ALLOCATED"
    DISPATCHED = "DISPATCHED"
    IN_TRANSIT = "IN_TRANSIT"
    DELAYED = "DELAYED"
    REROUTED = "REROUTED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class EventType(str, Enum):
    REQUEST_RECEIVED = "REQUEST_RECEIVED"
    REQUEST_REJECTED = "REQUEST_REJECTED"
    PRIORITY_COMPUTED = "PRIORITY_COMPUTED"
    STOCK_UPDATED = "STOCK_UPDATED"
    ALLOCATED = "ALLOCATED"
    FULFILLED = "FULFILLED"
    DISPATCHED = "DISPATCHED"
    IN_TRANSIT = "IN_TRANSIT"
    DELAYED = "DELAYED"
    ROUTE_CHANGED = "ROUTE_CHANGED"
    DELIVERED = "DELIVERED"
    DELIVERY_FAILED = "DELIVERY_FAILED"
    DELIVERY_MISMATCH = "DELIVERY_MISMATCH"
    RECEIPT_MISMATCH = "RECEIPT_MISMATCH"
    FAILED = "FAILED"
    NOTIFICATION = "NOTIFICATION"
    ROUND_ADVANCED = "ROUND_ADVANCED"


# ------------------------------------------------------------------
# Delivery lifecycle


DELIVERY_TRANSITIONS = {
    DeliveryStatus.ALLOCATED: {
        DeliveryStatus.DISPATCHED,
        DeliveryStatus.FAILED,
    },
    DeliveryStatus.DISPATCHED: {
        DeliveryStatus.IN_TRANSIT,
        DeliveryStatus.DELAYED,
        DeliveryStatus.FAILED,
    },
    DeliveryStatus.IN_TRANSIT: {
        DeliveryStatus.DELAYED,
        DeliveryStatus.REROUTED,
        DeliveryStatus.DELIVERED,
        DeliveryStatus.FAILED,
    },
    DeliveryStatus.DELAYED: {
        DeliveryStatus.IN_TRANSIT,
        DeliveryStatus.REROUTED,
        DeliveryStatus.DELIVERED,
        DeliveryStatus.FAILED,
    },
    DeliveryStatus.REROUTED: {
        DeliveryStatus.IN_TRANSIT,
        DeliveryStatus.DELAYED,
        DeliveryStatus.DELIVERED,
        DeliveryStatus.FAILED,
    },
    DeliveryStatus.DELIVERED: set(),
    DeliveryStatus.FAILED: set(),
}


def can_transition(current, target):
    try:
        current = DeliveryStatus(current)
        target = DeliveryStatus(target)
    except (ValueError, TypeError):
        return False

    return target in DELIVERY_TRANSITIONS.get(current, set())


# ------------------------------------------------------------------
# Validation result


@dataclass
class ValidationResult:
    ok: bool
    request: object = None
    errors: list = field(default_factory=list)


# ------------------------------------------------------------------
# Site


@dataclass
class Site:
    site_id: str
    name: str
    lat: float
    lng: float
    contact_name: str = ""
    contact_phone: str = ""
    user_supplied_location: bool = False

    def to_dict(self):
        return {
            "site_id": self.site_id,
            "name": self.name,
            "lat": self.lat,
            "lng": self.lng,
            "contact_name": self.contact_name,
            "contact_phone": self.contact_phone,
            "user_supplied_location": self.user_supplied_location,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


# ------------------------------------------------------------------
# Depot


@dataclass
class Depot:
    depot_id: str
    name: str
    lat: float
    lng: float
    contact_name: str = ""
    contact_phone: str = ""
    resources: list = field(default_factory=list)

    def to_dict(self):
        return {
            "depot_id": self.depot_id,
            "name": self.name,
            "lat": self.lat,
            "lng": self.lng,
            "contact_name": self.contact_name,
            "contact_phone": self.contact_phone,
            "resources": list(self.resources),
        }

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


# ------------------------------------------------------------------
# Resource request


@dataclass
class ResourceRequest:
    site_id: str
    resource: Resource
    quantity: int
    people_affected: int
    severity: int
    vulnerable_groups: list = field(default_factory=list)
    request_id: str = field(default_factory=lambda: new_id("REQ"))
    created_at: str = field(default_factory=_now_iso)
    status: RequestStatus = RequestStatus.PENDING
    allocated_qty: int = 0
    round_no: int = 1
    unmet_reason: str = ""

    def __post_init__(self):
        self.resource = Resource(self.resource)
        self.status = RequestStatus(self.status)
        self.vulnerable_groups = [
            VulnerableGroup(group) for group in self.vulnerable_groups
        ]

    def to_dict(self):
        return {
            "request_id": self.request_id,
            "site_id": self.site_id,
            "resource": self.resource.value,
            "quantity": self.quantity,
            "people_affected": self.people_affected,
            "severity": self.severity,
            "vulnerable_groups": [
                group.value for group in self.vulnerable_groups
            ],
            "created_at": self.created_at,
            "status": self.status.value,
            "allocated_qty": self.allocated_qty,
            "round_no": self.round_no,
            "unmet_reason": self.unmet_reason,
        }

    @classmethod
    def from_dict(cls, data):
        values = dict(data)
        values["resource"] = Resource(values["resource"])
        values["status"] = RequestStatus(
            values.get("status", RequestStatus.PENDING.value)
        )
        values["vulnerable_groups"] = [
            VulnerableGroup(group)
            for group in values.get("vulnerable_groups", [])
        ]
        return cls(**values)


# ------------------------------------------------------------------
# Priority score


@dataclass
class PriorityScore:
    request_id: str
    severity_pts: float = 0.0
    vulnerability_pts: float = 0.0
    people_pts: float = 0.0
    wait_pts: float = 0.0
    prior_allocation_penalty: float = 0.0
    total: float = 0.0
    rank: int = 0

    def explain(self):
        return (
            f"Request {self.request_id} | "
            f"rank {self.rank if self.rank else 'unranked'} | "
            f"total {self.total:.2f} "
            f"(severity +{self.severity_pts:.2f}, "
            f"vulnerability +{self.vulnerability_pts:.2f}, "
            f"people +{self.people_pts:.2f}, "
            f"waiting +{self.wait_pts:.2f}, "
            f"prior-allocation penalty "
            f"-{self.prior_allocation_penalty:.2f})"
        )

    def to_dict(self):
        return {
            "request_id": self.request_id,
            "severity_pts": self.severity_pts,
            "vulnerability_pts": self.vulnerability_pts,
            "people_pts": self.people_pts,
            "wait_pts": self.wait_pts,
            "prior_allocation_penalty": self.prior_allocation_penalty,
            "total": self.total,
            "rank": self.rank,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


# ------------------------------------------------------------------
# Allocation


@dataclass
class Allocation:
    allocation_id: str
    request_id: str
    site_id: str
    resource: Resource
    requested_qty: int
    allocated_qty: int
    round_no: int = 1
    depot_id: str = ""
    distance_km: float = 0.0
    eta_minutes: float = 0.0
    route: str = ""
    delivery_id: str = field(default_factory=lambda: new_id("DEL"))

    def __post_init__(self):
        self.resource = Resource(self.resource)

    def to_dict(self):
        return {
            "allocation_id": self.allocation_id,
            "request_id": self.request_id,
            "site_id": self.site_id,
            "resource": self.resource.value,
            "requested_qty": self.requested_qty,
            "allocated_qty": self.allocated_qty,
            "round_no": self.round_no,
            "depot_id": self.depot_id,
            "distance_km": self.distance_km,
            "eta_minutes": self.eta_minutes,
            "route": self.route,
            "delivery_id": self.delivery_id,
        }

    @classmethod
    def from_dict(cls, data):
        values = dict(data)
        values["resource"] = Resource(values["resource"])
        return cls(**values)


# ------------------------------------------------------------------
# Delivery


@dataclass
class Delivery:
    delivery_id: str
    allocation_id: str
    site_id: str
    resource: Resource
    sent_qty: int
    depot_id: str = ""
    status: DeliveryStatus = DeliveryStatus.ALLOCATED
    received_qty: int = 0
    confirmed_by: str = ""
    confirmed_at: str = ""
    route: str = ""
    distance_km: float = 0.0
    eta_minutes: float = 0.0
    failure_reason: str = ""
    delay_reason: str = ""

    def __post_init__(self):
        self.resource = Resource(self.resource)
        self.status = DeliveryStatus(self.status)

    def to_dict(self):
        return {
            "delivery_id": self.delivery_id,
            "allocation_id": self.allocation_id,
            "site_id": self.site_id,
            "resource": self.resource.value,
            "sent_qty": self.sent_qty,
            "depot_id": self.depot_id,
            "status": self.status.value,
            "received_qty": self.received_qty,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
            "route": self.route,
            "distance_km": self.distance_km,
            "eta_minutes": self.eta_minutes,
            "failure_reason": self.failure_reason,
            "delay_reason": self.delay_reason,
        }

    @classmethod
    def from_dict(cls, data):
        values = dict(data)
        values["resource"] = Resource(values["resource"])
        values["status"] = DeliveryStatus(
            values.get("status", DeliveryStatus.ALLOCATED.value)
        )
        return cls(**values)


# ------------------------------------------------------------------
# Append-only event


@dataclass
class Event:
    event_type: EventType
    message: str
    request_id: str = ""
    allocation_id: str = ""
    delivery_id: str = ""
    site_id: str = ""
    data: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=_now_iso)
    event_id: str = field(default_factory=lambda: new_id("EVT"))

    def __post_init__(self):
        self.event_type = EventType(self.event_type)

    def to_dict(self):
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "message": self.message,
            "request_id": self.request_id,
            "allocation_id": self.allocation_id,
            "delivery_id": self.delivery_id,
            "site_id": self.site_id,
            "data": dict(self.data),
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data):
        values = dict(data)
        values["event_type"] = EventType(values["event_type"])
        return cls(**values)


# ------------------------------------------------------------------
# Smoke tests


if __name__ == "__main__":
    request = ResourceRequest(
        site_id="S1",
        resource=Resource.WATER,
        quantity=100,
        people_affected=50,
        severity=3,
        vulnerable_groups=[VulnerableGroup.CHILDREN],
    )
    restored_request = ResourceRequest.from_dict(request.to_dict())
    assert restored_request.resource == Resource.WATER
    assert restored_request.vulnerable_groups == [
        VulnerableGroup.CHILDREN
    ]

    allocation = Allocation(
        allocation_id="ALC-TEST",
        request_id=request.request_id,
        site_id="S1",
        resource=Resource.WATER,
        requested_qty=100,
        allocated_qty=80,
    )
    assert Allocation.from_dict(
        allocation.to_dict()
    ).delivery_id == allocation.delivery_id

    delivery = Delivery(
        delivery_id=allocation.delivery_id,
        allocation_id=allocation.allocation_id,
        site_id="S1",
        resource=Resource.WATER,
        sent_qty=80,
    )
    assert Delivery.from_dict(
        delivery.to_dict()
    ).status == DeliveryStatus.ALLOCATED

    event = Event(
        event_type=EventType.ALLOCATED,
        message="Test allocation",
        request_id=request.request_id,
        allocation_id=allocation.allocation_id,
        delivery_id=delivery.delivery_id,
    )
    assert Event.from_dict(
        event.to_dict()
    ).event_type == EventType.ALLOCATED

    assert Resource.WATER.unit == "litres"
    assert can_transition(
        DeliveryStatus.ALLOCATED, DeliveryStatus.DISPATCHED
    )
    assert not can_transition(
        DeliveryStatus.DELIVERED, DeliveryStatus.IN_TRANSIT
    )

    result = ValidationResult(False, None, ["Test error"])
    assert not result.ok and result.errors == ["Test error"]

    print("models.py OK")
