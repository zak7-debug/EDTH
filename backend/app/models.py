"""Shared data model. Every layer (repo, dispatch, API, simulator) speaks these types.

Ids carry a type prefix: sol-07, med-02, unit-1, drn-04, dep-01, nfz-1.
Coordinates are WGS84 decimal degrees. Distances in metres, speeds in m/s, times in seconds
(epoch seconds for timestamps).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

# One shared supply vocabulary for events, medic stock and drone payloads.
ITEMS = ("tourniquet", "blood_oneg", "chest_seal", "hemostatic_gauze", "morphine_autoinjector")

PersonKind = Literal["SOLDIER", "MEDIC"]
PersonStatus = Literal["OK", "WOUNDED", "CRITICAL"]
DroneStatus = Literal["IDLE", "EN_ROUTE", "RETURNING", "CHARGING"]
EventType = Literal["CASUALTY", "LOW_STOCK"]
Severity = Literal["CRITICAL", "WOUNDED"]

# Lower number is served first by the triage queue.
TRIAGE_PRIORITY = {"CRITICAL": 0, "WOUNDED": 1, "LOW_STOCK": 2}


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

    def low_items(self) -> dict[str, int]:
        """Items below threshold, with the quantity needed to get back to threshold."""
        return {
            item: thr - self.stock.get(item, 0)
            for item, thr in self.stock_threshold.items()
            if self.stock.get(item, 0) < thr
        }

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Depot:
    id: str
    name: str
    lat: float
    lon: float

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
        )

    @property
    def priority(self) -> int:
        return TRIAGE_PRIORITY[self.severity if self.type == "CASUALTY" else "LOW_STOCK"]

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
    status: Literal["EN_ROUTE", "DELIVERED"] = "EN_ROUTE"

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
