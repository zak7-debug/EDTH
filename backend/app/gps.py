"""Live geolocation: people's GPS fixes move them on the map, and geolocated field reports become zones.

How it fits the product:
- POST /positions (dev_server.py) takes real fixes from devices: apply_positions() writes each person's
  lat / lon to the graph and the server broadcasts one `positions` message (contracts/messages.md).
- LiveGps is a simulated feed for the demo, off by default (POST /geo/live {"on": true}). While on, the
  dev_server tick loop calls step() every STEP_S: a few unhurt people take a small random-walk step,
  staying near their unit and never walking into a no-fly zone. Casualties and admitted people stay put.
- Every REPORT_EVERY_S it also makes a geolocated field report (next_report()): a road block or an air
  threat seen from an unhurt soldier's current position. dev_server.process() sends it through the same
  geo pipeline as a spoken report (geo/), so it becomes a zone that routing avoids and that expires
  after LIVE_ZONE_TTL_S.

TuringDB writes cost about 60 ms each, so a step moves only MOVERS_PER_STEP people.

Searchable tags: TUNE (numbers to adjust), HOOK (integration points), DEMO (demo behaviour).
"""
from __future__ import annotations

import math
import random
import time
from typing import Iterable, Optional

from .geo.project import bearing_deg, distance_m, project
from .routing import Router

STEP_S = 3.0  # TUNE: how often the simulated feed moves people
MOVERS_PER_STEP = 8  # TUNE: people moved per step (one graph write each)
STEP_MIN_M, STEP_MAX_M = 20.0, 60.0  # TUNE: length of one random-walk step
LEASH_M = 400.0  # TUNE: nobody wanders further than this from their unit's starting centre
REPORT_EVERY_S = 45.0  # TUNE: a geolocated field report this often while live
REPORT_MIN_M, REPORT_MAX_M = 300.0, 900.0  # TUNE: how far from the reporter the reported zone is
REPORT_KINDS = ("ROAD_BLOCKED", "NO_FLY_ZONE")  # DEMO: alternated, road block first
LIVE_ZONE_TTL_S = 240.0  # TUNE: zones from the live feed expire this soon, so the map doesn't fill up
MOVABLE = ("OK",)  # only unhurt people move


def apply_positions(repo, positions: Iterable[dict]) -> tuple[list[dict], list]:
    """HOOK: real GPS fixes. Writes each known person's lat / lon; returns (moved, skipped ids).
    A fix with a missing or non-finite lat / lon is skipped like an unknown id."""
    known = {p.id for p in repo.list_personnel()}
    moved, skipped = [], []
    for fix in positions:
        pid = fix.get("id") if isinstance(fix, dict) else None
        try:
            lat, lon = float(fix["lat"]), float(fix["lon"])
        except (TypeError, KeyError, ValueError):
            skipped.append(pid)
            continue
        if pid not in known or not (math.isfinite(lat) and math.isfinite(lon)):
            skipped.append(pid)
            continue
        repo.update_person(pid, lat=lat, lon=lon)
        moved.append({"id": pid, "lat": lat, "lon": lon})
    return moved, skipped


