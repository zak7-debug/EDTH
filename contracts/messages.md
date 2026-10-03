# Event and WebSocket formats (frozen at 11:00, owner: Sasank)

Python builders for every message live in `backend/app/messages.py`; the dataclasses in `backend/app/models.py`.

## Events in: `POST /events`

Every input, simulated or real telemetry, is one JSON object:

```json
{"event_id": "evt-123", "type": "CASUALTY", "subject_id": "sol-07", "severity": "CRITICAL",
 "items": {}, "lat": 49.70, "lon": 11.87, "ts": 1791016000.123}
{"event_id": "evt-124", "type": "LOW_STOCK", "subject_id": "med-2", "severity": null,
 "items": {"blood_oneg": 2}, "lat": 49.69, "lon": 11.86, "ts": 1791016000.456}
```

- `type`: `CASUALTY` (needs `severity`: `CRITICAL` or `WOUNDED`) or `LOW_STOCK` (needs `items`).
- For `CASUALTY` the engine decides the items: CRITICAL needs 1 tourniquet, 2 blood_oneg, 1 hemostatic_gauze; WOUNDED needs 1 tourniquet, 1 chest_seal. `items`, if given, is added on top.
- `event_id` doubles as the `request_id` of any resulting dispatch.
- Triage order for the queue: CRITICAL, then WOUNDED, then LOW_STOCK; oldest first within a level.

## WebSocket out: `/ws`

One socket. Every message is `{"type": ..., "data": ...}`.

| type | Sent when | data |
| --- | --- | --- |
| `snapshot` | on connect | `units`, `personnel`, `drones`, `depots` (with `stock`), `no_fly_zones`, `facilities` (suppliers, distribution centres, hospitals, with `stock`), `supply_links` (`src_id`, `dst_id`, `lead_time_min`, `mode`), `dispatches` (active ones), each a list of the model dicts |
| `event` | event received | the event fields plus `received_ts` |
| `dispatch` | drone assigned | `request_id`, `drone_id`, `recipient_id`, `items`, `eta_s`, `distance_m`, `route` (list of `[lat, lon]`), `latency_ms`, `ts`, `status` |
| `no_dispatch` | no drone fits | `request_id`, `recipient_id`, `reason` (text), `reason_code` (`NO_STOCK`/`ALL_BUSY`/`OUT_OF_RANGE`), `nearest_alternative` (`{drone_id, eta_s, note}` or null), `latency_ms` |
| `drone_update` | each tick, about 2/s per moving drone | `drone_id`, `lat`, `lon`, `status`, `eta_s`, `request_id` |
| `delivered` | drone arrives | `request_id`, `drone_id`, `recipient_id`, `items`, `ts` |
| `query_log` | after each decision and its writes | `request_id`, `phase` (`decide` = inside the timed decision, `record` = graph writes after the broadcast), `queries`: list of `{graph, kind (read/write), cypher, ms, rows}`, `total_ms`. Empty `queries` on the in-memory repo |
| `zone_added` | a threat is reported (`POST /threats`) | the zone: `id`, `name`, `polygon` |
| `reroute` | a drone in the air changes course round a new zone | `drone_id`, `request_id`, `phase` (`EN_ROUTE`/`RETURNING`), `route` (from its current position), `distance_m`, `eta_s`, `added_m`, `zone` (name) |
| `drone_lost` | a drone is reported shot down (`POST /losses`) | `drone_id`, `lat`, `lon`, `phase` (what it was doing), `request_id` and `recipient_id` of the lost delivery (or null), `items_lost`. A `zone_added` for the loss spot and the retry's `dispatch` / `no_dispatch` (request_id + `-r1`) follow |
| `supply_chain` | on connect, and when a site is destroyed or restored (`POST /sites`) | `order` (the restock order), `status` (`{facility_id: OPERATIONAL/DESTROYED}`), `routes`: per launch site `{depot_id, source_id, path (ids, source first), legs [{src_id, dst_id, mode, minutes}], minutes}` or `path: null` when cut off, `changed` (`{facility_id, status, ms}` or null) |
| `queue` | queue changes | `pending`: list of `{request_id, type, severity, subject_id, items, ts, position}` in triage order |
| `stock_update` | on connect, and whenever stock moves: a drone reloads, a restock order is placed / lands / is lost, kit lands at a hospital, a patient is treated | `depots` (`{id, name, stock}`), `facilities` (`{id, stock, beds, beds_used, status}`), `orders` (`{order_id, depot_id, source_id, items, path, legs, minutes, placed_ts, status (IN_TRANSIT/DELIVERED/LOST), lost_at, replaces, remaining_min, remaining_s_real}`), `reorder_point`, `reorder_up_to`, `order_speed`, `change` (`{kind, note, ...}` or null; kind is `reload`, `order_placed`, `order_arrived`, `order_lost`, `order_failed`, `kit_delivered` or `treated`; a reload also carries `drone_id`, `taken`, `short`) |
| `evacuation` | a casualty starts evacuating, detours round a new zone, or is diverted (destination destroyed) | `evac_id`, `person_id`, `facility_id`, `facility_name`, `severity`, `route` (from the casualty), `distance_m`, `eta_s` (mission s, includes treat-and-load time), `kit`, `shortfall` (flown ahead), `resupply_request_id` (the kit drone's `request_id`, or null), `kit_eta_s`, `status`, `note`, `diverted_from` (facility id or null). `evac_id: null` + `reason` when nowhere can take them. A `dispatch` / `no_dispatch` for the kit follows when `shortfall` is not empty (`recipient_id` is the facility) |
| `evac_update` | each tick per evacuation | `evac_id`, `person_id`, `lat`, `lon`, `phase` (`LOADING` = being treated and loaded, `MOVING`), `eta_s` |
| `admitted` | the casualty arrives | `evac_id`, `person_id`, `facility_id`, `facility_name`, `kit_used`, `kit_short`, `beds_used`, `beds`. The person's status becomes `ADMITTED` |

`latency_ms` is measured from the moment `POST /events` receives the event to the moment the dispatch decision is made, before any graph writes.

REST besides events: `POST /sites` (`{facility_id, status}`) destroys or restores a supply-chain site; `POST /losses` (`{drone_id}`) reports a drone shot down; `POST /threats` (`{name, lat, lon, radius_m}` or `{name, polygon}`) reports a new threat zone; `GET /state` returns the `snapshot` data; `POST /scenario/{name}` triggers a scripted demo scenario; `GET /health`; `GET /tiles/{z}/{x}/{y}.png` serves the map background from the local cache (fetching and keeping missing tiles; 404 when offline and not cached).

The `snapshot` also carries `evacuations` (the ones under way, as in `evacuation`), and each facility has `role` and `beds_used`. A drone carrying a casualty's kit ahead of them has a facility id as `recipient_id` in `dispatch` / `delivered`.

Example `dispatch`:

```json
{"type": "dispatch", "data": {"request_id": "evt-123", "drone_id": "drn-04", "recipient_id": "sol-07",
 "items": {"tourniquet": 1, "blood_oneg": 2, "hemostatic_gauze": 1}, "eta_s": 214.6,
 "distance_m": 6008.0, "route": [[49.661, 11.84], [49.70, 11.87]], "latency_ms": 7.4,
 "ts": 1791016000.130, "status": "EN_ROUTE"}}
```
