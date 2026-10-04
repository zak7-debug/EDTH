"""Working API for the dashboard: POST /events, /ws, the tick loop and the demo scenario.

How it fits the product:
- Written while Sasank's main.py is still a stub, so the dashboard (frontend/index.html) runs end to
  end today. It follows contracts/messages.md exactly, so the frontend can't tell the two apart.
  Sasank can lift any part of it into main.py, or keep it and add the simulator on top.
- Run it with ./start.sh (default app) or:
    uvicorn backend.app.dev_server:app --port 8000            (EDTH_REPO=memory for no database)
- Flow of one request: POST /events -> broadcast `event` -> engine.handle (the timed decision)
  -> broadcast `dispatch` / `no_dispatch` -> engine.record (graph writes, off the latency path)
  -> FlightTracker flies the drone, ticking `drone_update` -> `delivered` -> return -> reload
  from the launch site's stock (stock.py, `stock_update`) -> queue drains.
- A casualty also starts an evacuation (evac.py): `evacuation` -> `evac_update` ticks -> `admitted`,
  with a drone flying any missing kit to the destination at the same time.
- GET /tiles/{z}/{x}/{y}.png serves the map background from a local cache, fetching and saving
  tiles it doesn't have yet, so the map works offline once scripts/fetch_tiles.py has run.

Searchable tags: TUNE (numbers to adjust), HOOK (integration points), DEMO (demo behaviour).
"""
from __future__ import annotations

import asyncio
import itertools
import math
import os
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .dispatch import DispatchEngine
from .evac import EvacTracker
from .flights import FlightTracker
from . import blocks, gps, querylog, resilience, tiles, voice
from .geo import api as geo_api
from .messages import (dispatch_msg, event_msg, no_dispatch_msg, query_log_msg, queue_msg, snapshot,
                       drone_lost_msg, positions_msg, supply_chain_msg, zone_added_msg)
from .models import Dispatch, Event, NoFlyZone
from .repo import get_repo
from .stock import StockKeeper
from .supply_chain import chain_status

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
TICK_S = 0.5  # TUNE: how often drones move on the map (contract says about 2 updates per second)
AUTO_EVACUATE = True  # TUNE: every casualty event also starts an evacuation (evac.py)
# TUNE: drones deliver to medics only. A CASUALTY event sets the soldier's status (shown on the map) and
# starts their evacuation; the squad medic treats them and asks for a restock (LOW_STOCK) if short.
CASUALTY_DRONES = False
# TUNE: map background. Tiles are cached under frontend/tiles/{z}/{x}/{y}.png; anything missing is
# fetched once from TILE_UPSTREAM and saved. scripts/fetch_tiles.py fills the cache for offline use.
TILE_DIR = Path(os.environ.get("EDTH_TILE_DIR", FRONTEND / "tiles"))
TILE_UPSTREAM = os.environ.get("EDTH_TILE_UPSTREAM", "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png")

# DEMO: the scripted scenario behind the "Run demo scenario" button. (seconds after start, event).
# Story: casualties come in and each squad medic calls for a restock with its urgency (drones deliver
# to medics only; a casualty report sets the soldier's status and starts their evacuation, which leaves
# once the medic has treated them). One restock forces a detour round the EW jamming zone, two land at
# the same instant (different drones), a new threat makes a drone in the air change course, a drone is
# shot down and its delivery retried, and a non-urgent restock waits in the queue for a drone coming
# home. Then the forward hub and the Role 2 hospital are hit.
DEMO_SCRIPT = [
    (0.0, {"type": "CASUALTY", "subject_id": "sol-03", "severity": "CRITICAL"}),
    (1.0, {"type": "LOW_STOCK", "subject_id": "med-1", "items": {"blood_oneg": 2, "tourniquet": 1},
           "urgency": "CRITICAL"}),
    (5.0, {"type": "CASUALTY", "subject_id": "sol-10", "severity": "CRITICAL"}),
    (5.5, {"type": "LOW_STOCK", "subject_id": "med-2", "items": {"blood_oneg": 2}, "urgency": "CRITICAL"}),
    (7.0, {"type": "THREAT"}),  # DEMO: new threat on FALCON 1's path; it reroutes mid-flight
    (10.0, {"type": "CASUALTY", "subject_id": "sol-15", "severity": "WOUNDED"}),
    (10.0, {"type": "LOW_STOCK", "subject_id": "med-3", "items": {"chest_seal": 2}, "urgency": "URGENT"}),
    (16.0, {"type": "CASUALTY", "subject_id": "sol-16", "severity": "CRITICAL"}),
    (16.5, {"type": "LOW_STOCK", "subject_id": "med-3", "items": {"blood_oneg": 2, "hemostatic_gauze": 1},
            "urgency": "CRITICAL"}),
    (19.0, {"type": "DRONE_LOST", "drone_id": "drn-01"}),  # DEMO: HAWK 1 shot down on its way to a medic
    (22.0, {"type": "CASUALTY", "subject_id": "sol-05", "severity": "CRITICAL"}),
    (22.5, {"type": "LOW_STOCK", "subject_id": "med-1", "items": {"hemostatic_gauze": 2, "tourniquet": 1},
            "urgency": "URGENT"}),
    (23.0, {"type": "CASUALTY", "subject_id": "sol-12", "severity": "WOUNDED"}),
    (23.5, {"type": "LOW_STOCK", "subject_id": "med-2", "items": {"chest_seal": 1, "morphine_autoinjector": 2},
            "urgency": "NON_URGENT"}),
    (27.0, {"type": "SITE", "facility_id": "dc-02", "status": "DESTROYED"}),  # DEMO: forward hub hit
    # DEMO: the Role 2 hospital is hit with CRITICAL casualties on the way: they divert to the Role 3 hours
    # away, and a forward surgical team is suggested. Press "Send team" on the map (resilience.py).
    (32.0, {"type": "SITE", "facility_id": "hos-01", "status": "DESTROYED"}),
]


