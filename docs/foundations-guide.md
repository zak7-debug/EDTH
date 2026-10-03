# Foundations guide: what each piece does and how to change it

This is the "why" behind the first-hour foundations. Every later piece (dispatch engine, API, simulator, map) is built on these files, so this guide says what each one decides, what depends on it, and where to edit when the demo needs something different.

## Finding things: search tags

Comments in the code carry three tags so you can grep for them (`grep -rn "TUNE:" backend`):

| Tag | Means | Examples |
| --- | --- | --- |
| `TUNE:` | A number or rule you may want to adjust | casualty needs table, range safety margin, triage order, medic thresholds |
| `HOOK:` | Where another part of the system plugs in | where the API calls the engine, where A* routing slots in, the snapshot the map loads |
| `DEMO:` | Data or behaviour chosen for the demo scenario | drone payloads, squad positions, threat zones, med-2's low blood |

## The big picture

```text
simulator / real telemetry
        |  JSON event (contracts/messages.md)
        v
  FastAPI  POST /events  ──>  dispatch engine  ──>  GraphRepo  ──>  TuringDB (personnel + logistics)
        |                         |                    ^
        |   WebSocket /ws  <──────┘                    └── InMemoryRepo (fallback, tests)
        v
  Leaflet map dashboard
```

The rule that keeps four people from blocking each other: **nobody talks to TuringDB directly except `turing_repo.py`**. Everyone else calls the `GraphRepo` methods, and everyone sends or receives the message shapes in `contracts/`.

## 1. TuringDB smoke test (`scripts/turingdb_smoke.py`, `docs/turingdb-notes.md`)

**What it does:** creates a graph, writes a drone and a supply item, reads them back with the same two-hop query dispatch will use, updates a property, and times every step.

**Why it matters for the final product:** latency is a headline metric. The numbers show a read costs ~3 ms and a write ~6 ms, but only if the server runs with `-in-memory`; a disk-backed server spends 70-150 ms per write saving the file. That one flag is the difference between a comfortable and a borderline "dispatch in under a second".

**What it found that shapes the design:**
- TuringDB doesn't detect two writers editing the same drone. So the "two emergencies never get the same drone" guarantee lives in Python (`claim_drone` with a lock), not in the database.
- Edges can't link the two graphs, so a dispatch points at a `Recipient` stand-in node.

**To change:** if a newer TuringDB version adds conflict detection or cross-graph edges, the workarounds are listed in the notes' last table, each with the one place in code it lives.

## 2. Data model (`backend/app/models.py`)

**What it does:** defines the Python types every layer passes around: `Person` (soldier or medic, with medic stock and low-stock thresholds), `Unit`, `Drone`, `Depot`, `NoFlyZone`, `Event` (input), `Dispatch` and `NoDispatch` (outputs). `ITEMS` is the one supply vocabulary.

**Why it matters:** these are the nouns of the demo. The map draws them, the engine reasons about them, the WebSocket ships them as JSON (`to_dict()`).

**To change:**
- New supply item: add it to `ITEMS`. TuringDB, seed and messages pick it up automatically; give drones and medics some in `seed.py`.
- New drone or person field: add it to the dataclass with a default, then add it to `_DRONE_FIELDS`/`_PERSON_FIELDS` in `turing_repo.py`. If it's a float, add its name to `FLOAT_FIELDS` there.
- Triage order: `TRIAGE_PRIORITY`.

## 3. GraphRepo interface (`backend/app/repo.py`)

**What it does:** one list of methods the rest of the app uses to read and change the world: get/update people, find candidate drones for a set of items, claim/release a drone, record and complete a dispatch. Two implementations:
- `InMemoryRepo`: Python dicts. Zero setup, used by tests, and the fallback if TuringDB misbehaves.
- `TuringRepo` (`turing_repo.py`): the real store.

`get_repo()` picks one from the `EDTH_REPO` env var (`turing` or `memory`).

**Why it matters:** this is what lets Sasank and Arnav build the API and map without waiting on the database, and what makes the TuringDB fallback a one-variable switch instead of a rewrite.

