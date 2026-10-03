"""Dispatch engine tests. Every test runs on InMemoryRepo and TuringRepo (see conftest)."""
import threading
import time

from backend.app.dispatch import DispatchEngine, needed_items
from backend.app.models import Dispatch, Event, NoDispatch

_n = 0


def casualty(repo, pid, severity="CRITICAL", items=None, ts=None):
    global _n
    _n += 1
    p = repo.get_person(pid)
    return Event(f"evt-{_n}", "CASUALTY", pid, p.lat, p.lon, ts or time.time(), severity, items or {})


def low_stock(repo, pid, items=None, ts=None):
    global _n
    _n += 1
    p = repo.get_person(pid)
    return Event(f"evt-{_n}", "LOW_STOCK", pid, p.lat, p.lon, ts or time.time(), None, items or {})


def test_needs_table(repo):
    assert needed_items(casualty(repo, "sol-07")) == {"tourniquet": 1, "blood_oneg": 2, "hemostatic_gauze": 1}
    assert needed_items(casualty(repo, "sol-07", "WOUNDED")) == {"tourniquet": 1, "chest_seal": 1}
    # LOW_STOCK with no items: the medic's own shortfall (med-2 is 1 blood under threshold)
    assert needed_items(low_stock(repo, "med-2"), repo) == {"blood_oneg": 1}


def test_picks_nearest_suitable_drone(repo):
    res = DispatchEngine(repo).handle(casualty(repo, "sol-07"))
    assert isinstance(res, Dispatch)
    assert res.drone_id == "drn-04"  # Launch Site West, ~6.4 km
    assert 200 < res.eta_s < 260 and res.route[0] == (47.66, 35.54)
    assert repo.get_drone("drn-04").status == "EN_ROUTE"


def test_skips_drone_without_enough_stock(repo):
    # WOUNDED needs a chest seal: drn-04 (nearest) has none, so drn-01 goes
    res = DispatchEngine(repo).handle(casualty(repo, "sol-07", "WOUNDED"))
    assert res.drone_id == "drn-01"


def test_skips_drone_out_of_range(repo):
    # drn-05 sits at Launch Site West with the right kit but only 4 km of battery
    engine = DispatchEngine(repo)
    assert engine.handle(casualty(repo, "sol-07")).drone_id == "drn-04"
    assert engine.handle(casualty(repo, "sol-08")).drone_id in {"drn-01", "drn-02"}


def test_no_stock_gives_reason(repo):
    res = DispatchEngine(repo).handle(casualty(repo, "sol-07", items={"morphine_autoinjector": 9}))
    assert isinstance(res, NoDispatch) and res.reason_code == "NO_STOCK"
    assert "morphine_autoinjector" in res.reason


def test_two_simultaneous_emergencies_get_different_drones(repo):
    engine = DispatchEngine(repo)
    events = [casualty(repo, "sol-07"), casualty(repo, "sol-08")]
    results = [None, None]

    def go(i):
        results[i] = engine.handle(events[i])
    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert all(isinstance(r, Dispatch) for r in results)
    assert results[0].drone_id != results[1].drone_id


def test_all_busy_queues_then_serves_on_return(repo):
    engine = DispatchEngine(repo)
    # four drones can serve a CRITICAL casualty in this sector (drn-05 lacks range)
    sent = [engine.handle(casualty(repo, f"sol-0{i}")) for i in range(1, 5)]
    assert all(isinstance(r, Dispatch) for r in sent)
    fifth = engine.handle(casualty(repo, "sol-05"))
    assert isinstance(fifth, NoDispatch) and fifth.reason_code == "ALL_BUSY"
    assert "position 1" in fifth.reason
    assert fifth.nearest_alternative is not None
    assert [e.event_id for e in engine.pending()] == [fifth.request_id]

    served = engine.drone_freed("drn-04")
    assert [d.request_id for d in served] == [fifth.request_id]
    assert engine.pending() == []


def test_queue_serves_critical_first(repo):
    engine = DispatchEngine(repo)
    for i in range(1, 5):
        engine.handle(casualty(repo, f"sol-0{i}"))
    t = time.time()
    low = engine.handle(low_stock(repo, "med-2", {"blood_oneg": 2}, ts=t))       # older, lower priority
    crit = engine.handle(casualty(repo, "sol-06", ts=t + 1))
    assert [e.event_id for e in engine.pending()] == [crit.request_id, low.request_id]
    served = engine.drone_freed("drn-02")
    assert served[0].request_id == crit.request_id


def test_record_writes_dispatch_and_status(repo):
    engine = DispatchEngine(repo)
    ev = casualty(repo, "sol-07")
    res = engine.handle(ev)
    engine.record(res, ev)
    assert repo.get_person("sol-07").status == "CRITICAL"
    assert [d.request_id for d in repo.list_dispatches()] == [ev.event_id]


# Event-to-dispatch budget per repo. The TuringDB tests use the embedded engine, which saves the
# graph to disk on every write (~85 ms per claim). The in-memory server used by start.sh measured
# 14-18 ms for the same dispatch (docs/turingdb-notes.md).
LATENCY_BUDGET_MS = {"InMemoryRepo": 50, "TuringRepo": 500}


def test_latency(repo):
    engine = DispatchEngine(repo)
    t0 = time.perf_counter()
    res = engine.handle(casualty(repo, "sol-07"), received_perf=t0)
    assert res.latency_ms < LATENCY_BUDGET_MS[type(repo).__name__], res.latency_ms
