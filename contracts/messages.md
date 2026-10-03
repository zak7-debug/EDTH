# Event and WebSocket formats (frozen at 11:00, owner: Sasank)

Python builders for every message live in `backend/app/messages.py`; the dataclasses in `backend/app/models.py`.

## Events in: `POST /events`

Every input, simulated or real telemetry, is one JSON object:

```json
{"event_id": "evt-123", "type": "CASUALTY", "subject_id": "sol-07", "severity": "CRITICAL",
 "items": {}, "lat": 49.70, "lon": 11.87, "ts": 1791016000.123}
{"event_id": "evt-124", "type": "LOW_STOCK", "subject_id": "med-2", "severity": null,
 "items": {"blood_oneg": 2}, "urgency": "URGENT", "lat": 49.69, "lon": 11.86, "ts": 1791016000.456}
```

- `type`: `CASUALTY` (needs `severity`: `CRITICAL` or `WOUNDED`) or `LOW_STOCK` (needs `items`).
- For `CASUALTY` the engine decides the items: CRITICAL needs 1 tourniquet, 2 blood_oneg, 1 hemostatic_gauze; WOUNDED needs 1 tourniquet, 1 chest_seal. `items`, if given, is added on top.
- `event_id` doubles as the `request_id` of any resulting dispatch.
- `urgency` (LOW_STOCK only, optional): `CRITICAL`, `URGENT` or `NON_URGENT` (the default). A medic's restock always ends with a drone: CRITICAL and URGENT get an idle drone loaded to order at a launch site holding the items; NON_URGENT waits for a drone coming home. If no launch site holds them, one is restocked and the `no_dispatch` (reason_code `AWAITING_STOCK`) gives the ETA.
- Triage order for the queue: CRITICAL casualty, CRITICAL restock, WOUNDED casualty, URGENT restock, NON_URGENT restock; oldest first within a level.

## WebSocket out: `/ws`

One socket. Every message is `{"type": ..., "data": ...}`.

