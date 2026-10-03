"""Snap a projected point to something real: the nearest road segment (blockages) or the nearest drop
point / road junction (destinations). Uses the sector's road network from roads.py."""
from __future__ import annotations

import math
from typing import Iterable, Optional

from ..roads import BBOX
from .project import Point, distance_m

ROAD_SNAP_MAX_M = 1_000  # TUNE: further than this from any road, a "road blocked" report is rejected


def _flat(ref_lat: float):
    kx, ky = 111_320.0 * math.cos(math.radians(ref_lat)), 110_540.0
    return lambda p: (p[1] * kx, p[0] * ky), lambda x, y: (y / ky, x / kx)


def _foot(p, a, b):
    """Closest point to p on segment a-b (flat metres) and the segment parameter t."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return (a[0] + t * dx, a[1] + t * dy), t


def _near_keys(net, p: Point):
    """Junction keys round p: the lattice's cells (RoadNet) or the spatial index (RealRoadNet)."""
    if hasattr(net, "grid"):  # RealRoadNet: node indexes bucketed by CELL degrees
        ci, cj = int(p[0] / net.CELL), int(p[1] / net.CELL)
        reach = int(ROAD_SNAP_MAX_M / (net.CELL * 75_000)) + 1
        return [i for di in range(-reach, reach + 1) for dj in range(-reach, reach + 1)
                for i in net.grid.get((ci + di, cj + dj), ())]
    s, w, _, _ = BBOX
    i0, j0 = int((p[0] - s) / net.dlat), int((p[1] - w) / net.dlon)
    return [(i0 + di, j0 + dj) for di in range(-2, 3) for dj in range(-2, 3) if (i0 + di, j0 + dj) in net.nodes]


def nearest_road_edge(net, p: Point, max_m: float = ROAD_SNAP_MAX_M
                      ) -> Optional[tuple[tuple[Point, Point], Point, float]]:
    """(segment as its two end points, the snapped point on it, metres moved), or None when p is off the
    network. Works on RoadNet and RealRoadNet. Only open segments are candidates."""
    if not net.covers(p):
        return None
    xy, ll = _flat(p[0])
    pp = xy(p)
    best, seen = None, set()
    for k in _near_keys(net, p):
        for nk, _, _ in net.adj[k]:
            e = frozenset((k, nk))
            if e in seen:
                continue
            seen.add(e)
            a, b = net.nodes[k], net.nodes[nk]
            foot, _ = _foot(pp, xy(a), xy(b))
            d = math.hypot(foot[0] - pp[0], foot[1] - pp[1])
            if best is None or d < best[2]:
                best = ((a, b), ll(*foot), d)
    if best is None or best[2] > max_m:
        return None
    return best


def snap_destination(p: Point, drop_points: Iterable[tuple[str, Point]], net=None,
                     within_m: float = 300.0) -> dict:
    """Nearest drop point within `within_m`; else the nearest road junction; else p itself."""
    best_id, best_p, best_d = None, None, float("inf")
    for pid, q in drop_points:
        d = distance_m(p, q)
        if d < best_d:
            best_id, best_p, best_d = pid, q, d
    if best_p is not None and best_d <= within_m:
        return {"lat": best_p[0], "lon": best_p[1], "snapped_to": best_id, "moved_m": round(best_d, 1)}
    if net is not None and net.covers(p):
        keys = [k for k in _near_keys(net, p) if net.adj[k]]
        if keys:
            q = min((net.nodes[k] for k in keys), key=lambda q: distance_m(p, q))
            return {"lat": q[0], "lon": q[1], "snapped_to": "road_junction", "moved_m": round(distance_m(p, q), 1)}
    return {"lat": p[0], "lon": p[1], "snapped_to": None, "moved_m": 0.0}
