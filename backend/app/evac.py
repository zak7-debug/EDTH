"""Casualty evacuation: get each casualty to the fastest place that can treat them, and fly the kit
they'll need there ahead of them.

How it fits the product:
- Every CASUALTY event starts two things at once (dev_server.process):
    1. the drone dispatch (dispatch.py) that gets supplies to the point of injury in minutes, and
    2. an evacuation (`start()` here) to a hospital or aid station.
- Where to: every operational facility that can treat that severity and has a free bed is a
  candidate (CRITICAL needs surgery, so Role 2 or 3; WOUNDED can go to a Role 1 aid station).
  Each candidate gets a ground route round the threat zones (the same Router the drones use);
  the fastest arrival wins. The bed is counted as taken from that moment, so two casualties
  can't be promised the last bed.
- Supplies ahead of the casualty: the destination's stock, minus kit already promised to casualties
  on the way there, is checked against what this casualty will need (TREATMENT_KIT). Anything
  missing goes out at once as a drone request to the facility, through the normal dispatch
  engine, so it usually lands before the casualty does.
- On arrival (`admitted`) the kit is used up from the facility's stock and the casualty is marked
  ADMITTED in the graph. If the destination is destroyed on the way, the casualty is diverted to
  the next-fastest one from where they are; if a new threat appears on their route, they detour.

Ground routes are cross-country straight lines round threat zones: there is no road network in the
graph yet. HOOK: swap `route_fn` for a road router to make the ETAs road-accurate.

Searchable tags: TUNE (numbers to adjust), HOOK (integration points), DEMO (demo behaviour).
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Optional

from .dispatch import DispatchEngine
from .flights import SIM_SPEED, FlightTracker, _Flight
from .messages import admitted_msg, dispatch_msg, evac_update_msg, evacuation_msg, no_dispatch_msg, queue_msg
from .models import Dispatch, Evacuation, Event, Facility, NoFlyZone, Person
from .routing import Router, haversine_m
from .stock import StockKeeper, _fmt

# TUNE: ground casualty-evacuation speed, m/s. 11 m/s is about 40 km/h cross-country.
CASEVAC_SPEED_MPS = 11.0
# TUNE: mission seconds spent treating and loading at the point of injury before the vehicle moves.
# The drone with point-of-injury supplies usually lands inside this window.
LOAD_S = 300.0
# TUNE: which treatment levels can take which severity (NATO roles: 1 aid station, 2 surgery, 3 hospital).
ACCEPTS = {"CRITICAL": ("ROLE_2", "ROLE_3"), "WOUNDED": ("ROLE_1", "ROLE_2", "ROLE_3")}
# TUNE: what the destination uses to treat one casualty. Missing items are flown in ahead of them.
TREATMENT_KIT = {
    "CRITICAL": {"blood_oneg": 4, "tourniquet": 1, "hemostatic_gauze": 2},
    "WOUNDED": {"chest_seal": 1, "hemostatic_gauze": 1},
}


@dataclass
class _Trip:
    evac: Evacuation
    flight: _Flight  # reuses the drones' polyline mover; speed = CASEVAC_SPEED_MPS
    load_left_s: float  # mission seconds of treating/loading left before it moves


class EvacTracker:
    def __init__(self, engine: DispatchEngine, flights: FlightTracker, stock: StockKeeper,
                 sim_speed: float = SIM_SPEED, clock=time.time):
        self.engine, self.flights, self.stock = engine, flights, stock
        self.repo = engine.repo
        self.sim_speed = sim_speed
        self.clock = clock
        self.trips: dict[str, _Trip] = {}  # evac_id -> trip in progress
        self._ids = itertools.count(1)

    # ---------------------------------------------------------------------------------------
    # Starting an evacuation
    # ---------------------------------------------------------------------------------------

    def start(self, person: Person, severity: str, origin: Optional[tuple[float, float]] = None,
              load_s: float = LOAD_S, avoid: tuple[str, ...] = ()) -> list[dict]:
        """Pick the fastest facility for this casualty and send them. Returns /ws messages: the
        `evacuation`, then the kit drone's `dispatch` / `no_dispatch` and `queue` if kit is short.
        HOOK: dev_server.process calls this after the casualty's own drone decision."""
        if person.status == "ADMITTED" or any(t.evac.person_id == person.id for t in self.trips.values()):
            return []  # already being evacuated / already in a bed
        origin = origin or (person.lat, person.lon)
        facilities = self.repo.list_facilities()
        best = self._fastest(origin, severity, facilities, load_s, avoid)
        if best is None:
            m = evacuation_msg(None, None, reason=f"no operational facility with a free bed can take a {severity} "
                                                  "casualty", note=f"No evacuation for {person.callsign}")
            m["data"]["person_id"] = person.id
            return [m]
        f, points, dist, eta = best
        kit = dict(TREATMENT_KIT.get(severity, {}))
        shortfall = self._shortfall(f, kit)
        evac = Evacuation(f"evac-{next(self._ids)}", person.id, f.id, severity, points, round(dist, 1),
                          round(eta, 1), self.clock(), kit, shortfall)
        if shortfall:
            evac.resupply_request_id = f"{evac.evac_id}-kit"
        self.repo.start_evacuation(evac)  # graph: EVACUATED_TO edge + one bed taken
        self.trips[evac.evac_id] = _Trip(evac, _Flight(evac.evac_id, person.id, [tuple(p) for p in points],
                                                       CASEVAC_SPEED_MPS, "EN_ROUTE"), load_s)
        kit_msgs, kit_eta = self._send_kit(evac, f) if shortfall else ([], None)
        note = f"{person.callsign} to {f.name}, {eta / 60:.0f} min"
        if shortfall:
            note += f". Short of {_fmt(shortfall)} there: " + (
                f"drone lands {kit_eta / 60:.0f} min ahead of them" if kit_eta is not None and kit_eta < eta
                else f"drone lands in {kit_eta / 60:.0f} min" if kit_eta is not None else "no drone free yet, queued")
        return [evacuation_msg(evac, f.name, kit_eta, note), *kit_msgs]

    def _fastest(self, origin, severity, facilities: list[Facility], load_s: float, avoid=()):
        """(facility, route points, metres, eta seconds) of the fastest suitable facility, or None."""
        best = None
        for f in facilities:
            if (f.kind != "HOSPITAL" or f.status != "OPERATIONAL" or f.id in avoid
                    or f.role not in ACCEPTS.get(severity, ACCEPTS["WOUNDED"]) or f.beds_used >= f.beds):
                continue
            if haversine_m(origin, (f.lat, f.lon)) / CASEVAC_SPEED_MPS + load_s >= (best[3] if best else float("inf")):
                continue  # even a straight road couldn't beat the best so far: skip the routing
            points, dist = self.engine.route_fn(origin, (f.lat, f.lon))
            eta = load_s + dist / CASEVAC_SPEED_MPS
            if best is None or eta < best[3]:
                best = (f, points, dist, eta)
        return best

    def _shortfall(self, f: Facility, kit: dict[str, int]) -> dict[str, int]:
        """What `f` is missing for this kit, after setting aside kit for casualties already heading there."""
        promised: dict[str, int] = {}
        for t in self.trips.values():
            if t.evac.facility_id == f.id:
                for i, q in t.evac.kit.items():
                    promised[i] = promised.get(i, 0) + q
        free = {i: f.stock.get(i, 0) - promised.get(i, 0) for i in kit}
        return {i: q - max(0, free[i]) for i, q in kit.items() if free[i] < q}

    def _send_kit(self, evac: Evacuation, f: Facility) -> tuple[list[dict], Optional[float]]:
        """Fly the missing kit to the facility through the normal dispatch engine (it queues if no
        drone is free, and is retried if the drone is shot down)."""
        event = Event(evac.resupply_request_id, "LOW_STOCK", f.id, f.lat, f.lon, self.clock(), items=evac.shortfall)
        result = self.engine.handle(event, time.perf_counter())
        msgs = [dispatch_msg(result) if isinstance(result, Dispatch) else no_dispatch_msg(result)]
        self.engine.record(result)
        if isinstance(result, Dispatch):
            self.flights.start(result)
        msgs.append(queue_msg(self.engine.pending()))
        return msgs, (result.eta_s if isinstance(result, Dispatch) else None)

    # ---------------------------------------------------------------------------------------
    # Moving, arriving, and reacting to the world changing
    # ---------------------------------------------------------------------------------------

    def step(self, dt_real: float) -> list[dict]:
        """Advance every evacuation. HOOK: the tick loop calls this next to FlightTracker.step."""
        out = []
        for t in list(self.trips.values()):
            sim_s = dt_real * self.sim_speed
            if t.load_left_s > 0:  # still being treated and loaded
                used = min(sim_s, t.load_left_s)
                t.load_left_s -= used
                sim_s -= used
            t.flight.flown_m = min(t.flight.total_m, t.flight.flown_m + CASEVAC_SPEED_MPS * sim_s)
            lat, lon = t.flight.position()
            eta = t.load_left_s + (t.flight.total_m - t.flight.flown_m) / CASEVAC_SPEED_MPS
            out.append(evac_update_msg(t.evac.evac_id, t.evac.person_id, lat, lon,
                                       "LOADING" if t.load_left_s > 0 else "MOVING", eta))
            if t.load_left_s <= 0 and t.flight.flown_m >= t.flight.total_m:
                out += self._admit(t)
        return out

    def _admit(self, t: _Trip) -> list[dict]:
        del self.trips[t.evac.evac_id]
        f = next(x for x in self.repo.list_facilities() if x.id == t.evac.facility_id)
        used = {i: min(q, f.stock.get(i, 0)) for i, q in t.evac.kit.items()}
        short = {i: q - used[i] for i, q in t.evac.kit.items() if used[i] < q}
        if any(used.values()):
            self.repo.adjust_stock(f.id, {i: -q for i, q in used.items() if q})  # treatment uses the kit
        self.repo.finish_evacuation(t.evac.evac_id, "ADMITTED", self.clock())
        self.repo.update_person(t.evac.person_id, status="ADMITTED", lat=f.lat, lon=f.lon)
        person = self.repo.get_person(t.evac.person_id)
        note = f"{person.callsign if person else t.evac.person_id} admitted to {f.name}"
        note += f", treated with {_fmt(used)}" if any(used.values()) else ""
        note += f". Still short of {_fmt(short)}" if short else ""
        return [admitted_msg(t.evac, f.name, used, short, f.beds_used, f.beds),
                self.stock.message("treated", note, facility_id=f.id)]

    def reroute(self, zone: NoFlyZone) -> list[dict]:
        """A new threat zone: casualties whose road ahead crosses it detour round it.
        HOOK: dev_server.add_threat calls this after engine.zones_changed()."""
        out, threat = [], Router([zone])
        for t in list(self.trips.values()):
            ahead = t.flight.remaining()
            if len(ahead) < 2 or all(threat.clear(ahead[i], ahead[i + 1]) for i in range(len(ahead) - 1)):
                continue
            points, dist = self.engine.route_fn(ahead[0], ahead[-1])
            t.flight = _Flight(t.evac.evac_id, t.evac.person_id, [tuple(p) for p in points], CASEVAC_SPEED_MPS, "EN_ROUTE")
            eta = t.load_left_s + dist / CASEVAC_SPEED_MPS
            t.evac.route, t.evac.distance_m, t.evac.eta_s = points, round(dist, 1), round(eta, 1)
            name = next((f.name for f in self.repo.list_facilities() if f.id == t.evac.facility_id), t.evac.facility_id)
            out.append(evacuation_msg(t.evac, name, note=f"Evacuation detours round {zone.name}, {eta / 60:.0f} min to go"))
        return out

    def site_changed(self, facility_id: str, status: str) -> list[dict]:
        """The destination was destroyed: divert every casualty heading there to the next-fastest
        facility, from where they are now. HOOK: dev_server.set_site calls this."""
        if status != "DESTROYED":
            return []
        out = []
        for t in [t for t in self.trips.values() if t.evac.facility_id == facility_id]:
            del self.trips[t.evac.evac_id]
            self.repo.finish_evacuation(t.evac.evac_id, "DIVERTED", self.clock())  # gives the bed back
            person = self.repo.get_person(t.evac.person_id)
            msgs = self.start(person, t.evac.severity, origin=t.flight.position(), load_s=t.load_left_s,
                              avoid=(facility_id,))
            if msgs and msgs[0]["type"] == "evacuation":
                lost = next((f.name for f in self.repo.list_facilities() if f.id == facility_id), facility_id)
                msgs[0]["data"]["diverted_from"] = facility_id
                msgs[0]["data"]["note"] = f"{lost} destroyed: diverting {msgs[0]['data']['note'] or ''}"
            out += msgs
        return out

    def facility_added(self, facility_id: str, min_gain: float = 0.2) -> list[dict]:
        """A temporary site was deployed: casualties still on the road whose trip it cuts by `min_gain`
        or more switch to it. HOOK: resilience.post_deploy calls this after writing the site."""
        out = []
        facilities = self.repo.list_facilities()
        f = next((x for x in facilities if x.id == facility_id), None)
        if f is None:
            return out
        for t in list(self.trips.values()):
            here = t.flight.position()
            left = t.load_left_s + (t.flight.total_m - t.flight.flown_m) / CASEVAC_SPEED_MPS
            best = self._fastest(here, t.evac.severity, [f], t.load_left_s)
            if best is None or best[3] > left * (1 - min_gain):
                continue
            del self.trips[t.evac.evac_id]
            self.repo.finish_evacuation(t.evac.evac_id, "DIVERTED", self.clock())  # gives the old bed back
            person = self.repo.get_person(t.evac.person_id)
            msgs = self.start(person, t.evac.severity, origin=here, load_s=t.load_left_s)
            if msgs and msgs[0]["type"] == "evacuation":
                msgs[0]["data"]["diverted_from"] = t.evac.facility_id
                msgs[0]["data"]["note"] = (f"{f.name} deployed: diverting, {left / 60:.0f} min cut to "
                                           f"{msgs[0]['data']['eta_s'] / 60:.0f} min. {msgs[0]['data']['note'] or ''}")
            out += msgs
        return out
