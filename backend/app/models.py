"""Shared data model. Every layer (repo, dispatch, API, simulator) speaks these types.

Ids carry a type prefix: sol-07, med-2, unit-1, drn-04, dep-01, nfz-1, sup-01, dc-01, hos-01.

The medical supply chain, rear to front:
  Supplier -> DistributionCentre / Hospital -> Depot (drone launch site) -> Drone -> Medic / Soldier
Each level holds stock (Facility.stock, Depot.stock); SupplyLink says who restocks whom.
Coordinates are WGS84 decimal degrees. Distances in metres, speeds in m/s, times in seconds
(epoch seconds for timestamps).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

# TUNE: one shared supply vocabulary for events, medic stock and drone payloads. Add an item here and it flows into TuringDB, seed and messages
# automatically; then give some drones / medics / sites stock of it in seed.py.
ITEMS = ("tourniquet", "blood_oneg", "chest_seal", "hemostatic_gauze", "morphine_autoinjector")

PersonKind = Literal["SOLDIER", "MEDIC"]
# ADMITTED: arrived at a hospital or aid station (evac.py). On the way they keep their severity.
PersonStatus = Literal["OK", "WOUNDED", "CRITICAL", "ADMITTED"]
DroneStatus = Literal["IDLE", "EN_ROUTE", "RETURNING", "CHARGING", "LOST"]
EventType = Literal["CASUALTY", "LOW_STOCK"]
Severity = Literal["CRITICAL", "WOUNDED"]

# TUNE: queue order when drones are busy (dispatch.py). Lower number = served first.
TRIAGE_PRIORITY = {"CRITICAL": 0, "WOUNDED": 2, "LOW_STOCK": 4}
# TUNE: a medic's restock request says how soon they need it (LOW_STOCK `urgency`). A CRITICAL restock
# (about to run out with casualties on their hands) goes ahead of WOUNDED casualties; URGENT after them;
# NON_URGENT (the default) last. CRITICAL and URGENT also get a drone loaded to order (dispatch.py).
RESTOCK_PRIORITY = {"CRITICAL": 1, "URGENT": 3, "NON_URGENT": 4}
Urgency = Literal["CRITICAL", "URGENT", "NON_URGENT"]


@dataclass
class Unit:
    id: str
    callsign: str


@dataclass
class Person:
    id: str
    kind: PersonKind
    callsign: str
    unit_id: str
    lat: float
    lon: float
    status: PersonStatus = "OK"
    last_update: float = 0.0
    # Medics only: current stock and the level below which they report LOW_STOCK.
    stock: dict[str, int] = field(default_factory=dict)
    stock_threshold: dict[str, int] = field(default_factory=dict)

    # Used by dispatch.needed_items() when a LOW_STOCK event doesn't list items.
    def low_items(self) -> dict[str, int]:
        """Items below threshold, with the quantity needed to get back to threshold."""
        return {
            item: thr - self.stock.get(item, 0)
            for item, thr in self.stock_threshold.items()
            if self.stock.get(item, 0) < thr
        }

    def to_dict(self) -> dict:
        return asdict(self)


FacilityKind = Literal["SUPPLIER", "DISTRIBUTION_CENTRE", "HOSPITAL"]
TransportMode = Literal["TRUCK", "HELO", "DRONE"]


@dataclass
class Depot:
    """Drone launch site, the last link before the battlefield. Drones reload from its stock."""
    id: str
    name: str
    lat: float
    lon: float
    stock: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Facility:
    """Upstream medical node: a supplier (rear), a distribution centre, or a hospital.

    Hospitals both hold stock (e.g. a blood bank that can restock a nearby depot) and are where
    casualties are evacuated to; `role` is the NATO treatment level (ROLE_2, ROLE_3)."""
    id: str
    kind: FacilityKind
    name: str
    lat: float
    lon: float
    stock: dict[str, int] = field(default_factory=dict)
    role: str = ""  # hospitals only: ROLE_1 (aid station), ROLE_2, ROLE_3
    beds: int = 0  # hospitals only
    beds_used: int = 0  # taken by admitted patients and by casualties already on the way (evac.py)
    status: str = "OPERATIONAL"  # or "DESTROYED": the supply chain routes round it (supply_chain.py)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SupplyLink:
    """src restocks dst (Supplier -> DC/Hospital, DC/Hospital -> Depot)."""
    src_id: str
    dst_id: str
    lead_time_min: float
    mode: TransportMode = "TRUCK"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Drone:
    id: str
    callsign: str
    depot_id: str
    lat: float
    lon: float
    speed_mps: float
    range_m: float  # remaining range on current battery
    max_range_m: float
    capacity: int  # max total items it can carry
    status: DroneStatus = "IDLE"
    payload: dict[str, int] = field(default_factory=dict)
    claimed_by: Optional[str] = None  # request_id currently holding this drone

    # True if this drone holds at least the quantity of every requested item.
    def carries(self, items: dict[str, int]) -> bool:
        return all(self.payload.get(i, 0) >= q for i, q in items.items())

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NoFlyZone:
    id: str
    name: str
    polygon: list[tuple[float, float]]  # [(lat, lon), ...], not closed

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "polygon": [list(p) for p in self.polygon]}


@dataclass
class Event:
    """One input from the simulator or real telemetry. Wire format: contracts/messages.md."""
    event_id: str
    type: EventType
    subject_id: str
    lat: float
    lon: float
    ts: float
    severity: Optional[Severity] = None  # CASUALTY only
    items: dict[str, int] = field(default_factory=dict)  # LOW_STOCK: what is needed
    urgency: Optional[Urgency] = None  # LOW_STOCK only: CRITICAL / URGENT / NON_URGENT (None = NON_URGENT)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(
            event_id=d["event_id"],
            type=d["type"],
            subject_id=d["subject_id"],
            lat=float(d["lat"]),
            lon=float(d["lon"]),
            ts=float(d["ts"]),
            severity=d.get("severity"),
            items={k: int(v) for k, v in (d.get("items") or {}).items()},
            urgency=(d.get("urgency") or "").upper().replace("-", "_") or None,
        )

    # Triage level used by the dispatch queue (see TRIAGE_PRIORITY).
    @property
    def priority(self) -> int:
        if self.type == "CASUALTY":
            return TRIAGE_PRIORITY[self.severity]
        return RESTOCK_PRIORITY.get(self.urgency or "NON_URGENT", TRIAGE_PRIORITY["LOW_STOCK"])

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Dispatch:
    request_id: str  # equals the event_id that caused it
    drone_id: str
    recipient_id: str
    items: dict[str, int]
    eta_s: float
    distance_m: float
    route: list[tuple[float, float]]  # [(lat, lon), ...] from drone to recipient
    latency_ms: float  # event received -> dispatch decided
    ts: float
    status: Literal["EN_ROUTE", "DELIVERED", "LOST", "DIVERTED"] = "EN_ROUTE"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["route"] = [list(p) for p in self.route]
        return d


@dataclass
class NoDispatch:
    request_id: str
    recipient_id: str
    reason: str  # human-readable, e.g. "no drone carries blood_oneg"
    reason_code: Literal["NO_STOCK", "ALL_BUSY", "OUT_OF_RANGE"]
    nearest_alternative: Optional[dict] = None  # {"drone_id", "eta_s", "note"}
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


EvacStatus = Literal["EN_ROUTE", "ADMITTED", "DIVERTED"]


@dataclass
class Evacuation:
    """One casualty on the way to a hospital or aid station (evac.py).

    `kit` is what the destination will use to treat them; `shortfall` is the part of it the
    destination didn't have, flown in by drone (`resupply_request_id`) while the casualty travels."""
    evac_id: str
    person_id: str
    facility_id: str
    severity: str
    route: list[tuple[float, float]]  # [(lat, lon), ...] from the casualty to the facility
    distance_m: float
    eta_s: float  # mission seconds, including the time to treat and load before moving (evac.LOAD_S)
    ts: float
    kit: dict[str, int] = field(default_factory=dict)
    shortfall: dict[str, int] = field(default_factory=dict)
    resupply_request_id: Optional[str] = None
    status: EvacStatus = "EN_ROUTE"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["route"] = [list(p) for p in self.route]
        return d
