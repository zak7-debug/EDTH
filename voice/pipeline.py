"""Voice intake from a laptop: record or load a radio call and send it to the running server.

The server does the work (backend/app/voice.py): speech to text with faster-whisper, the Ukrainian /
English parser, then the usual dispatch. This script only captures the audio and posts it, so a report
from here is handled exactly like one from the dashboard's radio page.

Who is speaking: the medic says their callsign at the start of the call («Альфа, медик один. ...»).
If the device knows who is holding it, pass --speaker with the same Ukrainian callsign instead, and
the server reads it as if spoken first. The server maps it to the graph id (ALPHA-MED1 -> med-1).

    python -m voice.pipeline --file call.wav --speaker "Альфа, медик один"
    python -m voice.pipeline --mic --seconds 8 --speaker "Чарлі, медик один"
    python -m voice.pipeline --text "Закінчуються турнікети, потрібно три" --speaker "Альфа, медик один"
    python -m voice.pipeline --clip delta-critical-uk     # a scripted clip (frontend/audio/)

The microphone needs `pip install sounddevice`. Everything else uses the project's requirements.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import wave
from pathlib import Path
from typing import Optional

import httpx

AUDIO_DIR = Path(__file__).resolve().parents[1] / "frontend" / "audio"
SAMPLE_RATE = 16_000  # what Whisper expects


def record_mic(seconds: float) -> bytes:
    """Record from the default microphone and return a 16 kHz mono WAV."""
    try:
        import sounddevice as sd  # optional: only needed for --mic
    except ImportError:
        sys.exit("--mic needs sounddevice: pip install sounddevice (or use --file / --text)")
    print(f"Recording for {seconds:g} s. Speak now...")
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    return buf.getvalue()


def clip_audio(name: str) -> bytes:
    """A scripted clip's audio if it has been recorded, else empty (the server uses its transcript)."""
    for ext in ("wav", "mp3", "webm", "ogg"):
        path = AUDIO_DIR / f"{name}.{ext}"
        if path.exists():
            return path.read_bytes()
    return b""


def send(backend: str, *, audio: Optional[bytes] = None, text: Optional[str] = None, clip: Optional[str] = None,
         speaker: Optional[str] = None, language: Optional[str] = "uk") -> dict:
    """POST to /voice (audio) or /voice/text (transcript) and return the server's report."""
    if text is not None:
        body = {"text": text, "language": language, "speaker": speaker}
        r = httpx.post(f"{backend}/voice/text", json=body, timeout=30)
    else:
        params = {k: v for k, v in {"clip": clip, "speaker": speaker, "language": language}.items() if v}
        r = httpx.post(f"{backend}/voice", params=params, content=audio or b"", timeout=60)  # Whisper on CPU
    if r.status_code != 200:
        sys.exit(f"Server said {r.status_code}: {r.text}")
    return r.json()


def show(report: dict) -> None:
    print(f"Heard ({report['language'] or '?'}, {report['stt']}, {report['stt_ms']} ms): {report['transcript']}")
    print(f"Understood: {report['english'] or '-'}")
    for ev, res in zip(report["events"], report["results"]):
        if res.get("drone_id"):
            what = f"drone {res['drone_id']}, ETA {res['eta_s']:.0f} s, decided in {res['latency_ms']} ms"
        elif ev["type"] == "CASUALTY":
            what = "status set, evacuation started" if res.get("evacuation") else "status set"
        else:
            what = f"no drone yet: {res.get('reason') or 'queued'}"
        print(f"  {ev['type']} {ev['callsign']} ({ev['subject_id']}) -> {what}")
    for u in report["unparsed"]:
        print(f"  Not acted on: {u}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Send a voice report to the EDTH server")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="audio file (wav, mp3, webm, ogg)")
    src.add_argument("--mic", action="store_true", help="record from the microphone")
    src.add_argument("--text", help="a transcript, skipping speech to text")
    src.add_argument("--clip", help="a scripted clip name from frontend/audio/")
    ap.add_argument("--seconds", type=float, default=8, help="recording length for --mic")
    ap.add_argument("--speaker", default=os.environ.get("EDTH_SPEAKER"),
                    help='your callsign as you would say it, e.g. "Альфа, медик один" (or set EDTH_SPEAKER)')
    ap.add_argument("--language", default="uk", help="uk (default) or en; 'auto' lets Whisper detect it")
    ap.add_argument("--backend", default=os.environ.get("EDTH_BACKEND", "http://localhost:8000"))
    args = ap.parse_args()
    language = None if args.language == "auto" else args.language

    if args.file:
        audio = Path(args.file).read_bytes()
    elif args.mic:
        audio = record_mic(args.seconds)
    elif args.clip:
        audio = clip_audio(args.clip)
    else:
        audio = None
    try:
        report = send(args.backend, audio=audio, text=args.text, clip=args.clip, speaker=args.speaker,
                      language=language)
    except httpx.ConnectError:
        sys.exit(f"Can't reach the server at {args.backend}. Start it with ./start.sh first.")
    show(report)


if __name__ == "__main__":
    main()
