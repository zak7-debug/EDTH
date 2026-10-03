"""Voice reports: radio audio (Ukrainian or English) -> transcript -> the same events as POST /events.

Who reports what:
- Medics (Ukrainian callsign, «Борсук один, медик»): casualties, restocks with an urgency, spoken zones,
  and «Скільки до прибуття?» (how long until my drone arrives).
- Drone pilots: «Яструб один збитий» (HAWK 1 shot down) -> DRONE_LOST; a threat seen from the drone
  («Сокіл два, ворожий дрон, вісімсот метрів на північ») -> a zone placed from the drone's position.
- Truck drivers: road blocks and threats from their device position (lat / lon).
Every report gets a short read-back (`readback`: Ukrainian and English) that the dashboard speaks.

    POST /voice?clip=<name>   body = raw audio bytes (wav/webm/mp3/ogg). clip names a scripted clip
                              in frontend/audio/, whose .txt transcript is the fallback when speech
                              to text is unavailable.
    POST /voice/text          {"text": "...", "language": "uk"}: skip speech to text (tests, fallback)
    Both take an optional `speaker`: the reporter's callsign, e.g. "Борсук один, медик" (voice/pipeline.py),
    and optional `lat` / `lon`: the device's position, which places a truck driver's road or threat report.

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
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from .geo.parse_spatial import KILOMETRES, METRES, parse_spatial
from .messages import voice_report_msg
from .models import RESTOCK_PRIORITY

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
    # English
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "nine": 9, "ten": 10,
}
CALLSIGN_STEMS = {"борсук": "BADGER", "барсук": "BADGER", "badger": "BADGER", "borsuk": "BADGER"}  # DEMO: the seed's invented squad names
MEDIC_STEMS = ("медик", "лікар", "санінструктор", "medic", "doc")

# Order matters: CRITICAL is checked first, so "важко поранений" (badly wounded) is CRITICAL.
SEVERITY_STEMS = {
    "CRITICAL": ("важк", "тяжк", "тяжел", "критич", "масивн", "critical", "urgent", "severe", "massive"),
    "WOUNDED": ("поранен", "ранен", "легк", "трьохсот", "300", "wounded", "injured", "light"),
}
ITEM_STEMS = {
    "blood_oneg": ("кров", "плазм", "blood", "plasma"),
    "tourniquet": ("турнікет", "джгут", "tourniquet"),
    "chest_seal": ("оклюзійн", "наліпк", "seal"),
    "hemostatic_gauze": ("гемостат", "бинт", "пов'язк", "gauze", "hemostatic", "bandage"),
    "morphine_autoinjector": ("морфін", "знебол", "morphine", "painkiller"),
}
NOT_ITEMS = ("кровотеч",)  # "кровотеча" is bleeding, not a request for blood
NEED_STEMS = ("закінчу", "мало", "потріб", "треба", "нужн", "надо", "бракує", "немає", "нема", "надішл", "need",
              "low", "running", "out", "send", "short")

# Spoken map reports (geo/, README_audio_geolocation.md): a sentence with one of these and a direction
# ("вісімсот метрів на північний схід") becomes a zone round the projected point. Checked in order.
ZONE_INTENTS = (
    ("ROAD_BLOCKED", ("дорог", "шлях", "міст", "road", "bridge"), ("заблок", "перекрит", "закрит", "blocked",
                                                                  "closed", "cut")),
    ("NO_GO_AREA", ("мін", "розтяжк", "minefield", "mines", "mined"), ()),
    ("NO_FLY_ZONE", ("ппо", "пзрк", "перехоплюв", "шахед", "manpads", "shahed", "interceptor"), ()),
    # "дрон" alone is usually OUR drone ("надішліть дрон"): only a hostile or closed one is a threat
    ("NO_FLY_ZONE", ("дрон", "повітр", "drone", "airspace", "fly"),
     ("ворож", "против", "загроз", "небезпе", "закри", "enemy", "hostile", "threat", "danger", "close", "no")),
)
NO_ENTRY = ("не заїжд", "не заход", "do not enter", "no go")  # "не заїжджати": a ground no-go area
DIST_UNITS = METRES | KILOMETRES
ENGLISH_ZONES = {"NO_FLY_ZONE": "no-fly zone", "NO_GO_AREA": "no-go area", "ROAD_BLOCKED": "road blocked"}

# Restock urgency (LOW_STOCK `urgency`, models.RESTOCK_PRIORITY). Checked in this order, as phrases on
# word starts: "не терміново" contains "терміново". A restock in a report with a CRITICAL casualty is CRITICAL.
URGENCY_STEMS = {
    "NON_URGENT": ("не терміново", "не срочно", "планов", "звичайн", "коли буде змога", "not urgent",
                   "routine", "when you can"),
    "CRITICAL": ("критичн", "важк", "тяжк", "масивн", "негайно", "critical", "immediately"),
    "URGENT": ("терміново", "швидко", "закінчу", "немає", "нема", "urgent", "asap", "running out", "out of"),
}
# DEMO: the seed's invented drone callsigns, as a pilot says them (Ukrainian case forms by stem).
DRONE_STEMS = {"hawk": "HAWK", "яструб": "HAWK", "falcon": "FALCON", "сокіл": "FALCON", "сокол": "FALCON",
               "owl": "OWL", "сова": "OWL", "сови": "OWL", "сову": "OWL"}
DRONE_UK = {"HAWK": "Яструб", "FALCON": "Сокіл", "OWL": "Сова"}
LOST_STEMS = ("збит", "втрач", "знищ", "впав", "впала", "shot down", "lost", "crashed", "destroyed")
ETA_STEMS = ("скільки до", "коли прибуд", "коли буде", "де дрон", "де мій", "how long", "when will", "eta")

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
        if re.fullmatch(r"[1-9]{2}", toks[i + 1]) and _number(nxt) is None:  # Whisper writes "Борсук 32" for три-два
            unit, nxt = int(toks[i + 1][0]), toks[i + 1][1]
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
    """Stems matched at word starts, so multi-word phrases ("не терміново", "shot down") work too."""
    s = " " + " ".join(_tokens(sentence)) + " "
    return any(f" {stem}" in s for stem in stems)


def _urgency(sentence: str) -> Optional[str]:
    return next((u for u, stems in URGENCY_STEMS.items() if _has(sentence, stems)), None)


def _drones(toks: list[str]) -> list[str]:
    """Drone callsigns spoken: "HAWK 1", «Яструб один»."""
    found = []
    for i, t in enumerate(toks[:-1]):
        name = next((v for k, v in DRONE_STEMS.items() if t.startswith(k)), None)
        if name and _number(toks[i + 1]) is not None:
            found.append(f"{name} {_number(toks[i + 1])}")
    return found


def _zone_intent(sentence: str, toks: list[str]) -> Optional[str]:
    low = sentence.lower()
    for kind, stems, also in ZONE_INTENTS:
        if any(t.startswith(stems) for t in toks) and (not also or any(t.startswith(also) for t in toks)):
            return kind
    if any(cue in low for cue in NO_ENTRY):
        return "NO_GO_AREA"
    return None


def _zone_events(sentences: list[str], speaker: Optional[str], callsign_ids: dict[str, str],
                 out: ParsedReport, position: Optional[tuple[float, float]] = None,
                 drone: Optional[tuple[str, tuple[float, float]]] = None) -> None:
    """Zone reports: the intent's sentence and the ones after it (until the next intent) carry the
    distance, direction and radius, e.g. "Ворожий дрон. Вісімсот метрів на північний схід. Закрити п'ятсот"."""
    marks = [(k, _zone_intent(s, _tokens(s))) for k, s in enumerate(sentences)]
    marks = [(k, kind) for k, kind in marks if kind]
    for n, (k, kind) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(sentences)
        spatial_text = ". ".join(sentences[k:end])
        sp = parse_spatial(spatial_text)
        pid = callsign_ids.get(speaker or "")
        if drone is not None:  # a pilot: placed from the drone they named
            position = drone[1]
            pid = None
        if pid is None and position is not None:
            # A truck driver isn't in the graph: place it from the device's position (geo/api `source`).
            # No direction heard means "here".
            here = sp.bearing_deg is None
            ev = {"type": kind, "source": {"lat": position[0], "lon": position[1],
                                           "user_id": drone[0] if drone else "driver"},
                  "callsign": drone[0] if drone else "DRIVER", "distance_m": 0.0 if here else sp.distance_m,
                  "bearing_deg": 0.0 if here else sp.bearing_deg,
                  "assumptions": sp.assumptions + (["at the reporter's position"] if here else []), "text": spatial_text}
            if sp.radius_m is not None:
                ev["radius_m"] = sp.radius_m
            out.events.append(ev)
        elif pid is None:
            out.unparsed.append(f"{ENGLISH_ZONES[kind]} reported but no callsign heard to place it from")
        elif sp.bearing_deg is None:
            out.unparsed.append(f"{ENGLISH_ZONES[kind]}: {'; '.join(sp.reasons)}, not placed (ask for a direction)")
        else:
            ev = {"type": kind, "subject_id": pid, "callsign": speaker, "distance_m": sp.distance_m,
                  "bearing_deg": sp.bearing_deg, "assumptions": sp.assumptions, "text": spatial_text}
            if sp.radius_m is not None:
                ev["radius_m"] = sp.radius_m
            out.events.append(ev)


def parse_report(text: str, callsign_ids: dict[str, str],
                 position: Optional[tuple[float, float]] = None,
                 drones: Optional[dict[str, tuple[str, tuple[float, float]]]] = None,
                 unhurt: Optional[list[str]] = None) -> ParsedReport:
    """Turn one radio report into partial events. callsign_ids maps "BADGER 2-4" -> "sol-10";
    drones maps "HAWK 1" -> ("drn-01", (lat, lon)); position is the reporter's device (a driver).

    The squad named first without a soldier number ("Борсук два, медик") is the speaker; their
    medic is the subject of any low-stock request. A soldier's callsign plus a severity word is a
    casualty; supplies asked for in the same sentence are a restock for that soldier's squad medic,
    because drones deliver to medics only. A sentence with supplies and a "need / running out" word is
    a low-stock request, with an urgency. A drone callsign with a loss word is a lost drone.
    A casualty with no soldier named ("один поранений, важкий") is taken as the next unhurt soldier
    of the speaker's squad (unhurt: callsigns of soldiers still OK), and the summary says so.
    """
    start = time.perf_counter()
    out = ParsedReport()
    drones = drones or {}
    speaker: Optional[str] = None  # the medic's callsign
    named_drone: Optional[str] = None  # the drone a pilot named: zones are placed from it
    report_critical = False
    unhurt = list(unhurt or [])
    assumed: list[str] = []  # soldiers taken for an unnamed casualty
    sentences = [s for s in (s.strip() for s in re.split(r"[.!?;\n]+", text)) if s]

    def restock(medic: Optional[str], items: dict, urgency: Optional[str], sentence: str):
        pid = callsign_ids.get(medic or "")
        if pid is None:
            out.unparsed.append(f"supplies requested but no medic callsign heard: {sentence}")
            return
        rank = lambda u: RESTOCK_PRIORITY[u] if u else 99  # unsaid: settled after the last sentence
        for ev in out.events:  # one restock per medic per report
            if ev["type"] == "LOW_STOCK" and ev["subject_id"] == pid:
                for k, q in items.items():
                    ev["items"][k] = max(ev["items"].get(k, 0), q)
                if rank(urgency) < rank(ev["urgency"]):
                    ev["urgency"] = urgency
                return
        out.events.append({"type": "LOW_STOCK", "subject_id": pid, "items": dict(items), "urgency": urgency,
                           "callsign": medic})

    for sentence in sentences:
        toks = _tokens(sentence)
        # "трьохсот метрів" is a distance, not "300" (wounded): drop numbers followed by a unit
        sev_toks = [t for i, t in enumerate(toks) if not (i + 1 < len(toks) and toks[i + 1] in DIST_UNITS)]
        calls = _callsigns(toks)
        for _, cs in calls:
            if speaker is None and not re.search(r"-\d+$", cs):
                speaker = cs if cs.endswith("-DOC") else f"{cs}-DOC"
        soldiers = [cs for _, cs in calls if re.search(r"-\d+$", cs)]
        severity = next((sev for sev, stems in SEVERITY_STEMS.items()
                         if any(t.startswith(stems) for t in sev_toks)), None)
        items = _items(toks)
        needs = any(t.startswith(NEED_STEMS) for t in toks)
        urgency = _urgency(sentence)

        # Drone pilots: a named drone lost, or the drone a later threat is seen from.
        for dcs in _drones(toks):
            if dcs not in drones:
                out.unparsed.append(f"unknown drone {dcs}")
                continue
            named_drone = named_drone or dcs
            if _has(sentence, LOST_STEMS):
                out.events.append({"type": "DRONE_LOST", "drone_id": drones[dcs][0], "callsign": dcs})
        if _has(sentence, ETA_STEMS) and not items:
            out.events.append({"type": "ETA_QUERY", "callsign": speaker})  # answered in handle_transcript

        for cs in soldiers:
            pid = callsign_ids.get(cs)
            if pid is None:
                out.unparsed.append(f"unknown callsign {cs}")
            elif severity is None:
                out.unparsed.append(f"{cs}: no severity heard (critical / wounded)")
            else:
                out.events.append({"type": "CASUALTY", "subject_id": pid, "severity": severity, "callsign": cs})
                report_critical = report_critical or severity == "CRITICAL"
                if items and needs:  # the squad medic treats them, so the medic gets the supplies
                    restock(cs.rsplit("-", 1)[0] + "-DOC", items,
                            "CRITICAL" if severity == "CRITICAL" else urgency or "URGENT", sentence)
        if severity and not soldiers and not any(t in DIST_UNITS for t in toks):
            # "Один поранений, важкий": no soldier named, so the next unhurt soldier(s) of the speaker's squad
            squad = (speaker or "").removesuffix("-DOC")
            at = next(i for i, t in enumerate(sev_toks) if t.startswith(SEVERITY_STEMS["CRITICAL"] + SEVERITY_STEMS["WOUNDED"]))
            count = next((_number(t) for t in reversed(sev_toks[max(0, at - 3):at]) if _number(t)), 1)
            picks = [cs for cs in unhurt if squad and cs.startswith(f"{squad}-")][:min(count, 5)]
            if not picks:
                out.unparsed.append(f"casualty heard but not which soldier: say their callsign, e.g. «Борсук три-два, "
                                    f"важкий» ({sentence})")
            for cs in picks:
                unhurt.remove(cs)
                assumed.append(cs)
                soldiers.append(cs)
                out.events.append({"type": "CASUALTY", "subject_id": callsign_ids[cs], "severity": severity,
                                   "callsign": cs})
                report_critical = report_critical or severity == "CRITICAL"
            if picks and items and needs:
                restock(f"{squad}-DOC", items, "CRITICAL" if severity == "CRITICAL" else urgency or "URGENT", sentence)
        if items and needs and not soldiers:
            medic = next((cs for _, cs in calls if cs.endswith("-DOC")), speaker)
            restock(medic, items, urgency, sentence)
    for ev in out.events:  # unless said otherwise, running short while treating a CRITICAL casualty is CRITICAL
        if ev["type"] == "LOW_STOCK" and ev["urgency"] != "NON_URGENT":
            ev["urgency"] = "CRITICAL" if report_critical else ev["urgency"] or "NON_URGENT"
    lost = {e["callsign"] for e in out.events if e["type"] == "DRONE_LOST"}
    pilot = (named_drone, drones[named_drone][1]) if named_drone and named_drone not in lost else None
    _zone_events(sentences, speaker, callsign_ids, out, position, pilot)
    if not out.events and not out.unparsed:
        out.unparsed.append("no casualty, supply request, zone or lost drone heard")
    out.english = english_summary(out.events)
    if assumed:
        out.english += f" (soldier not named: taken as {', '.join(assumed)})"
    out.parse_ms = round((time.perf_counter() - start) * 1000, 2)
    return out


def english_summary(events: list[dict]) -> str:
    parts = []
    for e in events:
        extra = ", ".join(f"{q} x {ENGLISH_ITEMS.get(k, k)}" for k, q in e.get("items", {}).items())
        if e["type"] == "CASUALTY":
            parts.append(f"{e['callsign']} is {e['severity']}")
        elif e["type"] == "DRONE_LOST":
            parts.append(f"{e['callsign']} lost")
        elif e["type"] == "ETA_QUERY":
            parts.append(f"{e['callsign'] or 'Medic'} asks when their drone arrives")
        elif e["type"] in ENGLISH_ZONES:
            r = f", radius {e['radius_m']:.0f} m" if e.get("radius_m") else ""
            where = ("at their position" if not e["distance_m"]
                     else f"{e['distance_m']:.0f} m at {e['bearing_deg']:.0f} degrees")
            parts.append(f"{e['callsign']} reports {ENGLISH_ZONES[e['type']]} {where}{r}")
        else:
            urgency = (e.get("urgency") or "NON_URGENT").replace("_", "-").lower()
            parts.append(f"{e['callsign']} is running low: needs {extra} ({urgency})")
    return ". ".join(parts) + ("." if parts else "")


# ---- speech to text -------------------------------------------------------------------------------

_model = None


def _whisper():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # optional dependency: requirements-voice.txt
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


SAMPLE_RATE = 16_000  # what Whisper expects


def decode_audio(audio: bytes):
    """Audio bytes -> 16 kHz mono float32 samples, without faster-whisper's own decoder.

    faster_whisper.audio.decode_audio calls av.open(..., metadata_errors=...), which some PyAV builds
    reject ("open() got an unexpected keyword argument 'metadata_errors'"). WAV (scripted clips, the
    laptop pipeline) is read with the standard library; anything else (the browser's webm/ogg mic
    recording) goes through PyAV with plain arguments."""
    import numpy as np
    if audio[:4] == b"RIFF":
        import wave
        with wave.open(io.BytesIO(audio)) as w:
            if w.getsampwidth() == 2:
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
                pcm = pcm.reshape(-1, w.getnchannels()).mean(axis=1)
                rate = w.getframerate()
                if rate != SAMPLE_RATE:  # linear resample: fine for speech
                    n = int(len(pcm) * SAMPLE_RATE / rate)
                    pcm = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm).astype(np.float32)
                return pcm
    import av
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    chunks = []
    with av.open(io.BytesIO(audio), mode="r") as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame) or []:
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None) or []:  # flush
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        raise ValueError("no audio in the recording")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


SPOKEN = ("uk", "en")  # the languages the parser reads
# Whisper spells unusual words better when it has seen them: the callsigns and kit names, no full report.
PROMPT = {"uk": "Борсук, Яструб, Сокіл, Сова. Медик, водій, пілот. Турнікети, гемостатики, кров.",
          "en": "Badger, Hawk, Falcon, Owl. Medic, driver, pilot. Tourniquets, gauze, blood."}


def transcribe(audio: bytes, language: Optional[str] = None) -> tuple[str, str]:
    """(transcript, detected language). Raises if faster-whisper or its model is unavailable.
    With no language given, Whisper detects it, but a short Ukrainian call is often heard as Russian,
    which the parser can't read: anything other than uk / en is redone as the likelier of the two.
    Segments are lazy, so the first pass only costs the detection."""
    model, pcm = _whisper(), decode_audio(audio)
    opts = dict(beam_size=1, vad_filter=False, condition_on_previous_text=False)
    if language not in SPOKEN:
        _, info = model.transcribe(pcm, language=None, **opts)
        probs = dict(getattr(info, "all_language_probs", None) or [])
        language = info.language if info.language in SPOKEN else max(SPOKEN, key=lambda l: probs.get(l, 0.0))
    segments, info = model.transcribe(pcm, language=language, initial_prompt=PROMPT[language], **opts)
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
                            speaker: Optional[str] = None, position: Optional[tuple[float, float]] = None) -> dict:
    """Parse, broadcast the voice_report, then run each event through the normal pipeline.
    speaker is the reporter's own callsign as they'd say it ("Борсук один, медик"), for a device that
    knows who is holding it; it is read as if spoken first, so "I'm out of blood" finds their medic."""
    from . import dev_server  # late import: dev_server includes this router
    world = dev_server.world
    callsign_ids = {p.callsign: p.id for p in world.repo.list_personnel()}
    drones = {d.callsign: (d.id, (d.lat, d.lon)) for d in world.repo.list_drones() if d.status != "LOST"}
    for f in world.tracker.flights.values():  # a drone in the air: where it is now, not where it was stored
        cs = next((c for c, (i, _) in drones.items() if i == f.drone_id), None)
        if cs:
            drones[cs] = (f.drone_id, f.position())
    unhurt = sorted(p.callsign for p in world.repo.list_personnel()
                    if p.status == "OK" and re.search(r"-\d+$", p.callsign or ""))
    parsed = parse_report(f"{speaker}. {text}" if speaker else text, callsign_ids, position, drones, unhurt)
    report_id = f"voice-{int(time.time())}-{next(_ids)}"
    for k, ev in enumerate(parsed.events, start=1):
        ev["event_id"] = f"{report_id}-{k}"  # becomes the dispatch's request_id
    await dev_server.broadcast(voice_report_msg(report_id, text, language, parsed.english, parsed.events,
                                                parsed.unparsed, stt, stt_ms, parsed.parse_ms))
    results = []
    for ev in parsed.events:
        if ev["type"] == "ETA_QUERY":  # a question, not an event: answer it from the live flights
            results.append(eta_answer(world, callsign_ids.get(ev["callsign"] or "")))
            continue
        raw = {k: v for k, v in ev.items() if k != "callsign"}
        results.append(await dev_server.process(raw, time.perf_counter()))
    readback = build_readback(parsed.events, results, parsed.unparsed, world)
    return {"report_id": report_id, "transcript": text, "language": language, "english": parsed.english,
            "events": parsed.events, "unparsed": parsed.unparsed, "stt": stt, "stt_ms": stt_ms,
            "parse_ms": parsed.parse_ms, "results": results, "readback": readback}


# ---- read-back (README_eta_relay.md): what the radio says back, Ukrainian first ------------------

def _minutes_uk(n: int) -> str:
    """"через 1 хвилину / 3 хвилини / 7 хвилин"."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} хвилину"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} хвилини"
    return f"{n} хвилин"


