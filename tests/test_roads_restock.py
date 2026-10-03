"""Evacuations by road after treatment (roads.py, evac.py) and medic restocks that always get a drone
(dispatch.py: urgency, loading to order, restocking a launch site with an ETA for the medic)."""
import time

from backend.app.dispatch import DispatchEngine
from backend.app.evac import EvacTracker, TREAT_S
from backend.app.flights import FlightTracker
from backend.app.models import Dispatch, Event, NoDispatch
from backend.app.roads import ROAD_KMH, RoadNet
from backend.app.routing import Router
from backend.app.stock import StockKeeper
from backend.app.supply_chain import chain_status

_n = 0


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def world(repo):
    clock = Clock()
    engine = DispatchEngine(repo, clock=clock)
    stock = StockKeeper(repo, clock=clock, speed=60)
    flights = FlightTracker(engine, sim_speed=1.0, clock=clock, stock=stock)
    return clock, engine, flights, EvacTracker(engine, flights, stock, sim_speed=1.0, clock=clock)


def event(repo, pid, type_="CASUALTY", severity=None, items=None, urgency=None):
    global _n
    _n += 1
    p = repo.get_person(pid)
    return Event(f"evt-r{_n}", type_, pid, p.lat, p.lon, time.time() + _n, severity, items or {}, urgency)


# ---- roads ------------------------------------------------------------------------------------------

def test_road_route_follows_the_network_round_zones(repo):
    zones = repo.list_no_fly_zones()
    net, router = RoadNet(zones), Router(zones)
    a, b = (47.622, 35.602), (47.76, 35.42)  # a squad to the Role 2
    pts, metres, secs = net.route(a, b)
    assert len(pts) > 5 and pts[0] == a and pts[-1] == b
    assert all(router.clear(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    assert secs > metres / (ROAD_KMH / 3.6)  # tracks and the drive on and off are slower than the road


def test_truck_legs_in_the_sector_follow_roads(repo):
    legs = chain_status(repo)["road_legs"]
    assert len(legs["dc-02>dep-02"]) > 3  # not a straight line
    assert "sup-01>dc-03" not in legs  # Poland to Lviv keeps the national corridor


# ---- evacuation waits for treatment -----------------------------------------------------------------

def test_casualty_leaves_only_after_drone_lands_and_treatment(repo):
    clock, engine, flights, evac = world(repo)
    ev = event(repo, "sol-07", severity="CRITICAL")
    d = engine.handle(ev)
    assert isinstance(d, Dispatch)
    engine.record(d, ev)
    flights.start(d)
    msgs = evac.start(repo.get_person("sol-07"), "CRITICAL")
    assert "by road" in msgs[0]["data"]["note"]
    trip = next(iter(evac.trips.values()))
    start = trip.flight.position()
    flight_s = d.eta_s
    for _ in range(int(flight_s // 10) - 1):  # drone still flying: the casualty doesn't move
        flights.step(10)
        u = [m for m in evac.step(10) if m["type"] == "evac_update"][0]["data"]
        assert u["phase"] == "WAITING_FOR_DRONE" and trip.flight.position() == start
    for _ in range(3):
        flights.step(10)
        evac.step(10)
    assert trip.treating and trip.flight.position() == start  # drone landed: treatment, not moving yet
    for _ in range(int(TREAT_S // 10) + 2):
        flights.step(10)
        u = [m for m in evac.step(10) if m["type"] == "evac_update"][0]["data"]
    assert u["phase"] == "MOVING" and trip.flight.position() != start


# ---- restock urgency --------------------------------------------------------------------------------

def test_critical_restock_goes_ahead_of_wounded():
    crit = Event("a", "LOW_STOCK", "med-1", 0, 0, 2.0, None, {"blood_oneg": 1}, "CRITICAL")
    wounded = Event("b", "CASUALTY", "sol-1", 0, 0, 1.0, "WOUNDED")
    urgent = Event("c", "LOW_STOCK", "med-1", 0, 0, 0.0, None, {"blood_oneg": 1}, "URGENT")
    plain = Event("d", "LOW_STOCK", "med-1", 0, 0, 0.0, None, {"blood_oneg": 1})
    order = sorted([plain, urgent, wounded, crit], key=lambda e: e.priority)
    assert [e.event_id for e in order] == ["a", "b", "c", "d"]
    assert Event.from_dict({"event_id": "x", "type": "LOW_STOCK", "subject_id": "med-1", "lat": 0, "lon": 0,
                            "ts": 0, "urgency": "non-urgent"}).urgency == "NON_URGENT"


def _ground_carriers(repo, item):
    """Take `item` off every drone's payload, so no drone carries it."""
    for d in repo.list_drones():
        if d.payload.get(item):
            repo.update_drone(d.id, payload={item: 0})


def test_critical_restock_loads_a_drone_to_order(repo):
    clock, engine, flights, evac = world(repo)
    _ground_carriers(repo, "morphine_autoinjector")
    before = {d.id: d.stock.get("morphine_autoinjector", 0) for d in repo.list_depots()}
    r = engine.handle(event(repo, "med-2", "LOW_STOCK", items={"morphine_autoinjector": 2}, urgency="CRITICAL"))
    assert isinstance(r, Dispatch)
    drone = repo.get_drone(r.drone_id)
    assert drone.payload.get("morphine_autoinjector") == 2
    after = {d.id: d.stock.get("morphine_autoinjector", 0) for d in repo.list_depots()}
    assert after[drone.depot_id] == before[drone.depot_id] - 2  # taken off the launch site's shelf
    assert any(m["data"]["change"]["kind"] == "loaded_to_order" for m in flights.stock.step())


def test_restock_nobody_holds_restocks_a_launch_site_and_gives_an_eta(repo):
    clock, engine, flights, evac = world(repo)
    _ground_carriers(repo, "chest_seal")
    for d in repo.list_depots():
        repo.adjust_stock(d.id, {"chest_seal": -999})
    r = engine.handle(event(repo, "med-3", "LOW_STOCK", items={"chest_seal": 3}, urgency="URGENT"))
    assert isinstance(r, NoDispatch) and r.reason_code == "AWAITING_STOCK"
    alt = r.nearest_alternative
    assert alt["order_id"] in flights.stock.orders and alt["eta_s"] > 0 and "drone" in alt["note"]
    assert engine.queue_position(r.request_id) == 1  # never dropped
    order = flights.stock.orders[alt["order_id"]]
    clock.t += order.minutes * 60 / flights.stock.speed + 1  # the shipment lands...
    msgs = flights.step(0.5)
    sent = [m for m in msgs if m["type"] == "dispatch" and m["data"]["request_id"] == r.request_id]
    assert sent and sent[0]["data"]["recipient_id"] == "med-3"  # ...and a drone takes off with it


def test_non_urgent_waits_for_a_drone_coming_home(repo):
    clock, engine, flights, evac = world(repo)
    for d in repo.list_drones():  # every blood carrier busy
        if d.payload.get("blood_oneg") and d.status == "IDLE":
            repo.claim_drone(d.id, "busy")
    r = engine.handle(event(repo, "med-1", "LOW_STOCK", items={"blood_oneg": 1}, urgency="NON_URGENT"))
    assert isinstance(r, NoDispatch) and engine.queue_position(r.request_id) >= 1