| type | Sent when | data |
| --- | --- | --- |
| `snapshot` | on connect | `units`, `personnel`, `drones`, `depots` (with `stock`), `no_fly_zones`, `facilities` (suppliers, distribution centres, hospitals, with `stock`), `supply_links` (`src_id`, `dst_id`, `lead_time_min`, `mode`), `dispatches` (active ones), each a list of the model dicts |
| `event` | event received | the event fields plus `received_ts` |
| `dispatch` | drone assigned | `request_id`, `drone_id`, `recipient_id`, `items`, `eta_s`, `distance_m`, `route` (list of `[lat, lon]`), `latency_ms`, `ts`, `status` |
| `no_dispatch` | no drone fits | `request_id`, `recipient_id`, `reason` (text), `reason_code` (`NO_STOCK`/`ALL_BUSY`/`OUT_OF_RANGE`/`AWAITING_STOCK`), `nearest_alternative` (`{drone_id, eta_s, note}` or null; for `AWAITING_STOCK` also `via_depot` and `order_id`, and `eta_s` is the medic's ETA in mission seconds), `latency_ms` |
| `drone_update` | each tick, about 2/s per moving drone | `drone_id`, `lat`, `lon`, `status`, `eta_s`, `request_id` |
| `delivered` | drone arrives | `request_id`, `drone_id`, `recipient_id`, `items`, `ts` |
| `query_log` | after each decision and its writes | `request_id`, `phase` (`decide` = inside the timed decision, `record` = graph writes after the broadcast), `queries`: list of `{graph, kind (read/write), cypher, ms, rows}`, `total_ms`. Empty `queries` on the in-memory repo |
| `zone_added` | a threat is reported (`POST /threats`) | the zone: `id`, `name`, `polygon` |
| `reroute` | a drone in the air changes course round a new zone | `drone_id`, `request_id`, `phase` (`EN_ROUTE`/`RETURNING`), `route` (from its current position), `distance_m`, `eta_s`, `added_m`, `zone` (name) |
| `drone_lost` | a drone is reported shot down (`POST /losses`) | `drone_id`, `lat`, `lon`, `phase` (what it was doing), `request_id` and `recipient_id` of the lost delivery (or null), `items_lost`. A `zone_added` for the loss spot and the retry's `dispatch` / `no_dispatch` (request_id + `-r1`) follow |
| `supply_chain` | on connect, and when a site is destroyed or restored (`POST /sites`) | `order` (the restock order), `status` (`{facility_id: OPERATIONAL/DESTROYED/SETTING_UP}`), `routes`: per launch site `{depot_id, source_id, path (ids, source first), legs [{src_id, dst_id, mode, minutes}], minutes}` or `path: null` when cut off, `changed` (`{facility_id, status, ms}` or null; status `SETTING_UP` when a temporary site's team was sent, `READY` when it opened), `road_legs` (`{"src>dst": [[lat, lon], ...]}`: road waypoints for in-sector TRUCK links, roads.py), `suggestions`: per destroyed site with no stand-in yet, `{id (`tmp-<site>`, or null with a `note` when none is needed), replaces, replaces_name, kind, role, name, lat, lon, where, stock, beds, links [{src_id, dst_id, lead_time_min, mode}], km_from_lost, cover, setup, convoy, backfill, drone (as in `site_deployed`), evac_min {before, now, with}, supply_min {depot_id: {before, now, with}}, why [text], ms}` (resilience.py). Deploy one with `POST /sites/deploy {"replaces": id}` |
| `queue` | queue changes | `pending`: list of `{request_id, type, severity, urgency, subject_id, items, ts, position}` in triage order |
| `stock_update` | on connect, and whenever stock moves: a drone reloads, a restock order is placed / lands / is lost, kit lands at a hospital, a patient is treated | `depots` (`{id, name, stock}`), `facilities` (`{id, stock, beds, beds_used, status}`), `orders` (`{order_id, depot_id, source_id, items, path, legs, minutes, placed_ts, status (IN_TRANSIT/DELIVERED/LOST), lost_at, replaces, kind (RESTOCK, or TEAM/CONVOY/BACKFILL for a temporary site), remaining_min, remaining_s_real}`), `reorder_point`, `reorder_up_to`, `order_speed`, `change` (`{kind, note, ...}` or null; kind is `reload`, `order_placed`, `order_arrived`, `order_lost`, `order_failed`, `kit_delivered`, `treated`, `loaded_to_order` (a drone loaded at a launch site for a medic's restock), `site_deployed` or `site_ready`; a reload also carries `drone_id`, `taken`, `short`) |
| `evacuation` | a casualty starts evacuating, detours round a new zone, or is diverted (destination destroyed) | `evac_id`, `person_id`, `facility_id`, `facility_name`, `severity`, `route` (from the casualty), `distance_m`, `eta_s` (mission s, includes treat-and-load time), `kit`, `shortfall` (flown ahead), `resupply_request_id` (the kit drone's `request_id`, or null), `kit_eta_s`, `status`, `note`, `diverted_from` (facility id or null). `evac_id: null` + `reason` when nowhere can take them. A `dispatch` / `no_dispatch` for the kit follows when `shortfall` is not empty (`recipient_id` is the facility) |
| `evac_update` | each tick per evacuation | `evac_id`, `person_id`, `lat`, `lon`, `phase` (`WAITING_FOR_DRONE` = the casualty's supplies haven't landed, `TREATING` = 10 min of treatment and loading after they land, `MOVING` = driving by road), `eta_s` |
| `admitted` | the casualty arrives | `evac_id`, `person_id`, `facility_id`, `facility_name`, `kit_used`, `kit_short`, `beds_used`, `beds`. The person's status becomes `ADMITTED` |
| `site_deployed` | a suggested temporary site's team was sent (`POST /sites/deploy`) | `facility` (as in `snapshot.facilities`, `status` `SETTING_UP`, empty stock), `links` (its new SUPPLIES links), `replaces`, `ms`, `where`, `cover` (`{id, kind WOODLAND/STRUCTURES, name, lat, lon, radius_m}` or null), `setup` (`{team_from, team_from_name, team_min, team_kit, setup_min, ready_min, ready_in_s}`; `ready_in_s` is real seconds), `convoy` and `backfill` (best_path shape plus `items`, or null), `drone` (`{items}` or null). An updated `supply_chain` (changed.status `SETTING_UP`) and `stock_update`s for the team, convoy and refill orders follow. The site takes no casualties and passes no stock on until `site_ready` |
| `site_ready` | a temporary site finished setting up (its set-up time passed, or `POST /sites/ready {"facility_id"}` skipped the wait) | `facility` (now `OPERATIONAL`), `ms`. An updated `supply_chain` (changed.status `READY`), any `evacuation`s it diverts, the drone with blood (`dispatch` or `no_dispatch`, then `queue`) and a `stock_update` (change kind `site_ready`) follow |
| `voice_report` | a medic's radio report is heard (`POST /voice` audio, or `POST /voice/text`) | `report_id`, `transcript`, `language` (`uk`/`en`), `english` (summary built from the parsed fields), `events` (each `{type, subject_id, severity, items, callsign, event_id}`; `event_id` is the resulting dispatch's `request_id`), `unparsed` (what was heard but not acted on, and why), `stt` (`whisper`/`cached`/`text`), `stt_ms`, `parse_ms`. The usual `event` / `dispatch` messages follow for each event |

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
