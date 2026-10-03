"""FastAPI app. STUB from the foundations step: Sasank owns this file and replaces it with
POST /events, /ws and the tick loop. It exists so ./start.sh runs from day one."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pathlib import Path

from .messages import snapshot
from .repo import get_repo

app = FastAPI(title="EDTH medical resupply")
repo = get_repo()
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


@app.get("/health")
def health():
    return {"ok": True, "repo": type(repo).__name__}


@app.get("/state")
def state():
    return snapshot(repo)["data"]


@app.get("/")
def index():
    page = FRONTEND / "index.html"
    return FileResponse(page) if page.exists() else {"msg": "frontend/index.html not built yet"}
