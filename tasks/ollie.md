# Ollie (B): TuringDB and routing

> **Status 2026-10-03 11:30 (covered while Ollie is out until 16:00):** everything below is done and merged or in PR #6. TuringDB is the real store (`turing_repo.py`, 62 tests on both repos), the go/no-go call is **go** (see the 20:00 line), and the slide content is drafted in `docs/turingdb-slide.md`. When you arrive: read `docs/turingdb-notes.md` (new soak-test section), run `./start.sh`, play the demo, then own the TuringDB slide and the questions on it.

Ollie makes TuringDB the real store behind `GraphRepo` by 14:00 and builds the no-fly routing in the evening; nobody else waits on either, because the in-memory repo covers until then.

### Arrival to +1 hour: Get TuringDB solid

- [x] Read Zak's smoke-test numbers, then reproduce: `pip install turingdb`, `turingdb -demon`, connect with `TuringDB(host="http://localhost:6666")`
- [x] Confirm the write cycle: `new_change()`, `checkout(change=...)`, `CREATE` or `SET`, `COMMIT`, `CHANGE SUBMIT`, `checkout()` back to main
- [x] Measure: one `MATCH` read, one single-property `SET` commit; post both in ms to the team
- [x] Check the embedded mode `TuringDB(type="embedded", data_dir=...)` in case it avoids a network hop
- [x] Done when: a written-down list of what works, what doesn't, and the timings

### Until 12:30: Load both graphs

- [x] `scripts/load_turingdb.py` creates graphs `personnel` and `logistics` and loads `seed.py`
- [x] Personnel: `Soldier`, `Medic`, `Unit` nodes; `MEMBER_OF`, `ATTACHED_TO` edges; medic stock as properties such as `stock_blood_oneg`
- [x] Logistics: `Drone`, `Depot`, `SupplyItem` nodes; `BASED_AT` and `CARRIES {qty}` edges
- [x] Done when: the TuringDB UI (`turingdb -ui`, port 8080) shows both graphs

### Until 14:00: `TuringRepo`

- [x] `find_candidate_drones` as one `MATCH (d:Drone)-[c:CARRIES]->(s:SupplyItem) WHERE d.status = 'IDLE' RETURN ...`, filtered for quantities in Python
- [x] Run Zak's repo tests against `TuringRepo` as well as `InMemoryRepo`
- [x] Done when: tests pass, for the 14:00 go or no-go

### 14:00 to 18:00: Write path

- [x] `update_drone`, `update_person`, `create_dispatch` (a `DISPATCHED_TO` edge with eta and timestamp), `complete_dispatch`
- [x] If a commit takes over about 100 ms, move writes to a background writer task so they never sit on the dispatch path (not needed: writes run after the dispatch is broadcast, and a write change takes 6-13 ms on the in-memory server)
- [x] Drone claiming stays in Zak's in-process lock; TuringDB records the result
- [x] Done when: after a demo run, the graph shows the dispatches and updated stock

### 18:00 to 20:00: No-fly routing (`routing.py`)

> Done 2026-10-03 as a visibility graph + A* instead of a grid (exact shortest path, nothing to store in the graph). See docs/foundations-guide.md section 9.

- [x] ~~Waypoint grid at about 500 m spacing~~ replaced by a visibility graph (see note above)
- [x] ~~Store waypoints in the graph~~ not needed: the router is built from the `NoFlyZone` nodes and rebuilt when a threat is added
- [x] A* in Python: `route(a, b)` returns the list of [lat, lon] points and the length in metres
- [x] Done when: a route between two points on either side of a no-fly zone bends around it
- [x] 20:00: TuringDB final call with Zak. **Go**, decided early: decision 14-18 ms, supply-chain re-plan about 30 ms, all tests green on both repos. One operating rule from the soak test: restart TuringDB before every rehearsal and the demo.

### 20:00 to 22:00: Query log

> Done 2026-10-03: `query_log` message and the dashboard's Graph queries panel (guide section 12).

- [x] Record each Cypher query with its duration, and send it over the WebSocket as a new `query_log` message (agree the shape with Sasank and Arnav)

### Sunday

- [ ] "What the graph query did" panel content and the TuringDB slide: **drafted** in `docs/turingdb-slide.md`; Ollie reviews and presents
