"""WebSocket message builders. Every message on /ws is {"type": ..., "data": ...}.

The formats are frozen in contracts/messages.md; the frontend (Arnav) and the backend
(Sasank) both code against that file, so change the two together.
"""
from __future__ import annotations

from typing import Iterable, Optional

from .models import Dispatch, Event, NoDispatch
from .repo import GraphRepo

WS_TYPES = ("snapshot", "event", "dispatch", "no_dispatch", "drone_update", "delivered", "queue", "query_log", "zone_added", "reroute", "drone_lost", "supply_chain",
            "stock_update", "evacuation", "evac_update", "admitted", "voice_report", "site_deployed",
            "site_ready")


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
        "evacuations": [e.to_dict() for e in repo.list_evacuations() if e.status == "EN_ROUTE"],
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
    """Pending requests, already in triage order (models.TRIAGE_PRIORITY / RESTOCK_PRIORITY; oldest first)."""
    return msg("queue", {"pending": [
        {"request_id": e.event_id, "type": e.type, "severity": e.severity, "urgency": e.urgency,
         "subject_id": e.subject_id, "items": e.items, "ts": e.ts, "position": i + 1}
        for i, e in enumerate(pending)
    ]})


def query_log_msg(request_id: str, phase: str, entries: list[dict]) -> dict:
    """Graph queries one request ran. phase: "decide" (inside the timed decision) or "record"
    (the writes after the broadcast). entries come from querylog.capture()."""
    return msg("query_log", {"request_id": request_id, "phase": phase, "queries": entries,
                             "total_ms": round(sum(e["ms"] for e in entries), 2)})


def zone_added_msg(zone) -> dict:
    """A new threat / no-fly zone, drawn on the map the moment it is reported."""
    return msg("zone_added", zone.to_dict())


def reroute_msg(drone_id: str, request_id: str, phase: str, route: list, distance_m: float,
                eta_s: float, added_m: float, zone_name: str) -> dict:
    """A drone already in the air changed course round a new zone. route starts at its current position."""
    return msg("reroute", {"drone_id": drone_id, "request_id": request_id, "phase": phase,
                           "route": [list(p) for p in route], "distance_m": round(distance_m, 1),
                           "eta_s": round(eta_s, 1), "added_m": round(added_m, 1), "zone": zone_name})


def drone_lost_msg(drone_id: str, lat: float, lon: float, phase: Optional[str], request_id: Optional[str],
                   recipient_id: Optional[str], items_lost: dict) -> dict:
    """A drone was shot down where it was flying. If it was carrying someone's supplies, a `dispatch`
    (or a queued `no_dispatch`) for the retry follows, with request_id = original + "-r1"."""
    return msg("drone_lost", {"drone_id": drone_id, "lat": lat, "lon": lon, "phase": phase,
                              "request_id": request_id, "recipient_id": recipient_id,
                              "items_lost": items_lost})


def supply_chain_msg(chain: dict, changed: Optional[dict] = None) -> dict:
    """Every launch site's current restock route (supply_chain.chain_status) and, after a site is
    destroyed or restored, which one changed: {"facility_id", "status"}."""
    return msg("supply_chain", {**chain, "changed": changed})


def stock_update_msg(state: dict, change: Optional[dict] = None) -> dict:
    """Launch-site and facility stock, beds, and restock shipments on the way (stock.StockKeeper.state).
    change = {"kind", "note", ...} says what just happened: reload, order_placed, order_arrived,
    order_lost, order_failed, kit_delivered, treated. None when sent on connect."""
    return msg("stock_update", {**state, "change": change})


def evacuation_msg(evac, facility_name: Optional[str], kit_eta_s: Optional[float] = None,
                   note: Optional[str] = None, reason: Optional[str] = None) -> dict:
    """A casualty is being evacuated (or re-routed / diverted: same person, new route).
    evac is None when no facility can take them; reason then says why."""
    data = evac.to_dict() if evac else {"evac_id": None}
    return msg("evacuation", {**data, "facility_name": facility_name, "kit_eta_s": kit_eta_s,
                              "note": note, "reason": reason, "diverted_from": None})


def evac_update_msg(evac_id: str, person_id: str, lat: float, lon: float, phase: str, eta_s: float,
                    treated: bool = False) -> dict:
    """Where an evacuation is now. phase: WAITING_FOR_DRONE (the casualty's supplies haven't landed),
    TREATING (treated and loaded, TREAT_S after the drone lands) or MOVING (by road). `treated` turns true
    once the medic's TREAT_S is up: the dashboard marks the soldier treated."""
    return msg("evac_update", {"evac_id": evac_id, "person_id": person_id, "lat": lat, "lon": lon,
                               "phase": phase, "eta_s": round(eta_s, 1), "treated": treated})


def admitted_msg(evac, facility_name: str, kit_used: dict, kit_short: dict, beds_used: int, beds: int) -> dict:
    """The casualty arrived. kit_short is anything the facility still lacked when they got there."""
    return msg("admitted", {"evac_id": evac.evac_id, "person_id": evac.person_id, "facility_id": evac.facility_id,
                            "facility_name": facility_name, "kit_used": kit_used, "kit_short": kit_short,
                            "beds_used": beds_used, "beds": beds})



def voice_report_msg(report_id: str, transcript: str, language: Optional[str], english: str, events: list[dict],
                     unparsed: list[str], stt: str, stt_ms: float, parse_ms: float) -> dict:
    """A medic's radio report, heard and parsed (voice.py). Sent before the `event` / `dispatch`
    messages it causes; each event's event_id is the request_id of its dispatch."""
    return msg("voice_report", {"report_id": report_id, "transcript": transcript, "language": language,
                                "english": english, "events": events, "unparsed": unparsed,
                                "stt": stt, "stt_ms": stt_ms, "parse_ms": parse_ms})


def site_deployed_msg(facility, links, replaces: str, ms: float, **plan) -> dict:
    """A temporary site's team was sent in place of a destroyed site (resilience.py). It is SETTING_UP:
    the map shows it with a countdown (plan["setup"]["ready_in_s"], real seconds). plan also carries
    `where`, `cover`, `convoy` and `drone`. An updated `supply_chain` and `stock_update`s for the team
    and convoy follow; `site_ready` comes when it opens."""
    return msg("site_deployed", {"facility": facility.to_dict(), "links": [l.to_dict() for l in links],
                                 "replaces": replaces, "ms": ms, **plan})


def site_ready_msg(facility, ms: float) -> dict:
    """A temporary site finished setting up and is OPERATIONAL (resilience.finish_setup). An updated
    `supply_chain`, any `evacuation`s it diverts and the drone with blood follow."""
    return msg("site_ready", {"facility": facility.to_dict(), "ms": ms})
