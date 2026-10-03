"""Safeguarding medical infrastructure (resilience.py): a destroyed site gets a suggested stand-in,
placed safely, that measurably helps; deploying it changes the supply chain and evacuations."""
from fastapi.testclient import TestClient

from backend.app.models import Facility, SupplyLink
from backend.app.resilience import STRIKE_STANDOFF_M, ZONE_BUFFER_M, _clear_of, temp_id
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
    assert haversine_m((s["lat"], s["lon"]), (hit.lat, hit.lon)) >= STRIKE_STANDOFF_M
    assert _clear_of([z.polygon for z in repo.list_no_fly_zones()], (s["lat"], s["lon"]))
    ev = s["evac_min"]
    assert ev["with"] < ev["now"] / 2  # far better than driving to the Role 3 hours away
    assert s["why"] and s["where"]


def test_hub_strike_suggests_a_distribution_point_that_beats_the_replanned_chain(repo):
    s = _suggestion(repo, "dc-02")
    assert s["kind"] == "DISTRIBUTION_CENTRE"
    better = [d for d, m in s["supply_min"].items() if m["now"] is not None and m["with"] < m["now"]]
    assert better


def test_deploy_writes_the_site_and_links(repo):
    s = _suggestion(repo, "hos-01")
    from backend.app.resilience import deploy
    deploy(repo, s)
    f = next(f for f in repo.list_facilities() if f.id == s["id"])
    assert f.status == "OPERATIONAL" and f.beds == s["beds"] and f.stock.get("blood_oneg") == s["stock"]["blood_oneg"]
    links = {(l.src_id, l.dst_id) for l in repo.list_supply_links()}
    assert {(l["src_id"], l["dst_id"]) for l in s["links"]} <= links
    assert not [x for x in chain_status(repo)["suggestions"] if x["replaces"] == "hos-01"]  # replaced: no more suggestion


def test_deploy_endpoint_diverts_casualties(monkeypatch):
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
        assert r.status_code == 200 and r.json()["diverted"] == ["sol-10"]
        trip = next(iter(dev_server.world.evac.trips.values()))
        assert trip.evac.facility_id == "tmp-hos-01"
        assert client.post("/sites/deploy", json={"replaces": "hos-01"}).status_code == 404  # only once
