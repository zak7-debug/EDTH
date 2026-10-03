"""Ground routes along roads and field tracks, round threat zones: casualty evacuations (evac.py) and the
in-sector truck legs of the supply chain drawn on the map (stock.py, supply_chain.py).

How it fits the product:
- A casualty is driven to hospital by road, not cross-country: `RoadNet.route(a, b)` returns the
  waypoints, metres and seconds of the fastest drive, so the map shows the ambulance turning along
  roads and tracks, and the ETA is a driving time.
- Real public roads when they have been downloaded: `python scripts/fetch_roads.py` (run once, online)
  saves the OpenStreetMap roads under the map's road overlay to backend/data/roads.json, and
  `RealRoadNet` drives on them, so trucks and ambulances follow the roads the overlay draws. These are
  ordinary public roads; the sites, units and routes between them stay invented (see seed.py).
- Without that file (or with EDTH_ROADS=grid) the network is generic and invented: farmland here is
  laid out in big rectangular fields with a track along most field edges, so the sector gets a lattice
  of field tracks every TRACK_M, with every ROAD_EVERY-th line a paved road.
- Segments that cross a threat zone are closed, so routes go round zones along other roads. Getting
  on and off the network (from a squad's position, into a hospital) is a short cross-country leg.

Searchable tags: TUNE (numbers to adjust), HOOK (integration points).
"""
from __future__ import annotations

import heapq
import json
import math
import os
from pathlib import Path
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

# HOOK: written by scripts/fetch_roads.py. TUNE: driving speeds per OpenStreetMap road class.
ROADS_FILE = Path(__file__).resolve().parents[1] / "data" / "roads.json"
KIND_KMH = {"motorway": 80.0, "trunk": 70.0, "primary": 60.0, "secondary": 50.0, "tertiary": 40.0,
            "unclassified": 30.0, "track": TRACK_KMH}
ENTRY_SEARCH_M = 6_000  # how far from a road a site or squad may be and still drive off it


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
        """Fastest drive from a to b: (waypoints, metres, seconds). See _dijkstra."""
        return _dijkstra(self, a, b, lambda k: self.nodes[k])

    def _offroad(self, a: Point, b: Point) -> tuple[list[Point], float, float]:
        pts, m = self.router.route(a, b)
        return [tuple(p) for p in pts], m, m / (OFFROAD_KMH / 3.6)


# ---- real roads (scripts/fetch_roads.py) ------------------------------------------------------------

def _simplify(pts: list[Point], keep: set[int], tol_m: float) -> list[int]:
    """Indexes of `pts` to keep: Douglas-Peucker at tol_m, never dropping an index in `keep`."""
    out, cut = [0], sorted({0, len(pts) - 1} | keep)
    lat0 = math.radians(pts[0][0])

    def off(p, a, b):  # metres from p to the line a-b (flat earth: fine at road scale)
        ax, ay = a[1] * 111_320 * math.cos(lat0), a[0] * 110_540
        bx, by = b[1] * 111_320 * math.cos(lat0), b[0] * 110_540
        px, py = p[1] * 111_320 * math.cos(lat0), p[0] * 110_540
        dx, dy = bx - ax, by - ay
        if dx == dy == 0:
            return math.hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - ax - t * dx, py - ay - t * dy)

    def dp(i, j):
        best, k = 0.0, None
        for m in range(i + 1, j):
            d = off(pts[m], pts[i], pts[j])
            if d > best:
                best, k = d, m
        if k is not None and best > tol_m:
            dp(i, k)
            out.append(k)
            dp(k, j)

    for i, j in zip(cut, cut[1:]):
        dp(i, j)
        out.append(j)
    return sorted(set(out))


