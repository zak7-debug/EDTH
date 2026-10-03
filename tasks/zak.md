# Zak (A): dispatch engine and contracts

Zak owns the decision at the heart of the demo: given an event, which drone goes and when it arrives, in well under a second.

### 10:00 to 11:00: Foundation

- [ ] Everything in the Start now section of [PLAN.md](../PLAN.md): skeleton, venv, TuringDB smoke test, contracts, seed data, push
- [ ] Done when: teammates can pull `main` and import `models`, `repo` and `seed`

### 11:00 to 12:30: Dispatch engine v1 (`dispatch.py`)

- [ ] Needs table: CASUALTY + CRITICAL needs 1 tourniquet, 2 blood_oneg, 1 hemostatic_gauze; CASUALTY + WOUNDED needs 1 tourniquet, 1 chest_seal; LOW_STOCK needs the items named in the event
- [ ] `InMemoryRepo` implementing every `GraphRepo` method from seed data
- [ ] Candidate filter: drone IDLE, carries every needed item in enough quantity
- [ ] Range check: distance to recipient plus distance from recipient to nearest depot, times 1.2 safety margin, must fit in remaining range
- [ ] ETA: haversine distance divided by drone speed; pick the lowest ETA
- [ ] Done when: `dispatch(event)` returns a `Dispatch` or a `NoDispatch` with a reason

### 12:30 to 14:00: Tests and latency

- [ ] pytest: picks the nearest suitable drone; skips a drone with too little stock; skips one out of range; no drone gives a reason
- [ ] pytest: two events dispatched at the same moment get two different drones
- [ ] Time `dispatch()` with `time.perf_counter()` and log it; target under 50 ms in memory
- [ ] Done when: tests green on `main`, ready for checkpoint 1

### 14:00 to 16:00: Concurrency and triage queue

- [ ] `claim_drone` guarded by one `asyncio.Lock`, so the check and the claim can't interleave
- [ ] Priority queue for unserved requests: CRITICAL first, then WOUNDED, then LOW_STOCK, oldest first within a level
- [ ] When a drone becomes IDLE, drain the queue before anything else
- [ ] Done when: firing three CRITICAL events with two free drones dispatches two and queues one

### 16:00 to 18:00: Integrate

- [ ] Pair with Sasank to call the engine from `POST /events`
- [ ] Pair with Ollie to swap in `TuringRepo`; keep the dispatch path to one candidate query and do graph writes after the dispatch message is sent
- [ ] Done when: checkpoint 2 passes on one laptop

### 18:00 to 20:00: No-drone handling

- [ ] Reasons: no drone carries the item, all suitable drones busy (queued, with position), suitable drones out of range
- [ ] `nearest_alternative`: the closest drone that could serve after resupplying at a depot, with its longer ETA
- [ ] Done when: each reason shows up on Arnav's map

### 20:00 to 22:00: Routing and run-through

- [ ] Use Ollie's route length instead of straight-line distance for ETA and range
- [ ] Run the demo scenario end to end twice, fix what breaks
- [ ] Draft the pitch outline

### Sunday

- [ ] Pitch narrative and delivery; lead the three rehearsals
