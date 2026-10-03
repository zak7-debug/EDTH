"""Drone shot down: written off in the graph, its delivery retried at the front of the queue."""
import time

from backend.app.dispatch import DispatchEngine
from backend.app.flights import FlightTracker
from backend.app.models import Event


def casualty(repo, pid, eid, sev="CRITICAL", ts=None):
    p = repo.get_person(pid)
    return Event(eid, "CASUALTY", pid, p.lat, p.lon, ts or time.time(), sev)


def test_lost_drone_is_written_off_and_request_retried(repo):
    engine = DispatchEngine(repo)
    tracker = FlightTracker(engine, sim_speed=1.0)
    first = engine.handle(casualty(repo, "sol-10", "e1"))
    engine.record(first)
    tracker.start(first)
    tracker.step(30.0)

    where = tracker.lose(first.drone_id)
    assert where["phase"] == "EN_ROUTE" and where["request_id"] == "e1"
    retry = engine.drone_lost(first.drone_id, "e1")

    lost = repo.get_drone(first.drone_id)
    assert lost.status == "LOST" and not any(lost.payload.values())
    assert [d.status for d in repo.list_dispatches() if d.request_id == "e1"] == ["LOST"]
    assert retry.request_id == "e1-r1" and retry.recipient_id == "sol-10"
    if hasattr(retry, "drone_id"):
        assert retry.drone_id != first.drone_id


def test_retry_jumps_the_queue_when_no_drone_is_free(repo):
    engine = DispatchEngine(repo)
    for d in repo.list_drones():  # only HAWK 1 in service
        if d.id != "drn-01":
            repo.update_drone(d.id, status="CHARGING")
    t0 = time.time()
    first = engine.handle(casualty(repo, "sol-10", "e1", ts=t0))
    assert first.drone_id == "drn-01"
    later = engine.handle(casualty(repo, "sol-05", "e2", ts=t0 + 5))
    assert later.reason_code == "ALL_BUSY"

    retry = engine.drone_lost("drn-01", "e1")
    assert retry.reason_code == "ALL_BUSY"  # nothing free, but a carrier exists (charging)
    assert [e.event_id for e in engine.pending()] == ["e1-r1", "e2"]  # older casualty first


def test_lost_drone_never_counts_as_busy(repo):
    engine = DispatchEngine(repo)
    for d in repo.list_drones():
        repo.lose_drone(d.id)
    res = engine.handle(casualty(repo, "sol-10", "e1"))
    assert res.reason_code == "NO_STOCK" and not engine.pending()