def compile_osm(osm: dict, tol_m: float = 15.0) -> dict:
    """Overpass JSON (ways with highway tags, plus their nodes) -> the compact roads.json:
    {"nodes": [[lat, lon], ...], "ways": [[kind, [node index, ...]], ...]}. Junctions are kept exactly,
    the bends between them simplified to tol_m, and only the largest connected network is kept."""
    coords = {e["id"]: (e["lat"], e["lon"]) for e in osm["elements"] if e["type"] == "node"}
    ways = []
    for e in osm["elements"]:
        if e["type"] != "way":
            continue
        kind = e.get("tags", {}).get("highway", "").replace("_link", "")
        ids = [n for n in e.get("nodes", []) if n in coords]
        if kind in KIND_KMH and len(ids) >= 2:
            ways.append((kind, ids))
    uses: dict[int, int] = {}  # > 1: a junction (on two ways) or a way's end, kept exactly
    for _, ids in ways:
        for n in set(ids):
            uses[n] = uses.get(n, 0) + 1
        uses[ids[0]] += 1
        uses[ids[-1]] += 1
    kept = []
    for kind, ids in ways:
        junctions = {i for i, n in enumerate(ids) if uses[n] > 1}
        kept.append((kind, [ids[i] for i in _simplify([coords[n] for n in ids], junctions, tol_m)]))
    # Largest connected network only: islands would trap a route start.
    parent: dict[int, int] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, ids in kept:
        for n in ids[1:]:
            parent[find(n)] = find(ids[0])
    sizes: dict[int, int] = {}
    for _, ids in kept:
        r = find(ids[0])
        sizes[r] = sizes.get(r, 0) + len(ids)
    main = max(sizes, key=sizes.get) if sizes else None
    index: dict[int, int] = {}
    nodes, out = [], []
    for kind, ids in kept:
        if find(ids[0]) != main:
            continue
        for n in ids:
            if n not in index:
                index[n] = len(nodes)
                nodes.append([round(coords[n][0], 5), round(coords[n][1], 5)])
        out.append([kind, [index[n] for n in ids]])
    return {"source": "OpenStreetMap contributors (ODbL)", "nodes": nodes, "ways": out}


class RealRoadNet:
    """Same interface as RoadNet (route, covers), on the roads in roads.json."""

    CELL = 0.02  # degrees: spatial index cell for finding the roads near a point

    def __init__(self, data: dict, zones: Iterable[NoFlyZone] = ()):
        self.zones = list(zones)
        self.router = Router(self.zones)
        self.nodes: list[Point] = [tuple(p) for p in data["nodes"]]
        lats, lons = [p[0] for p in self.nodes], [p[1] for p in self.nodes]
        pad = 0.05
        self.bbox = (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)
        boxes = []  # threat zones' lat/lon boxes: only segments touching one need the full check
        for z in self.zones:
            zl, zo = [p[0] for p in z.polygon], [p[1] for p in z.polygon]
            boxes.append((min(zl), min(zo), max(zl), max(zo)))
        self.adj: list[list[tuple[int, float, float]]] = [[] for _ in self.nodes]
        for kind, ids in data["ways"]:
            mps = KIND_KMH.get(kind, TRACK_KMH) / 3.6
            for a, b in zip(ids, ids[1:]):
                p, q = self.nodes[a], self.nodes[b]
                if any(min(p[0], q[0]) <= n and max(p[0], q[0]) >= s and min(p[1], q[1]) <= e
                       and max(p[1], q[1]) >= w for s, w, n, e in boxes) and not self.router.clear(p, q):
                    continue
                m = haversine_m(p, q)
                self.adj[a].append((b, m, m / mps))
                self.adj[b].append((a, m, m / mps))
        self.grid: dict[tuple[int, int], list[int]] = {}
        for i, p in enumerate(self.nodes):
            if self.adj[i]:
                self.grid.setdefault((int(p[0] / self.CELL), int(p[1] / self.CELL)), []).append(i)

    def covers(self, p: Point) -> bool:
        s, w, n, e = self.bbox
        return s <= p[0] <= n and w <= p[1] <= e

    def _entries(self, p: Point) -> list[tuple[int, float, float]]:
        """The nearest road points round `p` reachable cross-country without crossing a zone."""
        ci, cj = int(p[0] / self.CELL), int(p[1] / self.CELL)
        reach = int(ENTRY_SEARCH_M / (self.CELL * 75_000)) + 1
        near = []
        for r in range(reach + 1):  # widen ring by ring until something is found
            for di in range(-r, r + 1):
                for dj in range(-r, r + 1):
                    if max(abs(di), abs(dj)) == r:
                        near += [(haversine_m(p, self.nodes[i]), i) for i in self.grid.get((ci + di, cj + dj), ())]
            if len(near) >= ENTRY_NODES * 3:
                break
        out = []
        for m, i in sorted(near)[:ENTRY_NODES * 6]:
            if m <= ENTRY_SEARCH_M and self.router.clear(p, self.nodes[i]):
                out.append((i, m, m / (OFFROAD_KMH / 3.6)))
                if len(out) == ENTRY_NODES:
                    break
        return out

    def route(self, a: Point, b: Point) -> tuple[list[Point], float, float]:
        return _dijkstra(self, a, b, lambda k: self.nodes[k])

    def _offroad(self, a: Point, b: Point) -> tuple[list[Point], float, float]:
        pts, m = self.router.route(a, b)
        return [tuple(p) for p in pts], m, m / (OFFROAD_KMH / 3.6)


