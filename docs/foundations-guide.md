# Foundations guide: what each piece does and how to change it

This is the "why" behind the first-hour foundations. Every later piece (dispatch engine, API, simulator, map) is built on these files, so this guide says what each one decides, what depends on it, and where to edit when the demo needs something different.

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

**What it does:** builds the same world for both repos around Grafenwöhr Training Area: 3 squads (20 people, one medic each), 3 depots, 8 drones and 2 no-fly zones. Positions are fixed (random jitter with a fixed seed), so every run and every test sees the same world.

**Why the numbers are what they are (all chosen for the demo):**
- The squads patrol west of the **central impact area** no-fly zone, and **Main Post** depot sits on its far side, so drones from Main Post must route around it. That's the reroute beat.
- Payloads are deliberately uneven: `drn-03` and `drn-06` carry no blood, `drn-05` has a nearly flat battery (only 4 km of range left), `drn-08` is charging. A CRITICAL casualty has five possible drones; once those are busy, the next one gets a clear "no drone" reason.
- **`med-2` starts with 1 unit of blood against a threshold of 2**: the demo's low-stock event is already true in the data.

**To change:** edit the tables at the top of `seed.py` (`_DRONES`, `DEPOTS`, `NO_FLY_ZONES`, `_MEDIC_STOCK`, `_SQUAD_CENTRES`). `python -m backend.app.seed` prints the counts; `pytest` checks the candidate sets, so update `test_find_candidate_drones` if you change payloads.

## 5b. The full medical supply chain

**What it does:** the logistics graph models every level, not just drones:

```text
Supplier (rear)  ──SUPPLIES──>  Distribution centre / Hospital  ──SUPPLIES──>  Depot  <──BASED_AT──  Drone  ──DISPATCHED_TO──>  Medic / Soldier
```

Each level holds stock (`STOCKS {qty}` edges to the same `SupplyItem` nodes drones `CARRIES`), and each `SUPPLIES` link has a lead time and a transport mode. Seeded: 2 suppliers (Nuremberg and Regensburg areas), 2 distribution centres, a Role 2 field hospital near Main Post and a Role 3 hospital near Weiden, 10 links.

**Why it matters for the final product:**
- **The pitch:** one graph shows the whole chain from factory to wounded soldier. A question like "which facility can restock Range 301 with blood, and how fast?" is a single 2-hop query (`find_resupply_sources`), which is exactly what graph databases are good at.
- **The "no drone available" answer gets better:** instead of only "nothing carries blood", the engine can say "drn-07 can serve after reloading at Range 301, restocked from Vilseck forward distribution point in 20 min".
- **Later:** hospitals are where casualties get evacuated to, so a CASEVAC extension has its destinations already.

The real-time dispatch path is unchanged: drones still launch from depots, and the upstream levels are read only when explaining or planning resupply. That keeps the sub-second dispatch untouched.

**To change:** the `FACILITIES` and `SUPPLY_LINKS` tables in `seed.py`. Range 301 (`dep-02`) is deliberately short of blood, and the field hospital holds no chest seals, so resupply answers differ by item.

## 6. Event and WebSocket formats (`contracts/messages.md`, `backend/app/messages.py`)

**What it does:** fixes the JSON for everything that crosses a boundary: events into `POST /events`, and the seven message types out on `/ws` (`snapshot`, `event`, `dispatch`, `no_dispatch`, `drone_update`, `delivered`, `queue`). `messages.py` has one builder per type so the backend can't drift from the contract.

**Why it matters:** this is the seam between Sasank's backend and Arnav's map. As long as both stick to it, they can build in parallel and integrate in minutes. Swapping the simulator for real telemetry later only means producing the same event JSON.

**To change:** after the 11:00 freeze, agree it with whoever consumes the message, then update `contracts/messages.md` and the builder together.

## 7. Skeleton, start script and tests

- `start.sh`: creates the venv on first run, starts TuringDB in memory with its UI, and runs the API on port 8000. `EDTH_REPO=memory ./start.sh` runs without a database.
- `backend/app/main.py`: a stub (`/health`, `/state`) so the start script works today. Sasank replaces it.
- `dispatch.py`, `routing.py`, `sim/simulator.py`, `frontend/`: empty placeholders owned by Zak, Ollie, Sasank and Arnav.
- `tests/`: `pytest` runs every repo test against both repos. The TuringDB run uses the embedded engine, so no server is needed. `TURINGDB_TEST_HOST=http://localhost:6666 pytest` runs them against a live server.
