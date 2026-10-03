"""Working API for the dashboard: POST /events, /ws, the tick loop and the demo scenario.

How it fits the product:
- Written while Sasank's main.py is still a stub, so the dashboard (frontend/index.html) runs end to
  end today. It follows contracts/messages.md exactly, so the frontend can't tell the two apart.
  Sasank can lift any part of it into main.py, or keep it and add the simulator on top.
- Run it with ./start.sh (default app) or:
    uvicorn backend.app.dev_server:app --port 8000            (EDTH_REPO=memory for no database)
- Flow of one request: POST /events -> broadcast `event` -> engine.handle (the timed decision)
  -> broadcast `dispatch` / `no_dispatch` -> engine.record (graph writes, off the latency path)
  -> FlightTracker flies the drone, ticking `drone_update` -> `delivered` -> return -> queue drains.

Searchable tags: TUNE (numbers to adjust), HOOK (integration points), DEMO (demo behaviour).
"""
from __future__ import annotations

import asyncio
import itertools
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .dispatch import DispatchEngine
from .flights import FlightTracker
from . import querylog
from .messages import (dispatch_msg, event_msg, no_dispatch_msg, query_log_msg, queue_msg, snapshot,
                       drone_lost_msg, zone_added_msg)
from .models import Dispatch, Event, NoFlyZone
from .repo import get_repo

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
TICK_S = 0.5  # TUNE: how often drones move on the map (contract says about 2 updates per second)

# DEMO: the scripted scenario behind the "Run demo scenario" button. (seconds after start, event).
# Story: a critical casualty, then one that forces a detour round the EW jamming zone, then two
# requests at the same instant (they get different drones), a new threat that forces a drone
# already in the air to change course, then a burst that exhausts the drones so
# the triage queue fills and drains as drones come home.
DEMO_SCRIPT = [
    (0.0, {"type": "CASUALTY", "subject_id": "sol-03", "severity": "CRITICAL"}),
    (5.0, {"type": "CASUALTY", "subject_id": "sol-10", "severity": "CRITICAL"}),
    (7.0, {"type": "THREAT"}),  # DEMO: new threat on FALCON 1's path; it reroutes mid-flight
    (10.0, {"type": "LOW_STOCK", "subject_id": "med-2", "items": {"blood_oneg": 2}}),
    (10.0, {"type": "CASUALTY", "subject_id": "sol-15", "severity": "WOUNDED"}),
    (16.0, {"type": "CASUALTY", "subject_id": "sol-16", "severity": "CRITICAL"}),
    (19.0, {"type": "DRONE_LOST", "drone_id": "drn-01"}),  # DEMO: HAWK 1 shot down on its way to BADGER 2-4
    (22.0, {"type": "CASUALTY", "subject_id": "sol-05", "severity": "CRITICAL"}),
    (23.0, {"type": "CASUALTY", "subject_id": "sol-12", "severity": "WOUNDED"}),
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
        self.repo = get_repo()  # HOOK: EDTH_REPO=turing|memory picks the store
        self.engine = DispatchEngine(self.repo)  # routes round threat zones by default
        self.tracker = FlightTracker(self.engine)


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
    reroutes = world.tracker.reroute(zone)  # drones already flying change course
    ms = (time.perf_counter() - t) * 1000
    await broadcast(zone_added_msg(zone))
    for m in reroutes:
        await broadcast(m)
    return {"zone": zone.to_dict(), "rerouted": [m["data"]["drone_id"] for m in reroutes], "ms": round(ms, 1)}


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


@app.post("/losses")
async def post_loss(body: dict):
    """HOOK: report a drone lost (dashboard button, scenario, or real telemetry). Body: {"drone_id"}."""
    return await lose_drone(body["drone_id"])


@app.post("/threats")
async def post_threat(body: dict):
    """HOOK: report a new threat zone (dashboard button, scenario, or real intel feed)."""
    return await add_threat(body)


async def process(raw: dict, received_perf: float) -> dict:
    if raw.get("type") == "DRONE_LOST":  # DEMO: scripted losses share the event timeline
        return await lose_drone(raw["drone_id"])
    if raw.get("type") == "THREAT":  # DEMO: scripted threats share the event timeline
        return await add_threat({k: v for k, v in raw.items() if k != "type"} or dict(DEMO_THREAT))
    event = _complete(raw)
    await broadcast(event_msg(event, time.time()))
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
    return result.to_dict()


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
    return {"ok": True}


@app.post("/scenario/{name}")
async def scenario(name: str):
    global _scenario
    if name != "demo":
        raise HTTPException(404, "only 'demo' exists")
    if _scenario and not _scenario.done():
        return {"ok": False, "msg": "scenario already running"}

    async def run():
        start = time.monotonic()
        for at, ev in DEMO_SCRIPT:
            await asyncio.sleep(max(0.0, at - (time.monotonic() - start)))
            await process(dict(ev), time.perf_counter())

    _scenario = asyncio.create_task(run())
    return {"ok": True, "events": len(DEMO_SCRIPT)}


@app.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    clients.add(socket)
    await socket.send_json(snapshot(world.repo))
    await socket.send_json(queue_msg(world.engine.pending()))
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
            for m in world.tracker.step(now - last):
                await broadcast(m)
        except Exception as e:  # never let one bad tick kill the loop mid-demo
            print("tick error:", e)
        last = now


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


# Anything else under frontend/ (mock/snapshot.json, assets). Mounted last so the API routes win.
app.mount("/", StaticFiles(directory=FRONTEND), name="frontend")
