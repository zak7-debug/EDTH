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