LOSS_THREAT_RADIUS_M = 600  # TUNE: size of the zone drawn where a drone was shot down

# DEMO: the threat reported mid-scenario (fictional), between Launch Site West and BADGER 1.
DEMO_THREAT = {"name": "New air-defence threat", "lat": 47.6498, "lon": 35.5888, "radius_m": 900}


def _hexagon(lat: float, lon: float, radius_m: float) -> list[tuple[float, float]]:
    """Six corners round a centre: how a reported threat (point + radius) becomes a zone polygon."""
    dlat = radius_m / 110_540.0
    dlon = radius_m / (111_320.0 * math.cos(math.radians(lat)))
    return [(round(lat + dlat * math.sin(math.radians(a)), 6), round(lon + dlon * math.cos(math.radians(a)), 6))
            for a in range(0, 360, 60)]


class World:
    """Everything that gets rebuilt on POST /reset."""

    def __init__(self):
        blocks.BLOCKS.clear()  # road and rail blocks belong to one run (blocks.py)
        self.repo = get_repo()  # HOOK: EDTH_REPO=turing|memory picks the store
        self.geo = geo_api.new_service()  # zones from spoken reports (geo/); first: clears last run's road blocks
        self.engine = DispatchEngine(self.repo)  # routes round threat zones by default
        self.stock = StockKeeper(self.repo)  # launch-site stock and restock orders
        self.tracker = FlightTracker(self.engine, stock=self.stock)
        self.evac = EvacTracker(self.engine, self.tracker, self.stock)
        self.gps = gps.LiveGps()  # simulated GPS feed and field reports, off until POST /geo/live
        self.stock.check_all()  # DEMO: Launch Site West starts short of blood, so it reorders straight away


world = World()
clients: set[WebSocket] = set()
_ids = itertools.count(1)
_scenario: Optional[asyncio.Task] = None


@asynccontextmanager
async def _lifespan(_app):
    ticker = asyncio.create_task(_tick_loop())  # drones move while the server runs
    yield
    ticker.cancel()


app = FastAPI(title="EDTH medical resupply (dev server)", lifespan=_lifespan)


async def broadcast(message: dict) -> None:
    for ws in list(clients):
        try:
            await ws.send_json(message)
        except Exception:
            clients.discard(ws)


def _complete(d: dict) -> Event:
    """Accept partial events (the dashboard's trigger panel sends only type/subject/severity/items):
    fill event_id, ts and the subject's current position from the graph."""
    if "subject_id" not in d or "type" not in d:
        raise HTTPException(422, "event needs type and subject_id")
    person = world.repo.get_person(d["subject_id"])
    if person is None:
        raise HTTPException(404, f"unknown subject_id {d['subject_id']!r}")
    d = {"event_id": f"evt-{int(time.time())}-{next(_ids)}", "ts": time.time(),
         "lat": person.lat, "lon": person.lon, **d}
    return Event.from_dict(d)


