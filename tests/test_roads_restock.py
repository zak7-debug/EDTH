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


def test_casualty_waits_for_the_medics_drone_not_their_own(repo):
    """Drones go to medics only: a casualty in a squad whose medic has a drone on the way waits for it."""
    clock, engine, flights, evac = world(repo)
    ev = event(repo, "med-2", "LOW_STOCK", items={"blood_oneg": 2}, urgency="CRITICAL")  # CHARLIE's medic
    d = engine.handle(ev)
    engine.record(d)
    flights.start(d)
    evac.start(repo.get_person("sol-10"), "CRITICAL")  # CHARLIE-4
    trip = next(iter(evac.trips.values()))
    u = [m for m in evac.step(1) if m["type"] == "evac_update"][0]["data"]
    assert u["phase"] == "WAITING_FOR_DRONE" and not u["treated"]
    other = evac.start(repo.get_person("sol-03"), "CRITICAL")  # ALPHA: no drone coming, treated at once
    assert "in 10 min" in other[0]["data"]["note"]


# ---- a higher-ranked medic takes over a drone already flying ----------------------------------------

def _fly(engine, flights, ev):
    d = engine.handle(ev)
    assert isinstance(d, Dispatch)
    engine.record(d, ev)
    flights.start(d)
    return d


def test_critical_restock_takes_over_a_drone_flying_to_a_non_urgent_medic(repo):
    clock, engine, flights, evac = world(repo)
    low = event(repo, "med-1", "LOW_STOCK", items={"blood_oneg": 2}, urgency="NON_URGENT")
    first = _fly(engine, flights, low)
    flights.step(5)  # in the air
    for d in repo.list_drones():  # nothing else free
        if d.status == "IDLE" and not d.claimed_by:
            repo.claim_drone(d.id, "busy")
    r = engine.handle(event(repo, "med-2", "LOW_STOCK", items={"blood_oneg": 2}, urgency="CRITICAL"))
    assert isinstance(r, Dispatch) and r.drone_id == first.drone_id and r.items == {"blood_oneg": 2}
    assert repo.get_drone(r.drone_id).claimed_by == r.request_id
    assert next(x for x in repo.list_dispatches() if x.request_id == low.event_id).status == "DIVERTED"
    out = flights.stock.step()
    assert any(m["data"]["change"] and m["data"]["change"]["kind"] == "diverted" for m in out)
    assert any(q.event_id.startswith(low.event_id) for q in engine.pending())  # ALPHA still served
    engine.record(r)
    flights.start(r)
    for _ in range(200):
        flights.step(5)
    assert next(x for x in repo.list_dispatches() if x.request_id == r.request_id).status == "DELIVERED"


def test_take_over_sends_a_second_drone_for_what_the_first_lacks(repo):
    clock, engine, flights, evac = world(repo)
    repo.update_drone("drn-08", payload={"morphine_autoinjector": 0})
    low = event(repo, "med-1", "LOW_STOCK", items={"morphine_autoinjector": 1}, urgency="NON_URGENT")
    first = _fly(engine, flights, low)
    flights.step(5)
    r = engine.handle(event(repo, "med-2", "LOW_STOCK", items={"morphine_autoinjector": 2}, urgency="CRITICAL"))
    assert isinstance(r, Dispatch) and r.drone_id == first.drone_id and r.items == {"morphine_autoinjector": 1}
    sent = [m["data"] for m in flights.step(0.1) if m["type"] == "dispatch"]  # broadcast on the next tick
    top = [d for d in sent if d["recipient_id"] == "med-2"]
    assert top and top[0]["items"] == {"morphine_autoinjector": 1} and top[0]["drone_id"] != r.drone_id


def test_equal_or_lower_rank_never_takes_over(repo):
    clock, engine, flights, evac = world(repo)
    first = _fly(engine, flights, event(repo, "med-1", "LOW_STOCK", items={"blood_oneg": 2}, urgency="URGENT"))
    flights.step(5)
    for d in repo.list_drones():
        if d.status == "IDLE" and not d.claimed_by:
            repo.claim_drone(d.id, "busy")
    r = engine.handle(event(repo, "med-2", "LOW_STOCK", items={"blood_oneg": 2}, urgency="URGENT"))
    assert not (isinstance(r, Dispatch) and r.drone_id == first.drone_id)


