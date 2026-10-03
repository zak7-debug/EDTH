"""TuringDB smoke test: create a graph, write through a change, read back, SET, time each step.

    python scripts/turingdb_smoke.py embedded      # in-process engine, no server
    python scripts/turingdb_smoke.py json          # needs: turingdb start -demon -in-memory
Results from 2026-10-03 are in docs/turingdb-notes.md.
"""
import sys
import tempfile
import time

from turingdb import TuringDB

mode = sys.argv[1] if len(sys.argv) > 1 else "embedded"
db = (TuringDB(type="embedded", data_dir=tempfile.mkdtemp()) if mode == "embedded"
      else TuringDB(type=mode, host="http://localhost:6666"))


def timed(label, fn):
    t = time.perf_counter()
    out = fn()
    print(f"{label:<28}{(time.perf_counter() - t) * 1000:8.2f} ms")
    return out


def write(*queries):
    db.new_change()
    for q in queries:
        db.query(q)
        db.query("COMMIT")
    db.query("CHANGE SUBMIT")
    db.checkout()


graph = f"smoke_{int(time.time())}"
timed("create_graph", lambda: db.create_graph(graph))
db.set_graph(graph)
timed("CREATE + submit", lambda: write(
    "CREATE (d:Drone {id: 'drn-01', status: 'IDLE', range_m: 20000.0})"
    "-[:CARRIES {qty: 3}]->(s:SupplyItem {id: 'blood_oneg'})"))
q = ("MATCH (d:Drone)-[k:CARRIES]->(s:SupplyItem) WHERE d.status = 'IDLE' AND k.qty >= 2 "
     "RETURN d.id, k.qty, s.id")
print(timed("MATCH 2-hop (cold)", lambda: db.query(q)))
for _ in range(3):
    timed("MATCH 2-hop", lambda: db.query(q))
for status in ("EN_ROUTE", "IDLE", "EN_ROUTE"):
    timed("SET 1 prop + submit", lambda: write(f"MATCH (d:Drone) WHERE d.id = 'drn-01' SET d.status = '{status}'"))
print(db.query("MATCH (d:Drone) RETURN d.id, d.status"))
