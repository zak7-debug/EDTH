"""Safeguarding medical infrastructure: when a hub or hospital is destroyed, suggest where a temporary
replacement should go, set it up over a realistic time, and bring its supplies in.

How it fits the product:
- supply_chain.py already re-plans every launch site's restock route round a destroyed site. This module
  answers the next question: "and where do we put a temporary one, and when is it working?"
- For each destroyed distribution centre or hospital, `suggestions()` picks the best place for a stand-in:
    * a destroyed DISTRIBUTION_CENTRE -> a temporary distribution point. Scored on the launch sites'
      restock minutes.
    * a destroyed HOSPITAL -> a temporary one of the same role (a forward surgical team for Role 2, an aid
      station for Role 1). Scored on the squads' evacuation time to the nearest place that can treat them.
- Where it may go. Candidates are points inside the cover areas (seed.COVER_AREAS: woodland, and disused
  buildings on a town's outskirts) plus rings of open ground round the lost site. A candidate must be
  STRIKE_STANDOFF_M to MAX_FROM_LOST_M from the lost site (clear of the strike, but close enough to take
  over its catchment and roads), clear of every threat zone and behind the squads. Distance from the lost
  site and open ground both cost score, so a covered spot a few km away wins over a field nearer the squads.
  Cover is woodland or empty buildings on the outskirts, never inside a town: medical units rely on
  protected status, and setting up among civilians would put both at risk.
- Set-up is not instant. Deploying writes the site as SETTING_UP: the team drives in by truck from the
  nearest working base, then sets up (SETUP_MIN, plus camouflage for the cover type). Until it is READY
  the supply chain doesn't route through it and casualties aren't sent there (both need OPERATIONAL).
- Supplies come in by truck, rail and drone: the team's own kit rides with it (truck); a bulk resupply
  convoy comes down the fastest working chain from a logistics hub (truck, or rail for long hauls from a
  hub); that hub refills what it sent from the national hubs (hub to hub, by rail in this network); and
  once a hospital is open a drone flies in blood. Team, convoy and refill are orders in stock.py, so the
  dashboard shows them moving and counts them down; the drone goes through the normal dispatch engine.
- When the set-up time has passed, `finish_setup()` makes the site OPERATIONAL, re-plans the chain, diverts
  casualties for whom it is now faster and sends the drone.

The suggestions ride on the `supply_chain` message (chain_status adds them), so they show up the moment a
site is destroyed, with no extra round trip. Deploying is `POST /sites/deploy {"replaces": id}`.

Ground and truck times are straight lines times a road factor: there is no road network in the graph yet
(same caveat as evac.py). Times run on the restock clock (stock.ORDER_SPEED, 60x: one minute a second).
Searchable tags: TUNE (numbers to adjust), HOOK (integration points), DEMO (demo behaviour).
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from dataclasses import replace
from typing import Optional

from fastapi import APIRouter, HTTPException

from .models import Event, Facility, SupplyLink
from .routing import haversine_m
from .seed import COVER_AREAS, CoverArea

# TUNE: where a temporary site may go.
STRIKE_STANDOFF_M = 3_000  # away from the site that was hit: the same target may be hit again
MAX_FROM_LOST_M = 15_000  # ... but close enough to take over its catchment, roads and supply links
ZONE_BUFFER_M = 1_500  # clear of every threat zone's edge
SQUAD_STANDOFF_M = {"DISTRIBUTION_CENTRE": 15_000, "ROLE_2": 8_000, "ROLE_3": 15_000, "ROLE_1": 3_000}
COVER_POINTS = 6  # candidates inside each cover area: its centre plus this many round it
RING_STEP_M, RING_BEARINGS = 1_500, 16  # open-ground candidates: rings round the lost site

# TUNE: how candidates are scored, in minutes (lower is better), on top of evacuation or restock minutes.
KM_COST_MIN = 4.0  # each km from the lost site: the stand-in takes over its catchment and links, not moves them
OPEN_COST_MIN = 40.0  # open ground is easy to find from the air: only used when no cover will do

# TUNE: how long things take on the ground.
ROAD_FACTOR = 1.3  # roads are ~30% longer than the straight line
TRUCK_KMH = 50.0
HANDLING_MIN = 10.0  # loading and unloading per leg
CASEVAC_KMH = 40.0  # same as evac.CASEVAC_SPEED_MPS
RAIL_KMH = 45.0  # military rail, door to door including waits at junctions
RAILHEAD_MIN = 40.0  # unloading at the railhead and the last few km by truck
RAIL_MIN_KM = 60.0  # hubs further away than this can also send by rail

# TUNE: set-up, in minutes after the team arrives. Rough planning figures: an aid station is up in about
# half an hour, a light forward surgical team in about an hour and a half, a field distribution point
# (tentage, racking, cold chain for blood) in about two hours, a field hospital in most of a day.
SETUP_MIN = {"ROLE_1": 30, "ROLE_2": 90, "ROLE_3": 360, "DISTRIBUTION_CENTRE": 120}
COVER_SETUP_MIN = {"WOODLAND": 20, "STRUCTURES": 10, None: 30}  # camouflage nets and vehicle hides / clearing
# rooms and blackout / digging in and netting on open ground
COVER_TEXT = {"WOODLAND": "under tree cover", "STRUCTURES": "inside disused buildings on the outskirts"}
# Where the team comes from: the nearest working site of this kind and role.
TEAM_FROM = {"ROLE_1": ("ROLE_2", "ROLE_3"), "ROLE_2": ("ROLE_3",), "ROLE_3": ("ROLE_3",)}

# TUNE: what a temporary site gets. A hospital team brings its own kit and a few beds; a distribution
# point is stocked by its first convoy. Then a bulk resupply convoy, and a drone with blood once open.
FIRST_CONVOY_LOADS = 3  # x the standard restock order (supply_chain.RESTOCK_ORDER)
TEMP_BEDS = {"ROLE_1": 4, "ROLE_2": 8, "ROLE_3": 20}
TEMP_KIT = {"blood_oneg": 12, "tourniquet": 6, "hemostatic_gauze": 8, "chest_seal": 4, "morphine_autoinjector": 6}
RESUPPLY_KIT = {"blood_oneg": 10, "tourniquet": 10, "hemostatic_gauze": 10, "chest_seal": 6}
DRONE_KIT = {"blood_oneg": 4}  # flown in as soon as a temporary hospital opens
TEMP_NAMES = {"DISTRIBUTION_CENTRE": "Temporary distribution point", "ROLE_1": "Temporary aid station (Role 1)",
              "ROLE_2": "Forward surgical team (temporary Role 2)", "ROLE_3": "Temporary field hospital (Role 3)"}
SERVES = {"ROLE_1": "WOUNDED", "ROLE_2": "CRITICAL", "ROLE_3": "CRITICAL"}  # who the stand-in must take

FEED_RADIUS_M = 300_000  # feed the temporary site from working sites this close (else the nearest one)
MIN_GAIN = 0.10  # suggest only if it beats the re-planned network by at least 10%
HURT_MIN = 0.05  # ... and only if losing the site made evacuation or restock at least 5% slower
UP = ("OPERATIONAL",)  # a site that is SETTING_UP or DESTROYED doesn't pass stock on or take casualties


def _speed() -> float:
    from .stock import ORDER_SPEED  # late import: stock imports supply_chain, which imports this module
    return ORDER_SPEED


def temp_id(destroyed_id: str) -> str:
    return f"tmp-{destroyed_id}"


def truck_min(a: tuple[float, float], b: tuple[float, float]) -> float:
    return round(haversine_m(a, b) * ROAD_FACTOR / 1000 / TRUCK_KMH * 60 + HANDLING_MIN, 1)


def rail_min(a: tuple[float, float], b: tuple[float, float]) -> float:
    return round(haversine_m(a, b) * 1.2 / 1000 / RAIL_KMH * 60 + RAILHEAD_MIN + HANDLING_MIN, 1)


def casevac_min(a: tuple[float, float], b: tuple[float, float]) -> float:
    return haversine_m(a, b) * ROAD_FACTOR / 1000 / CASEVAC_KMH * 60


def _hm(m: Optional[float]) -> str:
    if m is None:
        return "cut off"
    h, mins = divmod(int(round(m)), 60)
    return f"{h} h {mins:02d}" if h else f"{mins} min"


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


def _offset(p, metres: float, bearing_deg: float) -> tuple[float, float]:
    b = math.radians(bearing_deg)
    return (round(p[0] + metres * math.cos(b) / 110_540.0, 5),
            round(p[1] + metres * math.sin(b) / (111_320.0 * math.cos(math.radians(p[0]))), 5))


def _candidates(site: Facility) -> list[tuple[tuple[float, float], Optional[CoverArea]]]:
    """Points inside every cover area, then rings of open ground round the lost site."""
    out = []
    for a in COVER_AREAS:
        out.append(((a.lat, a.lon), a))
        out += [(_offset((a.lat, a.lon), a.radius_m * 0.6, 360 * k / COVER_POINTS), a) for k in range(COVER_POINTS)]
    for r in range(STRIKE_STANDOFF_M, MAX_FROM_LOST_M + 1, RING_STEP_M):
        out += [(_offset((site.lat, site.lon), r, 360 * k / RING_BEARINGS), None) for k in range(RING_BEARINGS)]
    return out


def _nearest(p, facilities: list[Facility], ok) -> Optional[Facility]:
    fs = [f for f in facilities if f.status in UP and ok(f)]
    return min(fs, key=lambda f: haversine_m(p, (f.lat, f.lon))) if fs else None


def _team_from(site: Facility, c, facilities: list[Facility]) -> Optional[Facility]:
    """Where the stand-in's team and kit set off from: the nearest working site that can spare one."""
    if site.kind == "HOSPITAL":
        roles = TEAM_FROM.get(site.role, ("ROLE_3",))
        return (_nearest(c, facilities, lambda f: f.kind == "HOSPITAL" and f.role in roles)
                or _nearest(c, facilities, lambda f: f.kind == "HOSPITAL" and f.id != site.id))
    return (_nearest(c, facilities, lambda f: f.kind == "DISTRIBUTION_CENTRE")
            or _nearest(c, facilities, lambda f: f.kind != "HOSPITAL"))


