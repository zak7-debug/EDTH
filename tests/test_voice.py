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
    assert r.english == "BADGER 3-2 is CRITICAL. BADGER 3-DOC needs 2 x blood (O-neg) (critical)."


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
    assert parse_report("Перевірка зв'язку.", _ids()).unparsed == ["no casualty, supply request, threat or lost drone heard"]


def test_restock_urgency_words():
    def urgency(text):
        return [e["urgency"] for e in parse_report(text, _ids()).events if e["type"] == "LOW_STOCK"]
    assert urgency("Борсук два, медик. Потрібен один турнікет, не терміново.") == ["NON_URGENT"]
    assert urgency("Badger two medic. Need two chest seals urgently.") == ["URGENT"]
    assert urgency("Борсук два, медик. Потрібна кров.") == ["NON_URGENT"]  # the model's default


def test_supplies_for_a_casualty_go_to_the_squad_medic():
    """Drones deliver to medics only, so "BADGER 1-2 critical, need blood" is a restock for BADGER 1-DOC."""
    r = parse_report("Борсук один-два важкий, потрібна кров дві одиниці.", _ids())
    assert r.events == [
        {"type": "CASUALTY", "subject_id": "sol-02", "severity": "CRITICAL", "callsign": "BADGER 1-2"},
        {"type": "LOW_STOCK", "subject_id": "med-1", "items": {"blood_oneg": 2}, "urgency": "CRITICAL",
         "callsign": "BADGER 1-DOC"}]


def _fleet():
    repo = InMemoryRepo()
    drones = {d.callsign: d.id for d in repo.list_drones()}
    where = {p.callsign: (p.lat, p.lon) for p in repo.list_personnel()} | \
        {d.callsign: (d.lat, d.lon) for d in repo.list_drones()}
    return drones, where


def test_pilot_reports_lost_drone_and_threat():
    drones, where = _fleet()
    lost = parse_report("Сокіл один збитий.", _ids(), drones, where)
    assert lost.events == [{"type": "DRONE_LOST", "drone_id": "drn-04", "callsign": "FALCON 1"}]
    r = parse_report("Яструб два, пілот. Бачу ППО, вісімсот метрів на північ.", _ids(), drones, where)
    [ev] = r.events
    assert ev["type"] == "THREAT" and ev["radius_m"] == 800 and ev["callsign"] == "HAWK 2"
    lat, lon = where["HAWK 2"]
    assert abs((ev["lat"] - lat) * 111_000 - 800) < 10 and abs(ev["lon"] - lon) < 1e-6  # 800 m due north


def test_driver_reports_blocked_road_at_their_position():
    drones, where = _fleet()
    r = parse_report("Дорога заблокована за двісті метрів на схід.", _ids(), drones, where, position=(47.7, 35.6))
    [ev] = r.events
    assert ev["type"] == "THREAT" and ev["radius_m"] == 300 and ev["lat"] == 47.7 and ev["lon"] > 35.6
    assert parse_report("Дорога заблокована.", _ids(), drones, where).unparsed[0].startswith("threat or blocked road")


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
        r = client.post("/voice/text", json={"text": "Road blocked, mines.", "lat": 47.70, "lon": 35.60}).json()
        assert r["results"][0]["zone"]["name"] == "Road blocked (radio)"
        assert client.post("/voice/text", json={"text": "HAWK 3 shot down"}).json()["results"][0]["ok"]
