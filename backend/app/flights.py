"""Flight tracker: moves dispatched drones along their routes and closes the loop.

How it fits the product:
- The API's tick loop calls `tracker.step(dt)` a few times a second and broadcasts every message
  it returns over /ws. That is what makes drones visibly fly on the map and ETAs count down.
- Lifecycle of one job: dispatch -> EN_ROUTE along the route -> `delivered` (graph updated via
  complete_dispatch) -> RETURNING to its home launch site -> reload from that site's stock
  (stock.py, so the site's numbers go down) -> engine.drone_freed(), which serves the triage
  queue. Any new dispatches from the queue start flying straight away.
- Pure Python, no web framework, so Sasank's main.py and scripts/dev_server.py both use it and it
  can be unit tested without a server.

Searchable tags: TUNE (numbers to adjust), HOOK (integration points), DEMO (demo behaviour).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional

from .dispatch import DispatchEngine
from .messages import delivered_msg, dispatch_msg, drone_update_msg, queue_msg, reroute_msg
from .models import Dispatch, NoFlyZone
from .routing import Router, haversine_m
from .stock import StockKeeper, _fmt

Point = tuple[float, float]

# DEMO / TUNE: simulated seconds per real second. Real flights take 5-15 minutes; at 10x the judges
# see a delivery in about a minute. ETAs on the wire stay in real mission seconds.
SIM_SPEED = float(os.environ.get("EDTH_SIM_SPEED", "10"))


@dataclass
class _Flight:
    drone_id: str
    request_id: str
    points: list[Point]
    speed_mps: float
    phase: str  # "EN_ROUTE" (outbound) or "RETURNING"
    dispatch: Optional[Dispatch] = None
    flown_m: float = 0.0
    legs: list[float] = field(default_factory=list)

    def __post_init__(self):
        self.legs = [haversine_m(self.points[i], self.points[i + 1]) for i in range(len(self.points) - 1)]

    @property
    def total_m(self) -> float:
        return sum(self.legs)

    def position(self) -> Point:
        """Interpolate along the polyline at flown_m."""
        left = self.flown_m
        for i, leg in enumerate(self.legs):
            if left <= leg and leg > 0:
                f = left / leg
                (a_lat, a_lon), (b_lat, b_lon) = self.points[i], self.points[i + 1]
                return a_lat + (b_lat - a_lat) * f, a_lon + (b_lon - a_lon) * f
            left -= leg
        return self.points[-1]

    def remaining(self) -> list[Point]:
        """Current position followed by the waypoints still ahead."""
        left = self.flown_m
        for i, leg in enumerate(self.legs):
            if left < leg:
                return [self.position()] + self.points[i + 1:]
            left -= leg
        return [self.points[-1]]


class FlightTracker:
    def __init__(self, engine: DispatchEngine, sim_speed: float = SIM_SPEED, clock=time.time,
                 stock: Optional[StockKeeper] = None):
        self.engine = engine
        self.repo = engine.repo
        self.sim_speed = sim_speed
        self.clock = clock
        self.stock = stock or StockKeeper(self.repo, clock=clock)  # HOOK: reloads come out of launch-site stock
        engine.stock = self.stock  # medic restocks can order a launch site restocked (dispatch.py)
        engine.tracker = self  # a higher-ranked restock can take over a drone in the air (dispatch.py)
        self.flights: dict[str, _Flight] = {}  # drone_id -> flight
        # Each drone's standard loadout: its payload the first time it takes off. On landing it is
        # topped back up to this from its launch site's stock (stock.py), as far as the stock allows.
        self._loadout: dict[str, dict[str, int]] = {}

    def start(self, d: Dispatch) -> None:
        """HOOK: call right after broadcasting a `dispatch` message."""
        drone = self.repo.get_drone(d.drone_id)
        self._loadout.setdefault(d.drone_id, dict(drone.payload))
        self.flights[d.drone_id] = _Flight(d.drone_id, d.request_id, [tuple(p) for p in d.route],
                                           drone.speed_mps, "EN_ROUTE", dispatch=d)

    def reroute(self, zone: NoFlyZone) -> list[dict]:
        """A new zone appeared: every drone whose remaining path now crosses a zone flies the new
        shortest safe path from where it is. HOOK: call after engine.zones_changed().
        Returns `reroute` messages (only for drones that actually changed course)."""
        out = []
        threat = Router([zone])
        for f in list(self.flights.values()):
            ahead = f.remaining()
            if len(ahead) < 2 or all(threat.clear(ahead[i], ahead[i + 1]) for i in range(len(ahead) - 1)):
                continue  # this drone's path doesn't touch the new zone
            old_m = sum(haversine_m(ahead[i], ahead[i + 1]) for i in range(len(ahead) - 1))
            new_pts, new_m = self.engine.route_fn(ahead[0], ahead[-1])
            # TUNE: no range re-check here; the 1.2 safety margin absorbs a detour in the demo.
            self.flights[f.drone_id] = _Flight(f.drone_id, f.request_id, [tuple(p) for p in new_pts],
                                               f.speed_mps, f.phase, dispatch=f.dispatch)
            out.append(reroute_msg(f.drone_id, f.request_id, f.phase, new_pts, new_m,
                                   new_m / f.speed_mps, new_m - old_m, zone.name))
        return out

    def lose(self, drone_id: str) -> Optional[dict]:
        """Take a drone out of the air where it is now. Returns where and what it was doing,
        or None if it wasn't flying. HOOK: POST /losses calls this first."""
        f = self.flights.pop(drone_id, None)
        self._loadout.pop(drone_id, None)  # it never comes home to reload
        if f is None:
            return None
        lat, lon = f.position()
        return {"lat": lat, "lon": lon, "phase": f.phase, "request_id": f.request_id,
                "recipient_id": f.dispatch.recipient_id if f.dispatch else None}

    def step(self, dt_real: float) -> list[dict]:
        """Advance every flight by dt_real wall seconds. Returns /ws messages to broadcast, in order."""
        out: list[dict] = []
        for f in list(self.flights.values()):
            f.flown_m = min(f.total_m, f.flown_m + f.speed_mps * dt_real * self.sim_speed)
            lat, lon = f.position()
            eta = (f.total_m - f.flown_m) / f.speed_mps
            out.append(drone_update_msg(f.drone_id, lat, lon, f.phase, round(eta, 1), f.request_id))
            if f.flown_m >= f.total_m:
                out += self._arrived(f)
        for m in self.stock.step():  # restock shipments landing at launch sites
            out.append(m)
            change = m["data"].get("change") if m["type"] == "stock_update" else None  # the outbox also carries dispatches
            if change and change["kind"] == "order_arrived":
                out += self._restocked(change["depot_id"])
        return out

    def _restocked(self, depot_id: str) -> list[dict]:
        """A shipment landed: top up drones waiting there short of kit, then serve the queue,
        since a request may have been waiting on exactly this stock."""
        out = []
        for d in self.repo.list_drones():
            loadout = self._loadout.get(d.id)
            if d.depot_id == depot_id and d.status == "IDLE" and loadout and not d.carries(loadout):
                out += self.stock.reload(d, loadout)
        served = self.engine.drain_queue()
        for res in served:
            out.append(dispatch_msg(res))
            self.engine.record(res)
            self.start(res)
        if served:
            out.append(queue_msg(self.engine.pending()))
        return out

    # internals -----------------------------------------------------------------------------------

    def _arrived(self, f: _Flight) -> list[dict]:
        drone = self.repo.get_drone(f.drone_id)
        lat, lon = f.points[-1]
        self.repo.update_drone(f.drone_id, lat=lat, lon=lon, range_m=max(0.0, drone.range_m - f.total_m))
        if f.phase == "EN_ROUTE":
            now = self.clock()
            self.repo.complete_dispatch(f.request_id, now)  # moves items into a medic's stock
            # TUNE: a casualty stays WOUNDED/CRITICAL after delivery (the medic still has to treat them).
            home = next(d for d in self.repo.list_depots() if d.id == drone.depot_id)
            back, _ = self.engine.route_fn((lat, lon), (home.lat, home.lon))
            self.repo.update_drone(f.drone_id, status="RETURNING")
            self.flights[f.drone_id] = _Flight(f.drone_id, f.request_id, [tuple(p) for p in back],
                                               f.speed_mps, "RETURNING")
            msgs = [delivered_msg(f.dispatch, now)] if f.dispatch else []
            if f.dispatch and not f.dispatch.recipient_id.startswith(("sol-", "med-")):
                # A casualty's kit flown ahead to a hospital or aid station: its stock just went up.
                where = next((x.name for x in self.repo.list_facilities() if x.id == f.dispatch.recipient_id),
                             f.dispatch.recipient_id)
                msgs.append(self.stock.message("kit_delivered", f"{drone.callsign} delivered {_fmt(f.dispatch.items)} "
                                               f"to {where}", facility_id=f.dispatch.recipient_id))
            return msgs
        # Back home: battery swap, reload from the launch site's stock, then serve whoever is waiting.
        del self.flights[f.drone_id]
        # TUNE: instant turnaround. Real turnaround is RELOAD_S in dispatch.py (used for ETA estimates).
        self.repo.update_drone(f.drone_id, range_m=drone.max_range_m)
        msgs = [drone_update_msg(f.drone_id, lat, lon, "IDLE")]
        msgs += self.stock.reload(self.repo.get_drone(f.drone_id), self._loadout.get(f.drone_id, drone.payload))
        for res in self.engine.drone_freed(f.drone_id):
            msgs.append(dispatch_msg(res))
            self.engine.record(res)
            self.start(res)
        msgs.append(queue_msg(self.engine.pending()))
        return msgs