async def add_threat(body: dict) -> dict:
    """A threat reported mid-mission: store it in the graph, then reroute.
    Body: {"name", "polygon": [[lat, lon], ...]} or {"name", "lat", "lon", "radius_m"}."""
    poly = body.get("polygon") or _hexagon(float(body["lat"]), float(body["lon"]), float(body.get("radius_m", 800)))
    zone = NoFlyZone(body.get("id") or f"nfz-live-{next(_ids)}", body.get("name", "Reported threat"),
                     [tuple(p) for p in poly])
    t = time.perf_counter()
    world.repo.add_no_fly_zone(zone)  # graph first, so every later decision sees it
    world.engine.zones_changed()  # new decisions route round it
    reroutes = world.tracker.reroute(zone) + world.evac.reroute(zone)  # drones and casualties on the move detour
    ms = (time.perf_counter() - t) * 1000
    await broadcast(zone_added_msg(zone))
    for m in reroutes:
        await broadcast(m)
    return {"zone": zone.to_dict(), "rerouted": [m["data"].get("drone_id") or m["data"].get("person_id")
                                                 for m in reroutes], "ms": round(ms, 1)}


async def lose_drone(drone_id: str) -> dict:
    """A drone is shot down: write it off, mark the spot as a threat, re-send the casualty's supplies."""
    drone = world.repo.get_drone(drone_id)
    if drone is None:
        raise HTTPException(404, f"unknown drone {drone_id!r}")
    if drone.status == "LOST":
        return {"ok": False, "msg": "already lost"}
    where = world.tracker.lose(drone_id) or {"lat": drone.lat, "lon": drone.lon, "phase": drone.status,
                                             "request_id": None, "recipient_id": None}
    world.repo.update_drone(drone_id, lat=where["lat"], lon=where["lon"])
    lost_items = {k: v for k, v in drone.payload.items() if v}
    await broadcast(drone_lost_msg(drone_id, where["lat"], where["lon"], where["phase"], where["request_id"],
                                   where["recipient_id"], lost_items))
    # TUNE: the loss spot becomes a threat zone so nothing else flies into the same fire.
    await add_threat({"name": f"Suspected shoot-down ({drone.callsign})", "lat": where["lat"],
                      "lon": where["lon"], "radius_m": LOSS_THREAT_RADIUS_M})
    retry_request = where["request_id"] if where["phase"] == "EN_ROUTE" else None  # returning drones were empty-handed
    t = time.perf_counter()
    result = world.engine.drone_lost(drone_id, retry_request)
    if result is not None:
        await broadcast(dispatch_msg(result) if isinstance(result, Dispatch) else no_dispatch_msg(result))
        world.engine.record(result)
        if isinstance(result, Dispatch):
            world.tracker.start(result)
    await broadcast(queue_msg(world.engine.pending()))
    return {"ok": True, "retry": result.to_dict() if result else None, "ms": round((time.perf_counter() - t) * 1000, 1)}


async def set_site(facility_id: str, status: str) -> dict:
    """A hub, hospital or supplier is destroyed (or back in service): store it, re-plan every chain."""
    if status not in ("DESTROYED", "OPERATIONAL"):
        raise HTTPException(422, "status must be DESTROYED or OPERATIONAL")
    if facility_id not in {f.id for f in world.repo.list_facilities()}:
        raise HTTPException(404, f"unknown facility {facility_id!r}")
    t = time.perf_counter()
    world.repo.set_facility_status(facility_id, status)
    chain = chain_status(world.repo)
    ms = (time.perf_counter() - t) * 1000
    await broadcast(supply_chain_msg(chain, {"facility_id": facility_id, "status": status, "ms": round(ms, 1)}))
    # Shipments still to pass through the site are re-sent another way; casualties heading there are diverted.
    for m in world.stock.site_changed(facility_id, status) + world.evac.site_changed(facility_id, status):
        await broadcast(m)
    return {**chain, "ms": round(ms, 1)}


@app.post("/sites")
async def post_site(body: dict):
    """HOOK: mark a supply-chain site destroyed or restored. Body: {"facility_id", "status"}."""
    return await set_site(body["facility_id"], body.get("status", "DESTROYED"))


@app.post("/losses")
async def post_loss(body: dict):
    """HOOK: report a drone lost (dashboard button, scenario, or real telemetry). Body: {"drone_id"}."""
    return await lose_drone(body["drone_id"])


@app.post("/threats")
async def post_threat(body: dict):
    """HOOK: report a new threat zone (dashboard button, scenario, or real intel feed)."""
    return await add_threat(body)


