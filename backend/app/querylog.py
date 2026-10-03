"""Query log: which graph queries a dispatch decision ran, and how long each took.

How it fits the product:
- TuringRepo calls `record()` for every read and every write change. Nothing is kept unless a
  `capture()` block is open, so the log costs nothing on paths nobody is watching.
- The API opens `capture()` around `engine.handle()` (the decision) and `engine.record()` (the
  writes), then broadcasts a `query_log` WebSocket message. The dashboard's "Graph queries" panel
  shows it, so the judges see the actual Cypher TuringDB ran and its milliseconds.

Searchable tags: TUNE (numbers to adjust), HOOK (integration points).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

MAX_CYPHER_CHARS = 500  # TUNE: long CREATE / SET statements are cut to this for the panel

_active: ContextVar[Optional[list]] = ContextVar("edth_querylog", default=None)


@contextmanager
def capture():
    """Collect every query run inside the block: `with capture() as qs: ...` -> list of dicts."""
    entries: list[dict] = []
    token = _active.set(entries)
    try:
        yield entries
    finally:
        _active.reset(token)


def record(graph: str, cypher: str, ms: float, rows: Optional[int] = None, kind: str = "read") -> None:
    """HOOK: called by TuringRepo after each query or write change."""
    entries = _active.get()
    if entries is None:
        return
    text = " ".join(cypher.split())
    if len(text) > MAX_CYPHER_CHARS:
        text = text[:MAX_CYPHER_CHARS] + " …"
    entries.append({"graph": graph, "kind": kind, "cypher": text, "ms": round(ms, 2), "rows": rows})
