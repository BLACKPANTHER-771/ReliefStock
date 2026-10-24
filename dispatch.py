"""Dispatch planning for existing ReliefStock allocations."""

from dataclasses import dataclass

from registry import lookup_site
from routing import nearest_depot


@dataclass
class DispatchPlan:
    request_id: str
    site_id: str
    resource: str
    quantity: int
    depot_id: str
    distance_km: float
    eta_minutes: int
    route: str

    def to_dict(self):
        return {
            "request_id": self.request_id,
            "site_id": self.site_id,
            "resource": self.resource,
            "quantity": self.quantity,
            "depot_id": self.depot_id,
            "distance_km": self.distance_km,
            "eta_minutes": self.eta_minutes,
            "route": self.route,
        }


def plan_dispatch(allocation):
    """
    Build a dispatch plan for a committed allocation.

    Accepts an Allocation model instance.
    Returns DispatchPlan, or None if no eligible depot is available.
    """
    if allocation.allocated_qty <= 0:
        return None

    site = lookup_site(allocation.site_id)
    if site is None:
        raise ValueError(
            f"Unknown site: {allocation.site_id}"
        )

    resource_value = getattr(
        allocation.resource, "value", allocation.resource
    )

    route_plan = nearest_depot(site, resource_value)
    if route_plan is None:
        return None

    return DispatchPlan(
        request_id=allocation.request_id,
        site_id=allocation.site_id,
        resource=resource_value,
        quantity=allocation.allocated_qty,
        depot_id=route_plan.depot_id,
        distance_km=route_plan.distance_km,
        eta_minutes=route_plan.eta_minutes,
        route=route_plan.route,
    )


def apply_dispatch_plan(allocation, delivery, plan):
    """
    Copy dispatch details to existing Allocation and Delivery objects.
    Persist them using Store in the calling layer.
    """
    if plan is None:
        raise ValueError("Cannot dispatch without a valid plan")

    if plan.quantity != allocation.allocated_qty:
        raise ValueError("Dispatch quantity differs from allocation")

    allocation.depot_id = plan.depot_id
    allocation.distance_km = plan.distance_km
    allocation.eta_minutes = plan.eta_minutes
    allocation.route = plan.route

    delivery.depot_id = plan.depot_id
    delivery.distance_km = plan.distance_km
    delivery.eta_minutes = plan.eta_minutes
    delivery.route = plan.route

    return allocation, delivery