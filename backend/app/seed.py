"""Seed data for both graphs: a fictional brigade medical network in south-east Ukraine.

Real geography, fictional military laydown. The sector sits in open farmland between
Zaporizhzhia and Orikhiv (~47.65 N, 35.60 E); upstream nodes are placed at region level
(Rzeszów region in Poland, Lviv, Kyiv, Dnipro, Zaporizhzhia). Every base, route, unit position
and facility name here is invented for the demo. None of it is a real military site,
a named real hospital, or a real supply route, and it must stay that way: publishing a
plausible map of real Ukrainian medical logistics could help someone target it.

Plain Python data so both repos (in-memory and TuringDB) load exactly the same world.
`load_seed()` returns fresh copies every call, so tests and repos never share mutable state.

The whole medical chain is modelled, rear to front:
  donor hub (PL) -> Lviv hub -> Dnipro hub -> Zaporizhzhia forward point -> 3 drone launch sites
  -> 8 drones -> 3 medics and 17 soldiers, with a Kyiv blood service, two hospitals and a Role 1
  aid station alongside (where casualties are evacuated to, evac.py).
Upstream levels mostly matter for "no drone can serve this": the answer can then say which
launch site must reload a drone and where that site gets restocked from.

Layout, designed around the demo scenario:
- 3 squads (20 personnel, one medic each) hold positions at the south of the sector.
- 3 launch sites: North (behind an EW jamming zone), West, and Rear (by the Role 2 hospital).
- 2 threat zones (stored as no-fly zones): an EW jamming zone sits between Launch Site North
  and the squads, so drones from there must route around it; an air-defence threat area
  lies further south-west.
- 8 drones with deliberately uneven payloads: some lack blood, one has a low battery,
  one is charging. That gives the dispatch engine real choices and gives the demo a
  "no drone available" case once the blood carriers are busy.
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass

from .models import Depot, Drone, Facility, NoFlyZone, Person, SupplyLink, Unit

CENTRE = (47.65, 35.60)


@dataclass
class SeedData:
    units: list[Unit]
    personnel: list[Person]
    depots: list[Depot]
    drones: list[Drone]
    no_fly_zones: list[NoFlyZone]
    facilities: list[Facility]
    supply_links: list[SupplyLink]


# DEMO: four teams, each two medics (ALPHA-MED1, ALPHA-MED2) and eight soldiers (ALPHA-1 .. ALPHA-8).
# Said on the radio in Ukrainian: «Альфа, медик один», «Альфа три» (voice.py).
UNITS = [
    Unit("unit-1", "ALPHA"),
    Unit("unit-2", "CHARLIE"),
    Unit("unit-3", "DELTA"),
    Unit("unit-4", "ECHO"),
]

# Squad positions (lat, lon), south of the EW jamming zone.
# DEMO: where each squad stands. Keep them out of NO_FLY_ZONES and within drone range.
_SQUAD_CENTRES = {
    "unit-1": (47.638, 35.640),
    "unit-2": (47.622, 35.602),
    "unit-3": (47.608, 35.662),
    "unit-4": (47.630, 35.718),
}
# The first medic and soldiers of the first three teams keep the ids the demo script and tests use
# (med-1..3, sol-01..17); the rest are added after them: med-4..8, sol-18..32.
_SQUAD_SIZES = {"unit-1": 7, "unit-2": 7, "unit-3": 6, "unit-4": 1}  # first medic + original soldiers
TEAM_MEDICS, TEAM_SOLDIERS = 2, 8  # -> 40 people

# TUNE: below these levels a medic counts as LOW_STOCK (Person.low_items).
MEDIC_THRESHOLDS = {
    "tourniquet": 2,
    "blood_oneg": 2,
    "chest_seal": 2,
    "hemostatic_gauze": 2,
    "morphine_autoinjector": 2,
}
# Stock per medic. med-2 starts short of blood: that is the demo's LOW_STOCK event.
# DEMO: med-2 is 1 blood under threshold so the low-stock event fires straight away.
_MEDIC_STOCK = {
    "med-1": {"tourniquet": 4, "blood_oneg": 3, "chest_seal": 3, "hemostatic_gauze": 4, "morphine_autoinjector": 4},
    "med-2": {"tourniquet": 3, "blood_oneg": 1, "chest_seal": 2, "hemostatic_gauze": 3, "morphine_autoinjector": 3},
    "med-3": {"tourniquet": 4, "blood_oneg": 2, "chest_seal": 3, "hemostatic_gauze": 3, "morphine_autoinjector": 2},
}
_SECOND_MEDIC_STOCK = {"tourniquet": 3, "blood_oneg": 2, "chest_seal": 2, "hemostatic_gauze": 3, "morphine_autoinjector": 2}

# Drone launch sites. Stock is what drones reload from. Launch Site West is short of blood on purpose.
# DEMO: launch sites. Moving Launch Site North changes whether the EW zone sits on its route.
DEPOTS = [
    Depot("dep-01", "Launch Site North", 47.7000, 35.7000,
          {"tourniquet": 20, "blood_oneg": 12, "chest_seal": 15, "hemostatic_gauze": 20, "morphine_autoinjector": 10}),
    Depot("dep-02", "Launch Site West", 47.6600, 35.5400,
          {"tourniquet": 10, "blood_oneg": 1, "chest_seal": 8, "hemostatic_gauze": 10, "morphine_autoinjector": 6}),
    Depot("dep-03", "Launch Site Rear", 47.7350, 35.4800,
          {"tourniquet": 12, "blood_oneg": 6, "chest_seal": 10, "hemostatic_gauze": 12, "morphine_autoinjector": 8}),
]

# Upstream of the launch sites, placed at region level (city-centre coordinates or open country).
# Names are generic on purpose: no real depots, hospitals or routes.
# DEMO: upstream supply chain. Region-level only, generic names: keep it fictional.
FACILITIES = [
    Facility("sup-01", "SUPPLIER", "International donor hub (Rzeszów region, PL)", 50.0400, 22.0000,
             {"tourniquet": 2000, "blood_oneg": 0, "chest_seal": 1500, "hemostatic_gauze": 2500, "morphine_autoinjector": 800}),
    Facility("sup-02", "SUPPLIER", "National blood service (Kyiv region)", 50.4500, 30.5200,
             {"blood_oneg": 400}),
    Facility("dc-03", "DISTRIBUTION_CENTRE", "Western medical logistics hub (Lviv region)", 49.8400, 24.0300,
             {"tourniquet": 600, "blood_oneg": 20, "chest_seal": 400, "hemostatic_gauze": 700, "morphine_autoinjector": 250}),
    Facility("dc-01", "DISTRIBUTION_CENTRE", "Eastern medical logistics hub (Dnipro region)", 48.4600, 35.0400,
             {"tourniquet": 150, "blood_oneg": 40, "chest_seal": 120, "hemostatic_gauze": 150, "morphine_autoinjector": 60}),
    Facility("dc-02", "DISTRIBUTION_CENTRE", "Forward distribution point (Zaporizhzhia region)", 47.8400, 35.1400,
             {"tourniquet": 40, "blood_oneg": 10, "chest_seal": 30, "hemostatic_gauze": 40, "morphine_autoinjector": 20}),
    Facility("hos-01", "HOSPITAL", "Role 2 field hospital (sector rear)", 47.7600, 35.4200,
             {"blood_oneg": 24, "tourniquet": 10, "hemostatic_gauze": 10}, role="ROLE_2", beds=20),
    Facility("hos-02", "HOSPITAL", "Role 3 hospital (Dnipro region)", 48.4700, 35.0000,
             {"blood_oneg": 80, "tourniquet": 20, "chest_seal": 20, "hemostatic_gauze": 30, "morphine_autoinjector": 20},
             role="ROLE_3", beds=150),
    # National tier: more hubs and a second blood source, joined by rail. Generic names, region level only.
    Facility("sup-03", "SUPPLIER", "Pharmaceutical manufacturer (Kyiv region)", 50.3000, 30.2500,
             {"tourniquet": 1500, "chest_seal": 800, "hemostatic_gauze": 1800, "morphine_autoinjector": 1200}),
    Facility("sup-04", "SUPPLIER", "Regional blood centre (Dnipro region)", 48.4200, 35.1200,
             {"blood_oneg": 150}),
    Facility("dc-04", "DISTRIBUTION_CENTRE", "Central medical logistics hub (Vinnytsia region)", 49.2300, 28.4700,
             {"tourniquet": 500, "blood_oneg": 30, "chest_seal": 300, "hemostatic_gauze": 500, "morphine_autoinjector": 200}),
    Facility("dc-05", "DISTRIBUTION_CENTRE", "Regional medical depot (Poltava region)", 49.5900, 34.5500,
             {"tourniquet": 200, "blood_oneg": 20, "chest_seal": 150, "hemostatic_gauze": 200, "morphine_autoinjector": 80}),
    Facility("hos-03", "HOSPITAL", "Role 3 hospital (Zaporizhzhia region)", 47.8700, 35.0600,
             {"blood_oneg": 40, "tourniquet": 15, "chest_seal": 15, "hemostatic_gauze": 20, "morphine_autoinjector": 15},
             role="ROLE_3", beds=60),
    # DEMO: a Role 1 aid station just behind the squads, where WOUNDED casualties go first.
    # Few beds and no chest seals on purpose, so the first WOUNDED evacuation shows a drone flying
    # the missing kit there while the casualty is still on the road (evac.py).
    Facility("aid-01", "HOSPITAL", "Role 1 aid station (sector)", 47.6680, 35.5550,
             {"tourniquet": 4, "hemostatic_gauze": 2, "morphine_autoinjector": 4}, role="ROLE_1", beds=4),
]

# Who restocks whom, with transport lead time in minutes.
# DEMO: who restocks whom, lead time in minutes. find_resupply_sources reads these.
SUPPLY_LINKS = [
    SupplyLink("sup-01", "dc-03", 180, "TRUCK"),
    SupplyLink("dc-03", "dc-01", 720, "TRUCK"),
    SupplyLink("sup-02", "hos-02", 360, "TRUCK"),
    SupplyLink("hos-02", "dc-01", 20, "TRUCK"),
    SupplyLink("hos-02", "hos-01", 45, "HELO"),
    SupplyLink("dc-01", "dc-02", 90, "TRUCK"),
    SupplyLink("dc-02", "dep-01", 60, "TRUCK"),
    SupplyLink("dc-02", "dep-02", 50, "TRUCK"),
    SupplyLink("dc-02", "dep-03", 40, "TRUCK"),
    SupplyLink("hos-01", "dep-03", 15, "TRUCK"),
    # DEMO: backup links, so knocking out one hub leaves a slower working chain to find.
    SupplyLink("dc-01", "dep-01", 150, "TRUCK"),  # long road straight from the Dnipro hub
    SupplyLink("dc-02", "hos-01", 30, "TRUCK"),
    SupplyLink("dep-03", "dep-01", 25, "DRONE"),  # launch sites relay stock to each other by cargo drone
    SupplyLink("dep-03", "dep-02", 20, "DRONE"),
    # National tier: rail between region hubs, so a hub lost in the west or centre has a way round.
    SupplyLink("sup-01", "dc-04", 600, "TRUCK"),
    SupplyLink("dc-03", "dc-04", 420, "RAIL"),
    SupplyLink("sup-03", "dc-04", 240, "TRUCK"),
    SupplyLink("sup-03", "dc-05", 360, "RAIL"),
    SupplyLink("dc-04", "dc-05", 480, "RAIL"),
    SupplyLink("dc-04", "dc-01", 660, "RAIL"),
    SupplyLink("dc-05", "dc-01", 240, "RAIL"),
    SupplyLink("sup-02", "dc-05", 300, "TRUCK"),
    # Blood comes from two places: the national service far to the north and a regional centre.
    SupplyLink("sup-04", "hos-02", 20, "TRUCK"),
    SupplyLink("sup-04", "dc-01", 20, "TRUCK"),
    SupplyLink("sup-04", "hos-03", 120, "TRUCK"),
    # The regional Role 3 hospital backs up the forward point and the sector's Role 2.
    SupplyLink("dc-01", "hos-03", 90, "TRUCK"),
    SupplyLink("hos-03", "dc-02", 20, "TRUCK"),
    SupplyLink("hos-03", "hos-01", 60, "TRUCK"),
    SupplyLink("dc-05", "dc-02", 300, "RAIL"),
]

# (id, callsign, depot, speed m/s, range left m, max range m, capacity, status, payload)
# DEMO: the fleet. Uneven on purpose (see module docstring). tests/test_dispatch.py assumes:
# drn-04 is nearest to unit-2, drn-05 lacks range, drn-03/06 carry no blood, drn-08 is charging.
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

# Threat zones, stored as no-fly zones: drones route around them.
# DEMO: threat zones as (lat, lon) polygons. nfz-1 must stay between Launch Site North and the squads
# for the reroute beat (checked by hand; see docs/foundations-guide.md).
NO_FLY_ZONES = [
    NoFlyZone("nfz-1", "EW jamming zone", [
        (47.690, 35.650), (47.688, 35.690), (47.668, 35.705),
        (47.648, 35.690), (47.650, 35.655), (47.670, 35.640),
    ]),
    NoFlyZone("nfz-2", "Air-defence threat area", [
        (47.590, 35.555), (47.593, 35.585), (47.580, 35.592), (47.576, 35.562),
    ]),
]


# Cover for temporary sites (resilience.py): where a stand-in hospital or distribution point can set up
# out of sight. Invented terrain tags, not survey data: real geography at region level, generic names,
# no real buildings, villages or positions. WOODLAND = tree cover (camouflage nets, vehicle hides);
# STRUCTURES = disused farm or industrial buildings on a town's outskirts (roof cover, hard standing).
# Deliberately no "inside a town" kind: medical units rely on protected status, and setting up among
# civilians would put both at risk. (id, kind, name, lat, lon, radius_m)
# DEMO / TUNE: three sit 4 to 6 km from the Role 2 hospital, two near the forward distribution point.
@dataclass
class CoverArea:
    id: str
    kind: str  # WOODLAND | STRUCTURES
    name: str
    lat: float
    lon: float
    radius_m: float

    def to_dict(self) -> dict:
        return dict(self.__dict__)


COVER_AREAS = [
    CoverArea("cov-01", "WOODLAND", "Woodland belt along a river valley (sector rear)", 47.7850, 35.3750, 1200),
    CoverArea("cov-02", "STRUCTURES", "Disused farm buildings (sector rear)", 47.7420, 35.3650, 400),
    CoverArea("cov-03", "WOODLAND", "Shelterbelt woodland (sector rear)", 47.7200, 35.4100, 700),
    CoverArea("cov-04", "STRUCTURES", "Disused industrial site on a town's outskirts (sector rear)", 47.8050, 35.4700, 600),
    CoverArea("cov-05", "WOODLAND", "Mixed woodland (Zaporizhzhia region)", 47.8750, 35.2050, 1500),
    CoverArea("cov-06", "STRUCTURES", "Disused warehouses on a town's outskirts (Zaporizhzhia region)", 47.8080, 35.2050, 600),
    CoverArea("cov-07", "WOODLAND", "Shelterbelt woodland (sector)", 47.6920, 35.5150, 600),
    CoverArea("cov-08", "WOODLAND", "Woodland strip (sector)", 47.7000, 35.5850, 700),
]

def _build_personnel(rng: random.Random) -> list[Person]:
    people: list[Person] = []
    jitter = lambda: (rng.uniform(-0.002, 0.002), rng.uniform(-0.003, 0.003))  # ~±200 m

    def medic(pid: str, unit: Unit, k: int, stock: dict) -> Person:
        clat, clon = _SQUAD_CENTRES[unit.id]
        dlat, dlon = jitter()
        return Person(id=pid, kind="MEDIC", callsign=f"{unit.callsign}-MED{k}", unit_id=unit.id,
                      lat=round(clat + dlat, 6), lon=round(clon + dlon, 6),
                      stock=dict(stock), stock_threshold=dict(MEDIC_THRESHOLDS))

    def soldier(pid: str, unit: Unit, j: int) -> Person:
        clat, clon = _SQUAD_CENTRES[unit.id]
        dlat, dlon = jitter()
        return Person(id=pid, kind="SOLDIER", callsign=f"{unit.callsign}-{j}", unit_id=unit.id,
                      lat=round(clat + dlat, 6), lon=round(clon + dlon, 6))

    # The original people first, in the original order, so their ids and positions don't change.
    sol_n, med_n, count = 0, 0, {}
    for i, unit in enumerate(UNITS, start=1):
        if unit.id == "unit-4":
            continue
        med_n += 1
        people.append(medic(f"med-{i}", unit, 1, _MEDIC_STOCK[f"med-{i}"]))
        for j in range(1, _SQUAD_SIZES[unit.id]):
            sol_n += 1
            people.append(soldier(f"sol-{sol_n:02d}", unit, j))
        count[unit.id] = (1, _SQUAD_SIZES[unit.id] - 1)
    # Then fill every team up to two medics and eight soldiers.
    for unit in UNITS:
        meds, sols = count.get(unit.id, (0, 0))
        for k in range(meds + 1, TEAM_MEDICS + 1):
            med_n += 1
            people.append(medic(f"med-{med_n}", unit, k, _SECOND_MEDIC_STOCK))
        for j in range(sols + 1, TEAM_SOLDIERS + 1):
            sol_n += 1
            people.append(soldier(f"sol-{sol_n:02d}", unit, j))
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
        facilities=copy.deepcopy(FACILITIES),
        supply_links=copy.deepcopy(SUPPLY_LINKS),
    )


if __name__ == "__main__":
    s = load_seed()
    print(f"{len(s.units)} units, {len(s.personnel)} personnel "
          f"({sum(p.kind == 'MEDIC' for p in s.personnel)} medics), "
          f"{len(s.drones)} drones, {len(s.depots)} depots, {len(s.no_fly_zones)} no-fly zones, "
          f"{len(s.facilities)} upstream facilities, {len(s.supply_links)} supply links")
