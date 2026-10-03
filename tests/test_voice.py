"""Medic voice reports (voice.py): Ukrainian and English transcripts become the usual events."""
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.repo import InMemoryRepo
from backend.app.voice import AUDIO_DIR, parse_report


def _ids():
    return {p.callsign: p.id for p in InMemoryRepo().list_personnel()}


def _clip(name):
    return (AUDIO_DIR / f"{name}.txt").read_text(encoding="utf-8")


def test_ukrainian_casualty_and_low_blood():
    r = parse_report(_clip("badger3-critical-uk"), _ids())
    assert [(e["type"], e["subject_id"]) for e in r.events] == [("CASUALTY", "sol-14"), ("LOW_STOCK", "med-3")]
    assert r.events[0]["severity"] == "CRITICAL" and "items" not in r.events[0]  # bleeding is not a blood request
    assert r.events[1]["items"] == {"blood_oneg": 2}
    assert r.events[1]["urgency"] == "CRITICAL"  # running out while treating a CRITICAL casualty
    assert not r.unparsed and r.parse_ms < 5
    assert r.english == "BADGER 3-2 is CRITICAL. BADGER 3-DOC is running low: needs 2 x blood (O-neg) (critical)."


def test_ukrainian_low_stock_quantities_before_and_after():
    r = parse_report(_clip("badger1-stock-uk"), _ids())
    assert r.events == [{"type": "LOW_STOCK", "subject_id": "med-1", "callsign": "BADGER 1-DOC",
                         "items": {"tourniquet": 3, "hemostatic_gauze": 2}, "urgency": "URGENT"}]


def test_english_wounded():
    r = parse_report(_clip("badger1-wounded-en"), _ids())
    assert r.events == [{"type": "CASUALTY", "subject_id": "sol-04", "severity": "WOUNDED", "callsign": "BADGER 1-4"}]


def test_whisper_style_variants():
    """Whisper writes numbers as digits, drops punctuation and uses other case endings."""
    r = parse_report("Борсук 2 медик борсука 2-5 тяжко поранений", _ids())
    assert [(e["subject_id"], e["severity"]) for e in r.events] == [("sol-11", "CRITICAL")]


def test_unresolved_parts_are_reported_not_dispatched():
    r = parse_report("Борсук дев'ять. Борсук два-три поранений. Борсук два-два на зв'язку.", _ids())
    assert [e["subject_id"] for e in r.events] == ["sol-09"]
    assert r.unparsed == ["BADGER 2-2: no severity heard (critical / wounded)"]
    assert parse_report("Потрібна кров.", _ids()).unparsed[0].startswith("supplies requested but no medic")
    assert parse_report("Перевірка зв'язку.", _ids()).unparsed == ["no casualty, supply request, zone or lost drone heard"]


def test_voice_endpoint_dispatches(monkeypatch):
    monkeypatch.setenv("EDTH_REPO", "memory")
    from backend.app import dev_server
    dev_server.world = dev_server.World()
    with TestClient(dev_server.app) as client:
        with client.websocket_connect("/ws") as ws:
            for _ in range(4):  # snapshot, queue, supply_chain, stock_update
                ws.receive_json()
            # No faster-whisper in CI: the scripted clip's transcript stands in for speech to text.
            r = client.post("/voice?clip=badger3-critical-uk", content=b"not really audio")
            assert r.status_code == 200
            body = r.json()
            assert body["stt"] in ("cached", "whisper")
            assert [e["type"] for e in body["events"]] == ["CASUALTY", "LOW_STOCK"]
            assert all(res.get("request_id") for res in body["results"])
            first = ws.receive_json()
            assert first["type"] == "voice_report" and first["data"]["english"].startswith("BADGER 3-2")
            assert ws.receive_json()["type"] == "event"
        assert client.post("/voice/text", json={"text": "Badger one-two critical"}).json()["events"][0]["subject_id"] == "sol-02"
        assert client.post("/voice?clip=missing", content=b"").status_code == 503


