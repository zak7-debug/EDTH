"""Flight lifecycle (flights.py) and the dev server's HTTP + WebSocket contract."""
import time

from fastapi.testclient import TestClient

from backend.app.dispatch import DispatchEngine
from backend.app.flights import FlightTracker
from backend.app.models import Event


def casualty(repo, pid, sev="CRITICAL", eid="e1"):
    p = repo.get_person(pid)
    return Event(eid, "CASUALTY", pid, p.lat, p.lon, time.time(), sev)


def test_full_lifecycle_drains_queue(repo):
    """Fly out, deliver, fly home, reload; a queued request is then served by the freed drone."""
    engine = DispatchEngine(repo)
    tracker = FlightTracker(engine, sim_speed=1.0)
    for d in repo.list_drones():  # leave a single drone in service
        if d.id != "drn-04":
            repo.update_drone(d.id, status="CHARGING")
    repo.adjust_stock("dep-02", {"blood_oneg": 10})  # West starts short of blood; that case is in test_stock.py
    first = engine.handle(casualty(repo, "sol-03", eid="e1"))
    engine.record(first)
    tracker.start(first)
    queued = engine.handle(casualty(repo, "sol-07", eid="e2"))
    assert queued.reason_code == "ALL_BUSY" and engine.pending()

    types = []
    for _ in range(400):  # 10 s steps of mission time; a round trip is well under an hour
        types += [m["type"] for m in tracker.step(10.0)]
        if not engine.pending():
            break
    assert "delivered" in types and "dispatch" in types
    assert not engine.pending()
    assert repo.get_drone("drn-04").status == "EN_ROUTE"  # straight back out for the queued casualty


def test_dev_server_event_roundtrip(monkeypatch):
    monkeypatch.setenv("EDTH_REPO", "memory")
    from backend.app import dev_server
    dev_server.world = dev_server.World()
    with TestClient(dev_server.app) as client:
        assert client.get("/health").json()["ok"]
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "snapshot"
            assert ws.receive_json()["type"] == "queue"
            chain = ws.receive_json()
            assert chain["type"] == "supply_chain" and len(chain["data"]["routes"]) == 3
            stock = ws.receive_json()
            assert stock["type"] == "stock_update"
            assert [o["depot_id"] for o in stock["data"]["orders"]] == ["dep-02"]  # West reorders blood at start
            # A casualty sets the soldier's status and starts their evacuation: no drone (medics only).
            r = client.post("/events", json={"type": "CASUALTY", "subject_id": "sol-10", "severity": "CRITICAL"})
            assert r.status_code == 200 and r.json()["status"] == "CRITICAL" and "drone_id" not in r.json()
            assert ws.receive_json()["type"] == "event"
            m = ws.receive_json()
            assert m["type"] == "evacuation" and m["data"]["facility_id"] == "hos-01"  # CRITICAL -> Role 2
            assert dev_server.world.repo.get_person("sol-10").status == "CRITICAL"
            # The squad medic asks for a restock: that gets the drone.
            r = client.post("/events", json={"type": "LOW_STOCK", "subject_id": "med-2", "items": {"blood_oneg": 2},
                                             "urgency": "CRITICAL"})
            assert r.status_code == 200 and r.json()["drone_id"]
            assert ws.receive_json()["type"] == "event"
            d = ws.receive_json()
            assert d["type"] == "dispatch" and d["data"]["latency_ms"] < 50
        assert client.post("/events", json={"type": "CASUALTY", "subject_id": "nobody"}).status_code == 404
        r = client.post("/sites", json={"facility_id": "dc-02", "status": "DESTROYED"})
        assert r.status_code == 200 and r.json()["status"]["dc-02"] == "DESTROYED"
        assert client.post("/losses", json={"drone_id": "drn-08"}).json()["ok"]  # charging, empty-handed
        assert "<title>" in client.get("/").text
        assert client.get("/mock/snapshot.json").status_code == 200
