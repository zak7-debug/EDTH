"""Spoken position -> distance (metres) and bearing (degrees true). Ukrainian first, English as well.

    "дрон за вісімсот метрів на північний схід"      -> 800 m, 45
    "на північному сході, півтора кілометра"          -> 1500 m, 45
    "азимут двісті сімдесят, два кілометри"           -> 2000 m, 270
    "на третій годині, триста метрів" (heading 10)    -> 300 m, 100   (clock = relative to heading)
    "закрити п'ятсот метрів"                          -> radius 500 m

Rules (README_audio_geolocation.md, step 1):
- Compass words and numeric azimuths are absolute. Clock positions are relative to the reporter's
  heading; with no heading the bearing is unknown and the result is `needs_confirmation`.
- A missing distance becomes DEFAULT_DISTANCE_M and is listed in `assumptions`.
- Rule based on purpose: offline, sub-millisecond and the same every rehearsal (like voice.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_DISTANCE_M = 500.0  # TUNE: used when a direction is heard but no distance

# ---- numbers --------------------------------------------------------------------------------------
# Cardinals with the case forms heard after "за" / "на" / "в радіусі" ("за двісті метрів", "в радіусі
# п'ятисот метрів"). Values below 1000 add up; "тисяча" multiplies what came before it.
UNITS = {
    "нуль": 0, "один": 1, "одна": 1, "одне": 1, "одного": 1, "одну": 1, "одній": 1,
    "два": 2, "дві": 2, "двох": 2, "три": 3, "трьох": 3, "чотири": 4, "чотирьох": 4,
    "п'ять": 5, "п'яти": 5, "шість": 6, "шести": 6, "сім": 7, "семи": 7, "вісім": 8, "восьми": 8,
    "дев'ять": 9, "дев'яти": 9,
    "десять": 10, "десяти": 10, "одинадцять": 11, "одинадцяти": 11, "дванадцять": 12, "дванадцяти": 12,
    "тринадцять": 13, "тринадцяти": 13, "чотирнадцять": 14, "чотирнадцяти": 14,
    "п'ятнадцять": 15, "п'ятнадцяти": 15, "шістнадцять": 16, "шістнадцяти": 16,
    "сімнадцять": 17, "сімнадцяти": 17, "вісімнадцять": 18, "вісімнадцяти": 18,
    "дев'ятнадцять": 19, "дев'ятнадцяти": 19,
    "zero": 0, "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
TENS = {
    "двадцять": 20, "двадцяти": 20, "тридцять": 30, "тридцяти": 30, "сорок": 40, "сорока": 40,
    "п'ятдесят": 50, "п'ятдесяти": 50, "шістдесят": 60, "шістдесяти": 60, "сімдесят": 70, "сімдесяти": 70,
    "вісімдесят": 80, "вісімдесяти": 80, "дев'яносто": 90,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90,
}
HUNDREDS = {
    "сто": 100, "ста": 100, "двісті": 200, "двохсот": 200, "триста": 300, "трьохсот": 300,
    "чотириста": 400, "чотирьохсот": 400, "п'ятсот": 500, "п'ятисот": 500, "шістсот": 600,
    "шестисот": 600, "сімсот": 700, "семисот": 700, "вісімсот": 800, "восьмисот": 800,
    "дев'ятсот": 900, "дев'ятисот": 900,
}
THOUSAND = ("тисяча", "тисячі", "тисяч", "тисячу", "thousand")
HUNDRED_EN = ("hundred",)
FRACTIONS = {"півтора": 1.5, "півтори": 1.5, "пів": 0.5, "половина": 0.5, "half": 0.5}
FILLERS = ("і", "й", "та", "and")  # "двісті і п'ятдесят", "two hundred and fifty"

# ---- units ----------------------------------------------------------------------------------------
METRES = {"м", "метр", "метри", "метрів", "метра", "метрах", "метрами", "m", "metre", "metres",
          "meter", "meters"}
KILOMETRES = {"км", "кілометр", "кілометри", "кілометрів", "кілометра", "кілометрах", "km", "kilometre",
              "kilometres", "kilometer", "kilometers", "klick", "klicks", "click", "clicks"}
HALF_KM = {"півкілометра", "півкілометр"}  # one word: "за півкілометра"
RADIUS_CUES = ("закри", "закрий", "радіус", "радіусі", "radius", "close", "закрити", "оточ")
HERE_CUES = ("тут", "над", "overhead", "here")  # "над нами", "right here": zone on the reporter

# ---- directions -----------------------------------------------------------------------------------
# Stems cover the forms a speaker uses: "на північ", "на півночі", "північніше", "північно-східний".
# "заход" / "сход" are exact words only: "заходити" (to go in) must not read as west.
COMPASS_STEMS = (("північ", 0), ("півноч", 0), ("півден", 180), ("півдн", 180), ("схід", 90),
                 ("захід", 270), ("north", 0), ("south", 180), ("east", 90), ("west", 270))
COMPASS_EXACT = {"сході": 90, "сходу": 90, "сходом": 90, "заході": 270, "заходу": 270, "заходом": 270}
COMPOUND_EN = {"northeast": 45, "northeastern": 45, "ne": 45, "southeast": 135, "southeastern": 135,
               "se": 135, "southwest": 225, "southwestern": 225, "sw": 225, "northwest": 315,
               "northwestern": 315, "nw": 315}
AZIMUTH_CUES = ("азимут", "bearing", "azimuth", "пеленг")
DEGREE_WORDS = ("градус", "degree", "°")
CLOCK_HOURS = (("одинадцят", 11), ("дванадцят", 12), ("перш", 1), ("друг", 2), ("трет", 3),
               ("четверт", 4), ("п'ят", 5), ("шост", 6), ("сьом", 7), ("восьм", 8), ("дев'ят", 9),
               ("десят", 10))
CLOCK_WORDS = ("годин", "o'clock", "oclock")


@dataclass
class SpatialParse:
    distance_m: Optional[float] = None
    bearing_deg: Optional[float] = None  # degrees true
    radius_m: Optional[float] = None
    bearing_source: Optional[str] = None  # compass / azimuth / clock / here
    assumptions: list[str] = field(default_factory=list)
    needs_confirmation: bool = False
    reasons: list[str] = field(default_factory=list)  # why it needs confirmation

    @property
    def located(self) -> bool:
        return self.distance_m is not None and self.bearing_deg is not None

    def to_dict(self) -> dict:
        return {"distance_m": self.distance_m, "bearing_deg": self.bearing_deg, "radius_m": self.radius_m,
                "bearing_source": self.bearing_source, "assumptions": self.assumptions,
                "needs_confirmation": self.needs_confirmation, "reasons": self.reasons}


def tokens(text: str) -> list[str]:
    s = text.lower().replace("’", "'").replace("ʼ", "'").replace("`", "'").replace("°", " градусів ")
    s = re.sub(r"(\d),(\d)", r"\1.\2", s)  # "1,5 км" -> "1.5 км"
    s = re.sub(r"(\d)(км|km|м|m)\b", r"\1 \2", s)  # "800м" -> "800 м"
    s = re.sub(r"(?<=\w)[-–—](?=\w)", " ", s)  # "північно-східний" -> two words
    # Punctuation stays as "," so a pause ends a number: "азимут двісті сімдесят, два кілометри" is
    # 270 and 2 km, not 272 km.
    return re.findall(r"\d+(?:\.\d+)?|o'clock|[^\W\d_]+(?:'[^\W\d_]+)*|[,.;:!?]", s)


def _is_numberish(t: str) -> bool:
    return (re.fullmatch(r"\d+(?:\.\d+)?", t) is not None or t in UNITS or t in TENS or t in HUNDREDS
            or t in THOUSAND or t in HUNDRED_EN or t in FRACTIONS)


def read_number(toks: list[str], i: int) -> tuple[Optional[float], int]:
    """Number starting at toks[i]: (value, index after it), or (None, i)."""
    if i >= len(toks):
        return None, i
    t = toks[i]
    if re.fullmatch(r"\d+(?:\.\d+)?", t):
        val, j = float(t), i + 1
        if j < len(toks) and toks[j] in THOUSAND:
            val, j = val * 1000, j + 1
        return val, j
    if t in FRACTIONS:  # "half a kilometre": the article belongs to the fraction
        skip = 2 if i + 1 < len(toks) and toks[i + 1] in ("a", "an") else 1
        return FRACTIONS[t], i + skip
    total, current, j, seen = 0.0, 0.0, i, False
    while j < len(toks):
        t = toks[j]
        if t in FILLERS and seen and j + 1 < len(toks) and _is_numberish(toks[j + 1]):
            j += 1
            continue
        if t in HUNDREDS:
            current += HUNDREDS[t]
        elif t in TENS:
            current += TENS[t]
        elif t in UNITS and not (t in ("a", "an") and seen):
            current += UNITS[t]
        elif t in HUNDRED_EN and seen:
            current = (current or 1) * 100
        elif t in THOUSAND:
            total += (current or 1) * 1000
            current = 0.0
        else:
            break
        seen, j = True, j + 1
    if not seen:
        return None, i
    return total + current, j


def _distances(toks: list[str]) -> list[tuple[int, float]]:
    """(index of the number, metres) for every distance heard."""
    out = []
    for i, t in enumerate(toks):
        if t in HALF_KM:
            out.append((i, 500.0))
            continue
        if t not in METRES and t not in KILOMETRES:
            continue
        scale = 1000.0 if t in KILOMETRES else 1.0
        # walk back over the number words before the unit
        start = i
        while start > 0 and _is_numberish(toks[start - 1]) or (
                start > 1 and toks[start - 1] in FILLERS and _is_numberish(toks[start - 2])):
            start -= 1
        val, end = read_number(toks, start) if start < i else (None, i)
        if val is not None and end == i:
            out.append((start, val * scale))
        elif scale == 1000.0 and t in ("кілометр", "kilometre", "kilometer", "km"):
            out.append((i, 1000.0))  # "за кілометр на північ"
    return out


def _compass(toks: list[str]) -> Optional[float]:
    def base(t: str) -> Optional[float]:
        if t in COMPOUND_EN:
            return float(COMPOUND_EN[t])
        if t in COMPASS_EXACT:
            return COMPASS_EXACT[t]
        return next((v for stem, v in COMPASS_STEMS if t.startswith(stem)), None)

    for i, t in enumerate(toks):
        b = base(t)
        if b is None:
            continue
        nxt = base(toks[i + 1]) if i + 1 < len(toks) else None
        if b in (0, 180) and nxt in (90, 270):  # "північний схід", "north east", "південно-західний"
            return float({(0, 90): 45, (180, 90): 135, (180, 270): 225, (0, 270): 315}[(b, nxt)])
        return float(b)
    return None


def _azimuth(toks: list[str]) -> Optional[float]:
    for i, t in enumerate(toks):
        if t.startswith(AZIMUTH_CUES):
            val, _ = read_number(toks, i + 1)
            if val is not None:
                return val % 360
        if t.startswith(DEGREE_WORDS) and i > 0:
            start = i
            while start > 0 and _is_numberish(toks[start - 1]):
                start -= 1
            val, end = read_number(toks, start)
            if val is not None and end == i:
                return val % 360
    return None


def _clock(toks: list[str]) -> Optional[int]:
    for i, t in enumerate(toks):
        if not t.startswith(CLOCK_WORDS) or i == 0:
            continue
        prev = toks[i - 1]
        if prev.isdigit() and 1 <= int(prev) <= 12:
            return int(prev)
        if prev in UNITS and 1 <= UNITS[prev] <= 12:  # "at three o'clock"
            return UNITS[prev]
        hour = next((h for stem, h in CLOCK_HOURS if prev.startswith(stem)), None)
        if hour is not None:
            return hour
    return None


def parse_spatial(text: str, heading_deg: Optional[float] = None,
                  default_distance_m: float = DEFAULT_DISTANCE_M) -> SpatialParse:
    """Distance, bearing and optional radius from one report. heading_deg is the reporter's heading,
    degrees true (needed only for clock positions)."""
    toks = tokens(text)
    out = SpatialParse()

    dists = _distances(toks)
    radius_idx = None
    for k, (idx, metres) in enumerate(dists):
        if any(toks[j].startswith(RADIUS_CUES) for j in range(max(0, idx - 3), idx)):
            out.radius_m, radius_idx = metres, k
            break
    rest = [m for k, (_, m) in enumerate(dists) if k != radius_idx]
    if rest:
        out.distance_m = rest[0]

    azimuth = _azimuth(toks)
    compass = _compass(toks)
    hour = _clock(toks)
    if azimuth is not None:
        out.bearing_deg, out.bearing_source = azimuth, "azimuth"
    elif compass is not None:
        out.bearing_deg, out.bearing_source = compass, "compass"
    elif hour is not None:
        if heading_deg is None:
            out.bearing_source = "clock"
            out.needs_confirmation = True
            out.reasons.append(f"clock position ({hour} o'clock) heard but no heading from the device")
        else:
            out.bearing_deg, out.bearing_source = (heading_deg + (hour % 12) * 30) % 360, "clock"

    if out.bearing_deg is None and out.distance_m is None and any(t in HERE_CUES for t in toks):
        out.distance_m, out.bearing_deg, out.bearing_source = 0.0, 0.0, "here"
    if out.distance_m is None and out.bearing_deg is not None:
        out.distance_m = default_distance_m
        out.assumptions.append(f"no distance heard: assumed {default_distance_m:.0f} m")
    if out.bearing_deg is None and not out.reasons:
        out.needs_confirmation = True
        out.reasons.append("no direction heard")
    return out
