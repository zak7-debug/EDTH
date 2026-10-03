"""Ground routes along roads and field tracks, round threat zones: casualty evacuations (evac.py) and the
in-sector truck legs of the supply chain drawn on the map (stock.py, supply_chain.py).

How it fits the product:
- A casualty is driven to hospital by road, not cross-country: `RoadNet.route(a, b)` returns the
  waypoints, metres and seconds of the fastest drive, so the map shows the ambulance turning along
  roads and tracks, and the ETA is a driving time.
- The network is generic and invented on purpose: farmland here is laid out in big rectangular fields
  with a track along most field edges, so the sector gets a lattice of field tracks every TRACK_M,
  with every ROAD_EVERY-th line a paved road. It is NOT traced from real roads: the map must never show
  anything that reads like a real front-line casualty or supply route (see seed.py).
- Segments that cross a threat zone are closed, so routes go round zones along other roads. Getting
  on and off the network (from a squad's position, into a hospital) is a short cross-country leg.

Searchable tags: TUNE (numbers to adjust), HOOK (integration points).
"""
from __future__ import annotations

import heapq
import math
from typing import Iterable, Optional

from .models import NoFlyZone
from .routing import Router, haversine_m

Point = tuple[float, float]

# TUNE: the area the network covers: the sector, the hospitals behind it and the Dnipro-region Role 3.
BBOX = (47.50, 34.90, 48.55, 35.90)  # (south, west, north, east)
TRACK_M = 2_000  # field tracks every 2 km
ROAD_EVERY = 4  # every 4th line is a paved road (8 km apart)
JITTER_M = 250  # nudge each junction so the lattice doesn't look ruled
ROAD_KMH, TRACK_KMH, OFFROAD_KMH = 50.0, 25.0, 10.0  # military ambulance or truck, loaded
ENTRY_NODES = 4  # join the network at the corners of the cell you are in


