"""Voice reports: radio audio (Ukrainian or English) -> transcript -> the same events as POST /events.

Who can report, and what it becomes (all through dev_server.process, so nothing downstream changes):
- Medics: casualties (CASUALTY, which starts the evacuation) and restocks (LOW_STOCK with an urgency).
  Supplies asked for alongside a casualty go to that squad's medic, since drones deliver to medics only.
- Drone pilots: "HAWK 1 shot down" -> DRONE_LOST; "HAWK 1, air defence 800 m north" -> THREAT there.
- Truck drivers (or anyone): "road blocked / mines / threat" -> THREAT at their position, which closes
  the roads through it and routes drones round it. Drivers aren't in the graph, so their position comes
  from the device (?lat=&lon=) or from a callsign they name; distance + compass word moves it.

    POST /voice?clip=<name>   body = raw audio bytes (wav/webm/mp3/ogg). clip names a scripted clip
                              in frontend/audio/, whose .txt transcript is the fallback when speech
                              to text is unavailable. Optional ?lat=&lon= is the reporter's position.
    POST /voice/text          {"text": "...", "language": "uk", "lat": .., "lon": ..}: skip speech to text

Speech to text is faster-whisper, offline on CPU (`pip install -r requirements-voice.txt`, then
`python scripts/fetch_whisper.py` once while online). If it isn't installed, the model is missing or
it fails, a scripted clip falls back to its transcript and the report says stt="cached".

The parser is deliberately rule based: offline, sub-millisecond and the same every rehearsal.
Location comes from the graph (the callsign's current position), exactly as for the trigger panel.

Plan: /mnt/project-files/plans/voice-reports.md. Wire format: contracts/messages.md (`voice_report`).
"""
from __future__ import annotations

import io
import itertools
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from .messages import voice_report_msg
from .models import RESTOCK_PRIORITY as RESTOCK_RANK

AUDIO_DIR = Path(__file__).resolve().parents[2] / "frontend" / "audio"
WHISPER_MODEL = os.environ.get("EDTH_WHISPER_MODEL", "small")  # TUNE: "base" if the demo CPU is slow

# ---- vocabulary -----------------------------------------------------------------------------------

NUMBERS = {
    # Ukrainian: cardinals with the case forms a medic would say, and ordinals ("Борсук другий")
    "один": 1, "одна": 1, "одну": 1, "одного": 1, "одне": 1, "перший": 1, "першого": 1, "перша": 1,
    "два": 2, "дві": 2, "двох": 2, "другий": 2, "другого": 2, "друга": 2,
    "три": 3, "трьох": 3, "третій": 3, "третього": 3, "третя": 3,
    "чотири": 4, "чотирьох": 4, "четвертий": 4, "четвертого": 4,
    "п'ять": 5, "п'яти": 5, "п'ятий": 5, "п'ятого": 5,
    "шість": 6, "шести": 6, "шостий": 6, "шостого": 6,
    "сім": 7, "семи": 7, "сьомий": 7, "сьомого": 7,
    "вісім": 8, "восьми": 8, "дев'ять": 9, "десять": 10,
    "сто": 100, "двісті": 200, "триста": 300, "чотириста": 400, "п'ятсот": 500, "шістсот": 600,
    "сімсот": 700, "вісімсот": 800, "дев'ятсот": 900, "тисяча": 1000, "тисячу": 1000,
    # English
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "nine": 9, "ten": 10,
}
CALLSIGN_STEMS = {"борсук": "BADGER", "badger": "BADGER"}  # DEMO: the seed's invented squad names
MEDIC_STEMS = ("медик", "лікар", "санінструктор", "medic", "doc")

# Order matters: CRITICAL is checked first, so "важко поранений" (badly wounded) is CRITICAL.
SEVERITY_STEMS = {
    "CRITICAL": ("важк", "тяжк", "критичн", "масивн", "critical", "urgent", "severe", "massive"),
    "WOUNDED": ("поранен", "легк", "трьохсот", "300", "wounded", "injured", "light"),
}
ITEM_STEMS = {
    "blood_oneg": ("кров", "плазм", "blood", "plasma"),
    "tourniquet": ("турнікет", "джгут", "tourniquet"),
    "chest_seal": ("оклюзійн", "наліпк", "seal"),
    "hemostatic_gauze": ("гемостат", "gauze", "hemostatic"),
    "morphine_autoinjector": ("морфін", "знебол", "morphine", "painkiller"),
}
NOT_ITEMS = ("кровотеч",)  # "кровотеча" is bleeding, not a request for blood
NEED_STEMS = ("закінчу", "мало", "потріб", "треба", "бракує", "немає", "нема", "надішл", "need",
              "low", "running", "out", "send", "short")

