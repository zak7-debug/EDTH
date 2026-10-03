"""WebSocket message builders. Every message on /ws is {"type": ..., "data": ...}.

The formats are frozen in contracts/messages.md; the frontend (Arnav) and the backend
(Sasank) both code against that file, so change the two together.
"""
from __future__ import annotations

from typing import Iterable

from .models import Dispatch, Event, NoDispatch
from .repo import GraphRepo

WS_TYPES = ("snapshot", "event", "dispatch", "no_dispatch", "drone_update", "delivered", "queue")


def msg(type_: str, data: dict) -> dict:
    assert type_ in WS_TYPES, f"unknown ws message type {type_!r}"
    return {"type": type_, "data": data}


# HOOK: sent on /ws connect and returned by GET /state. Arnav's map loads everything from this.
def snapshot(repo: GraphRepo) -> dict:
    """Whole world, sent on connect. GET /state returns the same `data`."""
    return msg("snapshot", {
        "units": [{"id": u.id, "callsign": u.callsign} for u in repo.list_units()],
        "personnel": [p.to_dict() for p in repo.list_personnel()],
        "drones": [d.to_dict() for d in repo.list_drones()],
        "depots": [d.to_dict() for d in repo.list_depots()],
        "no_fly_zones": [z.to_dict() for z in repo.list_no_fly_zones()],
        "facilities": [f.to_dict() for f in repo.list_facilities()],
        "supply_links": [link.to_dict() for link in repo.list_supply_links()],
        "dispatches": [d.to_dict() for d in repo.list_dispatches() if d.status == "EN_ROUTE"],
    })


def event_msg(event: Event, received_ts: float) -> dict:
    return msg("event", {**event.to_dict(), "received_ts": received_ts})


def dispatch_msg(d: Dispatch) -> dict:
    return msg("dispatch", d.to_dict())


def no_dispatch_msg(nd: NoDispatch) -> dict:
    return msg("no_dispatch", nd.to_dict())


def drone_update_msg(drone_id: str, lat: float, lon: float, status: str,
                     eta_s: float | None = None, request_id: str | None = None) -> dict:
    return msg("drone_update", {"drone_id": drone_id, "lat": lat, "lon": lon,
                                "status": status, "eta_s": eta_s, "request_id": request_id})


def delivered_msg(d: Dispatch, ts: float) -> dict:
    return msg("delivered", {"request_id": d.request_id, "drone_id": d.drone_id,
                             "recipient_id": d.recipient_id, "items": d.items, "ts": ts})


def queue_msg(pending: Iterable[Event]) -> dict:
    """Pending requests, already in triage order (CRITICAL, WOUNDED, LOW_STOCK; oldest first)."""
    return msg("queue", {"pending": [
        {"request_id": e.event_id, "type": e.type, "severity": e.severity,
         "subject_id": e.subject_id, "items": e.items, "ts": e.ts, "position": i + 1}
        for i, e in enumerate(pending)
    ]})