def _feed_links(temp: Facility, sources: list[Facility], hub: Optional[Facility]) -> list[SupplyLink]:
    """Truck from every site that fed the lost one; a hub further than RAIL_MIN_KM can also send by rail."""
    c = (temp.lat, temp.lon)
    feeds = sources + ([hub] if hub and hub.id not in {s.id for s in sources} else [])
    links = [SupplyLink(s.id, temp.id, truck_min((s.lat, s.lon), c), "TRUCK") for s in feeds]
    links += [SupplyLink(s.id, temp.id, rail_min((s.lat, s.lon), c), "RAIL") for s in feeds
              if s.kind != "HOSPITAL" and haversine_m((s.lat, s.lon), c) > RAIL_MIN_KM * 1000]
    return links


def _convoy(temp: Facility, items: dict[str, int], facilities, depots, links) -> Optional[dict]:
    """The bulk resupply: fastest working chain from a logistics hub or supplier (hospitals keep their
    own stock for their patients; they are a source only when nothing else holds the order)."""
    from .supply_chain import best_path
    hubs_only = [replace(f, stock={}) if f.kind == "HOSPITAL" else f for f in facilities]
    path = best_path(temp.id, items, hubs_only + [temp], depots, links) or \
        best_path(temp.id, items, facilities + [temp], depots, links)
    return {**path, "items": dict(items)} if path else None


