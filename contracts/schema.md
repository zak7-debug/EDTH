# Graph schema (frozen at 11:00, owner: Ollie)

Two TuringDB graphs, `personnel` and `logistics`. The code version is `backend/app/turing_repo.py`; the Python types are in `backend/app/models.py`.

Ids carry a type prefix: `sol-07`, `med-2`, `unit-1`, `drn-04`, `dep-01`, `nfz-1`. Coordinates are WGS84 degrees, distances metres, speeds m/s, timestamps epoch seconds.

Supply vocabulary (everywhere: events, medic stock, drone payload): `tourniquet`, `blood_oneg`, `chest_seal`, `hemostatic_gauze`, `morphine_autoinjector`.

## personnel

| Node | Properties |
| --- | --- |
| `Soldier` | id, kind=`SOLDIER`, callsign, unit_id, lat, lon, status (`OK`/`WOUNDED`/`CRITICAL`), last_update |
| `Medic` | same as Soldier with kind=`MEDIC`, plus `stock_<item>` and `threshold_<item>` for every item |
| `Unit` | id, callsign |

Edges: `(Soldier)-[:MEMBER_OF]->(Unit)`, `(Medic)-[:ATTACHED_TO]->(Unit)`.

## logistics

| Node | Properties |
| --- | --- |
| `Drone` | id, callsign, depot_id, lat, lon, speed_mps, range_m (remaining), max_range_m, capacity, status (`IDLE`/`EN_ROUTE`/`RETURNING`/`CHARGING`), claimed_by (`''` when free) |
| `Depot` | id, name, lat, lon |
| `SupplyItem` | id (one node per item in the vocabulary) |
| `NoFlyZone` | id, name, polygon_json (`[[lat, lon], ...]`) |
| `Recipient` | id (the person's id; a stand-in because edges cannot cross graphs) |

Edges:
- `(Drone)-[:BASED_AT]->(Depot)`
- `(Drone)-[:CARRIES {qty}]->(SupplyItem)`
- `(Drone)-[:DISPATCHED_TO {request_id, eta_s, distance_m, ts, latency_ms, status, items_json, route_json, delivered_ts}]->(Recipient)`

## Rules TuringDB imposes

- Each property name has one fixed type across the graph. Numbers listed as floats in `models.py` are always written as floats (`turing_repo.typed()` does this), or a later `SET` fails with "Int64 and Double are incompatible".
- No null writes: `claimed_by` uses `''` for "unclaimed".
- Lists and dicts are stored as JSON strings (`polygon_json`, `items_json`, `route_json`).