**The two methods that carry the demo:**
- `find_candidate_drones(items)` is the multi-hop graph query: every free drone and what it carries, in one round trip (~5 ms). The pitch's "why a graph database" moment.
- `claim_drone(drone_id, request_id)` is atomic: the first request wins, the second gets `False` and the engine tries the next drone. That is the "second simultaneous emergency takes a different drone" beat.

**To change:** add the method to the `GraphRepo` protocol, implement it in both classes, and add a test in `tests/test_repo.py`. The tests run against both implementations, so they keep the two honest.

## 4. TuringDB implementation (`backend/app/turing_repo.py`, `contracts/schema.md`)

**What it does:** maps the model onto two graphs.
- `personnel`: `Soldier` and `Medic` nodes linked to their `Unit` (`MEMBER_OF`, `ATTACHED_TO`). Medic stock is flat properties (`stock_blood_oneg`, `threshold_blood_oneg`) so a query can compare them directly.
- `logistics`: `Drone` nodes `BASED_AT` a `Depot` and `CARRIES {qty}` `SupplyItem`s; `NoFlyZone` nodes; `DISPATCHED_TO` edges recording each delivery with its ETA, route and latency.

Every write goes through `_write()`, which opens a change, runs the queries, commits and submits. Every call holds one lock, because the client isn't thread-safe.

**To change:** the schema is described at the top of the file and in `contracts/schema.md`; keep both in step. To inspect the live graphs, `./start.sh` also starts the TuringDB UI at http://localhost:8080.

## 5. Seed data (`backend/app/seed.py`)

**What it does:** builds the same world for both repos in a sector of south-east Ukraine, between Zaporizhzhia and Orikhiv: 3 squads (20 people, one medic each), 3 drone launch sites (depots), 8 drones and 2 threat zones (stored as no-fly zones).

**Real geography, fictional laydown.** Towns, regions and distances are real so the demo is plausible. Every base, route, unit position and facility is invented, and upstream nodes are placed only at region level. Keep it that way: a believable map of real Ukrainian medical logistics would be useful to whoever wants to target it. Positions are fixed (random jitter with a fixed seed), so every run and every test sees the same world.

**Why the numbers are what they are (all chosen for the demo):**
- An **EW jamming zone** sits between **Launch Site North** and all three squads, so drones from there must route around it. That's the reroute beat.
- Payloads are deliberately uneven: `drn-03` and `drn-06` carry no blood, `drn-05` has a nearly flat battery (only 4 km of range left), `drn-08` is charging. A CRITICAL casualty has five possible drones; once those are busy, the next one gets a clear "no drone" reason.
- **`med-2` starts with 1 unit of blood against a threshold of 2**: the demo's low-stock event is already true in the data.

**To change:** edit the tables at the top of `seed.py` (`_DRONES`, `DEPOTS`, `NO_FLY_ZONES`, `_MEDIC_STOCK`, `_SQUAD_CENTRES`). `python -m backend.app.seed` prints the counts; `pytest` checks the candidate sets, so update `test_find_candidate_drones` if you change payloads.

## 5b. The full medical supply chain

**What it does:** the logistics graph models every level, not just drones:

```text
Supplier (rear)  ──SUPPLIES──>  Distribution centre / Hospital  ──SUPPLIES──>  Depot  <──BASED_AT──  Drone  ──DISPATCHED_TO──>  Medic / Soldier
```

Each level holds stock (`STOCKS {qty}` edges to the same `SupplyItem` nodes drones `CARRIES`), and each `SUPPLIES` link has a lead time and a transport mode. Seeded: an international donor hub in the Rzeszów region (Poland) feeding a Lviv-region hub, then a Dnipro-region hub, then a forward point in the Zaporizhzhia region, then the launch sites. Alongside are a Kyiv-region blood service, a Role 3 hospital in the Dnipro region and a Role 2 field hospital at the sector rear. That makes 10 links, with lead times from 12 hours (Lviv to Dnipro by truck) down to 15 minutes.

