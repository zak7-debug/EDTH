"""Dispatch engine: event -> needed items -> candidate drones -> route + ETA -> claim the best.

How it fits the product:
- The API (Sasank) calls `engine.handle(event)` for every POST /events, broadcasts the result
  (`dispatch` or `no_dispatch` message), then calls `engine.record(result, event)` to write it to the
  graph. Writes come after the broadcast so they never sit on the latency path.
- When a drone is back at a launch site, the API calls `engine.drone_freed(drone_id)`, which
  releases it and serves the triage queue first (CRITICAL, then WOUNDED, then LOW_STOCK).
- Routing is pluggable: `route_fn(a, b) -> (points, metres)`. The default is a straight line;
  Ollie's A* around threat zones (routing.py) drops in without changing anything here.

The hot path is one graph query (`find_candidate_drones`) plus Python maths, then one atomic
`claim_drone` per attempt. Two simultaneous emergencies can't get the same drone: the loser's
claim returns False and it takes its next-best candidate.

Measured: 14-18 ms per decision on the in-memory TuringDB server, under 1 ms on InMemoryRepo.

Searchable tags used in this codebase:
  TUNE:  a number or rule you may want to adjust
  HOOK:  where another part of the system plugs in
  DEMO:  data or behaviour chosen for the demo scenario
"""
from __future__ import annotations

import heapq
import itertools
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

from .models import Depot, Dispatch, Drone, Event, NoDispatch
from .repo import GraphRepo

Point = tuple[float, float]  # (lat, lon) in decimal degrees
RouteFn = Callable[[Point, Point], tuple[list[Point], float]]  # (from, to) -> (waypoints, metres)

# TUNE: what each casualty severity needs. Added on top of anything the event itself lists.
# Changing this changes which drones qualify for a casualty; tests/test_dispatch.py::test_needs_table
# and the seed payloads in seed.py (_DRONES) assume these values.
NEEDS = {
    "CRITICAL": {"tourniquet": 1, "blood_oneg": 2, "hemostatic_gauze": 1},
    "WOUNDED": {"tourniquet": 1, "chest_seal": 1},
}
# TUNE: safety margin on battery. A drone qualifies only if
#   (distance to recipient + distance from recipient to the nearest launch site) x 1.2 <= range left.
RANGE_SAFETY = 1.2
# TUNE: seconds to swap a battery and reload at a launch site. Only used for the
# "nearest alternative" suggestion when no drone can go right now.
RELOAD_S = 120.0


def haversine_m(a: Point, b: Point) -> float:
    """Great-circle distance in metres between two (lat, lon) points."""
    r = math.radians
    dlat, dlon = r(b[0] - a[0]), r(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(r(a[0])) * math.cos(r(b[0])) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))  # 6,371 km = mean Earth radius


def straight_line(a: Point, b: Point) -> tuple[list[Point], float]:
    """Default route: fly direct. Ignores threat zones.
    HOOK: replace with routing.route (A* around no-fly / threat zones) via DispatchEngine(route_fn=...)."""
    return [a, b], haversine_m(a, b)


def needed_items(event: Event, repo: Optional[GraphRepo] = None) -> dict[str, int]:
    """What the recipient needs, as {item: quantity}.

    CASUALTY:  the NEEDS row for its severity, plus any event.items.
    LOW_STOCK: event.items; if the event is empty, the medic's shortfall below threshold
               (read from the personnel graph, so it costs one extra query).
    """
    items: dict[str, int] = {}
    if event.type == "CASUALTY":
        items.update(NEEDS.get(event.severity or "WOUNDED", {}))  # unknown severity -> treat as WOUNDED
    for k, v in event.items.items():
        items[k] = items.get(k, 0) + v
    if event.type == "LOW_STOCK" and not items and repo is not None:
        person = repo.get_person(event.subject_id)
        items = person.low_items() if person else {}
    return items


@dataclass(order=True)
class _Queued:
    """One waiting request. Sort order = triage order: priority (0 = CRITICAL), then oldest
    event first, then arrival order as a tie-break. Fields after `seq` don't take part in sorting."""
    priority: int
    ts: float
    seq: int
    event: Event = field(compare=False)
    items: dict = field(compare=False)


