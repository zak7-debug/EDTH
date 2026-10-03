"""Download the real roads under the map's road overlay once, so trucks and ambulances drive on them.

How it fits the product:
- backend/app/roads.py routes casualty evacuations and the in-sector truck legs of the supply chain on
  backend/data/roads.json when it exists, and on an invented lattice of roads and field tracks when
  it doesn't. This script makes that file from OpenStreetMap (the same roads the map's road overlay
  draws). Run it once on the demo laptop while it is online, then restart the server:

      python scripts/fetch_roads.py                  # ~1-3 minutes, a few MB
      python scripts/fetch_roads.py --from osm.json  # an Overpass JSON saved some other way

- Only public roads are fetched (motorway down to unclassified; field tracks with --tracks). The sites,
  units and routes between them stay invented (see seed.py). The file is OpenStreetMap data (ODbL):
  keep the attribution if it is shown anywhere. Commit it so the other laptops get it too.

Searchable tags: TUNE (numbers to adjust), DEMO (demo behaviour).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.roads import BBOX, ROADS_FILE, compile_osm  # noqa: E402

# TUNE: public Overpass servers, tried in order.
SERVERS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
CLASSES = "motorway|trunk|primary|secondary|tertiary|unclassified"


def query(tracks: bool) -> str:
    s, w, n, e = BBOX
    kinds = CLASSES + ("|track" if tracks else "")
    return (f'[out:json][timeout:300];way["highway"~"^({kinds})(_link)?$"]({s},{w},{n},{e});'
            f'(._;>;);out qt;')


def download(tracks: bool) -> dict:
    body = urllib.parse.urlencode({"data": query(tracks)}).encode()
    for url in SERVERS:
        try:
            print(f"asking {url} ...")
            req = urllib.request.Request(url, data=body, headers={"User-Agent": "edth-demo/1.0 (one-off)"})
            with urllib.request.urlopen(req, timeout=360) as r:
                return json.load(r)
        except Exception as exc:  # try the next server
            print(f"  failed: {exc}")
    raise SystemExit("no Overpass server answered: check the internet connection and try again")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="src", help="use a saved Overpass JSON instead of downloading")
    ap.add_argument("--tracks", action="store_true", help="also field tracks (bigger file, slower routing)")
    ap.add_argument("--out", default=str(ROADS_FILE))
    args = ap.parse_args()
    osm = json.loads(Path(args.src).read_text()) if args.src else download(args.tracks)
    data = compile_osm(osm)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, separators=(",", ":")))
    print(f"{len(data['nodes'])} road points, {len(data['ways'])} roads -> {out} "
          f"({out.stat().st_size / 1e6:.1f} MB). Restart the server to use them.")
    return 0 if data["nodes"] else 1


if __name__ == "__main__":
    sys.exit(main())
