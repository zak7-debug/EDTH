"""Safeguarding medical infrastructure: when a hub or hospital is destroyed, suggest where a temporary
replacement should go, and deploy it on request.

How it fits the product:
- supply_chain.py already re-plans every launch site's restock route round a destroyed site. This module
  answers the next question: "and where do we put a temporary one?"
- For each destroyed distribution centre or hospital, `suggestions()` searches a grid of candidate
  points round the sector and picks the best place for a stand-in:
    * a destroyed DISTRIBUTION_CENTRE -> a temporary distribution point, stocked by one convoy, that
      launch sites can restock from quickly. Scored on the launch sites' restock minutes.
    * a destroyed HOSPITAL -> a temporary one of the same role (a forward surgical team for Role 2,
      an aid station for Role 1). Scored on the squads' evacuation time to the nearest place that can
      treat them, with launch-site restock minutes as the tie-break.
  A candidate must sit outside every threat zone (with a buffer), away from the strike (it may be hit
  again) and behind the squads (out of direct fire). Those distances are TUNE numbers below.
- Each suggestion says what it buys ("CRITICAL evacuation 2 h 40 -> 14 min"), so the dashboard can show
  the choice and the reason. `deploy()` writes the temporary site and its supply links into the graph;
  from then on the supply chain, the stock reorders and casualty evacuation all use it.

The suggestions ride on the `supply_chain` message (chain_status adds them), so they show up the moment
a site is destroyed, with no extra round trip. Deploying is `POST /sites/deploy {"replaces": id}`.

Ground and truck times are straight lines times a road factor: there is no road network in the graph
yet (same caveat as evac.py). Searchable tags: TUNE (numbers to adjust), HOOK (integration points).
"""
from __future__ import annotations

import math
import time
from dataclasses import replace
from typing import Optional

from fastapi import APIRouter, HTTPException

from .models import Facility, SupplyLink
from .routing import haversine_m

# TUNE: where a temporary site may go.
STRIKE_STANDOFF_M = 5_000  # away from the site that was hit: the same target may be hit again
ZONE_BUFFER_M = 1_500  # clear of every threat zone's edge
SQUAD_STANDOFF_M = {"DISTRIBUTION_CENTRE": 15_000, "ROLE_2": 8_000, "ROLE_3": 15_000, "ROLE_1": 3_000}
GRID = 18  # candidate grid is GRID x GRID over the area of interest (~3 to 6 km apart)

# TUNE: how long things take on the ground.
ROAD_FACTOR = 1.3  # roads are ~30% longer than the straight line
TRUCK_KMH = 50.0
HANDLING_MIN = 10.0  # loading and unloading per leg
CASEVAC_KMH = 40.0  # same as evac.CASEVAC_SPEED_MPS

# TUNE: what a temporary site starts with. A distribution point is stocked by its first convoy;
# a forward surgical team arrives with its own kit and a few beds.
FIRST_CONVOY_LOADS = 3  # x the standard restock order (supply_chain.RESTOCK_ORDER)
TEMP_BEDS = {"ROLE_1": 4, "ROLE_2": 8, "ROLE_3": 20}
TEMP_KIT = {"blood_oneg": 12, "tourniquet": 6, "hemostatic_gauze": 8, "chest_seal": 4, "morphine_autoinjector": 6}
TEMP_NAMES = {"DISTRIBUTION_CENTRE": "Temporary distribution point", "ROLE_1": "Temporary aid station (Role 1)",
              "ROLE_2": "Forward surgical team (temporary Role 2)", "ROLE_3": "Temporary field hospital (Role 3)"}
SERVES = {"ROLE_1": "WOUNDED", "ROLE_2": "CRITICAL", "ROLE_3": "CRITICAL"}  # who the stand-in must take

FEED_RADIUS_M = 300_000  # feed the temporary site from working sites this close (else the nearest one)
MIN_GAIN = 0.10  # suggest only if it beats the re-planned network by at least 10%


def temp_id(destroyed_id: str) -> str:
    return f"tmp-{destroyed_id}"


def truck_min(a: tuple[float, float], b: tuple[float, float]) -> float:
    return round(haversine_m(a, b) * ROAD_FACTOR / 1000 / TRUCK_KMH * 60 + HANDLING_MIN, 1)


def casevac_min(a: tuple[float, float], b: tuple[float, float]) -> float:
    return haversine_m(a, b) * ROAD_FACTOR / 1000 / CASEVAC_KMH * 60


