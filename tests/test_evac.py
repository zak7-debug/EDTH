"""Casualty evacuation (evac.py): fastest suitable facility with a free bed, kit flown ahead,
admission uses the kit, destroyed destination diverts."""
from backend.app.dispatch import DispatchEngine
from backend.app.evac import EvacTracker, TREATMENT_KIT
from backend.app.flights import FlightTracker
from backend.app.stock import StockKeeper


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def world(repo):
    clock = Clock()
    engine = DispatchEngine(repo, clock=clock)
    stock = StockKeeper(repo, clock=clock, speed=60)
    flights = FlightTracker(engine, sim_speed=1.0, clock=clock, stock=stock)
    return clock, engine, flights, EvacTracker(engine, flights, stock, sim_speed=1.0, clock=clock)


def facility(repo, fid):
    return next(f for f in repo.list_facilities() if f.id == fid)


def test_critical_goes_to_surgery_wounded_to_aid_station(repo):
    _, _, _, evac = world(repo)
    crit = evac.start(repo.get_person("sol-03"), "CRITICAL")
    assert crit[0]["type"] == "evacuation" and crit[0]["data"]["facility_id"] == "hos-01"
    assert crit[0]["data"]["shortfall"] == {}  # the Role 2 hospital holds the kit already
    wounded = evac.start(repo.get_person("sol-10"), "WOUNDED")
    assert wounded[0]["data"]["facility_id"] == "aid-01"  # nearer, and Role 1 can take WOUNDED
    assert facility(repo, "hos-01").beds_used == 1 and facility(repo, "aid-01").beds_used == 1
    assert {e.person_id for e in repo.list_evacuations()} == {"sol-03", "sol-10"}
    assert evac.start(repo.get_person("sol-03"), "CRITICAL") == []  # already on the way


def test_missing_kit_is_flown_ahead(repo, monkeypatch):
    monkeypatch.setattr("backend.app.evac.KIT_AHEAD_BY_DRONE", True)  # off in the demo: drones go to medics only
    _, engine, flights, evac = world(repo)
    msgs = evac.start(repo.get_person("sol-10"), "WOUNDED")
    ev = msgs[0]["data"]
    assert ev["shortfall"] == {"chest_seal": 1}  # the aid station has no chest seals
    assert msgs[1]["type"] == "dispatch" and msgs[1]["data"]["recipient_id"] == "aid-01"
    assert msgs[1]["data"]["eta_s"] < ev["eta_s"]  # lands before the casualty does
    assert ev["kit_eta_s"] == msgs[1]["data"]["eta_s"]
    assert msgs[1]["data"]["drone_id"] in flights.flights


def test_full_trip_admits_and_uses_kit(repo, monkeypatch):
    monkeypatch.setattr("backend.app.evac.KIT_AHEAD_BY_DRONE", True)
    clock, _, _, evac = world(repo)
    evac.start(repo.get_person("sol-10"), "WOUNDED")
    kinds = []
    for _ in range(600):  # 10 s steps; drone and casualty both arrive inside an hour
        clock.t += 10
        out = evac.flights.step(10.0) + evac.step(10.0)
        kinds += [m["type"] for m in out]
        if "admitted" in kinds:
            break
    assert kinds.index("delivered") < kinds.index("admitted")  # kit got there first
    admitted = next(m for m in out if m["type"] == "admitted")["data"]
    assert admitted["kit_used"] == TREATMENT_KIT["WOUNDED"] and admitted["kit_short"] == {}
    aid = facility(repo, "aid-01")
    assert aid.stock["chest_seal"] == 0 and aid.stock["hemostatic_gauze"] == 1  # 1 flown in and used; 2 - 1
    assert repo.get_person("sol-10").status == "ADMITTED"
    assert repo.list_evacuations()[0].status == "ADMITTED"


def test_full_beds_and_promised_kit(repo):
    _, _, _, evac = world(repo)
    repo.adjust_stock("aid-01", {"chest_seal": 1})  # one casualty's worth
    first = evac.start(repo.get_person("sol-10"), "WOUNDED")
    assert first[0]["data"]["shortfall"] == {}
    second = evac.start(repo.get_person("sol-11"), "WOUNDED")
    assert second[0]["data"]["facility_id"] == "aid-01"
    assert second[0]["data"]["shortfall"] == {"chest_seal": 1}  # the first one's seal is promised
    for pid in ("sol-12", "sol-13"):
        evac.start(repo.get_person(pid), "WOUNDED")
    fifth = evac.start(repo.get_person("sol-14"), "WOUNDED")
    assert fifth[0]["data"]["facility_id"] != "aid-01"  # its 4 beds are taken


def test_missing_kit_is_not_flown_by_default(repo):
    _, _, flights, evac = world(repo)
    msgs = evac.start(repo.get_person("sol-10"), "WOUNDED")
    assert msgs[0]["data"]["shortfall"] == {"chest_seal": 1} and len(msgs) == 1 and not flights.flights


def test_destroyed_destination_diverts(repo):
    _, _, _, evac = world(repo)
    evac.start(repo.get_person("sol-03"), "CRITICAL")
    repo.set_facility_status("hos-01", "DESTROYED")
    out = evac.site_changed("hos-01", "DESTROYED")
    assert out[0]["type"] == "evacuation" and out[0]["data"]["diverted_from"] == "hos-01"
    assert out[0]["data"]["facility_id"] == "hos-03"  # the nearest other surgical facility (regional Role 3)
    assert facility(repo, "hos-01").beds_used == 0
    assert {e.status for e in repo.list_evacuations()} == {"DIVERTED", "EN_ROUTE"}
