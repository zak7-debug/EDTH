"""Launch-site stock: drones reload from it, and it reorders from the supply chain when it runs low.

How it fits the product:
- Every launch site holds real stock in the graph (STOCKS edges in TuringDB). When a drone lands
  back home, `reload()` tops it up to its standard loadout FROM that stock: the site's numbers go
  down by exactly what the drone took. If the site is short, the drone leaves with what there is,
  and the dashboard says what it is missing.
- After every change, `check()` looks for items below REORDER_POINT. If any, it places a restock
  order down the fastest working supply chain (supply_chain.best_path): the source's stock is taken
  at once, the shipment travels the chain's lead time, and on arrival the launch site's stock goes up.
- If a hub or hospital the shipment still has to pass through is destroyed, the shipment is written
  off and a new order goes out on the next-best chain straight away (`site_changed()`).
- Restock orders live here, in Python, for the demo. HOOK: to persist them, write each one as a
  (source)-[:SHIPMENT {order_id, items_json, status}]->(launch site) edge in turing_repo.py.

Searchable tags: TUNE (numbers to adjust), HOOK (integration points), DEMO (demo behaviour).
"""
from __future__ import annotations

import itertools
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from .models import ITEMS, Drone
from .supply_chain import best_path

# TUNE: a launch site reorders an item once it holds fewer than REORDER_POINT, and orders enough to
# get back to REORDER_UP_TO. DEMO: Launch Site West starts with 1 blood, so it orders on startup.
REORDER_POINT = 4
REORDER_UP_TO = 12
# DEMO / TUNE: simulated seconds per real second for restock shipments. Real lead times are 15 min
# to 12 h; at 60x one lead-time minute passes every real second, so a 35 min order lands in 35 s.
ORDER_SPEED = float(os.environ.get("EDTH_ORDER_SPEED", "60"))


@dataclass
class RestockOrder:
    order_id: str
    depot_id: str
    source_id: str
    items: dict[str, int]
    path: list[str]  # source first, launch site last
    legs: list[dict]  # [{src_id, dst_id, mode, minutes}]
    minutes: float  # total lead time
    placed_ts: float
    status: str = "IN_TRANSIT"  # then DELIVERED, or LOST (a site on its way was destroyed)
    lost_at: Optional[str] = None
    replaces: Optional[str] = None  # the lost order this one re-sends
    kind: str = "RESTOCK"  # or TEAM / CONVOY / BACKFILL: a temporary site's set-up team and first resupply (resilience.py)
    _arrive_at: dict = field(default_factory=dict, repr=False)  # node id -> lead minutes to reach it

    def __post_init__(self):
        t = 0.0
        self._arrive_at = {self.path[0]: 0.0}
        for leg in self.legs:
            t += leg["minutes"]
            self._arrive_at[leg["dst_id"]] = t

    def elapsed_min(self, now: float, speed: float) -> float:
        return (now - self.placed_ts) * speed / 60.0

    def still_to_pass(self, node_id: str, now: float, speed: float) -> bool:
        """True if the shipment hasn't reached `node_id` yet (so losing that site strands it)."""
        return node_id in self._arrive_at and node_id != self.path[0] and \
            self._arrive_at[node_id] > self.elapsed_min(now, speed)

    def to_dict(self, now: float, speed: float) -> dict:
        d = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        left = max(0.0, self.minutes - self.elapsed_min(now, speed)) if self.status == "IN_TRANSIT" else 0.0
        d["remaining_min"] = round(left, 1)
        d["remaining_s_real"] = round(left * 60 / speed, 1)  # what the dashboard counts down
        return d