**Why it matters for the final product:**
- **The pitch:** one graph shows the whole chain from factory to wounded soldier. A question like "which facility can restock Launch Site West with blood, and how fast?" is a single 2-hop query (`find_resupply_sources`), which is exactly what graph databases are good at.
- **The "no drone available" answer gets better:** instead of only "nothing carries blood", the engine can say "drn-07 can serve after reloading at Launch Site Rear, restocked from the Role 2 field hospital in 15 min".
- **Later:** hospitals are where casualties get evacuated to, so a CASEVAC extension has its destinations already.

The real-time dispatch path is unchanged: drones still launch from depots, and the upstream levels are read only when explaining or planning resupply. That keeps the sub-second dispatch untouched.

**To change:** the `FACILITIES` and `SUPPLY_LINKS` tables in `seed.py`. Launch Site West (`dep-02`) is deliberately short of blood, and the field hospital holds no chest seals, so resupply answers differ by item.

## 6. Event and WebSocket formats (`contracts/messages.md`, `backend/app/messages.py`)

**What it does:** fixes the JSON for everything that crosses a boundary: events into `POST /events`, and the seven message types out on `/ws` (`snapshot`, `event`, `dispatch`, `no_dispatch`, `drone_update`, `delivered`, `queue`). `messages.py` has one builder per type so the backend can't drift from the contract.

**Why it matters:** this is the seam between Sasank's backend and Arnav's map. As long as both stick to it, they can build in parallel and integrate in minutes. Swapping the simulator for real telemetry later only means producing the same event JSON.

**To change:** after the 11:00 freeze, agree it with whoever consumes the message, then update `contracts/messages.md` and the builder together.

## 7. Skeleton, start script and tests

- `start.sh`: creates the venv on first run, starts TuringDB in memory with its UI, and runs the API on port 8000. `EDTH_REPO=memory ./start.sh` runs without a database.
- `backend/app/main.py`: a stub (`/health`, `/state`). Sasank replaces it; until then `start.sh` runs `backend/app/dev_server.py` (section 10).
- `sim/simulator.py`: placeholder owned by Sasank.
- `tests/`: `pytest` runs every repo test against both repos. The TuringDB run uses the embedded engine, so no server is needed. `TURINGDB_TEST_HOST=http://localhost:6666 pytest` runs them against a live server.

## 8. Dispatch engine (`backend/app/dispatch.py`)

**What it does:** turns an event into a decision in five steps, each commented in `_try_dispatch`:
1. **Needs:** `needed_items()` maps the event to items. A CRITICAL casualty needs 1 tourniquet, 2 blood and 1 haemostatic gauze (the `NEEDS` table).
2. **Match:** one graph query, `find_candidate_drones`, returns every free drone carrying enough.
3. **Route, ETA, range:** for each candidate, in Python. ETA is distance ÷ speed. A drone only qualifies if the trip out, plus the hop to the nearest launch site, × 1.2 fits its battery.
4. **Pick:** fastest first, then `claim_drone`. If a simultaneous request already took that drone, the claim fails and it tries the next one.
5. **Explain:** if nothing can go, the result is a `NoDispatch`:
   - `ALL_BUSY`: the request is queued in triage order.
   - `OUT_OF_RANGE`: free drones have the kit but not the battery.
   - `NO_STOCK`: nothing carries the items.

   Each comes with a `nearest_alternative` such as "OWL 1 can reach you in 14 min after reloading at Launch Site Rear".

**Why it matters for the final product:** this is the "under a second" claim. It measured 14-18 ms on the TuringDB server and under 1 ms in memory, and `latency_ms` on every result is what the dashboard's latency counter shows.

**How the backend uses it:** `handle(event)`, then broadcast the result, then `record(result, event)` writes it to the graph. When a drone lands, `drone_freed(id)` releases it and returns any queued requests it can now serve.

