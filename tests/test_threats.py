"""Live threats: a zone added mid-mission is stored, used by new decisions, and reroutes drones in flight."""
import time

from backend.app.dev_server import DEMO_THREAT, _hexagon
from backend.app.dispatch import DispatchEngine
from backend.app.flights import FlightTracker
from backend.app.models import Event, NoFlyZone
from backend.app.routing import Router

ZONE = NoFlyZone("nfz-live-1", DEMO_THREAT["name"],
                 _hexagon(DEMO_THREAT["lat"], DEMO_THREAT["lon"], DEMO_THREAT["radius_m"]))


def test_add_zone_is_stored_and_replaced_by_id(repo):
    before = len(repo.list_no_fly_zones())
    repo.add_no_fly_zone(ZONE)
    repo.add_no_fly_zone(NoFlyZone(ZONE.id, "moved", ZONE.polygon))
    zones = {z.id: z for z in repo.list_no_fly_zones()}
    assert len(zones) == before + 1
    assert zones[ZONE.id].name == "moved"
    assert [tuple(p) for p in zones[ZONE.id].polygon] == [tuple(p) for p in ZONE.polygon]


def test_drone_in_flight_reroutes_round_new_threat(repo):
    engine = DispatchEngine(repo)
    tracker = FlightTracker(engine, sim_speed=1.0)
    p = repo.get_person("sol-03")
    d = engine.handle(Event("t1", "CASUALTY", p.id, p.lat, p.lon, time.time(), "CRITICAL"))
    tracker.start(d)
    assert d.drone_id == "drn-04" and len(d.route) == 2  # DEMO: FALCON 1 flies straight at first
    tracker.step(60.0)  # a minute in, the threat is still ahead of it

    repo.add_no_fly_zone(ZONE)
    engine.zones_changed()
    msgs = tracker.reroute(ZONE)
    assert [m["data"]["drone_id"] for m in msgs] == ["drn-04"]
    r = msgs[0]["data"]
    assert r["added_m"] > 0 and len(r["route"]) > 2
    threat = Router([ZONE])
    pts = [tuple(x) for x in r["route"]]
    assert all(threat.clear(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    assert tracker.reroute(ZONE) == []  # already clear: no second reroute

    # New decisions route round it too.
    q = repo.get_person("sol-05")
    d2 = engine.handle(Event("t2", "CASUALTY", q.id, q.lat, q.lon, time.time(), "CRITICAL"))
    assert all(threat.clear(d2.route[i], d2.route[i + 1]) for i in range(len(d2.route) - 1))