def _drone_uk(callsign: str) -> str:
    name, _, num = callsign.partition(" ")
    return f"{DRONE_UK.get(name, name)} {num}".strip()


def _person_uk(callsign: str) -> str:
    """"BADGER 3-2" -> "Борсук 3-2", so the Ukrainian read-back says the callsign the medic used."""
    uk = {v: k.capitalize() for k, v in CALLSIGN_STEMS.items() if not k.isascii()}
    name, _, rest = callsign.partition(" ")
    return f"{uk.get(name, name)} {rest}".strip()


def eta_answer(world, medic_id: Optional[str]) -> dict:
    """The live ETA of the drone flying to this medic, or where their request stands."""
    if medic_id is None:
        return {"status": "UNKNOWN_MEDIC"}
    flights = [f for f in world.tracker.flights.values()
               if f.phase == "EN_ROUTE" and f.dispatch is not None and f.dispatch.recipient_id == medic_id]
    if flights:
        f = min(flights, key=lambda f: f.total_m - f.flown_m)
        drone = world.repo.get_drone(f.drone_id)
        return {"status": "EN_ROUTE", "drone_id": f.drone_id, "callsign": drone.callsign if drone else f.drone_id,
                "eta_s": round((f.total_m - f.flown_m) / f.speed_mps, 1)}
    if any(e.subject_id == medic_id for e in world.engine.pending()):
        return {"status": "QUEUED"}
    return {"status": "NONE"}


