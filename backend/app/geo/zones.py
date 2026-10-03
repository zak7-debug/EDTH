"""Zone polygons, uncertainty, merging and expiry, plus GeoJSON output.

A zone is a circle round a projected centre, drawn as a regular polygon. The radius the router sees is
the spoken (or default) radius plus the uncertainty of the report, so a no-fly zone errs on the safe side.
Reports of the same kind within `merge_within_m` of an active zone's FIRST report merge into it (measured
from the first report, so a chain of nearby reports can't walk a zone across the map); the merged zone
covers every report's circle, its expiry is extended, and a second reporter makes it `confirmed`.
"""
from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import NoFlyZone
from .project import Point, distance_m, project

CONFIG_PATH = Path(__file__).with_name("zones.yaml")

DEFAULTS = {
    "uncertainty": {"distance_factor": 0.35, "gps_error_m": 10, "expand_zones": True},
    "polygon": {"vertices": 16, "circumscribe": True},
    "merge_within_m": 200, "max_report_distance_m": 20000, "default_distance_m": 500,
    "operating_area": [47.30, 34.60, 48.80, 36.30], "magnetic_declination_deg": 8.0,
    "types": {
        "NO_FLY_ZONE": {"radius_m": 500, "expires_min": 15, "air": True, "ground": False,
                        "name": "Reported air threat"},
        "NO_GO_AREA": {"radius_m": 200, "expires_min": 60, "air": False, "ground": True, "name": "No-go area"},
        "ROAD_BLOCKED": {"radius_m": 50, "expires_min": 60, "air": False, "ground": False,
                         "name": "Road blocked", "snap": "road"},
        "DESTINATION": {"snap": "drop_point", "snap_within_m": 300},
    },
}
ZONE_KINDS = ("NO_FLY_ZONE", "NO_GO_AREA", "ROAD_BLOCKED")


def load_config(path: Path = CONFIG_PATH) -> dict:
    """zones.yaml over the built-in defaults (the defaults alone if PyYAML is missing)."""
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    cfg["types"] = {k: dict(v) for k, v in DEFAULTS["types"].items()}
    try:
        import yaml
        loaded = yaml.safe_load(path.read_text()) or {}
    except (ImportError, OSError):
        return cfg
    for k, v in loaded.items():
        if k == "types":
            for t, tv in v.items():
                cfg["types"].setdefault(t, {}).update(tv)
        elif isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def circle_polygon(lat: float, lon: float, radius_m: float, n: int = 32,
                   circumscribe: bool = True) -> list[Point]:
    """n corners round (lat, lon), [(lat, lon), ...], not closed (the NoFlyZone convention).
    circumscribe=True puts the corners at radius / cos(pi / n), so every edge is at least radius_m from
    the centre and the polygon contains the whole circle."""
    r = radius_m / math.cos(math.pi / n) if circumscribe else radius_m
    return [project(lat, lon, r, 360.0 * i / n) for i in range(n)]


def geojson_ring(polygon: list[Point]) -> list[list[float]]:
    """[(lat, lon), ...] -> closed GeoJSON ring of [lon, lat]."""
    ring = [[round(p[1], 7), round(p[0], 7)] for p in polygon]
    return ring + [ring[0]] if ring and ring[0] != ring[-1] else ring


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Report:
    event_id: str
    reporter_id: Optional[str]
    reporter: Point
    centre: Point
    radius_m: float  # spoken or default
    uncertainty_m: float
    effective_m: float  # what the zone must cover for this report
    ts: float


@dataclass
class GeoZone:
    id: str
    kind: str
    name: str
    centre: Point
    radius_m: float
    uncertainty_m: float
    effective_radius_m: float
    polygon: list[Point]
    created_at: float
    updated_at: float
    expires_at: float
    reports: list[Report] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    needs_confirmation: bool = False
    edge: Optional[tuple[Point, Point]] = None  # ROAD_BLOCKED: the closed road segment

    @property
    def anchor(self) -> Point:
        return self.reports[0].centre

    @property
    def status(self) -> str:
        reporters = {r.reporter_id or r.event_id for r in self.reports}
        return "confirmed" if len(reporters) >= 2 else "unconfirmed"

    def no_fly_zone(self) -> NoFlyZone:
        """The shape the router and road network use."""
        label = "" if self.status == "confirmed" else " (unconfirmed)"
        return NoFlyZone(self.id, self.name + label, [(round(a, 6), round(b, 6)) for a, b in self.polygon])

    def properties(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "name": self.name, "status": self.status,
            "centre": [round(self.centre[0], 6), round(self.centre[1], 6)],
            "radius_m": round(self.radius_m, 1), "uncertainty_m": round(self.uncertainty_m, 1),
            "effective_radius_m": round(self.effective_radius_m, 1),
            "created_at": iso(self.created_at), "updated_at": iso(self.updated_at),
            "expires_at": iso(self.expires_at), "reports": len(self.reports),
            "event_ids": [r.event_id for r in self.reports],
            "assumptions": self.assumptions, "needs_confirmation": self.needs_confirmation,
            "edge": [[round(p[0], 6), round(p[1], 6)] for p in self.edge] if self.edge else None,
        }

    def feature(self) -> dict:
        return {"type": "Feature", "id": self.id, "properties": self.properties(),
                "geometry": {"type": "Polygon", "coordinates": [geojson_ring(self.polygon)]}}


