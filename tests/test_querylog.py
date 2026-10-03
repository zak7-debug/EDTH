"""Query log: TuringRepo reports its Cypher only inside capture()."""
import time

from backend.app import querylog
from backend.app.dispatch import DispatchEngine
from backend.app.models import Event


def test_capture_collects_decision_queries(repo):
    p = repo.get_person("sol-10")
    ev = Event("q1", "CASUALTY", p.id, p.lat, p.lon, time.time(), "CRITICAL")
    engine = DispatchEngine(repo)
    with querylog.capture() as qs:
        res = engine.handle(ev)
    assert res.drone_id
    if type(repo).__name__ == "TuringRepo":
        assert any("MATCH" in q["cypher"] and q["kind"] == "read" for q in qs)
        assert all(q["ms"] >= 0 for q in qs)
    else:
        assert qs == []


def test_record_is_free_outside_capture():
    querylog.record("g", "MATCH (n) RETURN n", 1.0, 1)  # no capture open: dropped, no error
    with querylog.capture() as qs:
        querylog.record("g", "X" * 2000, 1.0)
    assert len(qs) == 1 and len(qs[0]["cypher"]) <= querylog.MAX_CYPHER_CHARS + 2
