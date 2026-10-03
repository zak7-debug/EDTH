"""Road and rail blocks: ground closures that reroute trucks, ambulances and trains, never drones.

How it fits the product:
- A threat zone (dev_server.add_threat) closes the air AND the roads under it. A blocked road (a
  crater, a blown bridge, a checkpoint) or a cut rail line only stops ground traffic: drones fly
  straight over it. So blocks live here, apart from the graph's no-fly zones that the dispatch
  engine reads.
- ROAD block: a point + radius. Inside the sector it closes every road and field track through it
  (roads.py, via `ground_zones()`), so ambulances already driving detour round it and in-sector truck
  legs are redrawn along other roads. It can also cut long-haul supply links whose road it sits on
  (the dashboard sends those as `links`).
- RAIL block: cuts the rail links it names (`links`). The supply chain (supply_chain.best_path) then
  plans round them, and trains still to run on a cut line are re-planned from the last site they
  reached (StockKeeper.links_closed).
- Lifting a block re-opens it; new decisions use it again, vehicles already rerouted keep going.

Searchable tags: TUNE (numbers to adjust), HOOK (integration points).
"""
from __future__ import annotations

import heapq
import itertools
import math
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, HTTPException

from .models import NoFlyZone, SupplyLink

ROAD_BLOCK_RADIUS_M = 400  # TUNE: how much road one block closes (a crater or blown bridge plus its approaches)


def _hexagon(lat: float, lon: float, radius_m: float) -> list[tuple[float, float]]:
    dlat = radius_m / 110_540.0
    dlon = radius_m / (111_320.0 * math.cos(math.radians(lat)))
    return [(round(lat + dlat * math.sin(math.radians(a)), 6), round(lon + dlon * math.cos(math.radians(a)), 6))
            for a in range(0, 360, 60)]


def link_key(src: str, dst: str, mode: str) -> str:
    return f"{src}>{dst}|{mode}"


@dataclass
class Block:
    id: str
    kind: str  # ROAD | RAIL
    name: str
    lat: float
    lon: float
    radius_m: float = ROAD_BLOCK_RADIUS_M
    links: list[str] = field(default_factory=list)  # "src>dst|MODE" supply links it cuts (both directions)
    polygon: list[tuple[float, float]] = field(default_factory=list)

    def zone(self) -> NoFlyZone:
        """The block as a zone the road network closes round (never given to the drones)."""
        return NoFlyZone(self.id, self.name, self.polygon)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "name": self.name, "lat": self.lat, "lon": self.lon,
                "radius_m": self.radius_m, "links": list(self.links), "polygon": [list(p) for p in self.polygon]}


class Blocks:
    """Every block in place now. One set per world (cleared on reset)."""

    def __init__(self):
        self.items: dict[str, Block] = {}
        self._ids = itertools.count(1)

    def clear(self) -> None:
        self.items.clear()

    def add(self, kind: str, lat: float, lon: float, name: Optional[str] = None, radius_m: Optional[float] = None,
            links: Optional[list[str]] = None) -> Block:
        kind = kind.upper()
        if kind not in ("ROAD", "RAIL"):
            raise ValueError("kind must be ROAD or RAIL")
        r = float(radius_m or ROAD_BLOCK_RADIUS_M)
        b = Block(f"blk-{next(self._ids)}", kind, name or ("Road blocked" if kind == "ROAD" else "Rail line cut"),
                  float(lat), float(lon), r, list(links or []),
                  _hexagon(float(lat), float(lon), r) if kind == "ROAD" else [])
        self.items[b.id] = b
        return b

    def remove(self, block_id: str) -> Optional[Block]:
        return self.items.pop(block_id, None)

    def road_zones(self) -> list[NoFlyZone]:
        return [b.zone() for b in self.items.values() if b.kind == "ROAD"]

    def closed(self) -> set[str]:
        """Every cut link key, both directions (a cut line is cut whichever way you travel it)."""
        out = set()
        for b in self.items.values():
            for k in b.links:
                ends, mode = k.split("|", 1) if "|" in k else (k, "")
                src, dst = ends.split(">", 1)
                out |= {link_key(src, dst, mode), link_key(dst, src, mode)}
        return out

    def open_links(self, links: list[SupplyLink]) -> list[SupplyLink]:
        closed = self.closed()
        if not closed:
            return links
        return [l for l in links if link_key(l.src_id, l.dst_id, l.mode) not in closed]

    def list(self) -> list[dict]:
        return [b.to_dict() for b in self.items.values()]


BLOCKS = Blocks()  # HOOK: module-level like querylog; dev_server.World() clears it on reset


def ground_zones(repo) -> list[NoFlyZone]:
    """What closes roads: the graph's threat zones plus the road blocks. HOOK: roads.net_for input."""
    return list(repo.list_no_fly_zones()) + BLOCKS.road_zones()


