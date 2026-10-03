"""Event simulator: posts events to the running API over HTTP (never imports the backend).

Because it only speaks HTTP, real telemetry can replace it later without touching the server.

Usage (server must be running, e.g. uvicorn backend.app.dev_server:app --port 8000):
    python sim/simulator.py --scenario demo            # server-side scripted demo (POST /scenario/demo)
    python sim/simulator.py --scenario plan --seed 7   # the plan's story, posted from here, same every run
    python sim/simulator.py --random --seed 7 --duration 60 --mean-gap 6
    python sim/simulator.py --reset                    # reseed the world first, then run whatever else is given

Event bodies are partial on purpose: the server fills event_id, ts and position (see _complete()
in dev_server.py), so we send only type, subject_id, severity and items.
"""
from __future__ import annotations

import argparse
import random
import sys
import time

import httpx

ITEMS = ["tourniquet", "blood_oneg", "chest_seal", "hemostatic_gauze", "morphine_autoinjector"]


def people(client: httpx.Client) -> tuple[list[dict], list[dict]]:
    """Return (soldiers, medics) from GET /state, ordered by id so a seed gives the same picks."""
    state = client.get("/state").json()
    pers = sorted(state.get("personnel", []), key=lambda p: p["id"])
    soldiers = [p for p in pers if p["id"].startswith("sol-")]
    medics = [p for p in pers if p["id"].startswith("med-")]
    if not soldiers or not medics:
        sys.exit("no soldiers/medics in /state; is the server seeded?")
    return soldiers, medics


def unit_of(p: dict):
    return p.get("unit_id") or p.get("unit")


def post(client: httpx.Client, body: dict) -> None:
    t0 = time.perf_counter()
    try:
        r = client.post("/events", json=body)
        ms = (time.perf_counter() - t0) * 1000
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        # a Dispatch carries drone_id; a NoDispatch carries reason
        outcome = data.get("drone_id") or data.get("reason") or data.get("detail") or r.status_code
        print(f"{body['type']:<9} {body['subject_id']:<7} {body.get('severity', ''):<9} -> {outcome} ({ms:.0f} ms round trip)")
    except httpx.HTTPError as e:
        print(f"post failed: {e}")  # keep going; one bad post must not stop a rehearsal


def plan_scenario(client: httpx.Client, seed: int) -> None:
    """The plan's story: +5 s a soldier goes CRITICAL; +5.2 s a nearby medic reports low blood;
    +6 s a second CRITICAL in another squad."""
    rng = random.Random(seed)
    soldiers, medics = people(client)
    first = rng.choice(soldiers)
    near = [m for m in medics if unit_of(m) is not None and unit_of(m) == unit_of(first)]
    medic = rng.choice(near or medics)
    others = [s for s in soldiers if unit_of(s) != unit_of(first)] or [s for s in soldiers if s["id"] != first["id"]]
    second = rng.choice(others)
    script = [
        (5.0, {"type": "CASUALTY", "subject_id": first["id"], "severity": "CRITICAL"}),
        (5.2, {"type": "LOW_STOCK", "subject_id": medic["id"], "items": {"blood_oneg": 2}}),
        (6.0, {"type": "CASUALTY", "subject_id": second["id"], "severity": "CRITICAL"}),
    ]
    start = time.monotonic()
    for at, body in script:
        time.sleep(max(0.0, at - (time.monotonic() - start)))
        post(client, body)


def random_mode(client: httpx.Client, seed: int, duration: float, mean_gap: float) -> None:
    rng = random.Random(seed)
    soldiers, medics = people(client)
    end = time.monotonic() + duration
    while time.monotonic() < end:
        time.sleep(rng.expovariate(1.0 / mean_gap))
        if rng.random() < 0.7:
            post(client, {"type": "CASUALTY", "subject_id": rng.choice(soldiers)["id"],
                          "severity": rng.choice(["WOUNDED", "CRITICAL"])})
        else:
            item = rng.choice(ITEMS)
            post(client, {"type": "LOW_STOCK", "subject_id": rng.choice(medics)["id"], "items": {item: 1}})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--scenario", choices=["demo", "plan"])
    ap.add_argument("--random", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--duration", type=float, default=60.0, help="seconds, random mode")
    ap.add_argument("--mean-gap", type=float, default=6.0, help="mean seconds between events, random mode")
    ap.add_argument("--reset", action="store_true", help="POST /reset before running")
    args = ap.parse_args()

    with httpx.Client(base_url=args.url, timeout=10.0) as client:
        try:
            client.get("/health").raise_for_status()
        except httpx.HTTPError as e:
            sys.exit(f"server not reachable at {args.url}: {e}")
        if args.reset:
            client.post("/reset")
            print("world reset")
        if args.scenario == "demo":
            print(client.post("/scenario/demo").json())  # the server runs its own script
        elif args.scenario == "plan":
            plan_scenario(client, args.seed)
        elif args.random:
            random_mode(client, args.seed, args.duration, args.mean_gap)
        elif not args.reset:
            ap.print_help()


if __name__ == "__main__":
    main()