def _jitter(i: int, j: int) -> tuple[float, float]:
    """Deterministic +-JITTER_M offset per junction (same map on every run)."""
    h = (i * 73_856_093) ^ (j * 19_349_663)
    return ((h % 1000) / 500 - 1) * JITTER_M, (((h // 1000) % 1000) / 500 - 1) * JITTER_M


class RoadNet:
    def __init__(self, zones: Iterable[NoFlyZone] = ()):
        self.zones = list(zones)
        self.router = Router(self.zones)
        s, w, n, e = BBOX
        self.dlat = TRACK_M / 110_540.0
        self.dlon = TRACK_M / (111_320.0 * math.cos(math.radians((s + n) / 2)))
        self.rows = int((n - s) / self.dlat) + 1
        self.cols = int((e - w) / self.dlon) + 1
        self.nodes: dict[tuple[int, int], Point] = {}
        for i in range(self.rows):
            for j in range(self.cols):
                dy, dx = _jitter(i, j)
                self.nodes[(i, j)] = (s + i * self.dlat + dy / 110_540.0,
                                      w + j * self.dlon + dx / (111_320.0 * math.cos(math.radians(s + i * self.dlat))))
        # Edges: east-west along row i, north-south along column j. Closed if they cross a threat zone.
        self.adj: dict[tuple[int, int], list[tuple[tuple[int, int], float, float]]] = {k: [] for k in self.nodes}
        for (i, j), p in self.nodes.items():
            for (ni, nj), road in (((i, j + 1), i % ROAD_EVERY == 0), ((i + 1, j), j % ROAD_EVERY == 0)):
                q = self.nodes.get((ni, nj))
                if q is None or not self.router.clear(p, q):
                    continue
                m = haversine_m(p, q)
                sec = m / ((ROAD_KMH if road else TRACK_KMH) / 3.6)
                self.adj[(i, j)].append(((ni, nj), m, sec))
                self.adj[(ni, nj)].append(((i, j), m, sec))

    @classmethod
    def from_repo(cls, repo) -> "RoadNet":
        return cls(repo.list_no_fly_zones())

    def covers(self, p: Point) -> bool:
        s, w, n, e = BBOX
        return s <= p[0] <= n and w <= p[1] <= e

    def _entries(self, p: Point) -> list[tuple[tuple[int, int], float, float]]:
        """Junctions round `p` reachable cross-country without crossing a zone: (node, metres, seconds)."""
        s, w, _, _ = BBOX
        i0, j0 = int((p[0] - s) / self.dlat), int((p[1] - w) / self.dlon)
        out = []
        for di in (0, 1, -1, 2):
            for dj in (0, 1, -1, 2):
                k = (i0 + di, j0 + dj)
                q = self.nodes.get(k)
                if q is not None and self.adj[k] and self.router.clear(p, q):
                    m = haversine_m(p, q)
                    out.append((k, m, m / (OFFROAD_KMH / 3.6)))
        return sorted(out, key=lambda e: e[2])[:ENTRY_NODES]

    def route(self, a: Point, b: Point) -> tuple[list[Point], float, float]:
        """Fastest drive from a to b: (waypoints, metres, seconds). Falls back to cross-country round
        the zones (at off-road speed) when either end is outside the network or nothing connects."""
        direct_m = haversine_m(a, b)
        best_direct = None
        if self.router.clear(a, b):  # very short hops: just drive across
            best_direct = ([a, b], direct_m, direct_m / (OFFROAD_KMH / 3.6))
        if not (self.covers(a) and self.covers(b)):
            return best_direct or self._offroad(a, b)
        starts, ends = self._entries(a), {k: (m, sec) for k, m, sec in self._entries(b)}
        if not starts or not ends:
            return best_direct or self._offroad(a, b)
        best = {k: sec for k, m, sec in starts}
        dist = {k: m for k, m, sec in starts}
        prev: dict = {}
        heap = [(sec, k) for k, m, sec in starts]
        heapq.heapify(heap)
        goal, goal_t = None, best_direct[2] if best_direct else float("inf")
        while heap:
            t, k = heapq.heappop(heap)
            if t >= goal_t:
                break
            if t > best.get(k, float("inf")):
                continue
            if k in ends and t + ends[k][1] < goal_t:
                goal, goal_t = k, t + ends[k][1]
            for nk, m, sec in self.adj[k]:
                nt = t + sec
                if nt < best.get(nk, float("inf")):
                    best[nk], dist[nk], prev[nk] = nt, dist[k] + m, k
                    heapq.heappush(heap, (nt, nk))
        if goal is None:
            return best_direct or self._offroad(a, b)
        path, k = [goal], goal
        while k in prev:
            k = prev[k]
            path.append(k)
        pts = [a] + [self.nodes[k] for k in reversed(path)] + [b]
        return pts, dist[goal] + ends[goal][0], goal_t

    def _offroad(self, a: Point, b: Point) -> tuple[list[Point], float, float]:
        pts, m = self.router.route(a, b)
        return [tuple(p) for p in pts], m, m / (OFFROAD_KMH / 3.6)



# ---- road geometry for the map's truck legs ---------------------------------------------------------

_nets: dict[tuple, RoadNet] = {}
_legs: dict[tuple, list[list[float]]] = {}


def net_for(zones: list[NoFlyZone]) -> RoadNet:
    """One RoadNet per set of threat zones (built once, ~50 ms)."""
    key = tuple(sorted((z.id, tuple(map(tuple, z.polygon))) for z in zones))
    if key not in _nets:
        _nets.clear()  # zones only ever grow during a run: keep just the current network
        _legs.clear()
        _nets[key] = RoadNet(zones)
    return _nets[key]


def truck_legs(nodes: dict, links, zones: list[NoFlyZone]) -> dict[str, list[list[float]]]:
    """HOOK: chain_status. Road waypoints for every TRUCK link whose both ends are on the road network,
    keyed "src>dst", so the map draws trucks (and moves shipments) along roads, not as the crow flies."""
    net = net_for(zones)
    out = {}
    for l in links:
        a, b = nodes.get(l.src_id), nodes.get(l.dst_id)
        if l.mode != "TRUCK" or a is None or b is None:
            continue
        pa, pb = (a.lat, a.lon), (b.lat, b.lon)
        if not (net.covers(pa) and net.covers(pb)):
            continue
        key = (l.src_id, l.dst_id, pa, pb)
        if key not in _legs:
            pts, _, _ = net.route(pa, pb)
            _legs[key] = [[round(p[0], 5), round(p[1], 5)] for p in pts]
        out[f"{l.src_id}>{l.dst_id}"] = _legs[key]
    return out
