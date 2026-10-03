"""(Re)create the `personnel` and `logistics` graphs in a running TuringDB and load seed.py.

    turingdb start -demon -in-memory
    python scripts/load_turingdb.py
Then browse them with `turingdb start -demon -in-memory -ui` on http://localhost:8080.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.app.seed import load_seed  # noqa: E402
from backend.app.turing_repo import TuringRepo  # noqa: E402

t = time.perf_counter()
repo = TuringRepo.from_env()
repo.load_seed(load_seed())
print(f"seeded in {(time.perf_counter() - t) * 1000:.0f} ms: "
      f"{len(repo.list_personnel())} personnel, {len(repo.list_drones())} drones, "
      f"{len(repo.list_depots())} depots, {len(repo.list_no_fly_zones())} no-fly zones")
t = time.perf_counter()
c = repo.find_candidate_drones({"tourniquet": 1, "blood_oneg": 2, "hemostatic_gauze": 1})
print(f"candidate query for a CRITICAL casualty: {[d.id for d in c]} "
      f"in {(time.perf_counter() - t) * 1000:.1f} ms")