**To change:** the `NEEDS`, `RANGE_SAFETY` and `RELOAD_S` constants at the top. Change the selection rule in the sort key in `_try_dispatch` (step 3). Routing round threat zones is on by default (section 9); `DispatchEngine(repo, route_fn=straight_line)` turns it off.

## 9. Routing round threat zones (`backend/app/routing.py`)

**What it does:** `Router(zones).route(a, b)` returns the shortest path from a to b that doesn't enter any no-fly or threat zone, as `(waypoints, metres)`.
- If the straight line is clear, it flies straight (most requests).
- Otherwise it builds a small visibility graph: start, goal, and every zone corner pushed `CLEARANCE_M` (250 m) outwards. Two points are linked if the line between them is clear. A* over those links gives the shortest safe path.
- A route costs under half a millisecond, so it adds nothing noticeable to the dispatch latency.

**Why it matters for the final product:** the seed puts the EW jamming zone between Launch Site North and every squad, so HAWK drones visibly bend round it on the map, and their ETAs and range checks use the real detour length. The event log marks those dispatches "rerouted round threat zone".

**How it is wired:** `DispatchEngine` builds a `Router` from the graph's `NoFlyZone` nodes when it starts. If you add a zone mid-demo, restart (or `POST /reset`).

**To change:** `CLEARANCE_M` for a wider berth. Zones live in `NO_FLY_ZONES` in `seed.py`. This replaces the planned 500 m waypoint grid: a visibility graph gives the exact shortest path with about a dozen nodes, so there was nothing worth storing in TuringDB.

## 10. Live API and flights (`backend/app/dev_server.py`, `backend/app/flights.py`)

