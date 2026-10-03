"""geo/zones.py: polygons, uncertainty, merging and expiry."""
import math

import pytest

from backend.app.geo.project import distance_m
from backend.app.geo.zones import ZoneStore, circle_polygon, geojson_ring, load_config

C = (47.65, 35.59)


def test_polygon_vertex_count_and_radius():
    poly = circle_polygon(*C, 500, n=32, circumscribe=False)
    assert len(poly) == 32
    assert all(abs(distance_m(C, p) - 500) < 1 for p in poly)


def test_circumscribed_polygon_contains_the_circle():
    for n in (6, 16, 32):
        poly = circle_polygon(*C, 500, n=n)
        mids = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(poly, poly[1:] + poly[:1])]
        assert all(distance_m(C, p) >= 499 for p in mids)  # the flat sides, not just the corners
        assert all(abs(distance_m(C, p) - 500 / math.cos(math.pi / n)) < 1 for p in poly)


def test_geojson_ring_is_closed_and_lon_lat():
    poly = circle_polygon(*C, 500, n=16)
    ring = geojson_ring(poly)
    assert len(ring) == 17 and ring[0] == ring[-1]
    assert ring[0][0] == pytest.approx(poly[0][1], abs=1e-6)  # [lon, lat]


def test_config_loads_yaml_with_defaults():
    cfg = load_config()
    assert cfg["types"]["NO_FLY_ZONE"]["radius_m"] == 500 and cfg["types"]["NO_GO_AREA"]["radius_m"] == 200
    assert cfg["types"]["ROAD_BLOCKED"]["radius_m"] == 50


def _store():
    return ZoneStore(load_config())


def test_zone_is_expanded_by_uncertainty():
    s = _store()
    u = s.uncertainty_m(800)
    assert u == pytest.approx(0.35 * 800 + 10)
    z, action = s.report("NO_FLY_ZONE", C, reporter=(47.64, 35.58), event_id="e1", uncertainty_m=u, now=0)
    assert action == "created" and z.status == "unconfirmed"
    assert z.effective_radius_m == pytest.approx(500 + u)
    assert z.expires_at == 15 * 60
    f = z.feature()
    assert f["properties"]["kind"] == "NO_FLY_ZONE" and f["geometry"]["type"] == "Polygon"


def test_nearby_reports_merge_confirm_and_extend():
    s = _store()
    z1, _ = s.report("NO_FLY_ZONE", C, reporter=C, event_id="e1", reporter_id="med-1", now=0)
    near = (C[0] + 0.001, C[1])  # ~110 m north
    z2, action = s.report("NO_FLY_ZONE", near, reporter=C, event_id="e2", reporter_id="med-2", now=600)
    assert action == "updated" and z2.id == z1.id and len(s.active()) == 1
    assert z2.status == "confirmed"
    assert z2.expires_at == 600 + 15 * 60
    # the merged circle still covers both reports' circles
    assert all(distance_m(z2.centre, r.centre) + r.effective_m <= z2.effective_radius_m + 1e-6 for r in z2.reports)


def test_same_reporter_twice_stays_unconfirmed_and_far_reports_stay_apart():
    s = _store()
    s.report("NO_FLY_ZONE", C, reporter=C, event_id="e1", reporter_id="med-1", now=0)
    z, _ = s.report("NO_FLY_ZONE", C, reporter=C, event_id="e2", reporter_id="med-1", now=1)
    assert z.status == "unconfirmed"
    s.report("NO_FLY_ZONE", (C[0] + 0.01, C[1]), reporter=C, event_id="e3", now=2)  # ~1.1 km away
    s.report("NO_GO_AREA", C, reporter=C, event_id="e4", now=3)  # different kind: never merges
    assert len(s.active()) == 3


def test_chained_reports_do_not_walk_the_zone():
    s = _store()
    for k in range(5):  # each 150 m further north than the last
        s.report("NO_FLY_ZONE", (C[0] + k * 0.00135, C[1]), reporter=C, event_id=f"e{k}", now=k)
    assert len(s.active()) >= 3  # merged only within 200 m of a zone's FIRST report


def test_expiry_and_delete():
    s = _store()
    a, _ = s.report("NO_FLY_ZONE", C, reporter=C, event_id="e1", now=0)
    b, _ = s.report("ROAD_BLOCKED", (47.6, 35.5), reporter=C, event_id="e2", now=0,
                    edge=((47.6, 35.5), (47.61, 35.5)))
    assert [z.id for z in s.expire(now=15 * 60)] == [a.id]
    assert s.blocked_edges() == [((47.6, 35.5), (47.61, 35.5))]
    assert s.delete(b.id) is b and s.active() == []