def test_speaker_callsign_in_ukrainian(monkeypatch):
    """voice/pipeline.py --speaker: the device's own callsign stands in for the one the medic didn't say."""
    monkeypatch.setenv("EDTH_REPO", "memory")
    from backend.app import dev_server
    dev_server.world = dev_server.World()
    with TestClient(dev_server.app) as client:
        body = {"text": "Закінчуються турнікети, потрібно три.", "speaker": "Борсук один, медик"}
        r = client.post("/voice/text", json=body).json()
        assert [(e["type"], e["subject_id"]) for e in r["events"]] == [("LOW_STOCK", "med-1")]
        assert r["transcript"] == body["text"]  # shown as heard; the speaker isn't added to it
        assert client.post("/voice/text", json={"text": body["text"]}).json()["events"] == []


def test_ukrainian_threat_clip_is_a_no_fly_zone():
    r = parse_report(_clip("badger2-threat-uk"), _ids())
    assert [(e["type"], e["subject_id"]) for e in r.events] == [("NO_FLY_ZONE", "med-2")]
    e = r.events[0]
    assert (e["distance_m"], e["bearing_deg"], e["radius_m"]) == (800, 45, 500)


def test_asking_for_a_drone_is_not_a_threat():
    r = parse_report("Борсук один, медик. Надішліть дрон, потрібно два турнікети.", _ids())
    assert [e["type"] for e in r.events] == ["LOW_STOCK"] and r.unparsed == []


def test_truck_driver_reports_from_device_position(monkeypatch):
    """A driver isn't in the graph: /voice/text with lat/lon places their report there."""
    monkeypatch.setenv("EDTH_REPO", "memory")
    from backend.app import dev_server
    dev_server.world = dev_server.World()
    with TestClient(dev_server.app) as client:
        r = client.post("/voice/text", json={"text": "Водій. Дорога заблокована, вирва.", "lat": 47.672, "lon": 35.585}).json()
        assert [e["type"] for e in r["events"]] == ["ROAD_BLOCKED"] and r["unparsed"] == []
        assert r["english"] == "DRIVER reports road blocked at their position."
        assert r["results"][0]["zone"]["properties"]["kind"] == "ROAD_BLOCKED"
        r = client.post("/voice/text", json={"text": "Дорога заблокована."}).json()
        assert r["events"] == [] and "no callsign" in r["unparsed"][0]


def test_decode_audio_without_faster_whispers_decoder():
    """Mic and WAV audio decode to 16 kHz mono ourselves (some PyAV builds reject faster-whisper's call)."""
    import io, wave
    import pytest
    np = pytest.importorskip("numpy")  # comes with requirements-voice.txt
    from backend.app.voice import decode_audio
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(np.zeros(44100 * 2, dtype="<i2").tobytes())
    pcm = decode_audio(buf.getvalue())
    assert pcm.dtype == np.float32 and len(pcm) == 16000


def test_restock_urgency_and_casualty_supplies_go_to_the_medic():
    def urgency(text):
        return [e["urgency"] for e in parse_report(text, _ids()).events if e["type"] == "LOW_STOCK"]
    assert urgency("Борсук два, медик. Потрібен один турнікет, не терміново.") == ["NON_URGENT"]
    assert urgency("Badger two medic. Need two chest seals urgently.") == ["URGENT"]
    assert urgency("Борсук два, медик. Потрібна кров.") == ["NON_URGENT"]  # the default
    r = parse_report("Борсук один-два важкий, потрібна кров дві одиниці.", _ids())
    assert [(e["type"], e["subject_id"]) for e in r.events] == [("CASUALTY", "sol-02"), ("LOW_STOCK", "med-1")]
    assert r.events[1]["urgency"] == "CRITICAL" and "items" not in r.events[0]


