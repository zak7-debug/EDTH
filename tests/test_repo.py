"""Contract tests every GraphRepo implementation must pass (the 14:00 TuringDB go/no-go)."""
import threading

from backend.app.models import Dispatch


def test_seed_counts(repo):
    people = repo.list_personnel()
    assert len(people) == 20
    assert sum(p.kind == "MEDIC" for p in people) == 3
    assert len(repo.list_units()) == 3
    assert len(repo.list_drones()) == 8
    assert len(repo.list_depots()) == 3
    assert len(repo.list_no_fly_zones()) == 2


def test_get_and_update_person(repo):
    p = repo.get_person("sol-01")
    assert p.kind == "SOLDIER" and p.status == "OK"
    repo.update_person("sol-01", status="CRITICAL", lat=49.7)
    p = repo.get_person("sol-01")
    assert p.status == "CRITICAL" and p.lat == 49.7 and p.last_update > 0

    med = repo.get_person("med-2")
    assert med.low_items() == {"blood_oneg": 1}
    repo.update_person("med-2", stock={"blood_oneg": 5})
    assert repo.get_person("med-2").stock["blood_oneg"] == 5
    assert repo.get_person("nobody") is None


def test_find_candidate_drones(repo):
    need = {"tourniquet": 1, "blood_oneg": 2, "hemostatic_gauze": 1}
    ids = {d.id for d in repo.find_candidate_drones(need)}
    # drn-03/06 lack blood, drn-08 is CHARGING
    assert ids == {"drn-01", "drn-02", "drn-04", "drn-05", "drn-07"}
    assert {d.id for d in repo.find_candidate_drones({"blood_oneg": 3})} == {"drn-02", "drn-07"}
    assert repo.find_candidate_drones({"blood_oneg": 99}) == []
    d = next(d for d in repo.find_candidate_drones(need) if d.id == "drn-01")
    assert d.payload["chest_seal"] == 2 and d.speed_mps == 25.0


def test_claim_is_exclusive(repo):
    assert repo.claim_drone("drn-01", "evt-1") is True
    assert repo.claim_drone("drn-01", "evt-2") is False
    d = repo.get_drone("drn-01")
    assert d.status == "EN_ROUTE" and d.claimed_by == "evt-1"
    assert "drn-01" not in {d.id for d in repo.find_candidate_drones({})}
    repo.release_drone("drn-01")
    d = repo.get_drone("drn-01")
    assert d.status == "IDLE" and d.claimed_by is None
    assert repo.claim_drone("drn-08", "evt-3") is False  # charging


def test_claim_race(repo):
    wins = []
    def grab(i):
        if repo.claim_drone("drn-02", f"evt-{i}"):
            wins.append(i)
    threads = [threading.Thread(target=grab, args=(i,)) for i in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(wins) == 1


def test_dispatch_lifecycle(repo):
    assert repo.claim_drone("drn-02", "evt-9")
    disp = Dispatch(request_id="evt-9", drone_id="drn-02", recipient_id="med-2",
                    items={"blood_oneg": 2}, eta_s=321.5, distance_m=8037.5,
                    route=[(49.72, 11.94), (49.69, 11.86)], latency_ms=12.3, ts=1000.0)
    repo.create_dispatch(disp)
    [stored] = repo.list_dispatches()
    assert stored.drone_id == "drn-02" and stored.eta_s == 321.5 and stored.status == "EN_ROUTE"
    assert stored.route[1] == (49.69, 11.86)

    done = repo.complete_dispatch("evt-9", ts=1300.0)
    assert done.status == "DELIVERED"
    assert repo.get_drone("drn-02").payload["blood_oneg"] == 2  # 4 - 2
    assert repo.get_person("med-2").stock["blood_oneg"] == 3  # 1 + 2
    assert repo.list_dispatches()[0].status == "DELIVERED"
    repo.complete_dispatch("evt-9")  # idempotent
    assert repo.get_person("med-2").stock["blood_oneg"] == 3


def test_update_drone(repo):
    repo.update_drone("drn-03", lat=49.70, lon=11.90, range_m=1234.0, payload={"blood_oneg": 1, "tourniquet": 0})
    d = repo.get_drone("drn-03")
    assert (d.lat, d.lon, d.range_m) == (49.70, 11.90, 1234.0)
    assert d.payload["blood_oneg"] == 1 and d.payload["tourniquet"] == 0


def test_supply_chain(repo):
    facilities = {f.id: f for f in repo.list_facilities()}
    assert {f.kind for f in facilities.values()} == {"SUPPLIER", "DISTRIBUTION_CENTRE", "HOSPITAL"}
    assert len(facilities) == 7
    assert facilities["hos-02"].role == "ROLE_3" and facilities["hos-02"].beds == 150
    assert facilities["sup-02"].stock == {"blood_oneg": 400}
    assert len(repo.list_supply_links()) == 14
    depots = {d.id: d for d in repo.list_depots()}
    assert depots["dep-02"].stock["blood_oneg"] == 1

    # Launch Site West's only upstream facility is the Zaporizhzhia forward point (dep-03 relays by drone).
    sources = repo.find_resupply_sources("dep-02", {"blood_oneg": 4})
    assert [(f.id, link.lead_time_min) for f, link in sources] == [("dc-02", 50.0)]
    # Launch Site Rear: the Role 2 hospital (15 min) has blood but no chest seals, so only dc-02 qualifies.
    assert [f.id for f, _ in repo.find_resupply_sources("dep-03", {"blood_oneg": 2})] == ["hos-01", "dc-02"]
    assert [f.id for f, _ in repo.find_resupply_sources("dep-03", {"blood_oneg": 2, "chest_seal": 2})] == ["dc-02"]
    assert repo.find_resupply_sources("dep-03", {"blood_oneg": 99}) == []
