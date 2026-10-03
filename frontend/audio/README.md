# Scripted radio clips for the voice demo

Each clip is `<name>.wav` (or .mp3/.webm) plus `<name>.txt`, the line as scripted. `POST /voice?clip=<name>`
falls back to the `.txt` when speech to text is unavailable, so the demo never stalls on it.

All lines are invented: they use the seed's fictional callsigns (BADGER 1 to 3). Record them by reading the
`.txt` aloud, or generate them with a Ukrainian TTS voice. Never use real field or intercepted recordings.

| Clip | Says | Becomes |
| --- | --- | --- |
| `badger3-critical-uk` | Badger 3 medic: Badger 3-2 critical, massive bleeding from the leg; I'm running out of blood, need two units | CASUALTY sol-14 CRITICAL + LOW_STOCK med-3 blood_oneg x2, CRITICAL |
| `badger1-stock-uk` | Badger 1 medic: running out of tourniquets, need three, and two haemostatic gauze | LOW_STOCK med-1 tourniquet x3, hemostatic_gauze x2, URGENT |
| `badger1-wounded-en` | (English) Badger 1 medic: Badger 1-4 wounded, shrapnel to the arm | CASUALTY sol-04 WOUNDED |
| `hawk2-threat-uk` | Hawk 2, pilot: I see air defence, 800 metres north | THREAT 800 m north of HAWK 2, radius 800 m |
| `driver-road-uk` | Driver, truck 3: road blocked, mines, 200 metres east | THREAT (road closure) 200 m east of the driver's device position, radius 300 m |

## Try it

    git checkout claude/voice-reports-vjfqv0
    ./start.sh        # add EDTH_REPO=memory to skip TuringDB

Open the dashboard at http://localhost:8000 and the test page at http://localhost:8000/audio/radio.html.
Clip buttons and typed text work straight away. For real speech to text (clips with audio, or the
microphone), first run `.venv/bin/pip install -r requirements-voice.txt` and `.venv/bin/python scripts/fetch_whisper.py`.