def _dijkstra(net, a: Point, b: Point, point_of) -> tuple[list[Point], float, float]:
    """Fastest drive from a to b on `net` (RoadNet or RealRoadNet): (waypoints, metres, seconds).
    Falls back to cross-country round the zones (at off-road speed) when either end is outside the
    network or nothing connects."""
    direct_m = haversine_m(a, b)
    best_direct = None
    if net.router.clear(a, b):  # very short hops: just drive across
        best_direct = ([a, b], direct_m, direct_m / (OFFROAD_KMH / 3.6))
    if not (net.covers(a) and net.covers(b)):
        return best_direct or net._offroad(a, b)
    starts, ends = net._entries(a), {k: (m, sec) for k, m, sec in net._entries(b)}
    if not starts or not ends:
        return best_direct or net._offroad(a, b)
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
        for nk, m, sec in net.adj[k]:
            nt = t + sec
            if nt < best.get(nk, float("inf")):
                best[nk], dist[nk], prev[nk] = nt, dist[k] + m, k
                heapq.heappush(heap, (nt, nk))
    if goal is None:
        return best_direct or net._offroad(a, b)
    path, k = [goal], goal
    while k in prev:
        k = prev[k]
        path.append(k)
    pts = [a] + [point_of(k) for k in reversed(path)] + [b]
    return pts, dist[goal] + ends[goal][0], goal_t


_road_data: dict = {}


def real_roads() -> Optional[dict]:
    """roads.json, loaded once, or None when it hasn't been downloaded (or EDTH_ROADS=grid)."""
    if os.environ.get("EDTH_ROADS", "").lower() == "grid":
        return None
    path = Path(os.environ.get("EDTH_ROADS_FILE", ROADS_FILE))
    key = (str(path), path.stat().st_mtime if path.exists() else None)
    if key not in _road_data:
        _road_data.clear()
        _road_data[key] = json.loads(path.read_text()) if key[1] is not None else None
    return _road_data[key]


# ---- road geometry for the map's truck legs ---------------------------------------------------------

_nets: dict[tuple, "RoadNet | RealRoadNet"] = {}
_legs: dict[tuple, list[list[float]]] = {}


def net_for(zones: list[NoFlyZone]):
    """One road network per set of threat zones (built once): the real roads if downloaded, else the
    invented lattice."""
    data = real_roads()
    key = (id(data), tuple(sorted((z.id, tuple(map(tuple, z.polygon))) for z in zones)))
    if key not in _nets:
        _nets.clear()  # zones only ever grow during a run: keep just the current network
        _legs.clear()
        _nets[key] = RealRoadNet(data, zones) if data else RoadNet(zones)
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
