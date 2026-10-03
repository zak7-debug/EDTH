# EDTH: battlefield medical resupply and dispatch

When a soldier goes critical or a medic runs low, the system finds the best drone carrying what's needed, dispatches it by the shortest safe route, and shows a live ETA. Personnel and drone fleet live in two linked TuringDB graphs.

## Run

```bash
./start.sh                    # TuringDB (in-memory server) + API on http://localhost:8000
EDTH_REPO=memory ./start.sh   # same, without a database
```

TuringDB's graph UI is at http://localhost:8080 while it runs. First run creates `.venv` and installs `requirements.txt` (Python 3.11+).

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
backend/app/seed.py        Grafenwöhr world: 20 personnel, 8 drones, 3 depots, 2 no-fly zones
backend/app/messages.py    WebSocket message builders
backend/app/main.py        FastAPI app and /ws
backend/app/dispatch.py    dispatch engine
backend/app/routing.py     waypoint graph + A*
sim/simulator.py           event simulator
frontend/                  Leaflet dashboard
contracts/                 graph schema, event and WebSocket formats
docs/                      TuringDB notes, foundations guide
```

See [docs/foundations-guide.md](docs/foundations-guide.md) for what each piece does and how to change it.
