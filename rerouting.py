"""Alternative depot selection when a delivery is delayed."""

from registry import depots_holding
from routing import get_route
from models import DeliveryStatus, can_transition


def find_alternative_route(delivery, site, exclude_depot_id=None):
    """Find the nearest eligible depot other than the current depot."""
    resource_value = getattr(
        delivery.resource, "value", delivery.resource
    )

    current_depot = exclude_depot_id or delivery.depot_id
    candidates = [
        get_route(depot, site)
        for depot in depots_holding(resource_value)
        if depot.depot_id != current_depot
    ]

    if not candidates:
        return None

    return min(candidates, key=lambda route: route.distance_km)


def apply_reroute(delivery, route_plan):
    """Apply a proposed alternative route to an existing delivery."""
    if route_plan is None:
        raise ValueError("No alternative route is available")

    current = delivery.status
    if not isinstance(current, DeliveryStatus):
        current = DeliveryStatus(current)

    if not can_transition(current, DeliveryStatus.REROUTED):
        raise ValueError(
            f"Cannot reroute delivery from status {current.value}"
        )

    delivery.depot_id = route_plan.depot_id
    delivery.distance_km = route_plan.distance_km
    delivery.eta_minutes = route_plan.eta_minutes
    delivery.route = route_plan.route
    delivery.status = DeliveryStatus.REROUTED

    return delivery