class DispatchEngine:
    def __init__(self, repo: GraphRepo, route_fn: RouteFn = straight_line, clock=time.time):
        self.repo = repo
        self.route_fn = route_fn  # HOOK: routing.py's A* goes here
        self.clock = clock  # injectable so tests / replay mode can control timestamps
        self._queue: list[_Queued] = []  # heap of requests waiting for a drone
        self._seq = itertools.count()
        self._qlock = threading.Lock()  # guards _queue; drone claims are guarded inside the repo
        self._depots: list[Depot] = repo.list_depots()  # launch sites don't move: cache once

    # ---------------------------------------------------------------------------------------
    # Public API: what the backend calls
    # ---------------------------------------------------------------------------------------

    def handle(self, event: Event, received_perf: Optional[float] = None) -> Union[Dispatch, NoDispatch]:
        """Decide which drone goes. Never writes the dispatch to the graph (see record()).

        HOOK: called by POST /events. `received_perf` should be time.perf_counter() taken the
        moment the request arrived, so latency_ms covers parsing + matching + claiming.
        """
        t0 = received_perf if received_perf is not None else time.perf_counter()
        items = needed_items(event, self.repo)
        result = self._try_dispatch(event, items, t0)
        if isinstance(result, NoDispatch) and result.reason_code == "ALL_BUSY":
            # A suitable drone exists but is out on a job: wait for it rather than give up.
            self._enqueue(event, items)
            result.reason += f" (queued, position {self.queue_position(event.event_id)})"
        return result

    def record(self, result: Union[Dispatch, NoDispatch], event: Optional[Event] = None) -> None:
        """Graph writes for a decision: the DISPATCHED_TO edge and the casualty's status.
        HOOK: call after broadcasting the result over /ws, so the writes stay off the latency path."""
        if isinstance(result, Dispatch):
            self.repo.create_dispatch(result)
        if event is not None and event.type == "CASUALTY" and event.severity:
            self.repo.update_person(event.subject_id, status=event.severity)

    def drone_freed(self, drone_id: str) -> list[Union[Dispatch, NoDispatch]]:
        """Drone is back at a launch site and reloaded: release it, then serve the queue.
        HOOK: called by the simulator / tick loop when a returning drone lands.
        Returns the new dispatches so the caller can broadcast them."""
        self.repo.release_drone(drone_id)
        return self.drain_queue()

    def drain_queue(self) -> list[Union[Dispatch, NoDispatch]]:
        """Try every waiting request in triage order; keep the ones that still can't be served."""
        served = []
        with self._qlock:
            waiting = sorted(self._queue)
            self._queue.clear()
        still_waiting = []
        for q in waiting:
            result = self._try_dispatch(q.event, q.items, time.perf_counter())
            if isinstance(result, Dispatch):
                served.append(result)
            else:
                still_waiting.append(q)
        with self._qlock:
            for q in still_waiting:
                heapq.heappush(self._queue, q)
        return served

    def pending(self) -> list[Event]:
        """Queued requests in triage order. HOOK: payload for the `queue` WebSocket message."""
        with self._qlock:
            return [q.event for q in sorted(self._queue)]

    def queue_position(self, event_id: str) -> int:
        """1-based position in the queue, or 0 if not queued."""
        ids = [e.event_id for e in self.pending()]
        return ids.index(event_id) + 1 if event_id in ids else 0

    # ---------------------------------------------------------------------------------------
    # Internals: matching, ETA, selection, explanations
    # ---------------------------------------------------------------------------------------

    def _plan(self, drone: Drone, target: Point) -> tuple[list[Point], float, float, bool]:
        """Route, distance (m), ETA (s), and whether the battery covers the trip out and back."""
        route, dist = self.route_fn((drone.lat, drone.lon), target)
        back = min(haversine_m(target, (d.lat, d.lon)) for d in self._depots)  # to nearest launch site
        in_range = (dist + back) * RANGE_SAFETY <= drone.range_m
        return route, dist, dist / drone.speed_mps, in_range  # TUNE: ETA ignores wind / climb

    def _try_dispatch(self, event: Event, items: dict[str, int], t0: float) -> Union[Dispatch, NoDispatch]:
        target = (event.lat, event.lon)
        # 1. One graph query: free drones carrying enough of every needed item.
        candidates = self.repo.find_candidate_drones(items)
        # 2. Route + ETA + range check for each, in Python (no further database round trips).
        planned = []
        for d in candidates:
            route, dist, eta, ok = self._plan(d, target)
            if ok:
                planned.append((eta, d, route, dist))
        # 3. Fastest first. TUNE: change the sort key to weigh other factors (battery left, payload spare).
        planned.sort(key=lambda p: p[0])
        # 4. Claim in that order. claim_drone is atomic, so a simultaneous request that took our
        #    first choice makes this return False and we fall through to the next-best drone.
        for eta, drone, route, dist in planned:
            if self.repo.claim_drone(drone.id, event.event_id):
                return Dispatch(
                    request_id=event.event_id, drone_id=drone.id, recipient_id=event.subject_id,
                    items=items, eta_s=round(eta, 1), distance_m=round(dist, 1), route=route,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2), ts=self.clock(),
                )
        # 5. Nobody could go: explain why.
        return self._explain(event, items, candidates, t0)

    def _explain(self, event: Event, items: dict[str, int], idle_carriers: list[Drone], t0: float) -> NoDispatch:
        """No drone fits. Pick the reason the UI shows, and suggest the best alternative.

        ALL_BUSY:     some drone carrying the items is out on a job -> request is queued.
        OUT_OF_RANGE: free drones carry the items but none has the battery for the trip.
        NO_STOCK:     no drone anywhere carries the items.
        """
        target = (event.lat, event.lon)
        all_drones = self.repo.list_drones()  # off the happy path, so an extra query is fine
        carriers = [d for d in all_drones if d.carries(items)]
        idle_ids = {c.id for c in idle_carriers}
        busy = [d for d in carriers if d.id not in idle_ids]  # EN_ROUTE, RETURNING or CHARGING
        missing = ", ".join(f"{q} {i}" for i, q in items.items())
        if busy:  # one will come back: queue it
            code, reason = "ALL_BUSY", f"every drone in range carrying {missing} is busy"
        elif idle_carriers:
            code, reason = "OUT_OF_RANGE", f"drones carrying {missing} are free but out of range"
        else:
            code, reason = "NO_STOCK", f"no drone is carrying {missing}"
        return NoDispatch(
            request_id=event.event_id, recipient_id=event.subject_id, reason=reason, reason_code=code,
            nearest_alternative=self._nearest_alternative(items, target, all_drones),
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
        )

    def _nearest_alternative(self, items: dict[str, int], target: Point, drones: list[Drone]) -> Optional[dict]:
        """Fastest free drone that could fly to a launch site stocking `items`, reload, then deliver.
        If no launch site has the stock, fall back to naming where a launch site gets restocked from
        (one supply-chain graph query per launch site)."""
        stocked = [d for d in self.repo.list_depots() if all(d.stock.get(i, 0) >= q for i, q in items.items())]
        best = None
        for drone in drones:
            if drone.status != "IDLE" or drone.claimed_by:
                continue
            for dep in stocked:
                leg1 = haversine_m((drone.lat, drone.lon), (dep.lat, dep.lon))  # drone -> launch site
                leg2 = haversine_m((dep.lat, dep.lon), target)  # launch site -> recipient
                # After reloading it has a fresh battery: it must cover leg2 out and back.
                if (leg1 + 2 * leg2) * RANGE_SAFETY > drone.max_range_m:
                    continue
                eta = (leg1 + leg2) / drone.speed_mps + RELOAD_S
                if best is None or eta < best["eta_s"]:
                    best = {"drone_id": drone.id, "eta_s": round(eta, 1), "via_depot": dep.id,
                            "note": f"{drone.callsign} can reach you in {eta / 60:.0f} min "
                                    f"after reloading at {dep.name}"}
        if best is None and items:
            for dep in self.repo.list_depots():
                sources = self.repo.find_resupply_sources(dep.id, items)  # 2-hop supply-chain query
                if sources:
                    fac, link = sources[0]  # fastest source first
                    return {"drone_id": None, "eta_s": None, "via_depot": dep.id,
                            "note": f"{dep.name} can be restocked from {fac.name} "
                                    f"in {link.lead_time_min:.0f} min"}
        return best

    def _enqueue(self, event: Event, items: dict[str, int]) -> None:
        """Add to the triage queue once (a retried event doesn't get a second place)."""
        with self._qlock:
            if any(q.event.event_id == event.event_id for q in self._queue):
                return
            heapq.heappush(self._queue, _Queued(event.priority, event.ts, next(self._seq), event, items))
