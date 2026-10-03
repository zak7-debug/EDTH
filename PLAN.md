# EDTH Weekend Plan

Team: A is Zak, B is Ollie, C is Sasank, D is Arnav. Live version: https://claude.ai/code/artifact/f11d418a-7639-4e09-adc4-a8d404a98756

## Goal and assumptions

By 22:00 tonight the full demo scenario runs end to end on TuringDB; Sunday morning is only polish, rehearsal and the pitch, with the demo at 13:00.

- **Team:** four people. Letters A to D are used throughout: A is Zak, B is Ollie, C is Sasank, D is Arnav.
- **Times** are local, assuming we start at 10:00 Saturday. If we start later, shift every Saturday slot by the same amount but keep the 22:00 freeze.
- **Must work in the demo:** event in, dispatch under a second, live ETA countdown, one reroute around a no-fly zone, a second simultaneous emergency taking a different drone, a visible latency counter, and a clear "no drone available" reason.
- **Rule for everyone:** merge small working increments to `main` at least every 90 minutes; nobody holds a branch overnight.

## Roles

Each person owns one layer end to end, so the four streams only meet at the contracts below.

| Person | Owns today | Owns Sunday |
| --- | --- | --- |
| A (Zak) | Dispatch engine: needs mapping, candidate matching, ETA, selection, drone locking, triage queue. Also owns the contracts and calls each checkpoint. | Pitch narrative and delivering the pitch |
| B (Ollie) | Data layer: TuringDB install, both graph schemas, seed data, the `GraphRepo` TuringDB implementation, then the waypoint graph and A\* routing around no-fly zones | "What the graph query did" panel content, TuringDB slide |
| C (Sasank) | Backend: FastAPI app, WebSocket hub, event simulator, drone movement tick loop, arrival and return handling, latency timing, one-command start script, README | Demo replay script so the scenario runs identically every time |
| D (Arnav) | Frontend: Leaflet map, soldier, medic and drone layers, routes and no-fly zones, ETA countdowns, event log, trigger-emergency panel, latency counter | Visual polish, pitch deck, screen recording as a backup |

A also writes the in-memory `GraphRepo` in the first hour, so A, C and D never wait on TuringDB.

## Contracts to agree by 11:00

These four contracts are written into `contracts/` in the repo during kickoff and frozen at 11:00; changing one after that needs a two-minute huddle with whoever consumes it.

**1. Graph schema (owner B).** Two namespaced graphs, `personnel` and `logistics`, as in the project brief. Node ids are strings with a type prefix (`sol-07`, `med-02`, `drn-04`, `dep-01`). Supplies use one shared item vocabulary: `tourniquet`, `blood_oneg`, `chest_seal`, `hemostatic_gauze`, `morphine_autoinjector`. Area: a plausible German training area such as Grafenwöhr, 20 personnel, 8 drones, 3 depots.

**2. `GraphRepo` interface (owner B, used by A and C).** One Python protocol with an in-memory and a TuringDB implementation:

- `get_person(id)`, `update_person(id, **props)`, `list_personnel()`
- `find_candidate_drones(items: dict[str, int]) -> list[Drone]` returns idle drones carrying enough of every item, in one query
- `claim_drone(drone_id, request_id) -> bool` is atomic and returns False if already claimed
- `create_dispatch(drone_id, recipient_id, eta_s, ts)`, `complete_dispatch(...)`, `update_drone(id, **props)`, `list_drones()`, `list_depots()`, `list_no_fly_zones()`

**3. Event format (owner C).** Every input, simulated or real, is one JSON object posted to `POST /events`:

```json
{"event_id": "evt-123", "type": "CASUALTY | LOW_STOCK", "subject_id": "sol-07", "severity": "CRITICAL", "items": {"blood_oneg": 2}, "lat": 49.70, "lon": 11.93, "ts": 1696320000.123}
```

**4. WebSocket messages (owner C, consumed by D).** One socket at `/ws`, each message `{"type": ..., "data": ...}`:

| type | Sent when | Key fields in data |
| --- | --- | --- |
| `snapshot` | On connect | all personnel, drones, depots, no-fly zones |
| `event` | Event received | the event plus `received_ts` |
| `dispatch` | Drone assigned | request_id, drone_id, recipient_id, route (list of [lat, lon]), eta_s, latency_ms |
| `no_dispatch` | No drone fits | request_id, reason, nearest_alternative |
| `drone_update` | Every tick, about 2 per second | drone_id, lat, lon, status, eta_s |
| `delivered` | Drone arrives | request_id, drone_id, recipient_id, items |
| `queue` | Queue changes | pending requests in triage order |

REST beyond events stays minimal: `GET /state` mirrors `snapshot`, and `POST /scenario/{name}` triggers a scripted demo scenario.

## Saturday: build day

The core flow must be integrated by 18:00, which leaves four hours for no-fly routing, the demo script and slack before the 22:00 freeze.