async def process(raw: dict, received_perf: float) -> dict:
    if raw.get("type") == "SITE":  # DEMO: scripted strikes on the supply chain
        return await set_site(raw["facility_id"], raw.get("status", "DESTROYED"))
    if raw.get("type") == "DRONE_LOST":  # DEMO: scripted losses share the event timeline
        return await lose_drone(raw["drone_id"])
    if raw.get("type") in geo_api.GEO_TYPES:  # spoken position -> zone / destination (geo/)
        return await geo_api.handle_event(raw)
    if raw.get("type") == "THREAT":  # DEMO: scripted threats share the event timeline
        return await add_threat({k: v for k, v in raw.items() if k != "type"} or dict(DEMO_THREAT))
    event = _complete(raw)
    await broadcast(event_msg(event, time.time()))
    if event.type == "CASUALTY" and not CASUALTY_DRONES:
        world.engine.record(None, event)  # status only: CRITICAL / WOUNDED
        msgs = []
        if AUTO_EVACUATE and event.severity:
            person = world.repo.get_person(event.subject_id)
            if person is not None and person.kind == "SOLDIER":
                msgs = world.evac.start(person, event.severity)  # treated by the squad medic, then by road
        for m in msgs:
            await broadcast(m)
        return {"request_id": event.event_id, "subject_id": event.subject_id, "status": event.severity,
                "evacuation": msgs[0]["data"] if msgs else None}
    with querylog.capture() as decide_q:
        result = world.engine.handle(event, received_perf)  # the timed decision
    await broadcast(dispatch_msg(result) if isinstance(result, Dispatch) else no_dispatch_msg(result))
    with querylog.capture() as record_q:
        world.engine.record(result, event)  # graph writes after the broadcast
    await broadcast(query_log_msg(event.event_id, "decide", decide_q))
    await broadcast(query_log_msg(event.event_id, "record", record_q))
    if isinstance(result, Dispatch):
        world.tracker.start(result)
    await broadcast(queue_msg(world.engine.pending()))
    if AUTO_EVACUATE and event.type == "CASUALTY" and event.severity:
        person = world.repo.get_person(event.subject_id)
        if person is not None and person.kind == "SOLDIER":
            for m in world.evac.start(person, event.severity):  # evacuation + kit flown ahead
                await broadcast(m)
    return result.to_dict()


@app.post("/positions")
async def post_positions(body: dict):
    """HOOK: GPS fixes from devices. Body: {"positions": [{"id", "lat", "lon", "accuracy_m"?}]}.
    Moves each known person and broadcasts one `positions` message; unknown ids are skipped."""
    fixes = body.get("positions")
    if not isinstance(fixes, list):
        raise HTTPException(422, "body needs a positions list")
    moved, skipped = gps.apply_positions(world.repo, fixes)
    if moved:
        await broadcast(positions_msg(moved))
    return {"moved": [m["id"] for m in moved], "skipped": skipped}


@app.get("/geo/live")
def get_geo_live():
    return world.gps.state()


@app.post("/geo/live")
async def post_geo_live(body: dict):
    """DEMO: turn the simulated GPS feed (gps.py) on or off. Body: {"on": true|false}."""
    return world.gps.set(bool(body.get("on")), world.repo.list_personnel())


def _busy_people(w) -> set[str]:
    """Medics with a drone on the way stay put, so it lands where they are."""
    return {f.dispatch.recipient_id for f in w.tracker.flights.values() if getattr(f, "dispatch", None)}


async def live_gps_step(w, step: bool = True, report: bool = False) -> dict:
    """One beat of the simulated feed: move a few people, maybe file a geolocated field report.
    The report goes through process() like a spoken one, so it becomes a zone (geo/)."""
    out: dict = {"moved": [], "report": None}
    if step:
        out["moved"] = w.gps.step(w.repo, _busy_people(w))
        if out["moved"]:
            await broadcast(positions_msg(out["moved"]))
    if report:
        for _ in range(4):  # a road block needs a road within reach: try another spot if there isn't one
            ev = w.gps.make_report(w.repo)
            if ev is None:
                break
            try:
                out["report"] = await process(ev, time.perf_counter())
                break
            except HTTPException:
                w.gps.reports -= 1  # same kind again from elsewhere
    return out


@app.post("/events")
async def post_event(body: dict):
    received = time.perf_counter()  # HOOK: latency_ms starts here
    return await process(body, received)


@app.get("/state")
def state():
    return snapshot(world.repo)["data"]