def test_pilot_reports(monkeypatch):
    """Drone pilots: a lost drone, and a threat placed from the drone; plus the medic's ETA question."""
    monkeypatch.setenv("EDTH_REPO", "memory")
    from backend.app import dev_server
    dev_server.world = dev_server.World()
    with TestClient(dev_server.app) as client:
        lost = client.post("/voice?clip=hawk3-lost-uk", content=b"").json()
        assert [(e["type"], e["drone_id"]) for e in lost["events"]] == [("DRONE_LOST", "drn-03")]
        assert lost["readback"]["uk"].startswith("Прийнято, Яструб 3 списано")
        zone = client.post("/voice?clip=falcon2-threat-uk", content=b"").json()
        [ev] = zone["events"]
        assert ev["type"] == "NO_FLY_ZONE" and ev["source"]["user_id"] == "FALCON 2"
        assert zone["results"][0]["zone"]["properties"]["kind"] == "NO_FLY_ZONE"

        asked = client.post("/voice?clip=badger1-eta-uk", content=b"").json()
        assert asked["results"][0]["status"] == "NONE" and asked["readback"]["uk"] == "Відкритих запитів немає."
        sent = client.post("/voice/text", json={"text": "Закінчуються турнікети, потрібно три.",
                                                 "speaker": "Борсук один, медик"}).json()
        drone = sent["results"][0]["drone_id"]
        asked = client.post("/voice?clip=badger1-eta-uk", content=b"").json()
        assert asked["results"][0]["drone_id"] == drone and asked["results"][0]["eta_s"] > 0
        assert "прибуде приблизно через" in asked["readback"]["uk"]


def test_free_speech_as_whisper_writes_it():
    ids = _ids()
    unhurt = sorted(cs for cs in ids if cs.startswith("BADGER") and not cs.endswith("-DOC"))
    # no soldier named: the next unhurt soldiers of the speaker's squad, and the summary says so
    r = parse_report("Борсук-3, медик. Двоє... Два поранені, важкий стан.", ids, unhurt=unhurt)
    assert [e["callsign"] for e in r.events] == ["BADGER 3-1", "BADGER 3-2"]
    assert "soldier not named" in r.english
    assert parse_report("Один поранений, критичний.", ids).unparsed[0].startswith("casualty heard but not which")
    # digits run together, Russian spellings and plain English kit names
    assert parse_report("Борсук 32 важкий", ids).events[0]["callsign"] == "BADGER 3-2"
    assert parse_report("Барсук три-два, тяжелый.", ids).events[0]["severity"] == "CRITICAL"
    ev = parse_report("Badger one, medic. I need more bandages.", ids).events[0]
    assert ev["subject_id"] == "med-1" and ev["items"] == {"hemostatic_gauze": 1}


def test_transcribe_redoes_other_languages_as_ukrainian(monkeypatch):
    from types import SimpleNamespace

    from backend.app import voice
    calls = []

    class Model:
        def transcribe(self, pcm, language=None, initial_prompt=None, **_):
            calls.append(language)
            info = SimpleNamespace(language=language or "ru", all_language_probs=[("ru", .6), ("uk", .3), ("en", .1)])
            return iter([SimpleNamespace(text=" Борсук один ")]), info

    monkeypatch.setattr(voice, "_whisper", lambda: Model())
    monkeypatch.setattr(voice, "decode_audio", lambda audio: audio)
    assert voice.transcribe(b"x") == ("Борсук один", "uk") and calls == [None, "uk"]
    calls.clear()
    assert voice.transcribe(b"x", "en")[1] == "en" and calls == ["en"]


def test_squad_named_while_another_medic_speaks():
    ids = _ids()
    unhurt = sorted(cs for cs in ids if cs.startswith("BADGER") and not cs.endswith("-DOC"))
    text = "Борсук три, медик. Борсук два, поранений критичний. Сильна кровотеча з ноги. Потрібен турнікет і кров."
    r = parse_report(text, ids, unhurt=unhurt)
    assert [(e["type"], e["callsign"]) for e in r.events] == [("CASUALTY", "BADGER 2-1"), ("LOW_STOCK", "BADGER 2-DOC")]
    assert r.events[1]["items"] == {"tourniquet": 1, "blood_oneg": 1} and r.events[1]["urgency"] == "CRITICAL"
    assert parse_report("Сокiл два, пілот. Ворожий дрон, вісімсот метрів на північ.", ids,
                        drones={"FALCON 2": ("drn-04", (47.6, 35.6))}).events[0]["type"] == "NO_FLY_ZONE"


def test_coordinator_test_sentences_with_another_speaker_selected():
    ids = _ids()
    r = parse_report("Борсук три, медик. Борсук один, терміново потрібно поповнення: дві одиниці крові нульова "
                     "негативна і три турнікети.", ids)
    assert r.events[0]["subject_id"] == "med-1"  # the medic named in the call, not the one selected
    r = parse_report("Сокіл 1 збитий.", ids, lost_drones=frozenset({"FALCON 1"}))
    assert r.unparsed == ["FALCON 1 was already written off"]