# Restock urgency (LOW_STOCK `urgency`, models.RESTOCK_PRIORITY). Checked in this order: "не терміново"
# contains "терміново". A restock in a report with a CRITICAL casualty is CRITICAL too.
URGENCY_STEMS = {
    "NON_URGENT": ("не терміново", "не срочно", "планов", "звичайн", "коли буде змога", "not urgent",
                   "routine", "when you can"),
    "CRITICAL": ("критичн", "важк", "тяжк", "масивн", "негайно", "critical", "immediately", "life"),
    "URGENT": ("терміново", "швидко", "закінчу", "немає", "нема", "urgent", "asap", "running out", "out of"),
}

# DEMO: the seed's invented drone callsigns, as a pilot might say them.
DRONE_STEMS = {"hawk": "HAWK", "яструб": "HAWK", "falcon": "FALCON", "сокіл": "FALCON", "сокол": "FALCON",
               "owl": "OWL", "сова": "OWL", "сови": "OWL", "сову": "OWL"}
LOST_STEMS = ("збит", "втрач", "знищ", "впав", "впала", "shot", "lost", "crashed", "destroyed")
THREAT_STEMS = ("загроз", "ппо", "реб", "глушін", "глушен", "перехоплюв", "ворожий дрон",
                "threat", "jamming", "air defence", "air defense", "interceptor", "enemy drone")
ROAD_STEMS = ("заблок", "перекрит", "завал", "мінн", "міни", "вирв", "blocked", "closed", "mines", "crater")
# TUNE: zone sizes for spoken reports (Sasank's README_audio_geolocation.md defaults).
THREAT_RADIUS_M, ROAD_RADIUS_M = 800, 300
BEARINGS = (("півн", 0), ("north", 0), ("півд", 180), ("south", 180),
            ("схід", 90), ("східн", 90), ("сході", 90), ("east", 90),
            ("захід", 270), ("західн", 270), ("заході", 270), ("west", 270))

ENGLISH_ITEMS = {"blood_oneg": "blood (O-neg)", "tourniquet": "tourniquets", "chest_seal": "chest seals",
                 "hemostatic_gauze": "haemostatic gauze", "morphine_autoinjector": "morphine"}


# ---- parser ---------------------------------------------------------------------------------------

@dataclass
class ParsedReport:
    events: list[dict] = field(default_factory=list)  # partial events for dev_server.process
    unparsed: list[str] = field(default_factory=list)  # what was heard but not acted on, and why
    english: str = ""
    parse_ms: float = 0.0


def _tokens(sentence: str) -> list[str]:
    s = sentence.lower().replace("’", "'").replace("ʼ", "'").replace("`", "'")
    s = re.sub(r"(\d)\s*[-–—]\s*(\d)", r"\1 \2", s)  # "2-4" -> "2 4"
    return re.findall(r"[\w']+", s)


def _number(tok: str) -> Optional[int]:
    if tok.isdigit():
        return int(tok)
    return NUMBERS.get(tok)


def _callsigns(toks: list[str]) -> list[tuple[int, str]]:
    """(token index, callsign) for each callsign spoken: "BADGER 2-4" or "BADGER 2-DOC"."""
    found = []
    for i, t in enumerate(toks):
        name = next((v for k, v in CALLSIGN_STEMS.items() if t.startswith(k)), None)
        if name is None or i + 1 >= len(toks) or _number(toks[i + 1]) is None:
            continue
        unit = _number(toks[i + 1])
        nxt = toks[i + 2] if i + 2 < len(toks) else ""
        if _number(nxt) is not None:
            found.append((i, f"{name} {unit}-{_number(nxt)}"))
        elif nxt.startswith(MEDIC_STEMS):
            found.append((i, f"{name} {unit}-DOC"))
        else:
            found.append((i, f"{name} {unit}"))  # a squad on its own: who is speaking
    return found


def _is_item(tok: str) -> Optional[str]:
    if tok.startswith(NOT_ITEMS):
        return None
    return next((k for k, stems in ITEM_STEMS.items() if tok.startswith(stems)), None)


