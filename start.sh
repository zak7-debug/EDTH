#!/usr/bin/env bash
# One-command start: ./start.sh            (TuringDB, the real store)
#                    EDTH_REPO=memory ./start.sh   (no database, in-memory fallback)
set -euo pipefail
cd "$(dirname "$0")"

# Python 3.11+ (macOS's built-in python3 is often 3.9: brew install python@3.12, or use pyenv).
PY="${PYTHON:-python3}"
if ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  for p in python3.13 python3.12 python3.11; do command -v "$p" >/dev/null && PY="$p" && break; done
fi
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Need Python 3.11+ (found $("$PY" --version 2>&1)). On a Mac: brew install python@3.12"; exit 1; }
if [ -d .venv ] && ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
  echo "Rebuilding .venv with $("$PY" --version)"; rm -rf .venv
fi
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
# Re-install whenever requirements.txt changes (a pulled branch may add a package).
if ! cmp -s requirements.txt .venv/.requirements.txt; then
  .venv/bin/pip install -q -r requirements.txt && cp requirements.txt .venv/.requirements.txt
fi
source .venv/bin/activate

export EDTH_REPO="${EDTH_REPO:-turing}"
if [ "$EDTH_REPO" = "turing" ]; then
  # In-memory server: writes take ~6 ms instead of ~80 ms; we reseed on every start anyway.
  turingdb stop >/dev/null 2>&1 || true
  turingdb start -demon -in-memory -ui
  trap 'turingdb stop >/dev/null 2>&1 || true' EXIT
fi

echo "Dashboard: http://localhost:8000   TuringDB UI: http://localhost:8080"
# HOOK: which API runs. The dev server is complete today; set EDTH_APP=backend.app.main:app once
# Sasank's main.py has /events and /ws.
uvicorn "${EDTH_APP:-backend.app.dev_server:app}" --host 0.0.0.0 --port "${PORT:-8000}"
