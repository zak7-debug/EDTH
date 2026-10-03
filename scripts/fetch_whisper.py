"""Download the speech-to-text model once, while online, so voice reports work offline.

    pip install -r requirements-voice.txt
    python scripts/fetch_whisper.py            # model "small" (default), or pass "base" for a slow CPU

Then times one transcription of each scripted clip in frontend/audio/ so you know the demo laptop's speed.
"""
import sys
import time
from pathlib import Path

from faster_whisper import WhisperModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.voice import decode_audio  # the server's own decoder (avoids the PyAV metadata_errors clash)

AUDIO = (".wav", ".mp3", ".webm", ".ogg", ".m4a")

name = sys.argv[1] if len(sys.argv) > 1 else "small"
t = time.perf_counter()
model = WhisperModel(name, device="cpu", compute_type="int8")  # downloads to the Hugging Face cache
print(f"model {name} ready in {time.perf_counter() - t:.1f} s")
clips = [c for c in sorted(ROOT.joinpath("frontend", "audio").iterdir()) if c.suffix in AUDIO]
if not clips:
    print("No recorded clips in frontend/audio/ yet: the scripted calls use their .txt transcripts. Model is ready.")
for clip in clips:
    t = time.perf_counter()
    segments, info = model.transcribe(decode_audio(clip.read_bytes()), beam_size=1, condition_on_previous_text=False)
    text = " ".join(s.text.strip() for s in segments)
    print(f"{clip.name}: {(time.perf_counter() - t) * 1000:.0f} ms [{info.language}] {text}")
