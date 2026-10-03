# Graph schema (frozen at 11:00, owner: Ollie)

Two TuringDB graphs, `personnel` and `logistics`. The code version is `backend/app/turing_repo.py`; the Python types are in `backend/app/models.py`.

Ids carry a type prefix: `sol-07`, `med-2`, `unit-1`, `drn-04`, `dep-01`, `nfz-1`, `sup-01`, `dc-01`, `hos-01`, and `tmp-<id>` for a temporary site deployed in place of a destroyed one.

The logistics graph holds the whole medical supply chain, rear to front: `Supplier` → `DistributionCentre` / `Hospital` → `Depot` → `Drone` → (personnel graph) `Medic` / `Soldier`. Coordinates are WGS84 degrees, distances metres, speeds m/s, timestamps epoch seconds.

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
| `Depot` | id, name, lat, lon (drone launch site; stock via `STOCKS`) |
| `Supplier` | id, kind=`SUPPLIER`, name, lat, lon (rear: pharma / blood service) |
| `DistributionCentre` | id, kind=`DISTRIBUTION_CENTRE`, name, lat, lon |
| `Hospital` | id, kind=`HOSPITAL`, name, lat, lon, role (`ROLE_1` aid station/`ROLE_2`/`ROLE_3`), beds, beds_used, status |
| `SupplyItem` | id (one node per item in the vocabulary) |
| `NoFlyZone` | id, name, polygon_json (`[[lat, lon], ...]`) |
| `Recipient` | id (the person's id; a stand-in because edges cannot cross graphs) |

Edges:
- `(Drone)-[:BASED_AT]->(Depot)`
- `(Drone)-[:CARRIES {qty}]->(SupplyItem)`
- `(Supplier|DistributionCentre|Hospital|Depot)-[:STOCKS {qty}]->(SupplyItem)`
- `(Supplier)-[:SUPPLIES {lead_time_min, mode}]->(DistributionCentre|Hospital)`, `(DistributionCentre|Hospital)-[:SUPPLIES {...}]->(Depot)`; `mode` is `TRUCK`/`RAIL`/`HELO`/`DRONE`
- `(Drone)-[:DISPATCHED_TO {request_id, eta_s, distance_m, ts, latency_ms, status, items_json, route_json, delivered_ts}]->(Recipient)`, or `->(Hospital)` when the drone flies a casualty's kit ahead of them
- `(Recipient)-[:EVACUATED_TO {evac_id, severity, status (EN_ROUTE/ADMITTED/DIVERTED), ts, eta_s, distance_m, kit_json, shortfall_json, route_json, resupply_request_id, closed_ts}]->(Hospital)`

## Rules TuringDB imposes

- Each property name has one fixed type across the graph. Numbers listed as floats in `models.py` are always written as floats (`turing_repo.typed()` does this), or a later `SET` fails with "Int64 and Double are incompatible".
- No null writes: `claimed_by` uses `''` for "unclaimed".
- Lists and dicts are stored as JSON strings (`polygon_json`, `items_json`, `route_json`).
