"""Audio geolocation end to end: POST /events -> zone in the graph -> dispatch routes round it; /zones API."""
import pytest
from fastapi.testclient import TestClient

from backend.app import roads
from backend.app.geo.project import bearing_deg, distance_m, project
from backend.app.routing import Router

MED2 = (47.6226, 35.60227)  # CHARLIE-MED1 in the seed


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EDTH_REPO", "memory")
    from backend.app import dev_server
    dev_server.world = dev_server.World()
    with TestClient(dev_server.app) as c:
        yield c, dev_server


def test_no_fly_zone_from_distance_and_bearing(client):
    c, dev = client
    r = c.post("/events", json={"type": "NO_FLY_ZONE", "subject_id": "med-2", "distance_m": 800, "bearing_deg": 45})
    assert r.status_code == 200, r.text
    body = r.json()
    centre = tuple(body["centre"])
    assert distance_m(centre, project(*MED2, 800, 45)) < 1
    zone = body["zone"]
    assert zone["properties"]["status"] == "unconfirmed"
    assert zone["properties"]["effective_radius_m"] == pytest.approx(500 + 0.35 * 800 + 10)
    ring = zone["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1]
    # in the graph, so the router treats it as a hard constraint
    nfz = {z.id: z for z in dev.world.repo.list_no_fly_zones()}
    assert zone["id"] in nfz
    router = Router(list(nfz.values()))
    west, east = project(*centre, 3000, 270), project(*centre, 3000, 90)
    assert not router.clear(west, east)
    path, _ = router.route(west, east)
    assert len(path) > 2  # detours round it
    assert c.get("/zones").json()["features"][0]["id"] == zone["id"]
    assert c.get(f"/zones/{zone['id']}").status_code == 200


def test_spoken_text_and_source_position(client):
    c, dev = client
    src = {"lat": 47.63, "lon": 35.61, "user_id": "driver_12", "accuracy_m": 25}
    r = c.post("/events", json={"type": "NO_FLY_ZONE", "source": src,
                                "text": "Ворожий дрон, вісімсот метрів на північний схід. Закрити триста метрів."})
    body = r.json()
    assert (body["distance_m"], body["bearing_deg"]) == (800, 45)
    assert body["uncertainty_m"] == pytest.approx(0.35 * 800 + 25)
    assert body["zone"]["properties"]["radius_m"] == 300
    # a second, independent reporter close by confirms and merges
    r2 = c.post("/events", json={"type": "NO_FLY_ZONE", "subject_id": "med-1",
                                 "lat": 0, "lon": 0, "distance_m": 0, "bearing_deg": 0,
                                 "source": {"lat": body["centre"][0], "lon": body["centre"][1]}})
    assert r2.json()["action"] == "updated"
    assert r2.json()["zone"]["properties"]["status"] == "confirmed"
    assert len(c.get("/zones").json()["features"]) == 1


def test_bad_reports_are_rejected(client):
    c, _ = client
    post = lambda **kw: c.post("/events", json={"type": "NO_FLY_ZONE", "subject_id": "med-2", **kw})
    assert post(distance_m=25_000, bearing_deg=0).status_code == 422  # beyond 20 km
    assert post(distance_m="nan", bearing_deg=0).status_code == 422
    assert post(distance_m=800).status_code == 422  # no bearing
    assert post(text="ворожий дрон на третій годині").status_code == 422  # clock with no heading
    assert c.post("/events", json={"type": "NO_FLY_ZONE", "source": {"lat": 50.45, "lon": 30.52},
                                   "distance_m": 100, "bearing_deg": 0}).status_code == 422  # outside the area
    ok = post(text="ворожий дрон на третій годині, 600 метрів", source={"lat": MED2[0], "lon": MED2[1],
                                                                       "heading_deg": 0, "heading_ref": "true"})
    assert ok.status_code == 200 and ok.json()["bearing_deg"] == 90


def test_delete_and_expiry_clear_the_graph(client):
    c, dev = client
    zid = c.post("/events", json={"type": "NO_FLY_ZONE", "subject_id": "med-2", "distance_m": 800,
                                  "bearing_deg": 45}).json()["zone"]["id"]
    with c.websocket_connect("/ws") as ws:
        for _ in range(4):
            ws.receive_json()
        assert c.delete(f"/zones/{zid}").status_code == 200
        msg = ws.receive_json()
        assert msg["type"] == "zone_expired" and msg["data"]["reason"] == "operator"
    assert zid not in {z.id for z in dev.world.repo.list_no_fly_zones()}
    assert c.delete(f"/zones/{zid}").status_code == 404
    zid = c.post("/events", json={"type": "NO_FLY_ZONE", "subject_id": "med-2", "distance_m": 800,
                                  "bearing_deg": 45}).json()["zone"]["id"]
    import asyncio, time
    asyncio.run(dev.geo_api.expire_due(dev.world, now=time.time() + 16 * 60))
    assert zid not in {z.id for z in dev.world.repo.list_no_fly_zones()} and c.get("/zones").json()["features"] == []


def test_road_blocked_closes_one_segment_not_the_air(client):
    c, dev = client
    n_air = len(dev.world.repo.list_no_fly_zones())
    r = c.post("/events", json={"type": "ROAD_BLOCKED", "subject_id": "med-2", "distance_m": 300, "bearing_deg": 0})
    assert r.status_code == 200, r.text
    edge = r.json()["zone"]["properties"]["edge"]
    assert edge and r.json()["snapped_m"] <= 1000
    assert len(dev.world.repo.list_no_fly_zones()) == n_air  # drones are unaffected
    net = roads.net_for(dev.world.repo.list_no_fly_zones())
    a, b = tuple(edge[0]), tuple(edge[1])
    keys = net.nodes.keys() if isinstance(net.nodes, dict) else range(len(net.nodes))  # lattice or real roads
    ka = next(k for k in keys if distance_m(net.nodes[k], a) < 0.5)
    assert all(distance_m(net.nodes[nk], b) > 0.5 for nk, _, _ in net.adj[ka])  # that segment is closed


def test_no_go_area_closes_roads_only(client):
    c, dev = client
    n_air = len(dev.world.repo.list_no_fly_zones())
    r = c.post("/events", json={"type": "NO_GO_AREA", "subject_id": "med-2", "distance_m": 400, "bearing_deg": 180})
    assert r.status_code == 200
    assert r.json()["zone"]["properties"]["radius_m"] == 200
    assert len(dev.world.repo.list_no_fly_zones()) == n_air
    assert any(z.id == r.json()["zone"]["id"] for z in roads.net_for(dev.world.repo.list_no_fly_zones()).zones)
    dev.world = dev.World()  # a reset clears the road constraints
    assert not any(z.id.startswith("geo-") for z in roads.net_for(dev.world.repo.list_no_fly_zones()).zones)


def test_destination_snaps_to_a_drop_point(client):
    c, _ = client
    med1 = (47.638558, 35.63715)
    d, b = distance_m(med1, MED2), bearing_deg(med1, MED2)
    r = c.post("/events", json={"type": "DESTINATION", "subject_id": "med-1",  # a rough guess at CHARLIE-MED1
                                "distance_m": round(d * 1.05), "bearing_deg": round(b + 3)})
    dest = r.json()["destination"]
    assert dest["snapped_to"] == "med-2" and dest["moved_m"] < 300


def test_voice_report_becomes_a_zone(client):
    c, dev = client
    text = "Чарлі, медик. Ворожий дрон, вісімсот метрів на північний схід. Закрити п'ятсот метрів."
    body = c.post("/voice/text", json={"text": text, "language": "uk"}).json()
    assert [e["type"] for e in body["events"]] == ["NO_FLY_ZONE"]
    assert body["english"].startswith("CHARLIE-MED1 reports no-fly zone 800 m at 45")
    res = body["results"][0]
    assert distance_m(tuple(res["centre"]), project(*MED2, 800, 45)) < 1
    assert any(z.id == res["zone"]["id"] for z in dev.world.repo.list_no_fly_zones())


def test_road_blocked_on_real_roads(client, monkeypatch, tmp_path):
    """Same, on downloaded roads (roads.json): a tiny two-road network round CHARLIE."""
    import json
    lat, lon = MED2
    data = {"nodes": [[lat + 0.003, lon - 0.01], [lat + 0.003, lon], [lat + 0.003, lon + 0.01],
                      [lat - 0.01, lon], [lat + 0.015, lon]],
            "ways": [["primary", [0, 1, 2]], ["secondary", [3, 1, 4]]]}
    f = tmp_path / "roads.json"
    f.write_text(json.dumps(data))
    monkeypatch.setenv("EDTH_ROADS_FILE", str(f))
    c, dev = client
    dev.world = dev.World()
    r = c.post("/events", json={"type": "ROAD_BLOCKED", "subject_id": "med-2", "distance_m": 330, "bearing_deg": 80})
    assert r.status_code == 200, r.text
    a, b = r.json()["zone"]["properties"]["edge"]
    ends = sorted(min(range(5), key=lambda i: distance_m(tuple(p), tuple(data["nodes"][i]))) for p in (a, b))
    assert ends == [1, 2]  # the road just north-east
    net = roads.net_for(dev.world.repo.list_no_fly_zones())
    assert isinstance(net, roads.RealRoadNet)
    assert 2 not in net.neighbours(1) and 0 in net.neighbours(1)
