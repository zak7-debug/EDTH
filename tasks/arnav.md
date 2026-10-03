# Arnav (D): map dashboard and pitch deck

> Status 2026-10-03: the dashboard is built (frontend/index.html, see docs/foundations-guide.md section 11). Left for Arnav: projector check at 1920 by 1080 and polish, the pitch deck, the backup recording.

Arnav owns what the judges see: one map page that tells the whole story at a glance, readable on a projector.

### Arrival to +1 hour: Map shell

- [x] `frontend/index.html` with Leaflet (vendored) and plain JavaScript, no build step
- [x] Map centred on the demo sector (about 47.66 N, 35.60 E), zoom 12
- [x] `frontend/mock/snapshot.json` matching the `snapshot` message in the contracts
- [x] Done when: the page opens from disk and shows the mock data

### Until 14:00: Layers

- [x] Soldiers coloured by status: OK green, WOUNDED amber, CRITICAL red
- [x] Medics with a distinct icon and a popup listing stock, low items highlighted
- [x] Drones by status, depots, no-fly zones as red polygons
- [x] Connect to `/ws` and render `snapshot`; fall back to the mock file if the socket is down
- [x] Done when: checkpoint 1 shows the live snapshot from Sasank's API

### 14:00 to 18:00: Live dispatch view

- [x] On `dispatch`: draw the route, show an ETA badge on the drone that counts down every second and resyncs on each `drone_update`
- [x] Move drones on `drone_update`; on `delivered`, flash the recipient and clear the route
- [x] Event log panel, newest first, with event-to-dispatch ms on each line
- [x] Latency counter in the header: last dispatch and rolling average in ms
- [x] Trigger panel: pick a soldier and set CRITICAL, or pick a medic and an item to report low stock; both `POST /events`
- [x] Done when: checkpoint 2, a trigger click ends with a drone flying and its ETA counting down

### 18:00 to 22:00: Edge cases and projector polish

- [x] `no_dispatch`: a clear banner with the reason and the suggested alternative
- [x] `queue`: a small panel listing waiting requests in triage order
- [x] Rerouted route visibly bending around the no-fly zone
- [x] "Run demo scenario" button calling `POST /scenario/demo`
- [ ] Dark theme, large fonts, test at 1920 by 1080

### Sunday

- [ ] Visual polish, pitch deck with Zak, and a backup screen recording of the best rehearsal
