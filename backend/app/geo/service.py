"""The audio geolocation pipeline, without the web layer: one event in, a zone or destination out.

    event {type, distance_m, bearing_deg, radius_m} or {type, text}   + the reporter's position
      -> step 1 parse (parse_spatial.py, when only text was sent)
      -> step 2 project the centre (project.py)
      -> sanity checks (finite, <= max_report_distance_m from the reporter, inside operating_area)
      -> step 3/4: zone (zones.py) or snapped road segment / destination (snap.py)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from .parse_spatial import parse_spatial
from .project import Point, finite_point, project
from .snap import nearest_road_edge, snap_destination
from .zones import ZONE_KINDS, GeoZone, ZoneStore

GEO_TYPES = ZONE_KINDS + ("DESTINATION",)


class GeoError(ValueError):
    """The report can't be placed on the map; the message says why (sent back as HTTP 422)."""


@dataclass
class Located:
    centre: Point
    distance_m: float
    bearing_deg: float
    radius_m: Optional[float]
    uncertainty_m: float
    assumptions: list[str] = field(default_factory=list)
    needs_confirmation: bool = False
    bearing_source: str = "given"


def _num(v, name: str) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise GeoError(f"{name} is not a number: {v!r}")
    if not math.isfinite(x):
        raise GeoError(f"{name} is not finite")
    return x


def true_heading(source: dict, cfg: dict) -> Optional[float]:
    h = _num(source.get("heading_deg"), "heading_deg")
    if h is None:
        return None
    if source.get("heading_ref", "magnetic") == "magnetic":
        h += float(cfg.get("magnetic_declination_deg", 0.0))
    return h % 360


def locate(event: dict, reporter: Point, cfg: dict, store: ZoneStore) -> Located:
    """Step 1 and 2 plus the sanity checks."""
    if not finite_point(reporter):
        raise GeoError("reporter position is missing or not finite")
    source = event.get("source") or {}
    distance = _num(event.get("distance_m"), "distance_m")
    bearing = _num(event.get("bearing_deg"), "bearing_deg")
    radius = _num(event.get("radius_m"), "radius_m")
    assumptions: list[str] = list(event.get("assumptions") or [])
    needs = bool(event.get("needs_confirmation", False))
    how = "given"
    text = event.get("text") or event.get("transcript_uk") or event.get("transcript")
    if (distance is None or bearing is None) and text:
        sp = parse_spatial(text, true_heading(source, cfg), float(cfg["default_distance_m"]))
        distance = sp.distance_m if distance is None else distance
        bearing = sp.bearing_deg if bearing is None else bearing
        radius = sp.radius_m if radius is None else radius
        assumptions += sp.assumptions
        needs = needs or sp.needs_confirmation
        how = sp.bearing_source or how
        if bearing is None:
            raise GeoError("; ".join(sp.reasons) or "no direction heard")
    if bearing is None:
        raise GeoError("bearing_deg missing (and no text to parse it from)")
    if distance is None:
        distance = float(cfg["default_distance_m"])
        assumptions.append(f"no distance given: assumed {distance:.0f} m")
    if distance < 0:
        raise GeoError("distance_m is negative")
    if radius is not None and radius <= 0:
        raise GeoError("radius_m must be positive")
    if distance > float(cfg["max_report_distance_m"]):
        raise GeoError(f"{distance:.0f} m from the reporter is beyond the {cfg['max_report_distance_m']} m limit")

    centre = project(reporter[0], reporter[1], distance, bearing % 360)
    s, w, n, e = cfg["operating_area"]
    if not finite_point(centre) or not (s <= centre[0] <= n and w <= centre[1] <= e):
        raise GeoError(f"centre {centre[0]:.5f}, {centre[1]:.5f} is outside the operating area")
    gps = _num(source.get("accuracy_m"), "accuracy_m")
    return Located(centre, distance, bearing % 360, radius, store.uncertainty_m(distance, gps), assumptions,
                   needs, how)


class GeoService:
    """Holds the zone store; the web layer (api.py) passes in the road network and drop points."""

    def __init__(self, store: Optional[ZoneStore] = None):
        self.store = store or ZoneStore()
        self.cfg = self.store.cfg

    def handle(self, event: dict, reporter: Point, *, event_id: str, reporter_id: Optional[str] = None,
               roadnet: Optional[Callable[[], object]] = None,
               drop_points: Optional[Callable[[], Iterable[tuple[str, Point]]]] = None,
               now: Optional[float] = None) -> tuple[dict, list[tuple[str, GeoZone]]]:
        """(response body, [(action, zone)]) where action is created / updated."""
        kind = event.get("type")
        if kind not in GEO_TYPES:
            raise GeoError(f"type must be one of {', '.join(GEO_TYPES)}")
        loc = locate(event, reporter, self.cfg, self.store)
        body = {"event_id": event_id, "type": kind, "reporter": list(reporter), "distance_m": loc.distance_m,
                "bearing_deg": round(loc.bearing_deg, 1), "bearing_source": loc.bearing_source,
                "centre": [round(loc.centre[0], 6), round(loc.centre[1], 6)],
                "uncertainty_m": round(loc.uncertainty_m, 1), "assumptions": loc.assumptions,
                "needs_confirmation": loc.needs_confirmation}

        if kind == "DESTINATION":
            within = float(self.cfg["types"]["DESTINATION"].get("snap_within_m", 300))
            dest = snap_destination(loc.centre, drop_points() if drop_points else (),
                                    roadnet() if roadnet else None, within)
            body["destination"] = dest
            return body, []

        edge, centre = None, loc.centre
        if kind == "ROAD_BLOCKED":
            net = roadnet() if roadnet else None
            hit = nearest_road_edge(net, loc.centre) if net is not None else None
            if hit is None:
                raise GeoError("no road within reach of the reported point")
            edge, centre, moved = hit
            body["snapped_m"] = round(moved, 1)
        zone, action = self.store.report(kind, centre, reporter=reporter, event_id=event_id,
                                         reporter_id=reporter_id, radius_m=loc.radius_m,
                                         uncertainty_m=loc.uncertainty_m, assumptions=loc.assumptions,
                                         needs_confirmation=loc.needs_confirmation, edge=edge, now=now,
                                         expires_s=_num(event.get("expires_s"), "expires_s"))
        body["zone"] = zone.feature()
        body["action"] = action
        return body, [(action, zone)]
