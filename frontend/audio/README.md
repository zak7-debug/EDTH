# Scripted radio clips for the voice demo

Each clip is `<name>.wav` (or .mp3/.webm) plus `<name>.txt`, the line as scripted. `POST /voice?clip=<name>`
falls back to the `.txt` when speech to text is unavailable, so the demo never stalls on it.

All lines are invented: they use the seed's fictional callsigns (BADGER 1 to 3). Record them by reading the
`.txt` aloud, or generate them with a Ukrainian TTS voice. Never use real field or intercepted recordings.

| Clip | Says | Becomes |
| --- | --- | --- |
| `badger3-critical-uk` | Badger 3 medic: Badger 3-2 critical, massive bleeding from the leg; I'm running out of blood, need two units | CASUALTY sol-14 CRITICAL + LOW_STOCK med-3 blood_oneg x2 |
| `badger1-stock-uk` | Badger 1 medic: running out of tourniquets, need three, and two haemostatic gauze | LOW_STOCK med-1 tourniquet x3, hemostatic_gauze x2 |
| `badger1-wounded-en` | (English) Badger 1 medic: Badger 1-4 wounded, shrapnel to the arm | CASUALTY sol-04 WOUNDED |
| `badger2-threat-uk` | Badger 2 medic: enemy drone 800 m north-east, close 500 m | NO_FLY_ZONE from med-2, 800 m at 45 degrees, radius 500 m (+ uncertainty) |

## Try it

    git checkout claude/voice-reports-vjfqv0
    ./start.sh        # add EDTH_REPO=memory to skip TuringDB

Open the dashboard at http://localhost:8000 and the test page at http://localhost:8000/audio/radio.html.
Clip buttons and typed text work straight away. For real speech to text (clips with audio, or the
microphone), first run `.venv/bin/pip install -r requirements-voice.txt` and `.venv/bin/python scripts/fetch_whisper.py`.

Added for drone pilots and the ETA read-back (all fictional, seed callsigns):

| Clip | Says | Becomes |
| --- | --- | --- |
| `badger1-eta-uk` | Badger 1 medic: how long until it arrives? | ETA_QUERY: the radio answers with the live ETA of the drone flying to med-1 |
| `falcon2-threat-uk` | Falcon 2, pilot: enemy drone, 800 metres north | NO_FLY_ZONE placed from FALCON 2's position |
| `hawk3-lost-uk` | Hawk 3, pilot: Hawk 3 shot down | DRONE_LOST drn-03 |
| `driver-road-uk` | Driver: road blocked, crater, 200 metres east | ROAD_BLOCKED from the driver's device position |

