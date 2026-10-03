"""Safeguarding medical infrastructure (resilience.py): a destroyed site gets a suggested stand-in,
placed close by, in cover and safely, that measurably helps. Deploying it takes a set-up time, brings
supplies in by truck, rail and drone, and only then changes the supply chain and evacuations."""
from fastapi.testclient import TestClient

from backend.app.models import Facility, SupplyLink
from backend.app.resilience import MAX_FROM_LOST_M, STRIKE_STANDOFF_M, _clear_of, temp_id
from backend.app.routing import haversine_m
from backend.app.supply_chain import chain_status


def _suggestion(repo, site_id):
    repo.set_facility_status(site_id, "DESTROYED")
    return next(s for s in chain_status(repo)["suggestions"] if s["replaces"] == site_id)


def test_nothing_destroyed_nothing_suggested(repo):
    assert chain_status(repo)["suggestions"] == []


def test_hospital_strike_suggests_a_safe_forward_surgical_team(repo):
    s = _suggestion(repo, "hos-01")
    assert s["id"] == temp_id("hos-01") and s["role"] == "ROLE_2"
    hit = next(f for f in repo.list_facilities() if f.id == "hos-01")
    assert STRIKE_STANDOFF_M <= haversine_m((s["lat"], s["lon"]), (hit.lat, hit.lon)) <= MAX_FROM_LOST_M
    assert _clear_of([z.polygon for z in repo.list_no_fly_zones()], (s["lat"], s["lon"]))
    ev = s["evac_min"]
    assert ev["with"] < ev["now"] / 2  # far better than driving to the Role 3 hours away
    assert s["why"] and s["where"]


def test_stand_in_sets_up_under_cover_close_to_the_lost_site(repo):
    s = _suggestion(repo, "hos-01")
    assert s["cover"] and s["cover"]["kind"] in ("WOODLAND", "STRUCTURES")
    assert haversine_m((s["lat"], s["lon"]), (s["cover"]["lat"], s["cover"]["lon"])) <= s["cover"]["radius_m"]
    assert s["km_from_lost"] <= 8  # a few km away, not across the sector
    setup = s["setup"]
    assert setup["team_from"] == "hos-03" and setup["team_min"] > 0  # the surgical team comes from the Role 3
    assert setup["ready_min"] == setup["team_min"] + setup["setup_min"] >= 90  # hours, not instant
    assert s["convoy"]["path"][-1] == s["id"] and s["drone"]["items"]


def test_bulk_resupply_comes_from_a_hub_and_the_hub_refills_by_rail(repo):
    repo.set_facility_status("dc-02", "DESTROYED")  # the demo order: the forward hub goes first
    s = _suggestion(repo, "hos-01")
    hubs = {f.id for f in repo.list_facilities() if f.kind == "DISTRIBUTION_CENTRE"}
    assert s["convoy"]["source_id"] in hubs  # hospitals keep their own stock for their patients
    assert s["backfill"]["path"][-1] == s["convoy"]["source_id"]
    assert {l["mode"] for l in s["backfill"]["legs"]} == {"RAIL"}


def test_hub_strike_suggests_a_distribution_point_that_beats_the_replanned_chain(repo):
    s = _suggestion(repo, "dc-02")
    assert s["kind"] == "DISTRIBUTION_CENTRE"
    better = [d for d, m in s["supply_min"].items() if m["now"] is not None and m["with"] < m["now"]]
    assert better


def test_deploy_writes_the_site_setting_up(repo):
    s = _suggestion(repo, "hos-01")
    from backend.app.resilience import deploy
    deploy(repo, s)
    f = next(f for f in repo.list_facilities() if f.id == s["id"])
    assert f.status == "SETTING_UP" and f.beds == s["beds"] and not f.stock  # its kit is still on the road
    links = {(l.src_id, l.dst_id) for l in repo.list_supply_links()}
    assert {(l["src_id"], l["dst_id"]) for l in s["links"]} <= links
    assert not [x for x in chain_status(repo)["suggestions"] if x["replaces"] == "hos-01"]  # replaced: no more suggestion
    routes = chain_status(repo)["routes"]
    assert not any(s["id"] in (r["path"] or []) for r in routes)  # nothing routes through it until it is ready


def test_deploy_endpoint_sets_up_then_diverts_casualties(monkeypatch):
    monkeypatch.setenv("EDTH_REPO", "memory")
    from backend.app import dev_server
    dev_server.world = dev_server.World()
    with TestClient(dev_server.app) as client:
        client.post("/events", json={"type": "CASUALTY", "subject_id": "sol-10", "severity": "CRITICAL"})
        r = client.post("/sites", json={"facility_id": "hos-01", "status": "DESTROYED"})
        assert any(s["replaces"] == "hos-01" and s["id"] for s in r.json()["suggestions"])
        trip = next(iter(dev_server.world.evac.trips.values()))
        assert trip.evac.facility_id == "hos-03"  # diverted to the regional Role 3, well over an hour away
        r = client.post("/sites/deploy", json={"replaces": "hos-01"})
        assert r.status_code == 200 and r.json()["ready_in_s"] > 60
        trip = next(iter(dev_server.world.evac.trips.values()))
        assert trip.evac.facility_id == "hos-03"  # not yet: the team is still setting up
        kinds = {o.kind for o in dev_server.world.stock.orders.values() if o.depot_id == "tmp-hos-01"}
        assert kinds == {"TEAM", "CONVOY"}  # the team with its kit, and the bulk resupply, are on the road
        assert client.post("/sites/deploy", json={"replaces": "hos-01"}).status_code == 404  # only once
        r = client.post("/sites/ready", json={"facility_id": "tmp-hos-01"})  # skip the wait
        assert r.json()["diverted"] == ["sol-10"]
        trip = next(t for t in dev_server.world.evac.trips.values() if t.evac.person_id == "sol-10")
        assert trip.evac.facility_id == "tmp-hos-01"
        f = next(f for f in dev_server.world.repo.list_facilities() if f.id == "tmp-hos-01")
        assert f.status == "OPERATIONAL"
        reqs = [e.event_id for e in dev_server.world.engine.pending()] + \
            [f.request_id for f in dev_server.world.tracker.flights.values()]
        assert any(str(q).startswith("tmp-hos-01-open") for q in reqs)  # a drone with blood is on its way (or queued)
        assert client.post("/sites/ready", json={"facility_id": "tmp-hos-01"}).status_code == 404


def test_team_kit_lands_with_the_team(repo):
    from backend.app.resilience import deploy, start_supplies
    from backend.app.stock import StockKeeper
    t = [1000.0]
    keeper = StockKeeper(repo, clock=lambda: t[0])
    s = _suggestion(repo, "hos-01")
    deploy(repo, s)
    start_supplies(keeper, s)
    t[0] += s["setup"]["team_min"] * 60 / keeper.speed + 0.1
    keeper.step()
    f = next(f for f in repo.list_facilities() if f.id == s["id"])
    assert f.stock.get("blood_oneg", 0) >= s["setup"]["team_kit"]["blood_oneg"]