def build_readback(events: list[dict], results: list[dict], unparsed: list[str], world) -> dict:
    """One short line per event, ETA first, under ten seconds of speech (README_eta_relay.md)."""
    uk, en = [], []
    for ev, res in zip(events, results):
        t = ev["type"]
        if res.get("drone_id") and res.get("eta_s") is not None and t in ("LOW_STOCK", "ETA_QUERY"):
            drone = world.repo.get_drone(res["drone_id"])
            cs = res.get("callsign") or (drone.callsign if drone else res["drone_id"])
            mins = max(1, round(res["eta_s"] / 60))
            lead = "Запит прийнято. " if t == "LOW_STOCK" else ""
            uk.append(f"{lead}{_drone_uk(cs)} прибуде приблизно через {_minutes_uk(mins)}.")
            en.append(f"{'Request received. ' if t == 'LOW_STOCK' else ''}{cs} arrives in about {mins} min.")
        elif t == "LOW_STOCK":
            uk.append("Запит прийнято. Вільного дрона зараз немає, ви в черзі.")
            en.append("Request received. No drone free yet: you're in the queue.")
        elif t == "ETA_QUERY":
            status = res.get("status")
            uk.append({"QUEUED": "Ваш запит у черзі, дрон ще не вилетів.",
                       "NONE": "Відкритих запитів немає."}.get(status, "Не знаю, хто питає. Назвіть позивний."))
            en.append({"QUEUED": "Your request is queued; no drone has left yet.",
                       "NONE": "No open requests."}.get(status, "Didn't catch who is asking: say your callsign."))
        elif t == "CASUALTY":
            ev_ = res.get("evacuation") or {}
            if ev_.get("eta_s"):
                mins = max(1, round(ev_["eta_s"] / 60))
                uk.append(f"Прийнято, {_person_uk(ev['callsign'])}. Евакуація: приблизно {_minutes_uk(mins)}.")
                en.append(f"Copy, {ev['callsign']}. Evacuation to {ev_.get('facility_name', 'hospital')}, about {mins} min.")
            else:
                uk.append(f"Прийнято, {_person_uk(ev['callsign'])}.")
                en.append(f"Copy, {ev['callsign']}.")
        elif t == "DRONE_LOST":
            uk.append(f"Прийнято, {_drone_uk(ev['callsign'])} списано. Доставку передано іншому дрону.")
            en.append(f"Copy, {ev['callsign']} written off. Its delivery goes to another drone.")
        else:  # spoken zones
            uk.append("Прийнято, зону нанесено на карту. Маршрути змінено.")
            en.append("Copy, zone on the map. Routes adjusted.")
    if not uk:
        uk.append("Не зрозумів. Повторіть, будь ласка.")
        en.append("Didn't catch that. Say again.")
    return {"uk": " ".join(dict.fromkeys(uk)), "en": " ".join(dict.fromkeys(en))}


@router.post("/voice")
async def post_voice(request: Request, clip: Optional[str] = None, language: Optional[str] = None,
                     speaker: Optional[str] = None, lat: Optional[float] = None, lon: Optional[float] = None):
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
            raise HTTPException(503, f"Speech to text failed on this machine ({type(e).__name__}: {e}). Scripted calls "
                                     "and typed text still work. To fix the mic: pip install -r requirements-voice.txt, "
                                     "then python scripts/fetch_whisper.py while online.")
    stt_ms = round((time.perf_counter() - start) * 1000, 1)
    return await handle_transcript(text, lang, stt, stt_ms, speaker, _position(lat, lon))


@router.post("/voice/text")
async def post_voice_text(body: dict):
    if not body.get("text"):
        raise HTTPException(422, "body needs text")
    return await handle_transcript(body["text"], body.get("language"), "text", 0.0, body.get("speaker"),
                                   _position(body.get("lat"), body.get("lon")))


def _position(lat, lon) -> Optional[tuple[float, float]]:
    """The reporter's device position (a truck driver), if sent."""
    return (float(lat), float(lon)) if lat is not None and lon is not None else None
