"""Dispatch engine: event -> needed items -> candidate drones -> route + ETA -> claim the best.

How it fits the product:
- The API (Sasank) calls `engine.handle(event)` for every POST /events, broadcasts the result
  (`dispatch` or `no_dispatch` message), then calls `engine.record(result, event)` to write it to the
  graph. Writes come after the broadcast so they never sit on the latency path.
- When a drone is back at a launch site, the API calls `engine.drone_freed(drone_id)`, which
  releases it and serves the triage queue first (CRITICAL, then WOUNDED, then LOW_STOCK).
- A medic's restock (LOW_STOCK) always ends with a drone on its way, now or later. It carries an
  urgency: CRITICAL, URGENT or NON_URGENT (models.RESTOCK_PRIORITY sets its place in the queue).
  If no free drone already carries the items, a CRITICAL or URGENT restock gets an idle drone loaded
  to order at the launch site that holds them (`_load_to_order`); a NON_URGENT one waits for a drone
  coming home, unless none carries the items at all. If no launch site holds the items, a restock
  shipment is ordered to the launch site that can serve the medic soonest (or one already on its way
  is used), the request waits in the queue, and the medic is told the ETA. It flies when it lands.
- Restocks are ranked (models.RESTOCK_PRIORITY). When no free drone carries what a medic needs, a drone
  already in the air to a LOWER-ranked medic that carries some of it is diverted to them at once
  (`_take_over`). If it doesn't carry everything, a second drone is sent with the rest; the
  lower-ranked medic's restock goes out again (keeping its place in the queue by its original time).
- Routing is pluggable: `route_fn(a, b) -> (points, metres)`. The default is routing.Router, an A*
  around the graph's no-fly / threat zones; pass `route_fn=straight_line` to switch it off.

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
from dataclasses import dataclass, field, replace
from typing import Callable, Optional, Union

from .models import Depot, Dispatch, Drone, Event, NoDispatch
from .repo import GraphRepo
from .routing import Router
from .supply_chain import best_path

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
# TUNE: restock urgencies that get a drone loaded to order instead of waiting for one coming home.
LOAD_TO_ORDER = ("CRITICAL", "URGENT")
# TUNE: a higher-ranked restock may take over a drone in the air to a lower-ranked medic.
TAKE_OVER = True


def haversine_m(a: Point, b: Point) -> float:
    """Great-circle distance in metres between two (lat, lon) points."""
    r = math.radians
    dlat, dlon = r(b[0] - a[0]), r(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(r(a[0])) * math.cos(r(b[0])) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))  # 6,371 km = mean Earth radius


def straight_line(a: Point, b: Point) -> tuple[list[Point], float]:
    """Default route: fly direct. Ignores threat zones.
    Kept for tests and as a fallback: DispatchEngine(repo, route_fn=straight_line)."""
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
    def __init__(self, repo: GraphRepo, route_fn: Optional[RouteFn] = None, clock=time.time):
        self.repo = repo
        # HOOK: routing. Default = A* around the zones in the graph (routing.py), built once here.
        # Restart the engine (or rebuild the Router) if zones change mid-demo.
        self._auto_route = route_fn is None
        self.route_fn = route_fn or Router.from_repo(repo).route
        self.clock = clock  # injectable so tests / replay mode can control timestamps
        self._queue: list[_Queued] = []  # heap of requests waiting for a drone
        self._seq = itertools.count()
        self._qlock = threading.Lock()  # guards _queue; drone claims are guarded inside the repo
        self._depots: list[Depot] = repo.list_depots()  # launch sites don't move: cache once
        self._events: dict[str, Event] = {}  # request_id -> event, for retries after a drone is lost
        self._retries = itertools.count(1)
        self.stock = None  # HOOK: the StockKeeper (flights.FlightTracker sets it), for restocking launch sites
        self.tracker = None  # HOOK: the FlightTracker (it sets itself), to divert drones in the air
        self._awaiting: dict[str, dict] = {}  # request_id -> restock shipment it waits for (order_id, eta)

    # ---------------------------------------------------------------------------------------
    # Public API: what the backend calls
    # ---------------------------------------------------------------------------------------

    def handle(self, event: Event, received_perf: Optional[float] = None) -> Union[Dispatch, NoDispatch]:
        """Decide which drone goes. Never writes the dispatch to the graph (see record()).

        HOOK: called by POST /events. `received_perf` should be time.perf_counter() taken the
        moment the request arrived, so latency_ms covers parsing + matching + claiming.
        """
        t0 = received_perf if received_perf is not None else time.perf_counter()
        self._events[event.event_id] = event  # kept so a lost drone's request can be retried
        items = needed_items(event, self.repo)
        result = self._try_dispatch(event, items, t0)
        if isinstance(result, NoDispatch) and event.type == "LOW_STOCK":
            return self._restock(event, items, result, t0)
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
            if isinstance(result, NoDispatch) and q.event.type == "LOW_STOCK":
                result = self._load_to_order(q.event, q.items, time.perf_counter()) or result
            if isinstance(result, Dispatch):
                self._awaiting.pop(q.event.event_id, None)
                served.append(result)
            else:
                still_waiting.append(q)
        with self._qlock:
            for q in still_waiting:
                heapq.heappush(self._queue, q)
        return served

    def drone_lost(self, drone_id: str, request_id: Optional[str] = None) -> Optional[Union[Dispatch, NoDispatch]]:
        """A drone was shot down. Writes it off in the graph and, if it was carrying someone's
        supplies, retries that request straight away as a new request with the original timestamp,
        so it keeps its place at the front of the triage queue if no drone is free.
        HOOK: called by POST /losses. Returns the retry's result (None if the drone was empty)."""
        t0 = time.perf_counter()
        self.repo.lose_drone(drone_id, request_id, self.clock())
        original = self._events.get(request_id) if request_id else None
        if original is None:
            return None
        retry = replace(original, event_id=f"{original.event_id}-r{next(self._retries)}")
        return self.handle(retry, t0)

    def zones_changed(self) -> None:
        """A threat zone was added or moved: rebuild the router from the graph so every later
        decision routes round it. HOOK: called by POST /threats (flights.py reroutes drones in the air)."""
        if self._auto_route:
            self.route_fn = Router.from_repo(self.repo).route

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
        carriers = [d for d in all_drones if d.carries(items) and d.status != "LOST"]
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
        If no launch site has the stock, fall back to the fastest working supply chain to a launch site."""
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
            # Nothing can fly it now: name the fastest working supply chain to any launch site,
            # skipping destroyed hubs and hospitals (supply_chain.py).
            facilities, depots, links = self.repo.list_facilities(), self.repo.list_depots(), self.repo.list_supply_links()
            names = {**{f.id: f.name for f in facilities}, **{d.id: d.name for d in depots}}
            paths = [p for p in (best_path(d.id, items, facilities, depots, links) for d in depots) if p]
            if paths:
                p = min(paths, key=lambda p: p["minutes"])
                return {"drone_id": None, "eta_s": None, "via_depot": p["path"][-1],
                        "note": f"{names[p['path'][-1]]} can be restocked from {names[p['source_id']]} "
                                f"in {p['minutes']:.0f} min"}
        return best

    # ---------------------------------------------------------------------------------------
    # Medic restocks: always end with a drone on its way
    # ---------------------------------------------------------------------------------------

    def _restock(self, event: Event, items: dict[str, int], first: NoDispatch, t0: float) -> Union[Dispatch, NoDispatch]:
        """No free drone carries the items. Load one to order, or wait for one, or restock a launch site."""
        urgency = event.urgency or "NON_URGENT"
        if TAKE_OVER:
            taken = self._take_over(event, items, t0)
            if taken:
                return taken
        if urgency in LOAD_TO_ORDER or first.reason_code != "ALL_BUSY":
            loaded = self._load_to_order(event, items, t0)
            if loaded:
                return loaded
        self._enqueue(event, items)
        pos = self.queue_position(event.event_id)
        stocked = [d for d in self.repo.list_depots() if all(d.stock.get(i, 0) >= q for i, q in items.items())]
        if stocked:  # the items are at a launch site: the next drone home there (or anywhere) loads them
            first.reason = f"{first.reason}; queued ({urgency.lower().replace('_', '-')}, position {pos}): " \
                           f"the first drone free at {' or '.join(d.name for d in stocked)} is loaded with them"
            first.reason_code = "ALL_BUSY"
            return first
        plan = self._stock_eta(event, items)
        if plan is None:
            first.reason = f"{first.reason}; queued (position {pos}), but no working supply chain holds {_fmt(items)}"
            first.reason_code = "NO_STOCK"
            return first
        self._awaiting[event.event_id] = plan
        return NoDispatch(
            request_id=event.event_id, recipient_id=event.subject_id, reason_code="AWAITING_STOCK",
            reason=f"no launch site holds {_fmt(items)}: {plan['note']}. Queued ({urgency.lower().replace('_', '-')}, "
                   f"position {pos}); a drone takes off when the shipment lands",
            nearest_alternative={"drone_id": None, "eta_s": plan["eta_s"], "via_depot": plan["depot_id"],
                                 "order_id": plan["order_id"], "note": plan["note"]},
            latency_ms=round((time.perf_counter() - t0) * 1000, 2))

    def _take_over(self, event: Event, items: dict[str, int], t0: float) -> Optional[Dispatch]:
        """Divert a drone in the air to a lower-ranked medic, carrying some of `items`, to this medic.
        Prefers the drone that covers the most, then the soonest. Side effects (broadcast through the
        StockKeeper outbox): the lower-ranked restock goes out again, and a top-up for anything missing."""
        if self.tracker is None:
            return None
        target, best = (event.lat, event.lon), None
        for f in self.tracker.flights.values():
            old = self._events.get(f.request_id)
            if f.phase != "EN_ROUTE" or f.dispatch is None or old is None or old.type != "LOW_STOCK" \
                    or old.priority <= event.priority or old.subject_id == event.subject_id:
                continue
            drone = self.repo.get_drone(f.drone_id)
            if drone.claimed_by != f.request_id:  # already taken over in this decision
                continue
            give = {i: min(q, drone.payload.get(i, 0)) for i, q in items.items()}
            give = {i: q for i, q in give.items() if q > 0}
            if not give:
                continue
            here = f.position()
            route, dist = self.route_fn(here, target)
            back = min(haversine_m(target, (d.lat, d.lon)) for d in self._depots)
            if (dist + back) * RANGE_SAFETY > drone.range_m - f.flown_m:
                continue
            key = (-sum(give.values()), dist / drone.speed_mps)
            if best is None or key < best[0]:
                best = (key, f, drone, give, route, dist, old)
        if best is None:
            return None
        _, f, drone, give, route, dist, old = best
        self.repo.divert_dispatch(f.request_id, event.event_id, self.clock())
        here = f.position()  # the new flight starts from here, so book the battery already used
        self.repo.update_drone(drone.id, lat=here[0], lon=here[1], range_m=max(0.0, drone.range_m - f.flown_m))
        taken = Dispatch(request_id=event.event_id, drone_id=drone.id, recipient_id=event.subject_id, items=give,
                         eta_s=round(dist / drone.speed_mps, 1), distance_m=round(dist, 1), route=route,
                         latency_ms=round((time.perf_counter() - t0) * 1000, 2), ts=self.clock())
        # The lower-ranked medic's restock goes out again with its original time, so it keeps its place.
        retry = replace(old, event_id=f"{old.event_id.split('-d')[0]}-d{next(self._retries)}")
        missing = {i: q - give.get(i, 0) for i, q in items.items() if q > give.get(i, 0)}
        side = []
        names = {p.id: p.callsign for p in self.repo.list_personnel()}
        note = (f"{drone.callsign} diverted from {names.get(old.subject_id, old.subject_id)} "
                f"({(old.urgency or 'NON_URGENT').lower().replace('_', '-')}) to {names.get(event.subject_id, event.subject_id)} "
                f"({(event.urgency or 'NON_URGENT').lower().replace('_', '-')}) with {_fmt(give)}")
        if missing:
            top = replace(event, event_id=f"{event.event_id}-top", items=missing)
            side.append(self.handle(top, time.perf_counter()))
            note += f"; a second drone brings {_fmt(missing)}"
        side.append(self.handle(retry, time.perf_counter()))
        note += f". {names.get(old.subject_id, old.subject_id)}'s restock goes out again"
        self._side_effects(side, note, drone.id)
        return taken

    def _side_effects(self, results: list, note: str, drone_id: str) -> None:
        """Start, record and announce decisions made inside another one (a take-over's retry and top-up)."""
        from .messages import dispatch_msg, no_dispatch_msg, queue_msg  # late import: messages imports repo
        if self.stock is None:
            return
        out = [self.stock.message("diverted", note, drone_id=drone_id)]
        for r in results:
            if isinstance(r, Dispatch):
                self.record(r)
                self.tracker.start(r)
                out.append(dispatch_msg(r))
            elif r is not None:
                out.append(no_dispatch_msg(r))
        out.append(queue_msg(self.pending()))
        self.stock.outbox += out

    def _load_to_order(self, event: Event, items: dict[str, int], t0: float) -> Optional[Dispatch]:
        """An idle drone at a launch site that holds the items is loaded with them and sent (fastest first).
        Anything else it carries goes back on the shelf if it needs the room."""
        target = (event.lat, event.lon)
        depots = {d.id: d for d in self.repo.list_depots()
                  if all(d.stock.get(i, 0) >= q for i, q in items.items())}
        need = sum(items.values())
        options = []
        for d in self.repo.list_drones():
            if d.status != "IDLE" or d.claimed_by or d.depot_id not in depots or d.capacity < need:
                continue
            route, dist, eta, ok = self._plan(replace(d, range_m=d.max_range_m), target)  # fresh battery while loading
            if ok:
                options.append((eta + RELOAD_S, d, route, dist))
        for eta, d, route, dist in sorted(options, key=lambda o: o[0]):
            if not self.repo.claim_drone(d.id, event.event_id):
                continue
            payload = {i: q for i, q in d.payload.items() if q}
            room = d.capacity - sum(payload.values())
            for i in sorted(payload, key=lambda i: i in items):  # shelve what isn't needed first
                if room >= need:
                    break
                back = payload.pop(i)
                self.repo.adjust_stock(d.depot_id, {i: back})
                room += back
            self.repo.adjust_stock(d.depot_id, {i: -q for i, q in items.items()})
            new = {i: payload.get(i, 0) + items.get(i, 0) for i in set(payload) | set(items)}
            self.repo.update_drone(d.id, range_m=d.max_range_m, payload={**{i: 0 for i in d.payload}, **new})
            if self.stock is not None:
                self.stock.outbox.append(self.stock.message(
                    "loaded_to_order", f"{d.callsign} loaded {_fmt(items)} at {depots[d.depot_id].name} for a "
                    f"{(event.urgency or 'NON_URGENT').lower().replace('_', '-')} restock", depot_id=d.depot_id,
                    drone_id=d.id))
            return Dispatch(
                request_id=event.event_id, drone_id=d.id, recipient_id=event.subject_id, items=items,
                eta_s=round(eta, 1), distance_m=round(dist, 1), route=route,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2), ts=self.clock())
        return None

    def _stock_eta(self, event: Event, items: dict[str, int]) -> Optional[dict]:
        """No launch site holds the items: use a shipment already on its way that brings them, or order one
        to the launch site that can serve this medic soonest. Returns {depot_id, order_id, eta_s, note}
        (eta_s in mission seconds: shipment, loading, then the flight)."""
        if self.stock is None:
            return None
        target = (event.lat, event.lon)
        depots = {d.id: d for d in self.repo.list_depots()}
        drones = [d for d in self.repo.list_drones() if d.status != "LOST" and d.capacity >= sum(items.values())]

        def fly_s(dep):
            speeds = [d.speed_mps for d in drones if d.depot_id == dep.id] or [d.speed_mps for d in drones] or [20.0]
            return haversine_m((dep.lat, dep.lon), target) / max(speeds) + RELOAD_S

        best = None
        for o in self.stock.inbound(items):  # already coming: no second order
            dep = depots[o["depot_id"]]
            eta = o["remaining_min"] * 60 + fly_s(dep)
            if best is None or eta < best[0]:
                best = (eta, dep, o["order_id"], o["remaining_min"], False)
        if best is None:
            facilities, links = self.repo.list_facilities(), self.repo.list_supply_links()
            for dep in depots.values():
                p = best_path(dep.id, items, facilities, list(depots.values()), links)
                if p and (best is None or p["minutes"] * 60 + fly_s(dep) < best[0]):
                    best = (p["minutes"] * 60 + fly_s(dep), dep, None, p["minutes"], True)
            if best is None:
                return None
            order_id = self.stock.order(best[1].id, items, note_for=event.subject_id)
            if order_id is None:
                return None
            best = (best[0], best[1], order_id, best[3], True)
        eta, dep, order_id, ship_min, new = best
        what = "restocking" if new else "already being restocked"
        note = (f"{dep.name} {what} ({_hm(ship_min)}), then a drone: about {_hm(eta / 60)} in all")
        return {"depot_id": dep.id, "order_id": order_id, "eta_s": round(eta, 1), "note": note}

    def _enqueue(self, event: Event, items: dict[str, int]) -> None:
        """Add to the triage queue once (a retried event doesn't get a second place)."""
        with self._qlock:
            if any(q.event.event_id == event.event_id for q in self._queue):
                return
            heapq.heappush(self._queue, _Queued(event.priority, event.ts, next(self._seq), event, items))


def _fmt(items: dict[str, int]) -> str:
    return ", ".join(f"{q} {i.replace('_', ' ')}" for i, q in items.items())


def _hm(m: float) -> str:
    h, mins = divmod(int(round(m)), 60)
    return f"{h} h {mins:02d}" if h else f"{mins} min"
