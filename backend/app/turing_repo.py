"""TuringRepo: GraphRepo backed by two TuringDB graphs, `personnel` and `logistics`.

Schema (also in contracts/schema.md):
  personnel:  (:Soldier|:Medic {id, kind, callsign, unit_id, lat, lon, status, last_update,
                                medics also stock_<item>, threshold_<item>})
              (:Unit {id, callsign})
              (soldier)-[:MEMBER_OF]->(unit), (medic)-[:ATTACHED_TO]->(unit)
  logistics:  (:Drone {id, callsign, depot_id, lat, lon, speed_mps, range_m, max_range_m,
                       capacity, status, claimed_by})          claimed_by '' means unclaimed
              (:Depot {id, name, lat, lon}), (:SupplyItem {id}), (:NoFlyZone {id, name, polygon_json})
              (:Supplier|:DistributionCentre|:Hospital {id, kind, name, lat, lon, role, beds, beds_used, status})
              (supplier|dc|hospital|depot)-[:STOCKS {qty}]->(item)
              (supplier)-[:SUPPLIES {lead_time_min, mode}]->(dc|hospital)-[:SUPPLIES]->(depot)
              (:Recipient {id})  stand-in for a person, since edges cannot cross graphs
              (drone)-[:BASED_AT]->(depot), (drone)-[:CARRIES {qty}]->(item)
              (drone)-[:DISPATCHED_TO {request_id, eta_s, distance_m, ts, latency_ms, status,
                                       items_json, route_json}]->(recipient | hospital)
                     a drone flying a casualty's kit ahead of them points straight at the hospital
              (recipient)-[:EVACUATED_TO {evac_id, severity, status, ts, eta_s, distance_m,
                                          kit_json, shortfall_json, route_json}]->(hospital)

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

from . import querylog
from .models import ITEMS, Depot, Dispatch, Drone, Evacuation, Facility, NoFlyZone, Person, SupplyLink, Unit
from .seed import SeedData

# Graph names. TURINGDB_GRAPH_PREFIX (env) prepends a prefix, e.g. tests use 'test_'.
PERSONNEL = "personnel"
LOGISTICS = "logistics"

# Drone / Person properties stored on the node. A new dataclass field must be listed here to be
# saved and read back (and added to FLOAT_FIELDS below if it is a float).
_DRONE_FIELDS = ("id", "callsign", "depot_id", "lat", "lon", "speed_mps", "range_m",
                 "max_range_m", "capacity", "status", "claimed_by")
_PERSON_FIELDS = ("id", "kind", "callsign", "unit_id", "lat", "lon", "status", "last_update")


# Builds Cypher literals by hand: the client has no query parameters. All values come from our
# own code or validated events; ids are escaped here.
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
# TuringDB fixes a property's type on first write, so floats must always be written as floats.
FLOAT_FIELDS = {"lat", "lon", "speed_mps", "range_m", "max_range_m", "last_update", "lost_ts",
                "eta_s", "distance_m", "ts", "latency_ms", "delivered_ts", "lead_time_min", "closed_ts"}
INT_FIELDS = {"capacity", "qty", "beds", "beds_used"}
FACILITY_LABELS = {"SUPPLIER": "Supplier", "DISTRIBUTION_CENTRE": "DistributionCentre", "HOSPITAL": "Hospital"}


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
            t = time.perf_counter()
            self.db.set_graph(graph)
            df = self.db.query(q)
            querylog.record(graph, q, (time.perf_counter() - t) * 1000, len(df), "read")  # HOOK: query log panel
            return df

    # Every write: CHANGE NEW -> query, COMMIT (each) -> CHANGE SUBMIT -> back to main.
    # ~6 ms on the in-memory server, ~85 ms on a disk-backed one (docs/turingdb-notes.md).
    def _write(self, graph: str, queries: Iterable[str]) -> None:
        """Run queries in one change and submit. COMMIT after each so later ones can MATCH."""
        queries = list(queries)
        with self._lock:
            t = time.perf_counter()
            self.db.set_graph(graph)
            self.db.new_change()  # also checks the client out onto the new change
            try:
                for q in queries:
                    self.db.query(q)
                    self.db.query("COMMIT")
                self.db.query("CHANGE SUBMIT")
            finally:
                self.db.checkout()
            # One log entry per change, timed end to end (new change -> submit).
            querylog.record(graph, " ; ".join(queries), (time.perf_counter() - t) * 1000, None, "write")

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

    # Wipes and reloads both graphs from seed.py in two big CREATE statements (~100-400 ms).
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
                parts += self._stocks_parts(dep.id, dep.stock)
            for f in seed.facilities:
                fprops = {'id': f.id, 'kind': f.kind, 'name': f.name, 'lat': f.lat, 'lon': f.lon,
                          'role': f.role, 'beds': f.beds, 'beds_used': f.beds_used, 'status': f.status}
                parts.append(f"({var(f.id)}:{FACILITY_LABELS[f.kind]} {props(fprops)})")
                parts += self._stocks_parts(f.id, f.stock)
            for link in seed.supply_links:
                parts.append(f"({var(link.src_id)})-[:SUPPLIES "
                             f"{props({'lead_time_min': link.lead_time_min, 'mode': link.mode})}]->({var(link.dst_id)})")
            for z in seed.no_fly_zones:
                parts.append(f"({var(z.id)}:NoFlyZone {props({'id': z.id, 'name': z.name, 'polygon_json': json.dumps(z.polygon)})})")
            for d in seed.drones:
                parts.append(f"({var(d.id)}:Drone {props({f: getattr(d, f) for f in _DRONE_FIELDS})})")
                parts.append(f"({var(d.id)})-[:BASED_AT]->({var(d.depot_id)})")
                for item, qty in d.payload.items():
                    parts.append(f"({var(d.id)})-[:CARRIES {{qty: {int(qty)}}}]->({var('item-' + item)})")
            self._write(self.g_logistics, ["CREATE " + ", ".join(parts)])

    @staticmethod
    def _stocks_parts(node_id: str, stock: dict[str, int]) -> list[str]:
        return [f"({var(node_id)})-[:STOCKS {{qty: {int(q)}}}]->({var('item-' + i)})" for i, q in stock.items()]

    @staticmethod
    def _stock_queries(node_id: str, stock: dict[str, int], existing: dict[str, int]) -> list[str]:
        """SET the STOCKS edges that exist, CREATE the ones that don't (same idea as _payload_queries)."""
        qs = []
        for item, qty in stock.items():
            if item in existing:
                qs.append(f"MATCH (n)-[k:STOCKS]->(s:SupplyItem) WHERE n.id = {lit(node_id)} "
                          f"AND s.id = {lit(item)} SET k.qty = {int(qty)}")
            else:
                qs.append(f"MATCH (n), (s:SupplyItem) WHERE n.id = {lit(node_id)} "
                          f"AND s.id = {lit(item)} CREATE (n)-[:STOCKS {{qty: {int(qty)}}}]->(s)")
        return qs

    def _node_stock(self, node_id: str) -> dict[str, int]:
        df = self._read(self.g_logistics, "MATCH (n)-[k:STOCKS]->(s:SupplyItem) "
                                          f"WHERE n.id = {lit(node_id)} RETURN s.id, k.qty")
        return {item: int(qty) for item, qty in df.itertuples(index=False, name=None)}

    # HOOK: every stock movement at a launch site or facility (drone reloads, restock orders
    # leaving and arriving, kit flown to a hospital, a patient treated) is one read + one change.
    def adjust_stock(self, node_id, delta):
        with self._lock:  # read-modify-write: must not interleave with another adjustment
            current = self._node_stock(node_id)
            new = {i: max(0, current.get(i, 0) + int(q)) for i, q in delta.items()}
            self._write(self.g_logistics, self._stock_queries(node_id, new, current))
            return new

    def _stock_by_node(self) -> dict[str, dict[str, int]]:
        df = self._read(self.g_logistics, "MATCH (n)-[k:STOCKS]->(s:SupplyItem) RETURN n.id, s.id, k.qty")
        out: dict[str, dict[str, int]] = {}
        for nid, item, qty in df.itertuples(index=False, name=None):
            out.setdefault(nid, {})[item] = int(qty)
        return out

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

    # HOT PATH: one 2-hop query over every free drone's CARRIES edges; quantities filtered in Python.
    # TUNE: to cut rows, add `AND s.id IN [...]` and fetch the full payload only for the winner.
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
        stock = self._stock_by_node()
        return [Depot(r["id"], r["name"], float(r["lat"]), float(r["lon"]), stock.get(r["id"], {}))
                for r in _rows(df)]

    def _facility(self, r: dict, stock: dict[str, int]) -> Facility:
        return Facility(r["id"], r["kind"], r["name"], float(r["lat"]), float(r["lon"]), stock,
                        role=r["role"] or "", beds=int(r["beds"] or 0), beds_used=int(r.get("beds_used") or 0),
                        status=r["status"] if isinstance(r["status"], str) and r["status"] else "OPERATIONAL")

    _FACILITY_RETURN = "f.id, f.kind, f.name, f.lat, f.lon, f.role, f.beds, f.beds_used, f.status"

    def set_facility_status(self, facility_id, status):
        self._write(self.g_logistics, [f"MATCH (f) WHERE f.id = {lit(facility_id)} SET f.status = {lit(status)}"])

    def add_facility(self, facility, links):
        fprops = {'id': facility.id, 'kind': facility.kind, 'name': facility.name, 'lat': facility.lat,
                  'lon': facility.lon, 'role': facility.role, 'beds': facility.beds, 'beds_used': facility.beds_used,
                  'status': facility.status}
        qs = [f"CREATE (f:{FACILITY_LABELS[facility.kind]} {props(fprops)})"]  # committed before the MATCHes below
        qs += self._stock_queries(facility.id, facility.stock, {})
        qs += [f"MATCH (a), (b) WHERE a.id = {lit(link.src_id)} AND b.id = {lit(link.dst_id)} "
               f"CREATE (a)-[:SUPPLIES {props({'lead_time_min': link.lead_time_min, 'mode': link.mode})}]->(b)"
               for link in links]
        self._write(self.g_logistics, qs)

    def list_facilities(self):
        stock = self._stock_by_node()
        out = []
        for label in FACILITY_LABELS.values():
            df = self._read(self.g_logistics, f"MATCH (f:{label}) RETURN {self._FACILITY_RETURN}")
            out += [self._facility(r, stock.get(r["id"], {})) for r in _rows(df)]
        return out

    def list_supply_links(self):
        df = self._read(self.g_logistics, "MATCH (a)-[l:SUPPLIES]->(b) RETURN a.id, b.id, l.lead_time_min, l.mode")
        return [SupplyLink(a, b, float(t), m) for a, b, t, m in df.itertuples(index=False, name=None)]

    # Supply-chain query used for the 'no drone' suggestion and the pitch's graph-query panel.
    def find_resupply_sources(self, depot_id, items):
        # One 2-hop query: who supplies this depot, and what do they hold.
        df = self._read(self.g_logistics,
                        "MATCH (s:SupplyItem)<-[k:STOCKS]-(f)-[l:SUPPLIES]->(d:Depot) "
                        f"WHERE d.id = {lit(depot_id)} "
                        f"RETURN {self._FACILITY_RETURN}, l.lead_time_min, l.mode, s.id, k.qty")
        found: dict[str, tuple[Facility, SupplyLink]] = {}
        keys = ("id", "kind", "name", "lat", "lon", "role", "beds", "beds_used", "status")
        for (*fvals, lead, mode, item, qty) in df.itertuples(index=False, name=None):
            r = dict(zip(keys, fvals))
            if not isinstance(r["kind"], str) or not r["kind"]:
                continue  # another launch site relaying stock (DRONE link): not an upstream facility
            if r["id"] not in found:
                found[r["id"]] = (self._facility(r, {}), SupplyLink(r["id"], depot_id, float(lead), mode))
            found[r["id"]][0].stock[item] = int(qty)
        ok = [fl for fl in found.values() if all(fl[0].stock.get(i, 0) >= q for i, q in items.items())]
        return sorted(ok, key=lambda fl: fl[1].lead_time_min)

    def list_no_fly_zones(self):
        df = self._read(self.g_logistics, "MATCH (z:NoFlyZone) RETURN z.id, z.name, z.polygon_json")
        return [NoFlyZone(r["id"], r["name"], [tuple(p) for p in json.loads(r["polygon_json"])])
                for r in _rows(df)]

    def add_no_fly_zone(self, zone):
        # MERGE on id so redrawing a zone replaces its polygon instead of duplicating it.
        self._write(self.g_logistics, [
            f"MERGE (z:NoFlyZone {{id: {lit(zone.id)}}}) "
            f"SET z.name = {lit(zone.name)}, z.polygon_json = {lit(json.dumps(zone.polygon))}"])

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
    # Atomic within this process only. If the app ever runs as several processes, move the claim to
    # a single owner (one dispatcher process) because TuringDB won't reject a conflicting write.
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

    def lose_drone(self, drone_id, request_id=None, ts=None):
        with self._lock:
            drone = self.get_drone(drone_id)
            qs = [f"MATCH (d:Drone) WHERE d.id = {lit(drone_id)} SET d.status = 'LOST', d.claimed_by = ''",
                  *self._payload_queries(drone_id, {k: 0 for k in drone.payload}, drone.payload)]
            if request_id:
                qs.append(f"MATCH (d:Drone)-[x:DISPATCHED_TO]->(r) WHERE x.request_id = {lit(request_id)} "
                          f"SET {sets('x', {'status': 'LOST', 'lost_ts': ts or time.time()})}")
            self._write(self.g_logistics, qs)

    # Edges can't cross graphs, so the drone points at a Recipient stand-in node holding the person's id.
    def create_dispatch(self, dispatch):
        edge = {
            "request_id": dispatch.request_id, "eta_s": dispatch.eta_s,
            "distance_m": dispatch.distance_m, "ts": dispatch.ts,
            "latency_ms": dispatch.latency_ms, "status": dispatch.status,
            "items_json": json.dumps(dispatch.items), "route_json": json.dumps(dispatch.route),
        }
        rid = lit(dispatch.recipient_id)
        if _is_person(dispatch.recipient_id):
            qs = [f"MERGE (r:Recipient {{id: {rid}}})",
                  f"MATCH (d:Drone), (r:Recipient) WHERE d.id = {lit(dispatch.drone_id)} AND r.id = {rid} "
                  f"CREATE (d)-[:DISPATCHED_TO {props(edge)}]->(r)"]
        else:  # a hospital or aid station lives in this graph already: point straight at it
            qs = [f"MATCH (d:Drone), (r) WHERE d.id = {lit(dispatch.drone_id)} AND r.id = {rid} "
                  f"CREATE (d)-[:DISPATCHED_TO {props(edge)}]->(r)"]
        self._write(self.g_logistics, qs)

    def _query_dispatches(self, where: str = "") -> list[Dispatch]:
        df = self._read(self.g_logistics,
                        f"MATCH (d:Drone)-[x:DISPATCHED_TO]->(r) {where} "
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
                f"MATCH (d:Drone)-[x:DISPATCHED_TO]->(r) WHERE x.request_id = {lit(request_id)} "
                f"SET {sets('x', {'status': 'DELIVERED', 'delivered_ts': ts or time.time()})}",
                *self._payload_queries(disp.drone_id, new_payload, drone.payload),
            ])
            if not _is_person(disp.recipient_id):  # kit flown ahead of a casualty (evac.py)
                self.adjust_stock(disp.recipient_id, disp.items)
            else:
                person = self.get_person(disp.recipient_id)
                if person is not None and person.kind == "MEDIC":
                    self.update_person(person.id, stock={i: person.stock.get(i, 0) + q for i, q in disp.items.items()})
            disp.status = "DELIVERED"
            return disp

    # casualty evacuation (evac.py). Same cross-graph trick as dispatches: the casualty is a
    # Recipient stand-in, the hospital is the real node, and its beds_used counts the bed.
    def _beds_used(self, facility_id: str) -> int:
        df = self._read(self.g_logistics, f"MATCH (f) WHERE f.id = {lit(facility_id)} RETURN f.beds_used")
        return int(df.iloc[0, 0] or 0) if not df.empty and not _isna(df.iloc[0, 0]) else 0

    def start_evacuation(self, evac):
        edge = {"evac_id": evac.evac_id, "severity": evac.severity, "status": evac.status, "ts": evac.ts,
                "eta_s": evac.eta_s, "distance_m": evac.distance_m, "kit_json": json.dumps(evac.kit),
                "shortfall_json": json.dumps(evac.shortfall), "route_json": json.dumps(evac.route),
                "resupply_request_id": evac.resupply_request_id or ""}
        pid, fid = lit(evac.person_id), lit(evac.facility_id)
        with self._lock:
            used = self._beds_used(evac.facility_id)
            self._write(self.g_logistics, [
                f"MERGE (r:Recipient {{id: {pid}}})",
                f"MATCH (r:Recipient), (f) WHERE r.id = {pid} AND f.id = {fid} "
                f"CREATE (r)-[:EVACUATED_TO {props(edge)}]->(f)",
                f"MATCH (f) WHERE f.id = {fid} SET f.beds_used = {used + 1}",
            ])

    def finish_evacuation(self, evac_id, status="ADMITTED", ts=None):
        with self._lock:
            found = self._query_evacuations(f"WHERE x.evac_id = {lit(evac_id)}")
            if not found:
                return
            qs = [f"MATCH (r:Recipient)-[x:EVACUATED_TO]->(f) WHERE x.evac_id = {lit(evac_id)} "
                  f"SET {sets('x', {'status': status, 'closed_ts': ts or time.time()})}"]
            if status == "DIVERTED":
                fid = found[0].facility_id
                qs.append(f"MATCH (f) WHERE f.id = {lit(fid)} SET f.beds_used = {max(0, self._beds_used(fid) - 1)}")
            self._write(self.g_logistics, qs)

    def _query_evacuations(self, where: str = "") -> list[Evacuation]:
        df = self._read(self.g_logistics,
                        f"MATCH (r:Recipient)-[x:EVACUATED_TO]->(f) {where} "
                        "RETURN x.evac_id, r.id, f.id, x.severity, x.route_json, x.distance_m, x.eta_s, x.ts, "
                        "x.kit_json, x.shortfall_json, x.resupply_request_id, x.status")
        return [Evacuation(evac_id=e, person_id=p, facility_id=f, severity=sev,
                           route=[tuple(pt) for pt in json.loads(route)], distance_m=float(dist),
                           eta_s=float(eta), ts=float(ts), kit=json.loads(kit), shortfall=json.loads(short),
                           resupply_request_id=req or None, status=st)
                for (e, p, f, sev, route, dist, eta, ts, kit, short, req, st) in df.itertuples(index=False, name=None)]

    def list_evacuations(self):
        return self._query_evacuations()


def _is_person(node_id: str) -> bool:
    """Soldiers and medics live in the personnel graph; everything else is a logistics node."""
    return node_id.startswith(("sol-", "med-"))