# ---- real roads (scripts/fetch_roads.py -> roads.json) ----------------------------------------------

def _fake_osm():
    """An Overpass-shaped answer: a curvy road every 0.05 deg each way across the sector, with
    shape points every ~300 m, sharing nodes where they cross."""
    from backend.app.roads import BBOX
    s, w, n, e = BBOX
    els, ids, ways = [], {}, []

    def node(lat, lon):
        k = (round(lat, 6), round(lon, 6))
        if k not in ids:
            ids[k] = len(ids) + 1
            els.append({"type": "node", "id": ids[k], "lat": k[0], "lon": k[1]})
        return ids[k]

    import math as m
    lats = [s + 0.05 * i for i in range(int((n - s) / 0.05) + 1)]
    lons = [w + 0.05 * j for j in range(int((e - w) / 0.05) + 1)]
    for i, lat in enumerate(lats):  # east-west, wiggling between the crossings
        pts = [node(lat + (0.004 * m.sin(k / 3) if k % 15 else 0), lon) for k, lon in
               enumerate(w + 0.05 * x / 15 for x in range(15 * (len(lons) - 1) + 1))]
        ways.append({"type": "way", "id": 10_000 + i, "nodes": pts, "tags": {"highway": "secondary"}})
    for j, lon in enumerate(lons):
        pts = [node(s + 0.05 * y / 15, lon) for y in range(15 * (len(lats) - 1) + 1)]
        ways.append({"type": "way", "id": 20_000 + j, "nodes": pts, "tags": {"highway": "tertiary"}})
    ways.append({"type": "way", "id": 30_000, "nodes": [node(48.6, 36.5), node(48.7, 36.6)],
                 "tags": {"highway": "primary"}})  # an island: dropped
    ways.append({"type": "way", "id": 30_001, "nodes": pts[:2], "tags": {"highway": "footway"}})  # not a road
    return {"elements": els + ways}


def test_real_roads_compile_and_route_round_zones(repo, tmp_path, monkeypatch):
    import json
    from backend.app import roads
    data = roads.compile_osm(_fake_osm())
    assert len(data["ways"]) == len(_fake_osm()["elements"]) - sum(1 for e in _fake_osm()["elements"]
                                                                   if e["type"] == "node") - 2
    f = tmp_path / "roads.json"
    f.write_text(json.dumps(data))
    monkeypatch.setenv("EDTH_ROADS_FILE", str(f))
    zones = repo.list_no_fly_zones()
    net = roads.net_for(zones)
    assert isinstance(net, roads.RealRoadNet)
    a, b = (47.622, 35.602), (47.76, 35.42)
    pts, metres, secs = net.route(a, b)
    on_road = {tuple(p) for p in data["nodes"]}
    assert len(pts) > 10 and all(tuple(p) in on_road for p in pts[2:-2])  # every waypoint is on a road
    assert all(tuple(p) in set(net.nodes) for p in (pts[1], pts[-2]))  # joined and left along a road
    router = Router(zones)
    assert all(router.clear(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    legs = chain_status(repo)["road_legs"]
    assert all(tuple(p) in {(round(x, 5), round(y, 5)) for x, y in on_road} for p in legs["dc-02>dep-02"][2:-2])
    monkeypatch.setenv("EDTH_ROADS", "grid")  # the invented lattice is still there as a fallback
    assert isinstance(roads.net_for(zones), roads.RoadNet)


def test_real_roads_joined_alongside_not_at_the_far_end():
    """OpenStreetMap gives a straight road two nodes: a squad beside its middle joins it there,
    rather than driving cross-country to one end kilometres away."""
    from backend.app import roads
    from backend.app.routing import haversine_m
    data = {"nodes": [[47.60, 35.50], [47.60, 35.60], [47.70, 35.60]], "ways": [["tertiary", [0, 1, 2]]]}
    net = roads.RealRoadNet(data)
    squad, dest = (47.603, 35.55), (47.70, 35.60)  # 330 m north of a 7.5 km road, 3.7 km from either end
    pts, metres, secs = net.route(squad, dest)
    assert haversine_m(squad, pts[1]) < 400 and (47.60, 35.60) in pts
    assert roads._dijkstra(net, squad, (47.61, 35.56), net.nodes.__getitem__)[0] != [squad, (47.61, 35.56)]  # 1.3 km: by road
