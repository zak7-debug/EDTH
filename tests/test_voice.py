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
    assert not r.unparsed and r.parse_ms < 5
    assert r.english == "BADGER 3-2 is CRITICAL. BADGER 3-DOC is running low: needs 2 x blood (O-neg)."


def test_ukrainian_low_stock_quantities_before_and_after():
    r = parse_report(_clip("badger1-stock-uk"), _ids())
    assert r.events == [{"type": "LOW_STOCK", "subject_id": "med-1", "callsign": "BADGER 1-DOC",
                         "items": {"tourniquet": 3, "hemostatic_gauze": 2}}]


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
    assert parse_report("Перевірка зв'язку.", _ids()).unparsed == ["no casualty or supply request heard"]


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
