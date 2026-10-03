# Arnav (D): map dashboard and pitch deck

Arnav owns what the judges see: one map page that tells the whole story at a glance, readable on a projector.

### Arrival to +1 hour: Map shell

- [ ] `frontend/index.html` with Leaflet from a CDN and plain JavaScript, no build step
- [ ] Map centred on Grafenwöhr training area (about 49.70 N, 11.93 E), zoom 13
- [ ] `frontend/mock/snapshot.json` matching the `snapshot` message in the contracts
- [ ] Done when: the page opens from disk and shows the mock data

### Until 14:00: Layers

- [ ] Soldiers coloured by status: OK green, WOUNDED amber, CRITICAL red
- [ ] Medics with a distinct icon and a popup listing stock, low items highlighted
- [ ] Drones by status, depots, no-fly zones as red polygons
- [ ] Connect to `/ws` and render `snapshot`; fall back to the mock file if the socket is down
- [ ] Done when: checkpoint 1 shows the live snapshot from Sasank's API

### 14:00 to 18:00: Live dispatch view

- [ ] On `dispatch`: draw the route, show an ETA badge on the drone that counts down every second and resyncs on each `drone_update`
- [ ] Move drones on `drone_update`; on `delivered`, flash the recipient and clear the route
- [ ] Event log panel, newest first, with event-to-dispatch ms on each line
- [ ] Latency counter in the header: last dispatch and rolling average in ms
- [ ] Trigger panel: pick a soldier and set CRITICAL, or pick a medic and an item to report low stock; both `POST /events`
- [ ] Done when: checkpoint 2, a trigger click ends with a drone flying and its ETA counting down

### 18:00 to 22:00: Edge cases and projector polish

- [ ] `no_dispatch`: a clear banner with the reason and the suggested alternative
- [ ] `queue`: a small panel listing waiting requests in triage order
- [ ] Rerouted route visibly bending around the no-fly zone
- [ ] "Run demo scenario" button calling `POST /scenario/demo`
- [ ] Dark theme, large fonts, test at 1920 by 1080

### Sunday

- [ ] Visual polish, pitch deck with Zak, and a backup screen recording of the best rehearsal