def _items(toks: list[str]) -> dict[str, int]:
    items: dict[str, int] = {}
    for i, t in enumerate(toks):
        item = _is_item(t)
        if item is None:
            continue
        qty = next((_number(toks[j]) for j in range(i - 1, max(-1, i - 4), -1) if _number(toks[j])), None)
        if qty is None:  # "турнікети, потрібно три": the number after, unless it belongs to the next item
            qty = next((_number(toks[j]) for j in range(i + 1, min(len(toks), i + 3))
                        if _number(toks[j]) and not _is_item(toks[j + 1] if j + 1 < len(toks) else "")), None)
        items[item] = max(items.get(item, 0), qty or 1)
    return items


def _has(sentence: str, stems) -> bool:
    """Stem match on word starts, so multi-word stems ("не терміново", "air defence") work too."""
    s = " " + " ".join(_tokens(sentence)) + " "
    return any(f" {stem}" in s for stem in stems)


def _urgency(sentence: str) -> Optional[str]:
    return next((u for u, stems in URGENCY_STEMS.items() if _has(sentence, stems)), None)


def _drones(toks: list[str]) -> list[str]:
    """Drone callsigns spoken: "HAWK 1", "Яструб один"."""
    found = []
    for i, t in enumerate(toks[:-1]):
        name = next((v for k, v in DRONE_STEMS.items() if t.startswith(k)), None)
        if name and _number(toks[i + 1]) is not None:
            found.append(f"{name} {_number(toks[i + 1])}")
    return found


def _offset(toks: list[str]) -> tuple[Optional[float], Optional[float]]:
    """(metres, bearing) from "800 метрів на північний схід" / "two kilometres west", else Nones."""
    dist = None
    for i, t in enumerate(toks):
        km = t.startswith(("кілометр", "km", "kilomet"))
        if not (km or t.startswith(("метр", "metre", "meter")) or t == "m"):
            continue
        j = i
        while j > 0 and (_number(toks[j - 1]) is not None or toks[j - 1] in ("hundred", "thousand")):
            j -= 1
        if j == i:
            continue
        total = 0
        for w in toks[j:i]:  # "вісімсот" = 800; "eight hundred" = 8 x 100; "два кілометри" = 2 km
            total = max(total, 1) * (100 if w == "hundred" else 1000) if w in ("hundred", "thousand") else total + _number(w)
        dist = float(total * (1000 if km else 1))
        break
    ns = next((b for stem, b in BEARINGS if b in (0, 180) and any(t.startswith(stem) for t in toks)), None)
    ew = next((b for stem, b in BEARINGS if b in (90, 270) and any(t.startswith(stem) for t in toks)), None)
    if ns is None and ew is None:
        bearing = None
    elif ns is None or ew is None:
        bearing = float(ns if ew is None else ew)
    else:  # north-east 45, south-east 135, south-west 225, north-west 315
        bearing = {(0, 90): 45.0, (180, 90): 135.0, (180, 270): 225.0, (0, 270): 315.0}[(ns, ew)]
    return dist, bearing


def _project(lat: float, lon: float, dist_m: float, bearing_deg: float) -> tuple[float, float]:
    """Destination point on a sphere (fine under ~20 km)."""
    d, b = dist_m / 6_371_000.0, math.radians(bearing_deg)
    la1, lo1 = math.radians(lat), math.radians(lon)
    la2 = math.asin(math.sin(la1) * math.cos(d) + math.cos(la1) * math.sin(d) * math.cos(b))
    lo2 = lo1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(la1), math.cos(d) - math.sin(la1) * math.sin(la2))
    return round(math.degrees(la2), 6), round(math.degrees(lo2), 6)


