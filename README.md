# EDTH: battlefield medical resupply and dispatch

When a soldier goes critical or a medic runs low, the system finds the best drone carrying what's needed, dispatches it by the shortest safe route, and shows a live ETA. Personnel and the whole medical supply chain (suppliers, distribution centres, hospitals, depots, drones) live in two linked TuringDB graphs.

## Run

```bash
./start.sh                    # TuringDB (in-memory server) + API + dashboard on http://localhost:8000
EDTH_REPO=memory ./start.sh   # same, without a database
```

Open http://localhost:8000 and press **Run demo scenario**. Rerun `./start.sh` before every rehearsal: TuringDB keeps every change's history, so a long session gets slower (docs/turingdb-notes.md). TuringDB's graph UI is at http://localhost:8080 while it runs. Run `python scripts/fetch_tiles.py` once while online so the map background works without internet. First run creates `.venv` and installs `requirements.txt` (Python 3.11+).

## Voice reports

Press **Radio** in the dashboard's dock. Medics, identified by their Ukrainian callsign («Борсук один, медик»), report casualties, restocks and threats. Truck drivers report blocked roads from their position on the map. Use the scripted calls, hold to talk, or type. Pick who is speaking first; then free speech works: «Один поранений, важкий, потрібна кров» marks the next unhurt soldier of that squad (name them, «Борсук три-два», to be exact). The panel shows what was heard and why anything wasn't acted on. Speech to text is offline faster-whisper (`pip install -r requirements-voice.txt`, then `python scripts/fetch_whisper.py` once while online). Without it, the scripted calls fall back to their saved transcripts. From a laptop: `python -m voice.pipeline --mic --speaker "Борсук один, медик"` (needs `pip install sounddevice`). Details: README_voice_intake.md, README_audio_geolocation.md.

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
backend/app/voice.py       voice reports: speech to text, Ukrainian/English parser, POST /voice
backend/app/geo/           spoken distance + direction -> zones and road blocks (GET /zones)
voice/pipeline.py          send a recorded or live call to /voice from a laptop
sim/simulator.py           event simulator
frontend/index.html        Leaflet dashboard (mock/ = offline seed, vendor/ = Leaflet)
contracts/                 graph schema, event and WebSocket formats
docs/                      TuringDB notes, foundations guide
```

See [docs/foundations-guide.md](docs/foundations-guide.md) for what each piece does and how to change it.