class LiveGps:
    """The simulated feed. dev_server.World owns one, so POST /reset turns it off."""

    def __init__(self, rng: Optional[random.Random] = None):
        self.on = False
        self.rng = rng or random.Random()
        self.centres: dict[str, tuple[float, float]] = {}  # unit_id -> where the unit started
        self.next_step = 0.0
        self.next_report = 0.0
        self.reports = 0

    def set(self, on: bool, people=(), now: Optional[float] = None) -> dict:
        now = time.monotonic() if now is None else now
        if on and not self.on:
            self.next_step = now + STEP_S
            self.next_report = now + REPORT_EVERY_S / 3  # DEMO: the first report comes quickly
            if not self.centres:
                self.centres = unit_centres(people)
        self.on = bool(on)
        return self.state()

    def state(self) -> dict:
        return {"on": self.on, "step_s": STEP_S, "movers_per_step": MOVERS_PER_STEP,
                "report_every_s": REPORT_EVERY_S, "reports": self.reports}

    def due(self, now: float) -> tuple[bool, bool]:
        """(step now?, report now?) and moves the timers on."""
        if not self.on:
            return False, False
        step = now >= self.next_step
        report = now >= self.next_report
        if step:
            self.next_step = now + STEP_S
        if report:
            self.next_report = now + REPORT_EVERY_S
        return step, report

    def step(self, repo, busy: Iterable[str] = ()) -> list[dict]:
        """Move up to MOVERS_PER_STEP unhurt people one random-walk step each; returns their new positions.
        busy: ids not to move (a medic with a drone on the way lands where the drone is headed)."""
        busy = set(busy)
        people = [p for p in repo.list_personnel() if p.status in MOVABLE and p.id not in busy]
        if not self.centres:
            self.centres = unit_centres(repo.list_personnel())
        router = Router(repo.list_no_fly_zones())
        moved = []
        for p in self.rng.sample(people, min(MOVERS_PER_STEP, len(people))):
            to = self._walk(p, router)
            if to is None:
                continue
            repo.update_person(p.id, lat=to[0], lon=to[1])
            moved.append({"id": p.id, "lat": to[0], "lon": to[1]})
        return moved

    def _walk(self, p, router: Router, tries: int = 6) -> Optional[tuple[float, float]]:
        here = (p.lat, p.lon)
        centre = self.centres.get(p.unit_id, here)
        for _ in range(tries):
            d = self.rng.uniform(STEP_MIN_M, STEP_MAX_M)
            # Drift back towards the centre when near the leash, otherwise any direction.
            if distance_m(here, centre) > LEASH_M * 0.7:
                b = (bearing_deg(here, centre) + self.rng.uniform(-60, 60)) % 360
            else:
                b = self.rng.uniform(0, 360)
            to = project(here[0], here[1], d, b)
            to = (round(to[0], 6), round(to[1], 6))
            if distance_m(to, centre) > max(LEASH_M, distance_m(here, centre)):
                continue  # beyond the leash (someone who started further out may only come closer)
            if router.zones and (not router.clear(here, to) or in_zone(router, to)):
                continue
            return to
        return None

    def make_report(self, repo) -> Optional[dict]:
        """A geolocated field report from a random unhurt soldier, as the geo pipeline takes it (the same
        fields voice._zone_events sends for a spoken zone). Alternates REPORT_KINDS."""
        soldiers = [p for p in repo.list_personnel() if p.kind == "SOLDIER" and p.status in MOVABLE]
        if not soldiers:
            return None
        who = self.rng.choice(soldiers)
        kind = REPORT_KINDS[self.reports % len(REPORT_KINDS)]
        self.reports += 1
        return {"type": kind, "subject_id": who.id, "callsign": who.callsign,
                "distance_m": round(self.rng.uniform(REPORT_MIN_M, REPORT_MAX_M)),
                "bearing_deg": round(self.rng.uniform(0, 360)), "expires_s": LIVE_ZONE_TTL_S,
                "assumptions": ["live GPS report"]}


def unit_centres(people) -> dict[str, tuple[float, float]]:
    """Each unit's mean position: the anchor the random walk stays near."""
    acc: dict[str, list[float]] = {}
    for p in people:
        a = acc.setdefault(p.unit_id, [0.0, 0.0, 0])
        a[0] += p.lat
        a[1] += p.lon
        a[2] += 1
    return {u: (a[0] / a[2], a[1] / a[2]) for u, a in acc.items()}


def in_zone(router: Router, pt: tuple[float, float]) -> bool:
    """True if pt is inside any of the router's zones."""
    from .routing import _inside
    xy = router.flat.xy(pt)
    return any(_inside(xy, poly) for poly in router._polys)