def _backfill(convoy: Optional[dict], facilities, depots, links) -> Optional[dict]:
    """The hub that sends the convoy refills the same items from the national hubs, hub to hub (rail in
    this network: bulk moves on the rail backbone, trucks do the last leg)."""
    from .supply_chain import best_path
    src = next((f for f in facilities if convoy and f.id == convoy["source_id"]), None)
    if src is None or src.kind != "DISTRIBUTION_CENTRE":
        return None
    hubs = [f if f.kind == "DISTRIBUTION_CENTRE" else replace(f, stock={}) for f in facilities]
    path = best_path(src.id, convoy["items"], hubs, depots, links)
    return {**path, "items": dict(convoy["items"])} if path else None


def suggest(site: Facility, facilities: list[Facility], depots, links: list[SupplyLink], personnel,
            zones, items: dict[str, int]) -> Optional[dict]:
    """Best place for a temporary stand-in for the destroyed `site`, or a note saying none is needed."""
    if site.kind == "SUPPLIER":
        return None  # rear suppliers sit outside the sector: re-planning round them is the answer
    started = time.perf_counter()
    key = site.role if site.kind == "HOSPITAL" else "DISTRIBUTION_CENTRE"
    up = {f.id: f for f in facilities if f.status in UP}
    sources = [up[l.src_id] for l in links if l.dst_id == site.id and l.src_id in up]
    sources = [f for f in sources if haversine_m((f.lat, f.lon), (site.lat, site.lon)) < FEED_RADIUS_M] or sources[:1]
    if not sources:  # everything that fed it is gone too: the nearest working site with stock feeds it
        stocked = [f for f in up.values() if f.kind != "HOSPITAL" or f.stock]
        sources = sorted(stocked, key=lambda f: haversine_m((f.lat, f.lon), (site.lat, site.lon)))[:1]
    hub = _nearest((site.lat, site.lon), facilities, lambda f: f.kind == "DISTRIBUTION_CENTRE")
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

    polys = [z.polygon for z in zones]
    standoff = SQUAD_STANDOFF_M.get(key, 8_000)
    rear = (sum(d.lat for d in depots) / len(depots), sum(d.lon for d in depots) / len(depots))  # launch sites sit behind the squads
    lost = (site.lat, site.lon)

    def service(evac, supply):  # what the stand-in is for: evacuation first for hospitals, restock for hubs
        return (evac if evac is not None else 24 * 60) * 10 + _total(supply) if site.kind == "HOSPITAL" else _total(supply)

    best = None
    for c, cover in _candidates(site):
        km = haversine_m(c, lost) / 1000
        if not STRIKE_STANDOFF_M <= km * 1000 <= MAX_FROM_LOST_M:
            continue
        if squads and (min(haversine_m(c, s) for s in squads) < standoff or not _behind(c, squads, rear)):
            continue
        if not _clear_of(polys, c):
            continue
        temp = Facility(temp_id(site.id), site.kind, TEMP_NAMES[key], c[0], c[1], {}, role=site.role,
                        beds=TEMP_BEDS.get(site.role, 0))
        new_links = _feed_links(temp, sources, hub)
        new_links += [SupplyLink(temp.id, n.id, truck_min(c, (n.lat, n.lon)), "TRUCK") for n in downstream]
        # Scored as if open and stocked: what it will do once ready.
        ready = replace(temp, stock=dict(TEMP_KIT) if site.kind == "HOSPITAL" else
                        {i: q * FIRST_CONVOY_LOADS for i, q in items.items()})
        supply = _chain_minutes(depot_ids, items, facilities + [ready], depots, links + new_links)
        evac = _evac_minutes(squads, facilities + [ready], severity) if site.kind == "HOSPITAL" else None
        main = (evac or 0) + _total(supply) / len(depot_ids) / 10 if site.kind == "HOSPITAL" else _total(supply) / len(depot_ids)
        score = main + KM_COST_MIN * km + (0 if cover else OPEN_COST_MIN)
        if best is None or score < best[0]:
            best = (score, temp, new_links, supply, evac, cover, km)

    ms = round((time.perf_counter() - started) * 1000, 1)
    base = {"replaces": site.id, "replaces_name": site.name, "ms": ms}
    if best is None:
        return {**base, "id": None, "note": "No safe place found near the lost site for a temporary one."}
    _, temp, new_links, supply, evac, cover, km = best
    now_score = service(evac_now, supply_now)
    hurt = now_score > service(evac_before, supply_before) * (1 + HURT_MIN)
    if not hurt or service(evac, supply) > now_score * (1 - MIN_GAIN):
        return {**base, "id": None, "note": f"No temporary site needed: the re-planned network already covers "
                                            f"{site.name}."}

    # Set-up: the team drives in, then sets up and camouflages. Then supplies.
    c = (temp.lat, temp.lon)
    team = _team_from(site, c, facilities)
    team_min = truck_min((team.lat, team.lon), c) if team else 0.0
    setup_min = SETUP_MIN[key] + COVER_SETUP_MIN[cover.kind if cover else None]
    ready_min = round(team_min + setup_min, 1)
    resupply = RESUPPLY_KIT if site.kind == "HOSPITAL" else {i: q * FIRST_CONVOY_LOADS for i, q in items.items()}
    convoy = _convoy(temp, resupply, facilities, depots, links + new_links)
    backfill = _backfill(convoy, facilities, depots, links)
    setup = {"team_from": team.id if team else None, "team_from_name": team.name if team else None,
             "team_min": team_min, "team_kit": dict(TEMP_KIT) if site.kind == "HOSPITAL" else {},
             "setup_min": setup_min, "ready_min": ready_min, "ready_in_s": round(ready_min * 60 / _speed(), 1)}

    landmarks = [(d.name, d.lat, d.lon) for d in depots] + [(f.name.split(" (")[0], f.lat, f.lon)
                                                           for f in facilities if f.status in UP]
    lost_name = site.name.split(" (")[0]
    why = []
    if site.kind == "HOSPITAL":
        why.append(f"{severity} evacuation from the squads: {_hm(evac_before)} before the strike, {_hm(evac_now)} now, "
                   f"{_hm(evac)} once this site is open")
    names = {d.id: d.name for d in depots}
    for d in depot_ids:
        if supply[d] is not None and (supply_now[d] is None or supply[d] < supply_now[d] - 1):
            why.append(f"{names[d]} restock: {_hm(supply_now[d])} now, {_hm(supply[d])} once this site is open")
    why.append(f"{km:.0f} km from the lost {lost_name}, {COVER_TEXT[cover.kind]} ({cover.name.split(' (')[0].lower()})"
               if cover else f"{km:.0f} km from the lost {lost_name}, on open ground: no cover within "
                             f"{MAX_FROM_LOST_M / 1000:.0f} km")
    team_text = f"team drives from {team.name.split(' (')[0]} ({_hm(team_min)})" if team else "team on site"
    why.append(f"Ready in {_hm(ready_min)}: {team_text}, then {_hm(setup_min)} to set up"
               + (" and camouflage" if cover else " and dig in"))
    if convoy:
        modes = " + ".join(dict.fromkeys(l["mode"].lower() for l in convoy["legs"]))
        src = next((f.name for f in facilities if f.id == convoy["source_id"]), convoy["source_id"])
        why.append(f"Resupply by {modes} from {src} ({_hm(convoy['minutes'])})"
                   + (f", then a drone with {DRONE_KIT['blood_oneg']} blood once open" if site.kind == "HOSPITAL" else ""))
    if backfill:
        modes = " + ".join(dict.fromkeys(l["mode"].lower() for l in backfill["legs"]))
        src = next((f.name for f in facilities if f.id == backfill["source_id"]), backfill["source_id"])
        why.append(f"{next(f.name for f in facilities if f.id == convoy['source_id']).split(' (')[0]} refills "
                   f"by {modes} from {src} ({_hm(backfill['minutes'])})")
    why.append(f"{STRIKE_STANDOFF_M / 1000:.0f}+ km from the strike, {standoff / 1000:.0f}+ km behind the squads, "
               f"clear of threat zones")
    return {**base, "id": temp.id, "kind": temp.kind, "role": temp.role, "name": temp.name,
            "lat": temp.lat, "lon": temp.lon, "where": _where(c, landmarks), "km_from_lost": round(km, 1),
            "cover": cover.to_dict() if cover else None, "stock": temp.stock, "beds": temp.beds,
            "links": [l.to_dict() for l in new_links], "setup": setup, "convoy": convoy, "backfill": backfill,
            "drone": {"items": dict(DRONE_KIT)} if site.kind == "HOSPITAL" else None,
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
    for site in sorted(destroyed, key=lambda f: f.kind != "HOSPITAL"):  # lives first: hospitals at the top
        if temp_id(site.id) in ids:
            continue  # already replaced
        s = suggest(site, facilities, depots, links, personnel, zones, items)
        if s:
            out.append(s)
    return out


def deploy(repo, suggestion: dict) -> tuple[Facility, list[SupplyLink]]:
    """Write the suggested temporary site (SETTING_UP, empty: its kit is still on the road) and its links."""
    f = Facility(suggestion["id"], suggestion["kind"], suggestion["name"], suggestion["lat"], suggestion["lon"],
                 {}, role=suggestion["role"], beds=suggestion["beds"], status="SETTING_UP")
    links = [SupplyLink(l["src_id"], l["dst_id"], float(l["lead_time_min"]), l["mode"]) for l in suggestion["links"]]
    repo.add_facility(f, links)
    return f, links


def start_supplies(stock, suggestion: dict) -> list[dict]:
    """The team (with its kit) and the bulk convoy, as restock orders the dashboard shows moving."""
    s, out = suggestion["setup"], []
    if s["team_from"]:
        team = {"source_id": s["team_from"], "path": [s["team_from"], suggestion["id"]], "minutes": s["team_min"],
                "legs": [{"src_id": s["team_from"], "dst_id": suggestion["id"], "mode": "TRUCK", "minutes": s["team_min"]}]}
        out += stock.send(suggestion["id"], s["team_kit"], team, kind="TEAM", take_from_source=False,
                          note=f"{suggestion['name']}: team and kit left {s['team_from_name']}, "
                               f"{_hm(s['team_min'])} by truck")
    c = suggestion.get("convoy")
    if c:
        out += stock.send(suggestion["id"], c["items"], c, kind="CONVOY")
    b = suggestion.get("backfill")
    if b:
        out += stock.send(b["path"][-1], b["items"], b, kind="BACKFILL")
    return out


# ---- set-up and opening -----------------------------------------------------------------------------

def _setups(world) -> dict:
    """Temporary sites still setting up in this world: facility id -> {ready_at, ...}. Lives on the World,
    so a reset forgets them."""
    return world.__dict__.setdefault("setups", {})


def setups_state(world) -> list[dict]:
    now = world.stock.clock()
    return [{**v, "remaining_s_real": round(max(0.0, v["ready_at"] - now), 1)} for v in _setups(world).values()]


async def finish_setup(world, facility_id: str) -> list[dict]:
    """The set-up time has passed: open the site. Returns the /ws messages, already broadcast."""
    from . import dev_server
    from .messages import site_ready_msg, supply_chain_msg
    from .supply_chain import chain_status
    _setups(world).pop(facility_id, None)
    f = next((x for x in world.repo.list_facilities() if x.id == facility_id), None)
    if f is None or f.status != "SETTING_UP":
        return []  # destroyed (or reset) while setting up
    t = time.perf_counter()
    world.repo.set_facility_status(facility_id, "OPERATIONAL")
    chain = chain_status(world.repo)
    ms = round((time.perf_counter() - t) * 1000, 1)
    out = _send_drone(world, f) if f.kind == "HOSPITAL" and DRONE_KIT else []  # first, so "open" is the last alert
    out += [site_ready_msg(replace(f, status="OPERATIONAL"), ms),
            supply_chain_msg(chain, {"facility_id": facility_id, "status": "READY", "ms": ms})]
    out += world.evac.facility_added(facility_id)  # casualties for whom it is now faster switch to it
    out.append(world.stock.message("site_ready", f"{f.name} is open: taking casualties and stock",
                                   facility_id=facility_id))
    for m in out:
        await dev_server.broadcast(m)
    return out


def _send_drone(world, f: Facility) -> list[dict]:
    """Blood by drone the moment a temporary hospital opens, through the normal dispatch engine."""
    from .messages import dispatch_msg, no_dispatch_msg, queue_msg
    from .models import Dispatch
    event = Event(f"{f.id}-open-{int(world.stock.clock())}", "LOW_STOCK", f.id, f.lat, f.lon, world.stock.clock(),
                  items=dict(DRONE_KIT))
    result = world.engine.handle(event, time.perf_counter())
    world.engine.record(result)
    if isinstance(result, Dispatch):
        world.tracker.start(result)
    return [dispatch_msg(result) if isinstance(result, Dispatch) else no_dispatch_msg(result),
            queue_msg(world.engine.pending())]


async def _open_when_ready(world, facility_id: str, delay_s: float) -> None:
    await asyncio.sleep(delay_s)
    from . import dev_server
    if dev_server.world is world:  # not reset in the meantime
        try:
            await finish_setup(world, facility_id)
        except Exception as e:  # never let a late set-up crash the server mid-demo
            print("set-up failed:", facility_id, e)


# ---- API --------------------------------------------------------------------------------------------

router = APIRouter()


@router.post("/sites/deploy")
async def post_deploy(body: dict):
    """Send the team for the suggested stand-in of a destroyed site. Body: {"replaces": "hos-01"}.
    The site is SETTING_UP until its set-up time has passed (on the restock clock), then opens."""
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
    ready_s = round(s["setup"]["ready_min"] * 60 / world.stock.speed, 1)
    _setups(world)[facility.id] = {"facility_id": facility.id, "replaces": replaces, "ready_min": s["setup"]["ready_min"],
                                   "ready_at": world.stock.clock() + ready_s}
    await dev_server.broadcast(site_deployed_msg(facility, links, replaces, ms, where=s["where"], cover=s["cover"],
                                                 setup={**s["setup"], "ready_in_s": ready_s}, convoy=s["convoy"],
                                                 backfill=s["backfill"], drone=s["drone"]))
    await dev_server.broadcast(supply_chain_msg(chain, {"facility_id": facility.id, "status": "SETTING_UP", "ms": ms}))
    for m in start_supplies(world.stock, s) + [world.stock.message(
            "site_deployed", f"{facility.name} setting up {s['where']}: ready in {_hm(s['setup']['ready_min'])}",
            facility_id=facility.id)]:
        await dev_server.broadcast(m)
    asyncio.create_task(_open_when_ready(world, facility.id, ready_s))
    return {"facility": facility.to_dict(), "where": s["where"], "ms": ms, "ready_in_s": ready_s,
            "setup": s["setup"], "convoy": s["convoy"], "backfill": s["backfill"]}


@router.post("/sites/ready")
async def post_ready(body: dict):
    """Open a temporary site now instead of waiting out its set-up time (rehearsals and tests).
    Body: {"facility_id": "tmp-hos-01"}."""
    from . import dev_server
    world = dev_server.world
    if body.get("facility_id") not in _setups(world):
        raise HTTPException(404, f"{body.get('facility_id')!r} is not setting up")
    out = await finish_setup(world, body["facility_id"])
    return {"ok": bool(out), "diverted": [m["data"]["person_id"] for m in out if m["type"] == "evacuation"]}
