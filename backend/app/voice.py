"""Medic voice reports: radio audio (Ukrainian or English) -> transcript -> the same events as POST /events.

    POST /voice?clip=<name>   body = raw audio bytes (wav/webm/mp3/ogg). clip names a scripted clip
                              in frontend/audio/, whose .txt transcript is the fallback when speech
                              to text is unavailable.
    POST /voice/text          {"text": "...", "language": "uk"}: skip speech to text (tests, fallback)
    Both take an optional `speaker`: the reporter's callsign, e.g. "Борсук один, медик" (voice/pipeline.py).

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
    "вісім": 8, "восьми": 8,
    # English
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
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


def _zone_intent(sentence: str, toks: list[str]) -> Optional[str]:
    low = sentence.lower()
    for kind, stems, also in ZONE_INTENTS:
        if any(t.startswith(stems) for t in toks) and (not also or any(t.startswith(also) for t in toks)):
            return kind
    if any(cue in low for cue in NO_ENTRY):
        return "NO_GO_AREA"
    return None


def _zone_events(sentences: list[str], speaker: Optional[str], callsign_ids: dict[str, str],
                 out: ParsedReport) -> None:
    """Zone reports: the intent's sentence and the ones after it (until the next intent) carry the
    distance, direction and radius, e.g. "Ворожий дрон. Вісімсот метрів на північний схід. Закрити п'ятсот"."""
    marks = [(k, _zone_intent(s, _tokens(s))) for k, s in enumerate(sentences)]
    marks = [(k, kind) for k, kind in marks if kind]
    for n, (k, kind) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(sentences)
        spatial_text = ". ".join(sentences[k:end])
        sp = parse_spatial(spatial_text)
        pid = callsign_ids.get(speaker or "")
        if pid is None:
            out.unparsed.append(f"{ENGLISH_ZONES[kind]} reported but no callsign heard to place it from")
        elif sp.bearing_deg is None:
            out.unparsed.append(f"{ENGLISH_ZONES[kind]}: {'; '.join(sp.reasons)}, not placed (ask for a direction)")
        else:
            ev = {"type": kind, "subject_id": pid, "callsign": speaker, "distance_m": sp.distance_m,
                  "bearing_deg": sp.bearing_deg, "assumptions": sp.assumptions, "text": spatial_text}
            if sp.radius_m is not None:
                ev["radius_m"] = sp.radius_m
            out.events.append(ev)


def parse_report(text: str, callsign_ids: dict[str, str]) -> ParsedReport:
    """Turn one radio report into partial events. callsign_ids maps "BADGER 2-4" -> "sol-10".

    The squad named first without a soldier number ("Борсук два, медик") is the speaker; their
    medic is the subject of any low-stock request. A soldier's callsign plus a severity word is a
    casualty. A sentence with supplies and a "need / running out" word is a low-stock request.
    """
    start = time.perf_counter()
    out = ParsedReport()
    speaker: Optional[str] = None  # the medic's callsign
    sentences = [s for s in (s.strip() for s in re.split(r"[.!?;\n]+", text)) if s]
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

        for cs in soldiers:
            pid = callsign_ids.get(cs)
            if pid is None:
                out.unparsed.append(f"unknown callsign {cs}")
            elif severity is None:
                out.unparsed.append(f"{cs}: no severity heard (critical / wounded)")
            else:
                ev = {"type": "CASUALTY", "subject_id": pid, "severity": severity, "callsign": cs}
                if items and needs:
                    ev["items"] = items
                out.events.append(ev)
        if items and needs and not soldiers:
            medic = next((cs for _, cs in calls if cs.endswith("-DOC")), speaker)
            pid = callsign_ids.get(medic or "")
            if pid is None:
                out.unparsed.append(f"supplies requested but no medic callsign heard: {sentence}")
            else:
                out.events.append({"type": "LOW_STOCK", "subject_id": pid, "items": items, "callsign": medic})
    _zone_events(sentences, speaker, callsign_ids, out)
    if not out.events and not out.unparsed:
        out.unparsed.append("no casualty or supply request heard")
    out.english = english_summary(out.events)
    out.parse_ms = round((time.perf_counter() - start) * 1000, 2)
    return out


def english_summary(events: list[dict]) -> str:
    parts = []
    for e in events:
        extra = ", ".join(f"{q} x {ENGLISH_ITEMS.get(k, k)}" for k, q in e.get("items", {}).items())
        if e["type"] == "CASUALTY":
            parts.append(f"{e['callsign']} is {e['severity']}" + (f", needs {extra}" if extra else ""))
        elif e["type"] in ENGLISH_ZONES:
            r = f", radius {e['radius_m']:.0f} m" if e.get("radius_m") else ""
            parts.append(f"{e['callsign']} reports {ENGLISH_ZONES[e['type']]} {e['distance_m']:.0f} m at "
                         f"{e['bearing_deg']:.0f} degrees{r}")
        else:
            parts.append(f"{e['callsign']} is running low: needs {extra}")
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
                            speaker: Optional[str] = None) -> dict:
    """Parse, broadcast the voice_report, then run each event through the normal pipeline.
    speaker is the reporter's own callsign as they'd say it ("Борсук один, медик"), for a device that
    knows who is holding it; it is read as if spoken first, so "I'm out of blood" finds their medic."""
    from . import dev_server  # late import: dev_server includes this router
    callsign_ids = {p.callsign: p.id for p in dev_server.world.repo.list_personnel()}
    parsed = parse_report(f"{speaker}. {text}" if speaker else text, callsign_ids)
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
                     speaker: Optional[str] = None):
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
    return await handle_transcript(text, lang, stt, stt_ms, speaker)


@router.post("/voice/text")
async def post_voice_text(body: dict):
    if not body.get("text"):
        raise HTTPException(422, "body needs text")
    return await handle_transcript(body["text"], body.get("language"), "text", 0.0, body.get("speaker"))