class ZoneStore:
    """Active zones from spoken reports, keyed by id. Rebuilt with the World on POST /reset."""

    def __init__(self, config: Optional[dict] = None):
        self.cfg = config or load_config()
        self.zones: dict[str, GeoZone] = {}
        self._ids = itertools.count(1)

    # what the rest of the backend reads ---------------------------------------------------------

    def active(self, kind: Optional[str] = None) -> list[GeoZone]:
        return [z for z in self.zones.values() if kind is None or z.kind == kind]

    def air_zones(self) -> list[GeoZone]:
        return [z for z in self.zones.values() if self.cfg["types"][z.kind].get("air")]

    def ground_zones(self) -> list[GeoZone]:
        return [z for z in self.zones.values() if self.cfg["types"][z.kind].get("ground")]

    def blocked_edges(self) -> list[tuple[Point, Point]]:
        return [z.edge for z in self.zones.values() if z.edge is not None]

    def feature_collection(self) -> dict:
        return {"type": "FeatureCollection", "features": [z.feature() for z in self.zones.values()]}

    def get(self, zone_id: str) -> Optional[GeoZone]:
        return self.zones.get(zone_id)

    # changes ------------------------------------------------------------------------------------

    def uncertainty_m(self, distance: float, gps_error_m: Optional[float] = None) -> float:
        u = self.cfg["uncertainty"]
        return u["distance_factor"] * distance + (u["gps_error_m"] if gps_error_m is None else gps_error_m)

    def report(self, kind: str, centre: Point, *, reporter: Point, event_id: str,
               reporter_id: Optional[str] = None, radius_m: Optional[float] = None,
               uncertainty_m: float = 0.0, assumptions: Optional[list[str]] = None,
               needs_confirmation: bool = False, edge: Optional[tuple[Point, Point]] = None,
               now: Optional[float] = None) -> tuple[GeoZone, str]:
        """Add one report: (zone, "created" | "updated")."""
        now = time.time() if now is None else now
        tcfg = self.cfg["types"][kind]
        radius = float(radius_m if radius_m is not None else tcfg["radius_m"])
        expand = self.cfg["uncertainty"]["expand_zones"] and kind != "ROAD_BLOCKED"  # a road is snapped
        rep = Report(event_id, reporter_id, reporter, centre, radius, uncertainty_m,
                     radius + (uncertainty_m if expand else 0.0), now)
        expires = now + tcfg["expires_min"] * 60

        zone = self._merge_target(kind, centre, edge)
        if zone is None:
            prefix = {"NO_FLY_ZONE": "nfz", "NO_GO_AREA": "ngo", "ROAD_BLOCKED": "road"}[kind]
            zone = GeoZone(f"geo-{prefix}-{next(self._ids)}", kind, tcfg["name"], centre, radius, uncertainty_m,
                           rep.effective_m, [], now, now, expires, [rep], list(assumptions or []),
                           needs_confirmation, edge)
            self._redraw(zone)
            self.zones[zone.id] = zone
            return zone, "created"

        zone.reports.append(rep)
        n = len(zone.reports)
        zone.centre = (sum(r.centre[0] for r in zone.reports) / n, sum(r.centre[1] for r in zone.reports) / n)
        zone.radius_m = max(r.radius_m for r in zone.reports)
        zone.uncertainty_m = max(r.uncertainty_m for r in zone.reports)
        # cover every report's circle from the new centre
        zone.effective_radius_m = max(distance_m(zone.centre, r.centre) + r.effective_m for r in zone.reports)
        zone.assumptions = sorted(set(zone.assumptions) | set(assumptions or []))
        zone.needs_confirmation = zone.needs_confirmation and needs_confirmation
        zone.updated_at, zone.expires_at = now, max(zone.expires_at, expires)
        self._redraw(zone)
        return zone, "updated"

    def expire(self, now: Optional[float] = None) -> list[GeoZone]:
        now = time.time() if now is None else now
        gone = [z for z in self.zones.values() if z.expires_at <= now]
        for z in gone:
            del self.zones[z.id]
        return gone

    def delete(self, zone_id: str) -> Optional[GeoZone]:
        return self.zones.pop(zone_id, None)

    # internals ----------------------------------------------------------------------------------

    def _merge_target(self, kind: str, centre: Point, edge) -> Optional[GeoZone]:
        best, best_d = None, float(self.cfg["merge_within_m"])
        for z in self.zones.values():
            if z.kind != kind:
                continue
            if kind == "ROAD_BLOCKED" and edge is not None and z.edge is not None and set(z.edge) != set(edge):
                continue  # a different road segment is a different blockage
            d = distance_m(z.anchor, centre)
            if d <= best_d:
                best, best_d = z, d
        return best

    def _redraw(self, zone: GeoZone) -> None:
        p = self.cfg["polygon"]
        zone.polygon = circle_polygon(zone.centre[0], zone.centre[1], zone.effective_radius_m,
                                      int(p["vertices"]), bool(p["circumscribe"]))