| Time | Phase | Work |
| --- | --- | --- |
| 10:00 to 11:00 | Kickoff, all four together | Agree schema, repo interface, event and WebSocket formats; push the repo skeleton. B starts the TuringDB install. A writes the in-memory repo and seed loader. |
| **11:00** | **Gate** | Contracts frozen in the repo |
| 11:00 to 14:00 | Build the four layers in parallel | A: matching, ETA and selection with unit tests. B: TuringDB seed data and repo. C: FastAPI, events, WebSocket and the simulator. D: map rendering a mock snapshot. |
| **14:00** | **Checkpoint 1** | Dispatch via the API under 1 s; TuringDB go or no-go |
| 14:00 to 18:00 | Integrate the core flow | Event, dispatch, drone movement, arrival; live ETA and latency counter on the map. B swaps TuringDB in behind the repo. A adds drone locking and the triage queue. |
| **18:00** | **Checkpoint 2** | Core flow runs on one machine from main |
| 18:00 to 22:00 | Build the demo scenario | B: waypoint graph and A\* around no-fly zones. A: no-drone reasons and alternatives. C: scripted scenario and start script. D: reroute and queue shown on the map. |
| **22:00** | **Checkpoint 3** | Demo runs twice in a row; feature freeze |

At each checkpoint all four stop for 15 minutes, pull `main`, run the whole stack on one laptop and decide what to cut. Lunch is at the 14:00 checkpoint and dinner around 19:00, eaten at the desk if behind.

**Done by 14:00, per person**

- [ ] A: `dispatch(event)` returns the best drone and ETA against the in-memory repo, with unit tests for matching, range check and two requests not sharing a drone
- [ ] B: TuringDB running, both graphs seeded, candidate-drone query returns results in one call
- [ ] C: `POST /events` calls the engine and broadcasts `dispatch` on `/ws`; simulator fires a CRITICAL casualty
- [ ] D: map of the training area showing personnel, drones and depots from a mock `snapshot`

**Done by 18:00**

- [ ] Trigger button on the map leads to a drone moving with a counting-down ETA, then `delivered`, inventory updated, drone returns
- [ ] Latency counter shows event-to-dispatch ms in the UI and logs
- [ ] Two emergencies fired at once get two different drones

**Done by 22:00**

- [ ] One drone routed around a no-fly zone, visible on the map
- [ ] No-drone case shows the reason and nearest alternative
- [ ] `./start.sh` brings up the whole stack from a fresh clone

## Sunday to 13:00

No new features on Sunday: anything not working at 22:00 Saturday is cut, and the morning goes to making the demo reliable and the pitch sharp.

| Time | Everyone | Notes |
| --- | --- | --- |
| 08:00 to 08:30 | Fresh clone on a clean laptop, run the start script, run the demo scenario | Whatever breaks here is the first fix |
| 08:30 to 10:30 | Bug fixes and polish only. A and D draft the pitch, C hardens the replay script, B builds the graph query panel and TuringDB slide | Merge to `main` only with a second person's check |
| 10:30 | Feature freeze, tag `demo-v1` | From here only demo-blocking fixes |
| 10:30 to 12:00 | Three full rehearsals: 3-minute pitch plus live demo, timed. D records a backup video of the best run | Each run, one person plays judge and asks hard questions |
| 12:00 to 12:45 | Final setup on the demo laptop, offline check, backup video ready | Phones off, notifications muted |
| 13:00 | Demo | |

- [ ] Pitch deck: problem, live demo, why a graph database, latency numbers, what's next
- [ ] Backup demo video recorded
- [ ] README with run instructions tested from a fresh clone

## Risks, TuringDB fallback and cut list

The biggest risk is TuringDB, so the plan never lets it block anyone but B, and gives it two hard decision points.

**TuringDB fallback**

- If B is stuck on any single TuringDB issue for 45 minutes, B tells A and everyone keeps building on the in-memory repo.
- **14:00 go or no-go:** the TuringDB repo must pass the same repo tests as the in-memory one (seed, candidate query, property update, atomic claim). If not, B keeps going on TuringDB but the integration runs on in-memory.
- **20:00 final call:** if TuringDB still can't run the demo scenario, we demo on in-memory and pitch TuringDB through the schema and query panel. Nobody touches the database layer after 20:00.
- Known gaps to check first: whether TuringDB supports in-place property updates and transactions. If not, model drone state as a latest-version node and do the atomic claim with an in-process lock in the engine.

**Other risks**

- **Latency over a second:** A measures from day one; the candidate query must be one round trip, and ETA math runs in Python on the results.
- **Integration drift:** contracts frozen at 11:00 and checkpoints at 14:00, 18:00 and 22:00, where we run the whole thing together on one machine.
- **Demo flakiness:** the scripted scenario uses a fixed random seed so every run is identical.

**Cut list if behind** (cut from the top first)

1. Multi-drone dispatch and relaying
2. Triage preemption (keep the CRITICAL-first queue)
3. Replay mode
4. Dynamic rerouting mid-flight (keep the static no-fly route at dispatch time)
5. Live graph query panel (replace with a slide)
6. Vitals and unit membership edges on the map

Never cut: event to dispatch to live ETA to arrival, the latency counter, two simultaneous emergencies taking different drones, and the no-drone reason.