def _hm(m: Optional[float]) -> str:
    if m is None:
        return "cut off"
    return f"{int(m // 60)} h {int(round(m % 60)):02d}" if m >= 60 else f"{m:.0f} min"


# ---- geometry ---------------------------------------------------------------------------------------

def _xy(p, ref_lat):
    return p[1] * 111_320.0 * math.cos(math.radians(ref_lat)), p[0] * 110_540.0


def _clear_of(zone_polys, p) -> bool:
    """Outside every zone polygon and at least ZONE_BUFFER_M from its edges."""
    x, y = _xy(p, p[0])
    for poly in zone_polys:
        pts = [_xy(q, p[0]) for q in poly]
        inside = False
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
            dx, dy = x2 - x1, y2 - y1
            t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy or 1)))
            if math.hypot(x - x1 - t * dx, y - y1 - t * dy) < ZONE_BUFFER_M:
                return False
        if inside:
            return False
    return True


def _behind(c, squads, rear) -> bool:
    """Not ahead of any squad: measured along the line from the squads towards the launch sites (the rear)."""
    sx, sy = _xy((sum(a for a, _ in squads) / len(squads), sum(b for _, b in squads) / len(squads)), rear[0])
    rx, ry = _xy(rear, rear[0])
    ux, uy = rx - sx, ry - sy
    norm = math.hypot(ux, uy) or 1
    cx, cy = _xy(c, rear[0])
    return all(((cx - qx) * ux + (cy - qy) * uy) / norm >= 0 for qx, qy in (_xy(q, rear[0]) for q in squads))


