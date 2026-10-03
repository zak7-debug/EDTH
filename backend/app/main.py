"""FastAPI entrypoint: `uvicorn backend.app.main:app`.

The working API (POST /events, /ws, tick loop, /scenario, /reset, /state, /health) lives in
dev_server.py. This module re-exports it so there is one app and two valid ways to start it.
Edit dev_server.py, not this file, when changing behaviour.
"""
from __future__ import annotations

from .dev_server import app  # noqa: F401
