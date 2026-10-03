# TuringDB: slide content and demo talking points

Ollie's Sunday task, drafted 2026-10-03. Every number here was measured on the TuringDB in-memory server (see docs/turingdb-notes.md) and shows live in the dashboard's Graph queries panel.

## The slide

**Title:** One graph from the donor hub to the medic's pouch

**Left: the model (two linked graphs)**
- *Personnel:* squads, soldiers and medics, with each medic's stock and the threshold that triggers a resupply.
- *Logistics:* suppliers, distribution hubs, hospitals, launch sites, drones, what each drone carries, and threat zones.
- Edges carry the facts that matter: `CARRIES {qty}`, `STOCKS {qty}`, `SUPPLIES {lead_time_min, mode}`, `DISPATCHED_TO {eta, route, status}`.

**Right: three numbers**
- **1 query** finds every free drone carrying the whole kit (drone → CARRIES → item, filtered on status).
- **About 15 ms** for the full decision on TuringDB: that query, route and battery maths, and an atomic claim.
- **About 30 ms** to re-plan every launch site's supply chain when a hub is destroyed.

**Footer line:** Writes happen after the map is told, so recording a dispatch never slows the decision down.

## What to say while the Graph queries panel is on screen

1. "This is what TuringDB just ran for that casualty." Point at the first query. "One pattern match: every idle drone and what it carries. Twenty-one rows back in about 5 ms."
2. "Then two tiny queries claim the drone: read its state, set it to en route. That's how two emergencies at the same instant never get the same drone."
3. "Below the line are the writes after the drone was already on its way: the dispatch edge and the casualty's status. They're off the clock."
4. When the forward hub is destroyed: "Its status flips in the graph, and every launch site's chain is re-planned by walking the supply edges backwards. The fastest route that avoids the destroyed hub wins. Thirty milliseconds."

## Why a graph (one line each, for questions)

- The question "who can get blood to this medic fastest" is a path question across suppliers, hubs, launch sites and drones. A graph answers it directly; tables need a join per hop.
- Adding a new kind of node (a field hospital, a cargo drone relay) is a new label and edge, with no schema migration in the middle of an operation.
- The same graph holds the audit trail: every dispatch is an edge with its ETA, route and outcome, including drones lost.

## Honest limits (if a judge asks)

- TuringDB has no write-conflict detection, so one backend process owns all writes and claims a drone under a lock.
- Every change keeps its history, so memory grows during a long run. We restart the database before each run (docs/turingdb-notes.md, soak test).
- Routes round threat zones are computed in Python from the zones stored in the graph; TuringDB doesn't do geometry.

## Reproducing the numbers

- Decision timing: run the demo and read the header's latency counter, or the Graph queries panel.
- Soak test: start `turingdb start -demon -in-memory`, then run a loop of `engine.handle` → `engine.record` → `repo.complete_dispatch` → `engine.drone_freed`, printing `ps -C turingdb -o rss=` every 100 cycles.
