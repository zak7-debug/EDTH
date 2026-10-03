"""Download the map background once so the dashboard works with no internet.

How it fits the product:
- The dashboard asks the backend for /tiles/{z}/{x}/{y}.png. The backend serves them from
  frontend/tiles/ and fetches (then keeps) any tile it doesn't have. This script fills that folder
  in one go for the two views the demo uses: the sector (zoomed in) and the whole supply chain
  (Poland to the front, zoomed out). Run it once on the demo laptop while it is online:

      python scripts/fetch_tiles.py            # every layer: ~1,500 tiles each, satellite ~40 MB, ~15 minutes
      python scripts/fetch_tiles.py --layers sat,roads   # just some layers
      python scripts/fetch_tiles.py --dry-run  # just count them

- Layers: dark (CARTO's dark basemap, OpenStreetMap data), sat (Esri World Imagery), roads (Esri World
  Transportation) and labels (Esri place names). None needs an API key; the map keeps their
  attribution. This is a small one-off cache for a demo: keep the rate limit and don't widen the
  areas much. frontend/tiles/ is git-ignored.

Searchable tags: TUNE (numbers to adjust), DEMO (demo behaviour).
"""
from __future__ import annotations

import argparse
import math
import sys
import time
import urllib.request
from pathlib import Path

TILE_DIR = Path(__file__).resolve().parents[1] / "frontend" / "tiles"
UPSTREAM = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/{}/MapServer/tile/{{z}}/{{y}}/{{x}}"
# layer -> (upstream, cache folder under frontend/tiles/, extension). Must match backend/app/tiles.py.
LAYERS = {
    "dark": (UPSTREAM, "", "png"),
    "sat": (_ESRI.format("World_Imagery"), "sat", "jpg"),
    "roads": (_ESRI.format("Reference/World_Transportation"), "roads", "png"),
    "labels": (_ESRI.format("Reference/World_Boundaries_and_Places"), "labels", "png"),
}
# DEMO / TUNE: (name, south, west, north, east, min zoom, max zoom).
AREAS = [
    ("sector", 47.50, 35.25, 47.90, 35.95, 9, 14),  # squads, launch sites, Role 1 and Role 2 (map opens at z12)
    ("zaporizhzhia-dnipro", 47.30, 34.60, 48.70, 36.30, 8, 11),  # forward point, Dnipro hubs and hospital
    ("supply-chain", 46.00, 21.00, 51.50, 37.50, 5, 8),  # the "Show supply chain on map" view
]
RATE_PER_S = 8  # TUNE: be polite to the tile server


def tile_xy(lat: float, lon: float, z: int) -> tuple[int, int]:
    """Slippy-map tile containing (lat, lon) at zoom z."""
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def tiles():
    seen = set()
    for _, s, w, n, e, z0, z1 in AREAS:
        for z in range(z0, z1 + 1):
            x0, y0 = tile_xy(n, w, z)
            x1, y1 = tile_xy(s, e, z)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    if (z, x, y) not in seen:
                        seen.add((z, x, y))
                        yield z, x, y


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="count tiles, download nothing")
    ap.add_argument("--layers", default="dark,sat,roads,labels", help="comma-separated: " + ",".join(LAYERS))
    args = ap.parse_args()
    status = 0
    for layer in args.layers.split(","):
        print(f"[{layer}]")
        status |= fetch_layer(layer, args.dry_run)
    return status


def fetch_layer(layer: str, dry_run: bool) -> int:
    upstream, folder, ext = LAYERS[layer]
    root = TILE_DIR / folder if folder else TILE_DIR
    todo = [t for t in tiles() if not (root / str(t[0]) / str(t[1]) / f"{t[2]}.{ext}").exists()]
    total = len(list(tiles()))
    print(f"{total} tiles in the demo areas, {total - len(todo)} cached already, {len(todo)} to fetch")
    if dry_run or not todo:
        return 0
    failed = 0
    for i, (z, x, y) in enumerate(todo, 1):
        path = root / str(z) / str(x) / f"{y}.{ext}"
        url = upstream.format(s="abcd"[(x + y) % 4], z=z, x=x, y=y)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "EDTH-hackathon-demo/1.0 (one-off tile cache)"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = r.read()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except Exception as e:
            failed += 1
            if failed <= 3:
                print(f"  failed {z}/{x}/{y}: {e}")
            if failed == 10 and i == 10:
                print("No tile came through: is this machine online?")
                return 1
        if i % 100 == 0:
            print(f"  {i}/{len(todo)}")
        time.sleep(1 / RATE_PER_S)
    print(f"Done: {len(todo) - failed} fetched, {failed} failed. Tiles are in {TILE_DIR}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
