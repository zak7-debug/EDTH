"""Live geolocation (gps.py): POST /positions, the simulated feed's random walk, and its field reports."""
import asyncio
import random

import pytest
from fastapi.testclient import TestClient

from backend.app import gps
from backend.app.geo.project import distance_m, project
from backend.app.routing import Router


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EDTH_REPO", "memory")
    from backend.app import dev_server
    dev_server.world = dev_server.World()
    with TestClient(dev_server.app) as c:
        yield c, dev_server


def test_post_positions_moves_and_broadcasts(client):
    c, dev = client
    p = dev.world.repo.list_personnel()[0]
    to = project(p.lat, p.lon, 100, 90)
    with c.websocket_connect("/ws") as ws:
        for _ in range(4):  # snapshot, queue, supply_chain, stock_update
            ws.receive_json()
        r = c.post("/positions", json={"positions": [{"id": p.id, "lat": to[0], "lon": to[1], "accuracy_m": 8},
                                                     {"id": "nobody", "lat": 1, "lon": 2}]})
        assert r.status_code == 200, r.text
        assert r.json() == {"moved": [p.id], "skipped": ["nobody"]}
        m = ws.receive_json()
    assert m == {"type": "positions", "data": {"positions": [{"id": p.id, "lat": to[0], "lon": to[1]}]}}
    moved = dev.world.repo.get_person(p.id)
    assert (moved.lat, moved.lon) == to
    assert c.post("/positions", json={}).status_code == 422


def test_live_toggle_and_reset(client):
    c, _ = client
    assert c.get("/geo/live").json()["on"] is False
    assert c.post("/geo/live", json={"on": True}).json()["on"] is True
    assert c.get("/geo/live").json()["on"] is True
    c.post("/reset")
    assert c.get("/geo/live").json()["on"] is False


def test_live_step_moves_only_ok_people_and_never_into_a_zone(client):
    c, dev = client
    repo = dev.world.repo
    people = repo.list_personnel()
    hurt = next(p for p in people if p.kind == "SOLDIER")
    repo.update_person(hurt.id, status="CRITICAL")
    # An air threat right next to one unhurt soldier: they must not step into it.
    near = next(p for p in people if p.kind == "SOLDIER" and p.id != hurt.id)
    c.post("/threats", json={"name": "test", "lat": project(near.lat, near.lon, 120, 0)[0],
                             "lon": near.lon, "radius_m": 90})
    live = gps.LiveGps(random.Random(7))
    gps_before = {p.id: (p.lat, p.lon) for p in repo.list_personnel()}
    live.set(True, repo.list_personnel(), now=0)
    router = Router(repo.list_no_fly_zones())
    seen = set()
    for _ in range(60):
        for m in live.step(repo):
            seen.add(m["id"])
            assert repo.get_person(m["id"]).status == "OK"
            assert not gps.in_zone(router, (m["lat"], m["lon"]))
    assert hurt.id not in seen and len(seen) > 5
    after = repo.get_person(hurt.id)
    assert (after.lat, after.lon) == gps_before[hurt.id]
    for p in repo.list_personnel():
        if p.id in seen:
            centre = live.centres[p.unit_id]
            assert distance_m((p.lat, p.lon), centre) <= max(gps.LEASH_M, distance_m(gps_before[p.id], centre)) + 1


def test_live_step_broadcasts_positions(client):
    c, dev = client
    c.post("/geo/live", json={"on": True})
    with c.websocket_connect("/ws") as ws:
        for _ in range(4):
            ws.receive_json()
        out = c.portal.call(dev.live_gps_step, dev.world, True, False)
        m = ws.receive_json()
    assert m["type"] == "positions"
    assert [p["id"] for p in m["data"]["positions"]] == [p["id"] for p in out["moved"]]
    assert 0 < len(out["moved"]) <= gps.MOVERS_PER_STEP


def test_live_report_creates_expiring_zones(client):
    c, dev = client
    dev.world.gps.rng = random.Random(3)
    c.post("/geo/live", json={"on": True})
    kinds = []
    for _ in range(2):
        out = asyncio.run(dev.live_gps_step(dev.world, step=False, report=True))
        assert out["report"] is not None
        kinds.append(out["report"]["type"])
    assert kinds == ["ROAD_BLOCKED", "NO_FLY_ZONE"]
    feats = c.get("/zones").json()["features"]
    assert {f["properties"]["kind"] for f in feats} == {"ROAD_BLOCKED", "NO_FLY_ZONE"}
    zones = dev.world.geo.store.active()
    for z in zones:
        assert z.expires_at - z.created_at == pytest.approx(gps.LIVE_ZONE_TTL_S)
    # the air threat is in the graph, so routing avoids it
    assert any(z.id.startswith("geo-nfz") for z in dev.world.repo.list_no_fly_zones())
    asyncio.run(dev.geo_api.expire_due(dev.world, now=max(z.expires_at for z in zones) + 1))
    assert c.get("/zones").json()["features"] == []
