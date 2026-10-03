"""TuringRepo: GraphRepo backed by two TuringDB graphs, `personnel` and `logistics`.

Schema (also in contracts/schema.md):
  personnel:  (:Soldier|:Medic {id, kind, callsign, unit_id, lat, lon, status, last_update,
                                medics also stock_<item>, threshold_<item>})
              (:Unit {id, callsign})
              (soldier)-[:MEMBER_OF]->(unit), (medic)-[:ATTACHED_TO]->(unit)
  logistics:  (:Drone {id, callsign, depot_id, lat, lon, speed_mps, range_m, max_range_m,
                       capacity, status, claimed_by})          claimed_by '' means unclaimed
              (:Depot {id, name, lat, lon}), (:SupplyItem {id}), (:NoFlyZone {id, name, polygon_json})
              (:Recipient {id})  stand-in for a person, since edges cannot cross graphs
              (drone)-[:BASED_AT]->(depot), (drone)-[:CARRIES {qty}]->(item)
              (drone)-[:DISPATCHED_TO {request_id, eta_s, distance_m, ts, latency_ms, status,
                                       items_json, route_json}]->(recipient)

TuringDB facts this relies on (verified in scripts/turingdb_smoke.py, see docs/turingdb-notes.md):
- Every write runs inside a change: CHANGE NEW, queries, COMMIT, CHANGE SUBMIT. A node created
  in a change must be COMMITted before a later query in the same change can MATCH it.
- No conflict detection between changes (last writer wins), so claim_drone uses a Python lock.
- The Python client keeps graph/change as client state and is not thread-safe, so every call
  goes through one lock and sets its graph first.
- Run the server with `turingdb start -demon -in-memory`: a write is ~6 ms instead of ~80 ms,
  because a disk-backed server rewrites the graph file on every submit.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Iterable, Optional

from turingdb import TuringDB

from .models import ITEMS, Depot, Dispatch, Drone, NoFlyZone, Person, Unit
from .seed import SeedData

PERSONNEL = "personnel"
LOGISTICS = "logistics"

_DRONE_FIELDS = ("id", "callsign", "depot_id", "lat", "lon", "speed_mps", "range_m",
                 "max_range_m", "capacity", "status", "claimed_by")
_PERSON_FIELDS = ("id", "kind", "callsign", "unit_id", "lat", "lon", "status", "last_update")


def lit(v) -> str:
    """Python value -> Cypher literal. None is stored as '' (TuringDB has no null writes)."""
    if v is None:
        return "''"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{s}'"


# TuringDB types each property name strictly (an Int64 property can't later be SET to a
# Double), so numeric fields are always written with the same Python type.
FLOAT_FIELDS = {"lat", "lon", "speed_mps", "range_m", "max_range_m", "last_update",
                "eta_s", "distance_m", "ts", "latency_ms", "delivered_ts"}
INT_FIELDS = {"capacity", "qty"}


def typed(k: str, v):
    if v is None:
        return v
    if k in FLOAT_FIELDS:
        return float(v)
    if k in INT_FIELDS or k.startswith(("stock_", "threshold_")):
        return int(v)
    return v


def props(d: dict) -> str:
    return "{" + ", ".join(f"{k}: {lit(typed(k, v))}" for k, v in d.items()) + "}"


def sets(alias: str, d: dict) -> str:
    return ", ".join(f"{alias}.{k} = {lit(typed(k, v))}" for k, v in d.items())


def var(node_id: str) -> str:
    return "n_" + node_id.replace("-", "_")


def _rows(df) -> list[dict]:
    """DataFrame -> list of dicts keyed by bare property name ('d.lat' -> 'lat')."""
    cols = [c.split(".", 1)[-1] for c in df.columns]
    out = []
    for values in df.itertuples(index=False, name=None):
        out.append({c: (None if _isna(v) else v) for c, v in zip(cols, values)})
    return out


def _isna(v) -> bool:
    try:
        return v is None or v != v  # NaN / pd.NA-safe enough for scalars
    except (TypeError, ValueError):
        return False


class TuringRepo:
    def __init__(self, client: TuringDB, graph_prefix: str = ""):
        self.db = client
        self.g_personnel = graph_prefix + PERSONNEL
        self.g_logistics = graph_prefix + LOGISTICS
        self._lock = threading.RLock()

    @classmethod
    def from_env(cls) -> "TuringRepo":
        mode = os.environ.get("TURINGDB_MODE", "json")
        prefix = os.environ.get("TURINGDB_GRAPH_PREFIX", "")
        if mode == "embedded":
            client = TuringDB(type="embedded", data_dir=os.environ.get("TURINGDB_DATA_DIR"))
        else:
            client = TuringDB(type=mode, host=os.environ.get("TURINGDB_HOST", "http://localhost:6666"))
        return cls(client, prefix)

    # low-level helpers
    def _read(self, graph: str, q: str):
        with self._lock:
            self.db.set_graph(graph)
            return self.db.query(q)

    def _write(self, graph: str, queries: Iterable[str]) -> None:
        """Run queries in one change and submit. COMMIT after each so later ones can MATCH."""
        with self._lock:
            self.db.set_graph(graph)
            self.db.new_change()  # also checks the client out onto the new change
            try:
                for q in queries:
                    self.db.query(q)
                    self.db.query("COMMIT")
                self.db.query("CHANGE SUBMIT")
            finally:
                self.db.checkout()

    # seeding
    def _reset_graph(self, name: str) -> None:
        try:
            self.db.create_graph(name)
            return
        except Exception:
            pass  # exists already: load it (if needed) and wipe it
        try:
            self.db.load_graph(name, raise_if_loaded=False)
        except Exception:
            pass
        self._write(name, ["MATCH (n) DETACH DELETE n"])

    def load_seed(self, seed: SeedData) -> None:
        with self._lock:
            self._reset_graph(self.g_personnel)
            self._reset_graph(self.g_logistics)

            parts = [f"({var(u.id)}:Unit {props({'id': u.id, 'callsign': u.callsign})})" for u in seed.units]
            for p in seed.personnel:
                label = "Medic" if p.kind == "MEDIC" else "Soldier"
                parts.append(f"({var(p.id)}:{label} {props(self._person_props(p))})")
                rel = "ATTACHED_TO" if p.kind == "MEDIC" else "MEMBER_OF"
                parts.append(f"({var(p.id)})-[:{rel}]->({var(p.unit_id)})")
            self._write(self.g_personnel, ["CREATE " + ", ".join(parts)])

            parts = [f"({var('item-' + i)}:SupplyItem {props({'id': i})})" for i in ITEMS]
            for dep in seed.depots:
                parts.append(f"({var(dep.id)}:Depot {props({'id': dep.id, 'name': dep.name, 'lat': dep.lat, 'lon': dep.lon})})")
            for z in seed.no_fly_zones:
                parts.append(f"({var(z.id)}:NoFlyZone {props({'id': z.id, 'name': z.name, 'polygon_json': json.dumps(z.polygon)})})")
            for d in seed.drones:
                parts.append(f"({var(d.id)}:Drone {props({f: getattr(d, f) for f in _DRONE_FIELDS})})")
                parts.append(f"({var(d.id)})-[:BASED_AT]->({var(d.depot_id)})")
                for item, qty in d.payload.items():
                    parts.append(f"({var(d.id)})-[:CARRIES {{qty: {int(qty)}}}]->({var('item-' + item)})")
            self._write(self.g_logistics, ["CREATE " + ", ".join(parts)])

    @staticmethod
    def _person_props(p: Person) -> dict:
        out = {f: getattr(p, f) for f in _PERSON_FIELDS}
        if p.kind == "MEDIC":
            for i in ITEMS:
                out[f"stock_{i}"] = int(p.stock.get(i, 0))
                out[f"threshold_{i}"] = int(p.stock_threshold.get(i, 0))
        return out

    # personnel
    def _query_people(self, label: str, where: str = "") -> list[Person]:
        cols = [f"p.{f}" for f in _PERSON_FIELDS]
        if label == "Medic":
            cols += [f"p.stock_{i}" for i in ITEMS] + [f"p.threshold_{i}" for i in ITEMS]
        df = self._read(self.g_personnel, f"MATCH (p:{label}) {where} RETURN {', '.join(cols)}")
        people = []
        for r in _rows(df):
            people.append(Person(
                id=r["id"], kind=r["kind"], callsign=r["callsign"], unit_id=r["unit_id"],
                lat=float(r["lat"]), lon=float(r["lon"]), status=r["status"],
                last_update=float(r["last_update"] or 0),
                stock={i: int(r[f"stock_{i}"]) for i in ITEMS if label == "Medic"},
                stock_threshold={i: int(r[f"threshold_{i}"]) for i in ITEMS if label == "Medic"},
            ))
        return people

    def get_person(self, person_id):
        labels = ("Medic", "Soldier") if person_id.startswith("med") else ("Soldier", "Medic")
        for label in labels:
            found = self._query_people(label, f"WHERE p.id = {lit(person_id)}")
            if found:
                return found[0]
        return None

    def update_person(self, person_id, **kw):
        kw.setdefault("last_update", time.time())
        stock = kw.pop("stock", None) or {}
        kw.update({f"stock_{i}": q for i, q in stock.items()})
        self._write(self.g_personnel, [f"MATCH (p) WHERE p.id = {lit(person_id)} SET {sets('p', kw)}"])

    def list_personnel(self):
        return self._query_people("Medic") + self._query_people("Soldier")

    def list_units(self):
        df = self._read(self.g_personnel, "MATCH (u:Unit) RETURN u.id, u.callsign")
        return [Unit(r["id"], r["callsign"]) for r in _rows(df)]

    # logistics
    def _drones_from_rows(self, rows: list[dict], payload_rows: list[dict]) -> list[Drone]:
        drones: dict[str, Drone] = {}
        for r in rows:
            if r["id"] in drones:
                continue
            drones[r["id"]] = Drone(
                id=r["id"], callsign=r["callsign"], depot_id=r["depot_id"],
                lat=float(r["lat"]), lon=float(r["lon"]), speed_mps=float(r["speed_mps"]),
                range_m=float(r["range_m"]), max_range_m=float(r["max_range_m"]),
                capacity=int(r["capacity"]), status=r["status"], claimed_by=r["claimed_by"] or None,
            )
        for r in payload_rows:
            if r["did"] in drones:
                drones[r["did"]].payload[r["item"]] = int(r["qty"])
        return list(drones.values())

    _DRONE_RETURN = ", ".join(f"d.{f}" for f in _DRONE_FIELDS)

    def list_drones(self):
        rows = _rows(self._read(self.g_logistics, f"MATCH (d:Drone) RETURN {self._DRONE_RETURN}"))
        pay = self._read(self.g_logistics,
                         "MATCH (d:Drone)-[k:CARRIES]->(s:SupplyItem) RETURN d.id, s.id, k.qty")
        pay_rows = [{"did": a, "item": b, "qty": c} for a, b, c in pay.itertuples(index=False, name=None)]
        return self._drones_from_rows(rows, pay_rows)

    def get_drone(self, drone_id):
        return next((d for d in self.list_drones() if d.id == drone_id), None)

    def find_candidate_drones(self, items):
        # One round trip: every CARRIES edge of every free drone, filtered by quantity in Python.
        df = self._read(self.g_logistics,
                        "MATCH (d:Drone)-[k:CARRIES]->(s:SupplyItem) "
                        "WHERE d.status = 'IDLE' AND d.claimed_by = '' "
                        f"RETURN {self._DRONE_RETURN}, s.id, k.qty")
        rows, pay_rows = [], []
        for values in df.itertuples(index=False, name=None):
            r = dict(zip(_DRONE_FIELDS, values[:len(_DRONE_FIELDS)]))
            rows.append(r)
            pay_rows.append({"did": r["id"], "item": values[-2], "qty": values[-1]})
        return [d for d in self._drones_from_rows(rows, pay_rows) if d.carries(items)]

    def list_depots(self):
        df = self._read(self.g_logistics, "MATCH (d:Depot) RETURN d.id, d.name, d.lat, d.lon")
        return [Depot(r["id"], r["name"], float(r["lat"]), float(r["lon"])) for r in _rows(df)]

    def list_no_fly_zones(self):
        df = self._read(self.g_logistics, "MATCH (z:NoFlyZone) RETURN z.id, z.name, z.polygon_json")
        return [NoFlyZone(r["id"], r["name"], [tuple(p) for p in json.loads(r["polygon_json"])])
                for r in _rows(df)]

    def _payload_queries(self, drone_id: str, payload: dict[str, int], existing: dict[str, int]) -> list[str]:
        qs = []
        for item, qty in payload.items():
            if item in existing:
                qs.append(f"MATCH (d:Drone)-[k:CARRIES]->(s:SupplyItem) WHERE d.id = {lit(drone_id)} "
                          f"AND s.id = {lit(item)} SET k.qty = {int(qty)}")
            else:
                qs.append(f"MATCH (d:Drone), (s:SupplyItem) WHERE d.id = {lit(drone_id)} "
                          f"AND s.id = {lit(item)} CREATE (d)-[:CARRIES {{qty: {int(qty)}}}]->(s)")
        return qs

    def update_drone(self, drone_id, **kw):
        qs = []
        payload = kw.pop("payload", None)
        if kw:
            qs.append(f"MATCH (d:Drone) WHERE d.id = {lit(drone_id)} SET {sets('d', kw)}")
        if payload:
            current = self.get_drone(drone_id)
            qs += self._payload_queries(drone_id, payload, current.payload if current else {})
        if qs:
            self._write(self.g_logistics, qs)

    # dispatch lifecycle
    def claim_drone(self, drone_id, request_id):
        with self._lock:  # check-then-set must not interleave; TuringDB won't catch conflicts
            df = self._read(self.g_logistics,
                            f"MATCH (d:Drone) WHERE d.id = {lit(drone_id)} RETURN d.status, d.claimed_by")
            if df.empty:
                return False
            status, claimed = df.iloc[0, 0], df.iloc[0, 1]
            if status != "IDLE" or (claimed or ""):
                return False
            self.update_drone(drone_id, status="EN_ROUTE", claimed_by=request_id)
            return True

    def release_drone(self, drone_id):
        self.update_drone(drone_id, status="IDLE", claimed_by=None)

    def create_dispatch(self, dispatch):
        edge = {
            "request_id": dispatch.request_id, "eta_s": dispatch.eta_s,
            "distance_m": dispatch.distance_m, "ts": dispatch.ts,
            "latency_ms": dispatch.latency_ms, "status": dispatch.status,
            "items_json": json.dumps(dispatch.items), "route_json": json.dumps(dispatch.route),
        }
        rid = lit(dispatch.recipient_id)
        self._write(self.g_logistics, [
            f"MERGE (r:Recipient {{id: {rid}}})",
            f"MATCH (d:Drone), (r:Recipient) WHERE d.id = {lit(dispatch.drone_id)} AND r.id = {rid} "
            f"CREATE (d)-[:DISPATCHED_TO {props(edge)}]->(r)",
        ])

    def _query_dispatches(self, where: str = "") -> list[Dispatch]:
        df = self._read(self.g_logistics,
                        f"MATCH (d:Drone)-[x:DISPATCHED_TO]->(r:Recipient) {where} "
                        "RETURN d.id, r.id, x.request_id, x.eta_s, x.distance_m, x.ts, x.latency_ms, "
                        "x.status, x.items_json, x.route_json")
        out = []
        for (did, rid, req, eta, dist, ts, lat_ms, status, items_j, route_j) in df.itertuples(index=False, name=None):
            out.append(Dispatch(
                request_id=req, drone_id=did, recipient_id=rid, items=json.loads(items_j),
                eta_s=float(eta), distance_m=float(dist), route=[tuple(p) for p in json.loads(route_j)],
                latency_ms=float(lat_ms), ts=float(ts), status=status,
            ))
        return out

    def list_dispatches(self):
        return self._query_dispatches()

    def complete_dispatch(self, request_id, ts=None):
        with self._lock:
            found = self._query_dispatches(f"WHERE x.request_id = {lit(request_id)}")
            if not found or found[0].status == "DELIVERED":
                return found[0] if found else None
            disp = found[0]
            drone = self.get_drone(disp.drone_id)
            new_payload = {i: max(0, drone.payload.get(i, 0) - q) for i, q in disp.items.items()}
            self._write(self.g_logistics, [
                f"MATCH (d:Drone)-[x:DISPATCHED_TO]->(r:Recipient) WHERE x.request_id = {lit(request_id)} "
                f"SET {sets('x', {'status': 'DELIVERED', 'delivered_ts': ts or time.time()})}",
                *self._payload_queries(disp.drone_id, new_payload, drone.payload),
            ])
            person = self.get_person(disp.recipient_id)
            if person is not None and person.kind == "MEDIC":
                self.update_person(person.id, stock={i: person.stock.get(i, 0) + q for i, q in disp.items.items()})
            disp.status = "DELIVERED"
            return disp
