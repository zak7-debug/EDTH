# EDTH: battlefield medical resupply and dispatch

When a soldier goes critical or a medic runs low, the system finds the best drone carrying what's needed, dispatches it by the shortest safe route, and shows a live ETA. Personnel and the whole medical supply chain (suppliers, distribution centres, hospitals, depots, drones) live in two linked TuringDB graphs.

## Run

```bash
./start.sh                    # TuringDB (in-memory server) + API + dashboard on http://localhost:8000
EDTH_REPO=memory ./start.sh   # same, without a database
```

Open http://localhost:8000 and press **Run demo scenario**. TuringDB's graph UI is at http://localhost:8080 while it runs. First run creates `.venv` and installs `requirements.txt` (Python 3.11+).

## Test

```bash
source .venv/bin/activate
pytest                                   # every repo test runs on InMemoryRepo and TuringRepo (embedded)
python scripts/turingdb_smoke.py         # TuringDB timings
```

## Layout

```text
backend/app/models.py      shared dataclasses: Person, Drone, Depot, Event, Dispatch, ...
backend/app/repo.py        GraphRepo interface, InMemoryRepo, get_repo()
backend/app/turing_repo.py TuringDB implementation
backend/app/seed.py        South-east Ukraine sector (fictional laydown): suppliers, hospitals, hubs, 3 launch sites,
                           8 drones, 20 personnel, 2 no-fly zones
backend/app/messages.py    WebSocket message builders
backend/app/main.py        FastAPI app (Sasank, stub for now)
backend/app/dev_server.py  working API: POST /events, /ws, tick loop, demo scenario, /reset
backend/app/flights.py     flies dispatched drones, delivers, returns, drains the queue
backend/app/dispatch.py    dispatch engine
backend/app/routing.py     shortest path round threat zones (visibility graph + A*)
sim/simulator.py           event simulator
frontend/index.html        Leaflet dashboard (mock/ = offline seed, vendor/ = Leaflet)
contracts/                 graph schema, event and WebSocket formats
docs/                      TuringDB notes, foundations guide
```

See [docs/foundations-guide.md](docs/foundations-guide.md) for what each piece does and how to change it.
