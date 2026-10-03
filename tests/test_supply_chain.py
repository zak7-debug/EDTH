"""Supply-chain routing round destroyed sites (supply_chain.py)."""
import time

from backend.app.dispatch import DispatchEngine
from backend.app.models import Event
from backend.app.supply_chain import chain_status


def routes(repo):
    return {r["depot_id"]: (r["path"], r.get("minutes")) for r in chain_status(repo)["routes"]}


def test_chain_finds_a_new_path_when_sites_are_destroyed(repo):
    assert routes(repo)["dep-01"] == (["dc-02", "dep-01"], 60.0)

    repo.set_facility_status("dc-02", "DESTROYED")  # forward distribution point hit
    assert chain_status(repo)["status"]["dc-02"] == "DESTROYED"
    # DEMO: blood now comes from the Role 3 hospital by helicopter, via the field hospital and a drone relay.
    assert routes(repo)["dep-01"] == (["hos-02", "hos-01", "dep-03", "dep-01"], 85.0)

    repo.set_facility_status("hos-01", "DESTROYED")
    r = routes(repo)
    assert r["dep-01"] == (["dc-01", "dep-01"], 150.0)
    assert r["dep-02"] == (None, None)  # cut off

    repo.set_facility_status("dc-02", "OPERATIONAL")
    assert routes(repo)["dep-02"] == (["dc-02", "dep-02"], 50.0)


def test_no_drone_suggestion_skips_destroyed_sites(repo):
    for d in repo.list_drones():
        repo.lose_drone(d.id)
    repo.set_facility_status("dc-02", "DESTROYED")
    p = repo.get_person("sol-10")
    res = DispatchEngine(repo).handle(Event("e1", "CASUALTY", p.id, p.lat, p.lon, time.time(), "CRITICAL"))
    note = res.nearest_alternative["note"]
    assert "Forward distribution point" not in note and "restocked from" in note
