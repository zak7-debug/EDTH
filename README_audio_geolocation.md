# Audio Geolocation: Target Coordinates and No-Fly Zone Perimeters

Converts a spoken report ("drone threat 800 metres north-east of me") into map coordinates: a target point for drone or transit dispatch, and a perimeter polygon for a no-fly zone centred on that point.

**Status: built** in `backend/app/geo/`. Try it: `curl -X POST localhost:8000/events -H "content-type: application/json" -d '{"type": "NO_FLY_ZONE", "subject_id": "med-2", "distance_m": 800, "bearing_deg": 45}'`, or the `badger2-threat-uk` clip on `/audio/radio.html`. Where the build differs from the first draft of this plan, the text below says so.

## Inputs

From the voice intake pipeline (see `README_voice_intake.md`):

- Reporter position `(lat, lon)` from device GPS, or a spoken grid reference as fallback.
- `distance_m`: spoken distance from the reporter.
- `bearing_deg`: spoken direction, true north = 0, clockwise.
- Optional `radius_m`: spoken zone radius ("закрити п'ятсот метрів").
- Event `type` (`NO_FLY_ZONE`, `ROAD_BLOCKED`, `DESTINATION`, ...).

## Step 1: Parse distance and bearing

| Spoken (Ukrainian) | Value |
|---|---|
| північ / північний схід / схід / південний схід | 0 / 45 / 90 / 135 |
| південь / південний захід / захід / північний захід | 180 / 225 / 270 / 315 |
| "на дев'яносто градусів", "азимут 270" | numeric bearing |
| "годинник": "на третій годині" | clock face relative to heading: 3 o'clock = +90 from facing |
| "двісті метрів", "два кілометри" | 200, 2000 |

Rules:

- Compass words and numeric azimuth are absolute. Clock positions are relative to the reporter's heading, so they need a heading from the device. If there is none, mark the event `needs_confirmation`.
- Normalise everything to metres and degrees true.
- If distance is missing, use a default (for example 500 m) and flag it as an assumption in the event.
- Inflected forms count: "на півночі", "північніше", "на північному сході", "із заходу", "двохсот метрів", "півтора кілометра", "півкілометра". A pause (comma) ends a number, so "азимут двісті сімдесят, два кілометри" is 270 and 2 km.
- Device headings arrive magnetic unless `heading_ref` is `"true"`; `magnetic_declination_deg` in `zones.yaml` (8 in the sector) converts them.

## Step 2: Project the centre point

Using a destination-point calculation on a sphere (adequate for distances under 10 km):

```python
from math import radians, degrees, sin, cos, asin, atan2

R = 6_371_000  # metres

def project(lat, lon, distance_m, bearing_deg):
    lat1, lon1, brg = radians(lat), radians(lon), radians(bearing_deg)
    d = distance_m / R
    lat2 = asin(sin(lat1) * cos(d) + cos(lat1) * sin(d) * cos(brg))
    lon2 = lon1 + atan2(sin(brg) * sin(d) * cos(lat1),
                        cos(d) - sin(lat1) * sin(lat2))
    return degrees(lat2), degrees(lon2)
```

This point is the zone centre, or the destination for a dispatch request.

## Step 3: Build the perimeter

Generate a regular polygon around the centre by projecting N points at equal bearings. As built, the corners sit at `radius / cos(pi / n)` so the flat sides, not just the corners, are at least the radius out, and `n` is 16 (`zones.yaml`): routing tests every corner, and 32 corners made a route about 5x slower (51 ms vs 10 ms with five extra zones).

```python
def circle_polygon(lat, lon, radius_m, n=32):
    return [project(lat, lon, radius_m, 360 * i / n) for i in range(n)]
```

Output as GeoJSON on `GET /zones` and the WebSocket (note: GeoJSON is `[lon, lat]` order, and the ring must close). Inside the backend zones stay `NoFlyZone` with unclosed `[lat, lon]` polygons, like the rest of the graph:

```json
{
  "type": "Feature",
  "properties": {"kind": "NO_FLY_ZONE", "radius_m": 500, "expires_at": "2026-10-03T14:20:00Z"},
  "geometry": {"type": "Polygon", "coordinates": [[[37.99, 48.51], "...", [37.99, 48.51]]]}
}
```

Default radii (override per event type in `zones.yaml`):

| Type | Default radius |
|---|---|
| `NO_FLY_ZONE` | 500 m |
| `NO_GO_AREA` | 200 m |
| `ROAD_BLOCKED` | 50 m (snap to nearest road segment instead of using a circle) |

## Step 4: Snap and validate

- Road blockages: snap the centre to the nearest open segment of the road network (`roads.py`) and close that segment for evacuations and truck legs; drones are unaffected. No-go areas close roads the same way. Both go through `roads.set_ground_constraints()`.
- Destinations: snap to the nearest valid drop point or road node.
- Sanity checks: reject centres more than 20 km from the reporter, or outside the operating area bounding box; reject non-finite values.
- Overlapping reports within 200 m merge into one zone, extending its expiry. Distance is measured from the zone's first report, so a chain of reports can't walk a zone across the map; the merged zone covers every report's circle, and a second reporter makes it `confirmed`.

## Accuracy and honesty about error

Spoken distance and bearing are rough estimates, often off by 20 to 30 percent in distance and 15 degrees or more in bearing. Handle this in the data, not just the UI:

- Store an uncertainty radius: `uncertainty_m = 0.35 * distance_m` plus GPS error (`source.accuracy_m`, default 10 m). The first draft said 0.25, but a 15 degree bearing error alone moves the point sideways by about 0.26 x distance.
- For no-fly zones, expand the radius by the uncertainty so the zone errs on the side of safety.
- Show the dashboard centre with a dashed uncertainty ring.
- Treat a single report as `unconfirmed` until corroborated.

## API

```
POST /events           # from voice pipeline: distance_m + bearing_deg (+ radius_m), or text to parse;
                       # reporter = source {lat, lon, accuracy_m, heading_deg, heading_ref, user_id} or subject_id
GET  /zones            # active zones as GeoJSON FeatureCollection
GET  /zones/{id}
DELETE /zones/{id}     # operator override
WS   /ws               # pushes zone_created / zone_expired / zone_updated
```

Dispatch consumes `GET /zones` and treats active no-fly polygons as hard constraints when planning drone paths, and blocked edges as removed from the road graph.

## Layout

```
backend/app/geo/
  project.py       # destination point, bearing/distance math
  zones.py         # polygon generation, merge, expiry
  snap.py          # snap to road graph / drop points
  parse_spatial.py # Ukrainian compass, numeric and clock-face parsing
  service.py       # event in -> zone / destination out, sanity checks
  api.py           # POST /events hook, /zones, WebSocket messages, expiry
  zones.yaml       # default radii, expiries, uncertainty, operating area
tests/
  test_project.py  # known-answer checks
  test_parse_spatial.py, test_geo_zones.py, test_geo_api.py
```

Voice: `voice.py` turns a sentence with a threat word plus a direction into one of these events, from the speaker's position. A bare "дрон" doesn't count (medics say it when asking for a delivery); it needs a hostile or closure word ("ворожий", "закрити").

## Testing

- Known answers: from `(51.5, 0.0)`, 1000 m at bearing 90 lands about 0.014447 degrees east (1000 / (R cos 51.5)); assert within 2 m. (The first draft said 0.0143, about 10 m off.) Compare against the spherical formula: a WGS84 tool differs by about 3 m per km here.
- Round trip: project out, then compute distance and bearing back, and assert equality within tolerance.
- Polygon has n corners (the GeoJSON ring n + 1, closed), every corner is within 1 m of `radius / cos(pi / n)`, and every edge midpoint is at least the radius out.
- Parser: every compass word and a handful of spoken numbers.

## Hackathon scope

Must have: compass word plus metres to centre point to circular GeoJSON zone shown on the map and respected by dispatch.
Nice to have: uncertainty ring, merge of overlapping reports, road snapping.
Do not build: triangulation from multiple microphones or real acoustic localisation. Everything here is based on what the speaker says, not on sound direction.