**What it does:** a complete API that follows `contracts/messages.md`, written so the dashboard runs end to end before Sasank's `main.py` lands.
- `POST /events` takes an event, broadcasts it, times the decision, broadcasts `dispatch` or `no_dispatch`, then writes to the graph. Partial events are fine: the Controls tab only sends type, subject, severity and items, and the server fills id, time and position.
- `/ws` sends a snapshot and the queue on connect, then every live message.
- `POST /scenario/demo` plays `DEMO_SCRIPT`; `POST /reset` reseeds between rehearsals.
- `FlightTracker` (flights.py) flies each drone along its route every `TICK_S` (0.5 s): `drone_update` while flying, `delivered` on arrival (moves the items into a medic's stock in the graph), then back to its home launch site, battery swap and reload, then `drone_freed()` serves the queue.

**Why it matters for the final product:** this is what makes the map move. `EDTH_SIM_SPEED` (default 10) plays flights ten times faster so a delivery takes about a minute on stage; ETAs on the wire stay in real mission seconds.

**To change:** the demo story is `DEMO_SCRIPT` at the top of dev_server.py (seconds after start, event). Flight speed-up is `SIM_SPEED` in flights.py or the `EDTH_SIM_SPEED` env var. Sasank can lift any of it into `main.py`, then run `EDTH_APP=backend.app.main:app ./start.sh`.

**The demo scenario, as it plays (about 90 s at 10x):**
1. CRITICAL casualty in BADGER 1: FALCON 1 from Launch Site West flies straight.
2. CRITICAL casualty in BADGER 2: HAWK 1 from Launch Site North bends round the jamming zone.
3. Two requests at the same instant (medic BADGER 2-DOC low on blood, a WOUNDED soldier in BADGER 3): they get different drones.
4. A CRITICAL casualty in BADGER 3 is served by OWL 1 from the rear site.
5. Two more casualties find every suitable drone busy: they queue in triage order and are served as drones come home.

## 11. Dashboard (`frontend/index.html`)

**What it does:** the one screen the judges watch. A dark Leaflet map with threat zones, launch sites, soldiers coloured by status (green OK, amber WOUNDED, red CRITICAL), medics as blue crosses (amber ring when stock is low; click for stock), and drones with live ETA badges (only drones in the air are labelled; hover a parked one for its name). The header shows connection state, the event-to-dispatch time in ms (last, with the rolling average beside it), and free drones. The side panel shows, from the top: the waiting queue (only when someone is waiting), every delivery and evacuation under way with a big countdown, then tabs for the event log, supply (launch-site stock, resupply routes, site strikes), graph queries and manual controls. A dot on a tab means news there; a strike on a site opens the Supply tab. Run demo scenario and Reset clear the log and latency first. A red banner explains any `no_dispatch` with the suggested alternative. The layer control (top left) can show the rear supply chain from Poland to the front.

**How it connects:** same host as the API, or `localhost:8000` when opened from disk. With no backend it draws `mock/snapshot.js` and says "offline: mock data". Leaflet is vendored in `frontend/vendor/leaflet` so only the map tiles need internet.

**To change:** colours are CSS variables at the top and `STATUS_COLOUR` / `DRONE_COLOUR` in the script. One function per message type lives in `handlers`. After editing `seed.py`, run `python scripts/export_mock.py` to refresh the mock.

## 12. Graph queries panel (`backend/app/querylog.py`)

**What it does:** shows the judges exactly what TuringDB did for the last request. The panel lists:
- the Cypher queries run inside the timed decision: the one candidate query, the claim check and the claim write
- the writes made after the broadcast: the `DISPATCHED_TO` edge and the casualty's status

Each query shows its milliseconds and row count; click one to see the full text.

**How it works:** `TuringRepo._read` and `_write` call `querylog.record()` after every query or write change. `record()` keeps nothing unless a `querylog.capture()` block is open, so it costs nothing elsewhere. The dev server opens one block around `engine.handle()` and one around `engine.record()`, then sends two `query_log` messages (`phase` "decide" and "record"; see contracts/messages.md).

**Why it matters for the final product:** it backs the TuringDB slide with live evidence: one query finds every suitable drone, and the whole decision is a few milliseconds of graph work. On the in-memory repo the panel says there are no graph queries.

**To change:** `MAX_CYPHER_CHARS` sets how much of a long statement is shown. Add `querylog.capture()` anywhere else you want logged, for example around `complete_dispatch` in flights.py.

## 13. Live threats and rerouting (`POST /threats`, `flights.py` `reroute`)

**What it does:** when a new threat is reported mid-mission, the system responds in four steps:
1. It writes the threat into the graph as a `NoFlyZone` (`repo.add_no_fly_zone`, MERGE by id, so redrawing a zone replaces it).
2. It rebuilds the router (`engine.zones_changed()`), so every later decision avoids the threat.
3. Every drone already in the air whose remaining path crosses the new zone gets a new shortest safe path from where it is (`tracker.reroute(zone)`).
4. The dashboard draws the zone with a pulse, redraws the affected routes and logs "FALCON 1 rerouted round New air-defence threat: +0.5 km, ETA now 3:39".

**How to trigger it:**
- On the dashboard, press **Report threat**, then click the map (800 m radius).
- From code or a feed, `POST /threats` with `{"name", "lat", "lon", "radius_m"}` or `{"name", "polygon": [[lat, lon], ...]}`.
- In the demo scenario it fires at 7 s (`DEMO_THREAT` in dev_server.py), right on FALCON 1's path while it is flying.

**Why it matters for the final product:** this is the pitch claim that a new route is found quickly when the situation changes. Measured on TuringDB, the whole reaction (graph write, router rebuild, reroutes) takes a few milliseconds.

**To change:** move or resize `DEMO_THREAT`, or edit the `(7.0, {"type": "THREAT"})` line in `DEMO_SCRIPT`. There is no range re-check after a detour (a `TUNE` note in `reroute`); the 1.2 safety margin covers demo-sized detours. The stored `DISPATCHED_TO.route_json` keeps the original route.

## 14. Drone shot down (`POST /losses`, `engine.drone_lost`)

**What happens:** when a drone is reported lost, five things follow in order:
1. The tracker takes it out of the air where it is (`tracker.lose`).
2. The graph marks it `LOST` with every item on board written off, and its `DISPATCHED_TO` edge becomes `LOST` (`repo.lose_drone`).
3. The loss spot becomes a 600 m threat zone, so nothing else flies into the same fire. Drones already flying nearby reroute.
4. If it was carrying someone's supplies, the request is retried at once as `<id>-r1` with the original timestamp. It gets the next-best drone, or if none is free it goes to the front of its triage level in the queue.
5. The dashboard greys the drone out and shows "HAWK 1 shot down on its way to BADGER 2-4. Re-sending the supplies."

**How to trigger it:** use the **Shot down** row in the Controls tab, call `POST /losses {"drone_id"}`, or watch the demo scenario, where HAWK 1 is lost at 19 s and BADGER 2-4 is served first when FALCON 1 comes home.

**To change:** `LOSS_THREAT_RADIUS_M` in dev_server.py sets the size of the zone. A `LOST` drone never counts as busy, so a request that only a lost drone could have served gets `NO_STOCK` instead of waiting forever.

## 15. Destroyed sites and supply-chain re-planning (`backend/app/supply_chain.py`, `POST /sites`)

**What it does:** every supplier, hub and hospital has a `status` in the graph (`OPERATIONAL` or `DESTROYED`). `best_path()` finds the fastest working chain that can deliver a standard restock order (`RESTOCK_ORDER`: 10 each of tourniquets, blood, gauze and chest seals) to a launch site. It searches backwards along the `SUPPLIES` edges, skips destroyed sites, and lets sites in between pass stock on without holding it themselves.

**What you see:** the **Resupply routes to launch sites** panel shows each launch site's time and chain. Destroying a site with the **Destroy** button, `POST /sites`, or the demo at 27 s (the Zaporizhzhia forward point) re-plans every chain in about 30 ms on TuringDB. The panel strikes through the old time (60 min) and shows the new one (1 h 25 min, by helicopter from the Role 3 hospital via the field hospital and a drone relay), and a banner explains it. **Show supply chain on map** zooms out to the whole chain with the active routes in green and destroyed sites marked ✕. **Restore** puts a site back in service.

**Backup links:** the seed has four backup links so knocking out one hub leaves a slower working chain:
- Dnipro hub straight to Launch Site North
- forward point to the field hospital
- two cargo-drone relays between launch sites

Destroy the field hospital as well and Launch Site West is cut off: the panel shows "cut off".

**Also uses it:** when no drone can go and no launch site holds the items, the "no drone" suggestion names the fastest working chain instead of a destroyed site.

**To change:** `RESTOCK_ORDER` sets what a chain must deliver. Add or retime `SUPPLY_LINKS` in seed.py, then run `python scripts/export_mock.py`.

Launch-site stock going down on reloads, and automatic restock orders down this chain, are section 16.


## 16. Launch-site stock and restock orders (`backend/app/stock.py`)

**What it does:** every launch site's stock is real and kept in the graph (`STOCKS` edges in TuringDB).
- **Reloads come out of it.** When a drone lands back home, `StockKeeper.reload()` tops it back up to its standard loadout (its payload the first time it took off) from that site's stock. The site goes down by exactly what the drone took. If the site is short, the drone leaves with what there is and the log says what it is missing.
- **Low sites reorder.** After every change, a site holding fewer than `REORDER_POINT` (4) of any item orders enough to get back to `REORDER_UP_TO` (12). The order goes down the fastest working supply chain (`best_path`, section 15). The source's stock is taken when it ships, and the launch site's stock goes up when it lands.
- **Shipments land after the real lead time,** played at `EDTH_ORDER_SPEED` (default 60x, so one lead-time minute is one real second).
- **Waiting drones and requests are served on arrival.** When a shipment lands, idle drones at that site that are short of kit are topped up, and the triage queue is served again, because a request may have been waiting on exactly that stock.
- **Lost shipments are re-sent.** If a hub or hospital the shipment still has to pass through is destroyed, the shipment is written off and a new order goes out on the next-best chain straight away.

**What you see:** the **Launch-site stock** panel shows each site's stock, with items below the reorder point in red and changed cells flashing. Shipments on the way are listed under the table, with the lead-time minutes left. Every reload, order placed, order landed and order lost is in the event log. Launch Site West starts with 1 unit of blood, so on startup it orders 11 from the Role 2 field hospital (35 min via a cargo-drone relay from Launch Site Rear), and they land 35 s into the demo.

**Graph cost:** each stock movement is one read and one change on TuringDB (`adjust_stock`, about 10 ms), off the decision path.

**To change:** `REORDER_POINT`, `REORDER_UP_TO` and `ORDER_SPEED` are at the top of stock.py. To keep orders in the graph too, see the `HOOK` there: write each as a `SHIPMENT` edge.

## 17. Casualty evacuation, with kit flown ahead (`backend/app/evac.py`)

**What it does:** every casualty event starts two things at once:
1. the drone with point-of-injury supplies (section 8), and
2. an evacuation to the fastest place that can treat them.

**Where they go:**
- `CRITICAL` needs surgery, so a Role 2 or Role 3 hospital. `WOUNDED` can also go to a Role 1 aid station (`ACCEPTS`).
- Only operational facilities with a free bed count. Each one gets a ground route round the threat zones (the same router the drones use) at `CASEVAC_SPEED_MPS` (about 40 km/h), plus `LOAD_S` (5 min) to treat and load first. The earliest arrival wins.
- The bed is taken from that moment (`beds_used`), so two casualties are never promised the last bed.

**Supplies ahead of the casualty:**
- The destination's stock, minus kit already promised to casualties on their way there, is checked against what this casualty needs (`TREATMENT_KIT`).
- Anything missing goes out at once as a drone request to the facility, through the normal dispatch engine. That way it queues when drones are busy and is retried if the drone is shot down.
- The seed's Role 1 aid station has no chest seals on purpose. The first `WOUNDED` casualty shows FALCON 3 flying a chest seal there, landing well before the casualty does.

**On arrival:** the kit is used up from the facility's stock, the casualty becomes `ADMITTED` in the graph and moves to the facility on the map. Anything still missing is reported.

**When the world changes:**
- A new threat on their road makes them detour.
- If the destination is destroyed, they are diverted from where they are to the next-fastest facility, and the bed is given back.

**In the graph:**
- `(:Recipient)-[:EVACUATED_TO {evac_id, severity, status, eta_s, kit_json, shortfall_json, route_json}]->(:Hospital)`.
- `beds_used` lives on the hospital.
- A kit drone's `DISPATCHED_TO` edge points straight at the hospital node.

**What you see:**
- Hospitals and the aid station are on the sector map (pink squares, beds in the popup).
- Each evacuation is a pink dotted route with an ambulance marker and an ETA countdown.
- The bed count is under the stock table, and the log says where each casualty is going and whether kit is being flown ahead.

**Limits:**
- Ground routes are straight lines round zones: there is no road network in the graph yet (`HOOK` in evac.py).
- At 10x, a 37 min evacuation takes about 4 real minutes, so admissions land after the scripted part of the demo.

## 18. Map background (`GET /tiles`, `scripts/fetch_tiles.py`)

**What it does:**
- The dashboard asks the backend for map tiles at `/tiles/{z}/{x}/{y}.png`.
- The backend serves them from `frontend/tiles/`. Any tile it doesn't have yet is fetched once from CARTO's dark basemap and kept, so every area you've looked at online works offline afterwards.
- If a tile still fails, the browser tries CARTO directly. If that fails too, the map shows an offline grid backdrop with a few real towns and a note, instead of going black.

**Before the demo:** run `python scripts/fetch_tiles.py` once on the demo laptop while it is online. It fetches about 1,600 tiles (around 10 MB, a few minutes at 8 tiles a second) for the sector, the Zaporizhzhia–Dnipro area and the whole supply chain view. After that the map needs no internet. `frontend/tiles/` is git-ignored.

**Online preview:** the hosted preview can't load map tiles (its sandbox blocks images from other sites), so it shows the offline backdrop. The real dashboard on a laptop shows the full map.
