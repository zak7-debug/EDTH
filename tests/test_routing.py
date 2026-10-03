"""Routing around threat zones (backend/app/routing.py)."""
import time

from backend.app.dispatch import DispatchEngine, straight_line
from backend.app.models import Event, NoFlyZone
from backend.app.routing import Router, haversine_m

SQUARE = NoFlyZone("z", "box", [(0.0, 0.0), (0.0, 0.01), (0.01, 0.01), (0.01, 0.0)])


def test_clear_line_stays_straight():
    pts, m = Router([SQUARE]).route((0.02, 0.0), (0.02, 0.01))
    assert pts == [(0.02, 0.0), (0.02, 0.01)]
    assert abs(m - haversine_m(*pts)) < 1e-6


def test_blocked_line_bends_round_the_zone():
    r = Router([SQUARE])
    a, b = (-0.005, 0.005), (0.015, 0.005)  # straight through the middle of the box
    assert r.crosses_zone(a, b) == "box"
    pts, m = r.route(a, b)
    assert len(pts) > 2 and pts[0] == a and pts[-1] == b
    assert all(r.clear(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    assert m > haversine_m(a, b)


def test_seed_routes_avoid_zones(repo):
    """DEMO: every Launch Site North -> squad line crosses the EW jamming zone, so all must bend."""
    r = Router.from_repo(repo)
    north = repo.list_depots()[0]
    assert north.id == "dep-01"
    for p in repo.list_personnel():
        a, b = (north.lat, north.lon), (p.lat, p.lon)
        t = time.perf_counter()
        pts, _ = r.route(a, b)
        assert (time.perf_counter() - t) < 0.02  # well under the latency budget
        assert all(r.clear(pts[i], pts[i + 1]) for i in range(len(pts) - 1)), p.id


def test_engine_routes_round_zones_by_default(repo):
    p = repo.get_person("sol-01")
    ev = Event("t1", "CASUALTY", p.id, p.lat, p.lon, time.time(), "CRITICAL")
    res = DispatchEngine(repo).handle(ev)
    assert res.drone_id
    r = Router.from_repo(repo)
    assert all(r.clear(res.route[i], res.route[i + 1]) for i in range(len(res.route) - 1))
    assert straight_line  # still importable as a fallback