def route_from(src_id: str, target_id: str, facilities, depots, links: list[SupplyLink]) -> Optional[dict]:
    """Fastest working chain from a given site to `target_id` (a shipment already part-way along).
    Same shape as supply_chain.best_path; only passes through OPERATIONAL sites."""
    nodes = {d.id: d for d in depots}
    nodes.update({f.id: f for f in facilities})
    up = lambda n: n == src_id or n == target_id or getattr(nodes.get(n), "status", "OPERATIONAL") == "OPERATIONAL"
    out: dict[str, list[SupplyLink]] = {}
    for l in BLOCKS.open_links(links):
        out.setdefault(l.src_id, []).append(l)
    best, via = {src_id: 0.0}, {}
    heap = [(0.0, src_id)]
    while heap:
        t, n = heapq.heappop(heap)
        if t > best.get(n, float("inf")):
            continue
        if n == target_id:
            legs, k = [], n
            while k != src_id:
                l = via[k]
                legs.append({"src_id": l.src_id, "dst_id": l.dst_id, "mode": l.mode, "minutes": l.lead_time_min})
                k = l.src_id
            legs.reverse()
            return {"source_id": src_id, "path": [src_id] + [l["dst_id"] for l in legs], "legs": legs, "minutes": t}
        for l in out.get(n, []):
            if l.dst_id not in nodes or not up(l.dst_id):
                continue
            nt = t + l.lead_time_min
            if nt < best.get(l.dst_id, float("inf")):
                best[l.dst_id], via[l.dst_id] = nt, l
                heapq.heappush(heap, (nt, l.dst_id))
    return None


# ---- API --------------------------------------------------------------------------------------------

router = APIRouter()


async def _apply(world, why: str, closed: set[str], block: Optional[Block] = None, lifted: bool = False) -> dict:
    """Re-plan everything on the ground after a block went up or came down, and tell every screen."""
    from . import dev_server  # late import: dev_server includes this router
    from .messages import supply_chain_msg
    from .supply_chain import chain_status
    t = time.perf_counter()
    msgs = []
    if block is not None and block.kind == "ROAD" and not lifted:
        msgs += world.evac.reroute(block.zone())  # ambulances whose road ahead runs through it detour
    elif block is not None and block.kind == "ROAD":
        from .roads import net_for
        world.evac.roads = net_for(ground_zones(world.repo))  # new evacuations may use the road again
    if closed and not lifted:
        msgs += world.stock.links_closed(closed, why)  # trains and trucks still to run on a cut line
    chain = chain_status(world.repo)
    ms = round((time.perf_counter() - t) * 1000, 1)
    await dev_server.broadcast(supply_chain_msg(chain, {"block": block.to_dict() if block else None,
                                                        "lifted": lifted, "ms": ms}))
    for m in msgs:
        await dev_server.broadcast(m)
    if msgs or closed:
        await dev_server.broadcast(world.stock.message())
    return {"block": block.to_dict() if block else None, "ms": ms,
            "rerouted": [m["data"].get("person_id") or (m["data"].get("change") or {}).get("order_id") for m in msgs]}


@router.post("/blocks")
async def post_block(body: dict):
    """HOOK: block a road or rail line. Body: {"kind": "ROAD"|"RAIL", "lat", "lon", "radius_m"?, "name"?,
    "links"?: ["src>dst|MODE", ...]}. A RAIL block needs at least one link; a ROAD block closes the roads
    round its point and any long-haul links listed."""
    from . import dev_server
    kind = str(body.get("kind", "ROAD")).upper()
    if kind == "RAIL" and not body.get("links"):
        raise HTTPException(422, "a rail block needs the rail link it cuts")
    try:
        b = BLOCKS.add(kind, body["lat"], body["lon"], body.get("name"), body.get("radius_m"), body.get("links"))
    except (KeyError, ValueError) as e:
        raise HTTPException(422, f"bad block: {e}")
    why = "road blocked" if kind == "ROAD" else "rail line cut"
    return await _apply(dev_server.world, why, BLOCKS.closed() if b.links else set(), b)


@router.post("/blocks/lift")
async def lift_block(body: dict):
    """Lift a block (or every block with {"all": true}). Body: {"id": "blk-1"}."""
    from . import dev_server
    ids = list(BLOCKS.items) if body.get("all") else [body.get("id")]
    gone = [BLOCKS.remove(i) for i in ids]
    gone = [b for b in gone if b]
    if not gone:
        raise HTTPException(404, f"no block {body.get('id')!r}")
    out = None
    for b in gone:
        out = await _apply(dev_server.world, "", set(), b, lifted=True)
    return {"lifted": [b.id for b in gone], "ms": out["ms"] if out else 0}


@router.get("/blocks")
def get_blocks():
    return BLOCKS.list()
