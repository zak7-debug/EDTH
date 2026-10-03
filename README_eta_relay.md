# ETA Relay: Telling Medics When Supplies Arrive

Calculates an estimated time of arrival for each supply request (by drone or ground transit) and sends it back to the requesting medic as a short spoken message in Ukrainian and as a text update on the dashboard. Updates are re-sent when the ETA changes materially.

## Flow

```
SUPPLY_REQUEST -> dispatch assigns drone/truck -> route planned -> ETA computed
      -> ETA message built -> text-to-speech (uk) -> delivered to medic + dashboard
      -> re-evaluated every tick -> re-sent if ETA shifts or route is blocked
```

## Inputs

- Assignment from the dispatch engine: vehicle ID, vehicle type (`drone` | `truck`), pickup point, destination.
- Active zones and blocked edges from the geolocation component (`GET /zones`).
- Live vehicle position from the simulator or telemetry (`/ws`).
- Vehicle speed profiles in `vehicles.yaml`.

## ETA calculation

```
ETA = now + prep_time + travel_time + handover_time
```

| Term | Drone | Truck |
|---|---|---|
| `prep_time` | load and launch, about 2 min | load and depart, about 5 min |
| `travel_time` | path length / cruise speed, routed around no-fly polygons | route cost from road graph with blocked edges removed |
| `handover_time` | land or drop, about 1 min | unload, about 3 min |

Notes:

- Drone paths treat active no-fly polygons as obstacles. Compute the shortest path around them (visibility graph or waypoint detour) rather than straight-line distance.
- Truck routes use edge travel times from the graph. Blocked edges are removed, so a new blockage can change the ETA or make the destination unreachable.
- Add a safety margin of 15 percent to travel time, and give a range rather than a single number: "10 to 14 minutes".
- All numbers above are placeholders for the demo. Replace them with the team's agreed values in `vehicles.yaml`.

## When to re-send

Send an update when any of these happen:

| Trigger | Message |
|---|---|
| Assignment made | "Запит прийнято. Орієнтовний час прибуття: 10-14 хвилин." |
| ETA changes by more than 2 min | "Оновлення: прибуття через 15-19 хвилин." |
| Rerouted around new zone | "Маршрут змінено через нову заборонену зону. Прибуття через 18-22 хвилини." |
| Vehicle within 2 min | "Доставка наближається, 2 хвилини." |
| Delivered | "Доставлено. Підтвердіть отримання." |
| Cannot deliver | "Доставка неможлива. Запит повернуто в чергу." |

Rate-limit updates to one per 60 seconds per request so medics are not flooded.

## Message delivery

1. Build text from templates in `eta_messages.yaml` (Ukrainian primary, English for the dashboard).
2. Convert to speech with a Ukrainian TTS. Options: `piper` with a Ukrainian voice (offline, good for the demo), or a cloud TTS if connectivity is not a concern.
3. Deliver via:
   - `WS /ws` message `{"type": "eta_update", "request_id": "...", "eta_min": 10, "eta_max": 14, "audio_url": "/audio/req_42.wav"}`
   - The dashboard card for that request shows the ETA and a play button.
4. Keep messages under 10 seconds of audio and put the ETA first.

## Data model

```json
{
  "request_id": "req_42",
  "medic_id": "medic_03",
  "vehicle_id": "drone_2",
  "status": "en_route",
  "eta_min_s": 600,
  "eta_max_s": 840,
  "route_version": 3,
  "reason": "assignment",
  "updated_at": "2026-10-03T14:07:00Z"
}
```

Store ETA history per request in TuringDB so the demo can replay how the estimate changed.

## API

```
GET  /requests/{id}/eta       # latest ETA
GET  /requests/{id}/history   # ETA changes over time
POST /requests/{id}/ack       # medic confirms receipt
WS   /ws                      # pushes eta_update events
```

## Edge cases

- Unreachable destination: say so, do not give a fake ETA, and return the request to the dispatch queue.
- Voice query "Скільки до прибуття": look up the latest ETA for that medic's open request and speak it back.
- Multiple open requests for one medic: read each one with its item name.
- Stale telemetry (no position for 30 s): widen the range and mark the ETA as estimated.

## Layout

```
eta/
  calculate.py       # prep + travel + handover, adds margin and range
  routing_bridge.py  # asks routing for path cost with current zones/blockages
  messages.py        # template selection and rate limiting
  tts.py             # Ukrainian text-to-speech
  vehicles.yaml      # speeds and handling times
  eta_messages.yaml  # uk/en templates
tests/
  test_eta.py
```

## Testing

- Straight route: ETA matches distance / speed plus handling times, within rounding.
- Add a no-fly polygon across the path: drone ETA increases and `reason` becomes `rerouted`.
- Block the only truck road: request reports unreachable.
- Change under 2 min: no message sent. Change over 2 min: exactly one message sent.

## Hackathon scope

Must have: ETA on assignment, shown on the dashboard, with at least one update when the route changes.
Nice to have: spoken Ukrainian audio, voice query for ETA, range display.
Do not build: real radio or telephony delivery.
