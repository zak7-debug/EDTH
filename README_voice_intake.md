# Voice Intake: Ukrainian Speech to Map Updates

Turns short Ukrainian radio/phone voice reports from truck drivers, medics and soldiers into structured events that update the shared map (no-go areas, no-fly zones, road blockages). Also covers the simple voice-command interface medics use to report status and request supplies.

## What it does

1. Captures audio (push-to-talk clip, uploaded file, or live stream chunk).
2. Detects speech and confirms language (Ukrainian, with Russian fallback since many speakers code-switch).
3. Transcribes to text.
4. Extracts a structured event (type, location phrase, distance, bearing, confidence).
5. Posts the event to the backend, which writes it to TuringDB and pushes it to the dashboard.

```
audio -> VAD -> language ID -> ASR -> intent + entity extraction -> POST /events -> TuringDB -> /ws -> map
```

## Components

| Stage | Suggested tool | Notes |
|---|---|---|
| Voice activity detection | `silero-vad` | Drops silence and radio static before ASR |
| Language ID + ASR | `faster-whisper` (model `small` or `medium`, `language="uk"`) | Runs on CPU for the demo; use `large-v3` if a GPU is available |
| Noise reduction (optional) | `noisereduce` or `rnnoise` | Helps with engine and wind noise |
| Extraction | Rules plus an LLM call returning strict JSON | Rules handle the fixed command vocabulary; the LLM handles free speech |
| Transport | `POST /events` on the existing FastAPI backend | Same contract the simulator already uses |

## Event types

| Type | Example (Ukrainian) | Meaning |
|---|---|---|
| `ROAD_BLOCKED` | "Дорога заблокована біля мосту" | Road segment closed |
| `NO_GO_AREA` | "Не заїжджати, мінне поле" | Ground no-go polygon |
| `NO_FLY_ZONE` | "Ворожий дрон-перехоплювач, повітря закрите" | Air no-go zone (see geolocation README) |
| `SUPPLY_REQUEST` | "Потрібні турнікети та кров, двоє важких" | Medic request, goes to dispatch |
| `ALL_CLEAR` | "Дорога вільна" | Clears a previous blockage |

Add new types in `intents.yaml` rather than in code.

## Event schema

```json
{
  "id": "evt_0001",
  "type": "ROAD_BLOCKED",
  "source": {"user_id": "driver_12", "role": "truck_driver", "lat": 48.512, "lon": 37.994},
  "transcript_uk": "Дорога заблокована за двісті метрів на північ",
  "transcript_en": "Road blocked two hundred metres to the north",
  "distance_m": 200,
  "bearing_deg": 0,
  "location_text": null,
  "language": "uk",
  "asr_confidence": 0.87,
  "extract_confidence": 0.81,
  "needs_confirmation": false,
  "received_at": "2026-10-03T14:05:00Z"
}
```

## Handling uncertainty

Wrong map data can get people hurt, so the pipeline is conservative:

- If either confidence is below the threshold (default 0.7), set `needs_confirmation: true`. The dashboard shows the event as "unconfirmed" and the speaker gets a read-back prompt.
- Always keep the original audio clip and transcript so an operator can verify.
- Events expire (default 60 min for blockages, 15 min for no-fly zones) unless re-confirmed.
- A second independent report within 200 m upgrades an event to "confirmed".

## Medic voice commands

Keep the command set small so recognition is reliable. The extractor matches these first and only falls back to the LLM for free speech.

| Intent | Spoken pattern |
|---|---|
| Request supplies | "Запит: [item] [quantity], [priority]" |
| Confirm receipt | "Отримано" |
| Cancel | "Скасувати" |
| Report my location | "Моя позиція" (uses device GPS or spoken grid) |
| Repeat ETA | "Скільки до прибуття" |

Item keywords map to supply catalogue IDs (tourniquet, blood, bandage, etc.) in `supplies.yaml`. Priority words: "терміново" (urgent), "звичайний" (routine).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install faster-whisper silero-vad torch sounddevice fastapi httpx pyyaml
python voice/transcribe.py samples/driver_blockage_uk.wav
```

## Run

```bash
# One clip
python -m voice.pipeline --file call.wav --speaker "Борсук один, медик"

# Live microphone (push-to-talk)
python -m voice.pipeline --mic --speaker "Борсук три, медик"   # needs: pip install sounddevice
```

## Layout

```
voice/
  pipeline.py        # records or loads a call and posts it to /voice; the server transcribes and parses
  (speech to text and parsing live in backend/app/voice.py, behind POST /voice)
  intents.yaml       # event types and trigger phrases
  supplies.yaml      # item keywords -> catalogue IDs
samples/             # test clips with expected JSON
tests/
```

## Testing

Record or synthesise 10 to 15 short Ukrainian clips, one per event type plus noisy variants. For each, store the expected event JSON and check `type`, `distance_m` and `bearing_deg`. Ukrainian number words ("двісті", "п'ятсот") and compass words ("північ", "схід") are the most common failure points, so cover them explicitly.

## Hackathon scope

Must have: file-based clip to transcript to `POST /events` for three event types.
Nice to have: live mic, read-back prompt, unconfirmed state on the map.
Do not build: speaker identification, encryption, or anything touching real field networks.