def parse_report(text: str, callsign_ids: dict[str, str], drone_ids: Optional[dict[str, str]] = None,
                 where: Optional[dict[str, tuple[float, float]]] = None,
                 position: Optional[tuple[float, float]] = None) -> ParsedReport:
    """Turn one radio report into partial events. callsign_ids maps "BADGER 2-4" -> "sol-10",
    drone_ids "HAWK 1" -> "drn-01"; where maps any callsign to its (lat, lon); position is the
    reporter's own (device GPS), used for road and threat reports when no callsign is named.

    The squad named first without a soldier number ("Борсук два, медик") is the speaker; their
    medic is the subject of any low-stock request. A soldier's callsign plus a severity word is a
    casualty; supplies asked for in the same sentence are a restock for that soldier's squad medic
    (drones deliver to medics only). A sentence with supplies and a "need / running out" word is a
    low-stock request, with an urgency.
    """
    start = time.perf_counter()
    out = ParsedReport()
    drone_ids, where = drone_ids or {}, where or {}
    speaker: Optional[str] = None  # the medic's callsign
    report_critical = False
    last_anchor: Optional[str] = None  # the last drone or person named: "HAWK 2. Air defence 800 m north."

    def _rank(u: Optional[str]) -> int:
        return RESTOCK_RANK[u] if u else RESTOCK_RANK["NON_URGENT"] + 1  # unsaid: settled at the end

    def restock(medic: Optional[str], items: dict, urgency: Optional[str], sentence: str):
        pid = callsign_ids.get(medic or "")
        if pid is None:
            out.unparsed.append(f"supplies requested but no medic callsign heard: {sentence}")
            return
        for ev in out.events:  # one restock per medic per report
            if ev["type"] == "LOW_STOCK" and ev["subject_id"] == pid:
                for k, q in items.items():
                    ev["items"][k] = max(ev["items"].get(k, 0), q)
                if _rank(urgency) < _rank(ev["urgency"]):
                    ev["urgency"] = urgency
                return
        out.events.append({"type": "LOW_STOCK", "subject_id": pid, "items": dict(items), "urgency": urgency,
                           "callsign": medic})

    for sentence in filter(None, (s.strip() for s in re.split(r"[.!?;\n]+", text))):
        toks = _tokens(sentence)
        calls = _callsigns(toks)
        for _, cs in calls:
            if speaker is None and not re.search(r"-\d+$", cs):
                speaker = cs if cs.endswith("-DOC") else f"{cs}-DOC"
        soldiers = [cs for _, cs in calls if re.search(r"-\d+$", cs)]
        severity = next((sev for sev, stems in SEVERITY_STEMS.items()
                         if any(t.startswith(stems) for t in toks)), None)
        items = _items(toks)
        needs = any(t.startswith(NEED_STEMS) for t in toks)
        urgency = _urgency(sentence)

        # Drone pilots: a named drone lost (dev_server also marks the spot as a threat), or a threat seen from it.
        drones = _drones(toks)
        lost = [d for d in drones if d in drone_ids] if _has(sentence, LOST_STEMS) else []
        out.unparsed += [f"unknown drone {d}" for d in drones if d not in drone_ids]
        out.events += [{"type": "DRONE_LOST", "drone_id": drone_ids[d], "callsign": d} for d in lost]
        threat, road = _has(sentence, THREAT_STEMS), _has(sentence, ROAD_STEMS)
        last_anchor = next((cs for cs in drones if cs in where), None) \
            or next((cs for _, cs in calls if cs in where), None) or last_anchor
        if (threat or road) and not lost:
            anchor = last_anchor
            here = where[anchor] if anchor else position
            if here is None:
                out.unparsed.append(f"threat or blocked road heard but no position: {sentence}")
            else:
                dist, bearing = _offset(toks)
                if bearing is not None:
                    here = _project(here[0], here[1], dist or 500.0, bearing)  # TUNE: unspoken distance
                kind = "Reported threat" if threat else "Road blocked"
                out.events.append({"type": "THREAT", "name": f"{kind} (radio{', ' + anchor if anchor else ''})",
                                   "lat": here[0], "lon": here[1],
                                   "radius_m": THREAT_RADIUS_M if threat else ROAD_RADIUS_M,
                                   "callsign": anchor or "reporter"})

        for cs in soldiers:
            pid = callsign_ids.get(cs)
            if pid is None:
                out.unparsed.append(f"unknown callsign {cs}")
            elif severity is None:
                out.unparsed.append(f"{cs}: no severity heard (critical / wounded)")
            else:
                out.events.append({"type": "CASUALTY", "subject_id": pid, "severity": severity, "callsign": cs})
                report_critical = report_critical or severity == "CRITICAL"
                if items and needs:
                    restock(cs.rsplit("-", 1)[0] + "-DOC",
                            items, "CRITICAL" if severity == "CRITICAL" else urgency or "URGENT", sentence)
        if items and needs and not soldiers:
            medic = next((cs for _, cs in calls if cs.endswith("-DOC")), speaker)
            restock(medic, items, urgency, sentence)
    for ev in out.events:  # unless said otherwise, running short while treating a CRITICAL casualty is CRITICAL
        if ev["type"] == "LOW_STOCK" and ev["urgency"] != "NON_URGENT":
            ev["urgency"] = "CRITICAL" if report_critical else ev["urgency"] or "NON_URGENT"
    if not out.events and not out.unparsed:
        out.unparsed.append("no casualty, supply request, threat or lost drone heard")
    out.english = english_summary(out.events)
    out.parse_ms = round((time.perf_counter() - start) * 1000, 2)
    return out


