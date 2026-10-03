"""Launch-site stock (stock.py): reloads come out of the site's stock, low sites reorder down the
supply chain, shipments land after their lead time, and a destroyed hub re-routes them."""
import time

from backend.app.dispatch import DispatchEngine
from backend.app.flights import FlightTracker
from backend.app.models import Event
from backend.app.stock import REORDER_UP_TO, StockKeeper


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def depot(repo, did):
    return next(d for d in repo.list_depots() if d.id == did)


def test_adjust_stock_never_goes_negative(repo):
    assert repo.adjust_stock("dep-01", {"blood_oneg": -5, "chest_seal": 3}) == {"blood_oneg": 7, "chest_seal": 18}
    assert repo.adjust_stock("dep-01", {"blood_oneg": -50})["blood_oneg"] == 0
    assert depot(repo, "dep-01").stock["blood_oneg"] == 0
    assert repo.adjust_stock("hos-01", {"chest_seal": 2}) == {"chest_seal": 2}  # creates the STOCKS edge


def test_reload_takes_from_the_launch_site(repo):
    keeper = StockKeeper(repo, clock=Clock())
    repo.update_drone("drn-01", payload={"blood_oneg": 0, "tourniquet": 1})
    keeper.reload(repo.get_drone("drn-01"), {"tourniquet": 2, "blood_oneg": 2, "hemostatic_gauze": 2, "chest_seal": 2})
    assert repo.get_drone("drn-01").payload["blood_oneg"] == 2
    assert repo.get_drone("drn-01").payload["tourniquet"] == 2
    north = depot(repo, "dep-01")
    assert north.stock["blood_oneg"] == 10 and north.stock["tourniquet"] == 19  # exactly what it took


def test_short_site_reloads_partly_and_reorders(repo):
    clock = Clock()
    keeper = StockKeeper(repo, clock=clock, speed=60)
    repo.update_drone("drn-04", payload={"blood_oneg": 0})
    msgs = keeper.reload(repo.get_drone("drn-04"), {"blood_oneg": 2, "tourniquet": 2})
    assert repo.get_drone("drn-04").payload["blood_oneg"] == 1  # West only had 1
    assert depot(repo, "dep-02").stock["blood_oneg"] == 0
    kinds = [m["data"]["change"]["kind"] for m in msgs]
    assert kinds == ["reload", "order_placed"]
    assert msgs[0]["data"]["change"]["short"] == {"blood_oneg": 1}
    order = next(iter(keeper.orders.values()))
    # Fastest source holding 12 blood: the Role 2 hospital, relayed through Launch Site Rear by drone.
    assert order.path == ["hos-01", "dep-03", "dep-02"] and order.minutes == 35
    assert order.items == {"blood_oneg": REORDER_UP_TO}
    hos = next(f for f in repo.list_facilities() if f.id == "hos-01")
    assert hos.stock["blood_oneg"] == 24 - REORDER_UP_TO  # taken from the source when it ships
    assert keeper.check("dep-02") == []  # one shipment at a time

    clock.t += 34  # 34 lead minutes at 60x
    assert keeper.step() == []
    clock.t += 2
    arrived = keeper.step()
    assert arrived[0]["data"]["change"]["kind"] == "order_arrived"
    assert depot(repo, "dep-02").stock["blood_oneg"] == REORDER_UP_TO


def test_destroyed_hub_reroutes_shipment(repo):
    clock = Clock()
    keeper = StockKeeper(repo, clock=clock, speed=60)
    repo.adjust_stock("dep-03", {"blood_oneg": -6})  # Rear can't relay
    repo.adjust_stock("hos-01", {"blood_oneg": -24})  # and the Role 2 hospital has none
    msgs = keeper.check("dep-02")
    order = keeper.orders["ord-1"]
    # From the regional Role 3 hospital, through the forward distribution point.
    assert order.path == ["hos-03", "dc-02", "dep-02"], msgs
    clock.t += 1
    repo.set_facility_status("dc-02", "DESTROYED")
    out = keeper.site_changed("dc-02", "DESTROYED")
    assert [m["data"]["change"]["kind"] for m in out] == ["order_lost", "order_placed"]
    assert order.status == "LOST" and keeper.orders["ord-2"].replaces == "ord-1"
    # Re-sent by helicopter from the Dnipro Role 3, through the empty Role 2 hospital and Launch Site Rear.
    assert keeper.orders["ord-2"].path == ["hos-02", "hos-01", "dep-03", "dep-02"]
    # A site the shipment has already left behind doesn't strand it.
    assert keeper.site_changed("dc-03", "DESTROYED") == []


def test_restock_arrival_serves_the_queue(repo):
    """West's only drone comes home, can't fully reload blood, a CRITICAL waits; the shipment lands,
    the drone is topped up and goes."""
    clock = Clock()
    engine = DispatchEngine(repo, clock=clock)
    tracker = FlightTracker(engine, sim_speed=1.0, clock=clock, stock=StockKeeper(repo, clock=clock, speed=60))
    for d in repo.list_drones():
        if d.id != "drn-04":
            repo.update_drone(d.id, status="CHARGING")
    p = repo.get_person("sol-03")
    first = engine.handle(Event("e1", "CASUALTY", p.id, p.lat, p.lon, clock(), "CRITICAL"))
    engine.record(first)
    tracker.start(first)
    p = repo.get_person("sol-07")
    engine.handle(Event("e2", "CASUALTY", p.id, p.lat, p.lon, clock(), "CRITICAL"))
    kinds = []
    for _ in range(400):  # 10 s steps
        clock.t += 10
        kinds += [m["data"]["change"]["kind"] if m["type"] == "stock_update" else m["type"] for m in tracker.step(10.0)]
        if not engine.pending():
            break
    assert "order_placed" in kinds and "order_arrived" in kinds
    assert kinds.index("order_arrived") < kinds.index("dispatch")  # it waited for the blood
    assert not engine.pending()
    assert repo.get_drone("drn-04").status == "EN_ROUTE"