def _where(p, landmarks: list[tuple[str, float, float]]) -> str:
    """'9 km NW of Launch Site Rear': where a point is, for people reading the map."""
    name, lat, lon = min(landmarks, key=lambda n: haversine_m(p, (n[1], n[2])))
    km = haversine_m(p, (lat, lon)) / 1000
    bearing = math.degrees(math.atan2((p[1] - lon) * math.cos(math.radians(lat)), p[0] - lat)) % 360
    compass = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((bearing + 22.5) // 45) % 8]
    return f"{km:.0f} km {compass} of {name}"


# ---- the search -------------------------------------------------------------------------------------

def _chain_minutes(depot_ids, items, facilities, depots, links) -> dict[str, Optional[float]]:
    from .supply_chain import best_path  # late import: supply_chain imports this module
    out = {}
    for d in depot_ids:
        p = best_path(d, items, facilities, depots, links)
        out[d] = p["minutes"] if p else None
    return out


def _total(minutes: dict[str, Optional[float]]) -> float:
    return sum(m if m is not None else 24 * 60 for m in minutes.values())  # cut off counts as a day


def _squads(personnel) -> list[tuple[float, float]]:
    by_unit: dict[str, list] = {}
    for p in personnel:
        if p.status != "ADMITTED" and p.unit_id:
            by_unit.setdefault(p.unit_id, []).append((p.lat, p.lon))
    return [(sum(a for a, _ in ps) / len(ps), sum(b for _, b in ps) / len(ps)) for ps in by_unit.values()]


def _evac_minutes(squads, hospitals: list[Facility], severity: str) -> Optional[float]:
    """Mean minutes from each squad to the nearest operational hospital that can take `severity`."""
    from .evac import ACCEPTS  # late import: evac imports the dispatch engine
    ok = [h for h in hospitals if h.kind == "HOSPITAL" and h.status == "OPERATIONAL" and h.role in ACCEPTS[severity]]
    if not ok or not squads:
        return None
    return sum(min(casevac_min(s, (h.lat, h.lon)) for h in ok) for s in squads) / len(squads)


def suggest(site: Facility, facilities: list[Facility], depots, links: list[SupplyLink], personnel,
            zones, items: dict[str, int]) -> Optional[dict]:
    """Best place for a temporary stand-in for the destroyed `site`, or a note saying none is needed."""
    if site.kind == "SUPPLIER":
        return None  # rear suppliers sit outside the sector: re-planning round them is the answer
    started = time.perf_counter()
    key = site.role if site.kind == "HOSPITAL" else "DISTRIBUTION_CENTRE"
    up = {f.id: f for f in facilities if f.status == "OPERATIONAL"}
    sources = [up[l.src_id] for l in links if l.dst_id == site.id and l.src_id in up]
    sources = [f for f in sources if haversine_m((f.lat, f.lon), (site.lat, site.lon)) < FEED_RADIUS_M] or sources[:1]
    if not sources:  # everything that fed it is gone too: the nearest working site with stock feeds it
        stocked = [f for f in up.values() if f.kind != "HOSPITAL" or f.stock]
        sources = sorted(stocked, key=lambda f: haversine_m((f.lat, f.lon), (site.lat, site.lon)))[:1]
    nodes = {f.id: f for f in facilities} | {d.id: d for d in depots}
    downstream = [nodes[l.dst_id] for l in links if l.src_id == site.id and l.dst_id in nodes
                  and getattr(nodes[l.dst_id], "status", "OPERATIONAL") == "OPERATIONAL"]
    depot_ids = [d.id for d in depots]
    squads = _squads(personnel)
    severity = SERVES.get(site.role, "CRITICAL")

    # Before the strike, and now (re-planned round it).
    before_f = [replace(f, status="OPERATIONAL") if f.id == site.id else f for f in facilities]
    supply_before = _chain_minutes(depot_ids, items, before_f, depots, links)
    supply_now = _chain_minutes(depot_ids, items, facilities, depots, links)
    evac_before = _evac_minutes(squads, before_f, severity) if site.kind == "HOSPITAL" else None
    evac_now = _evac_minutes(squads, facilities, severity) if site.kind == "HOSPITAL" else None

    # Candidate area: the destroyed site, what it fed, what fed it and (for hospitals) the squads.
    pts = [(site.lat, site.lon)] + [(n.lat, n.lon) for n in downstream]
    pts += [(s.lat, s.lon) for s in sources if haversine_m((s.lat, s.lon), (site.lat, site.lon)) < 150_000]
    pts += squads if site.kind == "HOSPITAL" else []
    lat0, lat1 = min(p[0] for p in pts) - 0.08, max(p[0] for p in pts) + 0.08
    lon0, lon1 = min(p[1] for p in pts) - 0.12, max(p[1] for p in pts) + 0.12
    polys = [z.polygon for z in zones]
    standoff = SQUAD_STANDOFF_M.get(key, 8_000)
    rear = (sum(d.lat for d in depots) / len(depots), sum(d.lon for d in depots) / len(depots))  # launch sites sit behind the squads
    stock = ({i: q * FIRST_CONVOY_LOADS for i, q in items.items()} if site.kind != "HOSPITAL" else dict(TEMP_KIT))

    best = None
    for i in range(GRID + 1):
        for j in range(GRID + 1):
            c = (round(lat0 + (lat1 - lat0) * i / GRID, 5), round(lon0 + (lon1 - lon0) * j / GRID, 5))
            if haversine_m(c, (site.lat, site.lon)) < STRIKE_STANDOFF_M:
                continue
            if squads and (min(haversine_m(c, s) for s in squads) < standoff or not _behind(c, squads, rear)):
                continue
            if not _clear_of(polys, c):
                continue
            temp = Facility(temp_id(site.id), site.kind, TEMP_NAMES[key], c[0], c[1], stock, role=site.role,
                            beds=TEMP_BEDS.get(site.role, 0))
            new_links = [SupplyLink(s.id, temp.id, truck_min((s.lat, s.lon), c), "TRUCK") for s in sources]
            new_links += [SupplyLink(temp.id, n.id, truck_min(c, (n.lat, n.lon)), "TRUCK") for n in downstream]
            supply = _chain_minutes(depot_ids, items, facilities + [temp], depots, links + new_links)
            evac = _evac_minutes(squads, facilities + [temp], severity) if site.kind == "HOSPITAL" else None
            score = (evac if evac is not None else 0) * 10 + _total(supply) if site.kind == "HOSPITAL" else _total(supply)
            if best is None or score < best[0]:
                best = (score, temp, new_links, supply, evac)

    ms = round((time.perf_counter() - started) * 1000, 1)
    base = {"replaces": site.id, "replaces_name": site.name, "ms": ms}
    if best is None:
        return {**base, "id": None, "note": "No safe place found inside the sector for a temporary site."}
    _, temp, new_links, supply, evac = best
    now_score = (evac_now if evac_now is not None else 24 * 60) * 10 + _total(supply_now) if site.kind == "HOSPITAL" \
        else _total(supply_now)
    if best[0] > now_score * (1 - MIN_GAIN):
        return {**base, "id": None, "note": f"No temporary site needed: the re-planned network already covers "
                                            f"{site.name}."}

    landmarks = [(d.name, d.lat, d.lon) for d in depots] + [(f.name.split(" (")[0], f.lat, f.lon)
                                                           for f in facilities if f.status == "OPERATIONAL"]
    why = []
    if site.kind == "HOSPITAL":
        why.append(f"{severity} evacuation from the squads: {_hm(evac_before)} before the strike, {_hm(evac_now)} now, "
                   f"{_hm(evac)} with this site")
    names = {d.id: d.name for d in depots}
    for d in depot_ids:
        if supply[d] is not None and (supply_now[d] is None or supply[d] < supply_now[d] - 1):
            why.append(f"{names[d]} restock: {_hm(supply_now[d])} now, {_hm(supply[d])} with this site")
    feed = ", ".join(f"{s.name.split(' (')[0]} ({_hm(l.lead_time_min)} by truck)"
                     for s, l in zip(sources, new_links))
    if site.kind != "HOSPITAL":
        why.append(f"First convoy from {feed} stocks it")
    else:
        why.append(f"Arrives with its own kit and {temp.beds} beds; resupplied from {feed}")
    why.append(f"{STRIKE_STANDOFF_M / 1000:.0f}+ km from the strike, {standoff / 1000:.0f}+ km behind the squads, "
               f"clear of threat zones")
    return {**base, "id": temp.id, "kind": temp.kind, "role": temp.role, "name": temp.name,
            "lat": temp.lat, "lon": temp.lon, "where": _where((temp.lat, temp.lon), landmarks),
            "stock": temp.stock, "beds": temp.beds, "links": [l.to_dict() for l in new_links],
            "evac_min": {"before": _r(evac_before), "now": _r(evac_now), "with": _r(evac)},
            "supply_min": {d: {"before": _r(supply_before[d]), "now": _r(supply_now[d]), "with": _r(supply[d])}
                           for d in depot_ids},
            "why": why}


def _r(m: Optional[float]) -> Optional[float]:
    return None if m is None else round(m, 1)


def suggestions(repo, facilities, depots, links, items) -> list[dict]:
    """One suggestion (or note) per destroyed site that has no stand-in yet. HOOK: chain_status."""
    destroyed = [f for f in facilities if f.status == "DESTROYED"]
    if not destroyed:
        return []
    ids = {f.id for f in facilities}
    personnel, zones = repo.list_personnel(), repo.list_no_fly_zones()
    out = []
    for site in destroyed:
        if temp_id(site.id) in ids:
            continue  # already replaced
        s = suggest(site, facilities, depots, links, personnel, zones, items)
        if s:
            out.append(s)
    return out


def deploy(repo, suggestion: dict) -> tuple[Facility, list[SupplyLink]]:
    """Write the suggested temporary site and its supply links into the graph."""
    f = Facility(suggestion["id"], suggestion["kind"], suggestion["name"], suggestion["lat"], suggestion["lon"],
                 dict(suggestion["stock"]), role=suggestion["role"], beds=suggestion["beds"])
    links = [SupplyLink(l["src_id"], l["dst_id"], float(l["lead_time_min"]), l["mode"]) for l in suggestion["links"]]
    repo.add_facility(f, links)
    return f, links


# ---- API --------------------------------------------------------------------------------------------

router = APIRouter()


@router.post("/sites/deploy")
async def post_deploy(body: dict):
    """Set up the suggested stand-in for a destroyed site. Body: {"replaces": "hos-01"}."""
    from . import dev_server  # late import: dev_server includes this router
    from .messages import site_deployed_msg, supply_chain_msg
    from .supply_chain import chain_status
    world = dev_server.world
    replaces = body.get("replaces")
    t = time.perf_counter()
    s = next((x for x in chain_status(world.repo)["suggestions"] if x["replaces"] == replaces), None)
    if s is None or not s.get("id"):
        raise HTTPException(404, (s or {}).get("note") or f"no suggestion for {replaces!r}")
    facility, links = deploy(world.repo, s)
    chain = chain_status(world.repo)
    ms = round((time.perf_counter() - t) * 1000, 1)
    await dev_server.broadcast(site_deployed_msg(facility, links, replaces, ms))
    await dev_server.broadcast(supply_chain_msg(chain, {"facility_id": facility.id, "status": "DEPLOYED", "ms": ms}))
    diverted = world.evac.facility_added(facility.id)
    for m in diverted + [world.stock.message("site_deployed", f"{facility.name} set up {s['where']}",
                                             facility_id=facility.id)]:
        await dev_server.broadcast(m)
    return {"facility": facility.to_dict(), "where": s["where"], "ms": ms,
            "diverted": [m["data"]["person_id"] for m in diverted if m["type"] == "evacuation"]}
