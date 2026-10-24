"""Route and nearest-depot calculations for ReliefStock AI."""

from dataclasses import dataclass
from math import atan2, cos, radians, sin, sqrt
import os

import requests

from config import (
    AVG_SPEED_KMH,
    HTTP_TIMEOUT_S,
    OSRM_BASE_URL,
    ROAD_FACTOR,
)
from registry import depots_holding


@dataclass
class RoutePlan:
    depot_id: str
    distance_km: float
    eta_minutes: int
    route: str
    source: str

    def to_dict(self):
        return {
            "depot_id": self.depot_id,
            "distance_km": self.distance_km,
            "eta_minutes": self.eta_minutes,
            "route": self.route,
            "source": self.source,
        }


def haversine_km(lat1, lng1, lat2, lng2):
    """Calculate straight-line distance between two coordinates."""
    earth_radius_km = 6371.0

    lat1, lng1, lat2, lng2 = map(
        radians, (lat1, lng1, lat2, lng2)
    )

    dlat = lat2 - lat1
    dlng = lng2 - lng1

    a = (
        sin(dlat / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    )

    return earth_radius_km * 2 * atan2(
        sqrt(a), sqrt(max(0.0, 1.0 - a))
    )


def get_route(depot, site):
    """
    Get road distance and ETA from OSRM.
    Fall back to an estimate if the routing service is unavailable.
    Coordinates must be longitude,latitude in the OSRM URL.
    """
    straight_distance = haversine_km(
        depot.lat, depot.lng, site.lat, site.lng
    )

    fallback_distance = straight_distance * ROAD_FACTOR
    fallback_eta = (
        fallback_distance / max(AVG_SPEED_KMH, 1)
    ) * 60

    fallback = RoutePlan(
        depot_id=depot.depot_id,
        distance_km=round(fallback_distance, 2),
        eta_minutes=max(1, round(fallback_eta)),
        route="Estimated route; road routing unavailable",
        source="straight-line estimate",
    )

    base_url = os.getenv("OSRM_BASE_URL", OSRM_BASE_URL).rstrip("/")
    url = (
        f"{base_url}/route/v1/driving/"
        f"{depot.lng},{depot.lat};{site.lng},{site.lat}"
    )

    try:
        response = requests.get(
            url,
            params={"overview": "false"},
            timeout=HTTP_TIMEOUT_S,
        )
        response.raise_for_status()
        data = response.json()

        routes = data.get("routes", [])
        if not routes:
            return fallback

        route_data = routes[0]
        return RoutePlan(
            depot_id=depot.depot_id,
            distance_km=round(route_data["distance"] / 1000, 2),
            eta_minutes=max(
                1, round(route_data["duration"] / 60)
            ),
            route="OSRM road route",
            source="OSRM",
        )

    except (requests.RequestException, ValueError, KeyError, TypeError):
        return fallback


def nearest_depot(site, resource_value):
    """
    Return the closest eligible depot and its route plan.
    Resource eligibility comes from registry.py.
    """
    depots = depots_holding(resource_value)

    if not depots:
        return None

    candidates = [get_route(depot, site) for depot in depots]
    return min(candidates, key=lambda item: item.distance_km)


def route_for_depot(depot_id, site, resource_value):
    """Calculate a route to a specific eligible depot."""
    for depot in depots_holding(resource_value):
        if depot.depot_id == depot_id:
            return get_route(depot, site)

    raise ValueError(
        f"Depot {depot_id} does not hold resource {resource_value}"
    )
