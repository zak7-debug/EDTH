"""Download the speech-to-text model once, while online, so voice reports work offline.

    pip install -r requirements-voice.txt
    python scripts/fetch_whisper.py            # model "small" (default), or pass "base" for a slow CPU

Then times one transcription of each scripted clip in frontend/audio/ so you know the demo laptop's speed.
"""
import sys
import time
from pathlib import Path

from faster_whisper import WhisperModel

name = sys.argv[1] if len(sys.argv) > 1 else "small"
t = time.perf_counter()
model = WhisperModel(name, device="cpu", compute_type="int8")  # downloads to the Hugging Face cache
print(f"model {name} ready in {time.perf_counter() - t:.1f} s")
for clip in sorted(Path(__file__).resolve().parents[1].joinpath("frontend", "audio").glob("*.*")):
    if clip.suffix in (".txt", ".md"):
        continue
    t = time.perf_counter()
    segments, info = model.transcribe(str(clip), beam_size=1, condition_on_previous_text=False)
    text = " ".join(s.text.strip() for s in segments)
    print(f"{clip.name}: {(time.perf_counter() - t) * 1000:.0f} ms [{info.language}] {text}")