@app.get("/health")
def health():
    return {"ok": True, "repo": type(world.repo).__name__, "app": "dev_server"}


@app.post("/reset")
async def reset():
    """Reseed the world (between rehearsals) and push a fresh snapshot to every screen."""
    global world, _scenario
    if _scenario:
        _scenario.cancel()
    world = World()
    await broadcast(snapshot(world.repo))
    await broadcast(supply_chain_msg(chain_status(world.repo)))
    await broadcast(world.stock.message())
    await broadcast(queue_msg(world.engine.pending()))  # fix: panel kept the old list after a restart
    return {"ok": True}


@app.post("/scenario/{name}")
async def scenario(name: str):
    global _scenario
    if name != "demo":
        raise HTTPException(404, "only 'demo' exists")
    if _scenario and not _scenario.done():
        return {"ok": False, "msg": "scenario already running"}

    await reset()  # every run starts from the same seeded world

    async def run():
        start = time.monotonic()
        for at, ev in DEMO_SCRIPT:
            await asyncio.sleep(max(0.0, at - (time.monotonic() - start)))
            try:
                await process(dict(ev), time.perf_counter())
            except Exception as e:  # one bad event must not stall the rest of the demo
                print("scenario event failed:", ev, e)

    _scenario = asyncio.create_task(run())
    return {"ok": True, "events": len(DEMO_SCRIPT)}


@app.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    clients.add(socket)
    await socket.send_json(snapshot(world.repo))
    await socket.send_json(queue_msg(world.engine.pending()))
    await socket.send_json(supply_chain_msg(chain_status(world.repo)))
    await socket.send_json(world.stock.message())
    try:
        while True:
            await socket.receive_text()  # clients don't send anything; this just waits for close
    except WebSocketDisconnect:
        clients.discard(socket)


async def _tick_loop():
    last = time.monotonic()
    while True:
        await asyncio.sleep(TICK_S)
        now = time.monotonic()
        try:
            w = world  # a reset mid-tick swaps the world; finish this tick on the old one
            for m in w.tracker.step(now - last) + w.evac.step(now - last):  # flights, shipments, evacuations
                await broadcast(m)
            await geo_api.expire_due(w)  # spoken-report zones past their expiry
            step, report = w.gps.due(now)  # simulated GPS feed (gps.py), when on
            if step or report:
                await live_gps_step(w, step, report)
        except Exception as e:  # never let one bad tick kill the loop mid-demo
            print("tick error:", e)
        last = now


_tile_misses: set[str] = set()


def _fetch_tile(z: int, x: int, y: int) -> bytes:
    url = TILE_UPSTREAM.format(s="abcd"[(x + y) % 4], z=z, x=x, y=y)
    req = urllib.request.Request(url, headers={"User-Agent": "EDTH-hackathon-demo/1.0 (tile cache)"})
    with urllib.request.urlopen(req, timeout=6) as r:  # TUNE: give up quickly when offline
        return r.read()


@app.get("/tiles/{z}/{x}/{y}.png")
async def tile(z: int, x: int, y: int):
    """HOOK: the dashboard's map background. Cached tile if we have it, else fetched once and saved.
    404 when offline and not cached: the dashboard then shows its offline backdrop instead."""
    path = TILE_DIR / str(z) / str(x) / f"{y}.png"
    if not path.exists():
        key = f"{z}/{x}/{y}"
        if key in _tile_misses:
            raise HTTPException(404, "tile not cached and upstream unreachable")
        try:
            data = await asyncio.to_thread(_fetch_tile, z, x, y)
        except Exception:
            _tile_misses.add(key)  # don't retry the same tile on every pan while offline
            raise HTTPException(404, "tile not cached and upstream unreachable")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return Response(data, media_type="image/png", headers={"Cache-Control": "max-age=86400"})
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


# Extra endpoints from their own modules: temporary sites (resilience.py), medic voice reports (voice.py),
# the satellite / roads map layers (tiles.py) and road / rail blocks (blocks.py).
# They must be included before the static mount below, which catches every other path.
app.include_router(resilience.router)
app.include_router(voice.router)
app.include_router(tiles.router)  # satellite, roads and place-name map layers
app.include_router(blocks.router)  # road and rail blocks: trucks, ambulances and trains reroute
app.include_router(geo_api.router)  # GET / DELETE /zones (audio geolocation)

# Anything else under frontend/ (mock/snapshot.json, assets). Mounted last so the API routes win.
app.mount("/", StaticFiles(directory=FRONTEND), name="frontend")
