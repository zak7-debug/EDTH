"""GraphRepo: the only way the rest of the app touches graph data.

Two implementations share this interface:
- InMemoryRepo (here): plain dicts, zero setup, used for tests and as the fallback.
- TuringRepo (turing_repo.py): the real store, two TuringDB graphs.

Pick one with the EDTH_REPO env var ("memory" or "turing") via get_repo().

Concurrency rule: claim_drone() is the single point that decides which request owns a drone.
It is atomic within one process (a lock), because TuringDB itself does not detect conflicting
writes (last writer wins). The dispatch engine must call claim_drone() and treat False as
"someone else got it, try the next candidate".
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional, Protocol

from .models import Depot, Dispatch, Drone, Facility, NoFlyZone, Person, SupplyLink, Unit
from .seed import SeedData, load_seed


# HOOK: the contract between the app and storage. Adding a method? Add it here, in InMemoryRepo
# and in TuringRepo, and add a test in tests/test_repo.py (runs against both).
class GraphRepo(Protocol):
    # personnel graph
    def get_person(self, person_id: str) -> Optional[Person]: ...
    def update_person(self, person_id: str, **props) -> None: ...
    def list_personnel(self) -> list[Person]: ...
    def list_units(self) -> list[Unit]: ...

    # logistics graph
    def get_drone(self, drone_id: str) -> Optional[Drone]: ...
    def list_drones(self) -> list[Drone]: ...
    def list_depots(self) -> list[Depot]: ...
    def list_no_fly_zones(self) -> list[NoFlyZone]: ...
    def add_no_fly_zone(self, zone: NoFlyZone) -> None:
        """A threat appeared (or a zone was redrawn): create or replace it by id."""
        ...
    def list_facilities(self) -> list[Facility]: ...
    def list_supply_links(self) -> list[SupplyLink]: ...
    def find_resupply_sources(self, depot_id: str, items: dict[str, int]) -> list[tuple[Facility, SupplyLink]]:
        """Upstream facilities that restock `depot_id` and hold enough of `items`, fastest first."""
        ...
    def find_candidate_drones(self, items: dict[str, int]) -> list[Drone]:
        """IDLE, unclaimed drones carrying at least `items`. One query on TuringDB."""
        ...
    def update_drone(self, drone_id: str, **props) -> None:
        """Set any Drone field. `payload=` replaces quantities for the items given."""
        ...

    # dispatch lifecycle
    def claim_drone(self, drone_id: str, request_id: str) -> bool:
        """Atomically mark an IDLE, unclaimed drone EN_ROUTE for request_id. False if taken."""
        ...
    def release_drone(self, drone_id: str) -> None:
        """Clear the claim and set the drone IDLE (after it is back at a depot)."""
        ...
    def create_dispatch(self, dispatch: Dispatch) -> None:
        """Record drone -> recipient (DISPATCHED_TO with eta and ts)."""
        ...
    def complete_dispatch(self, request_id: str, ts: Optional[float] = None) -> Optional[Dispatch]:
        """Mark delivered: move items from drone payload into the recipient's stock (medics)."""
        ...
    def list_dispatches(self) -> list[Dispatch]: ...


# Plain-dict implementation: zero setup, used by tests and as the fallback (EDTH_REPO=memory).
class InMemoryRepo:
    def __init__(self, seed: Optional[SeedData] = None):
        seed = seed or load_seed()
        self._lock = threading.Lock()
        self.units = {u.id: u for u in seed.units}
        self.personnel = {p.id: p for p in seed.personnel}
        self.depots = {d.id: d for d in seed.depots}
        self.drones = {d.id: d for d in seed.drones}
        self.no_fly_zones = {z.id: z for z in seed.no_fly_zones}
        self.facilities = {f.id: f for f in seed.facilities}
        self.supply_links = list(seed.supply_links)
        self.dispatches: dict[str, Dispatch] = {}

    # personnel
    def get_person(self, person_id):
        return self.personnel.get(person_id)

    def update_person(self, person_id, **props):
        p = self.personnel[person_id]
        for k, v in props.items():
            if k == "stock":
                p.stock.update(v)
            else:
                setattr(p, k, v)
        p.last_update = props.get("last_update", time.time())

    def list_personnel(self):
        return list(self.personnel.values())

    def list_units(self):
        return list(self.units.values())

    # logistics
    def get_drone(self, drone_id):
        return self.drones.get(drone_id)

    def list_drones(self):
        return list(self.drones.values())

    def list_depots(self):
        return list(self.depots.values())

    def list_no_fly_zones(self):
        return list(self.no_fly_zones.values())

    def add_no_fly_zone(self, zone):
        self.no_fly_zones[zone.id] = zone

    def list_facilities(self):
        return list(self.facilities.values())

    def list_supply_links(self):
        return list(self.supply_links)

    def find_resupply_sources(self, depot_id, items):
        out = []
        for link in self.supply_links:
            f = self.facilities.get(link.src_id)
            if link.dst_id == depot_id and f and all(f.stock.get(i, 0) >= q for i, q in items.items()):
                out.append((f, link))
        return sorted(out, key=lambda fl: fl[1].lead_time_min)

    # Hot path for dispatch: must stay a single pass / single query.
    def find_candidate_drones(self, items):
        return [d for d in self.drones.values()
                if d.status == "IDLE" and d.claimed_by is None and d.carries(items)]

    def update_drone(self, drone_id, **props):
        d = self.drones[drone_id]
        for k, v in props.items():
            if k == "payload":
                d.payload.update(v)
            else:
                setattr(d, k, v)

    # dispatch lifecycle
    # The concurrency guard: check-then-set under one lock, so two requests can't both win.
    def claim_drone(self, drone_id, request_id):
        with self._lock:
            d = self.drones.get(drone_id)
            if d is None or d.status != "IDLE" or d.claimed_by is not None:
                return False
            d.claimed_by = request_id
            d.status = "EN_ROUTE"
            return True

    def release_drone(self, drone_id):
        with self._lock:
            d = self.drones[drone_id]
            d.claimed_by = None
            d.status = "IDLE"

    def create_dispatch(self, dispatch):
        self.dispatches[dispatch.request_id] = dispatch

    # HOOK: called by the tick loop on arrival. Moves items from the drone into a medic's stock.
    def complete_dispatch(self, request_id, ts=None):
        disp = self.dispatches.get(request_id)
        if disp is None or disp.status == "DELIVERED":
            return disp
        drone = self.drones[disp.drone_id]
        for item, qty in disp.items.items():
            drone.payload[item] = max(0, drone.payload.get(item, 0) - qty)
        person = self.personnel.get(disp.recipient_id)
        if person is not None and person.kind == "MEDIC":
            for item, qty in disp.items.items():
                person.stock[item] = person.stock.get(item, 0) + qty
        disp.status = "DELIVERED"
        return disp

    def list_dispatches(self):
        return list(self.dispatches.values())


# HOOK: main.py builds its repo here. EDTH_REPO=turing needs `turingdb start -demon -in-memory`.
def get_repo(kind: Optional[str] = None) -> GraphRepo:
    """Build the repo named by `kind` or EDTH_REPO (default "memory"), seeded and ready."""
    kind = (kind or os.environ.get("EDTH_REPO", "memory")).lower()
    if kind == "memory":
        return InMemoryRepo()
    if kind == "turing":
        from .turing_repo import TuringRepo
        repo = TuringRepo.from_env()
        repo.load_seed(load_seed())
        return repo
    raise ValueError(f"unknown EDTH_REPO={kind!r} (expected memory or turing)")
