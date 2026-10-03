# Ollie (B): TuringDB and routing

Ollie makes TuringDB the real store behind `GraphRepo` by 14:00 and builds the no-fly routing in the evening; nobody else waits on either, because the in-memory repo covers until then.

### Arrival to +1 hour: Get TuringDB solid

- [ ] Read Zak's smoke-test numbers, then reproduce: `pip install turingdb`, `turingdb -demon`, connect with `TuringDB(host="http://localhost:6666")`
- [ ] Confirm the write cycle: `new_change()`, `checkout(change=...)`, `CREATE` or `SET`, `COMMIT`, `CHANGE SUBMIT`, `checkout()` back to main
- [ ] Measure: one `MATCH` read, one single-property `SET` commit; post both in ms to the team
- [ ] Check the embedded mode `TuringDB(type="embedded", data_dir=...)` in case it avoids a network hop
- [ ] Done when: a written-down list of what works, what doesn't, and the timings

### Until 12:30: Load both graphs

- [ ] `scripts/load_turingdb.py` creates graphs `personnel` and `logistics` and loads `seed.py`
- [ ] Personnel: `Soldier`, `Medic`, `Unit` nodes; `MEMBER_OF`, `ATTACHED_TO` edges; medic stock as properties such as `stock_blood_oneg`
- [ ] Logistics: `Drone`, `Depot`, `SupplyItem` nodes; `BASED_AT` and `CARRIES {qty}` edges
- [ ] Done when: the TuringDB UI (`turingdb -ui`, port 8080) shows both graphs

### Until 14:00: `TuringRepo`

- [ ] `find_candidate_drones` as one `MATCH (d:Drone)-[c:CARRIES]->(s:SupplyItem) WHERE d.status = 'IDLE' RETURN ...`, filtered for quantities in Python
- [ ] Run Zak's repo tests against `TuringRepo` as well as `InMemoryRepo`
- [ ] Done when: tests pass, for the 14:00 go or no-go

### 14:00 to 18:00: Write path

- [ ] `update_drone`, `update_person`, `create_dispatch` (a `DISPATCHED_TO` edge with eta and timestamp), `complete_dispatch`
- [ ] If a commit takes over about 100 ms, move writes to a background writer task so they never sit on the dispatch path
- [ ] Drone claiming stays in Zak's in-process lock; TuringDB records the result
- [ ] Done when: after a demo run, the graph shows the dispatches and updated stock

### 18:00 to 20:00: No-fly routing (`routing.py`)

- [ ] Waypoint grid at about 500 m spacing over the area, dropping points inside no-fly polygons
- [ ] Store waypoints and `ROUTE_SEGMENT` edges in the logistics graph; load once at startup
- [ ] A* in Python: `route(a, b)` returns the list of [lat, lon] points and the length in metres
- [ ] Done when: a route between two points on either side of a no-fly zone bends around it
- [ ] 20:00: TuringDB final call with Zak

### 20:00 to 22:00: Query log

- [ ] Record each Cypher query with its duration, and send it over the WebSocket as a new `query_log` message (agree the shape with Sasank and Arnav)

### Sunday

- [ ] "What the graph query did" panel content and the TuringDB slide
