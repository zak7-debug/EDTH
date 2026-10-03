"""Map layers for the dashboard, cached on disk so the demo works offline.

    GET /tiles/{layer}/{z}/{x}/{y}.{ext}     layer: sat (satellite, jpg), roads (png), labels (png)

The dark basemap stays on dev_server's /tiles/{z}/{x}/{y}.png. A tile we don't have yet is fetched once
from its upstream and kept under frontend/tiles/<layer>/; scripts/fetch_tiles.py fills the cache in one go
while online. None of these need an API key.

- sat: Esri World Imagery. roads: Esri World Transportation (roads and tracks, transparent, drawn over
  the imagery). labels: Esri World Boundaries and Places (towns and borders, transparent).
  The map shows Esri's attribution. This is a small one-off cache for a demo, not a tile server.

Searchable tags: TUNE (numbers to adjust), HOOK (integration points).
"""
from __future__ import annotations

import asyncio
import os
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

TILE_DIR = Path(os.environ.get("EDTH_TILE_DIR", Path(__file__).resolve().parents[2] / "frontend" / "tiles"))
_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/{}/MapServer/tile/{{z}}/{{y}}/{{x}}"
# TUNE: layer -> (upstream URL, file extension). Esri orders tiles z/y/x.
LAYERS = {
    "sat": (_ESRI.format("World_Imagery"), "jpg"),
    "roads": (_ESRI.format("Reference/World_Transportation"), "png"),
    "labels": (_ESRI.format("Reference/World_Boundaries_and_Places"), "png"),
}
MEDIA = {"jpg": "image/jpeg", "png": "image/png"}

router = APIRouter()
_misses: set[str] = set()  # tiles that failed while offline: don't retry on every pan


def tile_path(layer: str, z: int, x: int, y: int) -> Path:
    return TILE_DIR / layer / str(z) / str(x) / f"{y}.{LAYERS[layer][1]}"


def fetch(layer: str, z: int, x: int, y: int, timeout: float = 6) -> bytes:
    url = LAYERS[layer][0].format(z=z, x=x, y=y)
    req = urllib.request.Request(url, headers={"User-Agent": "EDTH-hackathon-demo/1.0 (tile cache)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # TUNE: give up quickly when offline
        return r.read()


@router.get("/tiles/{layer}/{z}/{x}/{y}.{ext}")
async def tile(layer: str, z: int, x: int, y: int, ext: str):
    """HOOK: satellite, roads and labels layers. 404 when offline and not cached: the dashboard then
    falls back to fetching the tile from Esri directly, then to its offline backdrop."""
    if layer not in LAYERS or ext != LAYERS[layer][1]:
        raise HTTPException(404, f"unknown map layer {layer}.{ext}")
    path = tile_path(layer, z, x, y)
    headers = {"Cache-Control": "max-age=86400"}
    if path.exists():
        return FileResponse(path, media_type=MEDIA[ext], headers=headers)
    key = f"{layer}/{z}/{x}/{y}"
    if key in _misses:
        raise HTTPException(404, "tile not cached and upstream unreachable")
    try:
        data = await asyncio.to_thread(fetch, layer, z, x, y)
    except Exception:
        _misses.add(key)
        raise HTTPException(404, "tile not cached and upstream unreachable")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return Response(data, media_type=MEDIA[ext], headers=headers)
