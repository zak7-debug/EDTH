# TuringDB notes (smoke-tested 2026-10-03, `pip install turingdb` 3.0)

Reproduce with `python scripts/turingdb_smoke.py embedded` or, with a server running, `python scripts/turingdb_smoke.py json`.

## Timings

| Step | Embedded engine | Server, disk-backed | Server, `-in-memory` |
| --- | --- | --- | --- |
| 2-hop `MATCH` (drone, CARRIES, item) | 2 ms | 2.5-5 ms | 2.5-4 ms |
| `CREATE` / `SET` query inside a change | <1 ms | 1.5 ms | 1.5 ms |
| `COMMIT` | 1 ms | 2 ms | 2 ms |
| `CHANGE SUBMIT` | 85-125 ms | 70-150 ms | 2 ms |
| Full single-property write (new change, SET, submit) | 85 ms | 70 ms | **6 ms** |
| Seed both graphs | | | ~100 ms |
| Candidate-drone query through `TuringRepo` | | | ~5 ms |
| Full dispatch decision (query, ETA maths, atomic claim) | ~100 ms | | **14-18 ms** |

**Decision:** run the server with `turingdb start -demon -in-memory` (what `start.sh` does). Disk-backed submits rewrite the graph file every time; we reseed on start anyway, so persistence buys nothing.

## Soak test (300 dispatch cycles, in-memory server, 2026-10-03)

Each cycle: decide, record, deliver, free the drone, reload it (about 8 write changes).

| After | Server memory | Decision median / p95 | Writes per cycle (median) |
| --- | --- | --- | --- |
| start | 74 MB | | |
| 100 requests | 385 MB | 14 / 20 ms | 66 ms |
| 200 requests | 721 MB | 23 / 28 ms | 98 ms |
| 300 requests | 1,101 MB | 30 / 40 ms | 115 ms |
| reseed (`POST /reset`) | 1,169 MB | 20 ms | |

Every submitted change keeps its history, so memory grows about 3.5 MB per request and reads slow down as it grows. Reseeding helps speed a little but frees no memory. A demo run is about 15 requests, so this doesn't matter on stage. **Restart TuringDB (rerun `./start.sh`) before every rehearsal and before the demo.** Don't leave a simulator firing events into it for hours: at one event every 5 s it would reach about 2.5 GB within the hour. Reproduce with the soak script described in docs/turingdb-slide.md.

## What works

- Server: `turingdb start -demon [-in-memory] [-ui]`, `turingdb stop`. Port 6666, UI on 8080 with `-ui`.
- Python client `TuringDB(host="http://localhost:6666")` (JSON over HTTP), or `TuringDB(type="embedded", data_dir=...)` running the engine in-process (used by the tests).
- Several graphs per server: `create_graph`, `set_graph`. Switching graph is a client-side setting, free.
- Writes: `new_change()` (also checks the client out onto it), queries, `COMMIT`, `CHANGE SUBMIT`, `checkout()` back to main. `CHANGE SUBMIT` without a prior `COMMIT` also works.
- Reads on main don't see a change's writes until it is submitted.
- Cypher tested OK: multi-pattern `CREATE a, b, (a)-[:R {..}]->(b)`, `MATCH ... WHERE ... AND/OR/IN/>=`, `SET` several properties (also on edges, also new property names), `MERGE`, `UNWIND`, `DELETE`/`DETACH DELETE`, `count()`, `ORDER BY`, `LIMIT`, `labels()`, `type()`, multi-label nodes, list and bool properties.

## Limitations and workarounds

| Limitation | Workaround |
| --- | --- |
| No conflict detection: two changes editing the same node both submit, last writer wins | `claim_drone` checks and sets under a Python lock. One backend process owns all writes. |
| A node created in a change can't be `MATCH`ed in the same change until `COMMIT` | `TuringRepo._write` commits after every query. |
| Each property name has one fixed type (Int64 vs Double) | `turing_repo.typed()` always writes the same Python type per field. |
| Edges can't cross graphs | `DISPATCHED_TO` targets a `Recipient {id}` stand-in in `logistics`. |
| No null writes | `claimed_by = ''` means unclaimed. |
| Client keeps graph/change as state, not thread-safe | `TuringRepo` serialises calls through one lock. |
| Native (binary) client fails: "Proto header dataLen does not match chunk payload size" | Use the default JSON client. |
| Every submit keeps the full history: the embedded test engine used ~2.4 GB of disk per test run | Tests delete their data directory on exit (`tests/conftest.py`). Watch the in-memory server's RAM during a long demo run; restart it between rehearsals. |
| Disk-backed submit is 70-150 ms | `-in-memory` server (6 ms). If a write path still hurts latency, do graph writes after the dispatch message is sent. |
