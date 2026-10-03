"""Road and rail blocks: ground traffic reroutes round them, drones don't notice them."""

import pytest
from fastapi.testclient import TestClient

from backend.app import dev_server as ds
from backend.app.blocks import BLOCKS, Blocks, route_from
from backend.app.models import SupplyLink
from backend.app.routing import Router
from backend.app.supply_chain import best_path


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EDTH_REPO", "memory")
    with TestClient(ds.app) as c:
        c.post("/reset")
        yield c
    BLOCKS.clear()


def _clear_of(zone, pts):
    r = Router([zone])
    pts = [tuple(p) for p in pts]
    return all(r.clear(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def test_road_block_reroutes_ambulance_but_not_drones(client):
    zones_before = len(ds.world.repo.list_no_fly_zones())
    client.post("/events", json={"type": "CASUALTY", "subject_id": "sol-03", "severity": "CRITICAL"})
    trip = next(iter(ds.world.evac.trips.values()))
    pts = trip.flight.points
    mid = pts[len(pts) * 2 // 3]  # a junction well ahead on its road
    r = client.post("/blocks", json={"kind": "ROAD", "lat": mid[0], "lon": mid[1]}).json()
    assert r["block"]["kind"] == "ROAD" and trip.evac.person_id in r["rerouted"]
    zone = BLOCKS.road_zones()[0]
    assert _clear_of(zone, ds.world.evac.trips[trip.evac.evac_id].flight.points)
    assert len(ds.world.repo.list_no_fly_zones()) == zones_before  # drones never see a road block


def test_road_block_redraws_truck_leg(client):
    from backend.app.supply_chain import chain_status
    legs = chain_status(ds.world.repo)["road_legs"]
    key, pts = max(legs.items(), key=lambda kv: len(kv[1]))
    mid = pts[len(pts) // 2]
    client.post("/blocks", json={"kind": "ROAD", "lat": mid[0], "lon": mid[1]})
    after = chain_status(ds.world.repo)
    assert after["blocks"] and after["road_legs"][key] != pts
    assert _clear_of(BLOCKS.road_zones()[0], after["road_legs"][key][1:-1])
    lift = client.post("/blocks/lift", json={"id": after["blocks"][0]["id"]}).json()
    assert lift["lifted"] and chain_status(ds.world.repo)["road_legs"][key] == pts


def test_rail_block_needs_a_link(client):
    assert client.post("/blocks", json={"kind": "RAIL", "lat": 48.5, "lon": 33}).status_code == 422


def test_cut_rail_line_reroutes_train_in_transit(client):
    stock, repo = ds.world.stock, ds.world.repo
    facilities, depots, links = repo.list_facilities(), repo.list_depots(), repo.list_supply_links()
    path = route_from("dc-04", "dc-01", facilities, depots, links)
    assert [l["mode"] for l in path["legs"]] == ["RAIL"]  # straight down the rail line
    stock.send("dc-01", {"blood_oneg": 5}, path, take_from_source=False)
    r = client.post("/blocks", json={"kind": "RAIL", "lat": 48.5, "lon": 32.3, "links": ["dc-04>dc-01|RAIL"]}).json()
    assert r["rerouted"]
    live = [o for o in stock.orders.values() if o.status == "IN_TRANSIT" and o.depot_id == "dc-01"]
    assert len(live) == 1 and live[0].source_id == "dc-04"
    assert ("dc-04", "dc-01") not in {(l["src_id"], l["dst_id"]) for l in live[0].legs}


def test_cut_links_are_skipped_both_ways():
    b = Blocks()
    links = [SupplyLink("a", "b", 10, "RAIL"), SupplyLink("b", "a", 10, "RAIL"), SupplyLink("a", "b", 30, "TRUCK")]
    b.add("RAIL", 0, 0, links=["a>b|RAIL"])
    assert [(l.src_id, l.dst_id, l.mode) for l in b.open_links(links)] == [("a", "b", "TRUCK")]
