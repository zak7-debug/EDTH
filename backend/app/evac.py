"""Casualty evacuation: get each casualty, once treated by their squad medic, to the fastest place
that can treat them, by road.

How it fits the product:
- Every CASUALTY event sets the soldier's status and starts an evacuation (`start()` here) to a
  hospital or aid station (dev_server.process). It sends no drone: drones deliver to medics only,
  when a medic asks for a restock.
- Where to: every operational facility that can treat that severity and has a free bed is a
  candidate (CRITICAL needs surgery, so Role 2 or 3; WOUNDED can go to a Role 1 aid station).
  Each candidate gets a road route round the threat zones (roads.py); the fastest arrival wins. The bed is counted as taken from that moment, so two casualties
  can't be promised the last bed.
- Supplies ahead of the casualty: the destination's stock, minus kit already promised to casualties
  on the way there, is checked against what this casualty will need (TREATMENT_KIT) and any shortfall
  is reported. With KIT_AHEAD_BY_DRONE on (off by default), it is also flown there ahead of them.
- On arrival (`admitted`) the kit is used up from the facility's stock and the casualty is marked
  ADMITTED in the graph. If the destination is destroyed on the way, the casualty is diverted to
  the next-fastest one from where they are; if a new threat appears on their route, they detour.

- The casualty only leaves once treated by their squad medic: if a drone is bringing the medic (or the
  casualty) supplies, the vehicle waits for it to land (WAITING_FOR_DRONE); then TREAT_S of treatment
  and loading (TREATING); then it drives (MOVING). Drones deliver to medics only (dev_server).
- Ground routes follow roads and field tracks round the threat zones (roads.py), and the ETA is the
  driving time on them.

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
from .blocks import ground_zones
from .roads import ROAD_KMH, net_for
from .routing import Router, haversine_m
from .stock import StockKeeper, _fmt

# TUNE: ground casualty-evacuation speed, m/s, for quick "could this possibly be faster" checks only.
# Real drive times come from roads.py (road 50 km/h, field track 25, off-road 10).
CASEVAC_SPEED_MPS = ROAD_KMH / 3.6
# TUNE: mission seconds of treatment and loading after the drone with the casualty's supplies lands.
TREAT_S = 600.0
# TUNE: fly a casualty's missing kit to the destination hospital ahead of them. Off: drones deliver to
# medics only (a casualty report never sends a drone); the shortfall is still reported.
KIT_AHEAD_BY_DRONE = False
LOAD_S = TREAT_S  # older name, kept for callers
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
    flight: _Flight  # reuses the drones' polyline mover; speed = average speed of this road route
    load_left_s: float  # mission seconds of treating/loading left before it moves
    treating: bool = False  # True once the drone has landed and treatment started (or nothing to wait for)


class EvacTracker:
    def __init__(self, engine: DispatchEngine, flights: FlightTracker, stock: StockKeeper,
                 sim_speed: float = SIM_SPEED, clock=time.time):
        self.engine, self.flights, self.stock = engine, flights, stock
        self.repo = engine.repo
        self.sim_speed = sim_speed
        self.clock = clock
        self.trips: dict[str, _Trip] = {}  # evac_id -> trip in progress
        self.roads = net_for(ground_zones(self.repo))  # HOOK: rebuilt in reroute() when a threat or road block appears
        self._medic_cache: dict[str, set[str]] = {}  # soldier -> their squad's medic(s)
        self._ids = itertools.count(1)

    # ---------------------------------------------------------------------------------------
    # Starting an evacuation
    # ---------------------------------------------------------------------------------------

    def start(self, person: Person, severity: str, origin: Optional[tuple[float, float]] = None,
              load_s: float = TREAT_S, avoid: tuple[str, ...] = (), treating: bool = False) -> list[dict]:
        """Pick the fastest facility for this casualty and send them. Returns /ws messages: the
        `evacuation`, then the kit drone's `dispatch` / `no_dispatch` and `queue` if kit is short.
        HOOK: dev_server.process calls this after the casualty's own drone decision."""
        if person.status == "ADMITTED" or any(t.evac.person_id == person.id for t in self.trips.values()):
            return []  # already being evacuated / already in a bed
        origin = origin or (person.lat, person.lon)
        facilities = self.repo.list_facilities()
        wait_s = 0.0 if treating else self._drone_wait_s(person.id)
        best = self._fastest(origin, severity, facilities, load_s + (wait_s or 0.0), avoid)
        if best is None:
            m = evacuation_msg(None, None, reason=f"no operational facility with a free bed can take a {severity} "
                                                  "casualty", note=f"No evacuation for {person.callsign}")
            m["data"]["person_id"] = person.id
            return [m]
        f, points, dist, eta = best
        kit = dict(TREATMENT_KIT.get(severity, {}))
        shortfall = self._shortfall(f, kit)
        fly_kit = bool(shortfall) and KIT_AHEAD_BY_DRONE
        evac = Evacuation(f"evac-{next(self._ids)}", person.id, f.id, severity, points, round(dist, 1),
                          round(eta, 1), self.clock(), kit, shortfall)
        if fly_kit:
            evac.resupply_request_id = f"{evac.evac_id}-kit"
        self.repo.start_evacuation(evac)  # graph: EVACUATED_TO edge + one bed taken
        drive_s = eta - load_s - (wait_s or 0.0)
        self.trips[evac.evac_id] = _Trip(evac, _Flight(evac.evac_id, person.id, [tuple(p) for p in points],
                                                       dist / drive_s if drive_s > 0 else CASEVAC_SPEED_MPS,
                                                       "EN_ROUTE"), load_s, treating=treating or wait_s is None)
        kit_msgs, kit_eta = self._send_kit(evac, f) if fly_kit else ([], None)
        note = f"{person.callsign} to {f.name} by road, {eta / 60:.0f} min"
        if not treating and wait_s is not None:
            note += f" (leaves once treated, {TREAT_S / 60:.0f} min after the medic's drone lands)"
        elif not treating:
            note += f" (leaves once treated, in {TREAT_S / 60:.0f} min)"
        if shortfall and not fly_kit:
            note += f". Short of {_fmt(shortfall)} there"
        elif shortfall:
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
            points, dist, drive_s = self.roads.route(origin, (f.lat, f.lon))
            eta = load_s + drive_s
            if best is None or eta < best[3]:
                best = (f, points, dist, eta)
        return best

    def _drone_wait_s(self, person_id: str) -> Optional[float]:
        """Mission seconds until the drone bringing supplies to this casualty's medic (or to the casualty)
        lands, or None if no drone is in the air for them (the medic treats them straight away with what
        they have; a request still waiting in the queue doesn't hold an evacuation up)."""
        who = {person_id} | self._medics_of(person_id)
        flying = [(fl.total_m - fl.flown_m) / fl.speed_mps for fl in self.flights.flights.values()
                  if fl.phase == "EN_ROUTE" and fl.dispatch and fl.dispatch.recipient_id in who]
        return min(flying) if flying else None  # a request still queued doesn't hold the casualty up

    def _medics_of(self, person_id: str) -> set[str]:
        if person_id not in self._medic_cache:
            p = self.repo.get_person(person_id)
            self._medic_cache[person_id] = {m.id for m in self.repo.list_personnel()
                                            if p and m.kind == "MEDIC" and m.unit_id == p.unit_id}
        return self._medic_cache[person_id]

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
            wait = None if t.treating else self._drone_wait_s(t.evac.person_id)
            if wait is not None:  # their drone hasn't landed: nobody moves an untreated casualty
                lat, lon = t.flight.position()
                drive = (t.flight.total_m - t.flight.flown_m) / t.flight.speed_mps
                out.append(evac_update_msg(t.evac.evac_id, t.evac.person_id, lat, lon, "WAITING_FOR_DRONE",
                                           wait + t.load_left_s + drive))
                continue
            t.treating = True
            if t.load_left_s > 0:  # being treated and loaded
                used = min(sim_s, t.load_left_s)
                t.load_left_s -= used
                sim_s -= used
            t.flight.flown_m = min(t.flight.total_m, t.flight.flown_m + t.flight.speed_mps * sim_s)
            lat, lon = t.flight.position()
            eta = t.load_left_s + (t.flight.total_m - t.flight.flown_m) / t.flight.speed_mps
            out.append(evac_update_msg(t.evac.evac_id, t.evac.person_id, lat, lon,
                                       "TREATING" if t.load_left_s > 0 else "MOVING", eta,
                                       treated=t.load_left_s <= 0))
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
        """A new threat zone or road block: casualties whose road ahead crosses it detour round it.
        HOOK: dev_server.add_threat calls this after engine.zones_changed(); blocks.post_block for road blocks."""
        out, threat = [], Router([zone])
        self.roads = net_for(ground_zones(self.repo))  # the zone or block is in place already: close its roads
        for t in list(self.trips.values()):
            ahead = t.flight.remaining()
            if len(ahead) < 2 or all(threat.clear(ahead[i], ahead[i + 1]) for i in range(len(ahead) - 1)):
                continue
            points, dist, drive_s = self.roads.route(ahead[0], ahead[-1])
            t.flight = _Flight(t.evac.evac_id, t.evac.person_id, [tuple(p) for p in points], dist / max(drive_s, 1), "EN_ROUTE")
            eta = t.load_left_s + drive_s
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
                              avoid=(facility_id,), treating=t.treating)
            if msgs and msgs[0]["type"] == "evacuation":
                lost = next((f.name for f in self.repo.list_facilities() if f.id == facility_id), facility_id)
                msgs[0]["data"]["diverted_from"] = facility_id
                msgs[0]["data"]["note"] = f"{lost} destroyed: diverting {msgs[0]['data']['note'] or ''}"
            out += msgs
        return out

    def facility_added(self, facility_id: str, min_gain: float = 0.2) -> list[dict]:
        """A temporary site opened: casualties still on the road whose trip it cuts by `min_gain`
        or more switch to it. HOOK: resilience.finish_setup calls this once the site is OPERATIONAL."""
        out = []
        facilities = self.repo.list_facilities()
        f = next((x for x in facilities if x.id == facility_id), None)
        if f is None:
            return out
        for t in list(self.trips.values()):
            here = t.flight.position()
            left = t.load_left_s + (t.flight.total_m - t.flight.flown_m) / t.flight.speed_mps
            best = self._fastest(here, t.evac.severity, [f], t.load_left_s)
            if best is None or best[3] > left * (1 - min_gain):
                continue
            del self.trips[t.evac.evac_id]
            self.repo.finish_evacuation(t.evac.evac_id, "DIVERTED", self.clock())  # gives the old bed back
            person = self.repo.get_person(t.evac.person_id)
            msgs = self.start(person, t.evac.severity, origin=here, load_s=t.load_left_s, treating=t.treating)
            if msgs and msgs[0]["type"] == "evacuation":
                msgs[0]["data"]["diverted_from"] = t.evac.facility_id
                msgs[0]["data"]["note"] = (f"{f.name} open: diverting, {left / 60:.0f} min cut to "
                                           f"{msgs[0]['data']['eta_s'] / 60:.0f} min. {msgs[0]['data']['note'] or ''}")
            out += msgs
        return out