def english_summary(events: list[dict]) -> str:
    parts = []
    for e in events:
        extra = ", ".join(f"{q} x {ENGLISH_ITEMS.get(k, k)}" for k, q in e.get("items", {}).items())
        if e["type"] == "CASUALTY":
            parts.append(f"{e['callsign']} is {e['severity']}")
        elif e["type"] == "LOW_STOCK":
            parts.append(f"{e['callsign']} needs {extra} ({e['urgency'].replace('_', '-').lower()})")
        elif e["type"] == "DRONE_LOST":
            parts.append(f"{e['callsign']} lost")
        else:
            parts.append(f"{e['name']} at {e['lat']:.4f}, {e['lon']:.4f}")
    return ". ".join(parts) + ("." if parts else "")


# ---- speech to text -------------------------------------------------------------------------------

_model = None


def _whisper():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # optional dependency: requirements-voice.txt
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


def transcribe(audio: bytes, language: Optional[str] = None) -> tuple[str, str]:
    """(transcript, detected language). Raises if faster-whisper or its model is unavailable."""
    segments, info = _whisper().transcribe(io.BytesIO(audio), language=language, beam_size=1,
                                           vad_filter=False, condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segments).strip(), info.language


def cached_transcript(clip: Optional[str]) -> Optional[str]:
    if not clip or not re.fullmatch(r"[\w-]+", clip):
        return None
    path = AUDIO_DIR / f"{clip}.txt"
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


# ---- API ------------------------------------------------------------------------------------------

router = APIRouter()
_ids = itertools.count(1)


async def handle_transcript(text: str, language: Optional[str], stt: str, stt_ms: float,
                            position: Optional[tuple[float, float]] = None) -> dict:
    """Parse, broadcast the voice_report, then run each event through the normal pipeline."""
    from . import dev_server  # late import: dev_server includes this router
    repo = dev_server.world.repo
    people, drones = repo.list_personnel(), [d for d in repo.list_drones() if d.status != "LOST"]
    where = {p.callsign: (p.lat, p.lon) for p in people} | {d.callsign: (d.lat, d.lon) for d in drones}
    parsed = parse_report(text, {p.callsign: p.id for p in people}, {d.callsign: d.id for d in drones},
                          where, position)
    report_id = f"voice-{int(time.time())}-{next(_ids)}"
    for k, ev in enumerate(parsed.events, start=1):
        ev["event_id"] = f"{report_id}-{k}"  # becomes the dispatch's request_id
    await dev_server.broadcast(voice_report_msg(report_id, text, language, parsed.english, parsed.events,
                                                parsed.unparsed, stt, stt_ms, parsed.parse_ms))
    results = []
    for ev in parsed.events:
        raw = {k: v for k, v in ev.items() if k != "callsign"}
        results.append(await dev_server.process(raw, time.perf_counter()))
    return {"report_id": report_id, "transcript": text, "language": language, "english": parsed.english,
            "events": parsed.events, "unparsed": parsed.unparsed, "stt": stt, "stt_ms": stt_ms,
            "parse_ms": parsed.parse_ms, "results": results}


@router.post("/voice")
async def post_voice(request: Request, clip: Optional[str] = None, language: Optional[str] = None,
                     lat: Optional[float] = None, lon: Optional[float] = None):
    audio = await request.body()
    start = time.perf_counter()
    try:
        if not audio:
            raise ValueError("empty audio")
        text, lang = transcribe(audio, language)
        stt = "whisper"
    except Exception as e:  # no faster-whisper, no model, or bad audio: use the scripted transcript
        text, lang, stt = cached_transcript(clip), language or "uk", "cached"
        if text is None:
            raise HTTPException(503, f"speech to text unavailable ({e}) and no transcript for clip {clip!r}")
    stt_ms = round((time.perf_counter() - start) * 1000, 1)
    return await handle_transcript(text, lang, stt, stt_ms, (lat, lon) if lat is not None and lon is not None else None)


@router.post("/voice/text")
async def post_voice_text(body: dict):
    if not body.get("text"):
        raise HTTPException(422, "body needs text")
    pos = (float(body["lat"]), float(body["lon"])) if body.get("lat") is not None and body.get("lon") is not None else None
    return await handle_transcript(body["text"], body.get("language"), "text", 0.0, pos)
