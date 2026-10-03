import sys
import argparse
import httpx
from datetime import datetime, timezone
from voice.transcribe import transcribe_audio
from voice.extract import extract_event

def adapt_to_backend_contract(event_payload: dict) -> dict:
    """Translates generic voice schemas into backend-accepted event structures."""
    event_type = event_payload.get("type")
    user_id = event_payload.get("source", {}).get("user_id", "medic-1")
    
    subject_id = user_id if user_id else "medic-1"

    if event_type == "SUPPLY_REQUEST":
        details = event_payload.get("details", {})
        return {
            "type": "LOW_STOCK",
            "subject_id": subject_id,
            "item": details.get("item", "tourniquet"),
            "quantity": details.get("quantity", 1),
            "source": "voice_intake"
        }

    return {
        "type": event_type,
        "subject_id": subject_id,
        "details": event_payload.get("details", {}),
        "transcript": event_payload.get("transcript_uk"),
        "needs_confirmation": event_payload.get("needs_confirmation", False)
    }

def process_voice_report(audio_path: str, user_id: str = "medic-1", backend_url: str = None) -> dict:
    """End-to-end pipeline: Audio -> Text -> Structured JSON Schema -> Backend POST."""
    print(f"[1/3] Transcribing audio file: {audio_path}...")
    transcript = transcribe_audio(audio_path)
    print(f"      Transcript: '{transcript}'")

    asr_conf = 0.90 if transcript.strip() else 0.30

    print("[2/3] Extracting structured event using rule engine + LLM fallback...")
    event_payload = extract_event(transcript, asr_confidence=asr_conf)
    event_payload["source"]["user_id"] = user_id
    print(f"      Extracted Voice Payload: {event_payload}")

    backend_payload = adapt_to_backend_contract(event_payload)

    if backend_url:
        print(f"[3/3] Sending adapted payload to backend ({backend_url}/events)...")
        try:
            response = httpx.post(f"{backend_url}/events", json=backend_payload, timeout=5.0)
            print(f"      Backend Response Status: {response.status_code}")
            print(f"      Backend Output: {response.text}")
        except Exception as e:
            print(f"      [!] Could not connect to backend: {e}")

    return backend_payload

def record_live_mic(duration_sec: int = 5, output_wav: str = "mic_input.wav") -> str:
    """Captures live audio from default microphone using sounddevice."""
    try:
        import sounddevice as sd
        import wave

        sample_rate = 16000
        print(f"[*] Recording from microphone for {duration_sec} seconds... Speak now!")
        audio_data = sd.rec(int(duration_sec * sample_rate), samplerate=sample_rate, channels=1, dtype='int16')
        sd.wait()
        print("[*] Recording finished.")

        with wave.open(output_wav, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(audio_data.tobytes())

        return output_wav
    except Exception as e:
        print(f"[!] Mic Error: {e}")
        print("[!] Falling back to 'test_sample.wav'...")
        return "test_sample.wav"

def main():
    parser = argparse.ArgumentParser(description="Voice Intake Pipeline for Field Reports")
    parser.add_argument("--file", type=str, help="Path to input audio file (.wav)")
    parser.add_argument("--user", type=str, default="medic-1", help="Operator or Medic Graph ID")
    parser.add_argument("--mic", action="store_true", help="Capture live microphone input")
    parser.add_argument("--backend", type=str, default=None, help="Backend URL (e.g. http://localhost:8000)")

    args = parser.parse_args()

    audio_file = args.file

    if args.mic:
        audio_file = record_live_mic(duration_sec=5)
    elif not audio_file:
        print("Error: Please provide --file <path> or use --mic flag.")
        sys.exit(1)

    process_voice_report(audio_file, user_id=args.user, backend_url=args.backend)

if __name__ == "__main__":
    main()