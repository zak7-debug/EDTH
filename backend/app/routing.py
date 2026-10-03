"""Routing around threat / no-fly zones: a waypoint grid plus A*.

How it fits the product:
- The dispatch engine calls `router.route(a, b)` for every candidate drone, so ETAs and range checks
  use the real flight path instead of a straight line. Wire it in with
  `DispatchEngine(repo, route_fn=Router.from_repo(repo).route)`.
- The route is a list of (lat, lon) waypoints that goes out in the `dispatch` WebSocket message;
  the map draws it, so the judges see the drone bend around the EW jamming zone.

How it works:
1. If the straight line from a to b doesn't cross any zone, fly straight (the common, fast case).
2. Otherwise build a visibility graph: the start, the end, and each zone's corners pushed slightly
   outwards. Two points are connected if the line between them crosses no zone.
3. A* over that graph (distance = great-circle metres) gives the shortest safe path.

A visibility graph gives the true shortest path around polygons with a handful of nodes
(12 corners here), so a route costs well under a millisecond. No grid resolution to tune.

Searchable tags: TUNE (numbers to adjust), HOOK (integration points). See docs/foundations-guide.md.
"""
from __future__ import annotations

import heapq
import math
from typing import Iterable, Optional

from .models import NoFlyZone

Point = tuple[float, float]  # (lat, lon)

# TUNE: how far outside a zone's corner the detour waypoint sits, in metres. Bigger = wider berth.
CLEARANCE_M = 250.0


def haversine_m(a: Point, b: Point) -> float:
    r = math.radians
    dlat, dlon = r(b[0] - a[0]), r(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(r(a[0])) * math.cos(r(b[0])) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def path_length_m(points: list[Point]) -> float:
    return sum(haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1))


# --- 2D geometry on a local flat projection (fine over tens of km) -------------------------------

class _Flat:
    """Equirectangular projection to metres around a reference latitude, for segment tests."""

    def __init__(self, ref_lat: float):
        self.kx = 111_320.0 * math.cos(math.radians(ref_lat))  # metres per degree of longitude
        self.ky = 110_540.0  # metres per degree of latitude

    def xy(self, p: Point) -> tuple[float, float]:
        return p[1] * self.kx, p[0] * self.ky

    def ll(self, x: float, y: float) -> Point:
        return y / self.ky, x / self.kx


def _cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segments_cross(p1, p2, q1, q2) -> bool:
    """Proper intersection of segments p1-p2 and q1-q2 (touching at an end doesn't count)."""
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def _inside(pt, poly) -> bool:
    """Ray casting point-in-polygon."""
    x, y = pt
    inside = False
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i], poly[i - 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


class Router:
    def __init__(self, zones: Iterable[NoFlyZone]):
        self.zones = list(zones)
        lats = [p[0] for z in self.zones for p in z.polygon] or [0.0]
        self.flat = _Flat(sum(lats) / len(lats))
        self._polys = [[self.flat.xy(p) for p in z.polygon] for z in self.zones]
        self._corners = [self.flat.ll(*c) for poly in self._polys for c in self._offset_corners(poly)]

    @classmethod
    def from_repo(cls, repo) -> "Router":
        """HOOK: build once at startup from the logistics graph's NoFlyZone nodes."""
        return cls(repo.list_no_fly_zones())

    # public -----------------------------------------------------------------------------------

    def route(self, a: Point, b: Point) -> tuple[list[Point], float]:
        """Shortest path from a to b that avoids every zone: (waypoints incl. a and b, metres).
        HOOK: matches the engine's RouteFn signature."""
        if self.clear(a, b):
            return [a, b], haversine_m(a, b)
        path = self._astar(a, b)
        if path is None:  # boxed in (start or end inside a zone): fly direct, the UI shows it crossing
            return [a, b], haversine_m(a, b)
        return path, path_length_m(path)

    def clear(self, a: Point, b: Point) -> bool:
        """True if the straight segment a-b crosses no zone."""
        pa, pb = self.flat.xy(a), self.flat.xy(b)
        mid = ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)
        for poly in self._polys:
            if _inside(mid, poly):
                return False
            for i in range(len(poly)):
                if _segments_cross(pa, pb, poly[i - 1], poly[i]):
                    return False
        return True

    def crosses_zone(self, a: Point, b: Point) -> Optional[str]:
        """Name of the first zone the straight line a-b crosses, or None (for logs / UI)."""
        for z in self.zones:
            if not Router([z]).clear(a, b):
                return z.name
        return None

    # internals --------------------------------------------------------------------------------

    @staticmethod
    def _offset_corners(poly):
        """Each corner pushed CLEARANCE_M outwards along the bisector, away from the polygon's centre."""
        cx = sum(p[0] for p in poly) / len(poly)
        cy = sum(p[1] for p in poly) / len(poly)
        out = []
        for x, y in poly:
            dx, dy = x - cx, y - cy
            n = math.hypot(dx, dy) or 1.0
            out.append((x + dx / n * CLEARANCE_M, y + dy / n * CLEARANCE_M))
        return out

    def _astar(self, a: Point, b: Point) -> Optional[list[Point]]:
        nodes = [a, b] + self._corners  # 0 = start, 1 = goal
        goal = 1
        h = lambda i: haversine_m(nodes[i], b)
        best = {0: 0.0}
        prev: dict[int, int] = {}
        frontier = [(h(0), 0)]
        while frontier:
            _, i = heapq.heappop(frontier)
            if i == goal:
                path = [goal]
                while path[-1] in prev:
                    path.append(prev[path[-1]])
                return [nodes[k] for k in reversed(path)]
            for j in range(len(nodes)):
                if j == i or not self.clear(nodes[i], nodes[j]):
                    continue
                g = best[i] + haversine_m(nodes[i], nodes[j])
                if g < best.get(j, math.inf):
                    best[j], prev[j] = g, i
                    heapq.heappush(frontier, (g + h(j), j))
        return None
