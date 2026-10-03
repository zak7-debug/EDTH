"""Web layer for audio geolocation: the POST /events hook, the /zones endpoints and zone WebSocket messages.

    POST /events           types NO_FLY_ZONE / NO_GO_AREA / ROAD_BLOCKED / DESTINATION, with
                           distance_m + bearing_deg (+ radius_m), or text to parse them from.
                           The reporter is `source` {lat, lon, accuracy_m?, heading_deg?, heading_ref?,
                           user_id?}, else the subject_id's position in the graph.
    GET  /zones            active spoken-report zones as a GeoJSON FeatureCollection
    GET  /zones/{id}
    DELETE /zones/{id}     operator override
    WS   /ws               zone_created / zone_updated / zone_expired (plus zone_added for air zones,
                           so the dashboard's existing threat drawing and reroute log keep working)

How it reaches dispatch: air zones (NO_FLY_ZONE) are written to the graph as NoFlyZone nodes, exactly
like POST /threats, so the router treats them as hard constraints and drones in the air reroute. Ground
zones (NO_GO_AREA) and blocked road segments go to roads.set_ground_constraints(), so evacuations and
truck legs avoid them while drones don't.
"""
from __future__ import annotations

import itertools
import time
from typing import Optional

from fastapi import APIRouter, HTTPException

from .. import roads
from ..messages import geo_zone_msg, zone_added_msg
from .service import GEO_TYPES, GeoError, GeoService
from .zones import GeoZone

router = APIRouter()
_ids = itertools.count(1)


def _dev():
    from .. import dev_server  # late import: dev_server includes this router
    return dev_server


def new_service() -> GeoService:
    """HOOK: dev_server.World builds one per reset; also clears the road constraints of the last run."""
    roads.set_ground_constraints([], [])
    return GeoService()


def _reporter(raw: dict, world) -> tuple[tuple[float, float], Optional[str]]:
    src = raw.get("source") or {}
    if "lat" in src and "lon" in src:
        return (float(src["lat"]), float(src["lon"])), src.get("user_id") or raw.get("subject_id")
    if raw.get("subject_id"):
        person = world.repo.get_person(raw["subject_id"])
        if person is None:
            raise HTTPException(404, f"unknown subject_id {raw['subject_id']!r}")
        return (person.lat, person.lon), person.id
    if "lat" in raw and "lon" in raw:
        return (float(raw["lat"]), float(raw["lon"])), None
    raise HTTPException(422, "need the reporter's position: source {lat, lon} or subject_id")


def _ground(world):
    """Threat zones plus the dashboard's road blocks (blocks.py), so a spoken report never reopens them."""
    from ..blocks import ground_zones
    return ground_zones(world.repo)


def _sync_ground(world) -> None:
    store = world.geo.store
    roads.set_ground_constraints([z.no_fly_zone() for z in store.ground_zones()], store.blocked_edges())
    world.evac.roads = roads.net_for(_ground(world))


async def _apply(world, action: str, zone: GeoZone) -> list[str]:
    """Push one created / updated zone into routing and onto every screen. Returns who rerouted."""
    dev = _dev()
    types = world.geo.cfg["types"][zone.kind]
    nfz = zone.no_fly_zone()
    reroutes: list[dict] = []
    if types.get("air"):
        world.repo.add_no_fly_zone(nfz)  # graph first, so every later decision sees it
        world.engine.zones_changed()
        reroutes = world.tracker.reroute(nfz) + world.evac.reroute(nfz)
        await dev.broadcast(zone_added_msg(nfz))
    else:
        _sync_ground(world)
        reroutes = world.evac.reroute(nfz)  # casualties on the road detour; drones are unaffected
    await dev.broadcast(geo_zone_msg("zone_created" if action == "created" else "zone_updated", zone.feature()))
    for m in reroutes:
        await dev.broadcast(m)
    return [m["data"].get("drone_id") or m["data"].get("person_id") for m in reroutes]


async def _remove(world, zone: GeoZone, reason: str) -> None:
    if world.geo.cfg["types"][zone.kind].get("air"):
        world.repo.remove_no_fly_zone(zone.id)
        world.engine.zones_changed()
        world.evac.roads = roads.net_for(_ground(world))
    else:
        _sync_ground(world)
    await _dev().broadcast(geo_zone_msg("zone_expired", zone.feature(), reason))


async def handle_event(raw: dict) -> dict:
    """HOOK: dev_server.process sends every geo event type here."""
    world = _dev().world
    reporter, reporter_id = _reporter(raw, world)
    event_id = raw.get("event_id") or raw.get("id") or f"geo-{int(time.time())}-{next(_ids)}"
    start = time.perf_counter()
    try:
        body, changes = world.geo.handle(
            raw, reporter, event_id=event_id, reporter_id=reporter_id,
            roadnet=lambda: roads.net_for(_ground(world)),
            drop_points=lambda: [(p.id, (p.lat, p.lon)) for p in world.repo.list_personnel()
                                 if p.kind == "MEDIC"] + [(d.id, (d.lat, d.lon)) for d in world.repo.list_depots()])
    except GeoError as e:
        raise HTTPException(422, str(e))
    rerouted = []
    for action, zone in changes:
        rerouted += await _apply(world, action, zone)
    body["rerouted"] = rerouted
    body["ms"] = round((time.perf_counter() - start) * 1000, 1)
    return body


async def expire_due(world, now: Optional[float] = None) -> None:
    """HOOK: the dev_server tick loop calls this; drops zones past expires_at."""
    for zone in world.geo.store.expire(now):
        await _remove(world, zone, "expired")


@router.get("/zones")
def get_zones():
    return _dev().world.geo.store.feature_collection()


@router.get("/zones/{zone_id}")
def get_zone(zone_id: str):
    zone = _dev().world.geo.store.get(zone_id)
    if zone is None:
        raise HTTPException(404, f"no active zone {zone_id!r}")
    return zone.feature()


@router.delete("/zones/{zone_id}")
async def delete_zone(zone_id: str):
    world = _dev().world
    zone = world.geo.store.delete(zone_id)
    if zone is None:
        raise HTTPException(404, f"no active zone {zone_id!r}")
    await _remove(world, zone, "operator")
    return {"deleted": zone_id}
