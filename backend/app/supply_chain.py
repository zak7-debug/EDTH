"""Supply-chain routing: the fastest working path that restocks a launch site.

How it fits the product:
- The logistics graph holds every supplier, hub, hospital and launch site, joined by SUPPLIES edges
  with a lead time. `best_path()` searches it for the quickest way to get a restock order to a
  launch site, passing only through sites that are OPERATIONAL (not destroyed, not still setting up).
- When a hub or hospital is destroyed (POST /sites), the dashboard recomputes every launch site's
  route at once, so the judges see "Launch Site North: was 60 min via the forward point, now
  2 h 30 min straight from the Dnipro hub". That is the "if infrastructure is hit, a new chain is
  found quickly" claim from the brief.
- The engine's "no drone" suggestion also uses it, so it never names a destroyed site.

How it works: Dijkstra backwards from the launch site along SUPPLIES edges (weight = lead time in
minutes). The first site reached that holds the whole order is the fastest source; the sites in
between only pass stock on, so they don't need to hold it themselves.

Searchable tags: TUNE (numbers to adjust), HOOK (integration points).
"""
from __future__ import annotations

import heapq
from typing import Optional

from . import blocks, resilience, roads
from .models import Depot, Facility, SupplyLink

# TUNE: the standard restock order a launch site asks for. A source must hold all of it.
RESTOCK_ORDER = {"tourniquet": 10, "blood_oneg": 10, "hemostatic_gauze": 10, "chest_seal": 10}


def best_path(target_id: str, items: dict[str, int], facilities: list[Facility], depots: list[Depot],
              links: list[SupplyLink]) -> Optional[dict]:
    """Fastest working chain from any site holding `items` to `target_id`, or None if cut off.
    Returns {"source_id", "path": [ids, source first], "legs": [{src_id, dst_id, mode, minutes}], "minutes"}."""
    links = blocks.BLOCKS.open_links(links)  # cut rail lines and blocked roads carry nothing (blocks.py)
    nodes: dict[str, object] = {d.id: d for d in depots}
    nodes.update({f.id: f for f in facilities})
    up = lambda n: getattr(nodes.get(n), "status", "OPERATIONAL") == "OPERATIONAL"  # depots are always up; a site
    # still SETTING_UP (resilience.py) or DESTROYED passes nothing on
    holds = lambda n: n != target_id and all(nodes[n].stock.get(i, 0) >= q for i, q in items.items())
    incoming: dict[str, list[SupplyLink]] = {}
    for link in links:
        incoming.setdefault(link.dst_id, []).append(link)

    best = {target_id: 0.0}
    via: dict[str, SupplyLink] = {}  # node -> the link it sends stock down towards the target
    frontier = [(0.0, target_id)]
    while frontier:
        minutes, node = heapq.heappop(frontier)
        if minutes > best.get(node, float("inf")):
            continue
        if node in nodes and holds(node):
            path, legs, n = [node], [], node
            while n != target_id:
                link = via[n]
                legs.append({"src_id": link.src_id, "dst_id": link.dst_id, "mode": link.mode,
                             "minutes": link.lead_time_min})
                n = link.dst_id
                path.append(n)
            return {"source_id": node, "path": path, "legs": legs, "minutes": minutes}
        for link in incoming.get(node, []):
            if link.src_id not in nodes or not up(link.src_id):
                continue
            m = minutes + link.lead_time_min
            if m < best.get(link.src_id, float("inf")):
                best[link.src_id], via[link.src_id] = m, link
                heapq.heappush(frontier, (m, link.src_id))
    return None


def chain_status(repo, items: Optional[dict[str, int]] = None) -> dict:
    """Every launch site's current restock route. HOOK: payload of the `supply_chain` message.
    Two graph reads (facilities with stock, SUPPLIES edges) plus Python, a few ms in total."""
    items = items or RESTOCK_ORDER
    facilities, depots, links = repo.list_facilities(), repo.list_depots(), repo.list_supply_links()
    return {
        "order": items,
        "status": {f.id: f.status for f in facilities},
        "routes": [{"depot_id": d.id, **(best_path(d.id, items, facilities, depots, links) or {"path": None})}
                   for d in depots],
        # Where to put a temporary stand-in for each destroyed site (resilience.py). Empty when nothing is down.
        "suggestions": resilience.suggestions(repo, facilities, depots, links, items),
        # Road waypoints for in-sector truck legs (roads.py), so the map draws trucks along roads, round
        # threat zones and road blocks.
        "road_legs": roads.truck_legs({**{d.id: d for d in depots}, **{f.id: f for f in facilities}}, links,
                                      blocks.ground_zones(repo)),
        "blocks": blocks.BLOCKS.list(),  # road and rail blocks in place (blocks.py)
    }
