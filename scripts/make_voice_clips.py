"""Generate the scripted radio clips in frontend/audio/ from their .txt lines with a TTS voice.

    pip install edge-tts        # needs internet once; the .mp3 files are then committed
    python scripts/make_voice_clips.py

Every line is invented (see frontend/audio/README.md). A teammate reading the .txt aloud works just as
well: save it as frontend/audio/<name>.mp3 (or .wav) next to the .txt.
"""
import asyncio
from pathlib import Path

import edge_tts

AUDIO = Path(__file__).resolve().parents[1] / "frontend" / "audio"
VOICES = {"uk": "uk-UA-OstapNeural", "en": "en-GB-RyanNeural"}  # TUNE: uk-UA-PolinaNeural for a female voice


async def main():
    for txt in sorted(AUDIO.glob("*.txt")):
        voice = VOICES["en" if txt.stem.endswith("-en") else "uk"]
        out = txt.with_suffix(".mp3")
        await edge_tts.Communicate(txt.read_text(encoding="utf-8").strip(), voice, rate="+5%").save(str(out))
        print(f"{out.name} ({voice})")


asyncio.run(main())