class StockKeeper:
    def __init__(self, repo, clock=time.time, speed: float = ORDER_SPEED):
        self.repo = repo
        self.clock = clock
        self.speed = speed
        self.orders: dict[str, RestockOrder] = {}
        self._ids = itertools.count(1)

    # ---------------------------------------------------------------------------------------
    # Drones reloading
    # ---------------------------------------------------------------------------------------

    def reload(self, drone: Drone, loadout: dict[str, int]) -> list[dict]:
        """Top `drone` up to `loadout` from its home launch site's stock. Returns /ws messages.
        HOOK: flights.py calls this when a drone lands back home."""
        depot = next(d for d in self.repo.list_depots() if d.id == drone.depot_id)
        need = {i: q - drone.payload.get(i, 0) for i, q in loadout.items() if q > drone.payload.get(i, 0)}
        take = {i: min(q, depot.stock.get(i, 0)) for i, q in need.items()}
        take = {i: q for i, q in take.items() if q > 0}
        short = {i: q - take.get(i, 0) for i, q in need.items() if q > take.get(i, 0)}
        if take:
            self.repo.adjust_stock(depot.id, {i: -q for i, q in take.items()})  # the site's stock goes down...
            self.repo.update_drone(drone.id, payload={i: drone.payload.get(i, 0) + q for i, q in take.items()})  # ...the drone's up
        note = f"{drone.callsign} reloaded at {depot.name}"
        note += f": took {_fmt(take)}" if take else ": nothing needed" if not short else ""
        if short:
            note += f". {depot.name} is out of {_fmt(short)}, so it flies without them"
        msgs = [self.message("reload", note, depot_id=depot.id, drone_id=drone.id, taken=take, short=short)]
        return msgs + self.check(depot.id)

    # ---------------------------------------------------------------------------------------
    # Restock orders
    # ---------------------------------------------------------------------------------------

    def check(self, depot_id: str) -> list[dict]:
        """Place a restock order if `depot_id` is low on anything and has none on the way."""
        if any(o.depot_id == depot_id and o.status == "IN_TRANSIT" for o in self.orders.values()):
            return []  # TUNE: one shipment at a time per launch site keeps the demo readable
        depots = self.repo.list_depots()
        depot = next((d for d in depots if d.id == depot_id), None)
        if depot is None:
            return []  # a shipment to a hospital or temporary site: they don't reorder by themselves
        items = {i: REORDER_UP_TO - depot.stock.get(i, 0) for i in ITEMS if depot.stock.get(i, 0) < REORDER_POINT}
        if not items:
            return []
        return self._place(depot_id, items, depots)

    def check_all(self) -> list[dict]:
        out = []
        for d in self.repo.list_depots():
            out += self.check(d.id)
        return out

    def _place(self, depot_id: str, items: dict[str, int], depots=None, replaces: Optional[str] = None) -> list[dict]:
        facilities, links = self.repo.list_facilities(), self.repo.list_supply_links()
        depots = depots or self.repo.list_depots()
        names = _names(facilities, depots)
        path = best_path(depot_id, items, facilities, depots, links)
        if path is None:
            return [self.message("order_failed", f"{names[depot_id]} needs {_fmt(items)} but no working "
                                                 "supply chain holds it", depot_id=depot_id)]
        order = RestockOrder(f"ord-{next(self._ids)}", depot_id, path["source_id"], items, path["path"],
                             path["legs"], path["minutes"], self.clock(), replaces=replaces)
        self.repo.adjust_stock(order.source_id, {i: -q for i, q in items.items()})  # it leaves the source now
        self.orders[order.order_id] = order
        via = " → ".join(names[n] for n in order.path)
        return [self.message("order_placed", f"{names[depot_id]} low: ordered {_fmt(items)} via {via}, "
                                             f"{order.minutes:.0f} min", depot_id=depot_id, order_id=order.order_id)]

    def send(self, target_id: str, items: dict[str, int], path: dict, kind: str = "RESTOCK",
             take_from_source: bool = True, note: Optional[str] = None) -> list[dict]:
        """Send `items` down a chain already planned (supply_chain.best_path shape) to any site.
        HOOK: resilience.start_supplies sends a temporary site's team and first convoy this way."""
        names = _names(self.repo.list_facilities(), self.repo.list_depots())
        order = RestockOrder(f"ord-{next(self._ids)}", target_id, path["source_id"], dict(items), path["path"],
                             path["legs"], path["minutes"], self.clock(), kind=kind)
        if take_from_source and items:
            self.repo.adjust_stock(order.source_id, {i: -q for i, q in items.items()})
        self.orders[order.order_id] = order
        if note is None:
            via = " → ".join(f"{names.get(n, n)}" for n in order.path)
            note = f"{names.get(target_id, target_id)}: {_fmt(items)} sent via {via}, {order.minutes:.0f} min"
        return [self.message("order_placed", note, depot_id=target_id, order_id=order.order_id)]

    def step(self) -> list[dict]:
        """Deliver every shipment whose lead time has passed. HOOK: the tick loop calls this."""
        now, out = self.clock(), []
        for o in list(self.orders.values()):
            if o.status == "IN_TRANSIT" and o.elapsed_min(now, self.speed) >= o.minutes:
                o.status = "DELIVERED"
                if o.items:
                    self.repo.adjust_stock(o.depot_id, o.items)
                name = _names(self.repo.list_facilities(), self.repo.list_depots()).get(o.depot_id, o.depot_id)
                what = {"TEAM": "Set-up team arrived at", "CONVOY": "Resupply convoy arrived at",
                        "BACKFILL": "Refill arrived at"}.get(o.kind, "Restock arrived at")
                out.append(self.message("order_arrived", f"{what} {name}" + (f": {_fmt(o.items)}" if o.items else ""),
                                        depot_id=o.depot_id, order_id=o.order_id))
                out += self.check(o.depot_id)  # still low on something else? order that too
        return out

    def site_changed(self, facility_id: str, status: str) -> list[dict]:
        """A site was destroyed: shipments still to pass through it are lost and re-sent another way.
        HOOK: dev_server.set_site calls this after storing the new status."""
        if status != "DESTROYED":
            return []
        now, out = self.clock(), []
        for o in list(self.orders.values()):
            if o.status == "IN_TRANSIT" and o.still_to_pass(facility_id, now, self.speed):
                o.status, o.lost_at = "LOST", facility_id
                names = _names(self.repo.list_facilities(), self.repo.list_depots())
                if o.depot_id == facility_id:  # the destination itself was hit (a temporary site setting up)
                    out.append(self.message("order_lost", f"Shipment to {names[o.depot_id]} stopped: site "
                                                          "destroyed", depot_id=o.depot_id, order_id=o.order_id))
                    continue
                out.append(self.message("order_lost", f"Shipment to {names[o.depot_id]} lost at "
                                                      f"{names[facility_id]}: re-sending", depot_id=o.depot_id,
                                        order_id=o.order_id))
                out += self._place(o.depot_id, o.items, replaces=o.order_id)
        return out

    # ---------------------------------------------------------------------------------------
    # What the dashboard sees
    # ---------------------------------------------------------------------------------------

    def state(self) -> dict:
        """Every launch site's and facility's stock, beds, and the shipments on the way."""
        now = self.clock()
        return {
            "depots": [{"id": d.id, "name": d.name, "stock": d.stock} for d in self.repo.list_depots()],
            "facilities": [{"id": f.id, "stock": f.stock, "beds": f.beds, "beds_used": f.beds_used,
                            "status": f.status} for f in self.repo.list_facilities()],
            "orders": [o.to_dict(now, self.speed) for o in self.orders.values()],
            "reorder_point": REORDER_POINT, "reorder_up_to": REORDER_UP_TO, "order_speed": self.speed,
        }

    def message(self, kind: Optional[str] = None, note: Optional[str] = None, **detail) -> dict:
        """HOOK: the `stock_update` /ws message. kind/note say what just changed (None on connect)."""
        from .messages import stock_update_msg  # late import: messages imports repo types
        return stock_update_msg(self.state(), {"kind": kind, "note": note, **detail} if kind else None)


def _fmt(items: dict[str, int]) -> str:
    return ", ".join(f"{q} {i.replace('_', ' ')}" for i, q in items.items())


def _names(facilities, depots) -> dict[str, str]:
    return {**{f.id: f.name for f in facilities}, **{d.id: d.name for d in depots}}
