"""Seed data for both graphs, around Grafenwöhr Training Area (Bavaria, centre ~49.70 N, 11.93 E).

Plain Python data so both repos (in-memory and TuringDB) load exactly the same world.
`load_seed()` returns fresh copies every call, so tests and repos never share mutable state.

Layout, designed around the demo scenario:
- 3 squads (20 personnel, one medic each) patrol west of the central impact area.
- 3 depots: Main Post (north-east, the far side of the impact area), Range 301 (south-west)
  and Vilseck LZ (far south-west).
- 2 no-fly zones: the central impact area sits between Main Post and the squads, so drones
  from Main Post must route around it; a small artillery firing point lies to the south.
- 8 drones with deliberately uneven payloads: some lack blood, one has a low battery,
  one is charging. That gives the dispatch engine real choices and gives the demo a
  "no drone available" case once the blood carriers are busy.
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass

from .models import Depot, Drone, NoFlyZone, Person, Unit

CENTRE = (49.70, 11.93)


@dataclass
class SeedData:
    units: list[Unit]
    personnel: list[Person]
    depots: list[Depot]
    drones: list[Drone]
    no_fly_zones: list[NoFlyZone]


UNITS = [
    Unit("unit-1", "BADGER 1"),
    Unit("unit-2", "BADGER 2"),
    Unit("unit-3", "BADGER 3"),
]

# Squad patrol centres (lat, lon). All west of the impact area.
_SQUAD_CENTRES = {
    "unit-1": (49.705, 11.872),
    "unit-2": (49.688, 11.858),
    "unit-3": (49.672, 11.884),
}
_SQUAD_SIZES = {"unit-1": 7, "unit-2": 7, "unit-3": 6}  # incl. one medic each -> 20 total

MEDIC_THRESHOLDS = {
    "tourniquet": 2,
    "blood_oneg": 2,
    "chest_seal": 2,
    "hemostatic_gauze": 2,
    "morphine_autoinjector": 2,
}
# Stock per medic. med-2 starts short of blood: that is the demo's LOW_STOCK event.
_MEDIC_STOCK = {
    "med-1": {"tourniquet": 4, "blood_oneg": 3, "chest_seal": 3, "hemostatic_gauze": 4, "morphine_autoinjector": 4},
    "med-2": {"tourniquet": 3, "blood_oneg": 1, "chest_seal": 2, "hemostatic_gauze": 3, "morphine_autoinjector": 3},
    "med-3": {"tourniquet": 4, "blood_oneg": 2, "chest_seal": 3, "hemostatic_gauze": 3, "morphine_autoinjector": 2},
}

DEPOTS = [
    Depot("dep-01", "Main Post", 49.7230, 11.9480),
    Depot("dep-02", "Range 301", 49.6610, 11.8400),
    Depot("dep-03", "Vilseck LZ", 49.6250, 11.8150),
]

# (id, callsign, depot, speed m/s, range left m, max range m, capacity, status, payload)
_DRONES = [
    ("drn-01", "HAWK 1", "dep-01", 25.0, 40000, 40000, 8, "IDLE",
     {"tourniquet": 2, "blood_oneg": 2, "hemostatic_gauze": 2, "chest_seal": 2}),
    ("drn-02", "HAWK 2", "dep-01", 25.0, 40000, 40000, 8, "IDLE",
     {"tourniquet": 2, "blood_oneg": 4, "hemostatic_gauze": 2}),
    ("drn-03", "HAWK 3", "dep-01", 22.0, 35000, 35000, 6, "IDLE",
     {"tourniquet": 3, "chest_seal": 3}),
    ("drn-04", "FALCON 1", "dep-02", 28.0, 30000, 30000, 6, "IDLE",
     {"tourniquet": 2, "blood_oneg": 2, "hemostatic_gauze": 1, "morphine_autoinjector": 1}),
    ("drn-05", "FALCON 2", "dep-02", 28.0, 4000, 30000, 6, "IDLE",  # low battery: out of range for most trips
     {"tourniquet": 2, "blood_oneg": 2, "hemostatic_gauze": 2}),
    ("drn-06", "FALCON 3", "dep-02", 20.0, 25000, 25000, 6, "IDLE",
     {"chest_seal": 3, "hemostatic_gauze": 3}),
    ("drn-07", "OWL 1", "dep-03", 18.0, 45000, 45000, 10, "IDLE",
     {"tourniquet": 3, "blood_oneg": 4, "hemostatic_gauze": 3}),
    ("drn-08", "OWL 2", "dep-03", 18.0, 10000, 45000, 10, "CHARGING",
     {"tourniquet": 2, "blood_oneg": 2, "chest_seal": 2, "morphine_autoinjector": 2}),
]

NO_FLY_ZONES = [
    NoFlyZone("nfz-1", "Central impact area", [
        (49.716, 11.893), (49.721, 11.925), (49.708, 11.945),
        (49.690, 11.938), (49.686, 11.905), (49.700, 11.890),
    ]),
    NoFlyZone("nfz-2", "Artillery firing point", [
        (49.652, 11.900), (49.656, 11.918), (49.646, 11.922), (49.642, 11.904),
    ]),
]


def _build_personnel(rng: random.Random) -> list[Person]:
    people: list[Person] = []
    sol_n = 0
    for i, unit in enumerate(UNITS, start=1):
        clat, clon = _SQUAD_CENTRES[unit.id]
        jitter = lambda: (rng.uniform(-0.002, 0.002), rng.uniform(-0.003, 0.003))  # ~±200 m
        med_id = f"med-{i}"
        dlat, dlon = jitter()
        people.append(Person(
            id=med_id, kind="MEDIC", callsign=f"{unit.callsign}-DOC", unit_id=unit.id,
            lat=round(clat + dlat, 6), lon=round(clon + dlon, 6),
            stock=dict(_MEDIC_STOCK[med_id]), stock_threshold=dict(MEDIC_THRESHOLDS),
        ))
        for j in range(1, _SQUAD_SIZES[unit.id]):
            sol_n += 1
            dlat, dlon = jitter()
            people.append(Person(
                id=f"sol-{sol_n:02d}", kind="SOLDIER", callsign=f"{unit.callsign}-{j}",
                unit_id=unit.id, lat=round(clat + dlat, 6), lon=round(clon + dlon, 6),
            ))
    return people


def _build_drones() -> list[Drone]:
    depots = {d.id: d for d in DEPOTS}
    return [
        Drone(id=i, callsign=cs, depot_id=dep, lat=depots[dep].lat, lon=depots[dep].lon,
              speed_mps=spd, range_m=rng_m, max_range_m=max_m, capacity=cap,
              status=st, payload=dict(payload))
        for (i, cs, dep, spd, rng_m, max_m, cap, st, payload) in _DRONES
    ]


def load_seed(seed: int = 42) -> SeedData:
    """Fresh, deterministic copy of the whole world."""
    return SeedData(
        units=copy.deepcopy(UNITS),
        personnel=_build_personnel(random.Random(seed)),
        depots=copy.deepcopy(DEPOTS),
        drones=_build_drones(),
        no_fly_zones=copy.deepcopy(NO_FLY_ZONES),
    )


if __name__ == "__main__":
    s = load_seed()
    print(f"{len(s.units)} units, {len(s.personnel)} personnel "
          f"({sum(p.kind == 'MEDIC' for p in s.personnel)} medics), "
          f"{len(s.drones)} drones, {len(s.depots)} depots, {len(s.no_fly_zones)} no-fly zones")
