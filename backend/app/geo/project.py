"""Destination point on a sphere, and its inverse. Adequate for the distances spoken on a radio (< 20 km):
the spherical model differs from WGS84 by up to about 0.5% of the distance, well inside the error of a
spoken estimate."""
from __future__ import annotations

from math import asin, atan2, cos, degrees, isfinite, radians, sin, sqrt

R = 6_371_000.0  # metres; the same radius as routing.haversine_m, so distances agree across the backend

Point = tuple[float, float]  # (lat, lon)


def project(lat: float, lon: float, distance_m: float, bearing_deg: float) -> Point:
    """The point `distance_m` from (lat, lon) along `bearing_deg` (degrees true, clockwise from north)."""
    lat1, lon1, brg = radians(lat), radians(lon), radians(bearing_deg)
    d = distance_m / R
    lat2 = asin(sin(lat1) * cos(d) + cos(lat1) * sin(d) * cos(brg))
    lon2 = lon1 + atan2(sin(brg) * sin(d) * cos(lat1), cos(d) - sin(lat1) * sin(lat2))
    return degrees(lat2), (degrees(lon2) + 540.0) % 360.0 - 180.0  # longitude back into [-180, 180)


def distance_m(a: Point, b: Point) -> float:
    """Great-circle distance (haversine)."""
    lat1, lat2 = radians(a[0]), radians(b[0])
    dlat, dlon = lat2 - lat1, radians(b[1] - a[1])
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * R * asin(min(1.0, sqrt(h)))


def bearing_deg(a: Point, b: Point) -> float:
    """Initial bearing from a to b, degrees true in [0, 360)."""
    lat1, lat2 = radians(a[0]), radians(b[0])
    dlon = radians(b[1] - a[1])
    y = sin(dlon) * cos(lat2)
    x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    return (degrees(atan2(y, x)) + 360.0) % 360.0


def finite_point(p) -> bool:
    try:
        return len(p) == 2 and all(isfinite(float(v)) for v in p) and -90 <= p[0] <= 90 and -180 <= p[1] <= 180
    except (TypeError, ValueError):
        return False
