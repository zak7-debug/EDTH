"""Flight tracker: moves dispatched drones along their routes and closes the loop.

How it fits the product:
- The API's tick loop calls `tracker.step(dt)` a few times a second and broadcasts every message
  it returns over /ws. That is what makes drones visibly fly on the map and ETAs count down.
- Lifecycle of one job: dispatch -> EN_ROUTE along the route -> `delivered` (graph updated via
  complete_dispatch) -> RETURNING to its home launch site -> reload -> engine.drone_freed(), which
  serves the triage queue. Any new dispatches from the queue start flying straight away.
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
from .messages import delivered_msg, dispatch_msg, drone_update_msg, queue_msg
from .models import Dispatch
from .routing import haversine_m

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


class FlightTracker:
    def __init__(self, engine: DispatchEngine, sim_speed: float = SIM_SPEED, clock=time.time):
        self.engine = engine
        self.repo = engine.repo
        self.sim_speed = sim_speed
        self.clock = clock
        self.flights: dict[str, _Flight] = {}  # drone_id -> flight
        self._loadout: dict[str, dict[str, int]] = {}  # payload at takeoff, restored on reload

    def start(self, d: Dispatch) -> None:
        """HOOK: call right after broadcasting a `dispatch` message."""
        drone = self.repo.get_drone(d.drone_id)
        self._loadout.setdefault(d.drone_id, dict(drone.payload))
        self.flights[d.drone_id] = _Flight(d.drone_id, d.request_id, [tuple(p) for p in d.route],
                                           drone.speed_mps, "EN_ROUTE", dispatch=d)

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
            return [delivered_msg(f.dispatch, now)] if f.dispatch else []
        # Back home: battery swap + reload, then serve whoever is waiting.
        del self.flights[f.drone_id]
        # TUNE: instant reload. Real turnaround is RELOAD_S in dispatch.py (used for ETA estimates).
        self.repo.update_drone(f.drone_id, range_m=drone.max_range_m,
                               payload=self._loadout.pop(f.drone_id, drone.payload))
        msgs = [drone_update_msg(f.drone_id, lat, lon, "IDLE")]
        for res in self.engine.drone_freed(f.drone_id):
            msgs.append(dispatch_msg(res))
            self.engine.record(res)
            self.start(res)
        msgs.append(queue_msg(self.engine.pending()))
        return msgs
