"""The dev server with voice reports switched on, without editing dev_server.py:

    EDTH_APP=backend.app.voice_server:app ./start.sh

Then open http://localhost:8000/audio/radio.html next to the dashboard. Once Sasank adds
`app.include_router(voice.router)` to dev_server.py (above the static mount), this file can go.
"""
from __future__ import annotations

from . import voice
from .dev_server import app

if not any(getattr(r, "path", "") == "/voice" for r in app.router.routes):
    app.include_router(voice.router)
    app.router.routes.sort(key=lambda r: getattr(r, "name", "") == "frontend")  # static mount at "/" stays last
