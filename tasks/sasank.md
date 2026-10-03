# Sasank (C): backend, simulator and live updates

Sasank owns everything that moves: events in, messages out over the WebSocket, drones flying along their routes, and the scripted demo.

### Arrival to +1 hour: API skeleton (`main.py`)

- [ ] FastAPI app with `GET /health`, `GET /state`, `POST /events`, and `/ws` with a small connection manager that broadcasts to every client
- [ ] `/ws` sends `snapshot` on connect, built from the repo
- [ ] Done when: `uvicorn backend.app.main:app --reload` runs and a test client receives `snapshot`

### Until 14:00: Events to dispatch

- [ ] `POST /events`: stamp `received_ts`, write the person update via the repo, call `dispatch()`, broadcast `event` then `dispatch` or `no_dispatch`
- [ ] `latency_ms` = time from request received to dispatch decided, measured with `time.perf_counter()`; log every one
- [ ] Use a stub `dispatch()` that returns the first idle drone until Zak's engine lands
- [ ] `sim/simulator.py` posts events over HTTP, so real telemetry can replace it later; random mode plus `--scenario demo`
- [ ] Done when: the simulator fires a CRITICAL casualty and a WebSocket client sees `dispatch`

### 14:00 to 18:00: Drone movement and arrival

- [ ] Background asyncio task ticking twice a second, with a time multiplier (say 10x) so demo flights last under a minute
- [ ] Move each EN_ROUTE drone along its route, reduce battery, broadcast `drone_update` with the remaining `eta_s`
- [ ] On arrival: broadcast `delivered`, update the recipient's stock via the repo, set the drone RETURNING to the nearest depot, then IDLE, then ask the engine to drain its queue
- [ ] Personnel drift slowly in the simulator so the map looks alive
- [ ] Done when: checkpoint 2, a full flight from trigger to drone back at base

### 18:00 to 22:00: Demo scenario and start script

- [ ] Scripted scenario with a fixed seed: squad on patrol; at +5 s a soldier goes CRITICAL; at +5.2 s a nearby medic reports low blood; at +6 s a second CRITICAL in another squad; one route crosses a no-fly zone
- [ ] `POST /scenario/{name}` starts it from the dashboard
- [ ] `start.sh`: start TuringDB, load seed data, start uvicorn serving the frontend, print the URL
- [ ] README: install and run in under five commands
- [ ] Done when: a fresh clone runs the demo with `./start.sh`

### Sunday

- [ ] Harden the replay so the scenario runs identically every time; test on the demo laptop
